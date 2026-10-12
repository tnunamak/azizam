"""Rotation keeps principal data visible without widening access to other apps."""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, custom_voices, media_ingestion, media_jobs, ownership, receipt_store, video_job_records, voices_api
from gateway.admin_oidc import AdminSession
from gateway.admin_runtime import runtime_identity_for_app_credential_hash
from gateway.api import create_gateway_router
from gateway.discovery import WorkflowRegistry
from gateway.receipts import RouteReceipt
from gateway.voice_audio import SampleLimits


@pytest.fixture
def owners(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    user = admin_store.upsert_oidc_user("fixture", "azizam-user", path=db)
    first = admin_store.create_app("Azizam one", kind="personal", owner_user_id=user["id"], path=db)
    second = admin_store.create_app("Azizam two", kind="personal", owner_user_id=user["id"], path=db)
    credentials = []
    for app, suffix in ((first, "old"), (first, "new"), (second, "other")):
        credentials.append(admin_store.create_credential_hash(
            subject_kind="app", subject_id=app["id"], hash=f"sha256:fixture-{suffix}", prefix="fixture", path=db,
        ))
    personal = admin_store.create_credential_hash(
        subject_kind="user", subject_id=user["id"], hash="sha256:fixture-personal", prefix="fixture", path=db,
    )
    monkeypatch.setattr(admin_store, "DB_PATH", db)
    monkeypatch.setattr(proxy, "ADMIN_STORE", SimpleNamespace(path=db))
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    contexts = [proxy.AuthContext(identity=runtime_identity_for_app_credential_hash(c["hash"], path=db)) for c in credentials]
    store = custom_voices.VoiceStore(tmp_path / "voices", legacy_owners=lambda owner: ownership.credential_owner_keys(owner, path=db))
    return SimpleNamespace(db=db, user=user, first=first, second=second, credentials=credentials,
                           personal=personal, contexts=contexts, store=store, root=tmp_path)


def make_voice(store, owner, *, consent_id=None):
    return store.create_voice(
        owner, name="Azizam narrator", voice_type="audio_sample", reference_wav=b"RIFF fixture",
        duration_seconds=9, original=None, original_extension="wav", consent_id=consent_id,
        description=None, labels={}, measurements={}, warnings=[],
    )


def test_owner_mapping_and_namespaces(owners):
    old, new, other = owners.contexts
    assert ownership.owner_key(old) == ownership.owner_key(new) == f"app:{owners.first['id']}"
    assert ownership.owner_key(other) != ownership.owner_key(old)
    assert ownership.principal_key("app", "same") != ownership.principal_key("user", "same")
    assert ownership.owner_key(proxy.NO_AUTH_CONTEXT) is None
    assert custom_voices.owner_dir_name(None) == "local"
    service = admin_store.ensure_service_account("azizam-fixture", path=owners.db)
    credential = admin_store.create_credential_hash(
        subject_kind="service_account", subject_id=service["id"], hash="sha256:fixture-service", prefix="fixture", path=owners.db,
    )
    auth = proxy.AuthContext(identity=SimpleNamespace(key_hash=credential["hash"]))
    assert ownership.owner_key(auth) == f"service_account:{service['id']}"
    assert ownership.owner_key(AdminSession(subject=f"service_account:{service['id']}", email="", role="none", issued_at=0, expires_at=1)) == ownership.owner_key(auth)


def test_rotation_voice_http_reads_mutation_and_legacy_duplicates(owners, monkeypatch):
    old, new, other = owners.contexts
    legacy = custom_voices.VoiceStore(owners.store.root)
    consent = legacy.create_consent(owners.credentials[0]["hash"], name="Azizam consent", language="en", recording=b"fixture", extension="wav")
    voice = make_voice(legacy, owners.credentials[0]["hash"], consent_id=consent["id"])
    old_dir = legacy.reference_path(owners.credentials[0]["hash"], voice["id"]).parents[2]
    new_dir = owners.store.root / custom_voices.owner_dir_name(owners.credentials[1]["hash"])
    shutil.copytree(old_dir, new_dir)
    fresh = make_voice(owners.store, ownership.owner_key(old), consent_id=consent["id"])
    assert owners.store.reference_path(ownership.owner_key(old), fresh["id"]).parents[2].name == custom_voices.owner_dir_name(ownership.owner_key(old))
    admin_store.revoke_credential(owners.credentials[0]["id"], path=owners.db)
    assert runtime_identity_for_app_credential_hash(owners.credentials[0]["hash"], path=owners.db) is None
    active = [new]
    app = FastAPI()
    monkeypatch.setattr(proxy, "authenticate_request", lambda request, path: active[0])
    monkeypatch.setattr(proxy, "_enforce_runtime_route_policy", lambda auth, path: None)
    monkeypatch.setattr(proxy, "_voice_store", lambda: owners.store)
    app.add_api_route("/v1/audio/voices", proxy.list_voices, methods=["GET"])
    voices_api.register_voice_routes(app, voices_api.VoiceApiDeps(
        store=lambda: owners.store, sample_limits=SampleLimits,
        authenticate=lambda request, path: active[0], enforce_policy=lambda auth, path: None,
        owner_of=proxy._asset_owner_key, probe_quality=lambda path: None, design_voice=lambda *args: None,
        design_settings=lambda: None, builtin_voice_names=frozenset,
        compatible_engines=lambda: [], quality_warn_below=lambda: 0.45,
    ))
    client = TestClient(app)
    assert client.get(f"/v1/audio/voices/{voice['id']}").status_code == 200
    assert client.get(f"/v1/audio/voices/{voice['id']}/sample").content == b"RIFF fixture"
    page = client.get("/v1/audio/voices?limit=1").json()
    assert page["has_more"] is True
    assert owners.store.usage(ownership.owner_key(new))["voices"] == 2
    assert client.get(f"/v1/audio/voice_consents/{consent['id']}").status_code == 200
    active[0] = other
    assert client.get(f"/v1/audio/voices/{voice['id']}").status_code == 404
    assert client.get(f"/v1/audio/voice_consents/{consent['id']}").status_code == 404
    active[0] = new
    assert client.post(f"/v1/audio/voices/{voice['id']}", json={"name": "Azizam renamed"}).status_code == 200
    assert client.delete(f"/v1/audio/voices/{voice['id']}").status_code == 200
    assert client.get(f"/v1/audio/voices/{voice['id']}").status_code == 404
    assert not (old_dir / "voices" / voice["id"]).exists()
    assert not (new_dir / "voices" / voice["id"]).exists()


def test_rotation_media_and_record_http_scopes(owners, monkeypatch):
    old, new, other = owners.contexts
    principal = ownership.owner_key(old)
    asset = proxy.store_image_bytes(b"Azizam image", owner_key_hash=proxy._asset_owner_key(old))
    assert proxy._read_asset_metadata(asset)["owner_key_hashes"] == [principal]
    legacy_asset = proxy.store_image_bytes(b"Azizam legacy image")
    proxy._write_asset_metadata(legacy_asset, {"owner_key_hashes": [owners.credentials[0]["hash"]]})
    db = owners.root / "media_jobs.db"
    receipts = owners.root / "receipts.db"
    media_jobs.init_db(db)
    receipt_store.init_db(receipts)
    job = media_ingestion.index_sync_assets(request_id="azizam-new", intent="image.generate", app_id=owners.first["id"],
        owner_key_hash=owners.credentials[0]["hash"], assets=[{"asset_id": asset}], path=db)
    assert job.owner_key_hash == principal
    receipt = RouteReceipt(request_id="azizam-new", protocol="openai.images", intent="image.generate", app_id=owners.first["id"], owner_key_hash=owners.credentials[0]["hash"])
    receipt_store.begin(receipt, path=receipts)
    receipt.finish("succeeded")
    receipt_store.finalize(receipt, path=receipts)
    assert receipt_store.get(receipt.request_id, audience="internal", path=receipts)["owner_key_hash"] == principal
    legacy_job = media_jobs.create(media_jobs.MediaJob(request_id="azizam-old", intent="image.generate", owner_key_hash="unmapped-fixture"), path=db)
    with sqlite3.connect(db) as conn:
        row = json.loads(conn.execute("SELECT job_json FROM media_jobs WHERE job_id=?", (legacy_job.job_id,)).fetchone()[0])
        row["owner_key_hash"] = owners.credentials[0]["hash"]
        conn.execute("UPDATE media_jobs SET job_json=? WHERE job_id=?", (json.dumps(row), legacy_job.job_id))
    monkeypatch.setattr(proxy, "_video_job_records", {"azizam-legacy-video": {"owner_key_hash": owners.credentials[0]["hash"]}})
    admin_store.revoke_credential(owners.credentials[0]["id"], path=owners.db)
    assert proxy._asset_access_allowed(asset, new)
    assert proxy._asset_access_allowed(legacy_asset, new)
    assert proxy._video_job_access_allowed("azizam-legacy-video", new)
    assert not proxy._asset_access_allowed(asset, other)
    assert not proxy._asset_access_allowed(legacy_asset, other)
    assert not proxy._video_job_access_allowed("azizam-legacy-video", other)
    active = [new]
    app = FastAPI()
    @app.middleware("http")
    async def auth(request, call_next):
        request.scope["auth_context"] = active[0]
        return await call_next(request)
    app.include_router(create_gateway_router(WorkflowRegistry(), media_job_store_path=db, receipt_store_path=receipts,
        record_scope_provider=proxy._gateway_record_scope, record_authorizer=proxy._gateway_record_authorizer))
    client = TestClient(app)
    for owned in (job, legacy_job):
        assert client.get(f"/gateway/v1/media/jobs/{owned.job_id}").status_code == 200
    assert len(client.get("/gateway/v1/media/jobs").json()["jobs"]) == 2
    assert client.get("/gateway/v1/receipts/azizam-new").status_code == 200
    active[0] = other
    for owned in (job, legacy_job):
        assert client.get(f"/gateway/v1/media/jobs/{owned.job_id}").status_code == 404
    assert client.get("/gateway/v1/media/jobs").json()["jobs"] == []
    assert client.get("/gateway/v1/receipts/azizam-new").status_code == 404


def test_personal_key_and_browser_session_share_user_items(owners):
    key = proxy.AuthContext(identity=SimpleNamespace(key_hash=owners.personal["hash"]))
    session = AdminSession(subject=owners.user["id"], email="", role="none", issued_at=time.time(), expires_at=time.time() + 60)
    assert ownership.owner_key(key) == ownership.owner_key(session) == f"user:{owners.user['id']}"
    voice = make_voice(owners.store, ownership.owner_key(key))
    assert owners.store.get_voice(ownership.owner_key(session), voice["id"])["id"] == voice["id"]
    asset = proxy.store_image_bytes(b"Azizam personal image", owner_key_hash=ownership.owner_key(key))
    assert proxy._asset_access_allowed(asset, session)
    assert not proxy._asset_access_allowed(asset, owners.contexts[2])
    db = owners.root / "media_jobs.db"
    media_jobs.init_db(db)
    job = media_jobs.create(media_jobs.MediaJob(intent="image.generate", owner_key_hash=owners.personal["hash"]), path=db)
    assert job.owner_key_hash == ownership.owner_key(session)
    video_job_records.save("azizam-personal", {"owner_key_hash": owners.personal["hash"]}, path=db)
    assert video_job_records.load_all(path=db)["azizam-personal"]["owner_key_hash"] == ownership.owner_key(session)


def test_unknown_credential_and_local_do_not_share_scoped_assets(owners):
    unknown = proxy.AuthContext(identity=SimpleNamespace(key_hash="sha256:unmapped"))
    assert ownership.readable_owner_keys(unknown) == ("sha256:unmapped",)
    asset = proxy.store_image_bytes(b"Azizam local image")
    assert proxy._asset_access_allowed(asset, proxy.NO_AUTH_CONTEXT)
    assert not proxy._asset_access_allowed(asset, unknown)
    assert not proxy._asset_access_allowed(asset, owners.contexts[0])


def test_authenticated_current_key_is_a_read_alias_without_a_registry_row(owners):
    auth = proxy.AuthContext(identity=proxy.AuthIdentity(name="Azizam imported", key_hash="sha256:fixture-imported", app_id="fixture-imported-app"))
    aliases = ownership.readable_owner_keys(auth)
    assert aliases == ("app:fixture-imported-app", "sha256:fixture-imported")
    asset = proxy.store_image_bytes(b"Azizam imported media")
    proxy._write_asset_metadata(asset, {"owner_key_hashes": [auth.identity.key_hash]})
    assert proxy._asset_access_allowed(asset, auth)
    assert not proxy._asset_access_allowed(asset, owners.contexts[0])

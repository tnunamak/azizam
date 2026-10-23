"""Real-credential regressions for service privacy and legacy Library visibility."""
from __future__ import annotations

import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import proxy
from gateway import accounts, admin_store, media_jobs, me_media, prompt_memory, receipt_store
from gateway.admin_api import AdminContext
from gateway.admin_oidc import AdminSession
from gateway.admin_runtime import AdminSQLiteStore, generate_app_api_key, runtime_identity_for_app_credential_hash
from gateway.runtime_auth import hash_api_key
from gateway.user_runtime import mint_user_app_key
from tests.support.access import service_app


SENTINEL = "zz-r2-sentinel-5c1d"


def _png(text: str | None = None) -> bytes:
    from PIL import Image, PngImagePlugin
    info = PngImagePlugin.PngInfo()
    if text:
        info.add_text("prompt", json.dumps({"6": {"inputs": {"text": text}}}))
        info.add_text("workflow", json.dumps({"nodes": [{"widgets_values": [text]}]}))
    out = io.BytesIO()
    Image.new("RGB", (8, 6), (1, 2, 3)).save(out, format="PNG", pnginfo=info)
    return out.getvalue()


@pytest.fixture
def hh(tmp_path, monkeypatch):
    admin = tmp_path / "data/admin.db"
    jobs = tmp_path / "data/media_jobs.db"
    responses = tmp_path / "data/responses.db"
    receipts = tmp_path / "data/receipts.db"
    admin_store.init_db(admin)
    accounts.initialize(admin)
    media_jobs.init_db(jobs)
    receipt_store.init_db(receipts)
    proxy.responses_store.init_db(responses)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    monkeypatch.setattr(prompt_memory, "DB_PATH", jobs)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts)
    monkeypatch.setattr(proxy.responses_store, "DB_PATH", responses)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "data/payloads")
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOADS", {})
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOAD_SIZES", {})
    owner = admin_store.upsert_oidc_user("iss", "owner", email="owner@x.test", path=admin)
    robin = admin_store.upsert_oidc_user("iss", "robin", email="robin@x.test", path=admin)
    robin_app = admin_store.create_app("Try", kind="personal", owner_user_id=robin["id"], access_plan="person", path=admin)
    store = AdminSQLiteStore(admin)
    owner_ctx = AdminContext(subject=owner["id"], actor_id=owner["id"], actor_kind="user", roles=frozenset({"owner"}))
    return SimpleNamespace(root=tmp_path, admin=admin, jobs=jobs, receipts=receipts, owner=owner, robin=robin,
                           robin_app=robin_app, store=store, owner_ctx=owner_ctx)


def _person_for(path, app_id):
    """The App's accountable person, who is the only actor that may issue its key (owner visibility step 2)."""
    owner_id = admin_store.get_app(app_id, path=path)["owner_user_id"]
    return AdminContext(subject=owner_id, actor_id=owner_id, actor_kind="user", roles=frozenset({"owner"}))


def _ctx_for(api_key: str, admin: Path) -> proxy.AuthContext:
    identity = runtime_identity_for_app_credential_hash(hash_api_key(api_key), path=admin)
    assert identity is not None
    return proxy.AuthContext(identity=identity)


def _robin_key(hh) -> str:
    session = AdminSession(subject=hh.robin["id"], role="none", email=None, issued_at=1, expires_at=4_000_000_000)
    return mint_user_app_key(session, hh.robin_app["id"], path=hh.admin)["api_key"]


def test_service_app_key_has_no_person_or_prompt_memory(hh):
    sa = admin_store.ensure_service_account("ha", path=hh.admin)
    svc = service_app("Home Assistant", path=hh.admin)
    result = hh.store.create_credential(app_id=svc["id"], data={}, actor=_person_for(hh.admin, svc["id"]))
    assert result["prompt_memory"]["person_id"] is None
    assert "not remembered" in result["prompt_memory"]["description"]
    assert "explicitly ask to save a conversation" in result["prompt_memory"]["description"]
    key = result["api_key"]
    auth = _ctx_for(key, hh.admin)
    assert auth.identity.person_id is None
    ctx = proxy._begin_media_delivery(auth)
    try:
        assert proxy._note_media_metadata_body({"prompt": "kid on shared tablet"}) is None
        assert ctx.prompt_decision.keep is False
        assert ctx.prompt_decision.person_id is None
    finally:
        proxy.media_delivery._CONTEXT.set(None)
    decision = prompt_memory.decide_for_person(auth.identity.person_id, {"input": "x"}, admin_path=hh.admin)
    assert decision.person_id is None and proxy._responses_scope(auth)["person_id"] is None


def test_removed_admins_service_key_keeps_app_access_without_person(hh):
    admin_member = admin_store.upsert_oidc_user("iss", "adm", email="adm@x.test", path=hh.admin)
    sa = admin_store.ensure_service_account("auto", path=hh.admin)
    svc = service_app("Automation", path=hh.admin)
    # An administrator may no longer mint a key on an App the owner answers for
    # (test_signin_who_can_enter), but keys made before that rule still exist.
    # Model one directly: it was minted by the administrator, who is removed below.
    key = generate_app_api_key()
    admin_store.create_credential_hash(subject_kind="app", subject_id=svc["id"], hash=hash_api_key(key),
                                       prefix=key[:8], created_by=admin_member["id"], path=hh.admin)
    accounts.change_person(admin_member["id"], hh.owner["id"], remove=True, path=hh.admin)
    auth = runtime_identity_for_app_credential_hash(hash_api_key(key), path=hh.admin)
    assert auth is not None and auth.person_id is None


def test_resolved_legacy_personal_key_remembers_prompt_and_keeps_library_item(hh):
    legacy = admin_store.create_credential_hash(subject_kind="app", subject_id=hh.robin_app["id"], app_id=hh.robin_app["id"],
                                                hash=hash_api_key("legacy-robin-key"), prefix="legacy", created_by=hh.robin["id"], path=hh.admin)
    auth = _ctx_for("legacy-robin-key", hh.admin)
    assert auth.identity.person_id == hh.robin["id"]
    ctx = proxy._begin_media_delivery(auth)
    try:
        proxy._note_media_metadata_body({"prompt": "made with old key"})
        asset_id = proxy.store_asset_bytes(_png(), kind="img", suffix=".png", owner_key_hash=proxy._asset_owner_key(auth))
    finally:
        proxy.media_delivery._CONTEXT.set(None)
    assert prompt_memory.get(asset_id, hh.robin["id"])["prompt_text"] == "made with old key"
    scope = me_media._member_media_scope(hh.robin["id"], hh.admin)
    items = me_media._media_items(
        request=Request({"type": "http", "method": "GET", "path": "/me/api/media", "headers": []}),
        scope=scope, signed_url_builder=lambda *_a, **_k: "u", asset_path_resolver=proxy.asset_cache_path,
        asset_media_type_resolver=lambda *_a: "image/png",
        asset_catalog_provider=me_media.create_asset_catalog_provider(proxy.IMAGE_CACHE_DIR),
        asset_metadata_reader=proxy._read_asset_metadata, admin_db_path=hh.admin, media_jobs_path=hh.jobs,
        receipts_path=hh.receipts, prompt_memory_path=hh.jobs)
    assert asset_id in {item["asset_id"] for item in items}
    meta = proxy._read_asset_metadata(asset_id)
    meta.pop("maker_person_ids")
    proxy._write_asset_metadata(asset_id, meta)
    items = me_media._media_items(
        request=Request({"type": "http", "method": "GET", "path": "/me/api/media", "headers": []}),
        scope=scope, signed_url_builder=lambda *_a, **_k: "u", asset_path_resolver=proxy.asset_cache_path,
        asset_media_type_resolver=lambda *_a: "image/png",
        asset_catalog_provider=me_media.create_asset_catalog_provider(proxy.IMAGE_CACHE_DIR),
        asset_metadata_reader=proxy._read_asset_metadata, admin_db_path=hh.admin, media_jobs_path=hh.jobs,
        receipts_path=hh.receipts, prompt_memory_path=hh.jobs)
    assert asset_id in {item["asset_id"] for item in items}


def test_service_output_stays_in_library_without_prompt_or_full_metadata(hh):
    prompt_memory.put_member(hh.robin["id"], remember=False, retention="never", path=hh.admin)
    sa = admin_store.ensure_service_account("webui", path=hh.admin)
    svc = service_app("Open WebUI", path=hh.admin)
    auth = _ctx_for(hh.store.create_credential(app_id=svc["id"], data={}, actor=_person_for(hh.admin, svc["id"]))["api_key"], hh.admin)
    ctx = proxy._begin_media_delivery(auth)
    try:
        proxy._note_media_metadata_body({"prompt": "robin typed this in the shared web UI"})
        asset_id = proxy.store_asset_bytes(_png(), kind="img", suffix=".png", owner_key_hash=proxy._asset_owner_key(auth))
    finally:
        proxy.media_delivery._CONTEXT.set(None)
    scope = me_media._MediaScope(app_ids=None, viewer_person_id=hh.owner["id"], user_ids=None, audit_everyone=True)
    items = me_media._media_items(
        request=Request({"type": "http", "method": "GET", "path": "/me/api/media", "headers": []}),
        scope=scope, signed_url_builder=lambda *_a, **_k: "u", asset_path_resolver=proxy.asset_cache_path,
        asset_media_type_resolver=lambda *_a: "image/png",
        asset_catalog_provider=me_media.create_asset_catalog_provider(proxy.IMAGE_CACHE_DIR),
        asset_metadata_reader=proxy._read_asset_metadata, admin_db_path=hh.admin, media_jobs_path=hh.jobs,
        receipts_path=hh.receipts, prompt_memory_path=hh.jobs)
    item = next(i for i in items if i["asset_id"] == asset_id)
    assert item["prompt"] is None
    assert prompt_memory.get_person_prompts(hh.owner["id"]) == {}
    assert item["workflow_download_url"] is None
    assert item["source"]["app_id"] == svc["id"]
    assert "robin typed this" not in item["title"]



def _library(hh, scope):
    return me_media._media_items(
        request=Request({"type": "http", "method": "GET", "path": "/me/api/media", "headers": []}),
        scope=scope, signed_url_builder=lambda *_a, **_k: "u", asset_path_resolver=proxy.asset_cache_path,
        asset_media_type_resolver=lambda *_a: "image/png",
        asset_catalog_provider=me_media.create_asset_catalog_provider(proxy.IMAGE_CACHE_DIR),
        asset_metadata_reader=proxy._read_asset_metadata, admin_db_path=hh.admin, media_jobs_path=hh.jobs,
        receipts_path=hh.receipts, prompt_memory_path=hh.jobs)


@pytest.mark.parametrize("makers", [[], ["another-person"]])
def test_app_library_visibility_does_not_depend_on_maker_metadata(hh, makers):
    auth = _ctx_for(_robin_key(hh), hh.admin)
    asset_id = proxy.store_asset_bytes(_png(), kind="img", suffix=".png", owner_key_hash=proxy._asset_owner_key(auth), maker_person_id=None)
    meta = proxy._read_asset_metadata(asset_id)
    meta["maker_person_ids"] = makers
    proxy._write_asset_metadata(asset_id, meta)
    items = _library(hh, me_media._member_media_scope(hh.robin["id"], hh.admin))
    assert asset_id in {item["asset_id"] for item in items}


def test_legacy_service_key_stays_no_person(hh):
    sa = admin_store.ensure_service_account("legacy-svc", path=hh.admin)
    app = service_app("Shared", path=hh.admin)
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash=hash_api_key("old-service"), prefix="old", path=hh.admin)
    assert _ctx_for("old-service", hh.admin).identity.person_id is None


def test_service_key_full_signed_download_is_refused(hh, monkeypatch):
    sa = admin_store.ensure_service_account("full-export", path=hh.admin)
    app = service_app("Shared", path=hh.admin)
    key = hh.store.create_credential(app_id=app["id"], data={}, actor=_person_for(hh.admin, app["id"]))["api_key"]
    auth = _ctx_for(key, hh.admin)
    ctx = proxy._begin_media_delivery(auth)
    try:
        proxy._note_media_metadata_body({"prompt": SENTINEL})
        asset_id = proxy.store_asset_bytes(_png(SENTINEL), kind="img", suffix=".png", owner_key_hash=proxy._asset_owner_key(auth))
    finally:
        proxy.media_delivery._CONTEXT.set(None)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-signing-secret")
    expires = int(time.time()) + 60
    sig = proxy._sign_asset_token(asset_id, expires, "full")
    request = Request({"type": "http", "method": "GET", "path": f"/v1/assets/{asset_id}/content",
        "query_string": f"expires={expires}&md=full&sig={sig}".encode(), "headers": []})
    response = proxy._signed_asset_content_response(asset_id, request, invalid_message="x", missing_message="y", auth_context=auth)
    assert response.status_code == 403
    assert SENTINEL.encode() not in proxy.asset_cache_path(asset_id).read_bytes()


def test_unresolved_legacy_personal_key_has_no_prompt_access(hh):
    prompt_memory.put_member(hh.robin["id"], remember=False, retention="never", path=hh.admin)
    admin_store.create_credential_hash(subject_kind="app", subject_id=hh.robin_app["id"],
        hash=hash_api_key("legacy-off-key"), prefix="old", path=hh.admin)
    auth = _ctx_for("legacy-off-key", hh.admin)
    ctx = proxy._begin_media_delivery(auth)
    try:
        proxy._note_media_metadata_body({"prompt": SENTINEL})
        asset_id = proxy.store_asset_bytes(_png(SENTINEL), kind="img", suffix=".png", owner_key_hash=proxy._asset_owner_key(auth))
    finally:
        proxy.media_delivery._CONTEXT.set(None)
    assert ctx.prompt_decision.person_id is None
    assert ctx.prompt_decision.reason == "no_person"
    item = next(item for item in _library(hh, me_media._member_media_scope(hh.robin["id"], hh.admin)) if item["asset_id"] == asset_id)
    assert item["prompt"] is None
    assert SENTINEL.encode() not in proxy.asset_cache_path(asset_id).read_bytes()


@pytest.mark.parametrize("self_service", [False, True])
def test_own_personal_key_warns_about_prompt_access(hh, self_service):
    if self_service:
        session = AdminSession(subject=hh.robin["id"], role="none", email=None,
                               issued_at=1, expires_at=4_000_000_000)
        result = mint_user_app_key(session, hh.robin_app["id"], path=hh.admin)
        person = hh.robin["id"]
    else:
        app = admin_store.create_app("Owner Try", kind="personal",
                                     owner_user_id=hh.owner["id"], access_plan="person", path=hh.admin)
        result = hh.store.create_credential(app_id=app["id"], data={}, actor=_person_for(hh.admin, app["id"]))
        person = hh.owner["id"]
    assert result["prompt_memory"]["person_id"] == person
    assert "follow your prompt-memory settings" in result["prompt_memory"]["description"]
    assert "Anyone holding it can access your saved prompts" in result["prompt_memory"]["description"]
    assert _ctx_for(result["api_key"], hh.admin).identity.person_id == person

"""Credential identity, rather than App ownership, protects prompt content."""

from __future__ import annotations

import asyncio
import json
import io
import sqlite3
import time
from unittest.mock import patch
from types import SimpleNamespace

import proxy
from tests.support.access import auth_identity  # noqa: E402
import httpx
from gateway import admin_store, media_jobs, me_media, prompt_memory, receipt_store
from gateway.admin_api import AdminContext
from gateway.admin_oidc import AdminSession
from gateway.admin_runtime import AdminSQLiteStore, runtime_identity_for_app_credential_hash
from gateway.runtime_auth import hash_api_key
from gateway.user_runtime import mint_user_app_key
from starlette.requests import Request
from fastapi.testclient import TestClient
from tests.test_video_list_delete_remix import _FakeVideoAsyncClient, _StubListRequest, _succeeded_history_entry


def _auth(app_id, person_id, key):
    identity = auth_identity(name="Try", key_hash=key, app_id=app_id, person_id=person_id)
    return proxy.AuthContext(identity=identity)


def test_same_app_other_person_can_read_media_but_not_video_prompt(tmp_path, monkeypatch):
    admin_db, jobs_db = tmp_path / "admin.db", tmp_path / "media_jobs.db"
    admin_store.init_db(admin_db)
    media_jobs.init_db(jobs_db)
    member = admin_store.upsert_oidc_user("test", "member", path=admin_db)
    owner = admin_store.upsert_oidc_user("test", "owner", path=admin_db)
    app = admin_store.create_app("Try", kind="personal", owner_user_id=member["id"], access_plan="person", path=admin_db)
    member_auth = _auth(app["id"], member["id"], "sha256:member")
    owner_auth = _auth(app["id"], owner["id"], "sha256:owner")
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin_db)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs_db)
    monkeypatch.setattr(prompt_memory, "DB_PATH", jobs_db)
    monkeypatch.setattr(proxy, "COMFY_ENABLED", True)
    monkeypatch.setattr(proxy, "_service_availability_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(proxy, "configured_downstream_headers", lambda *_a: {})
    monkeypatch.setattr(proxy, "_video_job_records", {"private": {
        "app_id": app["id"], "person_id": member["id"], "owner_key_hash": proxy._asset_owner_key(member_auth),
        "created_at": 1000,
    }})
    prompt_memory.put("video_private", member["id"], "member-secret-prompt", kept_until=None, admin_path=admin_db)
    client = _FakeVideoAsyncClient(history={"private": _succeeded_history_entry()})
    with patch.object(proxy.httpx, "AsyncClient", lambda **_kw: client):
        video_id = proxy._video_id_from_prompt_id("private")
        own = asyncio.run(proxy.proxy_comfy_video_status(video_id, member_auth))
        other = asyncio.run(proxy.proxy_comfy_video_status(video_id, owner_auth))
    assert own.status_code == other.status_code == 200
    assert json.loads(own.body)["prompt"] == "member-secret-prompt"
    assert json.loads(other.body)["prompt"] is None
    with patch.object(proxy.httpx, "AsyncClient", lambda **_kw: client):
        own_list = asyncio.run(proxy.proxy_comfy_video_list(_StubListRequest(), member_auth))
        other_list = asyncio.run(proxy.proxy_comfy_video_list(_StubListRequest(), owner_auth))
    assert json.loads(own_list.body)["data"][0]["prompt"] == "member-secret-prompt"
    assert json.loads(other_list.body)["data"][0]["prompt"] is None


def test_responses_same_display_name_does_not_grant_access():
    robin = _auth("app_robin", "person_robin", "sha256:a")
    sam = _auth("app_sam", "person_sam", "sha256:b")
    row = {"api_key_name": "Try", "app_id": "app_robin", "person_id": "person_robin"}
    assert proxy._responses_row_visible(row, robin)
    assert not proxy._responses_row_visible(row, sam)


def test_responses_legacy_name_only_rows_are_hidden():
    auth = _auth("app_robin", "person_robin", "sha256:a")
    assert not proxy._responses_row_visible({"api_key_name": "Try"}, auth)


def test_personal_key_must_be_minted_by_its_own_app_owner(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    member = admin_store.upsert_oidc_user("test", "member", path=path)
    owner = admin_store.upsert_oidc_user("test", "owner", path=path)
    app = admin_store.create_app("Try", kind="personal", owner_user_id=member["id"], access_plan="person", path=path)
    session = AdminSession(subject=member["id"], role="none", email=None, issued_at=1, expires_at=4_000_000_000)
    member_key = mint_user_app_key(session, app["id"], path=path)
    store = AdminSQLiteStore(path)
    owner_key = store.create_credential(app_id=app["id"], data={}, actor=AdminContext(
        subject=owner["id"], actor_kind="user", roles=frozenset({"owner"}),
    ))
    service_key = store.create_credential(app_id=app["id"], data={}, actor=AdminContext(
        subject="service", actor_kind="service_account", roles=frozenset({"owner"}),
    ))
    for key, expected, creator in ((member_key, member["id"], member["id"]), (owner_key, None, owner["id"]), (service_key, None, "")):
        identity = runtime_identity_for_app_credential_hash(hash_api_key(key["api_key"]), path=path)
        assert identity.app_id == app["id"]
        assert identity.person_id == expected
        credential = admin_store.get_credential(key["id"], path=path)
        assert credential["created_by"] == creator
    legacy = admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"],
        hash=hash_api_key("legacy-key"), prefix="legacy", path=path)
    assert runtime_identity_for_app_credential_hash(legacy["hash"], path=path).person_id is None


def test_new_output_from_owner_key_is_not_saved_as_the_app_owner(tmp_path, monkeypatch):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    member = admin_store.upsert_oidc_user("test", "member", path=path)
    owner = admin_store.upsert_oidc_user("test", "owner", path=path)
    app = admin_store.create_app("Try", kind="personal", owner_user_id=member["id"], access_plan="person", path=path)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    key = AdminSQLiteStore(path).create_credential(app_id=app["id"], data={}, actor=AdminContext(
        subject=owner["id"], actor_kind="user", roles=frozenset({"owner"}),
    ))
    identity = runtime_identity_for_app_credential_hash(hash_api_key(key["api_key"]), path=path)
    context = proxy._begin_media_delivery(proxy.AuthContext(identity=identity))
    try:
        assert proxy._note_media_metadata_body({"prompt": "owner-input"}) is None
        assert not context.prompt_decision.keep
        assert context.prompt_decision.person_id is None
        assert context.person_id is None
    finally:
        proxy.media_delivery._CONTEXT.set(None)


def _png(prompt):
    from PIL import Image, PngImagePlugin
    tags = PngImagePlugin.PngInfo()
    tags.add_text("prompt", json.dumps({"text": prompt}))
    tags.add_text("workflow", json.dumps({"input": prompt}))
    out = io.BytesIO()
    Image.new("RGB", (8, 8)).save(out, format="PNG", pnginfo=tags)
    return out.getvalue()


def test_signed_full_metadata_checks_creator_on_same_app(tmp_path, monkeypatch):
    path, jobs_db, cache = tmp_path / "admin.db", tmp_path / "jobs.db", tmp_path / "cache"
    admin_store.init_db(path)
    media_jobs.init_db(jobs_db)
    member = admin_store.upsert_oidc_user("test", "member", path=path)
    owner = admin_store.upsert_oidc_user("test", "owner", path=path)
    app = admin_store.create_app("Try", kind="personal", owner_user_id=member["id"], access_plan="person", path=path)
    member_auth = _auth(app["id"], member["id"], "sha256:member")
    owner_auth = _auth(app["id"], owner["id"], "sha256:owner")
    unknown_auth = _auth(app["id"], None, "sha256:legacy")
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs_db)
    monkeypatch.setattr(prompt_memory, "DB_PATH", jobs_db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-signing-secret")
    proxy._begin_media_delivery(member_auth)
    try:
        proxy._note_media_metadata_body({"prompt": "full-metadata-secret"})
        asset_id = proxy.store_asset_bytes(_png("full-metadata-secret"), kind="img", suffix=".png",
                                          owner_key_hash=proxy._asset_owner_key(member_auth))
    finally:
        proxy.media_delivery._CONTEXT.set(None)
    expires = int(time.time()) + 60
    sig = proxy._sign_asset_token(asset_id, expires, "full")
    client = TestClient(proxy.app)
    for auth, expected in ((member_auth, 200), (owner_auth, 403), (unknown_auth, 403)):
        monkeypatch.setattr(proxy, "authenticate_request", lambda *_a, _auth=auth, **_kw: _auth)
        response = client.get(f"/v1/assets/{asset_id}/content?expires={expires}&md=full&sig={sig}")
        assert response.status_code == expected
        if expected == 200:
            assert b"full-metadata-secret" in response.content
        else:
            assert b"full-metadata-secret" not in response.content
    monkeypatch.setattr(proxy, "_oidc_session_from_request", lambda *_a: SimpleNamespace(subject=member["id"]))
    for auth, expected in ((owner_auth, 403), (unknown_auth, 403)):
        monkeypatch.setattr(proxy, "authenticate_request", lambda *_a, _auth=auth, **_kw: _auth)
        response = client.get(f"/v1/assets/{asset_id}/content?expires={expires}&md=full&sig={sig}")
        assert response.status_code == expected


def test_responses_schema_migration_does_not_guess_legacy_scope(tmp_path):
    path = tmp_path / "responses.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE responses(id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, model TEXT,
                previous_response_id TEXT, request_json TEXT NOT NULL, response_json TEXT NOT NULL, api_key_name TEXT);
            CREATE TABLE schema_version(version INTEGER NOT NULL);
            INSERT INTO schema_version VALUES(1);
            INSERT INTO responses VALUES('old', 1, NULL, NULL, '{}', '{}', 'Try');
        """)
    proxy.responses_store.init_db(path)
    row = proxy.responses_store.get("old", path=path)
    assert row["person_id"] is None and row["app_id"] is None and row["owner_key_hash"] is None
    assert not proxy._responses_row_visible(row, _auth("member-app", "member", "sha256:member"))


def test_video_full_content_checks_creator_on_same_app(tmp_path, monkeypatch):
    path, jobs_db = tmp_path / "admin.db", tmp_path / "jobs.db"
    admin_store.init_db(path)
    media_jobs.init_db(jobs_db)
    member = admin_store.upsert_oidc_user("test", "member", path=path)
    owner = admin_store.upsert_oidc_user("test", "owner", path=path)
    app = admin_store.create_app("Try", kind="personal", owner_user_id=member["id"], access_plan="person", path=path)
    member_auth = _auth(app["id"], member["id"], "sha256:member")
    owner_auth = _auth(app["id"], owner["id"], "sha256:owner")
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs_db)
    monkeypatch.setattr(prompt_memory, "DB_PATH", jobs_db)
    monkeypatch.setattr(proxy, "COMFY_ENABLED", True)
    monkeypatch.setattr(proxy, "configured_downstream_headers", lambda *_a: {})
    monkeypatch.setattr(proxy, "_video_job_records", {"private": {
        "app_id": app["id"], "person_id": member["id"],
        "owner_key_hash": proxy._asset_owner_key(member_auth), "created_at": 1000,
    }})
    prompt_memory.put("video_private", member["id"], "video-full-secret", kept_until=None, admin_path=path)
    history = {"status": {"completed": True}, "outputs": {
        "6": {"videos": [{"filename": "result.mp4", "type": "output", "subfolder": ""}]}
    }}
    class Client(_FakeVideoAsyncClient):
        async def get(self, url, **kwargs):
            if url.endswith("/view"):
                return httpx.Response(200, content=b"video-with-workflow-and-video-full-secret")
            return await super().get(url, **kwargs)
    client = Client(history={"private": history})
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **_kw: client)
    # This test isolates authorization from metadata codecs: the retained
    # workflow bytes must never reach the non-creator, even on the same App.
    monkeypatch.setattr(proxy.media_delivery, "attach_provenance", lambda data, *_a: data)
    monkeypatch.setattr(prompt_memory, "capture_metadata", lambda *_a: {
        "prompt": "video-full-secret", "workflow": "video-full-workflow-secret",
    })
    for auth, expected in ((member_auth, 200), (owner_auth, 403), (_auth(app["id"], None, "sha256:old"), 403)):
        response = asyncio.run(proxy.proxy_comfy_video_content(
            proxy._video_id_from_prompt_id("private"), auth, metadata_mode="full", index_library=False,
        ))
        assert response.status_code == expected, response.body
        if expected == 200:
            assert b"video-full-secret" in response.body
        else:
            assert b"video-full-secret" not in response.body

    # The first clean download can be made by someone else on this App.
    # Indexing that read must preserve the original video maker.
    receipts_db = tmp_path / "receipts.db"
    receipt_store.init_db(receipts_db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(proxy.media_metadata, "strip_metadata", lambda data, *_a, **_kw: b"clean-video")
    saved_before = prompt_memory.get("video_private", member["id"])
    internal = asyncio.run(proxy.proxy_comfy_video_content(
        proxy._video_id_from_prompt_id("private"), member_auth, metadata_mode="none", index_library=False,
    ))
    assert internal.status_code == 200, internal.body
    assert internal.body == b"clean-video"
    saved_after = prompt_memory.get("video_private", member["id"])
    assert saved_after["metadata"]["workflow"] == "video-full-workflow-secret"
    assert saved_after["created_at"] == saved_before["created_at"]
    assert saved_after["kept_until"] == saved_before["kept_until"]
    assert "asset_id" not in proxy._video_job_records["private"]
    proxy._begin_media_delivery(owner_auth)
    try:
        response = asyncio.run(proxy.proxy_comfy_video_content(
            proxy._video_id_from_prompt_id("private"), owner_auth, metadata_mode="none",
        ))
    finally:
        proxy.media_delivery._CONTEXT.set(None)
    assert response.status_code == 200, response.body
    asset_id = proxy._video_job_records["private"]["asset_id"]
    assert proxy._read_asset_metadata(asset_id)["maker_person_ids"] == [member["id"]]
    assert prompt_memory.get(asset_id, member["id"])["prompt_text"] == "video-full-secret"
    assert prompt_memory.get(asset_id, member["id"])["metadata"]["workflow"] == "video-full-workflow-secret"
    assert prompt_memory.get(asset_id, owner["id"]) is None
    def library(person_id):
        return me_media._media_items(
            request=Request({"type": "http", "method": "GET", "path": "/me/api/media", "headers": []}),
            scope=me_media._MediaScope(app_ids=[app["id"]], person_id=person_id, viewer_person_id=person_id),
            signed_url_builder=lambda *_a, **_kw: "signed-url",
            asset_path_resolver=proxy.asset_cache_path,
            asset_media_type_resolver=lambda *_a: "video/mp4", asset_catalog_provider=None,
            asset_metadata_reader=proxy._read_asset_metadata, admin_db_path=path,
            media_jobs_path=jobs_db, receipts_path=receipts_db, prompt_memory_path=jobs_db,
        )
    assert library(member["id"])[0]["prompt"] == "video-full-secret"
    assert library(owner["id"])[0]["prompt"] is None


def test_delete_person_removes_only_that_persons_responses(tmp_path):
    path = tmp_path / "responses.db"
    proxy.responses_store.init_db(path)
    for person in ("robin", "sam"):
        proxy.responses_store.save(person, created_at=1, model="default", previous_response_id=None,
            request_json={"input": f"{person}-private"}, response_json={},
            person_id=person, app_id=f"app-{person}", path=path)
    assert proxy.responses_store.delete_person("robin", path=path) == 1
    assert proxy.responses_store.get("robin", path=path) is None
    assert proxy.responses_store.get("sam", path=path)["person_id"] == "sam"


def test_delete_person_before_preserves_later_and_other_person_responses(tmp_path):
    path = tmp_path / "responses.db"
    proxy.responses_store.init_db(path)
    for response_id, person, created_at in (("old", "robin", 9), ("boundary", "robin", 10),
                                           ("new", "robin", 11), ("other", "sam", 9)):
        proxy.responses_store.save(response_id, created_at=created_at, model="default", previous_response_id=None,
            request_json={"input": response_id}, response_json={}, person_id=person, app_id=f"app-{person}", path=path)
    assert proxy.responses_store.delete_person_before("robin", 10, path=path) == 2
    assert proxy.responses_store.get("old", path=path) is None
    assert proxy.responses_store.get("boundary", path=path) is None
    assert proxy.responses_store.get("new", path=path) is not None
    assert proxy.responses_store.get("other", path=path) is not None
    assert proxy.responses_store.delete_person_before("robin", 10, path=tmp_path / "absent.db") == 0


def test_home_identity_uses_session_person_or_personal_app_owner_key(tmp_path):
    from gateway import accounts
    from gateway.home_api import _identity, _new_app_identity
    from tests.support.access import store_plan
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    accounts.initialize(path)
    person = admin_store.upsert_oidc_user("iss", "native", path=path)
    accounts.change_person(person["id"], "test", path=path, plan_id=store_plan("Everything", path=path)["id"])
    native = _new_app_identity(path, person_id=person["id"])
    assert native.person_id == person["id"]
    app = admin_store.create_app("member-app", kind="personal", owner_user_id=person["id"], access_plan="person", path=path)
    other_creator = _identity(app, {"created_by": "owner", "hash": "sha256:key"}, path, person_id=person["id"])
    assert other_creator.person_id is None
    unknown_creator = _identity(app, {"hash": "sha256:old"}, path, person_id=person["id"])
    assert unknown_creator.person_id is None
    own_key = _identity(app, {"created_by": person["id"], "hash": "sha256:mine"}, path, person_id=person["id"])
    assert own_key.person_id == person["id"]

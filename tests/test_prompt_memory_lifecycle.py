"""Removal, operational spooling and legacy cache privacy contracts."""
from __future__ import annotations

import asyncio
import json
from io import BytesIO
import os
import time
from types import SimpleNamespace

import pytest

import proxy
from gateway import admin_store, media_delivery, media_jobs, media_metadata, prompt_memory

from PIL import Image
from tests.support.access import service_app, auth_identity
_buffer = BytesIO()
Image.new("RGB", (2, 2), (1, 2, 3)).save(_buffer, format="PNG")
PNG = _buffer.getvalue()


@pytest.fixture
def stores(tmp_path, monkeypatch):
    admin = tmp_path / "data/admin.db"
    jobs = tmp_path / "data/media_jobs.db"
    responses = tmp_path / "data/responses.db"
    admin_store.init_db(admin)
    media_jobs.init_db(jobs)
    proxy.responses_store.init_db(responses)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    monkeypatch.setattr(proxy.responses_store, "DB_PATH", responses)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOADS", {})
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOAD_SIZES", {})
    user = admin_store.upsert_oidc_user("issuer", "maker", email="maker@example.test", path=admin)
    app = admin_store.create_app("Try", kind="personal", owner_user_id=user["id"], access_plan="person", path=admin)
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], hash="sha256:maker", prefix="test", app_id=app["id"], created_by=user["id"], path=admin)
    auth = proxy.AuthContext(identity=auth_identity(name="Try", key_hash="sha256:maker", app_id=app["id"], person_id=user["id"]))
    return tmp_path, admin, jobs, user, app, auth


def request():
    return SimpleNamespace(headers={"host": "testserver"}, url=SimpleNamespace(scheme="http", netloc="testserver"))


@pytest.mark.parametrize("caller", ["lan", "service", "person"])
def test_normal_async_spool_survives_memory_loss(stores, caller):
    root, admin, _jobs, _user, _app, auth = stores
    if caller == "lan":
        auth = proxy.NO_AUTH_CONTEXT
    elif caller == "service":
        account = admin_store.create_service_account("Automation", path=admin)
        service = service_app("Automation", path=admin)
        auth = proxy.AuthContext(identity=auth_identity(name="Automation", key_hash="sha256:automation", app_id=service["id"]))
    body = {"prompt": "restart durability sentinel", "model": "test"}
    proxy._persist_image_media_job_payload("mj_durable", body=body, request=request(), auth_context=auth)
    path = proxy._image_media_job_payload_path("mj_durable")
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    proxy._EPHEMERAL_MEDIA_JOB_PAYLOADS.clear()  # A fresh worker/restart reads only disk.
    assert proxy._load_image_media_job_payload("mj_durable")["body"] == body
    proxy._delete_image_media_job_payload("mj_durable")
    assert not path.exists()


@pytest.mark.parametrize("caller", ["lan", "service", "person"])
def test_explicit_forget_never_creates_durable_execution_spool(stores, caller):
    root, admin, _jobs, _user, _app, auth = stores
    if caller == "lan":
        auth = proxy.NO_AUTH_CONTEXT
    elif caller == "service":
        account = admin_store.create_service_account("Automation", path=admin)
        service = service_app("Automation", path=admin)
        auth = proxy.AuthContext(identity=auth_identity(name="Automation", key_hash="sha256:automation", app_id=service["id"]))
    sentinel = "forget durable spool sentinel"
    proxy._persist_image_media_job_payload("mj_forget", body={"prompt": sentinel, "extensions": {prompt_memory.NAMESPACE: {"mode": "forget"}}}, request=request(), auth_context=auth)
    assert not proxy._image_media_job_payload_path("mj_forget").exists()
    assert proxy._load_image_media_job_payload("mj_forget")["body"]["prompt"] == sentinel
    assert all(sentinel.encode() not in p.read_bytes() for p in root.rglob("*") if p.is_file())


def test_person_delete_strips_legacy_cache_and_clears_chats_and_spools(stores):
    root, admin, jobs, user, app, auth = stores
    sentinel = "delete legacy sentinel"
    cache = root / "cache"
    cache.mkdir()
    digest = "a" * 64
    asset = f"img_{digest}"
    path = cache / f"{digest}.png"
    path.write_bytes(media_metadata.embed_png_text(PNG, {"prompt": sentinel, "workflow": sentinel}))
    proxy._write_asset_metadata(asset, {"owner_key_hashes": [f"app:{app['id']}"]})
    prompt_memory.put(asset, user["id"], sentinel, kept_until=None, path=jobs, admin_path=admin)
    proxy.responses_store.save("resp_private", request_json={"input": sentinel}, response_json={"id": "resp_private"}, api_key_name="Try", app_id=app["id"], person_id=user["id"], created_at=int(time.time()), model="test", previous_response_id=None, path=proxy.responses_store.DB_PATH)
    media_jobs.create(media_jobs.MediaJob(job_id="mj_active", intent="image.generate", workflow="test"), path=jobs)
    proxy._persist_image_media_job_payload("mj_active", body={"prompt": sentinel}, request=request(), auth_context=auth)
    result = proxy._purge_person_prompt_memory(user["id"])
    # Rows go at once; file stripping is queued off the request path.
    assert result["pending"] is True
    assert prompt_memory.get(asset, user["id"], path=jobs) is None
    proxy._drain_prompt_purges()
    assert path.exists() and sentinel.encode() not in path.read_bytes()
    assert proxy.responses_store.get("resp_private", path=proxy.responses_store.DB_PATH) is None
    assert not proxy._image_media_job_payload_path("mj_active").exists()
    assert proxy._load_image_media_job_payload("mj_active")["body"]["extensions"][prompt_memory.NAMESPACE] == {"mode": "forget"}
    assert proxy._pending_prompt_purges() == []


def test_kill_strips_legacy_cache_and_retries_partial_cleanup(stores, monkeypatch):
    root, _admin, _jobs, _user, _app, _auth = stores
    cache = root / "cache"
    cache.mkdir()
    path = cache / f"{'b' * 64}.png"
    path.write_bytes(media_metadata.embed_png_text(PNG, {"prompt": "kill legacy sentinel"}))
    real_strip = media_metadata.strip_metadata
    monkeypatch.setattr(media_metadata, "strip_metadata", lambda *_a, **_kw: (_ for _ in ()).throw(media_metadata.MediaMetadataError("failed")))
    assert proxy._purge_prompt_memory_on_kill()["pending"] is True
    assert proxy._pending_prompt_purges() == ["*"]
    monkeypatch.setattr(media_metadata, "strip_metadata", real_strip)
    proxy._maintain_prompt_memory()
    assert proxy._pending_prompt_purges() == []
    assert b"kill legacy sentinel" not in path.read_bytes()


def test_maintenance_keeps_old_media_and_prompt_row(stores):
    root, admin, jobs, user, _app, auth = stores
    ctx = proxy._begin_media_delivery(auth)
    proxy._note_media_metadata_body({"prompt": "retention sentinel"})
    asset = proxy.store_asset_bytes(PNG, owner_key_hash=proxy._asset_owner_key(auth))
    path = proxy.asset_cache_path(asset)
    assert prompt_memory.get(asset, user["id"], path=jobs)
    old = 1
    os.utime(path, (old, old))
    before = path.read_bytes()
    proxy._maintain_prompt_memory()
    assert path.read_bytes() == before
    assert prompt_memory.get(asset, user["id"], path=jobs)


def test_forget_seeded_digest_does_not_allow_prompt_guessing(stores):
    _root, _admin, _jobs, _user, _app, auth = stores
    def identify(prompt):
        return proxy._image_media_job_request_id(body={"seed": 12, "prompt": prompt, "extensions": {prompt_memory.NAMESPACE: {"mode": "forget"}}}, auth_context=auth, idempotency_key=None, fallback_request_id="request")
    first, second = identify("guessable secret"), identify("guessable secret")
    assert first[0] == "request" and second[0] == "request"
    assert first[1] != second[1]
    assert first[2] == second[2] == "request"


def test_backend_execution_error_never_copies_input(stores):
    sentinel = "private words quoted in backend error"
    summary = proxy._graph_error_message({"status": {"messages": [["execution_error", {"node_id": "6", "node_type": "TextEncode", "exception_message": sentinel}]]}})
    assert sentinel not in summary
    assert "TextEncode" in summary and "6" in summary and "execution failed" in summary


def test_delete_during_image_retry_keeps_original_erasure_clock(stores, monkeypatch):
    _root, admin, jobs, user, _app, auth = stores
    job = media_jobs.create(media_jobs.MediaJob(intent="image.generate", workflow="test"), path=jobs)
    proxy._persist_image_media_job_payload(job.job_id, body={"prompt": "retry deletion sentinel"}, request=request(), auth_context=auth)
    started = proxy._load_image_media_job_payload(job.job_id)["created_at"]
    calls = 0
    async def generate(shim, caller, *, receipt):
        nonlocal calls
        calls += 1
        proxy._begin_media_delivery(caller, receipt)
        proxy._note_media_metadata_body(await shim.json())
        if calls == 1:
            proxy._purge_person_prompt_memory(user["id"])
            # A subsequent attempt can run much later than deletion.
            monkeypatch.setattr(time, "time", lambda: started + 10)
            return proxy.JSONResponse({"error": {"message": "service unavailable", "type": "api_error"}}, status_code=503)
        decision = media_delivery.current().prompt_decision
        assert decision.created_at == started
        asset = proxy.store_asset_bytes(PNG, owner_key_hash=proxy._asset_owner_key(auth))
        assert prompt_memory.get(asset, user["id"], path=jobs) is None
        return proxy.JSONResponse({"data": [{"image_id": asset, "asset_id": asset, "url": "https://gateway.test/media"}]})
    monkeypatch.setattr(proxy, "proxy_graph_image_request", generate)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_RETRY_BASE_SECONDS", 0)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_RETRY_MAX_SECONDS", 0)
    asyncio.run(proxy._run_image_media_job(job.job_id))
    assert calls == 2
    assert media_jobs.get(job.job_id, audience="internal", path=jobs)["status"] == media_jobs.STATUS_SUCCEEDED


def test_settings_reconcile_cleanup_even_if_queue_write_never_happened(stores):
    _root, admin, jobs, user, _app, _auth = stores
    prompt_memory.put("video_unfetched", user["id"], "off cleanup sentinel", kept_until=None, path=jobs, admin_path=admin)
    prompt_memory.put_member(user["id"], remember=False, retention="never", path=admin)
    assert proxy._pending_prompt_purges() == []
    proxy._maintain_prompt_memory()
    assert prompt_memory.get("video_unfetched", user["id"], path=jobs) is None


def test_completed_member_erasure_does_not_rescan_or_delete_new_prompts(stores, monkeypatch):
    _root, admin, jobs, user, _app, _auth = stores
    person_id = user["id"]
    prompt_memory.put("old_asset", person_id, "old prompt", kept_until=None, path=jobs, admin_path=admin)
    prompt_memory.put_member(person_id, remember=False, retention="never", path=admin)
    proxy._maintain_person_prompt_memory(person_id)

    prompt_memory.put_member(person_id, remember=True, retention="1_year", path=admin)
    cutoff = prompt_memory.recorded_erasure_cutoff(person_id, admin_path=admin)
    prompt_memory.put("fresh_asset", person_id, "new prompt", kept_until=None, path=jobs,
                      admin_path=admin, created_at=cutoff + 1)
    calls = []
    monkeypatch.setattr(proxy.media_jobs, "erase_prompt_derivatives", lambda *a, **kw: calls.append("media"))
    monkeypatch.setattr(prompt_memory, "prune_person_before", lambda *a, **kw: calls.append("prompts"))
    monkeypatch.setattr(proxy.responses_store, "delete_person_before", lambda *a, **kw: calls.append("responses"))

    proxy._maintain_person_prompt_memory(person_id)

    assert calls == []
    assert prompt_memory.get("fresh_asset", person_id, path=jobs)["prompt_text"] == "new prompt"


def test_forget_payload_store_caps_count_and_releases_terminal_payload(stores, monkeypatch):
    _root, _admin, jobs, _user, _app, auth = stores
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_MAX_COUNT", 1)
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_MAX_BYTES", 10_000)
    forget = {"prompt": "private", "extensions": {prompt_memory.NAMESPACE: {"mode": "forget"}}}
    active = media_jobs.create(media_jobs.MediaJob(job_id="mj_cap_active", intent="image.generate"), path=jobs)
    proxy._persist_image_media_job_payload(active.job_id, body=forget, request=request(), auth_context=auth)

    with pytest.raises(proxy.EphemeralMediaPayloadCapacityError):
        proxy._persist_image_media_job_payload("mj_cap_refused", body=forget, request=request(), auth_context=auth)
    assert not proxy._image_media_job_payload_path("mj_cap_refused").exists()

    media_jobs.update_status(active.job_id, media_jobs.STATUS_RUNNING, path=jobs)
    media_jobs.update_status(active.job_id, media_jobs.STATUS_SUCCEEDED, path=jobs)
    assert proxy._prune_orphaned_media_job_payloads() == 1
    proxy._persist_image_media_job_payload("mj_cap_replacement", body=forget, request=request(), auth_context=auth)


def test_forget_payload_byte_cap_refuses_without_disk_fallback(stores, monkeypatch):
    root, _admin, _jobs, _user, _app, auth = stores
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_MAX_COUNT", 10)
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_MAX_BYTES", 100)
    forget = {"prompt": "x" * 1000, "extensions": {prompt_memory.NAMESPACE: {"mode": "forget"}}}
    with pytest.raises(proxy.EphemeralMediaPayloadCapacityError):
        proxy._persist_image_media_job_payload("mj_too_large", body=forget, request=request(), auth_context=auth)
    assert proxy._load_image_media_job_payload("mj_too_large") is None
    assert not proxy._image_media_job_payload_path("mj_too_large").exists()
    assert not list(root.rglob("mj_too_large.json"))

    response = proxy._ephemeral_media_payload_busy_response()
    assert response.status_code == 503
    assert json.loads(response.body)["error"]["code"] == "media_payload_capacity"
    assert "busy" in json.loads(response.body)["error"]["message"].lower()


def test_old_active_ephemeral_payload_keeps_its_capacity(stores, monkeypatch):
    _root, _admin, jobs, _user, _app, auth = stores
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_MAX_COUNT", 1)
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_MAX_BYTES", 10_000)
    forget = {"prompt": "private", "extensions": {prompt_memory.NAMESPACE: {"mode": "forget"}}}
    job = media_jobs.create(media_jobs.MediaJob(job_id="mj_cap_stale", intent="image.generate"), path=jobs)
    proxy._persist_image_media_job_payload(job.job_id, body=forget, request=request(), auth_context=auth)
    stale = proxy._load_image_media_job_payload(job.job_id)
    stale["created_at"] = 1
    assert proxy._prune_orphaned_media_job_payloads() == 0
    assert proxy._load_image_media_job_payload(job.job_id) == stale


def test_cancel_clears_upstream_video_prompt_and_all_execution_payloads(stores):
    _root, admin, jobs, user, _app, auth = stores
    job = media_jobs.create(media_jobs.MediaJob(intent="video.generate", metadata={"upstream_video_id": "video_cancel"}), path=jobs)
    prompt_memory.put("video_cancel", user["id"], "cancel sentinel", kept_until=None, path=jobs, admin_path=admin)
    proxy._persist_image_media_job_payload(job.job_id, body={"prompt": "cancel sentinel"}, request=request(), auth_context=auth)
    proxy._cancel_media_job(job.job_id, audience="internal")
    assert proxy._load_image_media_job_payload(job.job_id) is None
    assert prompt_memory.get("video_cancel", user["id"], path=jobs) is None
    proxy._purge_prompt_payload_files()
    assert job.job_id not in proxy._EPHEMERAL_MEDIA_JOB_PAYLOADS


@pytest.mark.parametrize("status", [media_jobs.STATUS_QUEUED, media_jobs.STATUS_RUNNING])
def test_old_active_disk_payload_survives_cleanup(stores, status):
    import os
    _root, _admin, jobs, _user, _app, _auth = stores
    job = media_jobs.create(media_jobs.MediaJob(intent="image.generate", status=status), path=jobs)
    path = proxy._image_media_job_payload_path(job.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"created_at": 1, "body": {"prompt": "keep"}}')
    os.utime(path, (1, 1))
    assert proxy._prune_orphaned_media_job_payloads() == 0
    assert path.exists()


def test_saved_conversation_survives_restart_and_maintenance(stores):
    _root, _admin, _jobs, user, app, _auth = stores
    path = proxy.responses_store.DB_PATH
    proxy.responses_store.save("resp_old", request_json={"input": "keep"},
        response_json={"id": "resp_old"}, api_key_name="Try", app_id=app["id"],
        person_id=user["id"], created_at=1, model="test", previous_response_id=None, path=path)
    proxy.responses_store.init_db(path)
    proxy._maintain_prompt_memory()
    assert proxy.responses_store.get("resp_old", path=path)["request_json"] == {"input": "keep"}


def test_corrupt_job_history_recovery_keeps_original_request_spool(stores):
    _root, _admin, jobs, _user, _app, _auth = stores
    job = media_jobs.create(media_jobs.MediaJob(intent="image.generate"), path=jobs)
    payload = proxy._image_media_job_payload_path(job.job_id)
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text('{"body": {"prompt": "recover me"}}')
    jobs.write_bytes(b'not a database')
    media_jobs.init_db(jobs)
    assert media_jobs.get(job.job_id, path=jobs) is None
    assert proxy._prune_orphaned_media_job_payloads() == 0
    assert payload.read_text() == '{"body": {"prompt": "recover me"}}'


@pytest.mark.parametrize("store", ["responses_store", "receipt_store", "media_jobs"])
def test_critical_store_failure_aborts_lifespan_before_workers(stores, monkeypatch, store):
    import sqlite3
    from unittest.mock import Mock
    monkeypatch.setattr(proxy.ADMIN_STORE, "init_db", lambda: None)
    for name in ("responses_store", "receipt_store", "media_jobs"):
        monkeypatch.setattr(getattr(proxy, name), "init_db", lambda: None)
    monkeypatch.setattr(getattr(proxy, store), "init_db", Mock(side_effect=sqlite3.OperationalError("database is locked")))
    launch = Mock(side_effect=AssertionError("worker started before stores were ready"))
    monkeypatch.setattr(proxy.providers, "recover_provider_spend", launch)
    async def start():
        async with proxy.lifespan(proxy.app):
            pytest.fail("startup should fail")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        asyncio.run(start())
    launch.assert_not_called()

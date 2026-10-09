"""Gateway cache writes keep prompt bytes in the dedicated maker record only."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import proxy
from gateway import admin_store, media_delivery, media_jobs, media_metadata, prompt_memory


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _files(root: Path):
    return [path for path in root.rglob("*") if path.is_file()]


def test_kept_prompt_has_one_maker_record_and_clean_media(tmp_path):
    sentinel = "private-sentinel-73fc"
    source = media_metadata.embed_png_text(PNG_1X1, {"prompt": sentinel, "workflow": sentinel})
    cache = tmp_path / "cache"
    db = tmp_path / "data" / "media_jobs.db"
    admin_db = tmp_path / "data" / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "person-a", email="a@example.test", path=admin_db)
    context = media_delivery.begin(profile_default="none")
    context.person_id = person["id"]
    context.prompt_decision = prompt_memory.Decision(person["id"], True, "remembered")
    context.prompt_text = sentinel
    with patch.object(proxy, "IMAGE_CACHE_DIR", cache), patch.object(media_jobs, "DB_PATH", db), patch.object(proxy.ADMIN_STORE, "path", admin_db):
        asset_id = proxy.store_asset_bytes(source, kind="img", suffix=".png")
        record = prompt_memory.get(asset_id, person["id"], path=db)
    assert record and record["prompt_text"] == sentinel
    assert prompt_memory.get(asset_id, "person-b", path=db) is None
    assert all(sentinel.encode() not in path.read_bytes() for path in _files(cache))


@pytest.mark.parametrize("reason", ["request_forget", "server_disabled"])
def test_forget_or_kill_switch_leaves_no_prompt_bytes_in_data_or_cache(tmp_path, reason):
    sentinel = "forget-sentinel-ef47"
    source = media_metadata.embed_png_text(PNG_1X1, {"prompt": sentinel, "workflow": sentinel})
    cache = tmp_path / "cache"
    db = tmp_path / "data" / "media_jobs.db"
    admin_db = tmp_path / "data" / "admin.db"
    admin_store.init_db(admin_db)
    context = media_delivery.begin(profile_default="none")
    context.prompt_decision = prompt_memory.Decision("person-a", False, reason)
    context.prompt_text = sentinel
    with patch.object(proxy, "IMAGE_CACHE_DIR", cache), patch.object(media_jobs, "DB_PATH", db), patch.object(proxy.ADMIN_STORE, "path", admin_db):
        proxy.store_asset_bytes(source, kind="img", suffix=".png")
    assert all(sentinel.encode() not in path.read_bytes() for root in (tmp_path / "data", cache) for path in _files(root))


def test_failed_payload_is_removed(tmp_path):
    payload_dir = tmp_path / "cache" / "payloads"
    payload_dir.mkdir(parents=True)
    file = payload_dir / "mj_failed.json"
    file.write_text('{"prompt":"orphan-sentinel"}', encoding="utf-8")
    temporary = payload_dir / "mj_failed.json.tmp"
    temporary.write_text('{"prompt":"temporary-sentinel"}', encoding="utf-8")
    with patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", payload_dir), patch.object(media_jobs, "DB_PATH", tmp_path / "data" / "jobs.db"):
        proxy._mark_image_media_job_failed("mj_failed", "failed")
    assert not file.exists()
    assert not temporary.exists()


def test_pruner_removes_interrupted_payload_temp_file(tmp_path):
    payload_dir = tmp_path / "cache" / "payloads"
    payload_dir.mkdir(parents=True)
    file = payload_dir / "mj_interrupted.json.tmp"
    file.write_text('{"prompt":"orphan-sentinel"}', encoding="utf-8")
    jobs_db = tmp_path / "data" / "jobs.db"
    media_jobs.init_db(jobs_db)
    with patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", payload_dir), patch.object(media_jobs, "DB_PATH", jobs_db):
        assert proxy._prune_orphaned_media_job_payloads() == 1
    assert not file.exists()


def test_owner_kill_switch_removes_pending_payload_files(tmp_path):
    payload_dir = tmp_path / "cache" / "payloads"
    payload_dir.mkdir(parents=True)
    normal = payload_dir / "mj_waiting.json"
    interrupted = payload_dir / "mj_writing.json.tmp"
    normal.write_text('{"prompt":"pending-secret"}', encoding="utf-8")
    interrupted.write_text('{"prompt":"interrupted-secret"}', encoding="utf-8")
    with patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", payload_dir):
        proxy._purge_prompt_payload_files()
    assert list(payload_dir.iterdir()) == []


def test_async_member_off_stays_forgotten_if_enabled_before_worker_starts(tmp_path):
    admin_db = tmp_path / "data" / "admin.db"
    payload_dir = tmp_path / "cache" / "payloads"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "maker", email="maker@example.test", path=admin_db)
    app = admin_store.create_app("Maker", kind="personal", owner_user_id=person["id"], path=admin_db)
    admin_store.put_prompt_memory_preference(person["id"], remember=False, retention="never", path=admin_db)
    auth = proxy.AuthContext(identity=proxy.AuthIdentity(
        name="maker", key_hash="sha256:maker", app_id=app["id"], person_id=person["id"],
    ))
    request = SimpleNamespace(
        headers={"host": "testserver"}, url=SimpleNamespace(scheme="http", netloc="testserver"),
    )
    job_id = "mj_sticky_forget"
    with patch.object(proxy.ADMIN_STORE, "path", admin_db), patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", payload_dir):
        try:
            proxy._persist_image_media_job_payload(
                job_id, body={"prompt": "sticky-secret"}, request=request, auth_context=auth,
            )
            payload = proxy._load_image_media_job_payload(job_id)
            assert payload and payload["body"]["extensions"][prompt_memory.NAMESPACE] == {"mode": "forget"}
            assert not payload_dir.exists()
            admin_store.put_prompt_memory_preference(person["id"], remember=True, retention="never", path=admin_db)
            decision = prompt_memory.decide_for_app(app["id"], payload["body"], admin_path=admin_db)
            assert decision.reason == "request_forget" and not decision.keep
        finally:
            proxy._delete_image_media_job_payload(job_id)

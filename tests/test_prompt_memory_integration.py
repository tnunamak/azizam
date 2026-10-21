"""Gateway cache writes keep prompt bytes in the dedicated maker record only."""

from __future__ import annotations

import base64
import asyncio
import os
import io
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image
import proxy
from tests.support.access import auth_identity, authenticated, everything, identity  # noqa: E402
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, scenario
from gateway import admin_store, media_delivery, media_jobs, media_metadata, prompt_memory, receipt_store, retention
from gateway.receipts import RouteReceipt


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
    context = media_delivery.begin(plan_bound="none")
    context.person_id = person["id"]
    context.prompt_decision = prompt_memory.Decision(person["id"], True, "remembered")
    context.prompt_text = sentinel
    with patch.object(proxy, "IMAGE_CACHE_DIR", cache), patch.object(media_jobs, "DB_PATH", db), patch.object(proxy.ADMIN_STORE, "path", admin_db):
        asset_id = proxy.store_asset_bytes(source, kind="img", suffix=".png")
        record = prompt_memory.get(asset_id, person["id"], path=db)
    assert record and record["prompt_text"] == sentinel
    assert prompt_memory.get(asset_id, "person-b", path=db) is None
    assert all(sentinel.encode() not in path.read_bytes() for path in _files(cache))


def test_library_receipt_uses_the_committed_owner_cap_deadline(tmp_path):
    cache = tmp_path / "cache"
    db = tmp_path / "data" / "media_jobs.db"
    admin_db = tmp_path / "data" / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "receipt-maker", path=admin_db)
    admin_store.put_operator_setting(
        "prompt_memory",
        {"default_remember": True, "never_keep": False, "retention_cap": "3_months"},
        path=admin_db,
    )
    receipt = RouteReceipt(protocol="openai.images", intent="image.generate")
    context = media_delivery.begin(plan_bound="none", receipt=receipt)
    context.person_id = person["id"]
    context.prompt_decision = prompt_memory.Decision(person["id"], True, "remembered")
    context.prompt_text = "private receipt prompt"
    with patch.object(proxy, "IMAGE_CACHE_DIR", cache), patch.object(media_jobs, "DB_PATH", db), patch.object(proxy.ADMIN_STORE, "path", admin_db):
        asset_id = proxy.store_asset_bytes(PNG_1X1, kind="img", suffix=".png")
        row = prompt_memory.get(asset_id, person["id"], path=db)
    assert row is not None
    assert receipt.to_dict("client")["retention"] == [{
        "kind": "library_prompt", "decision": "kept", "reason": "kept",
        "kept_until_ms": row["kept_until"] * 1000,
        "parts": ["accepted"], "limited_by": "owner_cap",
    }]


def test_library_receipt_does_not_claim_kept_after_a_writer_failure(tmp_path):
    cache = tmp_path / "cache"
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "receipt-maker-fail", path=admin_db)
    receipt = RouteReceipt(protocol="openai.images", intent="image.generate")
    context = media_delivery.begin(plan_bound="none", receipt=receipt)
    context.person_id = person["id"]
    context.prompt_decision = prompt_memory.Decision(person["id"], True, "remembered")
    context.prompt_text = "private receipt prompt"
    with patch.object(proxy, "IMAGE_CACHE_DIR", cache), patch.object(proxy.ADMIN_STORE, "path", admin_db), patch.object(prompt_memory, "safe_put", return_value=False):
        proxy.store_asset_bytes(PNG_1X1, kind="img", suffix=".png")
    assert receipt.to_dict("client")["retention"] == [{
        "kind": "library_prompt", "decision": "failed", "reason": "write_failed",
    }]


def test_media_job_submission_receipt_leaves_retention_to_the_work_receipt(tmp_path, monkeypatch):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "queued-maker", path=admin_db)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin_db)
    submit = RouteReceipt(path="/gateway/v1/media/jobs/image-generations", protocol="openai.images", intent="image.generate")
    submit.retention = None
    context = media_delivery.begin(plan_bound="none", receipt=submit)
    context.person_id = person["id"]
    assert proxy._note_media_metadata_body({"prompt": "queued private prompt"}) is None
    assert "retention" not in submit.to_dict("client")

    work = RouteReceipt(path="/v1/images/generations", protocol="openai.images", intent="image.generate")
    context = media_delivery.begin(plan_bound="none", receipt=work)
    context.person_id = person["id"]
    assert proxy._note_media_metadata_body({"prompt": "work private prompt"}) is None
    assert work.to_dict("client")["retention"] == [{
        "kind": "library_prompt", "decision": "not_kept", "reason": "incomplete",
    }]


def test_image_job_submit_receipt_has_no_outcome_and_worker_receipt_reports_the_write(tmp_path):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "image-job-maker", path=admin_db)
    admin_store.put_operator_setting(
        "prompt_memory", {"default_remember": True, "never_keep": False, "retention_cap": "3_months"},
        path=admin_db,
    )

    async def drive(rig):
        response = await rig.call(
            "POST", "/gateway/v1/media/jobs/image-generations", label="submit",
            json_body={"prompt": "queued private prompt", "n": 1, "response_format": "url", "background": "opaque"},
        )
        assert response.status_code in (200, 202), response.body
        queued = receipt_store.recent(path=rig.receipts_db, audience="client")
        submit = next(row for row in queued if row["path"] == "/gateway/v1/media/jobs/image-generations")
        assert "retention" not in submit
        await proxy._run_image_media_job(rig.scheduled[0])

    with GatewayRig("receipt_job", tmp_path / "rig") as rig:
        rig.set(proxy.ADMIN_STORE, "path", admin_db)
        rig._exit.enter_context(authenticated(identity(
            everything(media_metadata="none", receipt_detail="full"), person_id=person["id"])))
        rig.set(proxy, "_schedule_image_media_job", lambda job_id: rig.scheduled.append(job_id) or True)
        fake = rig.engine(
            "comfy", scenario("comfyui", "receipt-job", comfy_exchanges(
                views=[(PNG_1X1, "image/png")], strict=False,
            )), host="comfy.receipt.test",
        )
        rig.set(proxy, "COMFY_HOST", fake.base_url)
        asyncio.run(drive(rig))
        work = next(row for row in receipt_store.recent(path=rig.receipts_db, audience="client")
                    if row["path"] == "/v1/images/generations")
        assert work["retention"][0]["decision"] == "kept"
        assert prompt_memory.get_person_prompts(person["id"], path=rig.media_jobs_db)


def test_one_failed_image_write_does_not_erase_an_earlier_kept_image_receipt(tmp_path):
    cache, db, admin_db = tmp_path / "cache", tmp_path / "media.db", tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "two-images", path=admin_db)
    receipt = RouteReceipt(protocol="openai.images", intent="image.generate")
    context = media_delivery.begin(plan_bound="none", receipt=receipt)
    context.person_id = person["id"]
    context.prompt_decision = prompt_memory.Decision(person["id"], True, "remembered")
    context.prompt_text = "private two-image prompt"
    second = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(second, format="PNG")
    with patch.object(proxy, "IMAGE_CACHE_DIR", cache), patch.object(media_jobs, "DB_PATH", db), patch.object(proxy.ADMIN_STORE, "path", admin_db):
        first_id = proxy.store_asset_bytes(PNG_1X1, kind="img", suffix=".png")
        first = prompt_memory.get(first_id, person["id"], path=db)
        with patch.object(prompt_memory, "safe_put", return_value=False):
            proxy.store_asset_bytes(second.getvalue(), kind="img", suffix=".png")
    assert first is not None
    assert receipt.to_dict("client")["retention"] == [{
        "kind": "library_prompt", "decision": "kept", "reason": "kept",
        "kept_until_ms": first["kept_until"] * 1000 if first["kept_until"] is not None else None,
        "parts": ["accepted"], "limited_by": "member_choice",
    }]


def test_media_receipt_preserves_storage_unavailable_without_failing_request(tmp_path):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "busy-media", path=admin_db)
    receipt = RouteReceipt(protocol="openai.images", intent="image.generate")
    context = media_delivery.begin(plan_bound="none", receipt=receipt)
    context.person_id = person["id"]
    unavailable = prompt_memory.Decision(person["id"], False, "storage_unavailable")
    with patch.object(prompt_memory, "decide_for_person", return_value=unavailable):
        assert proxy._note_media_metadata_body({"prompt": "private busy prompt"}) is None
    assert receipt.to_dict("client")["retention"] == [{
        "kind": "library_prompt", "decision": "not_kept", "reason": "storage_unavailable",
    }]


def test_media_receipt_names_an_absent_prompt_as_no_content(tmp_path):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "empty-media", path=admin_db)
    admin_store.put_operator_setting("prompt_memory", {"default_remember": True, "never_keep": False, "retention_cap": "never"}, path=admin_db)
    receipt = RouteReceipt(protocol="openai.images", intent="image.generate")
    context = media_delivery.begin(plan_bound="none", receipt=receipt)
    context.person_id = person["id"]
    with patch.object(proxy.ADMIN_STORE, "path", admin_db):
        assert proxy._note_media_metadata_body({}) is None
    assert receipt.to_dict("client")["retention"] == [{
        "kind": "library_prompt", "decision": "not_kept", "reason": "no_content",
    }]


def test_erasure_cutoff_wins_over_default_remember_for_an_explicit_no_person_store(tmp_path):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    admin_store.put_operator_setting("prompt_memory", {"default_remember": False, "never_keep": False, "retention_cap": "never"}, path=admin_db)
    started = int(time.time()) - 5
    with sqlite3.connect(admin_db) as conn:
        retention.ensure_erasure_cutoff_table(conn)
        conn.execute("INSERT INTO prompt_memory_erasure_cutoffs(person_id,cutoff_at) VALUES('*',?)", (started + 1,))
    outcomes = []
    decision = retention.Decision(None, False, "no_person", created_at=started, kind=retention.CONVERSATION)
    assert not retention.write(retention.CONVERSATION, admin_path=admin_db, person_id=None,
                               decision=decision, created_at=started,
                               commit=lambda _deadline: pytest.fail("cutoff should refuse the write"),
                               on_result=outcomes.append)
    assert outcomes[0].reason == "erasure_cutoff"


@pytest.mark.parametrize(("controls", "reason"), [
    ({"extensions": {prompt_memory.NAMESPACE: {"mode": "forget"}}}, "request_forget"),
    ({}, "server_disabled"),
])
def test_media_receipt_names_the_control_that_prevented_prompt_retention(tmp_path, controls, reason):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "receipt-control", path=admin_db)
    if reason == "server_disabled":
        admin_store.put_operator_setting(
            "prompt_memory", {"default_remember": True, "never_keep": True, "retention_cap": "never"},
            path=admin_db,
        )
    receipt = RouteReceipt(protocol="openai.images", intent="image.generate")
    context = media_delivery.begin(plan_bound="none", receipt=receipt)
    context.person_id = person["id"]
    with patch.object(proxy.ADMIN_STORE, "path", admin_db):
        assert proxy._note_media_metadata_body({"prompt": "private control prompt", **controls}) is None
    assert receipt.to_dict("client")["retention"] == [{
        "kind": "library_prompt", "decision": "not_kept", "reason": reason,
    }]


@pytest.mark.parametrize("reason", ["request_forget", "server_disabled"])
def test_forget_or_kill_switch_leaves_no_prompt_bytes_in_data_or_cache(tmp_path, reason):
    sentinel = "forget-sentinel-ef47"
    source = media_metadata.embed_png_text(PNG_1X1, {"prompt": sentinel, "workflow": sentinel})
    cache = tmp_path / "cache"
    db = tmp_path / "data" / "media_jobs.db"
    admin_db = tmp_path / "data" / "admin.db"
    admin_store.init_db(admin_db)
    context = media_delivery.begin(plan_bound="none")
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
    os.utime(temporary, (1, 1))
    with patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", payload_dir), patch.object(media_jobs, "DB_PATH", tmp_path / "data" / "jobs.db"):
        proxy._mark_image_media_job_failed("mj_failed", "failed")
    assert not file.exists()
    assert not temporary.exists()


def test_pruner_keeps_payload_without_job_history(tmp_path):
    payload_dir = tmp_path / "cache" / "payloads"
    payload_dir.mkdir(parents=True)
    file = payload_dir / "mj_interrupted.json.tmp"
    file.write_text('{"prompt":"orphan-sentinel"}', encoding="utf-8")
    jobs_db = tmp_path / "data" / "jobs.db"
    media_jobs.init_db(jobs_db)
    with patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", payload_dir), patch.object(media_jobs, "DB_PATH", jobs_db):
        assert proxy._prune_orphaned_media_job_payloads() == 0
    assert file.exists()


def test_owner_kill_switch_removes_pending_payload_files(tmp_path):
    payload_dir = tmp_path / "cache" / "payloads"
    payload_dir.mkdir(parents=True)
    normal = payload_dir / "mj_waiting.json"
    interrupted = payload_dir / "mj_writing.json.tmp"
    normal.write_text('{"prompt":"pending-secret"}', encoding="utf-8")
    interrupted.write_text('{"prompt":"interrupted-secret"}', encoding="utf-8")
    os.utime(interrupted, (1, 1))
    jobs_db = tmp_path / "data" / "jobs.db"
    media_jobs.init_db(jobs_db)
    with patch.object(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", payload_dir), patch.object(media_jobs, "DB_PATH", jobs_db):
        proxy._purge_prompt_payload_files()
    assert list(payload_dir.iterdir()) == []


def test_async_member_off_stays_forgotten_if_enabled_before_worker_starts(tmp_path):
    admin_db = tmp_path / "data" / "admin.db"
    payload_dir = tmp_path / "cache" / "payloads"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "maker", email="maker@example.test", path=admin_db)
    app = admin_store.create_app("Maker", kind="personal", owner_user_id=person["id"], access_plan="person", path=admin_db)
    admin_store.put_prompt_memory_preference(person["id"], remember=False, retention="never", path=admin_db)
    auth = proxy.AuthContext(identity=auth_identity(
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
            decision = prompt_memory.decide_for_person(person["id"], payload["body"], admin_path=admin_db)
            assert decision.reason == "request_forget" and not decision.keep
        finally:
            proxy._delete_image_media_job_payload(job_id)

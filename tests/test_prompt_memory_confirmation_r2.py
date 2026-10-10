"""Privacy regressions from the independent Round 2 confirmation."""
import json
import asyncio
import sqlite3
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, identity_api, media_jobs, prompt_memory
from tests.test_identity_api import BINDING, GOOD, PHOTO, TEMPLATE, FakeClient
from tests.test_prompt_memory_final_cleanup import household
from tests.test_audio_media_job_integration import _StubJsonRequest
from tests.test_me_prompt_memory import _client
from tests.test_identity_api import build
from tests.test_settings_api import settings, OWNER

FORGET = {prompt_memory.NAMESPACE: {"mode": "forget"}}


@pytest.mark.parametrize("extensions", ["not json", "[]", "null", "42"])
@pytest.mark.parametrize("route", [identity_api.IDENTITY_PATH, identity_api.LIKENESS_PATH])
def test_identity_rejects_malformed_extensions_clearly(route, extensions):
    client, _, _ = build([GOOD])
    files = [("image", ("p.png", PHOTO, "image/png"))] if route == identity_api.IDENTITY_PATH else [
        ("reference", ("p.png", PHOTO, "image/png")), ("candidate", ("c.png", GOOD, "image/png"))]
    result = client.post(route, files=files, data={"prompt": "hello", "extensions": extensions})
    assert result.status_code == 400, result.text
    assert result.json()["error"]["param"] == "extensions"
    assert "JSON object" in result.json()["error"]["message"]


def test_video_edit_accepts_json_extensions_field(household):
    from tests.test_media_provenance import NativeWorkflowRunTests
    response, _raw = asyncio.run(NativeWorkflowRunTests()._run({"extensions": json.dumps(FORGET)}))
    assert response.status_code == 200, response.body
    assert response.media_type == "video/mp4"
    assert not proxy.media_metadata.read_media_tags(response.body, ".mp4")


@pytest.mark.parametrize("intent,extract", [
    ("image.generate", proxy._image_generation_body_from_media_job_payload),
    ("audio.generate", proxy._audio_generation_body_from_media_job_payload),
    ("music.generate", proxy._audio_generation_body_from_media_job_payload),
    ("sfx.generate", proxy._audio_generation_body_from_media_job_payload),
    ("video.generate", proxy._video_generation_body_from_media_job_payload),
])
@pytest.mark.parametrize("field", ["request", "parameters"])
def test_queued_outer_forget_reaches_execution_and_never_hits_disk(household, intent, extract, field):
    hh = household
    body = extract({"intent": intent, field: {"prompt": "queued private sentinel",
                   "extensions": {"com.azizam.workflow": {"seed": 42}}}, "extensions": FORGET})
    assert body["extensions"][prompt_memory.NAMESPACE] == {"mode": "forget"}
    assert body["extensions"]["com.azizam.workflow"] == {"seed": 42}
    proxy._persist_image_media_job_payload("mj_outer", body=body,
                                         request=_StubJsonRequest({}), auth_context=hh.auth)
    assert proxy._load_image_media_job_payload("mj_outer")["body"] == body
    assert not list(hh.root.rglob("mj_outer*.json*"))
    assert b"queued private sentinel" not in b"".join(p.read_bytes() for p in hh.root.rglob("*") if p.is_file())


@pytest.mark.parametrize("mode", ["forget", "member-off"])
@pytest.mark.parametrize("route", [identity_api.IDENTITY_PATH, identity_api.LIKENESS_PATH])
@pytest.mark.parametrize("encoding", ["json", "bracket"])
def test_multipart_identity_privacy_succeeds_without_prompt_storage(household, mode, route, encoding):
    hh = household
    if mode == "member-off":
        prompt_memory.put_member(hh.robin["id"], remember=False, retention="never", path=hh.admin)
    app = FastAPI()
    async def ready():
        return None
    async def render(*args):
        return GOOD
    def store(request, auth, content, fmt):
        asset = proxy.store_asset_bytes(content, kind="img", suffix=".png", owner_key_hash=proxy._asset_owner_key(auth))
        return {"asset_id": asset}
    identity_api.register_identity_routes(app, identity_api.IdentityApiDeps(
        authenticate=lambda *_: hh.auth, enforce_policy=lambda *_: None,
        ensure_face_service=ready, face_client=FakeClient, run_workflow=render,
        template=lambda: (TEMPLATE, BINDING), store_image=store, models=("buffalo_l",), note_delivery=proxy._identity_note_delivery,
    ))
    files = [("image", ("p.png", PHOTO, "image/png"))] if route == identity_api.IDENTITY_PATH else [
        ("reference", ("p.png", PHOTO, "image/png")), ("candidate", ("c.png", GOOD, "image/png"))]
    controls = {"extensions": json.dumps(FORGET)} if encoding == "json" else {
        f"extensions[{prompt_memory.NAMESPACE}][mode]": "forget"}
    result = TestClient(app).post(route, files=files, data={"prompt": "multipart private sentinel",
                                "mode": "sync", "attempts": "1", **controls})
    assert result.status_code == 200, result.text
    assert not prompt_memory.get_person_prompts(hh.robin["id"])
    assert b"multipart private sentinel" not in b"".join(p.read_bytes() for p in hh.root.rglob("*") if p.is_file())


def test_retention_trim_runs_after_admin_commit_without_its_write_lock(household, monkeypatch):
    hh = household
    client = _client(hh.admin, hh.jobs, user_id=hh.robin["id"])
    real_trim = prompt_memory.tighten_retention
    def trim(*args, **kwargs):
        assert prompt_memory.get_member(hh.robin["id"], path=hh.admin)["retention"] == "3_months"
        with sqlite3.connect(hh.admin, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
        assert prompt_memory.put("img_during_trim", hh.robin["id"], "new prompt", kept_until=None, admin_path=hh.admin)
        return real_trim(*args, **kwargs)
    monkeypatch.setattr(prompt_memory, "tighten_retention", trim)
    result = client.put("/me/api/prompt-memory", json={"remember": True, "retention": "3_months"},
                        headers={"x-azizam-user-action": "1"})
    assert result.status_code == 200, result.text
    assert prompt_memory.get("img_during_trim", hh.robin["id"])


def test_household_retention_trim_does_not_hold_admin_lock(settings, monkeypatch, tmp_path):
    client, admin, _ = settings
    monkeypatch.setattr(media_jobs, "DB_PATH", tmp_path / "jobs.db")
    def trim(*args, **kwargs):
        assert prompt_memory.household_settings(admin_path=admin)["retention_cap"] == "3_months"
        with sqlite3.connect(admin, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(prompt_memory, "tighten_retention", trim)
    before = client.get("/admin/api/settings/prompt-memory", headers=OWNER).json()
    result = client.put("/admin/api/settings/prompt-memory", json={**before["prompt_memory"], "retention_cap": "3_months"},
                        headers={**OWNER, "if-match": before["revision"]})
    assert result.status_code == 200, result.text


def test_failed_admin_retention_write_cannot_trim_prompts(household, monkeypatch):
    hh = household
    client = _client(hh.admin, hh.jobs, user_id=hh.robin["id"])
    def fail(*args, **kwargs):
        callback = kwargs.get("validate_in_transaction")
        if callback:
            callback(None)
        raise RuntimeError("setting commit failed")
    monkeypatch.setattr(admin_store, "put_prompt_memory_preference", fail)
    with patch.object(prompt_memory, "tighten_retention") as trim:
        result = client.put("/me/api/prompt-memory", json={"remember": True, "retention": "3_months"},
                            headers={"x-azizam-user-action": "1"})
    assert result.status_code == 503
    trim.assert_not_called()


def test_trim_failure_reports_that_setting_was_saved(household, monkeypatch):
    hh = household
    client = _client(hh.admin, hh.jobs, user_id=hh.robin["id"])
    monkeypatch.setattr(prompt_memory, "tighten_retention", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("busy")))
    result = client.put("/me/api/prompt-memory", json={"remember": True, "retention": "3_months"},
                        headers={"x-azizam-user-action": "1"})
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "prompt_retention_pending"
    assert "setting was saved" in result.json()["error"]["message"]
    assert prompt_memory.get_member(hh.robin["id"], path=hh.admin)["retention"] == "3_months"


def test_completed_household_erasure_is_not_replayed_every_maintenance(household):
    hh = household
    admin_store.put_operator_setting("prompt_memory", {"never_keep": True}, path=hh.admin)
    proxy._maintain_household_prompt_memory()
    with patch.object(media_jobs, "erase_prompt_derivatives") as erase, patch.object(prompt_memory, "prune_all_before") as prune:
        proxy._maintain_household_prompt_memory()
    erase.assert_not_called()
    prune.assert_not_called()
    assert prompt_memory.household_erasure_cutoff(admin_path=hh.admin)


def test_delete_failure_after_durable_intent_reports_retrying(household):
    hh = household
    def purge(person):
        prompt_memory.mark_person_erasure(person, admin_path=hh.admin)
        raise RuntimeError("erase temporarily unavailable")
    client = _client(hh.admin, hh.jobs, user_id=hh.robin["id"], on_person_prompt_purge=purge)
    result = client.delete("/me/api/prompt-memory/prompts", headers={"x-azizam-user-action": "1"})
    assert result.status_code == 202, result.text
    assert result.json()["prompt_purge"]["retry_scheduled"] is True
    assert result.json()["prompt_purge"]["database_pending"] is True
    assert not result.json()["prompt_purge"].get("retry_required")


def test_forgotten_image_job_cannot_reuse_a_remembered_idempotency_result(household):
    hh = household
    body = {"prompt": "same request"}
    remembered = proxy._image_media_job_request_id(body=body, auth_context=hh.auth,
        idempotency_key="same-client-key", fallback_request_id="one")
    forgotten = proxy._image_media_job_request_id(body={**body, "extensions": FORGET}, auth_context=hh.auth,
        idempotency_key="same-client-key", fallback_request_id="two")
    assert remembered[0] != forgotten[0]
    assert forgotten == proxy._image_media_job_request_id(body={**body, "extensions": FORGET}, auth_context=hh.auth,
        idempotency_key="same-client-key", fallback_request_id="three")

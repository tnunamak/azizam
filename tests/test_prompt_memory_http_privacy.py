"""End-to-end privacy checks for a buffered speech request."""

from __future__ import annotations

import json
import shutil
import wave
from io import BytesIO
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import proxy
from tests.support.access import access, auth_identity, plan_for  # noqa: E402
from gateway import admin_store, media_jobs, media_metadata, receipt_store, prompt_memory
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401


def _wav() -> bytes:
    out = BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x00\x00" * 12)
    return out.getvalue()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
@pytest.mark.parametrize("policy", ["request_forget", "server_disabled", "remember"])
def test_speech_http_request_keeps_no_prompt_bytes_in_gateway_data_or_cache(tmp_path, policy, fake_upstreams, monkeypatch):
    sentinel = f"speech-private-{policy}-e97d"
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    data_dir.mkdir()
    cache_dir.mkdir()
    admin_db = data_dir / "admin.db"
    jobs_db = data_dir / "media_jobs.db"
    receipts_db = data_dir / "receipts.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "speech-maker", email="maker@example.test", path=admin_db)
    app = admin_store.create_app("Try", kind="personal", owner_user_id=person["id"], access_plan="person", path=admin_db)
    auth = proxy.AuthContext(identity=auth_identity(name="Try", key_hash="sha256:maker", app_id=app["id"], person_id=person["id"], access=access(plan_for("maker"))))
    media_jobs.init_db(jobs_db)
    receipt_store.init_db(receipts_db)
    if policy == "server_disabled":
        admin_store.put_operator_setting(
            "prompt_memory", {"default_remember": True, "never_keep": True, "retention_cap": "never"},
            path=admin_db,
        )
    upstream = media_metadata.embed_media_tags(_wav(), ".wav", {"comment": sentinel})

    speech = fake_upstreams.asgi(load_scenario("openai-compatible/speech-buffered", wav_hex=upstream.hex()))
    monkeypatch.setattr(proxy, "host_for_capability", lambda _: speech.base_url)
    # One engine id ("tts") serves every model: the privacy contract does not depend on the tier.
    monkeypatch.setattr(proxy, "resolve_model_for_operation", lambda op, model: proxy.ModelResolution(model, model, op))

    body = {"model": "tts-1", "voice": "alloy", "input": sentinel, "response_format": "wav"}
    if policy == "request_forget":
        body["extensions"] = {"com.azizam.prompts": {"mode": "forget"}}
    with patch.object(proxy, "TTS_ENABLED", True), \
         patch.object(proxy, "IMAGE_CACHE_DIR", cache_dir), \
         patch.object(proxy.ADMIN_STORE, "path", admin_db), \
         patch.object(media_jobs, "DB_PATH", jobs_db), \
         patch.object(receipt_store, "DB_PATH", receipts_db), \
         patch.object(proxy, "_enforce_runtime_route_policy", return_value=None), \
         patch.object(proxy, "_service_availability_response", return_value=None), \
         patch.object(proxy, "_ensure_speech_service_ready", AsyncMock(return_value=None)), \
         patch.object(proxy, "authenticate_request", return_value=auth):
        response = TestClient(proxy.app).post("/v1/audio/speech", json=body)

    assert response.status_code == 200, response.text
    [forwarded] = speech.calls("speech")
    assert forwarded.json["input"] == sentinel
    assert sentinel not in json.dumps(media_metadata.read_media_tags(response.content, ".wav"))
    if policy == "remember":
        rows = prompt_memory.get_person_prompts(person["id"], path=jobs_db)
        assert len(rows) == 1
        assert next(iter(rows.values()))["prompt_text"] == sentinel
        assert all(sentinel.encode() not in file.read_bytes() for file in cache_dir.rglob("*") if file.is_file())
    else:
        assert prompt_memory.get_person_prompts(person["id"], path=jobs_db) == {}
        assert all(sentinel.encode() not in file.read_bytes()
                   for root in (data_dir, cache_dir) for file in root.rglob("*") if file.is_file())

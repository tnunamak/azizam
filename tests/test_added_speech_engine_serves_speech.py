"""A speech engine an owner adds as a registry record serves POST /v1/audio/speech.

The owner path as it exists today. The Add engine dialog offers only chat and embeddings (its capability rows are
registry/engine_definitions.json "capabilities"), so a speech server is added as a raw record through the headless
route of docs/FIRST_RUN_SETUP.md: ``POST /admin/api/operator/services`` with ``If-Match``, ``protocol: inline-tts``,
the speech capability words and one model the engine offers. The fake server is an OpenAI-shaped speech server
(tests/support/fake_upstream, scenario ``openai-compatible/speech-buffered``). The request goes in with an App key,
through the real model resolution, to the fake, and the audio comes back.

Nothing about the speech route is patched: not the model resolution, not the engine lookup, not the readiness wait.
Only the data directories are moved to the test's temporary directory.
"""

from __future__ import annotations

import io
import wave

import pytest

import proxy
from gateway import admin_store, media_jobs, receipt_store
from gateway.runtime_auth import hash_api_key
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

ADDED_MODEL = "added-voice"


def _wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x01\x00\x02\x00" * 6)
    return out.getvalue()


def _app_key(gateway, owner_id: str) -> str:
    app = admin_store.create_app("Voice app", kind="service", owner_user_id=owner_id, access_plan="person", path=gateway.path)
    key = "opk_added_speech_fixture_key"
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"], hash=hash_api_key(key),
                                       prefix=key[:16], created_by=owner_id, path=gateway.path)
    return key


def _rig(gateway, fake_upstreams, monkeypatch, tmp_path):
    """The owner adds a speech server as a registry record. Returns (owner client, App key, fake server)."""
    for module in (receipt_store, media_jobs):
        db = tmp_path / f"{module.__name__}.db"
        module.init_db(db)
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    # On a real loopback port: a record names host and port, as an owner's does for a server on this computer.
    fake = fake_upstreams.loopback(load_scenario("openai-compatible/speech-buffered", wav_hex=_wav().hex()))
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    etag = owner.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    added = owner.post(
        "/admin/api/operator/services",
        headers={**csrf_for(owner), "If-Match": etag},
        json={
            "name": "garage-voice",
            "base_url": fake.base_url,
            "protocol": "inline-tts",
            "capabilities": ["audio", "speech"],
            "models": [{"public": ADDED_MODEL, "upstream": "fake-speech-1"}],
        },
    )
    assert added.status_code == 201, added.text
    return owner, key, fake


def _speak(owner, key, model):
    return owner.post(
        "/v1/audio/speech",
        headers={"authorization": f"Bearer {key}"},
        json={"model": model, "voice": "alloy", "input": "hello", "response_format": "wav"},
    )


def test_a_speech_engine_added_as_a_registry_record_serves_speech(gateway, fake_upstreams, monkeypatch, tmp_path):
    owner, key, fake = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)

    spoken = _speak(owner, key, ADDED_MODEL)

    assert spoken.status_code == 200, spoken.text
    assert spoken.headers["content-type"].startswith("audio/")
    assert spoken.content[:4] == b"RIFF"
    [forwarded] = fake.calls("speech")
    assert forwarded.json["input"] == "hello"


def test_control_the_same_record_serves_speech_once_the_speech_section_names_it(gateway, fake_upstreams, monkeypatch, tmp_path):
    """The rig is sound: only the model_aliases.speech.engines entry (the speech route's own table) is missing above."""
    owner, key, fake = _rig(gateway, fake_upstreams, monkeypatch, tmp_path)
    revision = owner.get("/admin/api/config/sections/model_aliases", headers=csrf_for(owner)).json()["revision"]
    written = owner.patch(
        "/admin/api/config/changes",
        headers=csrf_for(owner),
        json={
            "items": [{"section": "model_aliases", "pointer": "/speech/engines/garage-voice", "op": "set",
                       "value": {"service": "garage-voice", "model": "fake-speech-1"}}],
            "if_match": {"model_aliases": revision},
        },
    )
    assert written.status_code == 200, written.text

    spoken = _speak(owner, key, "garage-voice")

    assert spoken.status_code == 200, spoken.text
    assert spoken.content[:4] == b"RIFF"
    [forwarded] = fake.calls("speech")
    assert forwarded.json["model"] == "fake-speech-1"

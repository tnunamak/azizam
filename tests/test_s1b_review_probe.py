"""Review probes for the ``openai-speech`` definition (train 84 review, scratch file).

Each test settles one review question against the real bodies captured from a running
audio.cpp speech server today (no /v1/audio/models route, an empty voices list, one model
whose ``task`` is ``tts``), or against the pure alias tables. Nothing here names a product
in code: the definition is named for the API dialect.
"""

from __future__ import annotations

import asyncio
import io
import json
import wave
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

import proxy
from gateway import admin_store, derived_engines, engine_check, media_jobs, receipt_store, stable_aliases
from gateway.engine_check import Answer, KeyInput
from gateway.engine_definitions import load_engine_catalog
from tests.support.engine_dns import use_fake_engine_dns
from tests.support.fake_upstream import fake_upstreams, load_scenario, scenario_from_data  # noqa: F401 - fixture
from tests.support.fake_upstream.scenario import ABSENT
from tests.test_added_speech_engine_serves_speech import _app_key
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

TOKEN = "t0k3n"
SECRET = "opk_review_scrub_secret_9f"
# The bodies a real audio.cpp speech server answered today.
REAL_MODELS = {"object": "list", "data": [
    {"id": "supertonic", "object": "model", "owned_by": "engine", "family": "supertonic",
     "task": "tts", "mode": "streaming", "loaded": True},
]}
REAL_AUDIO_MODELS_404 = {"error": {"message": "unknown endpoint: /v1/audio/models", "type": "not_found"}}
REAL_VOICES_EMPTY = {"voices": []}


@pytest.fixture(autouse=True)
def fixed_baseline(monkeypatch):
    monkeypatch.setattr(engine_check, "_baseline_token", lambda: TOKEN)


@pytest.fixture(autouse=True)
def fake_engine_dns(monkeypatch):
    use_fake_engine_dns(monkeypatch)


def _definition():
    definition = load_engine_catalog().definition("openai-speech")
    assert definition is not None, "no definition named openai-speech"
    return definition


def _wav_hex() -> str:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x01\x00\x02\x00" * 200)
    return out.getvalue().hex()


def _ex(name, method, path, response, match=None):
    row = {"id": name, "match": {"method": method, "path": path}, "response": response,
           "repeat": True, "required": False}
    if match:
        row["match"].update(match)
    return row


def _server(fakes, *, models=None, voices=None, slow_voices_s=None, models_status=200):
    """A speech server answering the real captured bodies (or a variant of them)."""
    models_body = REAL_MODELS if models is None else {"object": "list", "data": models}
    models_response = ({"status": models_status, "json": models_body} if models_status == 200
                       else {"status": models_status, "json": {"error": {"message": "Unauthorized", "type": "unauthorized"}}})
    voices_body = REAL_VOICES_EMPTY if voices is None else voices
    voices_response = {"status": 200, "json": voices_body}
    if slow_voices_s:
        voices_response["wait_before_headers"] = slow_voices_s
    audio = {"status": 200, "headers": {"content-type": "audio/wav"}, "body": [{"hex": _wav_hex()}]}
    not_found = {"status": 404, "json": {"detail": "Not Found"}}
    scenario = scenario_from_data({
        "protocol": "engine-probe", "name": "speech-server",
        "provenance": {"source": "hand-written",
                       "reference": "captured bodies of a running audio.cpp speech server (review evidence, 2026-11-19)"},
        "exchanges": [
            _ex("baseline-get", "GET", "/zz-not-a-route-{{token}}", not_found),
            _ex("baseline-post", "POST", "/zz-not-a-route-{{token}}", not_found),
            _ex("route-baseline-post", "POST", "/v1/zz-not-a-route-{{token}}", not_found),
            _ex("health", "GET", "/health", {"status": 200, "json": {"status": "ok", "backend": "cuda", "models": 1}}),
            _ex("models", "GET", "/v1/models", models_response),
            _ex("audio-models", "GET", "/v1/audio/models", {"status": 404, "json": REAL_AUDIO_MODELS_404}),
            _ex("voices", "GET", "/v1/audio/voices", voices_response),
            _ex("openapi", "GET", "/openapi.json", not_found),
            _ex("capability-document", "GET", "/v1/models/capabilities", not_found),
            # The check's route probe sends an empty body; the real test and speech requests carry input.
            _ex("speech-empty", "POST", "/v1/audio/speech",
                {"status": 400, "json": {"error": {"message": "input is required", "type": "invalid_request_error"}}},
                match={"json": {"input": ABSENT}}),
            _ex("speech", "POST", "/v1/audio/speech", audio, match={"json": {"input": "Hello."}}),
            _ex("speech-hello", "POST", "/v1/audio/speech", audio, match={"json": {"input": "hello"}}),
        ],
    }, token=TOKEN)
    return fakes.loopback(scenario)


def _move_media_dbs(monkeypatch, tmp_path):
    for module in (receipt_store, media_jobs):
        db = tmp_path / f"{module.__name__}.db"
        module.init_db(db)
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")


def _write_headers(client):
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def _check(owner, address):
    checked = owner.post("/admin/api/engines/check", headers=csrf_for(owner),
                         json={"definition": "openai-speech", "values": {"address": address}})
    assert checked.status_code == 200, checked.text
    return checked.json()["check"]


def _save(owner, address, name, capabilities, models=None):
    body = {"definition": "openai-speech", "name": name, "values": {"address": address},
            "capabilities": capabilities}
    if models is not None:
        body["models"] = models
    created = owner.post("/admin/api/operator/services", headers=_write_headers(owner), json=body)
    assert created.status_code == 201, created.text
    return created


def _speak(owner, key, model, *, voice=None, with_voice=True):
    body = {"model": model, "input": "hello", "response_format": "wav"}
    if with_voice:
        body["voice"] = voice
    return owner.post("/v1/audio/speech", headers={"authorization": f"Bearer {key}"}, json=body)


def _record(gateway, service_id="garage-voice"):
    return admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"][service_id]


def _speech_calls(fake, input_text="hello"):
    return [r for r in fake.requests if r.path == "/v1/audio/speech" and r.json.get("input") == input_text]


# ---- question 1 and 5: the real captured bodies ---------------------------------------------------------------


def test_real_server_bodies_check_save_and_speech(gateway, fake_upstreams, monkeypatch, tmp_path):
    """The server that answered exactly the captured bodies: check, save and speak."""
    _move_media_dbs(monkeypatch, tmp_path)
    fake = _server(fake_upstreams)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)

    # The check ends ok and offers the one model; it says nothing about voices.
    check = _check(owner, fake.base_url)
    assert check["outcome"] == "ok", check["findings"]
    assert [row["id"] for row in check["models"]] == ["supertonic"]
    assert check.get("voices") in (None, []), check.get("voices")
    assert check.get("default_voice") is None
    [speech_row] = check["capabilities"]
    assert speech_row["id"] == "speech" and speech_row["level"] == "possible" and speech_row["ticked"] is False

    # Question 5: the "Checking the key" row is shown, though the definition sends no key.
    assert check["steps"][1] == {"label": "Checking the key", "result": "No key needed"}
    assert {f["key"] for f in _definition()["fields"]} == {"address", "models"}

    # The dialog's own save payload (only ticked rows) is refused: nothing was ticked.
    refused = owner.post("/admin/api/operator/services", headers=_write_headers(owner), json={
        "definition": "openai-speech", "name": "garage-voice", "values": {"address": fake.base_url},
        "capabilities": [row["id"] for row in check["capabilities"] if row["ticked"]],
        "models": check["models"]})
    assert refused.status_code == 400 and "at least one" in refused.text, refused.text

    # The real test speaks with the server's model, with voice "default" and with no voice.
    for voice in ("default", None):
        body = {"definition": "openai-speech", "values": {"address": fake.base_url},
                "capability": "speech", "model": "supertonic"}
        if voice is not None:
            body["voice"] = voice
        tested = owner.post("/admin/api/engines/test", headers=csrf_for(owner), json=body)
        assert tested.status_code == 200, tested.text
        assert tested.json()["test"]["ok"] is True, tested.text

    # The owner ticks speech by hand and saves; the record stores no voices and no key.
    _save(owner, fake.base_url, "garage-voice", ["speech"], models=check["models"])
    record = _record(gateway)
    assert [(row["model_group"], row["upstream_model"]) for row in record["config"]["models"]] == [("supertonic", "supertonic")]
    assert record["config"]["engine_capabilities"] == ["speech"]
    assert set(record["capabilities"]) == {"audio", "speech"}
    assert not (record.get("observations") or {}).get("voices")

    # Speech works through the gateway with voice "default" and with no voice.
    spoken = _speak(owner, key, "supertonic", voice="default")
    assert spoken.status_code == 200, spoken.text
    assert spoken.content[:4] == b"RIFF"
    [default_call] = _speech_calls(fake)
    assert default_call.json["voice"] == "default" and default_call.json["model"] == "supertonic"

    no_voice = _speak(owner, key, "supertonic", with_voice=False)
    assert no_voice.status_code == 200, no_voice.text
    assert no_voice.content[:4] == b"RIFF"
    # A request with no voice is forwarded with an empty voice string, not with the key omitted.
    [_, bare_call] = _speech_calls(fake)
    assert bare_call.json["voice"] == ""


def test_a_server_that_needs_a_key_is_reported_but_this_definition_offers_no_key_field():
    """The check row says "Key needed" for a server that answers 401, but the owner cannot give one."""
    definition = _definition()

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        path = urlsplit(url).path
        if path == "/v1/models":
            return Answer(method=method, url=url, status=401, headers={"content-type": "application/json"},
                           body=json.dumps({"error": {"message": "Unauthorized", "type": "unauthorized"}}).encode())
        return Answer(method=method, url=url, status=404, headers={"content-type": "application/json"},
                      body=b'{"detail": "Not Found"}')

    result = asyncio.run(engine_check.run_check(
        load_engine_catalog(), definition, {"address": "http://203.0.113.9:8880"},
        key=KeyInput(), fetch=fetch))
    assert result["outcome"] == "error"
    assert result["steps"][1] == {"label": "Checking the key", "result": "Key needed"}
    assert result["findings"] and result["findings"][0]["severity"] == "error"
    assert {f["key"] for f in definition["fields"]} == {"address", "models"}


# ---- question 4: the list probes share the fetch's guards ------------------------------------------------------


def test_a_voices_answer_larger_than_the_list_cap_is_skipped_not_fatal(gateway, fake_upstreams):
    """A voices body above the list probe's byte cap is dropped; the check still ends ok."""
    big = _server(fake_upstreams, voices={"voices": [f"v{i:07d}" for i in range(150000)]})  # about 1.7 MB
    owner, *_ = owner_and_admin(gateway)

    check = _check(owner, big.base_url)

    assert check["outcome"] == "ok", check["findings"]
    assert [row["id"] for row in check["models"]] == ["supertonic"]  # the model list was still read
    assert check.get("voices") in (None, [])
    voices_calls = [r for r in big.requests if r.path == "/v1/audio/voices"]
    assert len(voices_calls) == 1  # one capped read, no retries


def test_a_voices_answer_slower_than_the_probe_timeout_is_skipped_not_fatal(gateway, fake_upstreams):
    """A voices answer slower than the definition's probe timeout is dropped; the check still ends ok."""
    slow = _server(fake_upstreams, slow_voices_s=7)  # the definition's probe timeout is 5 s
    owner, *_ = owner_and_admin(gateway)

    check = _check(owner, slow.base_url)

    assert check["outcome"] == "ok", check["findings"]
    assert [row["id"] for row in check["models"]] == ["supertonic"]
    assert check.get("voices") in (None, [])


def test_a_key_echoed_by_a_voices_list_is_scrubbed_before_the_check_reads_it():
    """The list probes go through the guarded fetch: a key the server echoes never reaches the result."""
    definition = _definition()

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        path = urlsplit(url).path
        if path == "/v1/models":
            body = json.dumps(REAL_MODELS).encode()
        elif path == "/v1/audio/voices":
            body = json.dumps({"voices": [{"id": SECRET, "name": SECRET}]}).encode()
        elif path == "/v1/audio/speech":
            return Answer(method=method, url=url, status=400, headers={"content-type": "application/json"},
                          body=b'{"error": {"message": "input is required"}}')
        else:
            body = b'{"detail": "Not Found"}'
        return Answer(method=method, url=url, status=404 if path == "/v1/audio/models" else 200,
                      headers={"content-type": "application/json"}, body=body)

    result = asyncio.run(engine_check.run_check(
        load_engine_catalog(), definition, {"address": "http://203.0.113.9:8880"},
        key=KeyInput(value=SECRET, source="pasted"), fetch=fetch))
    assert result["outcome"] == "ok", result["findings"]
    assert result["voices"] == [{"id": "[hidden]", "name": "[hidden]"}]
    assert SECRET not in json.dumps(result)


# ---- question 2: which path serves a model, and what happens when ids collide ---------------------------------


def test_speech_is_served_by_the_derived_engine_not_by_a_stable_alias(gateway, fake_upstreams, monkeypatch, tmp_path):
    """A public name no alias table holds is served by the derived engine; "tts-1" is not a stable alias."""
    _move_media_dbs(monkeypatch, tmp_path)
    fake = _server(fake_upstreams)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)

    # The owner renames the server's model to a name no alias table holds.
    _save(owner, fake.base_url, "renamed-voice", ["speech"], models=[{"id": "supertonic", "public_name": "quack"}])

    spoken = _speak(owner, key, "quack")
    assert spoken.status_code == 200, spoken.text
    assert spoken.content[:4] == b"RIFF"
    # The engine path rewrote the model to the name the server is called with.
    [call] = _speech_calls(fake)
    assert call.json["model"] == "supertonic"

    # "tts-1" is one of the gateway's OpenAI names, but the release ships no speech alias for it:
    # with no record offering it, the request is refused before anything is sent.
    unknown = _speak(owner, key, "tts-1")
    assert unknown.status_code == 400 and "not available" in unknown.text, unknown.text
    assert len(_speech_calls(fake)) == 1  # nothing new reached the server


def test_a_hand_engine_entry_with_the_same_id_wins_over_the_derived_engine(gateway, fake_upstreams, monkeypatch, tmp_path):
    """A record whose model is "tts-1" is reachable, until a hand engine entry takes the id."""
    _move_media_dbs(monkeypatch, tmp_path)
    fake_a = _server(fake_upstreams, models=[{"id": "tts-1"}])
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)

    # Only the record exists: the derived engine named tts-1 serves the request.
    _save(owner, fake_a.base_url, "alpha-tts", ["speech"])
    spoken = _speak(owner, key, "tts-1")
    assert spoken.status_code == 200, spoken.text
    assert len(_speech_calls(fake_a)) == 1

    # A hand engine entry with the same id wins whole; the derived engine is no longer reachable.
    fake_b = fake_upstreams.loopback(load_scenario("openai-compatible/speech-buffered", wav_hex=_wav_hex()))
    added = owner.post("/admin/api/operator/services", headers=_write_headers(owner), json={
        "name": "beta-voice", "base_url": fake_b.base_url, "protocol": "inline-tts",
        "capabilities": ["audio", "speech"], "models": [{"public": "beta-model", "upstream": "fake-speech-1"}]})
    assert added.status_code == 201, added.text
    revision = owner.get("/admin/api/config/sections/model_aliases", headers=csrf_for(owner)).json()["revision"]
    written = owner.patch("/admin/api/config/changes", headers=csrf_for(owner), json={
        "items": [{"section": "model_aliases", "pointer": "/speech/engines/tts-1", "op": "set",
                   "value": {"service": "beta-voice", "model": "fake-speech-1"}}],
        "if_match": {"model_aliases": revision}})
    assert written.status_code == 200, written.text

    again = _speak(owner, key, "tts-1")
    assert again.status_code == 200, again.text
    assert again.content[:4] == b"RIFF"
    [hand_call] = fake_b.calls("speech")
    assert hand_call.json["input"] == "hello" and hand_call.json["model"] == "fake-speech-1"
    assert len(_speech_calls(fake_a)) == 1  # the derived engine behind the same id was not used


def test_a_derived_engine_id_equal_to_an_openai_name_shadows_the_owners_openai_pattern():
    """The engines table is read before the OpenAI-name patterns, so a record named "tts-1" takes the name over."""
    hand = {"speech": {"stable": {"tts-fast": ["omnivoice"]},
                       "openai": [{"match": "tts-1", "to": "tts-fast"}],
                       "engines": {"omnivoice": {"service": "s1", "model": "m1"}}}}
    derived = {"tts-1": {"service": "record-1", "model": "supertonic"}}

    without = stable_aliases.resolve_alias(hand, "speech", "tts-1")
    assert without is not None and without.engine == "omnivoice"  # the owner's pattern wins alone

    merged = derived_engines.with_derived_speech_engines(hand, derived)
    with_derived = stable_aliases.resolve_alias(merged, "speech", "tts-1")
    assert with_derived is not None and with_derived.layer == "engine"
    assert with_derived.engine == "tts-1" and with_derived.service == "record-1"  # the record shadowed the pattern


def test_the_release_ships_no_speech_alias_so_only_a_records_engine_serves_tts_1():
    """The release names no speech engine, alias or pattern, so a record's engine id is the only "tts-1"."""
    defaults = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "registry" / "defaults" / "model_aliases.yml").read_text(encoding="utf-8"))
    assert not (defaults.get("speech") or {})
    assert stable_aliases.resolve_alias(defaults, "speech", "tts-1") is None

    merged = derived_engines.with_derived_speech_engines(defaults, {"tts-1": {"service": "r1", "model": "supertonic"}})
    resolution = stable_aliases.resolve_alias(merged, "speech", "tts-1")
    assert resolution is not None and resolution.layer == "engine"
    assert resolution.engine == "tts-1" and resolution.service == "r1"


# ---- question 3: observations merged on update -----------------------------------------------------------------


def test_a_recheck_with_fewer_or_no_voices_leaves_stale_observations(gateway, fake_upstreams, monkeypatch, tmp_path):
    """On an update whose recheck finds fewer voices, or none, the old voices stay stored."""
    _move_media_dbs(monkeypatch, tmp_path)
    fake_full = _server(fake_upstreams, voices={"voices": [{"id": "af_bella"}, {"id": "am_adam"}],
                                                "default_voice": "af_bella"})
    owner, *_ = owner_and_admin(gateway)

    _save(owner, fake_full.base_url, "garage-voice", ["speech"])
    record = _record(gateway)
    assert [row["id"] for row in record["observations"]["voices"]] == ["af_bella", "am_adam"]
    assert record["observations"]["default_voice"] == "af_bella"

    # The server now answers with an empty voice list: the recheck finds nothing, and the old list stays.
    fake_empty = _server(fake_upstreams)
    updated = owner.patch("/admin/api/operator/services/garage-voice", headers=_write_headers(owner),
                          json={"definition": "openai-speech", "values": {"address": fake_empty.base_url},
                                "recheck": True})
    assert updated.status_code == 200, updated.text
    assert any(r.path == "/v1/audio/voices" for r in fake_empty.requests)  # the recheck did read the new server
    record = _record(gateway)
    assert [row["id"] for row in record["observations"]["voices"]] == ["af_bella", "am_adam"]  # stale
    assert record["observations"]["default_voice"] == "af_bella"

    # A shrunken list replaces the voices, but a default the new answer did not name stays behind.
    fake_one = _server(fake_upstreams, voices={"voices": [{"id": "cc_cara"}]})
    updated = owner.patch("/admin/api/operator/services/garage-voice", headers=_write_headers(owner),
                          json={"definition": "openai-speech", "values": {"address": fake_one.base_url},
                                "recheck": True})
    assert updated.status_code == 200, updated.text
    record = _record(gateway)
    assert [row["id"] for row in record["observations"]["voices"]] == ["cc_cara"]
    assert record["observations"]["default_voice"] == "af_bella"  # the stale default stays

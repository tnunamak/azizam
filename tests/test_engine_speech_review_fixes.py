"""Review fixes for the openai-speech definition: a saved speech record can be edited and rechecked, a real server's
bodies give speech, a recheck leaves no stale voices, a derived engine id never takes over an owner name.

The bodies are the ones a real speech server answered (no /v1/audio/models route, an empty voice list, one model whose task
is tts). Nothing here names a product in code: the definition is named for the API dialect.
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
from gateway.admin_api import AdminContext
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



def _patch(owner, service_id, body):
    return owner.patch(f"/admin/api/operator/services/{service_id}", headers=_write_headers(owner), json=body)


# ---- finding 1: a saved speech record can be viewed, edited and rechecked -----------------------------------------


def test_a_saved_speech_record_can_be_renamed_and_rechecked_like_the_dialog_does(gateway, fake_upstreams, monkeypatch, tmp_path):
    """The engine page sends the address back with a new name, or asks for a recheck: both must succeed."""
    _move_media_dbs(monkeypatch, tmp_path)
    fake = _server(fake_upstreams, voices={"voices": [{"id": "af_bella"}], "default_voice": "af_bella"})
    owner, *_ = owner_and_admin(gateway)
    _save(owner, fake.base_url, "garage-voice", ["speech"])

    renamed = _patch(owner, "garage-voice", {"definition": "openai-speech", "name": "kitchen-voice",
                                              "values": {"address": fake.base_url}})
    assert renamed.status_code == 200, renamed.text
    assert _record(gateway)["name"] == "kitchen-voice"

    again = _patch(owner, "garage-voice", {"definition": "openai-speech", "values": {"address": fake.base_url},
                                            "recheck": True})
    assert again.status_code == 200, again.text

    bare = _patch(owner, "garage-voice", {"definition": "openai-speech", "recheck": True})
    assert bare.status_code == 200, bare.text


# ---- finding 2: the real server's bodies give speech, ticked ------------------------------------------------------


def test_real_server_bodies_tick_speech_and_the_dialog_save_works(gateway, fake_upstreams, monkeypatch, tmp_path):
    """A server whose speech route answers an empty body with 400 has a speech route: the row is ticked."""
    _move_media_dbs(monkeypatch, tmp_path)
    fake = _server(fake_upstreams)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)

    check = _check(owner, fake.base_url)
    assert check["outcome"] == "ok", check["findings"]
    assert [row["id"] for row in check["models"]] == ["supertonic"]
    [speech_row] = check["capabilities"]
    assert speech_row["id"] == "speech" and speech_row["level"] == "likely" and speech_row["ticked"] is True

    # What the dialog sends: only the ticked rows, and the models the check listed.
    saved = owner.post("/admin/api/operator/services", headers=_write_headers(owner), json={
        "definition": "openai-speech", "name": "garage-voice", "values": {"address": fake.base_url},
        "capabilities": [row["id"] for row in check["capabilities"] if row["ticked"]],
        "models": check["models"]})
    assert saved.status_code == 201, saved.text

    spoken = _speak(owner, key, "supertonic", voice="default")
    assert spoken.status_code == 200, spoken.text
    assert spoken.content[:4] == b"RIFF"


def _check_with(speech_status):
    definition = _definition()

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        path = urlsplit(url).path
        if path == "/v1/models":
            return Answer(method=method, url=url, status=200, headers={"content-type": "application/json"},
                          body=json.dumps({"object": "list", "data": [{"id": "m1"}]}).encode())
        status = speech_status if (path == "/v1/audio/speech" and method == "POST") else 404
        return Answer(method=method, url=url, status=status, headers={"content-type": "application/json"},
                      body=b'{"error": {"message": "input is required"}}' if status != 404 else b'{"detail": "Not Found"}')

    return asyncio.run(engine_check.run_check(
        load_engine_catalog(), definition, {"address": "http://203.0.113.9:8880"}, key=KeyInput(), fetch=fetch))


@pytest.mark.parametrize("status", [400, 422])
def test_a_speech_route_that_answers_an_empty_body_with_a_client_error_is_ticked(status):
    [row] = _check_with(status)["capabilities"]
    assert row["level"] == "likely" and row["ticked"] is True


@pytest.mark.parametrize("status", [404, 405])
def test_a_missing_speech_route_is_not_ticked(status):
    [row] = _check_with(status)["capabilities"]
    assert row["ticked"] is False and row["level"] == "none"


# ---- finding 3: the record's observations equal the latest check ---------------------------------------------------


def test_a_recheck_that_finds_no_voices_clears_the_observed_voices(gateway, fake_upstreams, monkeypatch, tmp_path):
    _move_media_dbs(monkeypatch, tmp_path)
    full = _server(fake_upstreams, voices={"voices": [{"id": "af_bella"}, {"id": "am_adam"}], "default_voice": "af_bella"})
    owner, *_ = owner_and_admin(gateway)
    _save(owner, full.base_url, "garage-voice", ["speech"])
    assert [row["id"] for row in _record(gateway)["observations"]["voices"]] == ["af_bella", "am_adam"]

    empty = _server(fake_upstreams)
    rechecked = _patch(owner, "garage-voice", {"definition": "openai-speech", "values": {"address": empty.base_url},
                                                "recheck": True})
    assert rechecked.status_code == 200, rechecked.text

    observations = _record(gateway).get("observations") or {}
    assert not observations.get("voices") and not observations.get("default_voice"), observations


def test_a_recheck_that_finds_fewer_voices_drops_the_old_default(gateway, fake_upstreams, monkeypatch, tmp_path):
    _move_media_dbs(monkeypatch, tmp_path)
    full = _server(fake_upstreams, voices={"voices": [{"id": "af_bella"}, {"id": "am_adam"}], "default_voice": "af_bella"})
    owner, *_ = owner_and_admin(gateway)
    _save(owner, full.base_url, "garage-voice", ["speech"])

    fewer = _server(fake_upstreams, voices={"voices": [{"id": "cc_cara"}]})
    rechecked = _patch(owner, "garage-voice", {"definition": "openai-speech", "values": {"address": fewer.base_url},
                                                "recheck": True})
    assert rechecked.status_code == 200, rechecked.text

    observations = _record(gateway)["observations"]
    assert [row["id"] for row in observations["voices"]] == ["cc_cara"]
    assert not observations.get("default_voice"), observations


# ---- finding 4: an empty voice is left out ------------------------------------------------------------------------


@pytest.mark.parametrize("voice", [None, ""])
def test_a_request_with_no_voice_forwards_no_voice_key(gateway, fake_upstreams, monkeypatch, tmp_path, voice):
    _move_media_dbs(monkeypatch, tmp_path)
    fake = _server(fake_upstreams)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    _save(owner, fake.base_url, "garage-voice", ["speech"], models=REAL_MODELS["data"] and [{"id": "supertonic"}])

    spoken = _speak(owner, key, "supertonic", voice=voice, with_voice=voice is not None)
    assert spoken.status_code == 200, spoken.text
    [call] = _speech_calls(fake)
    assert "voice" not in call.json, call.json


def test_a_request_with_no_voice_uses_the_engines_default_voice(gateway, fake_upstreams, monkeypatch, tmp_path):
    _move_media_dbs(monkeypatch, tmp_path)
    fake = _server(fake_upstreams, voices={"voices": [{"id": "af_bella"}, {"id": "am_adam"}], "default_voice": "am_adam"})
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    _save(owner, fake.base_url, "garage-voice", ["speech"], models=[{"id": "supertonic"}])

    spoken = _speak(owner, key, "supertonic", with_voice=False)
    assert spoken.status_code == 200, spoken.text
    [call] = _speech_calls(fake)
    assert call.json["voice"] == "am_adam"


# ---- finding 5: a server that wants a key is said so plainly ------------------------------------------------------


def test_a_server_that_needs_a_key_is_told_plainly_that_this_kind_cannot_be_given_one():
    definition = _definition()

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        status = 401 if urlsplit(url).path == "/v1/models" else 404
        return Answer(method=method, url=url, status=status, headers={"content-type": "application/json"},
                      body=b'{"error": {"message": "Unauthorized"}}')

    result = asyncio.run(engine_check.run_check(
        load_engine_catalog(), definition, {"address": "http://203.0.113.9:8880"}, key=KeyInput(), fetch=fetch))
    assert result["outcome"] == "error"
    [finding] = result["findings"]
    text = " ".join(str(finding.get(part) or "") for part in ("title", "what", "why", "fix")).lower()
    assert "without a key" in text and "cannot be added" in text, finding
    assert "paste the key" not in text, finding
    assert {f["key"] for f in definition["fields"]} == {"address", "models"}


# ---- finding 6: the owner's names win over a derived engine id ----------------------------------------------------


def test_an_owner_pattern_keeps_working_after_a_record_reports_the_same_model():
    hand = {"speech": {"stable": {"tts-fast": ["omnivoice"]},
                       "openai": [{"match": "tts-1", "to": "tts-fast"}],
                       "engines": {"omnivoice": {"service": "s1", "model": "m1"}}}}
    derived = {"tts-1": {"service": "record-1", "model": "supertonic"}, "quack": {"service": "record-1", "model": "supertonic"}}

    merged = derived_engines.with_derived_speech_engines(hand, derived)

    kept = stable_aliases.resolve_alias(merged, "speech", "tts-1")
    assert kept is not None and kept.engine == "omnivoice" and kept.service == "s1"
    other = stable_aliases.resolve_alias(merged, "speech", "quack")
    assert other is not None and other.engine == "quack" and other.service == "record-1"  # a free name still derives


def test_an_owner_stable_name_is_not_taken_over_by_a_derived_engine():
    hand = {"speech": {"stable": {"tts-fast": ["omnivoice"]}, "engines": {"omnivoice": {"service": "s1", "model": "m1"}}}}
    merged = derived_engines.with_derived_speech_engines(hand, {"tts-fast": {"service": "record-1", "model": "x"}})
    kept = stable_aliases.resolve_alias(merged, "speech", "tts-fast")
    assert kept is not None and kept.engine == "omnivoice"
    assert "tts-fast" not in merged["speech"]["engines"]


# ---- the list probe schema is GET only ----------------------------------------------------------------------------


def test_a_list_probe_that_declares_another_method_than_get_is_refused():
    from tests.test_engine_definitions_speech import SEED, _refused, _seed_with

    def post_probe(definition, data):
        definition["probes"]["lists"][0]["method"] = "POST"

    assert _refused(_seed_with(post_probe))


# ---- guards the review proved: the list probes share the fetch's caps, and the serving path is the derived engine ----


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
    assert unknown.status_code == 404 and "not available" in unknown.text, unknown.text
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


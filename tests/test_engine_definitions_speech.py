"""The ``openai-speech`` definition: an owner adds an OpenAI-shaped speech server by its address and gets speech.

The server is Kokoro-FastAPI, Speaches or Orpheus style: ``POST /v1/audio/speech`` answers audio bytes. The owner types the
address only. The models and the voices come from lists the server reports (``probes.lists``), and the real test judges
audio bytes, not JSON (``real_tests.<id>.kind = binary_post``). Nothing here names a product in code: the definition is
named for the API dialect.

Voice list shapes below are the ones recorded in the research entry for speech engines (Kokoro-FastAPI default and legacy,
Orpheus-FastAPI, Speaches nested and flat, LocalAI nested). They were read in code by an earlier lane; none was captured
from a running server.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

import proxy
from gateway import admin_store, engine_check, media_jobs, receipt_store
from gateway.engine_check import Answer, KeyInput
from gateway.engine_definitions import (
    DEFINITIONS_PATH,
    SCHEMA_PATH,
    EngineDefinitionError,
    load_engine_catalog,
    parse_engine_catalog,
)
from tests.support.engine_dns import use_fake_engine_dns
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture
from tests.test_added_speech_engine_serves_speech import _app_key
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

TOKEN = "t0k3n"
VOICES = ["af_bella", "am_adam"]
SEED = json.loads(DEFINITIONS_PATH.read_text(encoding="utf-8"))
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


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


# ---- (a) the list shapes ------------------------------------------------------------------------------------------

# One body for each voice list shape the research recorded. Every one names the same two voices.
RECORDED_VOICE_BODIES = {
    "kokoro-objects": {"voices": [{"id": "af_bella", "name": "af_bella", "overall_grade": "A-"},
                                  {"id": "am_adam", "name": "am_adam"}], "default_voice": "af_bella"},
    "kokoro-legacy-strings": {"voices": ["af_bella", "am_adam"]},
    "orpheus-strings": {"status": "ok", "voices": ["af_bella", "am_adam"]},
    "speaches-nested": {"models": [{"id": "speaches-ai/Kokoro-82M-v1.0-ONNX",
                                    "voices": [{"id": "af_bella"}, {"id": "am_adam"}]}]},
    "speaches-flat": {"object": "list", "voices": ["af_bella", "am_adam"]},
    "localai-nested": {"data": [{"model": "kokoro", "voices": ["af_bella", "am_adam"]}]},
}


def _voice_ids(body):
    """What the definition's own voice probes read from one answer: the first probe that finds voices wins."""
    for probe in _definition()["probes"]["lists"]:
        if probe["offers"] != "voice":
            continue
        found = engine_check.read_list(body, probe["list"])
        if found["ids"]:
            return [row["id"] for row in found["ids"]]
    return []


@pytest.mark.parametrize("shape", sorted(RECORDED_VOICE_BODIES))
def test_voice_list_shapes_map_to_ids(shape):
    assert _voice_ids(RECORDED_VOICE_BODIES[shape]) == VOICES


def test_the_default_voice_is_read_when_the_server_names_one():
    probe = next(p for p in _definition()["probes"]["lists"] if p["offers"] == "voice" and "default_select" in p["list"])
    found = engine_check.read_list(RECORDED_VOICE_BODIES["kokoro-objects"], probe["list"])
    assert found["default"] == "af_bella"


def test_the_object_keys_and_tuple_first_shapes_read_ids():
    keys = engine_check.read_list({"voices": {"af_bella": {"lang": "en"}, "am_adam": {}}}, {"shape": "object_keys", "select": "/voices"})
    assert [row["id"] for row in keys["ids"]] == VOICES
    # A tuple's first element is the list of choices (a loader node's combo input); the rest is settings.
    first = engine_check.read_list({"input": {"required": {"voice": [VOICES, {"default": "af_bella"}]}}},
                                   {"shape": "tuple_first", "select": "/input/required/voice"})
    assert [row["id"] for row in first["ids"]] == VOICES


@pytest.mark.parametrize("body", [None, [], "text", {"voices": "nope"}, {"voices": [None, 3, {"x": 1}]}, {"other": []}])
def test_an_answer_that_is_not_a_list_gives_no_ids(body):
    assert _voice_ids(body) == []


def test_a_name_is_kept_when_the_server_gives_one_that_differs_from_the_id():
    found = engine_check.read_list({"voices": [{"id": "af_bella", "name": "Bella"}]},
                                   {"shape": "array_of_objects", "select": "/voices", "id_keys": ["id"], "name_keys": ["name"]})
    assert found["ids"] == [{"id": "af_bella", "name": "Bella"}]


# ---- (d) the semantic check ---------------------------------------------------------------------------------------

def _seed_with(mutate):
    data = copy.deepcopy(SEED)
    definition = next(d for d in data["definitions"] if d["definition"] == "openai-speech")
    mutate(definition, data)
    return data


def _refused(data):
    with pytest.raises(EngineDefinitionError) as raised:
        parse_engine_catalog(data, schema=SCHEMA, source="test.json")
    return str(raised.value)


def test_the_seed_definition_is_valid_and_named_for_the_dialect():
    definition = _definition()
    assert definition["definition"] == "openai-speech"
    assert definition["capabilities"] == ["speech"]
    assert definition["record"]["protocol"] == "inline-tts"


def test_a_definition_with_an_unknown_list_shape_is_refused():
    message = _refused(_seed_with(lambda d, _: d["probes"]["lists"][0]["list"].update(shape="xml_blob")))
    assert "xml_blob" in message and "shape" in message


@pytest.mark.parametrize("change,needle", [
    (lambda d, _: d["probes"]["lists"][0]["list"].pop("id_keys"), "id_keys"),
    (lambda d, _: d["probes"]["lists"][0]["list"].update(select="voices"), "JSON pointer"),
    (lambda d, _: d["probes"]["lists"][0].update(path="/v1/../admin"), "path"),
    (lambda d, _: d["probes"]["lists"][0].update(capability="images"), "capability"),
    (lambda d, _: d["probes"]["lists"].append(copy.deepcopy(d["probes"]["lists"][0])), "share an id"),
    (lambda d, data: data["real_tests"]["speech"].pop("expect"), "expect"),
    (lambda d, data: data["real_tests"]["speech"]["expect"].pop("min_bytes"), "min_bytes"),
    (lambda d, data: data["real_tests"]["chat"].update(expect={"status": 200}), "expect"),
])
def test_other_list_and_test_rule_breaks_are_refused(change, needle):
    assert needle in _refused(_seed_with(change))


# ---- (c) the binary real test -------------------------------------------------------------------------------------

def _real_test(answer_for_speech, *, model="tts-1", voice=None):
    catalog = load_engine_catalog()
    definition = catalog.definition("openai-speech")
    sent = []

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        sent.append((method, url, headers, json.loads(content) if content else None))
        return answer_for_speech(method, url)

    result = asyncio.run(engine_check.run_real_test(
        catalog, definition, {"address": "http://203.0.113.9:8880"}, "speech", key=KeyInput(), fetch=fetch,
        model=model, voice=voice))
    return result, sent


def _answer(status=200, content_type="audio/wav", body=b"R" * 900, **extra):
    return lambda method, url: Answer(method=method, url=url, status=status, headers={"content-type": content_type},
                                      body=body, **extra)


def test_the_binary_real_test_posts_a_short_request_and_passes_on_audio_bytes():
    result, sent = _real_test(_answer(), voice="af_bella")

    assert result["ok"] is True, result
    assert sent[0][0] == "POST" and sent[0][1] == "http://203.0.113.9:8880/v1/audio/speech"
    assert sent[0][3]["model"] == "tts-1" and sent[0][3]["voice"] == "af_bella" and sent[0][3]["input"]
    assert "audio/wav" in result["reply"] and "900" in result["reply"]


def test_the_binary_real_test_leaves_the_voice_out_when_none_is_chosen():
    _, sent = _real_test(_answer())
    assert "voice" not in sent[0][3]


def test_a_json_answer_fails_the_binary_real_test_with_a_clear_reason():
    result, _ = _real_test(_answer(content_type="application/json", body=b'{"audio": "AAAA"}' + b" " * 400))

    assert result["ok"] is False
    assert "application/json" in result["reply"] and "audio/" in result["reply"]
    assert result["finding"]["what"]


def test_too_few_bytes_fail_the_binary_real_test_with_a_clear_reason():
    result, _ = _real_test(_answer(body=b"RIFF"))

    assert result["ok"] is False
    assert "4 bytes" in result["reply"] and "256" in result["reply"]


def test_a_wrong_status_fails_the_binary_real_test():
    result, _ = _real_test(_answer(status=204, body=b""))

    assert result["ok"] is False
    assert "204" in result["reply"]


# ---- (b) add, check, save and speak, through the gateway ----------------------------------------------------------

def _speech_server(fakes, host=None):
    """An OpenAI-shaped speech server on a real port: Kokoro-style routes, no key, one audio answer."""
    not_found = {"status": 404, "json": {"detail": "Not Found"}}
    audio = {"status": 200, "headers": {"content-type": "audio/wav"}, "body": [{"hex": _wav_long().hex()}]}

    def ex(name, method, path, response):
        return {"id": name, "match": {"method": method, "path": path}, "response": response, "repeat": True, "required": False}

    scenario = scenario_from_data({
        "protocol": "engine-probe", "name": "speech-server",
        "provenance": {"source": "hand-written", "reference": "Kokoro-FastAPI route table: GET /v1/models, GET /v1/audio/voices, POST /v1/audio/speech"},
        "exchanges": [
            # The check reads a made-up path at the origin (the probes read from it) and under /v1 (the route probes).
            ex("baseline-get", "GET", "/zz-not-a-route-{{token}}", not_found),
            ex("baseline-post", "POST", "/zz-not-a-route-{{token}}", not_found),
            ex("route-baseline-post", "POST", "/v1/zz-not-a-route-{{token}}", not_found),
            ex("models", "GET", "/v1/models", {"status": 200, "json": {"object": "list", "data": [{"id": "tts-1"}, {"id": "kokoro"}]}}),
            ex("audio-models", "GET", "/v1/audio/models", not_found),
            ex("voices", "GET", "/v1/audio/voices", {"status": 200, "json": RECORDED_VOICE_BODIES["kokoro-objects"]}),
            ex("openapi", "GET", "/openapi.json", not_found),
            ex("capability-document", "GET", "/v1/models/capabilities", not_found),
            ex("speech", "POST", "/v1/audio/speech", audio),
        ],
    }, token=TOKEN)
    return fakes.loopback(scenario) if host is None else fakes.asgi(scenario, host=host)   # a named host: another computer


def _wav_long() -> bytes:
    import io
    import wave

    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(b"\x01\x00\x02\x00" * 200)
    return out.getvalue()


def _write_headers(client):
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def test_speech_engine_added_from_fake_upstream_serves_speech(gateway, fake_upstreams, monkeypatch, tmp_path):
    for module in (receipt_store, media_jobs):
        db = tmp_path / f"{module.__name__}.db"
        module.init_db(db)
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    fake = _speech_server(fake_upstreams)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    address = fake.base_url

    # Draft and check: the owner typed the address and nothing else.
    checked = owner.post("/admin/api/engines/check", headers=csrf_for(owner),
                         json={"definition": "openai-speech", "values": {"address": address}})
    assert checked.status_code == 200, checked.text
    check = checked.json()["check"]
    assert check["outcome"] == "ok", check["findings"]
    assert [row["id"] for row in check["models"]] == ["tts-1", "kokoro"]
    assert [row["id"] for row in check["voices"]] == VOICES and check["default_voice"] == "af_bella"
    assert [(row["id"], row["ticked"]) for row in check["capabilities"]] == [("speech", True)]

    # The real test judges audio bytes.
    tested = owner.post("/admin/api/engines/test", headers=csrf_for(owner),
                        json={"definition": "openai-speech", "values": {"address": address}, "capability": "speech",
                              "model": "tts-1", "voice": "af_bella"})
    assert tested.status_code == 200, tested.text
    assert tested.json()["test"]["ok"] is True, tested.text

    # Save: what the dialog sends back is what the check found.
    created = owner.post("/admin/api/operator/services", headers=_write_headers(owner), json={
        "definition": "openai-speech", "name": "garage-voice", "values": {"address": address},
        "capabilities": [row["id"] for row in check["capabilities"] if row["ticked"]],
        "models": check["models"]})
    assert created.status_code == 201, created.text

    record = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["garage-voice"]
    config = record["config"]
    assert not {"api_key_secret_id", "api_key_env", "api_key_ref"} & set(config), "the record holds a key"
    assert "Authorization" not in json.dumps(config) and "Bearer" not in json.dumps(record)
    assert config["engine_capabilities"] == ["speech"]
    assert set(record["capabilities"]) == {"audio", "speech"}
    assert [(row["model_group"], row["upstream_model"]) for row in config["models"]] == [("tts-1", "tts-1"), ("kokoro", "kokoro")]
    assert [row["id"] for row in record["observations"]["voices"]] == VOICES
    assert record["observations"]["default_voice"] == "af_bella"
    assert not any(request.headers.get("authorization") for request in fake.requests)

    # The dialog can read the saved engine back.
    shown = owner.get("/admin/api/engines/garage-voice")
    assert shown.status_code == 200, shown.text
    assert shown.json()["engine"]["definition"] == "openai-speech"

    # Speak through the gateway with one of the listed voices.
    spoken = owner.post("/v1/audio/speech", headers={"authorization": f"Bearer {key}"},
                        json={"model": "tts-1", "voice": "am_adam", "input": "hello", "response_format": "wav"})
    assert spoken.status_code == 200, spoken.text
    assert spoken.headers["content-type"].startswith("audio/")
    assert spoken.content[:4] == b"RIFF"
    speech_calls = [r for r in fake.requests if r.path == "/v1/audio/speech" and r.json and r.json.get("input") == "hello"]
    assert speech_calls and speech_calls[-1].json["voice"] == "am_adam" and speech_calls[-1].json["model"] == "tts-1"

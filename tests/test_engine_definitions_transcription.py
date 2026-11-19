"""The ``openai-transcription`` definition: an owner adds an OpenAI-shaped transcription server by its address.

The server is Speaches (formerly faster-whisper-server), or any server with ``POST /v1/audio/transcriptions``. The owner
types the address only. The models come from the server's own list, the route probe proves the route with an empty
request, and the real test uploads a tiny generated clip as a multipart form (``real_tests.<id>.kind = multipart_post``).
Nothing here names a product in code: the definition is named for the API dialect.

The model list below is the shape Speaches answers (``GET /v1/models``: ``{"data": [...], "object": "list"}``, each model
with a ``task``), read from its source at 993994f in the research entry for speech servers. It was not captured from a
running server.
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import wave
from email.parser import BytesParser
from email.policy import default as email_policy

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
SEED = json.loads(DEFINITIONS_PATH.read_text(encoding="utf-8"))
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

ASR_ID = "Systran/faster-whisper-small"
ASR_PUBLIC = "faster-whisper-small"
SPEACHES_MODELS = {"object": "list", "data": [
    {"id": ASR_ID, "object": "model", "task": "automatic-speech-recognition", "language": ["en"]},
    {"id": "speaches-ai/Kokoro-82M-v1.0-ONNX", "object": "model", "task": "text-to-speech", "voices": []},
    {"id": "snakers4/silero-vad", "object": "model", "task": "voice-activity-detection"},
]}


@pytest.fixture(autouse=True)
def fixed_baseline(monkeypatch):
    monkeypatch.setattr(engine_check, "_baseline_token", lambda: TOKEN)


@pytest.fixture(autouse=True)
def fake_engine_dns(monkeypatch):
    use_fake_engine_dns(monkeypatch)


def _definition():
    definition = load_engine_catalog().definition("openai-transcription")
    assert definition is not None, "no definition named openai-transcription"
    return definition


def _form(body: bytes, content_type: str):
    """The parts of one multipart body, as ``{name: (filename, content type, bytes)}``."""
    message = BytesParser(policy=email_policy).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
    parts = {}
    for part in message.iter_parts():
        parts[part.get_param("name", header="content-disposition")] = (
            part.get_filename(), part.get_content_type(), part.get_payload(decode=True))
    return parts


# ---- (a) the definition ------------------------------------------------------------------------------------------

def _seed_with(mutate):
    data = copy.deepcopy(SEED)
    definition = next(d for d in data["definitions"] if d["definition"] == "openai-transcription")
    mutate(definition, data)
    return data


def _refused(data):
    with pytest.raises(EngineDefinitionError) as raised:
        parse_engine_catalog(data, schema=SCHEMA, source="test.json")
    return str(raised.value)


def test_the_seed_definition_is_valid_and_named_for_the_dialect():
    definition = _definition()
    assert definition["definition"] == "openai-transcription"
    assert definition["capabilities"] == ["transcription"]
    assert definition["record"]["protocol"] == "chunked-stt"
    assert definition["probes"]["route_proves"] is True
    # No key field: a transcription server of this kind is used without a key, as a speech server is.
    assert [field["key"] for field in definition["fields"]] == ["address", "models"]


@pytest.mark.parametrize("change,needle", [
    (lambda d, data: data["real_tests"]["transcription"].pop("expect"), "expect"),
    (lambda d, data: data["real_tests"]["transcription"]["expect"].pop("json_string"), "json_string"),
    (lambda d, data: data["real_tests"]["transcription"].pop("file"), "file"),
    (lambda d, data: data["real_tests"]["transcription"]["file"].update(sample="a_song"), "sample"),
    (lambda d, data: data["real_tests"]["speech"]["expect"].update(json_string="text"), "json_string"),
    (lambda d, data: data["real_tests"]["chat"].update(file={"field": "file", "filename": "a.wav", "content_type": "audio/wav", "sample": "tone_wav"}), "file"),
    (lambda d, _: d["probes"]["lists"][0]["list"].update(where={"key": "task"}), "where"),
])
def test_breaks_in_the_transcription_test_or_list_are_refused(change, needle):
    assert needle in _refused(_seed_with(change))


# ---- (b) a list read can keep only the entries of one task -----------------------------------------------------------

ASR_READ = {"shape": "array_of_objects", "select": "/data", "id_keys": ["id"], "where": {"key": "task", "equals": "automatic-speech-recognition"}}


def test_a_list_read_with_a_where_keeps_only_the_entries_that_match():
    found = engine_check.read_list(SPEACHES_MODELS, ASR_READ)
    assert [row["id"] for row in found["ids"]] == [ASR_ID]


def test_a_list_read_with_a_where_drops_entries_that_have_no_such_field():
    found = engine_check.read_list({"data": [{"id": "a"}, {"id": "b", "task": 3}]}, ASR_READ)
    assert found["ids"] == []


def test_the_definitions_model_probes_read_the_task_first_then_the_whole_list():
    probes = [p for p in _definition()["probes"]["lists"] if p["offers"] == "model"]
    assert [p["id"] for p in probes][0] != [p["id"] for p in probes][-1]
    first = engine_check.read_list(SPEACHES_MODELS, probes[0]["list"])
    assert [row["id"] for row in first["ids"]] == [ASR_ID]
    # A server that names no task (vLLM, LocalAI style) is read whole by the next probe.
    plain = {"object": "list", "data": [{"id": "whisper-large-v3", "object": "model"}]}
    assert engine_check.read_list(plain, probes[0]["list"])["ids"] == []
    assert [row["id"] for row in engine_check.read_list(plain, probes[-1]["list"])["ids"]] == ["whisper-large-v3"]


# ---- (c) the multipart real test -----------------------------------------------------------------------------------

def _real_test(answer_for, *, model: str | None = ASR_ID):
    catalog = load_engine_catalog()
    definition = catalog.definition("openai-transcription")
    sent = []

    async def fetch(method, url, *, headers, cap_bytes, timeout_s, content=None):
        sent.append((method, url, headers, content))
        return answer_for(method, url)

    result = asyncio.run(engine_check.run_real_test(
        catalog, definition, {"address": "http://203.0.113.9:8000"}, "transcription", key=KeyInput(), fetch=fetch, model=model))
    return result, sent


def _answer(status=200, content_type="application/json", body=b'{"text": "hello"}', **extra):
    return lambda method, url: Answer(method=method, url=url, status=status, headers={"content-type": content_type}, body=body, **extra)


def test_the_multipart_real_test_uploads_a_generated_clip_with_the_model_and_json_format():
    result, sent = _real_test(_answer())

    assert result["ok"] is True, result
    method, url, headers, content = sent[0]
    assert method == "POST" and url == "http://203.0.113.9:8000/v1/audio/transcriptions"
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    parts = _form(content, headers["content-type"])
    assert parts["model"][2] == ASR_ID.encode() and parts["response_format"][2] == b"json"
    filename, part_type, clip = parts["file"]
    assert filename.endswith(".wav") and part_type == "audio/wav"
    with wave.open(io.BytesIO(clip)) as audio:
        assert audio.getnframes() > 0 and audio.getnchannels() == 1
    assert len(clip) < 64 * 1024, "the clip is tiny"
    assert result["request"]["method"] == "POST" and result["request"]["body"] == {"model": ASR_ID, "response_format": "json"}
    assert "hello" in result["reply"]


def test_the_multipart_real_test_leaves_the_model_out_when_none_is_chosen():
    _, sent = _real_test(_answer(), model=None)
    assert "model" not in _form(sent[0][3], sent[0][2]["content-type"])


def test_an_empty_transcript_passes_because_the_clip_is_not_speech():
    result, _ = _real_test(_answer(body=b'{"text": ""}'))
    assert result["ok"] is True, result
    assert result["reply"]


@pytest.mark.parametrize("answer,needle", [
    (_answer(body=b'{"words": []}'), "text"),
    (_answer(body=b'{"text": 3}'), "text"),
    (_answer(content_type="text/plain", body=b"hello there"), "text"),
    (_answer(status=204, body=b""), "204"),
    (_answer(status=422, body=b'{"detail": "model is required"}'), "model is required"),
])
def test_other_answers_fail_the_multipart_real_test_with_a_reason(answer, needle):
    result, _ = _real_test(answer)

    assert result["ok"] is False
    assert needle in result["reply"]
    assert result["finding"]["what"]


# ---- (d) add, check, save and transcribe, through the gateway ---------------------------------------------------------

def _transcription_server(fakes):
    """An OpenAI-shaped transcription server on a real port: Speaches-style routes, no key."""
    not_found = {"status": 404, "json": {"detail": "Not Found"}}

    def ex(name, method, path, response, **match):
        return {"id": name, "match": {"method": method, "path": path, **match}, "response": response, "repeat": True, "required": False}

    scenario = scenario_from_data({
        "protocol": "engine-probe", "name": "transcription-server",
        "provenance": {"source": "hand-written", "reference": "Speaches route table: GET /v1/models, POST /v1/audio/transcriptions (form fields model, file, response_format)"},
        "exchanges": [
            ex("baseline-get", "GET", "/zz-not-a-route-{{token}}", not_found),
            ex("baseline-post", "POST", "/zz-not-a-route-{{token}}", not_found),
            ex("route-baseline-post", "POST", "/v1/zz-not-a-route-{{token}}", not_found),
            ex("models", "GET", "/v1/models", {"status": 200, "json": SPEACHES_MODELS}),
            ex("audio-models", "GET", "/v1/audio/models", not_found),
            ex("openapi", "GET", "/openapi.json", not_found),
            ex("capability-document", "GET", "/v1/models/capabilities", not_found),
            # The route probe posts an empty JSON object: a form route answers 422, as FastAPI does for a missing field.
            ex("transcription-empty", "POST", "/v1/audio/transcriptions",
               {"status": 422, "json": {"detail": [{"type": "missing", "loc": ["body", "model"]}]}}, json={}),
            ex("transcription", "POST", "/v1/audio/transcriptions", {"status": 200, "json": {"text": "hello from the fake"}}),
        ],
    }, token=TOKEN)
    return fakes.loopback(scenario)


def _write_headers(client):
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def _clip() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0" * 32_000)
    return out.getvalue()


def test_transcription_engine_added_from_fake_upstream_transcribes_by_model_name(gateway, fake_upstreams, monkeypatch, tmp_path):
    for module in (receipt_store, media_jobs):
        db = tmp_path / f"{module.__name__}.db"
        module.init_db(db)
        monkeypatch.setattr(module, "DB_PATH", db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "images")
    fake = _transcription_server(fake_upstreams)
    owner, owner_id, *_ = owner_and_admin(gateway)
    key = _app_key(gateway, owner_id)
    address = fake.base_url

    # The Add engine flow offers the definition.
    offered = owner.get("/admin/api/engine-definitions")
    assert offered.status_code == 200, offered.text
    assert "openai-transcription" in json.dumps(offered.json())

    # Draft and check: the owner typed the address and nothing else.
    checked = owner.post("/admin/api/engines/check", headers=csrf_for(owner),
                         json={"definition": "openai-transcription", "values": {"address": address}})
    assert checked.status_code == 200, checked.text
    check = checked.json()["check"]
    assert check["outcome"] == "ok", check["findings"]
    assert [(row["id"], row["public_name"]) for row in check["models"]] == [(ASR_ID, ASR_PUBLIC)], "only the speech-to-text model is listed"
    assert [(row["id"], row["ticked"]) for row in check["capabilities"]] == [("transcription", True)]

    # The real test uploads a clip.
    tested = owner.post("/admin/api/engines/test", headers=csrf_for(owner),
                        json={"definition": "openai-transcription", "values": {"address": address}, "capability": "transcription",
                              "model": ASR_ID})
    assert tested.status_code == 200, tested.text
    assert tested.json()["test"]["ok"] is True, tested.text

    # Save: what the dialog sends back is what the check found.
    created = owner.post("/admin/api/operator/services", headers=_write_headers(owner), json={
        "definition": "openai-transcription", "name": "garage-ear", "values": {"address": address},
        "capabilities": [row["id"] for row in check["capabilities"] if row["ticked"]],
        "models": check["models"]})
    assert created.status_code == 201, created.text

    record = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]["garage-ear"]
    config = record["config"]
    assert not {"api_key_secret_id", "api_key_env", "api_key_ref"} & set(config), "the record holds a key"
    assert "Bearer" not in json.dumps(record)
    assert set(record["capabilities"]) == {"audio", "transcriptions"}
    assert [(row["model_group"], row["upstream_model"]) for row in config["models"]] == [(ASR_PUBLIC, ASR_ID)]
    assert not any(request.headers.get("authorization") for request in fake.requests)

    # Transcribe through the gateway, naming the model the owner sees.
    heard = owner.post("/v1/audio/transcriptions", headers={"authorization": f"Bearer {key}"},
                       data={"model": ASR_PUBLIC, "response_format": "json"}, files={"file": ("clip.wav", _clip(), "audio/wav")})
    assert heard.status_code == 200, heard.text
    assert heard.json()["text"] == "hello from the fake"
    uploads = [r for r in fake.requests if r.path == "/v1/audio/transcriptions" and r.method == "POST" and b'name="file"' in r.body]
    assert uploads, "the clip never reached the server"
    form = _form(uploads[-1].body, uploads[-1].headers["content-type"])
    assert form["model"][2] == ASR_ID.encode(), "the server must receive its own model id, not the public name"

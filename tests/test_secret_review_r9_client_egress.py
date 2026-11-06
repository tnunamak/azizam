"""Round 9 of the secret handling, errors to a caller: what the review of phase1-r8 (8e9f247c) found (decision D45).

What a non-owner can read is deny-default: API bodies, headers, status lines, streamed events and stored responses. The review
found, with a fake registered credential as the engine's text:

* HIGH 1: ``error.code``, ``error.type`` and ``error.param`` passed whenever they were a short lower-case token, so a
  credential of that shape reached the HTTP body and the SSE event; ``Retry-After`` passed any matching text.
* HIGH 2: an engine's ``X-Azizam-Echo`` header on an error reply was kept because its name starts with ``x-azizam-``.
* HIGH 3: the Responses translator copied an in-stream ``error.message`` into its SSE error event and into the stored
  final response.

``CANARY`` is the credential the engine echoes; ``FORMS`` is every spelling the tests look for in what the caller receives.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import pytest

import proxy
import responses_translator
from gateway import client_errors, stt_chunking
from tests.support.fake_upstream import FakeUpstreams, HandlerTransport, fake_upstreams, scenario_from_data  # noqa: F401 - fixture
from tests.support.gateway import ResponseTap, gateway_client, register_litellm, use_admin_db

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "sk-r9-canary-echoed-7741"          # lower case and short: the shape the r8 token rule let through
MESSY = "R9/CANARY+echo 7741=x"             # needs URL encoding; base64 of it has '/', '+' and padding
MODEL = "fk-model"
MESSAGES = [{"role": "user", "content": "hi"}]


def _b64_forms(text: str) -> set[str]:
    raw = text.encode()
    out = set()
    for encoder in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encoder(raw).decode()
        out.update({encoded, encoded.rstrip("=")})
    return out


FORMS = {TOKEN, MESSY, quote(MESSY), quote_plus(MESSY), json.dumps(MESSY)[1:-1], *_b64_forms(TOKEN), *_b64_forms(MESSY)}


def leaked(*texts: object) -> list[str]:
    """Every form of the credential found in the texts the caller received."""
    blob = "\n".join(str(text) for text in texts)
    return sorted(form for form in FORMS if form in blob)


def received(response: httpx.Response) -> list[str]:
    """What a client reads of a reply: the status line, the headers and the body."""
    return [response.reason_phrase, str(response.headers), response.text]


# ---------------------------------------------------------------------------
# The helper: deny-default fields
# ---------------------------------------------------------------------------

def _reply(error: dict, *, headers: dict | None = None, status: int = 400, **options) -> httpx.Response | object:
    return client_errors.upstream_error_response(status, json.dumps({"error": error}), headers or {}, **options)


@pytest.mark.parametrize("field", ["code", "type", "param"])
@pytest.mark.parametrize("value", [TOKEN, *sorted(_b64_forms(TOKEN)), "context_length_exceeded_" + TOKEN])
def test_a_credential_shaped_like_a_token_does_not_pass_as_code_type_or_param(field, value):
    """HIGH 1: the r8 rule passed any lower-case token; a registered credential of that shape went through."""
    reply = _reply({"message": "x", "type": "invalid_request_error", "param": "model", "code": "server_error", field: value},
                   request_fields={"model", "messages"})
    assert leaked(reply.body.decode(), reply.headers) == [] and value not in reply.body.decode()
    error = json.loads(reply.body)["error"]
    assert error[field] != value


def test_code_and_type_pass_only_from_the_fixed_lists_and_param_only_from_the_callers_request():
    ok = json.loads(_reply({"message": "w", "type": "invalid_request_error", "param": "temperature", "code": "context_length_exceeded"},
                           request_fields={"model", "temperature"}).body)["error"]
    assert ok == {"message": client_errors.CODE_REASONS["context_length_exceeded"], "type": "invalid_request_error",
                  "param": "temperature", "code": "context_length_exceeded"}
    for request_fields in (None, set(), {"model"}):
        refused = json.loads(_reply({"param": "temperature"}, request_fields=request_fields).body)["error"]
        assert refused["param"] is None, "a param that is not a field of the caller's request is dropped"
    unknown = json.loads(_reply({"message": "w", "type": "made_up_type", "code": "made_up_code"}).body)["error"]
    assert unknown["code"] is None and unknown["type"] == "invalid_request_error"
    assert unknown["message"] == client_errors.status_reason(400)


def test_the_message_is_ours_and_a_known_limit_is_written_in_it():
    plain = json.loads(_reply({"message": "R9-ENGINE-WORDS", "code": "context_length_exceeded"}).body)["error"]["message"]
    assert plain == client_errors.CODE_REASONS["context_length_exceeded"]
    limited = json.loads(_reply({"message": "R9-ENGINE-WORDS", "code": "context_length_exceeded"}, limits={"context_window": 8192}).body)
    assert "8192" in limited["error"]["message"] and "R9-ENGINE-WORDS" not in limited["error"]["message"]
    for window in (0, -1, True, "8192", None):
        text = json.loads(_reply({"code": "context_length_exceeded"}, limits={"context_window": window}).body)["error"]["message"]
        assert text == client_errors.CODE_REASONS["context_length_exceeded"], window


@pytest.mark.parametrize("value, expected", [
    ("7", "7"), (" 12 ", "12"), ("1.2", "2"), ("0", "0"), ("999999", "86400"),
    ("Bearer " + TOKEN, None), (TOKEN, None), ("-5", None), ("7 seconds", None), ("1e3", None), ("", None), ("a" * 70, None),
])
def test_retry_after_is_a_number_we_write_or_it_is_dropped(value, expected):
    """HIGH 1: any text of letters, digits, spaces, commas, colons and dashes was re-emitted as the engine wrote it."""
    reply = client_errors.upstream_error_response(429, b"{}", {"Retry-After": value})
    assert reply.headers.get("retry-after") == expected
    assert leaked(reply.headers) == []


def test_retry_after_reads_an_http_date_and_writes_seconds():
    future = "Wed, 21 Oct 2099 07:28:00 GMT"
    past = "Wed, 21 Oct 2015 07:28:00 GMT"
    assert client_errors.retry_after(future) == "86400"
    assert client_errors.retry_after(past) == "0"
    assert client_errors.retry_after("Wed, 21 Oct 2099 07:28:00") is None, "a date with no zone is not trusted"
    assert client_errors.retry_after("Wed, 99 Foo 2099 07:28:00 GMT") is None


def test_every_code_and_type_the_gateway_names_in_its_own_chat_errors_is_on_the_list():
    """A frontend reads the gateway's own chat answer; a code the list lacks would vanish there."""
    pattern = re.compile(r'\bcode\s*=\s*"([a-z][a-z0-9_.\-]*)"|"code":\s*"([a-z][a-z0-9_.\-]*)"')
    missing: dict[str, str] = {}
    for path in [ROOT / "proxy.py", ROOT / "responses_translator.py", *sorted((ROOT / "gateway" / "protocols").glob("*.py"))]:
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            code = match.group(1) or match.group(2)
            if code not in client_errors.ERROR_CODES:
                missing.setdefault(code, path.name)
    assert not missing, f"codes the gateway puts in an error that client_errors would drop: {missing}"
    assert all(client_errors.CODE_REASONS[code].strip() for code in client_errors.CODE_REASONS)
    assert "invalid_request_error" in client_errors.ERROR_TYPES


# ---------------------------------------------------------------------------
# Streams: the rewrite, every shape of an error event
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("event", [
    {"error": {"message": "w {C}", "type": "api_error", "code": "{C}", "param": "{C}"}},
    {"error": "w {C}"},
    {"error": ["{C}"]},
    {"type": "error", "message": "w {C}", "code": "{C}", "param": "{C}"},
    {"object": "error", "message": "w {C}", "type": "{C}", "param": "{C}", "code": "{C}"},
])
def test_an_error_event_of_any_shape_carries_none_of_the_engines_words(event):
    text = json.dumps(event).replace("{C}", TOKEN)
    data = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n' + b"data: " + text.encode() + b"\n\n" + b"data: [DONE]\n\n")
    out = client_errors.rewrite_stream_errors(data, request_fields={"model"})
    assert leaked(out.decode()) == []
    assert out.startswith(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n') and out.endswith(b"data: [DONE]\n\n")
    [error] = [json.loads(line[6:])["error"] for line in out.decode().splitlines() if line.startswith("data: {") and '"error"' in line]
    assert set(error) == {"message", "type", "param", "code"}


def test_an_event_that_only_names_a_null_error_is_not_rewritten():
    chunk = b'data: {"choices":[],"error":null}\n\n'
    assert client_errors.rewrite_stream_errors(chunk) == chunk


# ---------------------------------------------------------------------------
# The Responses translator (HIGH 3)
# ---------------------------------------------------------------------------

def _translator(request: dict | None = None) -> responses_translator.StreamTranslator:
    return responses_translator.StreamTranslator(response_id="resp_r9", created_at=1, original_request=request or {"model": MODEL, "input": "hi"})


def _events(frames: list[bytes]) -> list[dict]:
    out = []
    for frame in frames:
        for line in frame.decode().splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


@pytest.mark.parametrize("error", [
    {"message": "bad key " + TOKEN, "type": "api_error", "code": "server_error"},
    {"message": "ok", "code": TOKEN, "param": TOKEN},
    {"message": "ok", "code": "server_error", "param": "model" + TOKEN},
    {"message": "ok", "code": "server_error", "param": "internal_secret_field"},
    TOKEN,
    [TOKEN],
])
def test_an_in_stream_error_reaches_neither_the_sse_event_nor_the_stored_response(error):
    """HIGH 3: both carried the upstream's message, code and param."""
    translator = _translator()
    frames = translator.start()
    frames += translator.feed(b'data: {"choices":[{"delta":{"content":"par"},"finish_reason":null}]}\n\n')
    frames += translator.feed(("data: " + json.dumps({"error": error}) + "\n\n").encode())
    frames += translator.finish()
    frames += translator.terminal_event()
    stored = json.dumps(translator.build_final_response())
    assert leaked(b"".join(frames).decode(), stored) == []
    assert "internal_secret_field" not in stored and "internal_secret_field" not in b"".join(frames).decode()
    failed = [event for event in _events(frames) if event.get("type") == "error"]
    assert failed and failed[0]["code"] in client_errors.ERROR_CODES
    assert failed[0]["param"] in (None, "model")


def test_an_in_stream_error_keeps_a_param_that_names_a_field_of_the_request():
    translator = _translator({"model": MODEL, "input": "hi", "temperature": 3})
    translator.start()
    frames = translator.feed(b'data: {"error":{"message":"w","code":"invalid_value","param":"temperature"}}\n\n')
    [event] = [e for e in _events(frames) if e.get("type") == "error"]
    assert event["param"] == "temperature" and event["code"] == "invalid_value"
    assert event["message"] == client_errors.CODE_REASONS["invalid_value"]


def test_a_failure_raised_by_the_translation_names_no_upstream_text():
    translator = _translator({"model": MODEL, "input": "hi", "tools": [{"type": "custom", "name": "apply_patch"}]})
    translator.start()
    arguments = json.dumps({"input": TOKEN, "other": TOKEN})
    chunk = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "apply_patch", "arguments": arguments}}]},
                          "finish_reason": "tool_calls"}]}
    frames = translator.feed(("data: " + json.dumps(chunk) + "\n\n").encode()) + translator.finish()
    assert leaked(b"".join(frames).decode()) == []


# ---------------------------------------------------------------------------
# Through the gateway: a fake engine that echoes the credential in every field it can
# ---------------------------------------------------------------------------

FIELDS = {
    "message": [f"bad key {MESSY} {TOKEN} " + " ".join(sorted(_b64_forms(MESSY) | _b64_forms(TOKEN)))],
    "code": [TOKEN, sorted(_b64_forms(TOKEN))[0].lower(), quote(MESSY)],
    "type": [TOKEN, sorted(_b64_forms(TOKEN))[0].lower(), quote(MESSY)],
    "param": [TOKEN, sorted(_b64_forms(TOKEN))[0].lower(), quote(MESSY)],
    "x-azizam-echo": [MESSY, TOKEN],
    "x-echo": [MESSY, TOKEN],
    "retry-after": ["Bearer " + TOKEN, MESSY, TOKEN],
    "www-authenticate": ['Bearer realm="' + TOKEN + '"'],
    "set-cookie": ["session=" + TOKEN],
    "location": ["https://x.invalid/?k=" + quote(MESSY)],
}
CASES = [(name, value) for name, values in FIELDS.items() for value in values]

# endpoint -> (gateway path, request body, the path the engine is asked)
ENDPOINTS = {
    "chat": ("/v1/chat/completions", lambda s: {"model": MODEL, "messages": MESSAGES, "stream": s}, "/v1/chat/completions"),
    "completions": ("/v1/completions", lambda s: {"model": MODEL, "prompt": "hi", "stream": s}, "/v1/completions"),
    "embeddings": ("/v1/embeddings", lambda s: {"model": MODEL, "input": "hi"}, "/v1/embeddings"),
    "responses": ("/v1/responses", lambda s: {"model": MODEL, "input": "hi", "stream": s, "store": False}, "/v1/chat/completions"),
    "messages": ("/v1/messages", lambda s: {"model": MODEL, "max_tokens": 10, "messages": MESSAGES, "stream": s}, "/v1/chat/completions"),
    "ollama": ("/api/chat", lambda s: {"model": MODEL, "messages": MESSAGES, "stream": s}, "/v1/chat/completions"),
}
VARIANTS = [("chat", False), ("chat", True), ("completions", False), ("embeddings", False), ("responses", False),
            ("responses", True), ("messages", False), ("messages", True), ("ollama", False), ("ollama", True)]


def _engine_error(field: str, value: str) -> tuple[dict, dict]:
    """The engine's headers and error object, with ``value`` in ``field`` and nothing else suspicious."""
    error = {"message": "ordinary", "type": "invalid_request_error", "param": None, "code": "server_error"}
    headers: dict[str, str] = {}
    if field in error:
        error[field] = value
    else:
        headers[field] = value
    return headers, error


@pytest.fixture
def engine(fake_upstreams: FakeUpstreams, monkeypatch, tmp_path):
    hop = ResponseTap(proxy.app)
    fake_upstreams.router.add(f"127.0.0.1:{proxy.PROXY_PORT}", hop)

    def serve(engine_path: str, response: dict) -> None:
        data = {"protocol": "openai-compatible", "name": "r9-echo", "provenance": {"source": "hand-written", "reference": "round 9"},
                "exchanges": [{"id": "echo", "match": {"method": "POST", "path": engine_path}, "response": response,
                               "required": False, "repeat": True}]}
        fake = fake_upstreams.asgi(scenario_from_data(data), host="litellm.fk.test")
        register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)

    return serve


def _post(path: str, body: dict) -> httpx.Response:
    async def call() -> httpx.Response:
        async with asyncio.timeout(30), gateway_client() as client:
            return await client.post(path, json=body)

    return asyncio.run(call())


@pytest.mark.parametrize("field, value", CASES, ids=[f"{field}:{index}" for index, (field, value) in enumerate(CASES)])
@pytest.mark.parametrize("name, stream", VARIANTS, ids=[f"{name}-{'stream' if stream else 'text'}" for name, stream in VARIANTS])
def test_an_error_reply_from_the_engine_never_carries_the_credential_to_the_client(engine, name, stream, field, value):
    """The engine answers 4xx with the credential in one field, in each spelling. The client reads none of it."""
    path, body, engine_path = ENDPOINTS[name]
    headers, error = _engine_error(field, value)
    engine(engine_path, {"status": 429, "headers": headers, "json": {"error": error}})
    response = _post(path, body(stream))
    assert leaked(*received(response)) == [], (name, stream, field, value)
    assert response.status_code in (400, 404, 429, 500, 502, 503), response.status_code
    if field == "retry-after" and "retry-after" in response.headers:
        assert response.headers["retry-after"].isdigit()


@pytest.mark.parametrize("field", ["message", "code", "type", "param"])
@pytest.mark.parametrize("name", ["chat", "responses", "messages", "ollama"])
def test_an_error_event_inside_a_stream_never_carries_the_credential_to_the_client(engine, name, field):
    """A 200 stream whose engine reports a failure as an event: chat rewrites it, the frontends read the rewritten stream."""
    path, body, engine_path = ENDPOINTS[name]
    _, error = _engine_error(field, FIELDS[field][0] if field == "message" else TOKEN)
    chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": MODEL,
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"}, "finish_reason": None}]}
    engine(engine_path, {"status": 200, "headers": {"x-echo": MESSY},
                         "body": [{"event": chunk}, {"event": {"error": error}}, {"done": True}]})
    response = _post(path, body(True))
    assert leaked(*received(response)) == []


@pytest.mark.parametrize("name", ["chat", "completions", "embeddings"])
def test_a_200_answer_that_holds_an_error_object_loses_the_engines_words(engine, name):
    path, body, engine_path = ENDPOINTS[name]
    engine(engine_path, {"status": 200, "json": {"error": {"message": "bad key " + MESSY, "type": TOKEN, "code": TOKEN, "param": TOKEN}}})
    response = _post(path, body(False))
    assert leaked(*received(response)) == []


def test_the_engines_x_azizam_headers_do_not_reach_the_client_on_an_error_or_a_success(engine):
    """HIGH 2: the error path kept every ``X-Azizam-*`` header of the engine; the success path now drops them too."""
    engine("/v1/chat/completions", {"status": 400, "headers": {"X-Azizam-Echo": MESSY, "X-Azizam-Backend": "attacker", "x-azizam-request-id": "evil"},
                                    "json": {"error": {"message": "w", "type": "invalid_request_error", "code": "invalid_value"}}})
    refused = _post("/v1/chat/completions", ENDPOINTS["chat"][1](False))
    assert "x-azizam-echo" not in refused.headers and leaked(*received(refused)) == []
    assert refused.headers.get("x-azizam-backend") != "attacker" and refused.headers.get("x-azizam-request-id") != "evil"
    assert refused.headers["x-azizam-request-id"], "the gateway's own header is still there"
    assert refused.json()["error"]["code"] == "invalid_value"


def test_the_engines_x_azizam_headers_do_not_reach_the_client_on_a_success(engine):
    ok = {"id": "c", "object": "chat.completion", "created": 1, "model": MODEL,
          "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}]}
    engine("/v1/chat/completions", {"status": 200, "headers": {"X-Azizam-Echo": MESSY, "X-Azizam-Backend": "attacker"}, "json": ok})
    response = _post("/v1/chat/completions", ENDPOINTS["chat"][1](False))
    assert response.status_code == 200 and "x-azizam-echo" not in response.headers
    assert response.headers.get("x-azizam-backend") != "attacker"


def test_a_context_length_error_names_the_window_the_gateway_knows(engine, monkeypatch):
    async def window(model_id, entry=None, **_):
        return 4096

    monkeypatch.setattr(proxy, "_context_window_for_model_id", window)
    engine("/v1/chat/completions", {"status": 400, "json": {"error": {"message": "R9-ENGINE-WORDS 99999 tokens", "type": "invalid_request_error",
                                                                      "param": "messages", "code": "context_length_exceeded"}}})
    response = _post("/v1/chat/completions", ENDPOINTS["chat"][1](False))
    error = response.json()["error"]
    assert response.status_code == 400 and error["code"] == "context_length_exceeded" and error["param"] == "messages"
    assert "4096" in error["message"] and "R9-ENGINE-WORDS" not in response.text and "99999" not in response.text


def test_a_param_the_caller_did_not_send_is_dropped(engine):
    engine("/v1/chat/completions", {"status": 400, "json": {"error": {"message": "w", "type": "invalid_request_error",
                                                                      "param": "tools", "code": "invalid_value"}}})
    assert _post("/v1/chat/completions", ENDPOINTS["chat"][1](False)).json()["error"]["param"] is None


def test_the_status_line_reason_of_the_engine_is_not_passed_on():
    """An engine chooses its reason phrase; the reply to the caller has the standard one (or none)."""
    resp = httpx.Response(401, json={"error": {"message": "w"}}, extensions={"reason_phrase": f"bad key {TOKEN}".encode()})
    assert resp.reason_phrase == f"bad key {TOKEN}"
    reply = asyncio.run(proxy._engine_error_response(resp, resp.content, {"X-Azizam-Request-Id": "req_1"}, "req_1"))
    assert reply.status_code == 401
    assert leaked(reply.body.decode(), reply.headers) == []
    assert reply.headers["x-azizam-request-id"] == "req_1"


# ---------------------------------------------------------------------------
# Speech, transcription, images
# ---------------------------------------------------------------------------

def _speech_fake(fakes: FakeUpstreams, status: int, headers: dict, error: dict):
    data = {"protocol": "openai-compatible", "name": "r9-speech", "provenance": {"source": "hand-written", "reference": "round 9"},
            "exchanges": [{"id": "speech", "match": {"method": "POST", "path": "/v1/audio/speech"},
                           "response": {"status": status, "headers": headers, "json": {"error": error}}}]}
    return fakes.asgi(scenario_from_data(data))


@pytest.mark.parametrize("field, value", CASES, ids=[f"{field}:{index}" for index, (field, value) in enumerate(CASES)])
def test_a_speech_engines_error_never_carries_the_credential_to_the_client(fake_upstreams, field, value):
    headers, error = _engine_error(field, value)
    fake = _speech_fake(fake_upstreams, 400, headers, error)

    async def open_stream():
        async with asyncio.timeout(10):
            return await proxy._stream_tts_upstream(url=f"{fake.base_url}/v1/audio/speech", headers={}, body=b'{"input":"hello","voice":"v"}',
                                                    stream_format="audio", response_format="pcm", native_rate=24000)

    response = asyncio.run(open_stream())
    assert response.status_code == 400
    assert leaked(response.body.decode(), response.headers) == []
    if "retry-after" in response.headers:
        assert response.headers["retry-after"].isdigit()


@pytest.mark.parametrize("field, value", CASES, ids=[f"{field}:{index}" for index, (field, value) in enumerate(CASES)])
def test_a_transcription_engines_error_never_carries_the_credential_to_the_client(field, value):
    headers, error = _engine_error(field, value)
    reply = stt_chunking.UpstreamResponse(400, json.dumps({"error": error}).encode(), {"content-type": "application/json", **headers})
    response = proxy._stt_response_from_upstream(reply, {"model", "file"})
    assert response.status_code == 400
    assert leaked(response.body.decode(), response.headers) == []


@pytest.mark.parametrize("field, value", CASES, ids=[f"{field}:{index}" for index, (field, value) in enumerate(CASES)])
def test_an_image_engines_refusal_never_carries_the_credential_to_the_client(field, value):
    """The image upload path (the r7 repro): the engine answers 401 with the Authorization header it received and more."""
    headers, error = _engine_error(field, value)

    def handler(request: httpx.Request) -> httpx.Response:
        text = json.dumps({"error": error}) if field in error else request.headers.get("authorization", "")
        return httpx.Response(401, text=text, headers=headers, extensions={"reason_phrase": TOKEN.encode()})

    async def call():
        async with httpx.AsyncClient(transport=HandlerTransport(handler)) as client:
            return await proxy._upload_graph_image_bytes(client, {"Authorization": f"Bearer {TOKEN}"}, b"\x89PNG", filename="a.png")

    name, response = asyncio.run(call())
    assert name is None
    assert leaked(response.body.decode(), response.headers) == []

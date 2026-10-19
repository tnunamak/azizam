"""Round 8 of the secret handling, errors to a caller: what the review of phase1-r7 (6d7f21d4) found (decision D44).

* HIGH 4: an error response to an API caller relayed what an upstream said. A fake image upload that answered 401 with the
  ``Authorization`` header it received as its text made ``_upload_graph_image_bytes`` return a 502 whose body held that
  credential; the same shape was in the image edit and video paths, the rerank, speech-to-text and speech routes, the
  sidecar and engine chat relays, an in-stream error event and the native lane.

An error to a caller is built from the gateway's own words (``gateway/client_errors.py``): a fixed reason for the status or
the engine's machine code, the code, the request id. ``WORDS`` is what the engine wrote; ``CANARY`` the credential it echoed.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import proxy
from gateway import log_scrub, stt_chunking
from tests.support.fake_upstream import FakeUpstreams, HandlerTransport, fake_upstreams, scenario_from_data  # noqa: F401 - fixture
from tests.support.gateway import gateway_client, register_litellm, use_admin_db

CANARY = "sk-r8-CANARY-echoed-4471"
WORDS = "R8-ENGINE-WORDS-5530"
MODEL = "fk-model"
MESSAGES = [{"role": "user", "content": "hi"}]
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client_errors():
    """The helper module, imported when a test needs it (the routes below are tested without it)."""
    from gateway import client_errors as module

    return module


# ---------------------------------------------------------------------------
# The reviewer's exact repro
# ---------------------------------------------------------------------------

def test_the_reviewers_repro_an_upload_refusal_that_echoes_the_authorization_header():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(401, text=request.headers.get("authorization", ""))

    async def call():
        async with httpx.AsyncClient(transport=HandlerTransport(handler)) as client:
            return await proxy._upload_graph_image_bytes(
                client, {"Authorization": f"Bearer {CANARY}"}, b"\x89PNG", filename="a.png")

    name, response = asyncio.run(call())
    assert name is None and sent and sent[0].headers["authorization"] == f"Bearer {CANARY}", "the credential was sent"
    assert response.status_code == 502
    assert CANARY not in response.body.decode() and "Bearer" not in response.body.decode()
    error = json.loads(response.body)["error"]
    assert error["type"] == "api_error" and error["message"].startswith("Image upload failed")


@pytest.mark.parametrize("which", ["fixed", "relayed"])
def test_a_client_error_is_openai_shaped_and_holds_only_our_words(client_errors, which):
    response = (client_errors.client_error(502, "upload_failed", error_type="api_error") if which == "fixed" else
                client_errors.upstream_error_response(401, json.dumps({"error": {"message": f"bad key {CANARY}"}}).encode(), {}))
    body = json.loads(response.body)
    assert set(body) == {"error"} and set(body["error"]) == {"message", "type", "param", "code"}
    assert CANARY not in response.body.decode()
    assert response.media_type == "application/json"


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

def test_an_engine_error_keeps_its_status_machine_code_and_retry_after_and_loses_its_words_and_headers(client_errors):
    reply = client_errors.upstream_error_response(
        429,
        json.dumps({"error": {"message": f"slow down {WORDS}", "type": "requests", "param": "model", "code": "rate_limit_exceeded"}}),
        {"retry-after": "7", "www-authenticate": f"Bearer realm={WORDS}", "x-echo": WORDS, "set-cookie": WORDS},
        request_id="req_1", own_headers={"X-Azizam-Backend": "d39"}, request_fields={"model", "messages"})
    assert reply.status_code == 429
    assert json.loads(reply.body) == {"error": {"message": client_errors.CODE_REASONS["rate_limit_exceeded"], "type": "requests",
                                                 "param": "model", "code": "rate_limit_exceeded"}}   # param: a field the caller sent
    assert reply.headers["retry-after"] == "7" and reply.headers["x-request-id"] == "req_1" and reply.headers["x-azizam-backend"] == "d39"
    assert WORDS not in str(reply.headers) and "www-authenticate" not in reply.headers and "set-cookie" not in reply.headers


@pytest.mark.parametrize("body", [b"", b"<html>" + WORDS.encode(), b"[1]", json.dumps({"error": WORDS}).encode(),
                                  json.dumps({"error": {"message": WORDS, "code": f"sk-{WORDS} with spaces", "type": ["x"]}}).encode()])
def test_whatever_the_engine_sent_the_message_is_ours(client_errors, body):
    reply = client_errors.upstream_error_response(502, body, {"retry-after": WORDS + " bad value!"})
    out = json.loads(reply.body)["error"]
    assert out["message"] == client_errors.status_reason(502)
    assert WORDS not in reply.body.decode() and "retry-after" not in reply.headers
    assert out["code"] is None and out["type"] == "api_error"


def test_a_reason_is_a_key_to_our_table_and_never_free_text(client_errors):
    with pytest.raises(KeyError):
        client_errors.client_error(502, f"upload failed: {WORDS}")
    assert all("{" not in text and "}" not in text for text in client_errors.REASONS.values())


def test_a_stream_error_event_is_rewritten_and_other_events_are_not(client_errors):
    chunk = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
             + f'data: {{"error":{{"message":"bad key {CANARY}","type":"api_error","code":"server_error"}}}}\n\n'.encode()
             + b"data: [DONE]\n\n")
    out = client_errors.rewrite_stream_errors(chunk)
    assert CANARY not in out.decode() and b"server_error" in out
    assert out.startswith(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n') and out.endswith(b"data: [DONE]\n\n")
    assert client_errors.rewrite_stream_errors(b'data: {"choices":[]}\n\n') == b'data: {"choices":[]}\n\n'


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_a_transcription_error_reply_is_not_passed_on_verbatim():
    reply = stt_chunking.UpstreamResponse(
        401, json.dumps({"error": {"message": f"bad key {CANARY}", "type": "invalid_request_error"}}).encode(),
        {"content-type": "application/json", "retry-after": "9", "x-echo": CANARY})
    response = proxy._stt_response_from_upstream(reply)
    assert response.status_code == 401 and response.headers["retry-after"] == "9"
    assert CANARY not in response.body.decode() and CANARY not in str(response.headers)
    assert json.loads(response.body)["error"]["type"] == "invalid_request_error"


def test_a_successful_transcription_is_still_passed_on_as_it_is():
    body = json.dumps({"text": "hello"}).encode()
    response = proxy._stt_response_from_upstream(stt_chunking.UpstreamResponse(200, body, {"content-type": "application/json"}))
    assert response.status_code == 200 and response.body == body


def _serve(fakes: FakeUpstreams, monkeypatch, tmp_path, exchange: dict) -> Any:
    data = {"protocol": "openai-compatible", "name": "r8-error", "provenance": {"source": "hand-written", "reference": "round 8"},
            "exchanges": [{"id": "chat", "match": {"method": "POST", "path": "/v1/chat/completions"}, "response": exchange}]}
    fake = fakes.asgi(scenario_from_data(data), host="litellm.fk.test")
    register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)
    return fake


def _chat(stream: bool = False) -> httpx.Response:
    async def call() -> httpx.Response:
        async with asyncio.timeout(20), gateway_client() as client:
            return await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "stream": stream})

    return asyncio.run(call())


@pytest.mark.parametrize("stream", [False, True], ids=["text", "stream"])
def test_a_chat_error_from_the_router_loses_its_words_and_headers(fake_upstreams, monkeypatch, tmp_path, stream):
    _serve(fake_upstreams, monkeypatch, tmp_path, {
        "status": 401,
        "headers": {"retry-after": "7", "www-authenticate": f"Bearer realm={WORDS}", "x-echo": WORDS},
        "json": {"error": {"message": f"Incorrect API key provided: {WORDS}", "type": "invalid_request_error",
                           "param": None, "code": "invalid_api_key"}}})
    response = _chat(stream)
    assert response.status_code == 401
    assert response.headers["retry-after"] == "7"
    assert WORDS not in response.text and WORDS not in str(response.headers)
    error = response.json()["error"]
    assert set(error) == {"message", "type", "param", "code"} and error["code"] == "invalid_api_key"
    assert response.headers["x-azizam-request-id"]


def test_an_error_event_inside_a_chat_stream_loses_the_engines_words(fake_upstreams, monkeypatch, tmp_path):
    _serve(fake_upstreams, monkeypatch, tmp_path, {
        "status": 200,
        "body": [{"event": {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": MODEL,
                            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hi"}, "finish_reason": None}]}},
                 {"event": {"error": {"message": f"upstream blew up on {WORDS}", "type": "api_error", "code": "server_error"}}},
                 {"done": True}]})
    response = _chat(True)
    assert response.status_code == 200
    assert WORDS not in response.text
    assert '"Hi"' in response.text and "server_error" in response.text and response.text.rstrip().endswith("[DONE]")


# ---------------------------------------------------------------------------
# The structure: no call passes a free-text reason
# ---------------------------------------------------------------------------

def test_every_client_error_call_names_its_reason_by_a_literal_key(client_errors):
    calls = []
    for path in [*sorted((ROOT / "gateway").rglob("*.py")), ROOT / "proxy.py"]:
        if path.name == "client_errors.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "client_error" \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "client_errors":
                calls.append((path.name, node.lineno, node.args[1] if len(node.args) > 1 else None))
    assert len(calls) >= 15, "the sweep of verbatim relays routes its sites through the helper"
    bad = [(name, line) for name, line, reason in calls
           if not (isinstance(reason, ast.Constant) and reason.value in client_errors.REASONS)]
    assert not bad, f"client_error needs a literal key of REASONS as its reason: {bad}"


def test_the_deny_default_egress_is_installed_whenever_the_gateway_is_imported():
    from gateway import log_egress

    assert log_egress.installed()
    assert log_scrub.WITHHELD

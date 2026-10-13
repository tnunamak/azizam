from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import Request

import proxy


def _request(body, path="/v1/responses"):
    raw = json.dumps(body).encode()
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.Future()

    return Request(
        {"type": "http", "method": "POST", "path": path,
         "headers": [], "query_string": b""},
        receive,
    )


@pytest.fixture
def isolated_handlers(monkeypatch):
    route_auth = proxy.RuntimeRouteAuth(auth_context=proxy.NO_AUTH_CONTEXT)
    monkeypatch.setattr(
        proxy, "authenticate_runtime_request",
        lambda *args, **kwargs: route_auth,
    )
    monkeypatch.setattr(
        proxy, "_model_dispatch_admission_response",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        proxy.prompt_memory, "decide_for_person",
        lambda *args, **kwargs: SimpleNamespace(
            person_id=None, keep=False, reason="no_person", created_at=0,
        ),
    )
    monkeypatch.setattr(
        proxy.receipt_store, "begin", lambda receipt, *a, **k: receipt.request_id,
    )
    monkeypatch.setattr(
        proxy.receipt_store, "finalize", lambda receipt, *a, **k: True,
    )
    return route_auth


# 1. Source-predicted RED: streaming setup currently has no cleanup owner.
@pytest.mark.parametrize("phase", ["build", "send"])
@pytest.mark.parametrize("cancel", [False, True])
def test_responses_loopback_setup_failure_closes_client(
    monkeypatch, isolated_handlers, phase, cancel,
):
    error = asyncio.CancelledError() if cancel else httpx.ConnectError("offline")
    closed = AsyncMock()

    def build_request(*args, **kwargs):
        if phase == "build":
            raise error
        return object()

    async def send(*args, **kwargs):
        raise error

    client = SimpleNamespace(
        build_request=build_request, send=send, aclose=closed,
    )
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(
        proxy, "_protocol_loopback_source_headers", lambda request: {},
    )
    monkeypatch.setattr(
        proxy, "build_chat_loopback_request",
        lambda **kwargs: SimpleNamespace(
            timeout=1, url="http://fake.invalid/v1/chat/completions",
            headers={}, body=b"{}",
        ),
    )

    async def exercise():
        with pytest.raises(type(error)) as caught:
            await proxy._forward_chat_completions(
                _request({}), proxy.NO_AUTH_CONTEXT,
                {"model": "fake", "messages": []}, stream=True,
            )
        assert caught.value is error

    asyncio.run(exercise())
    closed.assert_awaited_once()


# 2. Source-predicted RED: close failure must not skip client cleanup.
@pytest.mark.parametrize("mode", ["error_headers", "read_error"])
def test_responses_stream_close_failure_still_closes_client(
    monkeypatch, isolated_handlers, mode,
):
    closed = AsyncMock()

    class Upstream:
        status_code = 503 if mode == "error_headers" else 200
        extensions = {}
        headers = {}

        async def aiter_bytes(self):
            yield b'{"error":{"message":"offline"}}'

        async def aclose(self):
            raise RuntimeError("upstream close failed")

    async def forward(*args, **kwargs):
        return SimpleNamespace(aclose=closed), Upstream()

    async def broken_bytes(*args, **kwargs):
        raise OSError("read failed")
        yield b""  # Makes this an async iterator.

    monkeypatch.setattr(proxy, "_forward_chat_completions", forward)
    monkeypatch.setattr(proxy, "iter_llm_response_bytes", broken_bytes)

    async def exercise():
        response = await proxy.create_response(_request({
            "model": "fake", "input": "hello", "stream": True, "store": False,
        }))
        if hasattr(response, "body_iterator"):
            # Exception propagation/translation is a separate contract;
            # this test's invariant is unconditional transport cleanup.
            try:
                async for _ in response.body_iterator:
                    pass
            except RuntimeError as exc:
                assert str(exc) == "upstream close failed"

    asyncio.run(exercise())
    closed.assert_awaited_once()


# 3. Source-predicted RED: valid JSON is not necessarily an error object.
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [400, 429, 503])
@pytest.mark.parametrize("payload", [
    {"error": "upstream unavailable"},
    {"error": ["upstream unavailable"]},
    ["upstream unavailable"],
    None,
])
def test_responses_malformed_error_envelope_keeps_upstream_status(
    monkeypatch, isolated_handlers, stream, status, payload,
):
    closed = AsyncMock()

    async def forward(*args, **kwargs):
        response = httpx.Response(
            status, content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )
        return (SimpleNamespace(aclose=closed), response) if stream else response

    monkeypatch.setattr(proxy, "_forward_chat_completions", forward)
    response = asyncio.run(proxy.create_response(_request({
        "model": "fake", "input": "hello", "stream": stream, "store": False,
    })))
    assert response.status_code == status
    error = json.loads(response.body)["error"]
    assert error["type"] == "upstream_error"
    assert error["code"] == "upstream_error"
    assert isinstance(error["message"], str) and error["message"]
    if stream:
        closed.assert_awaited_once()



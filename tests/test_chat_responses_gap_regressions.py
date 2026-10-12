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
    route_auth = SimpleNamespace(
        auth_context=proxy.NO_AUTH_CONTEXT,
        delegated_to_litellm=True,
        delegated_litellm_api_key="",
    )
    monkeypatch.setattr(
        proxy, "authenticate_runtime_or_litellm_request",
        lambda *args, **kwargs: route_auth,
    )
    monkeypatch.setattr(proxy.state, "update_activity", lambda: None)
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


# 4. Expected GREEN: 429 and 5xx may retry; other 4xx, especially 499, may not.
@pytest.mark.parametrize(
    "status,retry",
    [(200, False), (400, False), (401, False), (403, False),
     (404, False), (409, False), (422, False), (429, True),
     (499, False), (500, True), (503, True), (504, True)],
)
def test_chat_fallback_status_boundary(
    monkeypatch, isolated_handlers, status, retry,
):
    alias = SimpleNamespace(
        target="primary", engine="primary", skipped_engines=(),
        fallbacks=(("backup", "backup-service"),),
    )
    monkeypatch.setattr(proxy, "resolve_stable_alias", lambda *a: alias)
    monkeypatch.setattr(proxy, "config", {
        "model_aliases": {"chat": {"engines": {"backup": {}}}},
    })
    monkeypatch.setattr(
        proxy, "_provider_alias_model", lambda engine, service, spec: engine,
    )
    once = AsyncMock(side_effect=[
        proxy.Response(status_code=status), proxy.Response(status_code=200),
    ])
    monkeypatch.setattr(proxy, "_handle_openai_litellm_request_once", once)
    request = _request(
        {"model": "assistant", "messages": [{"role": "user", "content": "hi"}]},
        "/v1/chat/completions",
    )
    response = asyncio.run(proxy.chat_completions(request))
    assert once.await_count == (2 if retry else 1)
    assert response.status_code == (200 if retry else status)
    assert request.state.provider_requested_model == "assistant"
    assert request.state.paid_provider_fallback is retry
    if retry:
        assert once.await_args_list[1].kwargs["model_override"] == "backup"
        assert response.headers["x-azizam-fallback"] == (
            f"primary->backup (http_{status})"
        )
    else:
        assert "x-azizam-fallback" not in response.headers


# 5. Expected GREEN: visible leaf cannot launder an invisible ancestor.
@pytest.mark.parametrize("hidden_index", [0, 1, 2])
def test_responses_rejects_any_hidden_ancestor_before_translation_or_dispatch(
    monkeypatch, isolated_handlers, hidden_index,
):
    rows = [
        {"id": "oldest", "visible": True},
        {"id": "middle", "visible": True},
        {"id": "leaf", "visible": True},
    ]
    rows[hidden_index]["visible"] = False
    monkeypatch.setattr(proxy.responses_store, "walk_chain", lambda rid: rows)
    monkeypatch.setattr(
        proxy, "_responses_row_visible", lambda row, auth: row["visible"],
    )
    translate = Mock(side_effect=AssertionError("hidden history translated"))
    forward = AsyncMock(side_effect=AssertionError("hidden history dispatched"))
    monkeypatch.setattr(
        proxy.responses_translator, "stored_chain_to_messages", translate,
    )
    monkeypatch.setattr(proxy, "_forward_chat_completions", forward)
    response = asyncio.run(proxy.create_response(_request({
        "model": "fake", "input": "continue",
        "previous_response_id": "leaf", "store": False,
    })))
    assert response.status_code == 404
    error = json.loads(response.body)["error"]
    assert error["code"] == "previous_response_not_found"
    assert error["param"] == "previous_response_id"
    translate.assert_not_called()
    forward.assert_not_awaited()

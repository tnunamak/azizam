from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

import proxy
from gateway.request_body_limits import RequestBodyLimitMiddleware, RequestBodyLimits


def _scope(path="/v1/chat/completions", headers=()):
    return {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "POST", "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "headers": list(headers),
        "server": ("testserver", 80), "client": ("203.0.113.2", 12345),
        "http_version": "1.1",
    }


def _run(app, *, chunks=(), scope=None):
    messages = []
    pending = list(chunks)
    receive = AsyncMock()

    async def next_chunk():
        chunk = pending.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(pending)}

    receive.side_effect = next_chunk

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope or _scope(), receive, send))
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, body, receive


async def _echo_body(scope, receive, send):
    body = await Request(scope, receive).body()
    await JSONResponse({"bytes": len(body)})(scope, receive, send)


def test_declared_oversized_body_rejected_without_reading_or_routing():
    downstream = AsyncMock()
    app = RequestBodyLimitMiddleware(downstream, limits=RequestBodyLimits(json_bytes=8))
    status, body, receive = _run(app, scope=_scope(headers=[(b"content-length", b"9")]))
    assert status == 413
    assert json.loads(body)["error"] == {
        "message": "Request body exceeds the 8-byte limit for this route.",
        "type": "invalid_request_error", "param": None, "code": "request_too_large",
    }
    receive.assert_not_awaited()
    downstream.assert_not_awaited()


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")], [(b"transfer-encoding", b"chunked")]])
def test_chunked_and_understated_lengths_cannot_exceed_limit(headers):
    app = RequestBodyLimitMiddleware(_echo_body, limits=RequestBodyLimits(json_bytes=8))
    status, body, receive = _run(app, chunks=[b"12345", b"6789", b"never read"], scope=_scope(headers=headers))
    assert status == 413
    assert json.loads(body)["error"]["code"] == "request_too_large"
    assert receive.await_count == 2


def test_body_exactly_at_limit_is_accepted():
    app = RequestBodyLimitMiddleware(_echo_body, limits=RequestBodyLimits(json_bytes=8))
    status, body, receive = _run(app, chunks=[b"1234", b"5678"])
    assert status == 200
    assert json.loads(body) == {"bytes": 8}
    assert receive.await_count == 2


def test_route_catching_body_errors_cannot_replace_413_with_500():
    async def catches_everything(scope, receive, send):
        try:
            await Request(scope, receive).body()
        except Exception:
            await JSONResponse({"error": "parser failed"}, status_code=500)(scope, receive, send)

    app = RequestBodyLimitMiddleware(catches_everything, limits=RequestBodyLimits(json_bytes=8))
    status, body, _ = _run(app, chunks=[b"123456789"])
    assert status == 413
    assert json.loads(body)["error"]["code"] == "request_too_large"


def test_limit_survives_fastapi_http_middleware_exception_groups():
    downstream = FastAPI()

    @downstream.middleware("http")
    async def pass_through(request, call_next):
        return await call_next(request)

    @downstream.post("/v1/chat/completions")
    async def read_body(request: Request):
        await request.body()
        return {"ok": True}

    app = RequestBodyLimitMiddleware(downstream, limits=RequestBodyLimits(json_bytes=8))
    response = TestClient(app).post("/v1/chat/completions", content=b"123456789", headers={"content-length": "1"})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_media_multipart_upload_uses_larger_route_limit():
    downstream = FastAPI()

    @downstream.post("/v1/audio/transcriptions")
    async def upload(request: Request):
        form = await request.form()
        return {"bytes": len(await form["file"].read())}

    app = RequestBodyLimitMiddleware(downstream, limits=RequestBodyLimits(json_bytes=32, media_bytes=4096))
    response = TestClient(app).post("/v1/audio/transcriptions", files={"file": ("sample.wav", b"x" * 64, "audio/wav")})
    assert response.status_code == 200
    assert response.json() == {"bytes": 64}


@pytest.mark.parametrize("path", [
    "/v1/audio/transcriptions", "/openai/v1/images/edits", "/v1/videos",
    "/gateway/v1/media/jobs", "/gateway/v1/images/identity", "/gateway/v1/faces/likeness", "/sdapi/v1/txt2img",
])
def test_media_route_classes(path):
    limits = RequestBodyLimits(json_bytes=8, media_bytes=16)
    assert limits.for_path(path) == 16


def test_content_type_cannot_raise_chat_limit():
    app = RequestBodyLimitMiddleware(_echo_body, limits=RequestBodyLimits(json_bytes=8, media_bytes=16))
    status, _, _ = _run(app, chunks=[b"123456789"], scope=_scope(headers=[(b"content-type", b"multipart/form-data")]))
    assert status == 413


def test_configuration_and_environment_override(monkeypatch):
    monkeypatch.setenv("AZIZAM_REQUEST_BODY_JSON_BYTES", "24")
    limits = RequestBodyLimits.from_config({"request_body_limits": {"json_bytes": 16, "media_bytes": 128}})
    assert limits == RequestBodyLimits(json_bytes=24, media_bytes=128)


@pytest.mark.parametrize("value", ["0", "-1", "unlimited"])
def test_config_cannot_disable_body_bound(monkeypatch, value):
    monkeypatch.setenv("AZIZAM_REQUEST_BODY_JSON_BYTES", value)
    with pytest.raises(ValueError):
        RequestBodyLimits.from_config({})


def test_unauthenticated_chat_authenticates_without_reading_body(monkeypatch):
    auth = Mock(return_value=proxy.openai_error_response(
        "Authentication required.", status_code=401, error_type="authentication_error", code="invalid_api_key",
    ))
    monkeypatch.setattr(proxy, "authenticate_runtime_request", auth)
    receive = AsyncMock(side_effect=AssertionError("unauthenticated body was read"))
    request = Request(_scope(), receive)
    response = asyncio.run(proxy._handle_openai_litellm_request(request, "/v1/chat/completions"))
    assert response.status_code == 401
    auth.assert_called_once()
    receive.assert_not_awaited()


def test_unauthenticated_huge_chunked_chat_is_rejected_without_buffering(monkeypatch):
    auth = Mock(return_value=proxy.openai_error_response(
        "Authentication required.", status_code=401, error_type="authentication_error", code="invalid_api_key",
    ))
    monkeypatch.setattr(proxy, "authenticate_runtime_request", auth)
    status, _, receive = _run(proxy.app, chunks=[b"x" * (33 * 1024 * 1024)], scope=_scope(headers=[(b"transfer-encoding", b"chunked")]))
    assert status == 401
    auth.assert_called_once()
    receive.assert_not_awaited()


def test_unauthenticated_huge_declared_chat_is_413_without_reading(monkeypatch):
    auth = Mock(side_effect=AssertionError("oversized declaration should be rejected before routing"))
    monkeypatch.setattr(proxy, "authenticate_runtime_request", auth)
    status, body, receive = _run(proxy.app, scope=_scope(headers=[(b"content-length", b"34603008")]))
    assert status == 413
    assert json.loads(body)["error"]["code"] == "request_too_large"
    auth.assert_not_called()
    receive.assert_not_awaited()


def test_authenticated_chat_auth_runs_before_body_and_is_reused(monkeypatch):
    order = []
    route_auth = proxy.RuntimeRouteAuth(auth_context=proxy.NO_AUTH_CONTEXT)

    def authenticate(*args, **kwargs):
        order.append("auth")
        return route_auth

    async def receive():
        order.append("body")
        return {"type": "http.request", "body": b'{"model":"unknown-test","messages":[]}', "more_body": False}

    handler = AsyncMock(return_value=proxy.Response(status_code=200))
    monkeypatch.setattr(proxy, "authenticate_runtime_request", authenticate)
    monkeypatch.setattr(proxy, "resolve_stable_alias", lambda *args: None)
    monkeypatch.setattr(proxy, "_handle_openai_litellm_request_once", handler)
    request = Request(_scope(), receive)
    response = asyncio.run(proxy._handle_openai_litellm_request(request, "/v1/chat/completions"))
    assert response.status_code == 200
    assert order == ["auth", "body"]
    assert handler.call_args.kwargs["route_auth"] is route_auth

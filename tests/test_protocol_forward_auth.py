"""Protocol loopback forwards the selected real credential, not a LAN placeholder."""
from __future__ import annotations

import asyncio
import ipaddress

import httpx
import pytest
from starlette.requests import Request

import proxy
from gateway import admin_store
from gateway.admin_oidc import AdminSession
from gateway.user_runtime import mint_user_app_key


class _CaptureLoopbackAsyncClient:
    calls: list[dict] = []

    def __init__(self, *args, **kwargs):
        self.closed = False

    async def post(self, url, *, headers=None, content=None, **kwargs):
        self.__class__.calls.append({"url": str(url), "headers": dict(headers or {}), "content": content})
        return httpx.Response(200, json={"ok": True})

    async def aclose(self):
        self.closed = True


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "AUTH_PUBLIC_PATHS", ["/health"])
    monkeypatch.setattr(proxy, "LAN_BYPASS_ENABLED", True)
    monkeypatch.setattr(proxy, "LAN_BYPASS_CIDRS", [ipaddress.ip_network("192.168.0.0/16")])
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [])
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", False)
    monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", False)
    monkeypatch.setattr(proxy, "PATH_TOKENS", {})
    monkeypatch.setattr(proxy.httpx, "AsyncClient", _CaptureLoopbackAsyncClient)
    _CaptureLoopbackAsyncClient.calls = []
    return path


@pytest.fixture
def member_key(gateway):
    user = admin_store.upsert_oidc_user("local", "member", email="member@example.test", path=gateway)
    app = admin_store.create_app("Member access", kind="personal", owner_user_id=user["id"], path=gateway)
    session = AdminSession(subject=user["id"], email=user["email"], role="none", issued_at=1, expires_at=4_000_000_000)
    return mint_user_app_key(session, app["id"], path=gateway)["api_key"]


def _request(headers, *, query_string=b""):
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "path": "/v1/messages",
        "query_string": query_string,
        "headers": [(b"host", b"gateway.example"), *headers, (b"x-client-feature", b"kept")],
        "client": ("192.168.1.25", 41000),
        "server": ("gateway.example", 443),
    })


def _chat_body():
    return {"model": "chat-model", "messages": [{"role": "user", "content": "hi"}]}


def _embeddings_body():
    return {"model": "embedding-model", "input": "hi"}


@pytest.mark.parametrize("forwarder", ["chat", "embeddings"])
@pytest.mark.parametrize("source", ["duplicate-bearer", "api-key", "query"])
def test_protocol_loopback_forwards_selected_member_key_over_lan_placeholder(gateway, monkeypatch, member_key, forwarder, source):
    headers = []
    query_string = b""
    if source == "duplicate-bearer":
        headers = [(b"authorization", b"Bearer dummy"), (b"authorization", f"Bearer {member_key}".encode())]
    elif source == "api-key":
        headers = [(b"api-key", b"dummy"), (b"api-key", member_key.encode())]
    elif source == "query":
        monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", True)
        headers = [(b"authorization", b"Bearer dummy")]
        query_string = f"api_key=dummy&api_key={member_key}".encode()
    request = _request(headers, query_string=query_string)

    if forwarder == "chat":
        response = asyncio.run(proxy._forward_chat_completions(request, proxy.NO_AUTH_CONTEXT, _chat_body(), stream=False))
        expected_path = "/v1/chat/completions"
    else:
        response = asyncio.run(proxy._forward_embeddings(request, proxy.NO_AUTH_CONTEXT, _embeddings_body()))
        expected_path = "/v1/embeddings"

    assert response.status_code == 200
    assert len(_CaptureLoopbackAsyncClient.calls) == 1
    call = _CaptureLoopbackAsyncClient.calls[0]
    assert call["url"].endswith(expected_path)
    assert call["headers"]["authorization"] == f"Bearer {member_key}"
    assert call["headers"].get("x-api-key") is None
    assert call["headers"].get("api-key") is None
    assert call["headers"][proxy._INTERNAL_PROTOCOL_FORWARD_HEADER] == proxy._INTERNAL_PROTOCOL_FORWARD_SECRET

"""The real transport: SSRF-checked, no redirects handled in the wire layer, real sockets on loopback."""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway.engine_protocol import description, executor, transport
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.errors import RedirectRefused, TransportFailure
from gateway.engine_protocol.types import CanonicalRequest, Connection, Credential
from tests.support.fake_upstream.loopback import LoopbackServer

OPS = {"x": {"class": "generate", "request": {"method": "GET", "path": "/x"}, "response": {"framing": "json"}},
       "s": {"class": "generate", "request": {"method": "GET", "path": "/s"}, "response": {"framing": "sse", "terminal": {"sentinel": "[DONE]"}}}}


def make(**kw):
    doc = {"protocol_version": 1, "id": "t", "executor": "http-v1", "operations": OPS, "auth": {"inference": {"kind": "bearer"}}}
    return executor.HttpV1Executor(description.parse_description(doc, source="t.yml"), transport=transport.HttpxTransport(**kw), assets=MemoryAssetStore())


def run(coro):
    return asyncio.run(coro)


def test_link_local_and_cloud_settings_addresses_are_never_dialled() -> None:
    ex = make()
    for address in ("http://169.254.169.254", "http://[fe80::1]:8080", "http://100.100.100.200"):
        with pytest.raises(TransportFailure) as raised:
            run(ex.invoke(Connection(address=address), CanonicalRequest(op="x", body={})))
        assert raised.value.kind == "blocked", address


def test_the_gateways_own_ports_on_this_computer_are_never_dialled() -> None:
    ex = make(gateway_ports=(5000, 15000))
    with pytest.raises(TransportFailure) as raised:
        run(ex.invoke(Connection(address="http://127.0.0.1:5000"), CanonicalRequest(op="x", body={})))
    assert raised.value.kind == "blocked"


def test_a_name_that_resolves_to_a_blocked_address_is_refused_and_only_checked_addresses_are_dialled() -> None:
    async def resolve(host, port=None):
        return ["169.254.169.254"]
    ex = make(resolve=resolve)
    with pytest.raises(TransportFailure) as raised:
        run(ex.invoke(Connection(address="http://engine.example"), CanonicalRequest(op="x", body={})))
    assert raised.value.kind == "blocked"

    async def fail(host, port=None):
        raise OSError("no such host")
    ex = make(resolve=fail)
    with pytest.raises(TransportFailure) as raised:
        run(ex.invoke(Connection(address="http://engine.example"), CanonicalRequest(op="x", body={})))
    assert raised.value.kind == "dns"


async def _app(scope, receive, send):
    path = scope["path"]
    seen.append((path, dict(scope["headers"]).get(b"authorization")))
    if path == "/x":
        body = json.dumps({"ok": True}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})
    elif path == "/s":
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        for piece in (b"data: a\n\n", b"data: b\n\n", b"data: [DONE]\n\n"):
            await send({"type": "http.response.body", "body": piece, "more_body": True})
        await send({"type": "http.response.body", "body": b""})
    else:
        await send({"type": "http.response.start", "status": 302, "headers": [(b"location", b"http://169.254.169.254/latest")]})
        await send({"type": "http.response.body", "body": b""})


seen: list = []


@pytest.fixture()
def server():
    seen.clear()
    srv = LoopbackServer(_app).start()
    try:
        yield srv
    finally:
        srv.stop()


def test_a_real_socket_round_trip_json_and_sse(server) -> None:
    ex = make(gateway_ports=())
    conn = Connection(address=server.base_url, credentials={"inference": Credential("k-123")})
    reply = run(ex.invoke(conn, CanonicalRequest(op="x", body={})))
    assert reply.body == {"ok": True}
    assert seen[0] == ("/x", b"Bearer k-123")

    async def stream():
        out = []
        async for event in await ex.invoke(conn, CanonicalRequest(op="s", body={})):
            out.append(event)
        return out
    assert run(stream()) == [b"a", b"b"]


def test_a_redirect_to_a_blocked_address_is_refused_on_a_real_socket(server) -> None:
    ops = {"r": {"class": "generate", "request": {"method": "GET", "path": "/redir"}, "response": {"framing": "json"}}}
    doc = {"protocol_version": 1, "id": "t", "executor": "http-v1", "operations": ops, "auth": {"inference": {"kind": "bearer"}}}
    ex = executor.HttpV1Executor(description.parse_description(doc, source="t.yml"), transport=transport.HttpxTransport(gateway_ports=()), assets=MemoryAssetStore())
    with pytest.raises(RedirectRefused):
        run(ex.invoke(Connection(address=server.base_url, credentials={"inference": Credential("k-123")}), CanonicalRequest(op="r", body={})))
    assert [p for p, _ in seen] == ["/redir"]  # the redirect target was not requested


def test_connection_refused_is_unreachable() -> None:
    ex = make(gateway_ports=())
    with pytest.raises(TransportFailure) as raised:
        run(ex.invoke(Connection(address="http://127.0.0.1:1"), CanonicalRequest(op="x", body={})))
    assert raised.value.kind == "unreachable"

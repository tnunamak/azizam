"""The one place a reviewed adapter gets an HTTP client, so a request to an engine on another computer cannot skip the guard.

A reviewed adapter (``inline-tts``, ``chunked-stt``) writes its requests with httpx. It does not build the client. It asks
``client(conn, timeout=...)``:

- A connection with no remote authority gets the ordinary client of ``engine_egress``. That client refuses any
  destination the registry marks as another computer (``engine_egress.check_request``), so an engine that is marked remote
  and reached without authority fails closed.
- A connection bound to a remote capability gets a client whose transport is the capability's ``BoundTransport``. Every
  request it sends is one ``BoundTransport.send``: the factory check before any I/O, the allowed header names, the
  operation slot, the connection and byte budgets, the checked address and the revoke watch. There is no second path.

``operation(conn)`` is the fence around a whole adapter call: it checks the service, origin and policy generation first,
then holds the operation open, so a revoke or a deadline cancels the work in flight. Without remote authority it does
nothing.

Nothing here knows an engine's name.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, AsyncIterator

import httpx

from gateway import engine_egress
from gateway.engine_protocol.errors import TransportFailure
from gateway.engine_protocol.transport import WireRequest
from gateway.engine_protocol.types import Connection, Timeouts

# Headers httpx writes itself from the request: the guarded transport rebuilds them (host, length) or sets them (the
# encoding), so they are not forwarded. Any other header the caller set is forwarded and judged by ``BoundTransport.send``.
_WRITTEN_BY_THE_WIRE = frozenset({"host", "content-length", "user-agent", "connection", "accept-encoding"})


def bound_transport(conn: Connection) -> Any:
    """The connection's guarded transport, ``None`` when it has no remote authority, a refusal when the pair is not one."""
    capability, bound = getattr(conn, 'remote_capability', None), getattr(conn, 'remote_transport', None)
    from gateway.remote_engine import BoundTransport, refuse_unbound_connection, refused
    if capability is None and bound is None:
        refuse_unbound_connection(conn)   # an address the registry marks as another computer needs authority
        return None
    if not isinstance(bound, BoundTransport) or bound.capability is not capability:
        raise refused()
    return bound


def client(conn: Connection | None, *, timeout: httpx.Timeout) -> httpx.AsyncClient:
    """An ``httpx`` client for the engine of ``conn``: guarded when the connection is remote, ordinary when it is local.

    No connection (a helper called on its own) gets the ordinary client, which refuses any address marked remote."""
    bound = bound_transport(conn) if conn is not None else None
    if bound is None:
        return engine_egress.async_client(timeout=timeout)
    return httpx.AsyncClient(transport=_GuardedHttpx(bound), timeout=timeout, follow_redirects=False, trust_env=False)


@asynccontextmanager
async def operation(conn: Connection) -> AsyncGenerator[None]:
    """Hold one adapter call inside its remote operation; a call with no remote authority runs as it is."""
    bound = bound_transport(conn)
    if bound is None:
        yield
        return
    capability = bound.capability
    # The check the http-v1 path makes before an operation: service, origin and policy generation, read fresh.
    await asyncio.to_thread(bound.factory.check, capability, service_id=capability.service_id, address=conn.address)
    async with bound.operation():
        yield


class _Body(httpx.AsyncByteStream):
    def __init__(self, response: Any) -> None:
        self._response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.chunks():
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class _GuardedHttpx(httpx.AsyncBaseTransport):
    """httpx's transport seam, answered by ``BoundTransport.send`` and by nothing else."""

    def __init__(self, bound: Any) -> None:
        self._bound = bound

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        wait = request.extensions.get("timeout") or {}
        timeouts = Timeouts(connect_s=wait.get("connect") or Timeouts.connect_s, first_byte_s=wait.get("read") or Timeouts.first_byte_s)
        headers = {name.lower(): value for name, value in request.headers.items() if name.lower() not in _WRITTEN_BY_THE_WIRE}
        body = await request.aread()
        response = await self._bound.send(WireRequest(request.method, str(request.url), headers, body or None), timeouts)
        if response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity"):
            await response.aclose()
            raise TransportFailure("aborted", "The engine compressed its answer although Azizam asked for none. Azizam refuses it.")
        return httpx.Response(response.status, headers=list(response.headers.items()), stream=_Body(response), request=request)

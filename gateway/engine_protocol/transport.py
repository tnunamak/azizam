"""The wire under the executor: one request, one streamed response, no redirects, SSRF-checked.

``HttpxTransport`` reuses the safety rules of ``gateway/engine_check.py``: the
name is looked up once, every address is checked (link-local and cloud settings
addresses refused, the gateway's own ports on this computer refused), and the
socket is opened only to a checked address with the name kept for Host and TLS.
It never follows a redirect: the executor decides, because only the executor
knows the origin a credential is bound to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

import httpx

from gateway import engine_check
from gateway.engine_protocol.errors import TransportFailure
from gateway.engine_protocol.types import Timeouts


@dataclass(frozen=True)
class WireRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None = None


class WireResponse(Protocol):
    status: int
    headers: dict[str, str]   # lower-case names

    def chunks(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class Transport(Protocol):
    async def send(self, request: WireRequest, timeouts: Timeouts) -> WireResponse: ...


class _HttpxResponse:
    def __init__(self, response: httpx.Response, client: httpx.AsyncClient) -> None:
        self._response = response
        self._client = client
        self.status = response.status_code
        self.headers = {k.lower(): v for k, v in response.headers.items()}
        self._closed = False

    async def chunks(self) -> AsyncIterator[bytes]:
        # Identity answers go through aiter_bytes (nothing to decode). An encoded answer is read raw, never decoded here:
        # the executor refuses it before it reads, and a small encoded body cannot expand past a budget.
        encoding = self.headers.get("content-encoding", "identity").strip().lower()
        source = self._response.aiter_bytes() if encoding in ("", "identity") else self._response.aiter_raw()
        try:
            async for chunk in source:
                yield chunk
        except httpx.TimeoutException as exc:
            raise TransportFailure("timeout", f"The engine stopped sending ({type(exc).__name__}).") from None
        except httpx.HTTPError as exc:
            raise TransportFailure("aborted", f"The connection to the engine broke ({type(exc).__name__}).") from None

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._response.aclose()
            await self._client.aclose()


class HttpxTransport:
    """The real transport. ``resolve`` and ``gateway_ports`` are the address-check inputs of ``engine_check``."""

    def __init__(self, *, resolve: engine_check.Resolve | None = None, gateway_ports: engine_check.Ports = engine_check.GATEWAY_PORTS) -> None:
        self._resolve = resolve
        self._gateway_ports = gateway_ports
        self._base: httpx.AsyncBaseTransport | None = None

    @classmethod
    def for_tests(cls, base: httpx.AsyncBaseTransport) -> "HttpxTransport":
        """A transport over ``base`` with no address check: only tests may reach a fake this way."""
        instance = cls()
        instance._base = base
        return instance

    async def send(self, request: WireRequest, timeouts: Timeouts) -> WireResponse:
        if self._base is None:
            destination = await engine_check.check_destination(request.url, resolve=self._resolve, gateway_ports=self._gateway_ports)
            if destination.transport != "ok":
                raise TransportFailure("blocked" if destination.transport == "blocked" else "dns", destination.reason)
            wire: httpx.AsyncBaseTransport = engine_check.checked_transport(destination)
        else:
            wire = self._base
        client = httpx.AsyncClient(
            transport=wire, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(connect=timeouts.connect_s, read=timeouts.first_byte_s, write=30.0, pool=timeouts.connect_s),
        )
        try:
            built = client.build_request(request.method, request.url, headers=request.headers, content=request.body)
            response = await client.send(built, stream=True)
        except httpx.TimeoutException as exc:
            await client.aclose()
            raise TransportFailure("timeout", f"The engine did not answer in time ({type(exc).__name__}).") from None
        except httpx.ConnectError as exc:
            await client.aclose()
            kind, _ = engine_check.connect_failure(exc)  # the text is the system's and the engine's; only the kind is kept
            said = {"dns": "its name could not be resolved", "tls": "the secure connection failed"}.get(kind, "it refused the connection")
            raise TransportFailure("unreachable" if kind == "refused" else kind, f"Azizam could not connect to the engine: {said}.") from None
        except httpx.HTTPError as exc:
            await client.aclose()
            raise TransportFailure("aborted", f"The connection to the engine failed ({type(exc).__name__}).") from None
        except BaseException:
            await client.aclose()
            raise
        return _HttpxResponse(response, client)

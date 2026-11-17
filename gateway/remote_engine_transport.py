"""HTTPS transport for one service capability, using the checked socket connector."""
from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit

import httpx
import httpcore

from gateway import engine_check, engine_egress
from gateway.engine_protocol.errors import TransportFailure
from gateway.engine_protocol.transport import _HttpxResponse
from gateway.remote_engine import origin, refused
from gateway.remote_engine_budget import GuardedStream


class _BoundNetwork(httpcore.AsyncNetworkBackend):
    def __init__(self, inner, bound):
        self.inner, self.bound = inner, bound

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = None
        try:
            async with self.bound.fence():
                budget = self.bound.budget
                async with asyncio.timeout(min(budget.remaining(), budget.limits['connect_s'])):
                    stream = await self.inner.connect_tcp(host, port, timeout=min(budget.remaining(), budget.limits['connect_s']),
                                                          local_address=local_address, socket_options=socket_options)
            return GuardedStream(stream, self.bound)
        except BaseException:
            if stream is not None:
                await asyncio.shield(stream.aclose())
            raise

    async def connect_unix_socket(self, *args, **kwargs):
        raise refused()

    async def sleep(self, seconds):
        await self.inner.sleep(seconds)


class RemoteNetworkTransport:
    def __init__(self, bound, *, resolve=None):
        self.bound = bound
        self.resolve = resolve

    async def send(self, request, timeouts):
        cap = self.bound.capability
        await asyncio.to_thread(self.bound.factory.check, cap, service_id=cap.service_id, address=request.url)
        limits = dict(cap.finite_budgets)
        parts = urlsplit(request.url)
        origin(request.url)
        host, port = parts.hostname, parts.port or 443
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                async with asyncio.timeout(min(limits['dns_s'], self.bound.budget.remaining())):
                    answers = await (self.resolve or engine_check._default_resolve)(host, port)
                addresses = [ipaddress.ip_address(str(answer)) for answer in answers]
            except Exception:
                raise TransportFailure('dns', 'Remote engine resolution failed.') from None
        if not addresses:
            raise refused()
        approved = []
        for address in addresses:
            address = engine_check.plain_address(address)
            # A private exception never permits special-use or metadata destinations.
            if (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast
                    or address.is_reserved or address in engine_check._METADATA_ADDRESSES or str(address) == '168.63.129.16'):
                raise refused()
            private_allowed = any(address.version == ipaddress.ip_network(cidr).version and
                                  address in ipaddress.ip_network(cidr) for cidr in cap.address_exceptions)
            if not address.is_global and not private_allowed:
                raise refused()
            approved.append(str(address))
        destination = engine_check.Destination('ok', '', host.lower(), port, tuple(approved[:1]))
        wire = engine_check.checked_transport(destination)
        wire._pool._network_backend = _BoundNetwork(wire._pool._network_backend, self.bound)
        client = engine_egress.async_client(
            _remote_bound=self.bound, transport=wire, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(connect=min(limits['connect_s'], limits['tls_s']),
                                  read=limits['first_byte_s'], write=limits['connect_s'], pool=limits['connect_s']))
        self.bound.clients.add(client)
        try:
            built = client.build_request(request.method, request.url, headers={**request.headers, 'accept-encoding': 'identity'},
                                         content=request.body)
            response = await client.send(built, stream=True)
            if 'transfer-encoding' in response.headers:
                # Refuse transfer encodings rather than leave trailer headers
                # outside the response-header ceiling.
                raise refused()
            return _HttpxResponse(response, client)
        except BaseException as error:
            await client.aclose()
            self.bound.clients.discard(client)
            if isinstance(error, httpx.HTTPError):
                raise TransportFailure('aborted', 'The remote engine connection failed.') from None
            raise

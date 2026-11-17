"""HTTPS transport for one service capability, using the checked socket connector."""
from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit

import httpx

from gateway import engine_check, engine_egress
from gateway.engine_protocol.errors import TransportFailure
from gateway.engine_protocol.transport import _HttpxResponse
from gateway.remote_engine import origin, refused


class RemoteNetworkTransport:
    def __init__(self, bound, *, resolve=None):
        self.bound = bound
        self.resolve = resolve

    async def send(self, request, timeouts):
        cap = self.bound.capability
        self.bound.factory.check(cap, service_id=cap.service_id, address=request.url)
        limits = dict(cap.finite_budgets)
        parts = urlsplit(request.url)
        origin(request.url)
        host, port = parts.hostname, parts.port or 443
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                async with asyncio.timeout(limits['dns_s']):
                    answers = await (self.resolve or engine_check._default_resolve)(host, port)
                addresses = [ipaddress.ip_address(str(answer)) for answer in answers]
            except Exception:
                raise TransportFailure('dns', 'Remote engine resolution failed.') from None
        if not addresses:
            raise refused()
        approved = []
        for address in addresses:
            address = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped else address
            # A private exception never permits special-use or metadata destinations.
            if (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast
                    or address.is_reserved or address in engine_check._METADATA_ADDRESSES or str(address) == '168.63.129.16'):
                raise refused()
            private_allowed = any(address.version == ipaddress.ip_network(cidr).version and
                                  address in ipaddress.ip_network(cidr) for cidr in cap.address_exceptions)
            if not address.is_global and not private_allowed:
                raise refused()
            approved.append(str(address))
        destination = engine_check.Destination('ok', '', host.lower(), port, tuple(dict.fromkeys(approved)))
        wire = engine_check.checked_transport(destination)
        client = engine_egress.async_client(
            transport=wire, follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(connect=min(limits['connect_s'], limits['tls_s']),
                                  read=limits['first_byte_s'], write=limits['connect_s'], pool=limits['connect_s']))
        try:
            built = client.build_request(request.method, request.url, headers={**request.headers, 'accept-encoding': 'identity'},
                                         content=request.body)
            response = await client.send(built, stream=True)
            return _HttpxResponse(response, client)
        except BaseException as error:
            await client.aclose()
            if isinstance(error, httpx.HTTPError):
                raise TransportFailure('aborted', 'The remote engine connection failed.') from None
            raise

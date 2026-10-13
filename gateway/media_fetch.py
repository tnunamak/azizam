"""Bounded media downloads that never connect to an unchecked DNS address."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping
from urllib.parse import urljoin, urlsplit

import httpx


DEFAULT_MAX_BYTES = 256 * 1024 * 1024
MAX_REDIRECTS = 3
_LAN_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))
_METADATA_ADDRESS = ipaddress.ip_address("fd00:ec2::254")
_EMBEDDED_IPV4_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "::/96", "::ffff:0:0:0/96", "64:ff9b::/96",
))


class MediaFetchError(ValueError):
    """A media URL is unsafe or its download exceeded a resource bound."""


@dataclass(frozen=True)
class FetchedMedia:
    data: bytes
    filename: str
    content_type: str
    headers: Mapping[str, str] = field(default_factory=dict)


def max_bytes() -> int:
    value = int(os.environ.get("AZIZAM_MEDIA_FETCH_MAX_BYTES", DEFAULT_MAX_BYTES))
    if value <= 0:
        raise ValueError("AZIZAM_MEDIA_FETCH_MAX_BYTES must be positive")
    return value


def _private_hosts() -> set[str]:
    return {
        host.strip().lower().rstrip(".")
        for host in os.environ.get("AZIZAM_MEDIA_FETCH_ALLOW_PRIVATE_HOSTS", "").split(",")
        if host.strip()
    }


async def _resolve_addresses(host: str, port: int) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    records = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
        timeout=10,
    )
    return list(dict.fromkeys(record[4][0] for record in records))


def _address_allowed(value: str, allow_private: bool) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    elif isinstance(address, ipaddress.IPv6Address):
        if address.is_site_local:
            return False
        if any(address in network for network in _EMBEDDED_IPV4_NETWORKS):
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if address == _METADATA_ADDRESS:
        return False
    if address.is_global and not address.is_multicast:
        return True
    return allow_private and any(
        address.version == network.version and address in network
        for network in _LAN_NETWORKS
    )


async def read_bounded_media_response(response: httpx.Response, *, limit: int | None = None) -> bytes:
    """Read an open streaming response without buffering past the media cap."""
    limit = max_bytes() if limit is None else limit
    encoding = response.headers.get("content-encoding", "identity").lower()
    if encoding != "identity":
        raise MediaFetchError("media server must send an uncompressed response")
    length = response.headers.get("content-length")
    if length is not None and int(length) > limit:
        raise MediaFetchError("media exceeds the download byte limit")
    data = bytearray()
    async for chunk in response.aiter_raw():
        if len(data) + len(chunk) > limit:
            raise MediaFetchError("media exceeds the download byte limit")
        data.extend(chunk)
    return bytes(data)


async def safe_fetch_media(
    url: str,
    *,
    internal_resolver: Callable[[str], Awaitable[FetchedMedia | None]] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    limit: int | None = None,
    max_redirects: int = MAX_REDIRECTS,
    schemes: frozenset[str] = frozenset({"http", "https"}),
    timeout: float = 60,
    accept: str | None = None,
) -> FetchedMedia:
    """Validate every hop, pin its IP, and stream at most the configured bytes.

    The optional transport is for deterministic tests. Production uses HTTPX's
    normal TLS transport, with certificate validation against the original SNI.
    No caller headers, cookies, credentials, or environment proxies are used.
    Other fetchers of untrusted URLs narrow ``limit``, ``max_redirects``,
    ``schemes`` and ``timeout``.
    """
    limit = max_bytes() if limit is None else limit
    async with asyncio.timeout(timeout):
        for hop in range(max_redirects + 1):
            parsed = urlsplit(url)
            if parsed.scheme not in schemes or not parsed.hostname:
                raise MediaFetchError(f"URL must use {' or '.join(sorted(schemes))}")
            if parsed.username is not None or parsed.password is not None:
                raise MediaFetchError("media URL must not contain credentials")
            if internal_resolver is not None:
                internal = await internal_resolver(url)
                if internal is not None:
                    if len(internal.data) > limit:
                        raise MediaFetchError("media exceeds the download byte limit")
                    return internal
            host = parsed.hostname.lower().rstrip(".")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = await _resolve_addresses(host, port)
            if not addresses or any(
                not _address_allowed(address, host in _private_hosts())
                for address in addresses
            ):
                raise MediaFetchError("media URL resolves to a blocked network address")

            # Connect to the checked IP rather than resolving the hostname
            # again. Host routes HTTP correctly; SNI also preserves TLS
            # hostname and certificate verification on HTTPS connections.
            # Each hop gets its own pool so two hostnames sharing an IP do not
            # reuse a TLS connection verified for the previous hostname.
            async with AsyncExitStack() as resources:
                for index, address in enumerate(addresses):
                    pinned = httpx.URL(url).copy_with(host=address)
                    client = await resources.enter_async_context(httpx.AsyncClient(
                        timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
                        trust_env=False, transport=transport,
                    ))
                    try:
                        response = await resources.enter_async_context(client.stream(
                            "GET", pinned,
                            headers={"host": parsed.netloc, "accept-encoding": "identity",
                                     **({"accept": accept} if accept else {})},
                            extensions={"sni_hostname": host},
                        ))
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        await client.aclose()
                        if index == len(addresses) - 1:
                            raise
                    else:
                        break
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == max_redirects:
                        raise MediaFetchError("media URL exceeded the redirect limit")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                data = await read_bounded_media_response(response, limit=limit)
                filename = parsed.path.rsplit("/", 1)[-1] or "input_reference.png"
                return FetchedMedia(data, filename, response.headers.get("content-type", "application/octet-stream"),
                                    dict(response.headers))
    raise MediaFetchError("media download did not complete")

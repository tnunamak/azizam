"""Add engine: check a draft engine, and run one real test against it.

The check is the probe ladder of the Add engine spec (sections 5 and 5a):
the definition's own request first, a random-path baseline beside it, then
metadata reads from the data table, then an empty POST to each standard route
whose answer is still unknown. It reads status classes and error bodies and
turns them into findings, capability levels and a model list. Every rule it
applies comes from ``registry/engine_definitions.json``; this module names no
engine.

Safety (spec section 5): only GET and empty-body POST during the check; no
redirects; a timeout per request; a size cap per answer; nothing to link-local
or cloud-metadata addresses; nothing to the gateway's own ports on this
computer. The real test is a separate, explicit request of one kind the owner picks.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import ipaddress
import json
import math
import re
import secrets
import socket
import ssl
import struct
import time
import wave
import zlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol
from urllib.parse import quote, urlsplit

import httpcore
import httpx

from gateway import transforms
from gateway.capability_ids import offered_model_kinds
from gateway.engine_definitions import LIST_SHAPES, UPLOAD_SAMPLES, EngineCatalog, real_test_id
from gateway.engine_readiness import safe_probe_path
from gateway import engine_egress

DEFAULT_CAP_BYTES = 64 * 1024
TEST_CAP_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_S = 5.0
TEST_TIMEOUT_S = 120.0
GATEWAY_PORTS = frozenset({5000, 15000})
# Cloud settings services answer on these addresses. They are never an engine.
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(value) for value in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200")
)
SECRET_MASK = "<configured>"
_BASELINE_PREFIX = "zz-not-a-route-"


def _baseline_token() -> str:
    """A fresh made-up path each check, so a server cannot learn and fake it."""
    return secrets.token_hex(4)
_REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
    403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    413: "Content Too Large", 415: "Unsupported Media Type", 422: "Unprocessable Content",
    429: "Too Many Requests", 500: "Internal Server Error", 501: "Not Implemented",
    502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout",
}


# ---------------------------------------------------------------------------
# One request and its answer
# ---------------------------------------------------------------------------


@dataclass
class Answer:
    """What one request got back. ``transport`` is ``ok`` when an HTTP answer arrived."""

    method: str
    url: str
    status: int | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    transport: str = "ok"  # ok, refused, dns, timeout, tls, redirect, blocked, too_large
    detail: str = ""
    location: str = ""
    seconds: float = 0.0
    # The request carried the owner's stored key and an administrator asked: the server's own words are not shown.
    withheld: bool = False

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def json(self) -> Any:
        try:
            return json.loads(self.body) if self.body else None
        except ValueError:
            return None

    @property
    def status_text(self) -> str:
        if self.status is None:
            return ""
        return f"{self.status} {_REASONS.get(self.status, '')}".strip()


class Fetch(Protocol):
    def __call__(
        self, method: str, url: str, *, headers: Mapping[str, str], cap_bytes: int,
        timeout_s: float, content: bytes | None = None,
    ) -> Awaitable[Answer]: ...


Resolve = Callable[..., Awaitable[list[str]]]
# The gateway's own ports: a fixed set, or a function read each time a request is made.
Ports = Iterable[int] | Callable[[], Iterable[int]]


async def _default_resolve(host: str, port: int | None = None) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=3)
    return sorted({info[4][0] for info in infos})


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def plain_address(address: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """The address a connection reaches: IPv4 written as IPv6 is that IPv4 address.

    Covers IPv4-mapped (::ffff:a.b.c.d), IPv4-translated (::ffff:0:a.b.c.d),
    IPv4-compatible (::a.b.c.d) and the NAT64 prefix (64:ff9b::a.b.c.d), so
    every rule below sees the IPv4 address.
    """
    if not isinstance(address, ipaddress.IPv6Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    packed = address.packed
    tail = ipaddress.IPv4Address(packed[12:])
    if packed[:12] == bytes(8) + b"\xff\xff\x00\x00":
        return tail
    if packed[:12] == bytes(12) and int(address) > 1:
        return tail
    if address in _NAT64:
        return tail
    return address


def is_this_computer(address: ipaddress._BaseAddress) -> bool:
    """True when ``address`` belongs to this computer: loopback, unspecified, or one a socket can bind to.

    Binding needs no interface list, so a LAN, VPN or container bridge address of
    this computer counts too.
    """
    address = plain_address(address)
    if address.is_loopback or address.is_unspecified:
        return True
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.bind((str(address), 0))
        return True
    except OSError:
        return False


def _port_of(parts: Any) -> int:
    try:
        port = parts.port
    except ValueError:
        port = None
    return port or (443 if parts.scheme == "https" else 80)


@dataclass(frozen=True)
class Destination:
    """Where one request may go. ``addresses`` are the checked addresses to dial, and only those."""

    transport: str  # ok, blocked or dns
    reason: str = ""
    host: str = ""
    port: int = 0
    addresses: tuple[str, ...] = ()


async def _ports(gateway_ports: Ports) -> set[int]:
    if callable(gateway_ports):
        # The gateway reads its configured ports from its store: off the event loop.
        return {int(port) for port in await asyncio.to_thread(gateway_ports)}
    return {int(port) for port in gateway_ports}


async def check_destination(url: str, *, resolve: Resolve | None = None, gateway_ports: Ports = GATEWAY_PORTS) -> Destination:
    """Look the name up once and check every address it answers.

    Nothing is sent when any address is a link-local or cloud settings address,
    or is this computer on one of the gateway's own ports, or when the name
    cannot be looked up (fail closed). The caller dials only ``addresses``, so
    a second lookup cannot answer something else (DNS rebinding).
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").strip("[]")
    if parts.scheme not in {"http", "https"} or not host:
        return Destination("blocked", "The address must start with http:// or https://.")
    port = _port_of(parts)
    try:
        found = [ipaddress.ip_address(host)]
    except ValueError:
        lookup = resolve or _default_resolve
        try:
            try:
                answers = await lookup(host, port)
            except TypeError:
                answers = await lookup(host)
            found = [ipaddress.ip_address(str(value).split("%")[0]) for value in answers]
        except Exception as exc:  # noqa: BLE001 - a name that cannot be looked up gets nothing (fail closed)
            return Destination("dns", f"Azizam could not find {host}: {exc or type(exc).__name__}", host, port)
        if not found:
            return Destination("dns", f"Azizam could not find {host}: the name has no address.", host, port)
    addresses = [plain_address(address) for address in found]
    ports = await _ports(gateway_ports)
    for address in addresses:
        if address in _METADATA_ADDRESSES or address.is_link_local:
            return Destination("blocked", f"{host} is a link-local or cloud settings address. No engine answers there.", host, port)
        if port in ports and is_this_computer(address):
            return Destination("blocked", f"Port {port} on this computer is the gateway itself, so Azizam does not call it.", host, port)
    return Destination("ok", "", host.lower(), port, tuple(dict.fromkeys(str(address) for address in addresses)))


async def blocked_reason(url: str, *, resolve: Resolve | None = None, gateway_ports: Ports = GATEWAY_PORTS) -> str | None:
    """A sentence when Azizam must not send anything to ``url``; ``None`` when it may."""
    destination = await check_destination(url, resolve=resolve, gateway_ports=gateway_ports)
    return destination.reason or None


def blocked_reason_blocking(url: str, *, resolve: Resolve | None = None, gateway_ports: Ports = GATEWAY_PORTS) -> str | None:
    """``blocked_reason`` for code that is not async (a save in the admin store). It runs the lookup in its own thread."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(blocked_reason(url, resolve=resolve, gateway_ports=gateway_ports))
        ).result()


def static_blocked_reason(url: str, *, gateway_ports: Iterable[int] = GATEWAY_PORTS) -> str | None:
    """The part of ``check_destination`` that needs no lookup: an address written in the URL.

    A link-local or cloud settings address, or a gateway port on this computer. A name is not looked up
    here; it is checked when it is used (``check_destination``) and when it is saved.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").strip("[]")
    if parts.scheme not in {"http", "https"} or not host:
        return "The address must start with http:// or https://."
    try:
        address = plain_address(ipaddress.ip_address(host))
    except ValueError:
        address = None
    if address is not None and (address in _METADATA_ADDRESSES or address.is_link_local):
        return f"{host} is a link-local or cloud settings address. No engine answers there."
    port = _port_of(parts)
    if port in {int(value) for value in gateway_ports} and (
        host.lower() == "localhost" or (address is not None and is_this_computer(address))
    ):
        return f"Port {port} on this computer is the gateway itself, so Azizam does not call it."
    return None


class _CheckedAddresses(httpcore.AsyncNetworkBackend):
    """Opens connections only to the addresses ``check_destination`` validated, for the one name it checked."""

    def __init__(self, destination: Destination) -> None:
        self._destination = destination
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None,
                          socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        if host.strip("[]").lower() != self._destination.host or port != self._destination.port:
            raise httpcore.ConnectError(f"{host}:{port} was not checked")
        failure: Exception = httpcore.ConnectError("no checked address")
        for address in self._destination.addresses:
            try:
                return await self._inner.connect_tcp(address, port, timeout=timeout, local_address=local_address,
                                                     socket_options=socket_options)
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure

    async def connect_unix_socket(self, path: str, timeout: float | None = None,
                                  socket_options: Any = None) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Add engine never opens a local socket file.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def checked_transport(destination: Destination) -> httpx.AsyncHTTPTransport:
    """An httpx transport whose sockets go only to the checked addresses.

    The URL keeps the name, so the Host header and the TLS server name (SNI and
    certificate check) stay the name. httpx builds its connection pool
    privately; this replaces it with one that dials through ``_CheckedAddresses``
    (tests/test_engine_check_network.py fails if that stops working).
    """
    transport = httpx.AsyncHTTPTransport(retries=0)
    transport._pool = httpcore.AsyncConnectionPool(  # noqa: SLF001
        ssl_context=transport._pool._ssl_context,  # noqa: SLF001
        network_backend=_CheckedAddresses(destination),
        retries=0,
    )
    return transport




class _Decoder:
    """Content decoding with a ceiling on what each step may produce, so a small compressed answer cannot fill memory."""

    def __init__(self, encoding: str) -> None:
        encoding = encoding.strip().lower()
        if encoding in ("gzip", "x-gzip"):
            self._zlib: Any = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._zlib = zlib.decompressobj(zlib.MAX_WBITS)
        else:
            # Identity, or an encoding Azizam did not ask for and does not unpack: the bytes as sent, capped.
            self._zlib = None

    def feed(self, data: bytes, limit: int) -> bytes:
        if self._zlib is None:
            return data[:limit]
        try:
            return self._zlib.decompress(self._zlib.unconsumed_tail + data if self._zlib.unconsumed_tail else data, limit)
        except zlib.error:
            return b""

    def more(self, limit: int) -> bytes:
        if self._zlib is None or not self._zlib.unconsumed_tail:
            return b""
        try:
            return self._zlib.decompress(self._zlib.unconsumed_tail, limit)
        except zlib.error:
            return b""


def make_http_fetch(*, resolve: Resolve | None = None, gateway_ports: Ports = GATEWAY_PORTS) -> Fetch:
    """The real fetcher: no redirects, a timeout, a size cap and the address rules above.

    ``resolve`` defaults to the computer's own lookup, read when a request is
    made. ``gateway_ports`` may be a function, so the ports the gateway is
    configured to listen on are read each time.
    """

    async def fetch(
        method: str, url: str, *, headers: Mapping[str, str], cap_bytes: int,
        timeout_s: float, content: bytes | None = None,
    ) -> Answer:
        answer = Answer(method=method, url=url)
        try:
            for name, value in headers.items():
                (name + value).encode("ascii")
        except UnicodeEncodeError:
            answer.transport, answer.detail = "blocked", "A key or header value holds a character a request cannot carry."
            return answer
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout_s * 2 + 1):
                destination = await check_destination(url, resolve=resolve, gateway_ports=gateway_ports)
                if destination.transport != "ok":
                    answer.transport, answer.detail = destination.transport, destination.reason
                    return answer
                # Ask for the answer uncompressed, whatever the header rows say.
                sent = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}
                sent["accept-encoding"] = "identity"
                async with engine_egress.async_client(
                    transport=checked_transport(destination), follow_redirects=False, trust_env=False,
                    timeout=httpx.Timeout(timeout_s),
                ) as client, client.stream(
                    method, url, headers=sent, content=content,
                    extensions=engine_egress.probe() if method.upper() == "GET" else None,
                ) as response:
                    answer.status = response.status_code
                    answer.headers = {k.lower(): v for k, v in response.headers.items()}
                    if 300 <= response.status_code < 400:
                        answer.transport = "redirect"
                        answer.location = response.headers.get("location", "")
                        return answer
                    decoder = _Decoder(response.headers.get("content-encoding", ""))
                    chunks: list[bytes] = []
                    total = 0
                    async for raw in response.aiter_raw():
                        piece = decoder.feed(raw, cap_bytes - total + 1)
                        while True:
                            total += len(piece)
                            chunks.append(piece)
                            if total > cap_bytes:
                                break
                            piece = decoder.more(cap_bytes - total + 1)
                            if not piece:
                                break
                        if total > cap_bytes:
                            answer.transport = "too_large"
                            answer.detail = f"The answer was larger than {cap_bytes // 1024} KB."
                            break
                    answer.body = b"".join(chunks)[:cap_bytes]
        except (httpx.TimeoutException, TimeoutError):
            answer.transport, answer.detail = "timeout", f"waited {timeout_s:g} s, no reply"
        except httpx.ConnectError as exc:
            answer.transport, answer.detail = _connect_failure(exc)
        except httpx.HTTPError as exc:
            answer.transport, answer.detail = "refused", str(exc) or type(exc).__name__
        except Exception as exc:  # noqa: BLE001 - a check reports a failure; it never crashes the route
            while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                exc = exc.exceptions[0]  # the real failure, not "unhandled errors in a TaskGroup"
            answer.transport, answer.detail = "refused", str(exc) or type(exc).__name__
        finally:
            answer.seconds = round(time.perf_counter() - started, 2)
        return answer

    return fetch


def _connect_failure(exc: BaseException) -> tuple[str, str]:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, socket.gaierror):
            return "dns", str(seen)
        if isinstance(seen, ssl.SSLError):
            return "tls", str(seen)
        seen = seen.__cause__ or seen.__context__
    text = str(exc) or type(exc).__name__
    lowered = text.lower()
    if "name or service not known" in lowered or "nodename nor servname" in lowered or "getaddrinfo" in lowered:
        return "dns", text
    if "ssl" in lowered or "certificate" in lowered:
        return "tls", text
    return "refused", text


connect_failure = _connect_failure  # public name for gateway/engine_protocol/transport.py


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


class DraftError(ValueError):
    """Draft values that cannot be checked at all. ``field`` names the input at fault."""

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


def normalize_address(value: Any, *, placeholder: str = "") -> str:
    text = str(value or "").strip().rstrip("/")
    parts = urlsplit(text)
    example = f", for example {placeholder}" if placeholder else ""
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise DraftError(f"Enter an address that starts with http:// or https://{example}.", field="address")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise DraftError("Leave the user name, password, query and fragment out of the address.", field="address")
    try:
        parts.port
    except ValueError:
        raise DraftError("The port in the address is not a number between 1 and 65535.", field="address") from None
    return text


def origin_of(address: str) -> str:
    parts = urlsplit(address)
    host = (parts.hostname or "").lower()
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme.lower()}://{host}:{_port_of(parts)}"


def join_url(base: str, path: str) -> str:
    if not path:
        return base.rstrip("/")
    return base.rstrip("/") + (path if path.startswith("/") else f"/{path}")


def _plain_route(url: str) -> bool:
    """True when ``url`` has no ``?`` or ``#`` (even an empty query or fragment, which ends the path the client sends)
    and a path the HTTP client sends as written (``safe_probe_path``)."""
    if "?" in url or "#" in url:
        return False
    return safe_probe_path(urlsplit(url).path or "/") is not None


def check_base(address: str, prefix: str) -> str:
    """The URL the check builds its routes on: the normalised ``address`` with the draft's path prefix.

    A dot segment, encoded separator, query or fragment in the address or the prefix would let the HTTP client send a
    route other than the one shown, so such a base is refused, as the readiness poll refuses such a ``base_path``.
    """
    base = join_url(address, prefix) if prefix else address
    if _plain_route(base):
        return base
    field = "address" if not _plain_route(address) else "path_prefix"
    raise DraftError("Use a plain path, with no '.' or '..' part, encoded slash, query or fragment.", field=field)


def comparable_address(address: str) -> str:
    """One spelling per address, so a duplicate is found whatever the case or default port."""
    parts = urlsplit(address.strip())
    return origin_of(address) + (parts.path or "").rstrip("/").lower()


def host_label(address: str) -> str:
    parts = urlsplit(address)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host.split(".")[0] if host not in {"localhost"} else f"Engine on this computer, port {_port_of(parts)}"
    return f"Engine at {host}:{_port_of(parts)}"


# ---------------------------------------------------------------------------
# Small data helpers
# ---------------------------------------------------------------------------


def _at(value: Any, path: str) -> Any:
    for part in path.split(".") if path else ():
        if isinstance(value, Mapping):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


class _Defaults(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _fill(text: str, values: Mapping[str, Any]) -> str:
    return text.format_map(_Defaults({k: "" if v is None else v for k, v in values.items()}))


def _excerpt(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _error_message(answer: Answer) -> str:
    data = answer.json
    for path in ("error.message", "error", "detail", "message", "msg"):
        value = _at(data, path)
        if isinstance(value, str) and value.strip():
            return _excerpt(value)
    return _excerpt(answer.text) if answer.text.strip() and "<html" not in answer.text.lower() else answer.status_text


def _auth_headers(key: str | None, values: Mapping[str, Any]) -> dict[str, str]:
    from gateway.litellm_substrate import header_problem

    headers: dict[str, str] = {}
    for row in values.get("headers") or ():
        if isinstance(row, Mapping):
            name, value = str(row.get("name") or "").strip(), row.get("value")
            if (name and isinstance(value, str) and value and value != SECRET_MASK
                    and not row.get("secret") and not header_problem(name, value) and _header_ok(name, value)):
                headers[name] = value
    if key and _header_ok("Authorization", key):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _header_ok(name: str, value: str) -> bool:
    return bool(re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)) and not any(ch in value for ch in "\r\n")


def _curl(answer: Answer, *, key_sent: bool, timeout: bool, timeout_s: float = DEFAULT_TIMEOUT_S) -> str:
    limit = f" --max-time {timeout_s:g}" if timeout else ""
    command = "curl -i" + limit + (" -X POST" if answer.method == "POST" else "") + f" {answer.url}"
    if key_sent:
        command += ' \\\n  -H "Authorization: Bearer YOUR_KEY"'
    if answer.method == "POST":
        command += " \\\n  -H \"content-type: application/json\" -d '{}'"
    return command


HIDDEN = "[hidden]"
WITHHELD_TEXT = ("Azizam doesn’t show the server’s own words here, because this request carried the owner’s "
                 "stored key; only the owner sees them.")


def secret_spellings(values: Iterable[str | None]) -> list[bytes]:
    """The ways a sent key can come back: as sent, after "Bearer ", base64, percent- and JSON-encoded."""
    out: set[bytes] = set()
    for value in values:
        value = (value or "").strip()
        if len(value) < 4:
            continue
        for form in (value, f"Bearer {value}"):
            raw = form.encode("utf-8")
            out.add(raw)
            out.add(json.dumps(form)[1:-1].encode("utf-8"))
            for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
                out.add(encoded)
                out.add(encoded.rstrip(b"="))
            out.add("".join(f"%{byte:02X}" for byte in raw).encode())
            out.add("".join(f"%{byte:02x}" for byte in raw).encode())
            out.add(quote(form, safe="").encode())
    return sorted(out, key=len, reverse=True)


def _scrub_bytes(data: bytes, needles: list[bytes]) -> bytes:
    for needle in needles:
        if needle in data:
            data = data.replace(needle, HIDDEN.encode())
    return data


def _scrub_text(text: str, needles: list[bytes]) -> str:
    return _scrub_bytes(text.encode("utf-8", "surrogateescape"), needles).decode("utf-8", "replace") if text else text


def guarded_fetch(fetch: Fetch, key: "KeyInput", *, viewer_role: str) -> Fetch:
    """Wrap ``fetch`` so no answer carries the key that was sent.

    Every spelling of the key is replaced in the body, the headers, Location and
    the error text before anything reads the answer, so neither results nor
    saved summaries can hold it. When an administrator's request carried the
    owner's stored key, a refusal or redirect also loses its body, headers and
    Location (a hostile server could split or re-encode the key past any
    scrub), and the answer is marked ``withheld`` so replies are not quoted.
    """
    needles = secret_spellings([key.value])
    withhold = viewer_role != "owner" and key.source in ("stored", "env") and bool(key.value)
    if not needles and not withhold:
        return fetch

    async def guarded(method: str, url: str, **kwargs: Any) -> Answer:
        answer = await fetch(method, url, **kwargs)
        answer.body = _scrub_bytes(answer.body, needles)
        answer.headers = {name: _scrub_text(value, needles) for name, value in answer.headers.items()}
        answer.location = _scrub_text(answer.location, needles)
        answer.detail = _scrub_text(answer.detail, needles)
        if withhold:
            answer.withheld = True
            if answer.status is None or answer.status >= 300:
                answer.body, answer.headers, answer.location = b"", {}, ""
        return answer

    return guarded  # type: ignore[return-value]


def _raw(answer: Answer) -> str:
    if answer.transport not in {"ok", "too_large", "redirect"} or answer.status is None:
        return f"{answer.method} {answer.url}\n{answer.detail}".strip()
    head = f"HTTP/1.1 {answer.status_text}"
    content_type = answer.headers.get("content-type")
    if content_type:
        head += f"\ncontent-type: {content_type}"
    if answer.location:
        head += f"\nlocation: {answer.location}"
    if answer.withheld:
        body = WITHHELD_TEXT
    elif (content_type or "").lower().startswith(("audio/", "image/", "video/", "application/octet-stream")):
        body = f"[{len(answer.body)} bytes]"
    else:
        body = answer.text[:800]
    return f"{head}\n\n{body}" if body else head


# ---------------------------------------------------------------------------
# Lists a server reports (probes.lists)
# ---------------------------------------------------------------------------

MAX_LIST_ENTRIES = 2000
_MISSING = object()


def _pointer(value: Any, pointer: str) -> Any:
    """The part of ``value`` a JSON pointer names, or ``_MISSING``."""
    for part in pointer.split("/")[1:] if pointer else ():
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def _first_text(item: Mapping[str, Any], keys: Iterable[str]) -> str:
    return next((item[key].strip() for key in keys if isinstance(item.get(key), str) and item[key].strip()), "")


def _entry_ids(items: Any, read: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``(id, name)`` for each entry of a list: a string is its own id, an object names its id by ``id_keys``."""
    out: list[tuple[str, str]] = []
    for item in items if isinstance(items, list) else ():
        if isinstance(item, str) and item.strip():
            out.append((item.strip(), item.strip()))  # a bare id names no field, so a where keeps it
        elif isinstance(item, Mapping):
            where = read.get("where")
            # An entry that names the field must hold the text; one that does not name it is kept (a server
            # that names no task lists only what it serves).
            if where and where["key"] in item and item[where["key"]] != where["equals"]:
                continue
            ident = _first_text(item, read.get("id_keys") or ("id",))
            if ident:
                out.append((ident, _first_text(item, read.get("name_keys") or ()) or ident))
    return out


def _read_array_of_strings(node: Any, read: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(item.strip(), item.strip()) for item in node if isinstance(item, str) and item.strip()] if isinstance(node, list) else []


def _read_array_of_objects(node: Any, read: Mapping[str, Any]) -> list[tuple[str, str]]:
    return _entry_ids([item for item in node if isinstance(item, Mapping)] if isinstance(node, list) else [], read)


def _read_object_keys(node: Any, read: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(key.strip(), key.strip()) for key in node if isinstance(key, str) and key.strip()] if isinstance(node, Mapping) else []


def _read_tuple_first(node: Any, read: Mapping[str, Any]) -> list[tuple[str, str]]:
    return _entry_ids(node[0], read) if isinstance(node, list) and node and isinstance(node[0], list) else []


def _read_nested(node: Any, read: Mapping[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in node if isinstance(node, list) else ():
        if isinstance(entry, Mapping):
            out += _entry_ids(entry.get(read.get("inner", "")), read)
    return out


# One reader for each shape the definitions may name (gateway.engine_definitions.LIST_SHAPES).
_LIST_READERS: dict[str, Callable[[Any, Mapping[str, Any]], list[tuple[str, str]]]] = {
    "array_of_strings": _read_array_of_strings,
    "array_of_objects": _read_array_of_objects,
    "object_keys": _read_object_keys,
    "tuple_first": _read_tuple_first,
    "nested": _read_nested,
}
assert tuple(_LIST_READERS) == LIST_SHAPES, "every list shape needs one reader"


def read_list(body: Any, read: Mapping[str, Any]) -> dict[str, Any]:
    """Read one JSON answer as a list of ids by the shape ``read`` names.

    ``{"ids": [{"id", "name"}, ...], "default": str | None, "truncated": bool}``: ids without repeats, in the server's
    order, at most ``max_entries`` of them (``MAX_LIST_ENTRIES`` when the read names none); ``truncated`` says more were
    there. An answer that does not have the shape gives no ids; nothing here raises on a stranger's JSON.
    """
    reader = _LIST_READERS.get(read.get("shape", ""))
    node = _pointer(body, read.get("select", ""))
    pairs = reader(node, read) if reader is not None and node is not _MISSING else []
    seen: set[str] = set()
    ids = []
    limit = read.get("max_entries") or MAX_LIST_ENTRIES
    truncated = False
    for ident, name in pairs:
        if ident in seen:
            continue
        if len(ids) >= limit:
            truncated = True
            break
        seen.add(ident)
        ids.append({"id": ident, "name": name})
    default = _pointer(body, read["default_select"]) if read.get("default_select") else _MISSING
    return {"ids": ids, "default": default.strip() if isinstance(default, str) and default.strip() else None,
            "truncated": truncated}


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


@dataclass
class KeyInput:
    """The key the check may send. ``withheld`` says a stored key was kept back from a new address."""

    value: str | None = None
    source: str = "none"  # none, pasted, env, stored
    withheld: bool = False


@dataclass
class _Ctx:
    catalog: EngineCatalog
    definition: Mapping[str, Any]
    values: Mapping[str, Any]
    address: str
    origin: str
    key: KeyInput
    headers: dict[str, str]
    fetch: Fetch
    timeout_s: float
    token: str
    viewer_role: str


def _route_url(ctx: _Ctx, path: str, frm: str = "address") -> str:
    return join_url(ctx.origin if frm == "origin" else ctx.address, path)


def _primary_url(ctx: _Ctx) -> tuple[str, str]:
    probe = ctx.definition["probes"]["primary"]
    url = _route_url(ctx, probe["path"], probe["from"])
    if safe_probe_path(probe["path"]) is None or not _plain_route(url):
        raise DraftError("The engine definition declares a readiness path that cannot be checked safely.", field="address")
    return probe["method"], url


def call_line(definition: Mapping[str, Any], values: Mapping[str, Any]) -> str:
    """The request the check makes first, as the dialog shows it: "GET http://…/models"."""
    try:
        address = normalize_address(values.get("address"))
        base = check_base(address, str(values.get("path_prefix") or "").strip())
        # The same URL the check sends (``_primary_url``): a probe from the origin ignores the path prefix.
        method, url = _primary_url(SimpleNamespace(definition=definition, address=base, origin=origin_of(address)))
    except DraftError:
        return ""
    return f"{method} {url}"


MODEL_LIST_CAP_BYTES = 1024 * 1024


async def _get(ctx: _Ctx, url: str, method: str = "GET", cap: int = DEFAULT_CAP_BYTES) -> Answer:
    content = b"{}" if method == "POST" else None
    headers = dict(ctx.headers)
    if content is not None:
        headers["content-type"] = "application/json"
    return await ctx.fetch(method, url, headers=headers, cap_bytes=cap, timeout_s=ctx.timeout_s, content=content)


def _matches(when: Mapping[str, Any], obs: Mapping[str, Any]) -> bool:
    for key, expected in when.items():
        if key == "transport_in":
            if obs.get("transport") not in expected:
                return False
        elif key == "status_in":
            if obs.get("status") not in expected:
                return False
        elif key == "status_class":
            status = obs.get("status")
            if not isinstance(status, int) or status // 100 != expected:
                return False
        elif key == "address_matches":
            if not re.search(expected, str(obs.get("address") or "")):
                return False
        elif key == "body_matches":
            if not re.search(expected, str(obs.get("body") or ""), re.IGNORECASE):
                return False
        elif obs.get(key) != expected:
            return False
    return True


def _finding(row: Mapping[str, Any], fill: Mapping[str, Any], *, viewer_role: str, answer: Answer | None,
             key_sent: bool, everywhere: bool = False) -> dict[str, Any]:
    admin = viewer_role != "owner"
    why = row.get("why_everywhere") if everywhere and row.get("why_everywhere") else row["why"]
    out: dict[str, Any] = {
        "code": row["code"],
        "severity": row["severity"],
        "field": row["field"],
        "title": _fill(row["title"], fill),
        "what": _fill(row["what"], fill),
        "why": _fill(why, fill),
        "fix": _fill(row.get("fix_admin") if admin and row.get("fix_admin") else row["fix"], fill),
        "keep_off": bool(row.get("keep_off")),
    }
    if admin and row.get("summary_admin"):
        out["summary"] = row["summary_admin"]
    if admin and row.get("admin_note"):
        out["owner_note"] = f"Please add the API key for the server at {fill.get('address')}. In Azizam, open Engines, then the engine, then API key."
    if row.get("action"):
        action = dict(row["action"])
        action["label"] = _fill(action["label"], fill)
        if action["id"] == "openexisting":
            action["service_id"] = fill.get("existing_id")
        out["action"] = action
    if answer is not None:
        out["details"] = {
            "raw": _raw(answer),
            "raw_label": row.get("raw_label") or "What the server said",
            "request": _curl(answer, key_sent=key_sent, timeout=answer.transport == "timeout",
                             timeout_s=float(fill.get("timeout", DEFAULT_TIMEOUT_S))),
            "with_key": key_sent,
        }
    return out


def _models_from(answer: Answer, probe: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """``(shape, entries)``: shape is ``list`` when the answer holds a model list.

    A server may list its models under more than one key (``data`` and
    ``models``); entries with the same id are merged so no field is lost.
    """
    data = answer.json
    if probe.get("require_json_key") and not (isinstance(data, Mapping) and isinstance(data.get(probe["require_json_key"]), list)):
        return "not_list", []
    if not isinstance(data, Mapping) or not any(isinstance(data.get(k), list) for k in probe.get("list_keys") or ("data",)):
        return "not_list", []
    merged: dict[str, dict[str, Any]] = {}
    for key in probe.get("list_keys") or ("data",):
        for item in data.get(key) or () if isinstance(data.get(key), list) else ():
            if not isinstance(item, Mapping):
                continue
            ident = next((item.get(k) for k in probe.get("id_keys") or ("id",) if isinstance(item.get(k), str) and item.get(k)), None)
            if ident:
                merged.setdefault(ident, {}).update({k: v for k, v in item.items() if k not in merged.get(ident, {})})
    return "list", [{"id": ident, "entry": entry} for ident, entry in merged.items()]


def public_name(model_id: str, rules: Iterable[Mapping[str, Any]]) -> str:
    name = model_id
    for rule in rules:
        if "strip" in rule:
            stripped = re.sub(rule["strip"], "", name, flags=re.IGNORECASE)
            name = stripped or name
        elif "replace" in rule:
            name = re.sub(rule["replace"], rule.get("with", ""), name)
        elif rule.get("lower"):
            name = name.lower()
    return name.strip("-_. ") or model_id


def _unique(name: str, taken: set[str]) -> str:
    candidate, n = name, 2
    while candidate in taken:
        candidate, n = f"{name}-{n}", n + 1
    taken.add(candidate)
    return candidate


def suggested_model_kind(capabilities: Iterable[str], catalog: EngineCatalog) -> str | None:
    """The one kind a probed model's capabilities name, or None when they name none or more than one.

    The engine's own words for its work (its record capabilities) map to the
    gateway's kinds through offered_model_kinds, so no engine name is needed.
    """
    kinds = offered_model_kinds(word for cap_id in capabilities for word in catalog.capability(cap_id)["record_capabilities"])
    return kinds[0] if len(kinds) == 1 else None


def _model_caps(entry: Mapping[str, Any], rules: Iterable[Mapping[str, Any]]) -> list[str]:
    found: list[str] = []
    for rule in rules:
        value = _at(entry, rule["field"])
        hit = False
        if "equals" in rule:
            hit = isinstance(value, str) and value.lower() == rule["equals"]
        elif "contains" in rule:
            hit = isinstance(value, list) and rule["contains"] in [str(v).lower() for v in value]
        elif "matches" in rule:
            hit = isinstance(value, str) and re.search(rule["matches"], value, re.IGNORECASE) is not None
        if hit and rule["capability"] not in found:
            found.append(rule["capability"])
    return found


def _context_window(entry: Mapping[str, Any], keys: Iterable[str]) -> int | None:
    for key in keys:
        value = _at(entry, key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _model_row(catalog: EngineCatalog, model_id: str, model_caps: list[str], taken: set[str], window: int | None) -> dict[str, Any]:
    """One model of the check's list, with the work the server's own words say it does."""
    data = catalog.data
    row = {
        "id": model_id,
        "public_name": _unique(public_name(model_id, data.get("public_name_rules") or ()), taken),
        "context_window": window,
        "capabilities": model_caps,
        "use": ", ".join(catalog.capability(c)["label"] for c in model_caps if c in data["capabilities"]) or "Not known yet",
    }
    # The kind is suggested only when the server's answer names exactly one; otherwise the row is as before.
    kind = suggested_model_kind(model_caps, catalog)
    if kind:
        row["kind"] = kind
    return row


# The record keys a check's list probes fill (``result["voices"]``, ``result["default_voice"]``). A write that carries a
# check's observations replaces exactly these on the record, so a later check never leaves an older answer behind.
OBSERVED_LIST_KEYS = ("voices", "default_voice")


async def _read_lists(ctx: _Ctx, probes: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Run the definition's list probes. For each ``offers`` kind, the first probe that finds ids wins.

    Two probes may read one URL (a server answers one JSON for two shapes); it is fetched once. An answer that fails,
    is not JSON or has no ids is skipped: a server without a list is not an error, the owner just sees no list. One
    exception: a list whose ``where`` drops every entry it read is the server's own answer that it has nothing for
    this work, so it wins with no ids and ``filtered_out`` set.
    """
    probes = list(probes)
    urls: dict[str, str] = {}
    for probe in probes:
        url = _route_url(ctx, probe["path"], probe["from"])
        if safe_probe_path(probe["path"]) is not None and _plain_route(url):
            urls[probe["id"]] = url
    # One fetch per URL, read up to the largest size any probe of that URL asks for.
    caps: dict[str, int] = {}
    for probe in probes:
        if probe["id"] in urls:
            caps[urls[probe["id"]]] = max(caps.get(urls[probe["id"]], 0), probe.get("cap_bytes", MODEL_LIST_CAP_BYTES))
    answers = dict(zip(dict.fromkeys(urls.values()), await asyncio.gather(
        *(_get(ctx, url, cap=caps[url]) for url in dict.fromkeys(urls.values())))))
    chosen: dict[str, dict[str, Any]] = {}
    for probe in probes:
        answer = answers.get(urls.get(probe["id"], ""))
        if answer is None or answer.status != 200:
            continue
        if probe["offers"] == "node":
            # An unreadable inventory is kept, so the check can say so; a later probe that reads it takes its place.
            if probe["offers"] in chosen and not chosen[probe["offers"]].get("unreadable"):
                continue
            node_found = _read_node_list(answer, probe)
            if node_found is not None:
                chosen[probe["offers"]] = node_found
            continue
        if probe["offers"] in chosen or answer.transport != "ok":
            continue
        found = read_list(answer.json, probe["list"])
        filtered_out = not found["ids"] and "where" in probe["list"] and bool(
            read_list(answer.json, {key: value for key, value in probe["list"].items() if key != "where"})["ids"])
        if found["ids"] or filtered_out:
            chosen[probe["offers"]] = {"id": probe["id"], "offers": probe["offers"], "path": probe["path"],
                                       "capability": probe.get("capability"), "filtered_out": filtered_out, **found}
    return list(chosen.values())


def _combo_choices(spec: Any) -> list[str]:
    """The names an input offers when its spec is a combo: ``[[names...], {...}]`` (the engine's own list inputs) or
    ``["COMBO", {"options": [...]}]`` (the form newer nodes use). Backslashes are read as folder marks, as on a Windows
    install, so a name reads the same on either side of a comparison."""
    if not isinstance(spec, list) or not spec:
        return []
    names = spec[0]
    if names == "COMBO":
        names = spec[1].get("options") if len(spec) > 1 and isinstance(spec[1], Mapping) else None
    if not isinstance(names, list):
        return []
    return [name.replace("\\", "/").strip() for name in names if isinstance(name, str) and name.strip()]


def node_list_inventory(body: Any) -> tuple[list[str], list[str]] | None:
    """``(node classes, every name a combo input offers)`` for an engine's node list, else ``None``.

    A node list is an object that holds at least one entry, each entry an object with an ``input`` key. Any other
    answer (an error body, a page, a list) is not a node list: it is unreadable, not a list of zero nodes.
    """
    if not isinstance(body, Mapping) or not body:
        return None
    names: dict[str, None] = {}
    for spec in body.values():
        if not isinstance(spec, Mapping) or "input" not in spec:
            return None
        groups = spec["input"]
        for group in groups.values() if isinstance(groups, Mapping) else ():
            for entry in group.values() if isinstance(group, Mapping) else ():
                names.update(dict.fromkeys(_combo_choices(entry)))
    return [str(key) for key in body], list(names)


NODE_LIST_UNREADABLE = {
    "too_large": "The engine's node list was too large to read",
    "not_json": "The engine's node list was not in a form Azizam can read",
    "not_node_list": "The engine's answer was not a node list",
    "cut_short": "The engine's node list was longer than Azizam reads",
}


def _read_node_list(answer: Answer, probe: Mapping[str, Any]) -> dict[str, Any] | None:
    """The entry of a node probe: the inventory when the list reads, else the reason it does not (``unreadable``)."""
    base = {"id": probe["id"], "offers": probe["offers"], "path": probe["path"], "capability": probe.get("capability"),
            "filtered_out": False, "ids": [], "default": None, "truncated": False}
    if answer.transport == "too_large":
        return {**base, "unreadable": "too_large"}
    if answer.transport != "ok":
        return None
    body = answer.json
    if body is None:
        return {**base, "unreadable": "not_json"}
    inventory = node_list_inventory(body)
    if inventory is None:
        return {**base, "unreadable": "not_node_list"}
    found = read_list(body, probe["list"])
    if found["truncated"]:
        # A cut inventory would call a workflow's node missing when it is only past the cut, so none is offered.
        return {**base, "unreadable": "cut_short"}
    return {**base, **found, "node_classes": inventory[0], "model_names": inventory[1]}


def _node_list_finding(reason: str) -> dict[str, Any]:
    """The finding for a node list that could not be read, in the words of the check's other findings."""
    return {
        "code": "node_list_unreadable", "severity": "warning", "field": "address", "keep_off": False,
        "title": "Workflows not listed",
        "what": f"{NODE_LIST_UNREADABLE[reason]}, so Azizam cannot say which workflows it can run.",
        "why": "Azizam judges each workflow by the nodes and model files the engine lists. Without the list, it shows none as ready.",
        "fix": "The engine can still be used. Check that its node list answers at its address, then check again.",
    }


def _binding_of(definition: Mapping[str, Any]) -> str:
    """The key under a workflow manifest's ``bindings`` that holds this kind of engine's graph: its protocol's own data."""
    from gateway.engine_protocol import description

    doc = description.load_registry().get(definition["record"]["protocol"])
    return str(getattr(doc, "binding", "") or "")


def workflow_offerings(manifests: Iterable[Mapping[str, Any]], binding: str, node_classes: Iterable[str],
                       model_names: Iterable[str] = ()) -> dict[str, Any]:
    """Which of these workflows an engine with these node classes and combo names can run, and what each other one lacks.

    ``manifests`` are the workflow manifests the release ships; a workflow an owner added is not judged here. A
    workflow is a manifest that holds a ``bindings.<binding>`` block. Its ``requires`` are judged by
    ``gateway.workflow_requirements``: each custom node against ``node_classes``, each model file against
    ``model_names``, the names the engine's own combo inputs offer (``node_list_inventory``). A combo name is a path
    inside a model folder and the node list does not say which folder, so a name stands for the file in any role the
    workflow asks of it: the file name is what is compared. A model file with a required hash is never confirmed.
    ``{"nodes": count, "offered": [{"id", "name"}], "unavailable": [{"id", "name", "missing": [{"kind", "name", "reason"}]}]}``
    """
    from gateway.workflow_requirements import judge_by_names

    classes = sorted({name for name in node_classes if isinstance(name, str) and name})
    names = list(dict.fromkeys(name for name in model_names if isinstance(name, str) and name))
    offered: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for manifest in manifests:
        block = (manifest.get("bindings") or {}).get(binding) if isinstance(manifest.get("bindings"), Mapping) else None
        if not isinstance(block, Mapping):
            continue
        row = {"id": str(manifest.get("id") or ""), "name": str(manifest.get("display_name") or manifest.get("id") or "")}
        requires = block.get("requires") if isinstance(block.get("requires"), Mapping) else {}
        try:
            verdict = judge_by_names(requires, classes, names)
        except ValueError:
            unavailable.append({**row, "missing": [{"kind": "requirements", "name": "",
                                                    "reason": "the workflow's requirements cannot be read"}]})
            continue
        if verdict.ok:
            offered.append(row)
        else:
            unavailable.append({**row, "missing": [{"kind": issue.kind, "name": issue.name, "reason": _missing_reason(issue)}
                                                   for issue in verdict.missing]})
    return {"nodes": len(classes), "offered": offered, "unavailable": unavailable}


def _missing_reason(issue: Any) -> str:
    """A missing model file says which file the engine does not list; a node keeps the requirement check's words."""
    if issue.kind == "model" and not issue.needs_hash:
        return f"the engine's node list offers no model file {issue.name.partition(':')[2]}"
    return issue.reason


def _classify_route(answer: Answer, baseline: Answer, against_body: str) -> str:
    """One route's empty POST, read against the made-up path (spec 5a)."""
    if answer.transport != "ok" or answer.status is None:
        return "none_transport"
    status = answer.status
    body = answer.text
    if status == 415:
        return "possible"
    if status == 501 or re.search(against_body, body, re.IGNORECASE):
        return "against"
    if status in (401, 403) and baseline.status not in (401, 403):
        return "needs_key"
    if status == baseline.status and baseline.transport == "ok":
        return "none"
    if status in (404, 405, 410):
        return "none"
    if status >= 500:
        return "none_server_error"
    if 200 <= status < 300 or status in (400, 415, 422):
        return "possible"
    return "none"


def duplicate_finding(
    catalog: EngineCatalog, definition: Mapping[str, Any], values: Mapping[str, Any],
    existing: Iterable[Mapping[str, Any]], *, viewer_role: str = "owner",
) -> dict[str, Any] | None:
    """"Already added" when another engine has this address; ``None`` otherwise. Sends nothing."""
    address = normalize_address(values.get("address"))
    wanted = comparable_address(address)
    for engine in existing:
        if not engine.get("address"):
            continue
        if comparable_address(str(engine["address"])) != wanted:
            continue
        fill = {"address": address, "existing_name": engine.get("name") or engine.get("id"), "existing_id": engine.get("id")}
        row = next(r for r in catalog.advice_rows(definition) if r["code"] == "dup")
        return _finding(row, fill, viewer_role=viewer_role, answer=None, key_sent=False)
    return None


def _served_dispatch_address(address: str) -> bool:
    """True when protocol dispatch would serve this address: the rule a saved record is held to, one place."""
    from gateway.service_registry import validate_protocol_dispatch_local_only

    try:
        validate_protocol_dispatch_local_only({"config": {"connection": {"dispatch": "protocol", "address": address}}})
    except ValueError:
        return False
    return True


async def run_check(
    catalog: EngineCatalog,
    definition: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    key: KeyInput,
    fetch: Fetch,
    existing: Iterable[Mapping[str, Any]] = (),
    viewer_role: str = "owner",
    baseline_token: str | None = None,
    now: Callable[[], float] = time.time,
    workflows: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Run the ladder within the definition's optional whole-check deadline.

    Omitting check_s preserves the caller's existing deadline. Expiry raises
    TimeoutError after cancelling outstanding probes; caller cancellation propagates.
    """
    async with asyncio.timeout(definition.get("timeouts", {}).get("check_s")):
        return await _run_check(
            catalog, definition, values, key=key, fetch=fetch, existing=existing,
            viewer_role=viewer_role, baseline_token=baseline_token, now=now, workflows=workflows,
        )


async def _run_check(
    catalog: EngineCatalog,
    definition: Mapping[str, Any],
    values: Mapping[str, Any],
    *,
    key: KeyInput,
    fetch: Fetch,
    existing: Iterable[Mapping[str, Any]] = (),
    viewer_role: str = "owner",
    baseline_token: str | None = None,
    now: Callable[[], float] = time.time,
    workflows: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Run the ladder for ``definition`` against draft ``values``. Persists nothing.

    ``workflows`` are the workflow manifests a definition with a node inventory list is judged against.
    """
    data = catalog.data
    fields = {f["key"]: f for f in definition["fields"]}
    address = normalize_address(values.get("address"), placeholder=fields["address"].get("placeholder", ""))
    prefix = str(values.get("path_prefix") or "").strip()
    check_address = check_base(address, prefix)
    timeout_s = definition.get("timeouts", {}).get("probe_s", DEFAULT_TIMEOUT_S)
    fetch = guarded_fetch(fetch, key, viewer_role=viewer_role)
    ctx = _Ctx(
        catalog=catalog, definition=definition, values=values, address=check_address, origin=origin_of(address),
        key=key, headers=_auth_headers(key.value, values), fetch=fetch, timeout_s=timeout_s,
        token=baseline_token or _baseline_token(), viewer_role=viewer_role,
    )
    key_sent = bool(key.value)
    method, url = _primary_url(ctx)
    host = urlsplit(address).hostname or ""
    fill: dict[str, Any] = {
        "host": f"{host}:{_port_of(urlsplit(address))}" if urlsplit(address).port else host,
        "address": address, "url": url, "route": urlsplit(url).path or "/",
        "timeout": f"{timeout_s:g}", "timeout_words": "1 second" if timeout_s == 1 else f"{timeout_s:g} seconds",
    }
    result: dict[str, Any] = {
        "definition": definition["definition"],
        "definition_version": definition["definition_version"],
        "address": address,
        "calls": f"{method} {url}",
        "checked_at": int(now()),
        "key_sent": key_sent,
        "key_withheld": key.withheld,
        "key_source": key.source if key_sent else "none",
        "reachable": False,
        "auth": "unknown",
        "findings": [],
        "capabilities": [],
        "models": [],
        "shapes": [],
        "suggested_name": "",
        "name_source": "",
        "steps": [],
        "readiness": None,
    }
    advice = catalog.advice_rows(definition)
    result["suggested_name"] = _fill(definition["name_fallback"], {"host": fill["host"], "host_label": host_label(address)})
    result["name_source"] = "address"

    def finish(outcome: str) -> dict[str, Any]:
        result["outcome"] = outcome
        return result

    # Duplicate address: cheap, before any request.
    dup = duplicate_finding(catalog, definition, values, existing, viewer_role=viewer_role)
    if dup is not None:
        result["findings"].append(dup)
        return finish("error")
    # The owner may have chosen another computer on their network: that address was checked, and its network computed,
    # before this check began. Otherwise a definition that has the gateway call the engine through an installed
    # description serves only this computer's addresses. Say so here, before any request and before a save, not when
    # the first generation fails.
    plan = values.get("remote_plan")
    if plan:
        result["remote"] = dict(plan)  # shown so the page can ask for the owner's yes
    elif definition["record"].get("dispatch") and not _served_dispatch_address(address):
        row = next(r for r in advice if r["code"] == "local_only")
        result["findings"].append(_finding(row, fill, viewer_role=viewer_role, answer=None, key_sent=False))
        return finish("error")

    baseline_path = f"/{_BASELINE_PREFIX}{ctx.token}"
    base_for_baseline = ctx.address if definition["probes"]["primary"]["from"] == "address" else ctx.origin
    primary_task = _get(ctx, url, method, cap=MODEL_LIST_CAP_BYTES if definition["probes"]["primary"]["kind"] == "model_list" else DEFAULT_CAP_BYTES)
    get_base_task = _get(ctx, join_url(base_for_baseline, baseline_path))
    post_base_task = _get(ctx, join_url(base_for_baseline, baseline_path), "POST")
    primary, get_base, post_base = await asyncio.gather(primary_task, get_base_task, post_base_task)
    baseline = post_base if method == "POST" else get_base
    probe = definition["probes"]["primary"]

    obs: dict[str, Any] = {
        "probe": probe["id"], "transport": primary.transport, "status": primary.status, "key_sent": key_sent,
        "address": address, "body": primary.text[:4000], "duplicate": False,
    }
    fill.update(status_text=primary.status_text, baseline_text=baseline.status_text or "no answer", location=primary.location, reason=primary.detail)
    if primary.transport == "too_large" and probe["kind"] != "model_list":
        primary.transport = "ok"
    obs["transport"] = primary.transport
    result["steps"].append({"label": definition["check_rows"][0], "result": fill["host"] if primary.transport == "ok" else "No answer"})

    if primary.transport != "ok":
        row = next((r for r in advice if r["severity"] == "error" and _matches(r["when"], obs)), None)
        if row is None:
            row = next(r for r in advice if r["code"] == "down")
        result["findings"].append(_finding(row, fill, viewer_role=viewer_role, answer=primary, key_sent=key_sent))
        return finish("error")

    result["reachable"] = True
    if primary.status in (401, 403):
        everywhere = baseline.status in (401, 403)
        result["auth"] = "rejected" if key_sent else "needed"
        result["steps"].append({"label": definition["check_rows"][1], "result": "Refused" if key_sent else "Key needed"})
        row = next((r for r in advice if _matches(r["when"], obs)), None)
        if row is not None:
            result["findings"].append(_finding(row, fill, viewer_role=viewer_role, answer=primary, key_sent=key_sent, everywhere=everywhere))
        for cap_id in definition["capabilities"]:
            cap = catalog.capability(cap_id)
            result["capabilities"].append({"id": cap_id, "label": cap["label"], "level": "needs_key",
                                           "evidence": "Not known yet. The server needs a key first.", "ticked": False})
        return finish("error")
    result["auth"] = "accepted" if key_sent else "not_needed"
    result["steps"].append({"label": definition["check_rows"][1], "result": "Accepted" if key_sent else "No key needed"})
    if primary.status == 429:
        row = next(r for r in advice if r["code"] == "rate_limited")
        result["findings"].append(_finding(row, fill, viewer_role=viewer_role, answer=primary, key_sent=key_sent))
        return finish("error")

    caps: dict[str, dict[str, Any]] = {}
    models: list[dict[str, Any]] = []
    shape = None
    evidence_text = data["evidence_text"]

    if probe["kind"] == "model_list" and primary.status == 200:
        shape, entries = _models_from(primary, probe)
        obs["shape"] = shape
        taken: set[str] = set()
        for item in entries:
            models.append(_model_row(catalog, item["id"], _model_caps(item["entry"], data["model_rules"]), taken,
                                     _context_window(item["entry"], data.get("context_window_keys") or ())))
        result["shapes"].append(probe["id"])
    elif probe["kind"] == "model_list" and primary.status is not None and primary.status != 200:
        obs["shape"] = "none"
    result["steps"].append({"label": definition["check_rows"][2], "result": f"{len(models)} found" if probe["kind"] == "model_list" else ""})
    # The primary read is the definition's readiness read (``gateway.engine_readiness``): the same method, URL, headers and
    # credential the request path sends once the engine is saved, so its answer is the engine's readiness answer.
    readiness_path = urlsplit(url).path or "/"
    result["readiness"] = {"method": method, "path": readiness_path, "status": primary.status, "ok": primary.status == 200}
    result["steps"].append({"label": "Readiness read", "result": f"{method} {readiness_path}: " + ("answered" if primary.status == 200 else f"{primary.status_text}")})

    # Metadata reads, all at once.
    rows = [catalog.metadata_row(row_id) for row_id in definition["probes"]["metadata"]]
    meta_answers, lists_found = await asyncio.gather(
        asyncio.gather(*(
            _get(ctx, _route_url(ctx, row["path"], row["from"]), cap=row.get("cap_bytes", DEFAULT_CAP_BYTES)) for row in rows
        )),
        _read_lists(ctx, definition["probes"].get("lists") or ()),
    )
    listed: dict[str, tuple[str, str]] = {}  # capability -> (document, path)
    documented: dict[str, list[str]] = {}  # capability -> model ids from a capability document
    name_candidates: list[str] = []
    for row, answer in zip(rows, meta_answers):
        body = answer.json if answer.transport == "ok" and answer.status == 200 else None
        if body is None:
            continue
        if row.get("require_json_key") and not (isinstance(body, Mapping) and row["require_json_key"] in body):
            continue
        result["shapes"].append(row["id"])
        if row["kind"] == "openapi_paths" and isinstance(body.get("paths") if isinstance(body, Mapping) else None, Mapping):
            for path in body["paths"]:
                for suffix, cap_id in row["paths"].items():
                    if (path.endswith(suffix) or (suffix == "/realtime" and "/realtime" in path)) and cap_id not in listed:
                        listed[cap_id] = (row["document"], path)
        if row["kind"] == "capability_document":
            for model_id, values_found in _capability_entries(body):
                for value in values_found:
                    cap_id = row["values"].get(value.lower())
                    if cap_id:
                        documented.setdefault(cap_id, []).append(model_id)
        name_from = row.get("name_from")
        if name_from:
            candidate = _at(body, name_from["json"])
            if isinstance(candidate, str) and candidate.strip() and candidate.strip() not in name_from.get("ignore", ()):
                if name_from.get("extract"):
                    match = re.search(name_from["extract"], candidate)
                    candidate = match.group(1) if match else ""
                if candidate:
                    name_candidates.append(candidate.strip() + name_from.get("suffix", ""))

    # Empty POSTs to the standard routes, against the made-up path.
    route_levels: dict[str, tuple[str, Answer]] = {}
    if definition["probes"]["route_probes"]:
        route_base = join_url(ctx.origin, definition["probes"]["route_base"]) if definition["probes"].get("route_base") else ctx.address
        if definition["probes"].get("route_base"):
            route_baseline = await _get(ctx, join_url(route_base, baseline_path), "POST")
        else:
            route_baseline = post_base
        targets = [(cap_id, catalog.capability(cap_id)) for cap_id in definition["capabilities"]]
        targets = [(cap_id, cap) for cap_id, cap in targets if cap.get("route")]
        answers = await asyncio.gather(*(_get(ctx, join_url(route_base, cap["route"]["path"]), "POST") for _, cap in targets))
        for (cap_id, _), answer in zip(targets, answers):
            route_levels[cap_id] = (_classify_route(answer, route_baseline, data["against_body"]), answer)

    listed_by_list: dict[str, tuple[str, int]] = {}  # capability -> (document, entries)
    own_list: set[str] = set()  # capabilities a definition list speaks for
    listed_none: set[str] = set()  # capabilities whose own list kept no entry
    for found in lists_found:
        result["shapes"].append(found["id"])
        if found["capability"]:
            own_list.add(found["capability"])
            if found["ids"]:
                listed_by_list.setdefault(found["capability"], (f"{found['offers']} list", len(found["ids"])))
            elif found["filtered_out"]:
                listed_none.add(found["capability"])
        if found["offers"] == "model":
            # The server's own model list for this work replaces the general one: every row is for that work.
            taken = set()
            models = [_model_row(catalog, row["id"], [found["capability"]] if found["capability"] else [], taken, None)
                      for row in found["ids"]]
        elif found["offers"] == "node":
            if found.get("unreadable"):
                result["findings"].append(_node_list_finding(found["unreadable"]))
            else:
                result["workflows"] = workflow_offerings(workflows, _binding_of(definition), found["node_classes"],
                                                         found["model_names"])
        else:
            result["voices"] = found["ids"]
            result["default_voice"] = found["default"]

    by_model_rule: dict[str, list[str]] = {}
    for model in models:
        for cap_id in model["capabilities"]:
            by_model_rule.setdefault(cap_id, []).append(model["id"])

    for cap_id in definition["capabilities"]:
        cap = catalog.capability(cap_id)
        noun = cap["noun"]
        route_level, answer = route_levels.get(cap_id, ("none_transport", None))
        level, evidence, why = "none", "", ""
        if route_level == "against" and answer is not None:
            level, evidence, why = "against", _fill(evidence_text["against"], {"noun": noun, "excerpt": _error_message(answer)}), evidence_text["against_why"]
        elif route_level == "needs_key" and answer is not None:
            level, evidence, why = "needs_key", _fill(evidence_text["needs_key"], {"status_text": answer.status_text}), evidence_text.get("needs_key_why", "")
        elif cap_id in listed_none:
            evidence, why = _fill(evidence_text.get("none_listed", evidence_text["none_why"]), {"noun": noun}), evidence_text["none_why"]
        elif route_level == "none" and answer is not None and cap_id in own_list:
            # A list that names no task keeps every model; the route that answered like a missing path decides.
            evidence, why = _fill(evidence_text["none"], {"status_text": answer.status_text}), evidence_text["none_why"]
        elif cap_id in by_model_rule:
            names = by_model_rule[cap_id]
            template = evidence_text["likely_model_field_many"] if len(names) > 1 else evidence_text["likely_model_field"]
            level, evidence = "likely", _fill(template, {"model": names[0], "others": len(names) - 1, "noun": noun})
        elif cap_id in documented:
            names = documented[cap_id]
            template = evidence_text["likely_model_field_many"] if len(names) > 1 else evidence_text["likely_model_field"]
            level, evidence = "likely", _fill(template, {"model": names[0], "others": len(names) - 1, "noun": noun})
        elif cap_id in listed_by_list:
            document, count = listed_by_list[cap_id]
            level, evidence = "likely", _fill(evidence_text["likely_list"], {"document": document, "count": count})
        elif cap_id in listed and route_level in ("possible", "none_server_error"):
            document, path = listed[cap_id]
            level, evidence = "likely", _fill(evidence_text["likely_metadata"], {"document": document, "path": path})
        elif cap_id in listed and cap_id not in route_levels:
            document, path = listed[cap_id]
            level, evidence = "likely", _fill(evidence_text["likely_metadata_only"], {"document": document, "path": path})
        elif route_level == "possible" and answer is not None and definition["probes"].get("route_proves"):
            level = "likely"
            evidence = _fill(evidence_text["likely_route"], {"noun": noun, "status_text": _error_message(answer) if answer.status and answer.status >= 400 else answer.status_text})
        elif route_level == "possible" and answer is not None:
            level = "possible"
            evidence = _fill(evidence_text["possible"], {"noun": noun, "status_text": _error_message(answer) if answer.status and answer.status >= 400 else answer.status_text})
            why = _fill(evidence_text["possible_why"], {"status_short": answer.status_text})
        elif route_level == "none_server_error" and answer is not None:
            evidence, why = _fill(evidence_text["none_server_error"], {"status_text": answer.status_text}), evidence_text["none_why"]
        elif route_level == "none" and answer is not None:
            evidence, why = _fill(evidence_text["none"], {"status_text": answer.status_text}), evidence_text["none_why"]
        elif not cap.get("route"):
            evidence, why = evidence_text.get("none_unlisted", evidence_text["none_why"]), evidence_text["none_why"]
        else:
            evidence, why = evidence_text.get("none_transport", "The route did not answer."), evidence_text["none_why"]
        row: dict[str, Any] = {"id": cap_id, "label": cap["label"], "level": level, "evidence": evidence,
                               "ticked": level == "likely"}
        if why:
            row["why"] = why
        test_id = real_test_id(catalog, definition, cap_id)
        if test_id:
            row["real_test"] = test_id
        if cap.get("route"):
            route_base = join_url(ctx.origin, definition["probes"]["route_base"]) if definition["probes"].get("route_base") else ctx.address
            row["sent_to"] = f"{cap['route']['method']} {join_url(route_base, cap['route']['path'])}"
        caps[cap_id] = row

    order = {"likely": 0, "possible": 1, "needs_key": 2, "against": 3, "none": 4}
    result["capabilities"] = sorted(caps.values(), key=lambda row: order.get(row["level"], 5))
    for model in models:
        model.pop("capabilities", None)
    result["models"] = models
    # A server whose own list for this work came back empty still answered as this kind of server.
    recognised = bool(models) or bool(listed_none) or any(row["level"] in ("likely", "possible") for row in caps.values())
    if not recognised:
        for row in result["capabilities"]:
            if row["level"] in ("none",):
                row["evidence"] = evidence_text.get("none_nothing_recognised", row["evidence"])
    obs.update(recognised=recognised, models_count=len(models) if shape == "list" else None)
    result["steps"].append({"label": definition["check_rows"][3], "result": ""})

    # The first row that applies decides: an error stops here, a warning blocks once.
    for row in advice:
        if row["code"] == "dup" or not _matches(row["when"], obs):
            continue
        result["findings"].append(_finding(row, fill, viewer_role=viewer_role, answer=primary, key_sent=key_sent))
        if row["severity"] == "error":
            return finish("error")
        break

    if name_candidates:
        result["suggested_name"], result["name_source"] = name_candidates[0], "server"
    return finish("warning" if result["findings"] else "ok")


def _capability_entries(body: Any) -> list[tuple[str, list[str]]]:
    """``(model id, capability words)`` pairs from a capability document of any nesting."""
    out: list[tuple[str, list[str]]] = []

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            words = value.get("capabilities") or value.get("usecases") or value.get("use_cases")
            ident = value.get("id") or value.get("name") or value.get("model")
            if isinstance(words, list) and isinstance(ident, str):
                out.append((ident, [str(w) for w in words if isinstance(w, str)]))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(body)
    return out


def _number(value: Any, default: float, *, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


# ---------------------------------------------------------------------------
# The real test
# ---------------------------------------------------------------------------


def _substitute(value: Any, slots: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        match = re.fullmatch(r"\{([a-z_]+)\}", value)
        if match:
            return slots.get(match.group(1))
        return value
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            filled = _substitute(item, slots)
            if filled is not None and filled != "":
                out[key] = filled
        return out
    if isinstance(value, list):
        return [_substitute(item, slots) for item in value]
    return value


def _tone_wav() -> bytes:
    """Half a second of a quiet 440 Hz tone, 16 kHz mono: a clip that is not speech, made here and never recorded."""
    rate = 16000
    frames = b"".join(struct.pack("<h", int(2000 * math.sin(2 * math.pi * 440 * n / rate))) for n in range(rate // 2))
    out = io.BytesIO()
    with wave.open(out, "wb") as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(rate)
        clip.writeframes(frames)
    return out.getvalue()


# One generator for each clip a definition may name (gateway.engine_definitions.UPLOAD_SAMPLES).
SAMPLE_CLIPS: dict[str, Callable[[], bytes]] = {"tone_wav": _tone_wav}
assert tuple(SAMPLE_CLIPS) == UPLOAD_SAMPLES, "every upload sample needs one generator"


def _multipart(fields: Mapping[str, Any], upload: Mapping[str, Any], clip: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body of text fields and one file, and its content type with the boundary."""
    boundary = "azizam-" + secrets.token_hex(12)
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{upload["field"]}"; filename="{upload["filename"]}"\r\n'
                  f'Content-Type: {upload["content_type"]}\r\n\r\n').encode() + clip + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


async def run_real_test(
    catalog: EngineCatalog,
    definition: Mapping[str, Any],
    values: Mapping[str, Any],
    capability_id: str,
    *,
    key: KeyInput,
    fetch: Fetch,
    model: str | None = None,
    voice: str | None = None,
    viewer_role: str = "owner",
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """One tiny real request of one kind, from the gateway's computer, to the draft address.

    ``model`` and ``voice`` fill the slots the test body names; an empty slot is left out of the request.
    """
    fetch = guarded_fetch(fetch, key, viewer_role=viewer_role)
    if capability_id not in definition["capabilities"]:
        raise DraftError("This kind of server does not offer that.", field="capability")
    address = normalize_address(values.get("address"))
    cap = catalog.capability(capability_id)
    test_id = real_test_id(catalog, definition, capability_id)
    if test_id is None:
        raise DraftError("This kind of server has no real test for that.", field="capability")
    test = catalog.data["real_tests"][test_id]
    if test.get("kind") == "get_probe":
        return await _run_get_probe(definition, values, capability_id, cap, test, address=address, key=key, fetch=fetch, now=now)
    headers = _auth_headers(key.value, values)
    prefix = str(values.get("path_prefix") or "").strip()
    base = check_base(address, prefix)
    if definition["probes"].get("route_base"):
        route_base = join_url(origin_of(address), definition["probes"]["route_base"])
        url = join_url(join_url(route_base, prefix), cap["route"]["path"])
    else:
        url = join_url(base, cap["route"]["path"])
    if not _plain_route(url):
        raise DraftError("This kind of server declares a route that cannot be tested safely.", field="capability")
    slots = {"model": model or "", "voice": voice or ""}
    body = _substitute(test.get("body") or {}, slots)
    # The request adjustments the engine will run with: the draft's, or the kind's default for a new engine.
    specs = values.get("transforms")
    if specs is None:
        specs = list(definition["record"].get("transforms") or [])
    base_result: dict[str, Any] = {
        "capability": capability_id, "label": cap["label"], "sent_to": f"POST {url}", "cost": test["cost"],
        "at": int(now()), "via": "draft",
    }
    if specs:
        try:
            body = transforms.run_request_pipeline(
                list(specs), body,
                transforms.TransformContext({"request_path": "/v1" + cap["route"]["path"], "reasoning": {}}),
                transforms.REGISTRY,
            )
        except (KeyError, ValueError, TypeError) as exc:
            name = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            what = f"Azizam could not apply the request adjustment “{name}”, so it sent nothing."
            return {**base_result, "sent": test["sent"], "adjusted": False, "ok": False, "status": None, "seconds": 0.0,
                    "reply": what, "cost": "None. Nothing was sent.",
                    "finding": {"what": what,
                                "why": "The engine runs every request through its request adjustments; this one is not known here or refused the request.",
                                "fix": "Check the adjustment under More options, then test again.", "raw": str(exc)}}
    shape = "a small JSON request in the OpenAI shape" + (", with your request adjustments applied" if specs else "")
    if test.get("kind") == "multipart_post":
        upload = test["file"]
        content, headers["content-type"] = _multipart(body, upload, SAMPLE_CLIPS[upload["sample"]]())
        shape = "a short generated audio clip in an OpenAI-style upload form, and read the JSON that came back"
    else:
        content = json.dumps(body).encode()
        headers["content-type"] = "application/json"
        if test.get("kind") == "binary_post":
            shape = "a short JSON request in the OpenAI shape, and read the bytes that came back"
    # The definition caps the draft's reply time; absent data keeps the two-minute cap.
    test_timeout_s = definition.get("timeouts", {}).get("real_test_s", TEST_TIMEOUT_S)
    timeout_s = min(_number(values.get("reply_timeout_s"), test_timeout_s, low=1, high=600), test_timeout_s)
    answer = await fetch("POST", url, headers=headers, cap_bytes=TEST_CAP_BYTES, timeout_s=timeout_s, content=content)
    result: dict[str, Any] = {
        **base_result, "seconds": answer.seconds, "status": answer.status, "adjusted": bool(specs),
        "sent": test["sent"] + (", with your request adjustments applied" if specs else ""),
        "request": {"method": "POST", "url": url,
                    "headers": {name: "Bearer [redacted]" if name.lower() == "authorization" else value
                                for name, value in headers.items()},
                    "body": body, "timeout_s": timeout_s,
                    **({"file": {"field": test["file"]["field"], "filename": test["file"]["filename"],
                                 "content_type": test["file"]["content_type"], "form_bytes": len(content)}} if test.get("kind") == "multipart_post" else {})},
    }
    ok, reply = _judge(test, answer)
    if answer.withheld:
        reply = (f"The server answered “{answer.status_text}”. " if answer.status else "") + WITHHELD_TEXT
    result["ok"] = ok
    result["reply"] = reply
    if not ok:
        result["cost"] = "None. The server refused it." if answer.status else "None. Nothing answered."
        if answer.transport != "ok":
            what = f"Azizam could not complete the request: {answer.detail or answer.transport}."
        else:
            what = f"The server answered “{answer.status_text}” at {result['sent_to']}."
        result["finding"] = {
            "what": what,
            "why": f"Azizam sent {shape}. The server may want a different request shape, or it cannot do this." if answer.transport == "ok"
                   else "The server did not answer in time, or the connection broke.",
            "fix": "Check what the server expects with whoever set it up. Its own text is under Details." if answer.transport == "ok"
                   else "Check that the server is running, then test again. A server that loads a model first can need a longer reply time.",
            "raw": _raw(answer),
        }
    return result


async def _run_get_probe(definition: Mapping[str, Any], values: Mapping[str, Any], capability_id: str,
                         cap: Mapping[str, Any], test: Mapping[str, Any], *, address: str, key: "KeyInput",
                         fetch: Fetch, now: Callable[[], float]) -> dict[str, Any]:
    """A real test that only reads: one GET of the test's status path. It sends no body and queues no work."""
    url = join_url(origin_of(address), test["path"])
    if safe_probe_path(test["path"]) is None or not _plain_route(url):
        raise DraftError("This kind of server declares a route that cannot be tested safely.", field="capability")
    headers = _auth_headers(key.value, values)
    test_timeout_s = definition.get("timeouts", {}).get("real_test_s", TEST_TIMEOUT_S)
    timeout_s = min(_number(values.get("reply_timeout_s"), test_timeout_s, low=1, high=600), test_timeout_s)
    answer = await fetch("GET", url, headers=headers, cap_bytes=DEFAULT_CAP_BYTES, timeout_s=timeout_s, content=None)
    ok, reply = _judge_get_probe(test["expect"], answer)
    if answer.withheld:
        reply = (f"The server answered “{answer.status_text}”. " if answer.status else "") + WITHHELD_TEXT
    result: dict[str, Any] = {
        "capability": capability_id, "label": cap["label"], "sent_to": f"GET {url}", "cost": test["cost"],
        "at": int(now()), "via": "draft", "seconds": answer.seconds, "status": answer.status, "adjusted": False,
        "sent": test["sent"],
        "request": {"method": "GET", "url": url,
                    "headers": {name: "Bearer [redacted]" if name.lower() == "authorization" else value
                                for name, value in headers.items()},
                    "body": {}, "timeout_s": timeout_s},
        "ok": ok, "reply": reply,
    }
    if not ok:
        what = (f"Azizam could not complete the request: {answer.detail or answer.transport}." if answer.transport != "ok"
                else f"The server answered “{answer.status_text}” at {result['sent_to']}.")
        result["finding"] = {
            "what": what,
            "why": "The engine did not answer its status page as this kind of engine does." if answer.transport == "ok"
                   else "The server did not answer in time, or the connection broke.",
            "fix": "Check that the engine is running, then test again.",
            "raw": _raw(answer),
        }
    return result


def _judge_get_probe(expect: Mapping[str, Any], answer: Answer) -> tuple[bool, str]:
    """A read passes on the status and, when the test names one, a key in the JSON reply."""
    if answer.transport != "ok" or answer.status is None:
        return False, answer.detail or "No answer"
    if answer.status != expect["status"]:
        if answer.status // 100 != 2:
            return False, f"“{_error_message(answer)}” (HTTP {answer.status})"
        return False, f"The server answered HTTP {answer.status}; Azizam expected HTTP {expect['status']}."
    wanted = expect.get("json_key")
    if wanted and _at(answer.json, wanted) is None:
        return False, f"An answer without “{wanted}”: {_excerpt(answer.text, 120)}"
    return True, "The engine answered"


def _judge_binary(expect: Mapping[str, Any], answer: Answer) -> tuple[bool, str]:
    """A binary reply passes on the status, the content type prefix and the number of bytes the test expects."""
    if answer.status != expect["status"]:
        if answer.status // 100 != 2:
            return False, f"“{_error_message(answer)}” (HTTP {answer.status})"
        return False, f"The server answered HTTP {answer.status}; Azizam expected HTTP {expect['status']}."
    content_type = str(answer.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not content_type.startswith(expect["content_type_prefix"].lower()):
        return False, (f"The server answered with {content_type or 'no content type'}, not {expect['content_type_prefix']}*. "
                       f"Its own words: {_excerpt(answer.text, 120)}")
    if len(answer.body) < expect["min_bytes"]:
        return False, f"The answer was only {len(answer.body)} bytes; at least {expect['min_bytes']} are expected."
    return True, f"{content_type}, {len(answer.body)} bytes"


def _judge_multipart(expect: Mapping[str, Any], answer: Answer) -> tuple[bool, str]:
    """A form upload passes on the status and a JSON string at the expected key. An empty string counts: the clip is no speech."""
    if answer.status != expect["status"]:
        if answer.status // 100 != 2:
            return False, f"“{_error_message(answer)}” (HTTP {answer.status})"
        return False, f"The server answered HTTP {answer.status}; Azizam expected HTTP {expect['status']}."
    value = _at(answer.json, expect["json_string"])
    if not isinstance(value, str):
        return False, f"An answer without “{expect['json_string']}” as text: {_excerpt(answer.text, 120)}"
    return True, f"“{_excerpt(value, 120)}”" if value else "An empty transcript, as expected for a clip that is not speech"


def _judge(test: Mapping[str, Any], answer: Answer) -> tuple[bool, str]:
    if answer.transport != "ok" or answer.status is None:
        return False, answer.detail or "No answer"
    if test.get("kind") == "binary_post":
        return _judge_binary(test["expect"], answer)
    if test.get("kind") == "multipart_post":
        return _judge_multipart(test["expect"], answer)
    if answer.status // 100 != 2:
        return False, f"“{_error_message(answer)}” (HTTP {answer.status})"
    if test.get("pass_json") and _at(answer.json, test["pass_json"]) in (None, [], ""):
        return False, f"An answer without “{test['pass_json']}”: {_excerpt(answer.text, 120)}"
    if test.get("reply_json"):
        value = _at(answer.json, test["reply_json"])
        return True, f"“{_excerpt(str(value), 120)}”" if value is not None else _excerpt(answer.text, 120)
    if test.get("reply_count_json"):
        value = _at(answer.json, test["reply_count_json"])
        return True, _fill(test.get("reply_count_text", "{count} items"), {"count": len(value) if isinstance(value, list) else "?"})
    return True, "An answer"

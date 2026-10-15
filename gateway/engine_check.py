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
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol
from urllib.parse import urlsplit

import httpcore
import httpx

from gateway.engine_definitions import EngineCatalog

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


def _checked_transport(destination: Destination) -> httpx.AsyncHTTPTransport:
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
                async with httpx.AsyncClient(
                    transport=_checked_transport(destination), follow_redirects=False, trust_env=False,
                    timeout=httpx.Timeout(timeout_s),
                ) as client, client.stream(method, url, headers=sent, content=content) as response:
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
    headers: dict[str, str] = {}
    for row in values.get("headers") or ():
        if isinstance(row, Mapping):
            name, value = str(row.get("name") or "").strip(), row.get("value")
            if name and isinstance(value, str) and value and value != SECRET_MASK and _header_ok(name, value):
                headers[name] = value
    if key and _header_ok("Authorization", key):
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _header_ok(name: str, value: str) -> bool:
    return bool(re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)) and not any(ch in value for ch in "\r\n")


def _curl(answer: Answer, *, key_sent: bool, timeout: bool) -> str:
    command = "curl -i" + (" --max-time 5" if timeout else "") + (" -X POST" if answer.method == "POST" else "") + f" {answer.url}"
    if key_sent:
        command += ' \\\n  -H "Authorization: Bearer YOUR_KEY"'
    if answer.method == "POST":
        command += " \\\n  -H \"content-type: application/json\" -d '{}'"
    return command


def _raw(answer: Answer) -> str:
    if answer.transport not in {"ok", "too_large", "redirect"} or answer.status is None:
        return f"{answer.method} {answer.url}\n{answer.detail}".strip()
    head = f"HTTP/1.1 {answer.status_text}"
    content_type = answer.headers.get("content-type")
    if content_type:
        head += f"\ncontent-type: {content_type}"
    if answer.location:
        head += f"\nlocation: {answer.location}"
    body = answer.text[:800]
    return f"{head}\n\n{body}" if body else head


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
    return probe["method"], _route_url(ctx, probe["path"], probe["from"])


def call_line(definition: Mapping[str, Any], values: Mapping[str, Any]) -> str:
    """The request the check makes first, as the dialog shows it: "GET http://…/models"."""
    try:
        address = normalize_address(values.get("address"))
    except DraftError:
        return ""
    probe = definition["probes"]["primary"]
    base = origin_of(address) if probe["from"] == "origin" else address
    prefix = str(values.get("path_prefix") or "")
    return f"{probe['method']} {join_url(join_url(base, prefix) if prefix else base, probe['path'])}"


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
            "request": _curl(answer, key_sent=key_sent, timeout=answer.transport == "timeout"),
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
) -> dict[str, Any]:
    """Run the ladder for ``definition`` against draft ``values``. Persists nothing."""
    data = catalog.data
    fields = {f["key"]: f for f in definition["fields"]}
    address = normalize_address(values.get("address"), placeholder=fields["address"].get("placeholder", ""))
    prefix = str(values.get("path_prefix") or "").strip()
    check_address = join_url(address, prefix) if prefix else address
    timeout_s = DEFAULT_TIMEOUT_S
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
            model_caps = _model_caps(item["entry"], data["model_rules"])
            models.append({
                "id": item["id"],
                "public_name": _unique(public_name(item["id"], data.get("public_name_rules") or ()), taken),
                "context_window": _context_window(item["entry"], data.get("context_window_keys") or ()),
                "capabilities": model_caps,
                "use": ", ".join(catalog.capability(c)["label"] for c in model_caps if c in data["capabilities"]) or "Not known yet",
            })
        result["shapes"].append(probe["id"])
    elif probe["kind"] == "model_list" and primary.status is not None and primary.status != 200:
        obs["shape"] = "none"
    result["steps"].append({"label": definition["check_rows"][2], "result": f"{len(models)} found" if probe["kind"] == "model_list" else ""})

    # Metadata reads, all at once.
    rows = [catalog.metadata_row(row_id) for row_id in definition["probes"]["metadata"]]
    meta_answers = await asyncio.gather(*(
        _get(ctx, _route_url(ctx, row["path"], row["from"]), cap=row.get("cap_bytes", DEFAULT_CAP_BYTES)) for row in rows
    ))
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
        elif cap_id in by_model_rule:
            names = by_model_rule[cap_id]
            template = evidence_text["likely_model_field_many"] if len(names) > 1 else evidence_text["likely_model_field"]
            level, evidence = "likely", _fill(template, {"model": names[0], "others": len(names) - 1, "noun": noun})
        elif cap_id in documented:
            names = documented[cap_id]
            template = evidence_text["likely_model_field_many"] if len(names) > 1 else evidence_text["likely_model_field"]
            level, evidence = "likely", _fill(template, {"model": names[0], "others": len(names) - 1, "noun": noun})
        elif cap_id in listed and route_level in ("possible", "none_server_error"):
            document, path = listed[cap_id]
            level, evidence = "likely", _fill(evidence_text["likely_metadata"], {"document": document, "path": path})
        elif cap_id in listed and cap_id not in route_levels:
            document, path = listed[cap_id]
            level, evidence = "likely", _fill(evidence_text["likely_metadata_only"], {"document": document, "path": path})
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
        if cap.get("real_test"):
            row["real_test"] = cap["real_test"]
        if cap.get("route"):
            route_base = join_url(ctx.origin, definition["probes"]["route_base"]) if definition["probes"].get("route_base") else ctx.address
            row["sent_to"] = f"{cap['route']['method']} {join_url(route_base, cap['route']['path'])}"
        caps[cap_id] = row

    order = {"likely": 0, "possible": 1, "needs_key": 2, "against": 3, "none": 4}
    result["capabilities"] = sorted(caps.values(), key=lambda row: order.get(row["level"], 5))
    for model in models:
        model.pop("capabilities", None)
    result["models"] = models
    recognised = bool(models) or any(row["level"] in ("likely", "possible") for row in caps.values())
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


async def run_real_test(
    catalog: EngineCatalog,
    definition: Mapping[str, Any],
    values: Mapping[str, Any],
    capability_id: str,
    *,
    key: KeyInput,
    fetch: Fetch,
    model: str | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """One tiny real request of one kind, from the gateway's computer, to the draft address."""
    if capability_id not in definition["capabilities"]:
        raise DraftError("This kind of server does not offer that.", field="capability")
    address = normalize_address(values.get("address"))
    cap = catalog.capability(capability_id)
    test = catalog.data["real_tests"][cap["real_test"]]
    headers = _auth_headers(key.value, values)
    prefix = str(values.get("path_prefix") or "").strip()
    base = join_url(address, prefix) if prefix else address
    if definition["probes"].get("route_base"):
        url = join_url(join_url(origin_of(address), definition["probes"]["route_base"]), cap["route"]["path"])
    else:
        url = join_url(base, cap["route"]["path"])
    slots = {"model": model or ""}
    content = json.dumps(_substitute(test.get("body") or {}, slots)).encode()
    headers["content-type"] = "application/json"
    shape = "a small JSON request in the OpenAI shape"
    # A test is one tiny request: it waits for the reply time, but never longer than two minutes.
    timeout_s = min(_number(values.get("reply_timeout_s"), TEST_TIMEOUT_S, low=1, high=600), TEST_TIMEOUT_S)
    answer = await fetch("POST", url, headers=headers, cap_bytes=TEST_CAP_BYTES, timeout_s=timeout_s, content=content)
    result: dict[str, Any] = {
        "capability": capability_id, "label": cap["label"], "sent": test["sent"], "sent_to": f"POST {url}",
        "seconds": answer.seconds, "cost": test["cost"], "at": int(now()), "status": answer.status, "via": "draft",
    }
    ok, reply = _judge(test, answer)
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


def _judge(test: Mapping[str, Any], answer: Answer) -> tuple[bool, str]:
    if answer.transport != "ok" or answer.status is None:
        return False, answer.detail or "No answer"
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

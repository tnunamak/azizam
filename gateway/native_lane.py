"""The per-engine native lane's rules (contract section 6): what an engine record lets through.

An engine record sets ``native_passthrough: true`` to open the lane and lists what may pass in
``native.allow``. Each entry names a method, a path template (``{name}`` stands for one path segment),
the query keys it allows, and a class: ``generate`` is plan-scoped, ``control`` (load, settings, restart,
file paths) is owner-only and never implied by generation access. A call no entry names is refused. An
entry that declares no ``query`` keys takes no query string; a key it does not declare is refused, and a
``generate`` entry refuses a value that looks like a file path. Request and reply bytes are capped
(``native.max_request_bytes``, ``native.max_response_bytes``).

Paths are matched exactly. An engine whose paths are case-insensitive says so with
``native.case_insensitive: true`` and is matched case-folded. For any other engine, a request that
matches a control entry only once case is folded is ambiguous, and the lane refuses it: a ``generate``
match never hides a control call.

The engine's own credentials sit in the record's ``connection.credentials``: ``inference`` for a
``generate`` call and ``control`` for a ``control`` call, each bound to the origin it was attached for.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode

from gateway.secret_store import credential_origin_from_url

GENERATE = "generate"
CONTROL = "control"
CLASSES = (GENERATE, CONTROL)
# What the native route serves (proxy.py registers it for these methods): HEAD is not among them.
METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"})
DEFAULT_MAX_REQUEST_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
CREDENTIAL_CLASSES = {GENERATE: "inference", CONTROL: "control"}

_QUERY_KEY = re.compile(r"[A-Za-z0-9_.\-\[\]]+")
_HEADER_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")

# Reply headers the lane never relays: the hop-by-hop set, and what belongs to the engine's origin
# (cookies, transport security, cross-origin policy, challenges) and must not act on the gateway's.
_HOP_HEADERS = frozenset({
    # content-encoding and content-length describe the engine's bytes; the lane relays them decoded.
    "connection", "content-encoding", "content-length", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "transfer-encoding", "upgrade",
})
_ORIGIN_HEADERS = frozenset({
    "set-cookie", "set-cookie2", "strict-transport-security", "www-authenticate", "alt-svc", "location",
    "content-security-policy", "content-security-policy-report-only", "clear-site-data", "public-key-pins",
    "public-key-pins-report-only", "server", "x-powered-by", "refresh", "report-to", "nel",
})
_ORIGIN_HEADER_PREFIXES = ("access-control-",)


@dataclass(frozen=True)
class AllowEntry:
    method: str
    path: str
    klass: str
    query: tuple[str, ...] = ()
    any_query: bool = False  # a call the record does not declare: any key, but the value rules still apply

    def matches(self, method: str, path: str, *, fold: bool = False) -> bool:
        return self.method == method.upper() and _template(self.path, fold).fullmatch(path) is not None


@dataclass(frozen=True)
class Resolution:
    """The entry a call matches, and whether letter case alone separates it from a control entry."""

    entry: AllowEntry | None = None
    ambiguous: bool = False


@dataclass(frozen=True)
class NativePolicy:
    allow: tuple[AllowEntry, ...] = ()
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    case_insensitive: bool = False

    def resolve(self, method: str, path: str) -> Resolution:
        """The entry for this call. A control entry wins over a generate entry for the same call."""
        matching = [entry for entry in self.allow if entry.matches(method, path, fold=self.case_insensitive)]
        found = next((entry for entry in matching if entry.klass == CONTROL), matching[0] if matching else None)
        if self.case_insensitive or (found is not None and found.klass == CONTROL):
            return Resolution(found)
        folded_control = any(
            entry.klass == CONTROL and entry.matches(method, path, fold=True) for entry in self.allow
        )
        return Resolution(found, ambiguous=folded_control)

    def entry_for(self, method: str, path: str) -> AllowEntry | None:
        return self.resolve(method, path).entry


def _template(path: str, fold: bool = False) -> re.Pattern[str]:
    parts = re.split(r"(\{[A-Za-z_][A-Za-z0-9_]*\})", path)
    return re.compile(
        "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts),
        re.IGNORECASE if fold else 0,
    )


def validate_native_block(block: Any) -> None:
    """Raise ValueError when an engine record's ``native`` block cannot be read as a policy."""
    if block is None:
        return
    if not isinstance(block, Mapping):
        raise ValueError("native must be a mapping")
    allow = block.get("allow", [])
    if not isinstance(allow, (list, tuple)):
        raise ValueError("native.allow must be a list")
    for entry in allow:
        if not isinstance(entry, Mapping):
            raise ValueError("each native.allow entry must be a mapping")
        if str(entry.get("method", "")).upper() not in METHODS:
            raise ValueError(f"native.allow method must be one of {sorted(METHODS)}")
        path = entry.get("path")
        if not isinstance(path, str) or not path.startswith("/") or "*" in path:
            raise ValueError("native.allow path must be an absolute path template without wildcards")
        if entry.get("class") not in CLASSES:
            raise ValueError(f"native.allow class must be one of {list(CLASSES)}")
        query = entry.get("query", [])
        if not isinstance(query, (list, tuple)) or not all(
            isinstance(key, str) and _QUERY_KEY.fullmatch(key) for key in query
        ):
            raise ValueError("native.allow query must be a list of query key names")
    if "case_insensitive" in block and not isinstance(block["case_insensitive"], bool):
        raise ValueError("native.case_insensitive must be true or false")
    for name in ("max_request_bytes", "max_response_bytes"):
        value = block.get(name)
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError(f"native.{name} must be a positive integer")


READ_METHODS = frozenset({"GET", "HEAD"})


def provider_route_resolution(policy: NativePolicy, method: str, path: str) -> Resolution:
    """The entry for a call on the provider route: the record's own, else a default by method.

    A call the record's allow list names is classed as the list says. Any other GET or HEAD is a
    ``generate`` read, and any other method is a ``control`` call, which only the owner may make.
    """
    resolution = policy.resolve(method, path)
    if resolution.entry is not None or resolution.ambiguous:
        return resolution
    klass = GENERATE if method.upper() in READ_METHODS else CONTROL
    return Resolution(AllowEntry(method.upper(), path, klass, any_query=True))


def policy_for(record: Mapping[str, Any]) -> NativePolicy:
    """The policy an engine record declares. A record with no valid ``native`` block allows nothing."""
    block = record.get("native")
    try:
        validate_native_block(block)
    except ValueError:
        return NativePolicy()
    if not block:
        return NativePolicy()
    return NativePolicy(
        allow=tuple(
            AllowEntry(str(entry["method"]).upper(), entry["path"], entry["class"], tuple(entry.get("query") or ()))
            for entry in block.get("allow", ())
        ),
        max_request_bytes=block.get("max_request_bytes") or DEFAULT_MAX_REQUEST_BYTES,
        max_response_bytes=block.get("max_response_bytes") or DEFAULT_MAX_RESPONSE_BYTES,
        case_insensitive=block.get("case_insensitive") is True,
    )


# --- query strings ---------------------------------------------------------------------------------

_DRIVE = re.compile(r"[A-Za-z]:")


def _path_like(value: str) -> bool:
    """A value that names a file or a place on a file system: the lane's generate calls must not carry one."""
    return (
        "/" in value or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or value.startswith("~") or value in {".", ".."}
        or _DRIVE.match(value) is not None
        or value.lower().startswith("file:")
    )


def query_problem(entry: AllowEntry, raw_query: str) -> tuple[str, str] | None:
    """(code, message) when the query string may not go to the engine for this entry, else None.

    Only the keys the entry declares pass, each once; a ``generate`` entry also refuses a value that
    looks like a file path. Keys and values are read after decoding, as the engine will read them.
    """
    seen: set[str] = set()
    for key, value in parse_qsl(raw_query, keep_blank_values=True):
        if key not in entry.query and not entry.any_query:
            return "native_query_not_allowed", (
                f"The query key {key[:48]!r} is not allowed for {entry.method} {entry.path}."
            )
        if key in seen and not entry.any_query:
            return "native_query_not_allowed", f"The query key {key[:48]!r} is repeated."
        seen.add(key)
        if entry.klass == GENERATE and _path_like(value):
            return "native_query_value_refused", (
                f"The value of query key {key[:48]!r} looks like a file path; a generate call cannot pass one."
            )
    return None


def forwarded_query(raw_query: str) -> str:
    """The query string that goes to the engine: rebuilt from the pairs ``query_problem`` accepted."""
    return urlencode(parse_qsl(raw_query, keep_blank_values=True))


# --- credentials -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Credential:
    """One class's credential for the engine: where its secret is, how it is sent, and the origin it is bound to."""

    header: str
    scheme: str
    secret_env: str
    secret_ref: str
    origin: str

    def bound_to(self, url: str) -> bool:
        try:
            return bool(self.origin) and credential_origin_from_url(self.origin) == credential_origin_from_url(url)
        except ValueError:
            return False


def validate_connection(block: Any) -> None:
    """Raise ValueError when a record's ``connection`` block holds a credential that cannot be read."""
    if block is None:
        return
    if not isinstance(block, Mapping):
        raise ValueError("connection must be a mapping")
    credentials = block.get("credentials")
    if credentials is None:
        return
    if not isinstance(credentials, Mapping):
        raise ValueError("connection.credentials must be a mapping")
    for name, credential in credentials.items():
        if name not in CREDENTIAL_CLASSES.values():
            raise ValueError(f"connection.credentials names a credential class {name!r}; use inference or control")
        if credential is None:
            continue
        if not isinstance(credential, Mapping):
            raise ValueError(f"connection.credentials.{name} credential must be a mapping or null")
        if not (credential.get("secret_env") or credential.get("secret_ref")):
            raise ValueError(f"connection.credentials.{name} credential names no secret_env or secret_ref")
        header = credential.get("header")
        if header is not None and not (isinstance(header, str) and _HEADER_TOKEN.fullmatch(header)):
            raise ValueError(f"connection.credentials.{name} credential header is not a header name")
        scheme = credential.get("scheme")
        if scheme is not None and not (isinstance(scheme, str) and _HEADER_TOKEN.fullmatch(scheme)):
            raise ValueError(f"connection.credentials.{name} credential scheme is not a token")
        origin = credential.get("origin")
        if origin is not None:
            try:
                credential_origin_from_url(str(origin))
            except ValueError:
                raise ValueError(f"connection.credentials.{name} credential origin must be an HTTP(S) URL") from None


def credential_for(record: Mapping[str, Any], klass: str) -> Credential | None:
    """The credential this record holds for a call class, or None. Never the other class's."""
    connection = record.get("connection")
    credentials = connection.get("credentials") if isinstance(connection, Mapping) else None
    raw = credentials.get(CREDENTIAL_CLASSES[klass]) if isinstance(credentials, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    named_header = raw.get("header")
    header = str(named_header) if named_header else "Authorization"
    scheme = str(raw.get("scheme") or "") if named_header else str(raw.get("scheme") or "Bearer")
    return Credential(
        header=header,
        scheme=scheme,
        secret_env=str(raw.get("secret_env") or ""),
        secret_ref=str(raw.get("secret_ref") or ""),
        origin=str(raw.get("origin") or ""),
    )


# --- the reply -------------------------------------------------------------------------------------

def relayed_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The engine's reply headers that go on to the caller: not hop-by-hop, and not the engine's own origin policy."""
    return {
        name: value for name, value in headers.items()
        if name.lower() not in _HOP_HEADERS
        and name.lower() not in _ORIGIN_HEADERS
        and not name.lower().startswith(_ORIGIN_HEADER_PREFIXES)
    }


def reply_refusal(policy: NativePolicy, status_code: int, content_length: str | None, engine: str) -> tuple[int, str, str] | None:
    """(status, code, message) when the engine's reply head alone makes the gateway refuse it, else None."""
    if status_code in REDIRECT_STATUSES:
        return 502, "native_redirect_refused", (
            f"Engine {engine!r} answered with a redirect, which this lane never follows or relays."
        )
    if content_length and content_length.isdigit() and int(content_length) > policy.max_response_bytes:
        return 502, "native_response_too_large", (
            f"Engine {engine!r} answered with more than the {policy.max_response_bytes}-byte limit of this lane."
        )
    return None

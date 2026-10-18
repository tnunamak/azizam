"""Typed values shared by the executor and the adapter interface.

Everything an adapter returns is one of these: a result, a job reference, a
status, an event or an asset reference. Core keeps the durable job state
(contract section 4, F8); these values are what crosses the boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

CREDENTIAL_HEADER = re.compile(r"auth|cookie|token|secret|key|credential|password|session", re.IGNORECASE)
_HEADER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,63}$")


@dataclass(frozen=True)
class Credential:
    """A secret value for one operation class, with optional overrides of where it goes. Never printed."""

    secret: str = field(repr=False)
    header: str | None = None    # overrides the description's header name
    scheme: str | None = None    # overrides the word before the secret; "" means none


@dataclass(frozen=True)
class Timeouts:
    connect_s: float = 5.0
    first_byte_s: float = 120.0
    total_s: float = 600.0


@dataclass(frozen=True)
class Connection:
    """How Azizam reaches one engine: the part of the engine record (contract section 2) the executor needs.

    ``address`` is the root origin (with an optional path prefix). ``credentials`` is keyed by operation class
    (``inference`` or ``control``); each is bound to this address's origin by the executor.
    """

    address: str
    credentials: Mapping[str, Credential] = field(default_factory=dict)
    timeouts: Timeouts = field(default_factory=Timeouts)
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parts = urlsplit(self.address)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("The engine address must start with http:// or https:// and name a host.")
        if parts.username is not None or parts.password is not None:
            raise ValueError("The engine address must not hold a user name or password; credentials are stored apart.")
        if parts.query or parts.fragment:
            raise ValueError("The engine address must not hold a query or fragment.")
        for name in self.headers:
            if not _HEADER_NAME.match(name) or CREDENTIAL_HEADER.search(name):
                raise ValueError(f"'{name}' cannot be an ordinary header here: credential-like headers are stored as credentials.")


@dataclass(frozen=True)
class AssetRef:
    """A canonical asset: bytes held by the media store, named by id."""

    id: str
    content_type: str
    size: int
    name: str | None = None


@dataclass
class CanonicalRequest:
    """One request in Azizam's own shape. ``body`` is JSON, ``params`` are path and query values, ``files`` are assets."""

    op: str
    body: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    files: Mapping[str, AssetRef] = field(default_factory=dict)
    # Headers the caller already prepared for this engine. The http-v1 executor ignores them (it builds headers from the
    # description and the connection); a reviewed in-tree adapter that wraps an older client may send them as they are.
    headers: Mapping[str, str] = field(default_factory=dict)
    # A body the caller already serialized. Like ``headers``: ignored by the http-v1 executor, sent as is by a reviewed
    # adapter that wraps an older client (so the bytes on the wire do not change).
    raw_body: bytes | None = None


class JobState(str, Enum):
    pending = "pending"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


@dataclass(frozen=True)
class JobRef:
    """The engine's id for one job. Core persists it; ``recover`` and ``poll`` take it back."""

    op: str
    id: str


@dataclass(frozen=True)
class JobStatus:
    ref: JobRef
    state: JobState
    detail: str | None = None
    assets: tuple[str, ...] = ()   # the engine's handles for finished artifacts; ``fetch_assets`` turns them into AssetRefs


@dataclass(frozen=True)
class Reply:
    """A finished answer. ``body`` is the parsed (and mapped) JSON; ``selected`` holds the named pointer selections."""

    status: int
    headers: Mapping[str, str]
    body: Any = None
    selected: Mapping[str, Any] = field(default_factory=dict)
    asset: AssetRef | None = None
    seconds: float = 0.0
    # The unparsed answer bytes, for a reviewed adapter whose caller post-processes them in memory (the http-v1 executor
    # leaves it empty: it returns parsed bodies and saved assets).
    raw: bytes | None = None


class StreamReply:
    """A streamed answer. Iterate it for events; each event is a dict (mapped) or the raw data bytes (unmapped).

    ``aclose`` is safe to call at any time, more than once, and releases the upstream connection.
    """

    def __init__(self, status: int, headers: Mapping[str, str], events: AsyncIterator[Any], close: Callable[[], Awaitable[None]]) -> None:
        self.status = status
        self.headers = headers
        self._events = events
        self._close = close

    def __aiter__(self) -> AsyncIterator[Any]:
        return self._events

    async def aclose(self) -> None:
        await self._events.aclose()  # type: ignore[attr-defined]
        await self._close()

    async def __aenter__(self) -> "StreamReply":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


@dataclass(frozen=True)
class JobEvent:
    """A change an engine pushed about a job (for adapters whose engine can say so; others are polled)."""

    ref: JobRef
    kind: str                     # status | progress | asset
    state: JobState | None = None
    progress: float | None = None
    detail: str | None = None


@dataclass(frozen=True)
class Health:
    """What Azizam observed. ``unknown`` is never green (contract F11)."""

    state: str                    # ok | failing | unknown
    detail: str = ""
    evidence: str = ""            # which operation answered


@dataclass(frozen=True)
class Inventory:
    ids: tuple[str, ...]
    evidence: str = ""

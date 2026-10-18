"""The versioned adapter interface (contract section 4).

An adapter is how Azizam talks to one kind of engine. Two kinds exist and both
implement this interface: the generic ``HttpV1Adapter`` (a protocol description
plus the bounded executor, no code per engine) and reviewed in-tree adapters
(``executor: adapter:<id>``) for what http-v1 cannot express. "builtin" is
provenance only: an adapter a package ships later implements the same interface.

Interface version 1, with ``conn`` the ``Connection`` of the engine record:

    health(conn)                      -> Health        observed, never assumed; unknown is never green
    inventory(conn)                   -> Inventory     the offering ids the engine reports
    invoke(conn, op, request)         -> Reply | StreamReply
    submit(conn, op, request)         -> JobRef        start a job
    poll(conn, ref)                   -> JobStatus     one look at a job
    events(conn, ref)                 -> async iterator of JobEvent (push, optional)
    cancel(conn, ref)                 -> None
    recover(conn, ref)                -> JobStatus | None   after a gateway restart; None = the engine lost the job
    fetch_assets(conn, ref, status)   -> tuple[AssetRef, ...]

Core owns the durable job state machine (F8): idempotency keys, retry
boundaries, event order, asset lifetime and receipts. An adapter only returns
typed references; it keeps no job state that core cannot rebuild from a
``JobRef``. A part an adapter does not implement raises ``UnsupportedByAdapter``
so core can choose another way (poll instead of events) or refuse.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable, Mapping, Protocol, runtime_checkable

from gateway.engine_protocol.assets import AssetStore
from gateway.engine_protocol.description import ProtocolDescription
from gateway.engine_protocol.errors import ProtocolError, UnknownProtocol
from gateway.engine_protocol.transport import Transport
from gateway.engine_protocol.types import (
    AssetRef, CanonicalRequest, Connection, Credential, Health, Inventory, JobEvent, JobRef, JobStatus, Reply, StreamReply,
    Timeouts,
)

ADAPTER_INTERFACE_VERSION = 1

__all__ = [
    "ADAPTER_INTERFACE_VERSION", "AdapterRegistry", "EngineAdapter", "Health", "Inventory", "connection_from_record",
]


@runtime_checkable
class EngineAdapter(Protocol):
    interface_version: int

    async def health(self, conn: Connection) -> Health: ...

    async def inventory(self, conn: Connection) -> Inventory: ...

    async def invoke(self, conn: Connection, op: str, request: CanonicalRequest) -> Reply | StreamReply: ...

    async def submit(self, conn: Connection, op: str, request: CanonicalRequest) -> JobRef: ...

    async def poll(self, conn: Connection, ref: JobRef) -> JobStatus: ...

    def events(self, conn: Connection, ref: JobRef) -> AsyncIterator[JobEvent]: ...

    async def cancel(self, conn: Connection, ref: JobRef) -> None: ...

    async def recover(self, conn: Connection, ref: JobRef) -> JobStatus | None: ...

    async def fetch_assets(self, conn: Connection, ref: JobRef, status: JobStatus) -> tuple[AssetRef, ...]: ...


AdapterFactory = Callable[[ProtocolDescription], EngineAdapter]


class AdapterRegistry:
    """Resolves a protocol id to the adapter that speaks it.

    ``executor: http-v1`` descriptions get a ``HttpV1Adapter``. ``executor: adapter:<id>`` needs a reviewed factory
    registered under that id; an unregistered id, or an adapter that implements another interface version, is refused
    by name rather than guessed.
    """

    def __init__(self, descriptions: Mapping[str, ProtocolDescription], *, transport: Transport | None = None,
                 assets: AssetStore | None = None) -> None:
        self._descriptions = dict(descriptions)
        self._transport = transport
        self._assets = assets
        self._factories: dict[str, AdapterFactory] = {}
        self._built: dict[str, EngineAdapter] = {}

    def register(self, adapter_id: str, factory: AdapterFactory) -> None:
        self._factories[adapter_id] = factory
        self._built = {pid: a for pid, a in self._built.items() if self._descriptions[pid].adapter_id != adapter_id}

    def adapter_for(self, protocol_id: str) -> EngineAdapter:
        if protocol_id in self._built:
            return self._built[protocol_id]
        description = self._descriptions.get(protocol_id)
        if description is None:
            raise UnknownProtocol(f"There is no protocol description named '{protocol_id}'.")
        if description.adapter_id is None:
            from gateway.engine_protocol.executor import HttpV1Executor
            from gateway.engine_protocol.http_adapter import HttpV1Adapter
            built: EngineAdapter = HttpV1Adapter(HttpV1Executor(description, transport=self._transport, assets=self._assets))
        else:
            factory = self._factories.get(description.adapter_id)
            if factory is None:
                raise ProtocolError(f"The protocol '{protocol_id}' needs the adapter '{description.adapter_id}', which is not registered.",
                                    code="adapter_not_registered")
            built = factory(description)
            version = getattr(built, "interface_version", None)
            if version != ADAPTER_INTERFACE_VERSION:
                raise ProtocolError(
                    f"The adapter '{description.adapter_id}' implements interface version {version}; this Azizam speaks version {ADAPTER_INTERFACE_VERSION}.",
                    code="adapter_version_mismatch",
                )
        self._built[protocol_id] = built
        return built


def connection_from_record(connection: Mapping[str, Any], secret: Callable[[str], str | None]) -> Connection:
    """The ``Connection`` of an engine record's ``connection`` block (contract section 2).

    ``secret`` looks a ``secret_ref`` up; a reference that resolves to nothing gives no credential (the request goes
    without a key, and the engine says so), never an empty one.
    """
    transport = connection.get("transport", "http")
    if transport != "http":
        raise ProtocolError(f"The transport '{transport}' needs a reviewed adapter; the http-v1 executor speaks http only.",
                            code="adapter_required")
    credentials: dict[str, Credential] = {}
    for cls, spec in (connection.get("credentials") or {}).items():
        if not spec:
            continue
        ref = spec.get("secret_ref")
        value = secret(ref) if ref else None
        if value:
            credentials[cls] = Credential(value, header=spec.get("header"), scheme=spec.get("scheme"))
    raw_headers = connection.get("headers") or {}
    if isinstance(raw_headers, Mapping):
        headers = dict(raw_headers)
    else:
        headers = {row["name"]: row["value"] for row in raw_headers}
    timeouts = connection.get("timeouts") or {}
    defaults = Timeouts()
    return Connection(
        address=connection["address"],
        credentials=credentials,
        timeouts=Timeouts(
            connect_s=float(timeouts.get("connect_s", defaults.connect_s)),
            first_byte_s=float(timeouts.get("first_byte_s", defaults.first_byte_s)),
            total_s=float(timeouts.get("total_s", defaults.total_s)),
        ),
        headers=headers,
    )

"""The generic adapter: a protocol description plus the http-v1 executor. No code per engine."""

from __future__ import annotations

import dataclasses
from typing import AsyncIterator

from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION
from gateway.engine_protocol.errors import ProtocolError, UnsupportedByAdapter, UpstreamError
from gateway.engine_protocol.executor import HttpV1Executor
from gateway.engine_protocol.types import (
    AssetRef, CanonicalRequest, Connection, Health, Inventory, JobEvent, JobRef, JobStatus, Reply, StreamReply,
)

# Operation names the adapter itself looks for. A description may omit them; then the part is unknown or unsupported.
_HEALTH_OPS = ("health", "inventory", "models")
_INVENTORY_OPS = ("inventory", "models")


class HttpV1Adapter:
    interface_version = ADAPTER_INTERFACE_VERSION

    def __init__(self, executor: HttpV1Executor) -> None:
        self._executor = executor

    def _first_request_op(self, names: tuple[str, ...]) -> str | None:
        for name in names:
            op = self._executor.description.operations.get(name)
            if op is not None and op.request is not None:
                return name
        return None

    async def health(self, conn: Connection) -> Health:
        name = self._first_request_op(_HEALTH_OPS)
        if name is None:
            return Health("unknown", "The protocol describes no health or inventory operation to ask.")
        try:
            reply = await self._executor.invoke(conn, CanonicalRequest(op=name))
            if isinstance(reply, StreamReply):
                await reply.aclose()
            return Health("ok", "", name)
        except UpstreamError as error:
            return Health("failing", f"{error} {error.advice or ''}".strip(), name)
        except ProtocolError as error:
            return Health("failing", str(error), name)

    async def inventory(self, conn: Connection) -> Inventory:
        name = self._first_request_op(_INVENTORY_OPS)
        if name is None:
            raise UnsupportedByAdapter("The protocol describes no inventory operation.")
        reply = await self._executor.invoke(conn, CanonicalRequest(op=name))
        if isinstance(reply, StreamReply):
            await reply.aclose()
            raise UnsupportedByAdapter("The inventory operation is streamed.")
        if "ids" not in reply.selected:
            raise UnsupportedByAdapter(f"The '{name}' operation selects no 'ids'.")
        return Inventory(tuple(str(item) for item in reply.selected["ids"]), name)

    async def invoke(self, conn: Connection, op: str, request: CanonicalRequest) -> Reply | StreamReply:
        return await self._executor.invoke(conn, dataclasses.replace(request, op=op))

    async def submit(self, conn: Connection, op: str, request: CanonicalRequest) -> JobRef:
        return await self._executor.submit(conn, dataclasses.replace(request, op=op))

    async def poll(self, conn: Connection, ref: JobRef) -> JobStatus:
        return await self._executor.poll(conn, ref)

    async def events(self, conn: Connection, ref: JobRef) -> AsyncIterator[JobEvent]:
        raise UnsupportedByAdapter("The http-v1 job pattern is polled; it has no event stream.")
        yield  # pragma: no cover - makes this an async generator so the error is raised on first iteration

    async def cancel(self, conn: Connection, ref: JobRef) -> None:
        await self._executor.cancel(conn, ref)

    async def recover(self, conn: Connection, ref: JobRef) -> JobStatus | None:
        try:
            return await self._executor.poll(conn, ref)
        except UpstreamError as error:
            if error.status in (404, 410):
                return None
            raise

    async def fetch_assets(self, conn: Connection, ref: JobRef, status: JobStatus) -> tuple[AssetRef, ...]:
        return await self._executor.fetch_assets(conn, ref, status)

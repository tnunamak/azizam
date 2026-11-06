"""The generic adapter: a protocol description plus the http-v1 executor. No code per engine."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import json
from typing import Any, AsyncIterator

from gateway import output_selectors
from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION_V2
from gateway.engine_protocol.errors import ProtocolError, SelectionRefused, UnsupportedByAdapter, UpstreamError
from gateway.engine_protocol.executor import HttpV1Executor
from gateway.engine_protocol.types import (
    AssetRef, AssetsResult, CanonicalRequest, Completed, Connection, Health, Inventory, JobEvent, JobRef, JobStatus, JsonResult,
    Pending, Reply, ResultBundle, StreamReply,
)

# Operation names the adapter itself looks for. A description may omit them; then the part is unknown or unsupported.
_HEALTH_OPS = ("health", "inventory", "models")
_INVENTORY_OPS = ("inventory", "models")


class HttpV1Adapter:
    interface_version = ADAPTER_INTERFACE_VERSION_V2

    def __init__(self, executor: HttpV1Executor) -> None:
        self._executor = executor
        # The key under a manifest's bindings that holds this description's selectors, and the form they take; both are
        # data the description declares (empty and None when it declares none).
        self.binding = executor.description.binding
        self.result_selectors = executor.description.result_selectors

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

    # --- interface version 2: one start outcome ----------------------------------------------------------------

    async def start(self, conn: Connection, op: str, request: CanonicalRequest) -> Pending | Completed:
        """Start one operation. An operation the description gives a job pattern is submitted, and the engine's job id is
        ``Pending``. Any other operation is one request and one answer: its answer is read through the binding's selectors
        into a ``ResultBundle``, and that is ``Completed``. The description decides which; no name is looked at."""
        described = self._executor.description.operations.get(op)
        if described is not None and described.job is not None:
            return Pending(await self.submit(conn, op, request))
        reply = await self._executor.invoke(conn, dataclasses.replace(request, op=op))
        if isinstance(reply, StreamReply):
            await reply.aclose()
            raise UnsupportedByAdapter("A streamed answer has no result selectors; the operation must answer once.")
        return Completed(await self._bundle(reply, request))

    async def _bundle(self, reply: Reply, request: CanonicalRequest) -> ResultBundle:
        """The named results the binding's selectors read from one answer. A selection that is missing or null, a value
        that does not fit what its output declares, or an answer the selectors cannot read is refused; nothing is guessed
        and a refused answer delivers nothing."""
        if self.result_selectors is None:
            raise SelectionRefused("selectors_unsupported", "this engine's description takes no output selectors")
        manifest = request.params.get("manifest")
        block = ((manifest or {}).get("bindings") or {}).get(self.binding) if isinstance(manifest, dict) else None
        if not isinstance(block, dict) or "selectors" not in block:
            raise SelectionRefused("selectors_missing", "this operation declares no output selectors, and a synchronous answer has no legacy reading")
        outputs = manifest.get("outputs") or {}
        try:
            selectors = output_selectors.check_binding(self.result_selectors, outputs, block)
        except output_selectors.SelectorError as exc:
            raise SelectionRefused(exc.code, str(exc)) from exc
        descriptor = self.result_selectors
        if sum(1 for sel in selectors.values() if sel["decode"] == "binary") > 1:
            raise SelectionRefused("result_ambiguous", "an answer has one binary body; at most one result can read it")
        results: dict[str, JsonResult | AssetsResult] = {}
        for name, selector in selectors.items():
            kind = descriptor.kind_of(selector)
            decode = selector["decode"]
            if decode == "binary":
                if reply.asset is None:
                    raise SelectionRefused("result_missing", f"result {name} is not in the engine's answer")
                files: tuple[AssetRef, ...] = (reply.asset,)
            elif decode == "base64":
                files = (await self._inline_file(reply, name, selector["source"]),)
            else:
                results[name] = self._json_value(reply, name, selector["source"])
                files = ()
            if kind == "assets":
                results[name] = AssetsResult(files)
            reason = output_selectors.wrong_type_reason(outputs.get(name), kind, [file.content_type for file in files])
            if reason is not None:
                raise SelectionRefused("result_wrong_type", f"result {name}: {reason}")
        return ResultBundle(results)

    @staticmethod
    def _selected(reply: Reply, name: str, source: str) -> Any:
        value = reply.selected.get(source)
        if value is None:
            raise SelectionRefused("result_missing", f"result {name} is not in the engine's answer")
        return value

    def _json_value(self, reply: Reply, name: str, source: str) -> JsonResult:
        value = self._selected(reply, name, source)
        try:
            return JsonResult(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, RecursionError) as exc:
            raise SelectionRefused("result_malformed", f"result {name} cannot be written as JSON") from exc

    async def _inline_file(self, reply: Reply, name: str, source: str) -> AssetRef:
        value = self._selected(reply, name, source)
        if not isinstance(value, str):
            raise SelectionRefused("result_malformed", f"result {name} is not an encoded file")
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SelectionRefused("result_malformed", f"result {name} is not valid base64") from exc
        if not data:
            raise SelectionRefused("result_missing", f"result {name} is empty")
        return await self._executor.save_bytes(data, name=name)

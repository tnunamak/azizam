"""The in-tree node-graph engine client, behind the adapter interface (``executor: adapter:node-graph``).

This is the reviewed adapter for engines that run a graph of nodes submitted as
one JSON prompt: the prompt goes in, a history entry says when it ended, and the
output files are read back by name. The ComfyUI server is the engine it was
written for. The adapter moves *where the requests leave the gateway*, not what
they send: each operation issues the very httpx call the gateway always made,
with the same URL, keyword arguments and timeouts, so wire bytes do not change.

Core keeps the policy and the durable state: admission, receipts, assets, media
jobs, progress records and retry choices. This module owns the wire and the
knowledge of the engine's answers: how a prompt is submitted, what a history
entry means, which output files a finished prompt names, how a queue lists its
prompts, how an execution error reads. Messages that name the engine use the
``label`` of the protocol description (data), so no module spells an engine
name.

Operations (``invoke``): ``graph.submit``, ``graph.history``, ``graph.history_page``,
``graph.queue``, ``graph.view``, ``graph.upload``, ``graph.cancel``,
``graph.delete_history``, ``graph.stats`` and ``graph.free``. Jobs: ``submit``
posts a prompt; ``poll`` reads its history entry; ``cancel`` asks the engine to
stop it; ``recover`` looks the prompt up in history and queue; ``events``
follows the engine's push channel; ``fetch_assets`` reads the output files into
the media store. ``fetch_outputs`` returns the same files as bytes for callers
that store them themselves.

A caller that makes several requests for one job opens one session
(``adapter.open``) and passes it to the job methods, so the requests share one
connection, as one client did before.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Mapping, Sequence
from urllib.parse import quote

import httpx

from gateway import log_scrub

from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION
from gateway.engine_protocol.assets import AssetStore, served_media_type
from gateway.engine_protocol.description import ProtocolDescription
from gateway.engine_protocol.errors import (
    MalformedResponse, TransportFailure, UnknownOperation, UnsupportedByAdapter, UpstreamError,
)
from gateway.engine_protocol.httpx_wire import UNSET, WireReply, failure, reply_from, supplied
from gateway.engine_protocol.types import (
    AssetRef, CanonicalRequest, Connection, Health, Inventory, JobEvent, JobRef, JobState, JobStatus, Reply, StreamReply,
)

ADAPTER_ID = "node-graph"
OPERATIONS = (
    "graph.submit", "graph.history", "graph.history_page", "graph.queue", "graph.view", "graph.upload", "graph.cancel",
    "graph.delete_history", "graph.stats", "graph.free",
)
CONTROL_TIMEOUT_S = 5.0          # the read timeout of a status look or a cancel; callers may pass their own
IMAGE_KEYS = ("images",)
VIDEO_KEYS = ("videos", "gifs", "images")
AUDIO_KEYS = ("audios", "audio", "wav", "mp3")
OUTPUT_KEYS = ("images", "videos", "gifs", "audios", "audio", "wav", "mp3")
VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv")
_DEFAULT_LABEL = "the engine"


# ---------------------------------------------------------------------------
# What the engine's answers mean (pure; no I/O)
# ---------------------------------------------------------------------------


def _own_node_label(graph: Mapping[str, Any] | None, node_id: Any) -> str:
    """The gateway's own label (``class_type``) for a node of the graph it submitted, or ``""``.

    The engine names the node that failed; the name is used only to look the node up in the graph the gateway sent. The
    words of the sentence come from that graph, never from the engine's ``node_type`` or ``node_id`` (decision D45b).
    """
    if not isinstance(graph, Mapping):
        return ""
    node = graph.get(str(node_id)) if node_id is not None else None
    if not isinstance(node, Mapping):
        return ""
    return str(node.get("class_type") or "").strip()[:64]


def execution_error_message(job: Mapping[str, Any] | None, label: str = _DEFAULT_LABEL,
                            graph: Mapping[str, Any] | None = None) -> str | None:
    """A short summary of an execution error in a history entry, or ``None``.

    ``graph`` is the prompt the gateway submitted (node id to node). A node is named only when the engine's node id is in
    that graph, and then by the graph's own ``class_type``; without the graph, or for an id that is not in it, the
    sentence names no node.
    """
    status_block = (job or {}).get("status", {}) if isinstance(job, Mapping) else {}
    if not isinstance(status_block, Mapping):
        return None

    messages = status_block.get("messages", []) or []
    for msg in messages:
        if not (isinstance(msg, (list, tuple)) and msg and msg[0] == "execution_error"):
            continue
        payload = msg[1] if len(msg) > 1 and isinstance(msg[1], Mapping) else {}
        node_id = payload.get("node_id")
        node_type = _own_node_label(graph, node_id)

        location = f"{label} execution error"
        if node_type:
            location = f"{label} {node_type} node {node_id}"

        return f"{location}: execution failed"[:500]

    status = str(status_block.get("status_str") or status_block.get("status") or "").lower()
    if status == "error":
        return f"{label} execution failed"
    return None


def execution_error_node_id(job: Mapping[str, Any] | None) -> str | None:
    """The id of the node that failed, when the history entry names one."""
    status_block = (job or {}).get("status", {}) if isinstance(job, Mapping) else {}
    for msg in (status_block.get("messages", []) or []) if isinstance(status_block, Mapping) else []:
        if isinstance(msg, (list, tuple)) and msg and msg[0] == "execution_error":
            payload = msg[1] if len(msg) > 1 and isinstance(msg[1], Mapping) else {}
            node_id = str(payload.get("node_id") or "").strip()
            return node_id or None
    return None


def history_state(job: Mapping[str, Any] | None, label: str = _DEFAULT_LABEL,
                  graph: Mapping[str, Any] | None = None) -> tuple[str, str | None]:
    """``(state, error_text)`` for a history entry: ``failed``, ``running`` or ``succeeded``."""
    error_message = execution_error_message(job, label, graph)
    if error_message:
        return ("failed", error_message)

    status_block = (job or {}).get("status", {}) or {}
    if not status_block.get("completed"):
        return ("running", None)
    return ("succeeded", None)


def queue_prompt_ids(queue_block: Any) -> set[str]:
    """The prompt ids in one half of a queue answer. Entries are lists: ``[priority, prompt_id, ...]``."""
    out: set[str] = set()
    if not isinstance(queue_block, list):
        return out
    for entry in queue_block:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            pid = entry[1]
            if isinstance(pid, str) and pid:
                out.add(pid)
    return out


def queue_position(queue: Mapping[str, Any], prompt_id: str) -> int | None:
    """The 1-based position of a pending prompt, when the queue lists it."""
    pending = queue.get("queue_pending")
    if not isinstance(pending, list):
        return None
    for index, entry in enumerate(pending, start=1):
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[1] == prompt_id:
            return index
    return None


def view_params(info: Mapping[str, Any], *, default_type: str = "output") -> dict[str, Any]:
    """The query of a file read for one output entry."""
    params: dict[str, Any] = {"filename": info.get("filename"), "type": info.get("type", default_type)}
    if info.get("subfolder"):
        params["subfolder"] = info["subfolder"]
    return params


def output_entries(job: Mapping[str, Any] | None, keys: Sequence[str]) -> list[dict[str, Any]]:
    """Every output entry under ``keys``, in node order, as the history lists them."""
    out: list[dict[str, Any]] = []
    outputs = (job or {}).get("outputs", {}) or {}
    for node_output in outputs.values():
        for key in keys:
            if key not in node_output:
                continue
            out.extend(node_output[key])
    return out


def first_video_output(job: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The first video file a finished prompt produced.

    A save node writes ``videos``; older graphs surface the file under ``gifs`` or ``images``. A load node can echo the
    uploaded source under ``images``, so the output directory is preferred over history order, which is not stable.
    """
    outputs = (job or {}).get("outputs", {}) or {}
    fallback = None
    for _node_id, node_output in outputs.items():
        for key in VIDEO_KEYS:
            entries = node_output.get(key) or []
            for entry in entries:
                filename = entry.get("filename") or ""
                if filename.lower().endswith(VIDEO_SUFFIXES):
                    candidate = {
                        "filename": filename,
                        "subfolder": entry.get("subfolder", ""),
                        "type": entry.get("type", "output"),
                    }
                    if candidate["type"] == "output":
                        return candidate
                    if fallback is None:
                        fallback = candidate
    return fallback


def first_audio_output(job: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The first audio file a finished prompt produced."""
    outputs = (job or {}).get("outputs", {}) or {}
    for _node_id, node_output in outputs.items():
        for key in AUDIO_KEYS:
            entries = node_output.get(key) or []
            for entry in entries:
                if isinstance(entry, dict):
                    return {
                        "filename": entry.get("filename"),
                        "subfolder": entry.get("subfolder", ""),
                        "type": entry.get("type", "output"),
                    }
    return None


def build_submit_payload(workflow: dict, ui_for: Callable[[Mapping[str, Any]], dict | None], **extra: Any) -> dict:
    """The JSON body of a prompt, with the UI-format graph embedded in its output metadata when one is known.

    Pops the gateway-private ``__ui_workflow__`` key off ``workflow`` (added by the workflow loaders). The embedded graph
    is derived from the graph actually submitted, so it shows this request's prompt, seed and sizes; the hand-authored
    twin is the fallback only. Without either, outputs carry no graph and cannot be re-opened in the engine's editor.
    """
    twin = workflow.pop("__ui_workflow__", None)
    ui = ui_for(workflow) or twin
    payload: dict[str, Any] = {"prompt": workflow}
    if ui:
        payload["extra_data"] = {"extra_pnginfo": {"workflow": ui}}
    payload.update(extra)
    return payload


def encode_output(kind: str, entry: Mapping[str, Any]) -> str:
    return json.dumps({"kind": kind, "filename": entry.get("filename"), "subfolder": entry.get("subfolder", ""),
                       "type": entry.get("type", "output")}, sort_keys=True)


def decode_output(handle: str) -> dict[str, Any]:
    try:
        decoded = json.loads(handle)
    except ValueError as error:
        raise MalformedResponse("An output handle is not one this adapter issued.") from error
    if not isinstance(decoded, dict):
        raise MalformedResponse("An output handle is not one this adapter issued.")
    return decoded


# ---------------------------------------------------------------------------
# The wire: one httpx client, one method per operation
# ---------------------------------------------------------------------------


class GraphSession:
    """One httpx client bound to one engine connection. Every method issues the call the gateway always made."""

    def __init__(self, conn: Connection, client: Any) -> None:
        self.conn = conn
        self.client = client

    def _url(self, path: str) -> str:
        return f"{self.conn.address}{path}"

    async def _send(self, call: Any, path: str, *, want_text: bool = False, **kwargs: Any) -> WireReply:
        try:
            response = await call(self._url(path), **kwargs)
        except httpx.HTTPError as error:
            raise failure(error) from error
        return reply_from(response, want_text=want_text)

    async def submit(self, payload: Mapping[str, Any], *, headers: Any = UNSET, timeout: Any = UNSET) -> WireReply:
        return await self._send(self.client.post, "/prompt", want_text=True, **supplied(headers=headers, json=payload, timeout=timeout))

    async def history(self, prompt_id: str, *, headers: Any = UNSET, timeout: Any = UNSET) -> WireReply:
        return await self._send(self.client.get, f"/history/{prompt_id}", **supplied(headers=headers, timeout=timeout))

    async def history_page(self, max_items: int) -> WireReply:
        return await self._send(self.client.get, f"/history?max_items={max_items}")

    async def queue(self, *, headers: Any = UNSET, timeout: Any = UNSET) -> WireReply:
        return await self._send(self.client.get, "/queue", **supplied(headers=headers, timeout=timeout))

    async def view(self, params: Mapping[str, Any], *, headers: Any = UNSET) -> WireReply:
        return await self._send(self.client.get, "/view", **supplied(headers=headers, params=params))

    @asynccontextmanager
    async def view_stream(self, params: Mapping[str, Any], *, headers: Any = UNSET) -> AsyncIterator[Any]:
        """The file read as a stream the caller bounds. Yields the response; a failure to start is a ``TransportFailure``."""
        try:
            async with self.client.stream("GET", self._url("/view"), **supplied(headers=headers, params=params)) as response:
                yield response
        except httpx.HTTPError as error:
            raise failure(error) from error

    async def upload(self, files: Mapping[str, Any], *, headers: Any = UNSET) -> WireReply:
        return await self._send(self.client.post, "/upload/image", want_text=True, **supplied(headers=headers, files=files))

    async def cancel(self, prompt_id: str, *, headers: Any = UNSET, timeout: Any = UNSET) -> WireReply:
        return await self._send(self.client.post, f"/api/jobs/{prompt_id}/cancel",
                                **supplied(headers=headers, timeout=timeout))

    async def delete_history(self, prompt_ids: Sequence[str], *, headers: Any = UNSET) -> WireReply:
        return await self._send(self.client.post, "/history",
                                **supplied(headers=headers, json={"delete": list(prompt_ids)}))

    async def object_info(self, class_type: str | None = None) -> WireReply:
        """The node definitions of one class, or of every class."""
        return await self._send(self.client.get, "/object_info" if class_type is None else f"/object_info/{quote(class_type, safe='')}")

    async def stats(self, *, timeout: Any = UNSET) -> WireReply:
        return await self._send(self.client.get, "/system_stats", **supplied(timeout=timeout))

    async def free(self, body: Mapping[str, Any], *, timeout: Any = UNSET) -> WireReply:
        return await self._send(self.client.post, "/free", **supplied(json=body, timeout=timeout))


def _headers_of(request: CanonicalRequest) -> Any:
    """The headers the caller prepared, passed even when empty: the older call always named ``headers=``."""
    return dict(request.headers)


class NodeGraphAdapter:
    interface_version = ADAPTER_INTERFACE_VERSION

    def __init__(self, description: ProtocolDescription | None = None, *, assets: AssetStore | None = None) -> None:
        self.label = (description.label if description is not None else "") or _DEFAULT_LABEL
        # The key under a workflow manifest's ``bindings`` that holds this engine's graph ("" when the description names none).
        self.binding = description.binding if description is not None else ""
        self._assets = assets

    # --- sessions ---------------------------------------------------------------------------

    @asynccontextmanager
    async def open(self, conn: Connection, **client_kwargs: Any) -> AsyncIterator[GraphSession]:
        """One client for several requests. ``client_kwargs`` go to ``httpx.AsyncClient`` as given (timeout, verify)."""
        if "timeout" not in client_kwargs:
            client_kwargs["timeout"] = httpx.Timeout(conn.timeouts.first_byte_s, connect=conn.timeouts.connect_s)
        async with httpx.AsyncClient(**client_kwargs) as client:
            yield GraphSession(conn, client)

    def attach(self, conn: Connection, client: Any) -> GraphSession:
        """A session over a client the caller already holds, so its requests go through this adapter."""
        return GraphSession(conn, client)

    @asynccontextmanager
    async def _use(self, conn: Connection, session: GraphSession | None) -> AsyncIterator[GraphSession]:
        if session is not None:
            yield session
            return
        async with self.open(conn) as opened:
            yield opened

    # --- caller-facing messages (the engine's name is data) -----------------------------------

    def unavailable_message(self, conn: Connection) -> str:
        return f"{self.label} unavailable at {log_scrub.describe_origin(conn.address)}"

    # --- the interface ----------------------------------------------------------------------

    async def health(self, conn: Connection) -> Health:
        try:
            async with self._use(conn, None) as session:
                reply = await session.stats(timeout=CONTROL_TIMEOUT_S)
        except TransportFailure as error:
            return Health("failing", f"{error.kind}: {error}", "graph.stats")  # a TransportFailure message is ours
        if reply.status == 200:
            return Health("ok", "", "graph.stats")
        return Health("failing", f"HTTP {reply.status}", "graph.stats")

    async def inventory(self, conn: Connection) -> Inventory:
        async with self._use(conn, None) as session:
            reply = await session.object_info()
        if reply.status != 200:
            raise UpstreamError(reply.status, f"The node list answered HTTP {reply.status}.")
        body = reply.json()
        if not isinstance(body, dict):
            raise MalformedResponse("The node list is not an object.")
        return Inventory(tuple(sorted(str(name) for name in body)), "graph.object_info")

    async def invoke(self, conn: Connection, op: str, request: CanonicalRequest) -> Reply | StreamReply:
        if op not in OPERATIONS:
            raise UnknownOperation(f"The node-graph adapter has no operation '{op}'.")
        params = request.params
        headers = _headers_of(request)
        timeout = params.get("timeout", UNSET)
        async with self._use(conn, params.get("session")) as session:
            if op == "graph.submit":
                return await session.submit(request.body, headers=headers, timeout=timeout)
            if op == "graph.history":
                return await session.history(params["id"], headers=headers, timeout=timeout)
            if op == "graph.history_page":
                return await session.history_page(int(params["max_items"]))
            if op == "graph.queue":
                return await session.queue(headers=headers, timeout=timeout)
            if op == "graph.view":
                return await session.view(params["query"], headers=headers)
            if op == "graph.upload":
                return await session.upload(params["files"], headers=headers)
            if op == "graph.cancel":
                return await session.cancel(params["id"], headers=headers, timeout=timeout)
            if op == "graph.delete_history":
                return await session.delete_history(params["ids"], headers=headers)
            if op == "graph.stats":
                return await session.stats(timeout=timeout)
            return await session.free(request.body, timeout=timeout)

    async def submit(self, conn: Connection, op: str, request: CanonicalRequest) -> JobRef:
        """Post a prompt. ``request.body`` is the finished payload; ``request.params['session']`` may share a client."""
        if op != "graph.submit":
            raise UnknownOperation(f"The node-graph adapter submits only 'graph.submit', not '{op}'.")
        reply = await self.invoke(conn, op, request)
        assert isinstance(reply, WireReply)
        if reply.status != 200:
            raise UpstreamError(reply.status, f"The prompt answered HTTP {reply.status}.", body=reply.raw or b"", excerpt=reply.text or "")
        prompt_id = reply.json().get("prompt_id")
        if not prompt_id:
            raise MalformedResponse("No prompt_id returned")
        return JobRef(op, str(prompt_id))

    async def poll(self, conn: Connection, ref: JobRef, *, headers: Any = UNSET, session: GraphSession | None = None,
                   timeout: Any = UNSET, graph: Mapping[str, Any] | None = None) -> JobStatus:
        """One look at the prompt's history entry.

        ``graph`` is the prompt the gateway submitted: a failure names a node only when it is in that graph (see
        ``execution_error_message``).

        An entry that is not there yet is ``pending``. A non-200 answer is an ``UpstreamError`` and a failure to reach
        the engine is a ``TransportFailure``; core decides whether to look again.
        """
        async with self._use(conn, session) as use:
            reply = await use.history(ref.id, headers=headers, timeout=timeout)
        if reply.status != 200:
            raise UpstreamError(reply.status, f"The history answered HTTP {reply.status}.")
        history = reply.json()
        if ref.id not in history:
            return JobStatus(ref, JobState.pending)
        job = history[ref.id]
        state, error = history_state(job, self.label, graph)
        if state == "failed":
            return JobStatus(ref, JobState.failed, error)
        if state == "running":
            return JobStatus(ref, JobState.running)
        return JobStatus(ref, JobState.succeeded, assets=self._handles(job))

    @staticmethod
    def _handles(job: Mapping[str, Any]) -> tuple[str, ...]:
        """One handle per output file, in node order, naming the kind of output it came from."""
        handles: list[str] = []
        for node_output in ((job or {}).get("outputs", {}) or {}).values():
            if not isinstance(node_output, Mapping):
                continue
            for key in OUTPUT_KEYS:
                if key in node_output:
                    handles.extend(encode_output(key, entry) for entry in node_output[key] if isinstance(entry, Mapping))
        return tuple(handles)

    async def events(self, conn: Connection, ref: JobRef, *, client_id: str | None = None) -> AsyncIterator[JobEvent]:
        async for event in self.watch(conn, client_id):
            if not isinstance(event, dict):
                continue
            data = event.get("data") or {}
            if data.get("prompt_id") != ref.id:
                continue
            job_event = job_event_from(ref, event)
            if job_event is not None:
                yield job_event
                if job_event.state in (JobState.succeeded, JobState.failed, JobState.cancelled):
                    return

    async def watch(self, conn: Connection, client_id: str | None = None, *,
                    on_open: Callable[[str], None] | None = None,
                    stop: Callable[[], bool] | None = None) -> AsyncIterator[Any]:
        """The engine's push channel as decoded JSON frames, for one connection's lifetime. Raises when it drops.

        Binary frames (previews) are skipped and text frames that are not JSON are ignored; a JSON frame is passed on
        whatever it holds, and the caller decides what a frame that is not an object means. ``on_open`` is called with
        the channel's URL once it is open, before any frame, so a caller can say it is connected. ``stop`` is asked
        after every frame, binary ones included, and ends the channel when it answers true.
        """
        try:
            from websockets.asyncio.client import connect as ws_connect
        except Exception as error:  # noqa: BLE001 - the optional dependency is absent
            raise UnsupportedByAdapter(f"The push channel needs the websockets library ({error}).") from error
        url = conn.address.replace("http://", "ws://").replace("https://", "wss://")
        url = f"{url}/ws" + (f"?clientId={client_id}" if client_id else "")
        async with ws_connect(url, max_size=8 * 1024 * 1024, ping_interval=20, ping_timeout=20) as socket:
            if on_open is not None:
                on_open(url)
            async for raw in socket:
                if stop is not None and stop():
                    break
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    event = json.loads(raw)
                except Exception:  # noqa: BLE001 - a frame that is not JSON says nothing
                    continue
                yield event

    async def cancel(self, conn: Connection, ref: JobRef, *, headers: Any = UNSET, session: GraphSession | None = None,
                     timeout: Any = CONTROL_TIMEOUT_S) -> None:
        async with self._use(conn, session) as use:
            reply = await use.cancel(ref.id, headers=headers, timeout=timeout)
        if reply.status != 200:
            raise UpstreamError(reply.status, f"The cancel answered HTTP {reply.status}.")

    async def recover(self, conn: Connection, ref: JobRef, *, headers: Any = UNSET,
                      session: GraphSession | None = None) -> JobStatus | None:
        """After a restart: the prompt's status from history, else ``pending`` or ``running`` from the queue, else ``None``."""
        async with self._use(conn, session) as use:
            status = await self.poll(conn, ref, headers=headers, session=use)
            if status.state is not JobState.pending:
                return status
            reply = await use.queue(headers=headers)
        if reply.status != 200:
            return None
        try:
            queue = reply.json()
        except ValueError:
            return None
        if not isinstance(queue, dict):
            return None
        if ref.id in queue_prompt_ids(queue.get("queue_running")):
            return JobStatus(ref, JobState.running)
        if ref.id in queue_prompt_ids(queue.get("queue_pending")):
            return JobStatus(ref, JobState.pending)
        return None

    async def fetch_outputs(self, conn: Connection, status: JobStatus, *, kinds: Sequence[str] = IMAGE_KEYS,
                            headers: Any = UNSET, session: GraphSession | None = None) -> list[bytes]:
        """The bytes of every output file of the given kinds, in order. A file that cannot be read is skipped."""
        out: list[bytes] = []
        async with self._use(conn, session) as use:
            for handle in status.assets:
                info = decode_output(handle)
                if info.get("kind") not in kinds:
                    continue
                try:
                    reply = await use.view(view_params(info), headers=headers)
                except TransportFailure:
                    continue
                if reply.status == 200 and reply.raw:
                    out.append(reply.raw)
        return out

    async def fetch_assets(self, conn: Connection, ref: JobRef, status: JobStatus) -> tuple[AssetRef, ...]:
        if self._assets is None:
            raise UnsupportedByAdapter("This adapter was built without a media store, so it cannot save assets.")
        saved: list[AssetRef] = []
        async with self._use(conn, None) as use:
            for handle in status.assets:
                info = decode_output(handle)
                reply = await use.view(view_params(info))
                if reply.status != 200 or not reply.raw:
                    raise UpstreamError(reply.status, "An output file could not be read.")
                content_type = served_media_type(reply.headers.get("content-type"), reply.raw[:16])
                if content_type is None:
                    raise MalformedResponse("An output file is not a media type that is served.")

                async def chunks(data: bytes = reply.raw) -> AsyncIterator[bytes]:
                    yield data

                saved.append(await self._assets.save(chunks(), content_type=content_type, max_bytes=len(reply.raw),
                                                     name=str(info.get("filename"))))
        return tuple(saved)


def job_event_from(ref: JobRef, event: Mapping[str, Any]) -> JobEvent | None:
    """A typed event for one pushed message about ``ref``, or ``None`` for messages that change nothing."""
    kind = event.get("type")
    data = event.get("data") or {}
    if kind == "execution_start":
        return JobEvent(ref, "status", JobState.running)
    if kind == "executing":
        if data.get("node") is None:
            return JobEvent(ref, "status", JobState.succeeded)
        return JobEvent(ref, "progress", JobState.running, detail=str(data.get("node")))
    if kind == "progress":
        total = data.get("max") or 0
        value = data.get("value") or 0
        return JobEvent(ref, "progress", JobState.running, progress=(float(value) / float(total)) if total else None,
                        detail=None if data.get("node") is None else str(data.get("node")))
    if kind == "execution_success":
        return JobEvent(ref, "status", JobState.succeeded)
    if kind == "execution_error":
        return JobEvent(ref, "status", JobState.failed, detail="execution failed")  # the engine's own message is not repeated (D45b)
    if kind == "execution_interrupted":
        return JobEvent(ref, "status", JobState.cancelled)
    return None


__all__ = [
    "ADAPTER_ID", "CONTROL_TIMEOUT_S", "GraphSession", "NodeGraphAdapter", "build_submit_payload", "decode_output",
    "encode_output", "execution_error_message", "execution_error_node_id", "first_audio_output", "first_video_output",
    "history_state", "job_event_from", "output_entries", "queue_position", "queue_prompt_ids", "view_params",
]

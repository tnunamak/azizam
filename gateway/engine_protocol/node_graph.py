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
the media store. ``fetch_selected`` returns the selected files as bytes for
callers that store them themselves.

A caller that makes several requests for one job opens one session
(``adapter.open``) and passes it to the job methods, so the requests share one
connection, as one client did before.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Sequence
from urllib.parse import quote

import httpx

from gateway import log_scrub

from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION_V2, AdapterOrigin
from gateway.engine_protocol.assets import AssetStore, served_media_type
from gateway.engine_protocol.description import ProtocolDescription
from gateway.engine_protocol.errors import (
    MalformedResponse, ProtocolError, SelectionRefused, TransportFailure, UnknownOperation, UnsupportedByAdapter, UpstreamError,
)
from gateway.engine_protocol.node_validation import validate_workflow_graph
from gateway.engine_protocol.httpx_wire import UNSET, WireReply, failure, reply_from, supplied
from gateway.engine_protocol.types import (
    AssetRef, AssetsResult, CanonicalRequest, Connection, Health, Inventory, JobEvent, JobRef, JobState, JobStatus, JsonResult,
    Pending, Reply, ResultBundle, ResultContext, StreamReply,
)
from gateway.manifests import bind_backend_request, validate_inputs
from gateway import binding_registry, engine_egress, output_selectors

ADAPTER_ID = "node-graph"
OPERATIONS = (
    "graph.submit", "graph.history", "graph.history_page", "graph.queue", "graph.view", "graph.upload", "graph.cancel",
    "graph.delete_history", "graph.stats", "graph.free",
)
# The operation that posts a finished graph. Any other operation is a manifest-backed workflow: its request carries the
# manifest in ``params`` and the typed inputs in ``body``, and the adapter compiles the graph (``compile_workflow``).
GRAPH_SUBMIT = "graph.submit"
CONTROL_TIMEOUT_S = 5.0          # the read timeout of a status look or a cancel; callers may pass their own
OUTPUT_KEYS = ("images", "videos", "gifs", "audios", "audio", "wav", "mp3")
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
# Which files are a workflow's results (pure; no I/O)
# ---------------------------------------------------------------------------
#
# A binding's selectors say where each named result is: a node of the graph and the file list under it in the history
# entry. The rules below are the adapter's, and each has a test:
#
# - Only files in the output directory count (``type`` is ``output``; an entry with no type is one). A load node's echo
#   of its source (``input``) and a preview (``temp``) are never results.
# - A node, a list or an entry that is missing, ``null`` or empty, or that holds no output-directory file, is the same
#   thing: the result is missing, and the run is refused. Nothing fills the gap.
# - Order is the order of the history. The same file listed twice is kept once, at its first place.
# - ``take: all`` is the whole list, ``first`` is its first file, ``single`` is the file and refuses a list of more.
# - An entry that is not an object, or a name that is not a plain file name or folder, is refused.


_SELECTOR_SOURCES = {"output_dir": "output"}   # a selector's ``from`` value, as the history's ``type``


def _plain_name(value: Any, *, folder: bool) -> bool:
    if not isinstance(value, str):
        return False
    if folder and value == "":
        return True
    if "\\" in value or (not folder and "/" in value):
        return False
    return bool(value) and all(part not in ("", ".", "..") for part in value.split("/"))


def _output_files(entries: Any, directory_type: str, *, result: str) -> list[dict[str, Any]]:
    """The output-directory files of one history list, in order, each once. Raises ``SelectionRefused`` for an entry that
    is not an object or names a path that leaves its folder."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise SelectionRefused("result_malformed", f"the file list for result {result} is not a list")
    files: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise SelectionRefused("result_malformed", f"an entry for result {result} is not an object")
        if entry.get("type", "output") != directory_type:
            continue
        filename, subfolder = entry.get("filename"), entry.get("subfolder", "") or ""
        if not _plain_name(filename, folder=False) or not _plain_name(subfolder, folder=True):
            raise SelectionRefused("result_malformed", f"an entry for result {result} names a file that cannot be read")
        if (filename, subfolder) in seen:
            continue
        seen.add((filename, subfolder))
        files.append({"filename": filename, "subfolder": subfolder, "type": directory_type})
    return files


def _history_outputs(job: Mapping[str, Any] | None) -> Mapping[str, Any]:
    outputs = (job or {}).get("outputs") if isinstance(job, Mapping) else None
    return outputs if isinstance(outputs, Mapping) else {}


def select_outputs(job: Mapping[str, Any] | None, selectors: Mapping[str, Mapping[str, str]]) -> dict[str, tuple[dict[str, Any], ...]]:
    """The files of each named result, by its selector, from one history entry. Raises ``SelectionRefused``."""
    history = _history_outputs(job)
    chosen: dict[str, tuple[dict[str, Any], ...]] = {}
    for name, selector in selectors.items():
        node_output = history.get(selector["node"])
        entries = node_output.get(selector["key"]) if isinstance(node_output, Mapping) else None
        files = _output_files(entries, _SELECTOR_SOURCES[selector["from"]], result=name)
        if not files:
            raise SelectionRefused("result_missing", f"result {name} is not in the engine's answer")
        if selector["take"] == "single" and len(files) != 1:
            raise SelectionRefused("result_ambiguous", f"result {name} is one file, but the engine listed {len(files)}")
        chosen[name] = tuple(files[:1] if selector["take"] in ("first", "single") else files)
    return chosen


def graph_selector_cardinality(selector: Mapping[str, str]) -> tuple[int, int | None]:
    """How many items a selector yields when it succeeds (the reviewed ``selector_cardinality`` hook, D5): ``first`` and
    ``single`` yield one file, ``all`` yields one or more. A ``json`` result is one document whatever ``take`` says,
    because ``results`` refuses two JSON files even for ``take: all``. ``None``: no bound of its own."""
    if selector.get("decode") == "json" or selector.get("take") in ("first", "single"):
        return (1, 1)
    return (1, None)


# ---------------------------------------------------------------------------
# Compiling a manifest-backed workflow (pure; no I/O)
# ---------------------------------------------------------------------------


class InputRefused(ProtocolError):
    """The typed inputs do not satisfy the manifest. ``param`` is the input's name."""

    code = "invalid_request"

    def __init__(self, message: str, *, param: str) -> None:
        super().__init__(message)
        self.param = param


def compile_workflow(manifest: Mapping[str, Any], binding: str, template: Mapping[str, Any],
                     inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The API-format graph for one manifest-backed request: fill defaults, validate, then bind.

    The one compile path for every graph-bound workflow. Declared defaults fill the inputs not given. The result is checked
    against the manifest's declared inputs, and an unknown or out-of-range input is refused by name. The binding map then
    writes each accepted input to the nodes it names. An accepted input that the map does not write (no mapping, or an
    empty one) would have no effect, so it is refused by name too. An input the binding marks ``edge_only_inputs`` is
    applied by the route outside the graph: it gets no default here and is refused if supplied. The template's ``_meta``
    is not read: the manifest's map is the one source of where an input goes. ``manifest``, ``template`` and ``inputs``
    are not modified.
    """
    block = (manifest.get("bindings") or {}).get(binding)
    edge_only = set(block.get("edge_only_inputs") or []) if isinstance(block, Mapping) else set()
    for name in inputs:
        if name in edge_only:
            raise InputRefused(f"{name} is applied by the route, not by the graph", param=name)
    # The graph's view of the manifest: only the selected binding, without its edge-only inputs, so they get no default
    # and are not required. Other bindings are left out, so their own markers cannot make this view invalid.
    # Selectors say how results are read, not how inputs are bound; they were checked when the manifest loaded.
    selected = ({key: value for key, value in block.items() if key not in ("edge_only_inputs", "selectors")}
                if isinstance(block, Mapping) else block)
    graph_manifest = dict(manifest, bindings={binding: selected},
                          inputs={name: spec for name, spec in manifest["inputs"].items() if name not in edge_only})
    supplied = {name: spec["default"] for name, spec in graph_manifest["inputs"].items()
                if "default" in spec and name not in inputs}
    supplied.update(inputs)
    checked = validate_inputs(graph_manifest, supplied)
    if checked.rejected:
        name = next(iter(checked.rejected))
        raise InputRefused(checked.rejected[name], param=name)
    bound = bind_backend_request(graph_manifest, binding, dict(template), checked.accepted)
    if bound.ignored_inputs:
        name = next(iter(bound.ignored_inputs))
        raise InputRefused(f"{name} has no effect: {bound.ignored_inputs[name]}", param=name)
    graph = bound.request
    graph.pop("_meta", None)
    return graph


# ---------------------------------------------------------------------------
# The wire: one httpx client, one method per operation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FinishedRecord:
    """The history entry a poll read when it found the prompt finished, and the size of the answer it came from."""

    job: Mapping[str, Any]
    size: int


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
    interface_version = ADAPTER_INTERFACE_VERSION_V2

    def __init__(self, description: ProtocolDescription | None = None, *, assets: AssetStore | None = None,
                 workflow_root: Path | None = None, client_id: str | None = None,
                 derive_ui: Callable[[Mapping[str, Any]], dict | None] | None = None) -> None:
        self.label = (description.label if description is not None else "") or _DEFAULT_LABEL
        # What this adapter was built from (None: it was built without a description, so it cannot run a pinned job).
        self.built_from = AdapterOrigin.of(description) if description is not None else None
        # The key under a workflow manifest's ``bindings`` that holds this engine's graph ("" when the description names none).
        self.binding = description.binding if description is not None else ""
        # The selector form this engine's bindings take, declared in its protocol description (None: it takes none).
        self.result_selectors = description.result_selectors if description is not None else None
        # The run operations' poll cadence, as the description declares it (empty: built without a description).
        self._operations = description.operations if description is not None else {}
        self._assets = assets
        # Where a manifest's graph path is resolved. Without it a manifest-backed request cannot compile.
        self._root = workflow_root
        # Session id the engine uses to route its push messages; one per adapter, as the proxy had one per process.
        self.client_id = client_id or uuid.uuid4().hex
        # The UI-format twin embedded in outputs. None means the outputs carry no embedded graph.
        self._derive_ui = derive_ui or (lambda _graph: None)

    # --- sessions ---------------------------------------------------------------------------

    @asynccontextmanager
    async def open(self, conn: Connection, **client_kwargs: Any) -> AsyncIterator[GraphSession]:
        """One client for several requests. ``client_kwargs`` go to ``httpx.AsyncClient`` as given (timeout, verify)."""
        if "timeout" not in client_kwargs:
            client_kwargs["timeout"] = httpx.Timeout(conn.timeouts.first_byte_s, connect=conn.timeouts.connect_s)
        async with engine_egress.async_client(**client_kwargs) as client:
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

    def poll_for(self, op: str) -> Any:
        """The poll cadence the description declares for run operation ``op`` (a ``description.Poll``)."""
        declared = self._operations.get(op)
        poll = getattr(declared, "poll", None)
        if poll is None:
            raise UnknownOperation(f"The node-graph description declares no poll cadence for '{op}'.")
        return poll

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

    def validate(self, conn: Connection, op: str, request: CanonicalRequest) -> None:
        """The local checks ``submit`` makes before any I/O. A manifest-backed request is compiled here, so a refusal
        happens before the caller records that a submit was attempted. The compiled graph is discarded: ``submit``
        compiles the request it is then given, so a change made in between is never missed."""
        if op != GRAPH_SUBMIT:
            self._workflow_payload(op, request)

    async def submit(self, conn: Connection, op: str, request: CanonicalRequest) -> JobRef:
        """Post a prompt. For ``graph.submit``, ``request.body`` is the finished payload. For any other operation,
        ``request.params['manifest']`` is the workflow's manifest and ``request.body`` its typed inputs; the graph is
        compiled from them as they are now. ``request.params['session']`` may share a client, and
        ``request.params['timeout']`` bounds the post."""
        if op == GRAPH_SUBMIT:
            reply = await self.invoke(conn, op, request)
        else:
            payload = self._workflow_payload(op, request)
            transport = {name: request.params[name] for name in ("session", "timeout") if name in request.params}
            reply = await self.invoke(conn, GRAPH_SUBMIT, CanonicalRequest(op=GRAPH_SUBMIT, body=payload,
                                                                          headers=request.headers, params=transport))
        assert isinstance(reply, WireReply)
        if reply.status != 200:
            raise UpstreamError(reply.status, f"The prompt answered HTTP {reply.status}.", body=reply.raw or b"", excerpt=reply.text or "")
        prompt_id = reply.json().get("prompt_id")
        if not prompt_id:
            raise MalformedResponse("No prompt_id returned")
        return JobRef(op, str(prompt_id))

    def _workflow_payload(self, op: str, request: CanonicalRequest) -> dict[str, Any]:
        """The prompt body of a manifest-backed request, compiled now from the request: the graph, this adapter's client
        id and any UI twin. Nothing is cached on the request, so the body is read at the moment it is sent."""
        manifest = request.params.get("manifest")
        if not isinstance(manifest, Mapping):
            raise UnknownOperation(f"The node-graph adapter submits {GRAPH_SUBMIT}, or an operation that carries a "
                                   f"manifest; '{op}' carries none.")
        pinned = request.params.get("template")
        template = self._pinned_template(pinned) if pinned is not None else self._template(manifest)
        graph = compile_workflow(manifest, self.binding, template, request.body)
        return build_submit_payload(graph, self._derive_ui, client_id=self.client_id)

    @staticmethod
    def _pinned_template(template: Any) -> dict[str, Any]:
        """A graph template the caller pinned (``request.params['template']``), checked as a file would be. It replaces the
        read of the manifest's graph path, so a run compiles from the bytes it was accepted with."""
        errors = validate_workflow_graph(template) if isinstance(template, Mapping) else ["the template is not an object"]
        if errors:
            raise ProtocolError("The pinned graph is not a valid node graph: " + "; ".join(errors), code="workflow_invalid")
        return dict(template)

    def _template(self, manifest: Mapping[str, Any] | None) -> dict[str, Any]:
        """The graph template the manifest's binding names, read from under the workflow root and validated."""
        block = ((manifest or {}).get("bindings") or {}).get(self.binding)
        relative = block.get("workflow") if isinstance(block, Mapping) else None
        if not isinstance(relative, str) or not relative:
            raise ProtocolError("The manifest names no graph for this engine.", code="workflow_unavailable")
        if self._root is None:
            raise ProtocolError("This adapter has no workflow root, so it cannot read a manifest's graph.",
                                code="workflow_unavailable")
        root = self._root.resolve()
        path = (root / relative).resolve()
        if root not in path.parents:
            raise ProtocolError("The manifest's graph path leaves the workflow root.", code="workflow_unavailable")
        try:
            template = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ProtocolError("The manifest's graph could not be read.", code="workflow_unavailable") from None
        errors = validate_workflow_graph(template)
        if errors:
            raise ProtocolError("The manifest's graph is not a valid node graph: " + "; ".join(errors),
                                code="workflow_invalid")
        return template

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
        return JobStatus(ref, JobState.succeeded, assets=self._handles(job),
                         record=FinishedRecord(job, len(reply.raw or b"")))

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
        """The pushed events about ``ref``. The push channel is the client id the submitted payload carries, by default."""
        async for event in self.watch(conn, client_id or self.client_id):
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

    async def fetch_selected(self, conn: Connection, status: JobStatus, block: Mapping[str, Any], outputs: Mapping[str, Any], *,
                             limits: Any, headers: Any = UNSET,
                             session: GraphSession | None = None) -> dict[str, list[bytes]]:
        """The bytes of each named result of a finished prompt, for a route that delivers files itself.

        ``status`` is the ``succeeded`` status that ``poll`` returned: the history entry it read is collected by the
        binding's selectors (``collect``), so no second look at the engine is made. The reads go through
        ``output_selectors.FETCH_GATE`` and are bounded while they are read: the number of files, the bytes of one file and
        the total are the deployment's ``limits`` (D6). A file that cannot be read is left out. A result of kind ``json`` is refused: a route that takes bytes has no place for a value.
        Raises ``SelectionRefused`` (a result that is missing, ambiguous, malformed or over a limit, or a fetch that waited too long).
        """
        record = status.record
        if not isinstance(record, FinishedRecord):
            raise SelectionRefused("result_missing", "the status holds no history entry to read the results from")
        picked = self.collect(record.job, block, outputs, limits=limits, history_bytes=record.size)
        if any(kind != "assets" for kind in self._result_kinds(block, outputs, picked).values()):
            raise SelectionRefused("result_wrong_type", "this route delivers files, and a result of this workflow is a value")
        if sum(len(files) for files in picked.values()) > limits.max_assets:
            raise SelectionRefused("too_many_assets", f"a result holds more than {limits.max_assets} files")
        chosen: dict[str, list[bytes]] = {}
        total = 0
        try:
            async with output_selectors.FETCH_GATE.slot(limits), self._use(conn, session) as use:
                for name, files in picked.items():
                    chosen[name] = []
                    for file in files:
                        room = min(limits.max_asset_bytes, limits.max_total_bytes - total)
                        data = await self._read_bounded(use, file, room, headers, limits)
                        if data:
                            total += len(data)
                            chosen[name].append(data)
        except output_selectors.FetchRefused as refusal:
            raise SelectionRefused(refusal.code, str(refusal)) from refusal
        return chosen

    async def fetch_file(self, conn: Connection, file: Mapping[str, Any], *, limits: Any, headers: Any = UNSET,
                         session: GraphSession | None = None) -> bytes:
        """The bytes of one file that ``collect`` chose, for a route that delivers a single file (a video).

        The read goes through ``output_selectors.FETCH_GATE`` and is bounded while it is read, as ``fetch_selected`` is: a
        file past the deployment's ``max_asset_bytes`` (or ``max_total_bytes``) is refused at the first byte over, and is
        never held whole. A reply that is not HTTP 200 is an ``UpstreamError``. Raises ``SelectionRefused`` for a limit or
        a fetch that waited too long."""
        room = min(limits.max_asset_bytes, limits.max_total_bytes)
        try:
            async with output_selectors.FETCH_GATE.slot(limits), self._use(conn, session) as use:
                async with use.view_stream(view_params(file), headers=headers) as reply:
                    if reply.status_code != 200:
                        raise UpstreamError(reply.status_code, f"A result file answered HTTP {reply.status_code}.")
                    data = bytearray()
                    async for chunk in reply.aiter_bytes():
                        if len(data) + len(chunk) > room:
                            raise SelectionRefused("asset_too_large" if room == limits.max_asset_bytes else "total_too_large",
                                                   f"a result file is larger than {room} bytes")
                        data.extend(chunk)
                    return bytes(data)
        except output_selectors.FetchRefused as refusal:
            raise SelectionRefused(refusal.code, str(refusal)) from refusal

    @staticmethod
    async def _read_bounded(session: GraphSession, file: Mapping[str, Any], room: int, headers: Any, limits: Any) -> bytes | None:
        """One file, read as it arrives and refused at the first byte past ``room``. ``None``: the engine did not give it."""
        try:
            async with session.view_stream(view_params(file), headers=headers) as reply:
                if reply.status_code != 200:
                    return None
                data = bytearray()
                async for chunk in reply.aiter_bytes():
                    if len(data) + len(chunk) > room:
                        raise SelectionRefused("asset_too_large" if room == limits.max_asset_bytes else "total_too_large",
                                               f"a result file is larger than {room} bytes")
                    data.extend(chunk)
                return bytes(data) or None
        except TransportFailure:
            return None

    def collect(self, job: Mapping[str, Any] | None, block: Mapping[str, Any], outputs: Mapping[str, Any], *, limits: Any,
                history_bytes: int | None = None, count: bool = True) -> dict[str, tuple[dict[str, Any], ...]]:
        """The files of each named result of a finished prompt, by the binding's selectors.

        ``block`` is the binding, ``outputs`` the contract's declared results, ``limits`` the deployment's result limits
        and ``history_bytes`` the size of the history answer that ``job`` came from. A binding with no selectors is
        refused (``selectors_missing``): the engine's answer is never read by guessing. With ``count`` (the default)
        each result's selected files are checked against its declared count (``result_count``); ``results`` passes
        ``count=False`` and checks the count after the media family. Raises ``SelectionRefused``; a refused run delivers
        nothing.
        """
        if history_bytes is not None and history_bytes > limits.max_history_bytes:
            raise SelectionRefused("history_too_large", f"the engine's answer is larger than {limits.max_history_bytes} bytes")
        if block.get("selectors") is None:  # an explicit null declares nothing, as an absent key does
            raise SelectionRefused("selectors_missing", "this workflow declares no output selectors, so its result cannot be read")
        if self.result_selectors is None:
            raise SelectionRefused("selectors_unsupported", "this engine's description takes no output selectors")
        try:
            selectors = output_selectors.check_binding(self.result_selectors, outputs, block, max_selectors=limits.max_selectors)
        except output_selectors.SelectorError as exc:
            raise SelectionRefused(exc.code, str(exc)) from exc
        chosen = select_outputs(job, selectors)
        if not count:
            # ``results`` checks the count itself, after the media family, so a file of the wrong family stays
            # ``result_wrong_type`` and is not reported as a short count.
            return chosen
        for name, files in chosen.items():
            # A JSON result is one document, however many files were listed: two files stay ``result_ambiguous`` in
            # ``results``. The count of an ``assets`` result is the files kept after filtering, de-duplication and ``take``.
            kept = min(len(files), 1) if self.result_selectors.kind_of(selectors[name]) == "json" else len(files)
            output_selectors.check_count(outputs.get(name), kept, name=name, max_assets=limits.max_assets)
        return chosen

    @staticmethod
    def selector_cardinality(selector: Mapping[str, str]) -> tuple[int, int | None]:
        """The reviewed cardinality hook of this adapter; see ``graph_selector_cardinality``."""
        return graph_selector_cardinality(selector)

    async def fetch_json(self, conn: Connection, file: Mapping[str, Any], *, max_bytes: int, headers: Any = UNSET,
                         session: GraphSession | None = None, limits: Any = None) -> bytes:
        """The bytes of one selected file that a ``json`` result decodes. Bigger than ``max_bytes`` is refused. The value's
        own checks (depth, items, finite numbers, repeated keys) are the result store's (``result_bundle.check_json``).

        The read goes through ``output_selectors.FETCH_GATE`` (D6: concurrency and time bound), under ``limits`` (the
        deployment's result limits; the code defaults if none are given). A caller that passes ``session`` already holds
        a fetch slot for the whole result read, as ``results`` does, so the gate is not taken a second time."""
        if session is None:
            from gateway.result_bundle import ResultLimits

            gate_limits = limits if limits is not None else ResultLimits()
            try:
                async with output_selectors.FETCH_GATE.slot(gate_limits):
                    return await self._read_json_file(conn, file, max_bytes, headers, None)
            except output_selectors.FetchRefused as refusal:
                raise SelectionRefused(refusal.code, str(refusal)) from refusal
        return await self._read_json_file(conn, file, max_bytes, headers, session)

    async def _read_json_file(self, conn: Connection, file: Mapping[str, Any], max_bytes: int, headers: Any,
                              session: GraphSession | None) -> bytes:
        # The file is read as it arrives and refused at the first byte past the limit (D6), so a large answer is never held.
        async with self._use(conn, session) as use, use.view_stream(view_params(file), headers=headers) as reply:
            if reply.status_code != 200:
                raise UpstreamError(reply.status_code, "A result file could not be read.")
            data = bytearray()
            async for chunk in reply.aiter_bytes():
                if len(data) + len(chunk) > max_bytes:
                    raise SelectionRefused("json_too_large", f"a JSON result is larger than {max_bytes} bytes")
                data.extend(chunk)
        return bytes(data)

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

    # --- interface version 2 -------------------------------------------------------------------------------

    async def start(self, conn: Connection, op: str, request: CanonicalRequest) -> Pending:
        """Post the prompt. A graph engine always takes a job: the outcome is ``Pending`` and core polls."""
        return Pending(await self.submit(conn, op, request))

    async def results(self, conn: Connection, ref: JobRef, status: JobStatus, request: CanonicalRequest,
                      context: ResultContext) -> ResultBundle:
        """The named results of a succeeded prompt, as the binding's selectors say.

        The history entry is read once; ``collect`` says which files make each result. A ``json`` result is read as bytes
        under the JSON size limit. An ``assets`` result is streamed into the media store through the limited store, so the
        asset limits hold while the bytes are read. A result that is missing, or that does not fit the type its output
        declares, refuses the whole run. Raises ``SelectionRefused``.
        """
        from gateway.result_bundle import LimitedAssetStore, check_json

        if self._assets is None:
            raise UnsupportedByAdapter("This adapter was built without a media store, so it cannot save assets.")
        manifest = request.params.get("manifest")
        block = ((manifest or {}).get("bindings") or {}).get(self.binding) if isinstance(manifest, Mapping) else None
        if not isinstance(block, Mapping):
            raise SelectionRefused("selectors_missing", "the request carries no binding for this engine")
        outputs = manifest.get("outputs") or {}
        limits = context.limits
        store = LimitedAssetStore(self._assets, limits)
        try:
            async with output_selectors.FETCH_GATE.slot(limits), self._use(conn, None) as use:
                reply = await use.history(ref.id)
                if reply.status != 200:
                    raise UpstreamError(reply.status, f"The history answered HTTP {reply.status}.")
                history = reply.json()
                job = history.get(ref.id) if isinstance(history, Mapping) else None
                if not isinstance(job, Mapping):
                    raise SelectionRefused("result_missing", "the engine's answer holds no history entry for the job")
                picked = self.collect(job, block, outputs, limits=limits, history_bytes=len(reply.raw or b""), count=False)
                kinds = self._result_kinds(block, outputs, picked)
                results: dict[str, JsonResult | AssetsResult] = {}
                for name, files in picked.items():
                    if kinds[name] == "json":
                        if len(files) != 1:
                            raise SelectionRefused("result_ambiguous", f"result {name} is one JSON value, but the engine listed {len(files)} files")
                        raw = await self.fetch_json(conn, files[0], max_bytes=limits.json_max_bytes, session=use)
                        results[name] = JsonResult(raw)
                        reason = output_selectors.wrong_type_reason(outputs.get(name), "json", ())
                        saved: list[AssetRef] = []
                    else:
                        saved = [await self._save_output(use, store, file, max_bytes=limits.max_asset_bytes) for file in files]
                        results[name] = AssetsResult(tuple(saved))
                        reason = output_selectors.wrong_type_reason(outputs.get(name), "assets", [ref_.content_type for ref_ in saved])
                    if reason is not None:
                        raise SelectionRefused("result_wrong_type", f"result {name}: {reason}")
                    if kinds[name] == "json":
                        # The document is decoded under the JSON limits only when a shape is declared to check it against;
                        # otherwise the one item is the bytes, and only the count is read.
                        document = check_json(raw, limits) if isinstance(outputs.get(name), Mapping) and outputs[name].get("schema") else raw
                        output_selectors.check_result(outputs.get(name), "json", (document,), name=name, max_assets=limits.max_assets)
                    else:
                        output_selectors.check_result(outputs.get(name), "assets", saved, name=name, max_assets=limits.max_assets)
        except output_selectors.FetchRefused as refusal:
            raise SelectionRefused(refusal.code, str(refusal)) from refusal
        return ResultBundle(results)

    def _result_kinds(self, block: Mapping[str, Any], outputs: Mapping[str, Any],
                      picked: Mapping[str, Any]) -> dict[str, str]:
        """The kind (``json`` or ``assets``) of each selected result. A binding with no selectors reads files only."""
        if "selectors" not in block or self.result_selectors is None:
            return {name: "assets" for name in picked}
        selectors = output_selectors.check_binding(self.result_selectors, outputs, block)
        return {name: self.result_selectors.kind_of(selectors[name]) for name in picked}

    async def _save_output(self, session: GraphSession, store: Any, file: Mapping[str, Any], *, max_bytes: int) -> AssetRef:
        """One selected file, read as a stream and saved through ``store``. Only the first bytes are held to name the
        media type; the rest goes to the store chunk by chunk, so the store's limits stop the read at the first byte over
        them and the whole body is never buffered here."""
        async with session.view_stream(view_params(file)) as response:
            if response.status_code != 200:
                raise UpstreamError(response.status_code, "An output file could not be read.")
            body = response.aiter_bytes()
            head: list[bytes] = []
            held = 0
            async for chunk in body:
                if chunk:
                    head.append(chunk)
                    held += len(chunk)
                if held >= 16:
                    break
            if not held:
                raise UpstreamError(response.status_code, "An output file could not be read.")
            content_type = served_media_type(response.headers.get("content-type"), b"".join(head)[:16])
            if content_type is None:
                raise MalformedResponse("An output file is not a media type that is served.")

            async def chunks() -> AsyncIterator[bytes]:
                for chunk in head:
                    yield chunk
                async for chunk in body:
                    yield chunk

            return await store.save(chunks(), content_type=content_type, max_bytes=max_bytes, name=str(file.get("filename")))


binding_registry.register_selector_cardinality(ADAPTER_ID, graph_selector_cardinality)


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
    "ADAPTER_ID", "CONTROL_TIMEOUT_S", "GRAPH_SUBMIT", "GraphSession", "InputRefused", "NodeGraphAdapter", "SelectionRefused",
    "build_submit_payload", "compile_workflow", "decode_output", "encode_output", "execution_error_message",
    "execution_error_node_id", "history_state", "job_event_from",
    "queue_position", "queue_prompt_ids", "select_outputs", "view_params",
]

"""The gateway side of the differential harness: recorder, rig and fake-engine builders.

Imports the gateway, so it is a test-support module (the pure pieces live in
``adapter_differential``). A ``GatewayRig`` patches the gateway onto temporary
stores, routes engine hosts to scenario-driven fakes, drives the real app over
HTTP chunk by chunk, and assembles the trace.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

import httpx
import pytest

from tests.support import adapter_differential as ad
from tests.support.fake_upstream import (
    FakeUpstream, FakeUpstreams, HandlerTransport, StreamingASGITransport, scenario_from_data, verify,
)

def _object_info_module():
    """The module with the blocking ``/object_info`` lookup, wherever this checkout keeps it."""
    import importlib

    for name in ("gateway.comfy_object_info", "gateway.engine_protocol.node_info"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ImportError("no module with the /object_info lookup found")


# The repo's conftest replaces the lookup's ``lookup`` for every test; a scenario that wants the real
# one (a blocking client) puts this one back.
comfy_object_info = _object_info_module()
REAL_OBJECT_INFO_LOOKUP = comfy_object_info.lookup

PROVENANCE = {
    "source": "hand-written",
    "reference": "adapter differential harness: the engine wire routes the gateway calls (tests/fixtures/adapter_differential)",
}
HANG_BOUND_SECONDS = 60


# --------------------------------------------------------------------------- fake engines

def exchange(id: str, method: str, path: str, response: Mapping[str, Any], *, query: Mapping[str, str] | None = None,
             times: int | None = None, repeat: bool = False, required: bool = True) -> dict[str, Any]:
    """One scenario exchange. Ordered unless ``repeat``; ``times`` is exact for a required one."""
    match: dict[str, Any] = {"method": method, "path": path}
    if query:
        match["query"] = dict(query)
    out: dict[str, Any] = {"id": id, "match": match, "response": dict(response)}
    if repeat:
        out["repeat"] = True
    elif times is not None:
        out["times"] = times
    if not required:
        out["required"] = False
    return out


def scenario(protocol: str, name: str, exchanges: Iterable[Mapping[str, Any]]):
    return scenario_from_data({"protocol": protocol, "name": name, "provenance": PROVENANCE,
                               "exchanges": list(exchanges)})


def hex_body(content: bytes, content_type: str, status: int = 200) -> dict[str, Any]:
    return {"status": status, "headers": {"content-type": content_type}, "body": [{"hex": content.hex()}]}


STATS = {"devices": [{"name": "fk", "type": "cuda", "vram_total": 24 << 30, "vram_free": 20 << 30}]}
QUEUE_IDLE = {"queue_running": [], "queue_pending": []}


def history_done(outputs_key: str = "images", filename: str = "out.png", prompt_id: str = "p1", *,
                 subfolder: str = "", count: int = 1) -> dict[str, Any]:
    items = [{"filename": filename if count == 1 else f"{Path(filename).stem}_{i}{Path(filename).suffix}",
              "subfolder": subfolder, "type": "output"} for i in range(count)]
    return {prompt_id: {"status": {"status_str": "success", "completed": True, "messages": []},
                        "outputs": {"9": {outputs_key: items}}}}


def comfy_exchanges(*, views: Iterable[tuple[bytes, str]] = (), prompt: Mapping[str, Any] | None = None,
                    history: Mapping[str, Any] | None = None, running_polls: int = 0, prompt_id: str = "p1",
                    queue: Mapping[str, Any] | None = None, strict: bool = True,
                    extra: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """The exchanges of a well-behaved ComfyUI: submit, poll, download, plus the housekeeping routes.

    ``running_polls`` history polls answer "not in history yet" before the final answer.
    One ``views`` entry answers every download; several answer the downloads in order.
    Housekeeping routes (stats, queue, free, cancel, delete, upload) are optional. With
    ``strict=False`` nothing is required either (a failure scenario stops part-way).
    """
    out: list[dict[str, Any]] = []
    if running_polls:
        out.append(exchange("history-running", "GET", f"/history/{prompt_id}", {"json": {}}, times=running_polls,
                            required=strict))
    out.append(exchange("prompt", "POST", "/prompt", prompt or {"json": {"prompt_id": prompt_id}}, repeat=True,
                        required=strict))
    out.append(exchange("history", "GET", f"/history/{prompt_id}", {"json": dict(history or history_done(prompt_id=prompt_id))},
                        repeat=True, required=strict))
    views = list(views)
    if len(views) == 1:
        out.append(exchange("view", "GET", "/view", hex_body(*views[0]), repeat=True, required=strict))
    else:
        for index, (content, media_type) in enumerate(views, start=1):
            out.append(exchange(f"view-{index}", "GET", "/view", hex_body(content, media_type), required=strict))
    out += [
        exchange("system-stats", "GET", "/system_stats", {"json": STATS}, repeat=True, required=False),
        exchange("queue", "GET", "/queue", {"json": dict(queue or QUEUE_IDLE)}, repeat=True, required=False),
        exchange("free", "POST", "/free", {"json": {}}, repeat=True, required=False),
        exchange("cancel", "POST", f"/api/jobs/{prompt_id}/cancel", {"json": {}}, repeat=True, required=False),
        exchange("history-delete", "POST", "/history", {"json": {}}, repeat=True, required=False),
        exchange("interrupt", "POST", "/interrupt", {"json": {}}, repeat=True, required=False),
        exchange("upload", "POST", "/upload/image", {"json": {"name": "uploaded.png", "subfolder": "", "type": "input"}},
                 repeat=True, required=False),
        *extra,
    ]
    return out


# --------------------------------------------------------------------------- recorder

class Recorder:
    """Taps the httpx transport boundary and ``asyncio.sleep`` for one scenario.

    Every request any ``httpx.AsyncClient`` in the process sends is logged in start
    order with the caller step in flight, before the host router (or the network)
    answers it. Gateway sleeps are logged with their delay and then skipped, so a
    poll cadence is part of the trace and no scenario waits on the real clock.
    """

    def __init__(self, norm: ad.Normalizer) -> None:
        self.norm = norm
        self.engine: list[dict[str, Any]] = []
        self.sleeps: list[dict[str, Any]] = []
        self.step = -1
        # host -> handler for the gateway's synchronous httpx clients (see ``GatewayRig.sync_engine``).
        self.sync_routes: dict[str, Any] = {}
        # Decides per sleep whether it is skipped. A sleep that is a deadline timer raced against
        # work (the STT outer deadline) must really run, so a scenario can set e.g. ``delay < 30``.
        self.skip_sleep = lambda delay: True

    def install(self, monkeypatch) -> None:
        inner = httpx.AsyncHTTPTransport.handle_async_request
        recorder = self
        real_sleep = asyncio.sleep

        async def tapped(transport, request: httpx.Request) -> httpx.Response:
            await request.aread()
            content_type = request.headers.get("content-type")
            entry: dict[str, Any] = {
                "step": recorder.step,
                "to": f"{request.url.host}" + (f":{request.url.port}" if request.url.port else ""),
                "method": request.method,
                "path": recorder.norm.text(request.url.path),
                "query": ad.sorted_query(request.url.query, recorder.norm),
                "headers": ad.describe_request_headers(request.headers.multi_items(), recorder.norm),
                "body": ad.describe_body(request.content, content_type, recorder.norm),
            }
            ad.settle_content_length(entry["headers"], request.content, entry["body"])
            recorder.engine.append(entry)
            try:
                response = await inner(transport, request)
            except BaseException as error:
                entry["outcome"] = {"error": type(error).__name__}
                raise
            entry["outcome"] = {"status": response.status_code}
            return response

        async def tapped_sleep(delay=0, result=None):
            module = sys._getframe(1).f_globals.get("__name__", "")
            if module == "proxy" or module.startswith("gateway"):
                recorder.sleeps.append({"step": recorder.step, "after_engine_requests": len(recorder.engine),
                                        "delay": round(float(delay), 6)})
            return await real_sleep(0 if recorder.skip_sleep(float(delay)) else delay, result)

        sync_inner = httpx.HTTPTransport.handle_request

        def tapped_sync(transport, request: httpx.Request) -> httpx.Response:
            request.read()
            host = f"{request.url.host}" + (f":{request.url.port}" if request.url.port else "")
            entry: dict[str, Any] = {
                "step": recorder.step,
                "to": host,
                "client": "sync",
                "method": request.method,
                "path": recorder.norm.text(request.url.path),
                "query": ad.sorted_query(request.url.query, recorder.norm),
                "headers": ad.describe_request_headers(request.headers.multi_items(), recorder.norm),
                "body": ad.describe_body(request.content, request.headers.get("content-type"), recorder.norm),
            }
            ad.settle_content_length(entry["headers"], request.content, entry["body"])
            recorder.engine.append(entry)
            handler = recorder.sync_routes.get(host) or recorder.sync_routes.get(request.url.host)
            if handler is None:
                entry["outcome"] = {"error": "ConnectError"}
                raise httpx.ConnectError("no synchronous route for " + host)
            response = handler(request)
            entry["outcome"] = {"status": response.status_code}
            return response

        async def inline_to_thread(func, /, *args, **kwargs):
            # Blocking work runs on the loop, one step at a time, so no OS-thread race can reorder
            # the gateway's concurrent engine calls (the VRAM probe starts one while a round runs).
            result = func(*args, **kwargs)
            await real_sleep(0)
            return result

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", tapped)
        monkeypatch.setattr(asyncio, "sleep", tapped_sleep)
        monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", tapped_sync)


# --------------------------------------------------------------------------- rig

def reset_store(path: Path) -> None:
    """Empty every table of a gateway store (and the autoincrement counters), keeping the file and schema.

    The gateway's route table binds the store paths when ``proxy`` is imported, so a scenario
    cannot point the routes at its own file; it uses the process's test store and starts it empty.
    """
    import sqlite3

    conn = sqlite3.connect(str(path), timeout=5.0)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            if table in {"schema_version", "media_jobs_schema_version", "receipt_history"}:
                continue
            conn.execute(f'DELETE FROM "{table}"')
        if "sqlite_sequence" in [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]:
            conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
    finally:
        conn.close()


class GatewayRig:
    """One scenario run's world: temporary stores, fake engines, the real app, the recorder.

    Use it as a context manager. It owns its ``MonkeyPatch`` context and its
    ``FakeUpstreams``, so a scenario can run twice in one process, each time on a
    fresh store, fresh fakes and a fresh recorder.
    """

    def __init__(self, name: str, tmp_path: Path, *, description: str = "") -> None:
        self.name = name
        self.description = description
        self.tmp = Path(tmp_path)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.norm = ad.Normalizer(tmp_roots=[self.tmp, self.tmp.resolve()])
        self.recorder = Recorder(self.norm)
        self.calls: list[dict[str, Any]] = []
        self.notes: dict[str, Any] = {}
        self.scheduled: list[str] = []  # job ids a route handed to a background executor (see the tests)
        self.engines: dict[str, FakeUpstream] = {}
        self.cache = self.tmp / "cache"
        self.receipts_db: Path
        self.media_jobs_db: Path
        self._exit = contextlib.ExitStack()
        self._client: httpx.AsyncClient | None = None
        self.monkeypatch: pytest.MonkeyPatch
        self.fakes: FakeUpstreams

    # -- set-up -----------------------------------------------------------------
    def __enter__(self) -> "GatewayRig":
        import proxy
        from gateway import media_jobs, receipt_store
        from tests.support.access import authenticated, everything, identity

        self.monkeypatch = self._exit.enter_context(pytest.MonkeyPatch.context())
        self.fakes = FakeUpstreams(self.monkeypatch)
        self._exit.callback(self.fakes.close)
        self._exit.callback(random.setstate, random.getstate())
        random.seed(20260101)
        self.media_jobs_db = Path(media_jobs.DB_PATH)
        self.receipts_db = Path(receipt_store.DB_PATH)
        media_jobs.init_db(self.media_jobs_db)
        receipt_store.init_db(self.receipts_db)
        for store in (self.media_jobs_db, self.receipts_db):
            reset_store(store)
            self._exit.callback(reset_store, store)
        self.cache.mkdir(parents=True, exist_ok=True)
        patch = self.monkeypatch.setattr
        patch(proxy, "IMAGE_CACHE_DIR", self.cache)
        patch(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", self.tmp / "payloads")
        patch(proxy, "_image_url_signing_secret", b"adapter-differential-secret")
        patch(proxy, "COMFY_ENABLED", True)
        # The sandbox has no ComfyUI process or GPU. Keep the probe's absent-data
        # result deterministic without launching the host's nvidia-smi.
        patch(proxy, "_resolve_gpu0_uuid", lambda: None)
        patch(proxy, "_resolve_comfy_pids", lambda: set())
        patch(proxy, "_sample_comfy_vram_bytes", lambda _pids, _uuid: None)
        # The VRAM probe samples on a real-time timer; one sample before and one after a round is the
        # deterministic part (a round that outlasts the interval would add a timer-driven /system_stats).
        patch(proxy, "_VRAM_PROBE_INTERVAL_MIN_S", 3600.0)
        patch(proxy, "_service_availability_response", lambda *a, **k: None)
        # The access plan keeps media metadata off, so stored bytes carry no per-request provenance.
        owner = identity(everything(media_metadata="none", receipt_detail="full"))
        self._exit.enter_context(authenticated(owner))
        for registry in (proxy._video_job_records, proxy._video_progress, proxy._video_workflows):
            saved = dict(registry)
            registry.clear()
            self._exit.callback(_restore_dict, registry, saved)
        self.fakes.router  # build the host router before the recorder wraps it
        self.recorder.install(self.monkeypatch)
        transport = StreamingASGITransport(proxy.app, client=("127.0.0.1", 40000))
        self._client = httpx.AsyncClient(transport=transport, base_url="http://gateway.test")
        return self

    def __exit__(self, exc_type, *exc: object) -> None:
        try:
            if exc_type is None:
                self.fakes.close()
                verify(self.fakes)
        finally:
            self._exit.close()

    def note(self, key: str, value: Any) -> None:
        """Record a scenario fact in the trace (``notes``)."""
        self.notes[key] = value

    def set(self, target: Any, name: str, value: Any) -> None:
        self.monkeypatch.setattr(target, name, value)

    def engine(self, name: str, scenario_, *, host: str) -> FakeUpstream:
        """Serve ``scenario_`` at ``http://<host>`` (``host`` may carry a port) and remember it as ``name``."""
        fake = self.fakes.asgi(scenario_, host=host)
        self.engines[name] = fake
        return fake

    def sync_engine(self, host: str, handler) -> None:
        """Answer the gateway's synchronous ``httpx.Client`` calls to ``host`` with ``handler(request)``.

        The async fakes cannot serve these (one lookup helper uses a blocking client); the request is
        recorded in the same ordered list as every other engine call, marked ``"client": "sync"``.
        """
        self.recorder.sync_routes[host] = handler

    def down_engine(self, host: str, error: Exception | None = None) -> None:
        """Make ``host`` refuse the connection (or fail with ``error``)."""
        raised = error or httpx.ConnectError("connection refused")

        def refuse(request: httpx.Request) -> httpx.Response:
            raise raised

        self.fakes.router.add_transport(host, HandlerTransport(refuse))

    # -- driving ----------------------------------------------------------------
    async def call(self, method: str, path: str, *, label: str = "", json_body: Any = None, content: bytes | None = None,
                   data: Mapping[str, Any] | None = None, files: Any = None, headers: Mapping[str, str] | None = None,
                   params: Mapping[str, Any] | None = None, unordered_json: bool = False,
                   chunks: bool = False, sse_audio: bool = False, cancel_when=None) -> httpx.Response | None:
        """One caller request through the real app. Returns the buffered response.

        ``sse_audio``: describe a speech SSE stream with its audio delta events joined (see
        ``describe_sse_audio``): their cuts follow an ffmpeg pipe read.
        ``chunks``: also record the sizes of the body chunks the caller received. Use it only where
        the gateway relays or builds the chunks itself; a stream that passes through an ffmpeg pipe
        is cut wherever the pipe read lands, which depends on timing.
        ``cancel_when``: an async callable; once it returns the caller walks away (the request is
        cancelled before any response) and the call returns None. The trace records the call as
        ``cancelled_by_caller``.
        ``unordered_json``: compare the JSON response with its keys sorted. Only for a document the
        gateway builds by iterating a set, whose key order follows the process hash seed.
        """
        assert self._client is not None
        self.recorder.step = len(self.calls)
        request = self._client.build_request(method, path, json=json_body, content=content, data=data, files=files,
                                             headers=headers, params=params)
        await request.aread()
        sent = {
            "method": method,
            "path": self.norm.text(request.url.path),
            "query": ad.sorted_query(request.url.query, self.norm),
            "headers": ad.describe_request_headers(
                [(k, v) for k, v in request.headers.multi_items() if k.lower() not in {"host", "accept", "accept-encoding", "connection", "user-agent"}],
                self.norm),
            "body": ad.describe_body(request.content, request.headers.get("content-type"), self.norm),
        }
        ad.settle_content_length(sent["headers"], request.content, sent["body"])
        if cancel_when is not None:
            sender = asyncio.ensure_future(self._client.send(request, stream=True))
            try:
                await asyncio.wait_for(cancel_when(), HANG_BOUND_SECONDS)
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            self.calls.append({"label": label or f"{method} {path}", "request": sent,
                               "response": {"cancelled_by_caller": True}})
            return None
        response = await asyncio.wait_for(self._client.send(request, stream=True), HANG_BOUND_SECONDS)
        received: list[bytes] = []
        try:
            async with asyncio.timeout(HANG_BOUND_SECONDS):
                async for chunk in response.aiter_raw():
                    received.append(chunk)
        finally:
            await response.aclose()
        body = b"".join(received)
        described: dict[str, Any] = {
            "status": response.status_code,
            "headers": ad.describe_response_headers(response.headers.multi_items(), self.norm),
            "body": (ad.describe_sse_audio(body, self.norm) if sse_audio else
                     ad.describe_body(body, response.headers.get("content-type"), self.norm, unordered_json=unordered_json)),
        }
        ad.settle_content_length(described["headers"], body, described["body"])
        if chunks:
            described["chunk_sizes"] = [len(c) for c in received]
        self.calls.append({"label": label or f"{method} {path}", "request": sent, "response": described})
        self._last = _Buffered(response, body)
        return self._last  # type: ignore[return-value]

    async def call_disconnecting(self, method: str, path: str, *, label: str, json_body: Any, disconnect_when) -> None:
        """Send a JSON request straight to the app and report the client gone once ``disconnect_when()`` is true.

        Unlike ``call(cancel_when=...)`` the handler is not cancelled: its ``receive()`` returns
        ``http.disconnect``, which is what a server reports when the peer closes. The route then
        takes its own disconnect path. The response it still produces is recorded.
        """
        import proxy

        body = json.dumps(json_body).encode()
        gone = asyncio.Event()
        delivered = False
        out: dict[str, Any] = {"status": None, "headers": [], "body": b""}

        async def receive() -> dict:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                out["status"] = message["status"]
                out["headers"] = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message["headers"]]
            elif message["type"] == "http.response.body":
                out["body"] += message.get("body", b"")

        headers = [(b"host", b"gateway.test"), (b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode())]
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
                 "root_path": "", "server": ("gateway.test", 80), "client": ("127.0.0.1", 40000),
                 "headers": headers, "extensions": {}}
        self.recorder.step = len(self.calls)
        sent = {"method": method, "path": path, "query": [], "headers": {"content-type": "application/json"},
                "body": ad.describe_body(body, "application/json", self.norm)}
        task = asyncio.ensure_future(proxy.app(scope, receive, send))
        async with asyncio.timeout(HANG_BOUND_SECONDS):
            while not disconnect_when() and not task.done():
                await asyncio.sleep(0)
            gone.set()
            await task
        described = {"status": out["status"], "headers": ad.describe_response_headers(out["headers"], self.norm),
                     "body": ad.describe_body(out["body"], dict(out["headers"]).get("content-type"), self.norm),
                     "disconnected_by_caller": True}
        ad.settle_content_length(described["headers"], out["body"], described["body"])
        self.calls.append({"label": label, "request": sent, "response": described})

    # -- the trace --------------------------------------------------------------
    def trace(self) -> dict[str, Any]:
        receipts = ad.dump_sqlite(self.receipts_db, self.norm, skip_tables=("receipt_history",))
        jobs = ad.dump_sqlite(self.media_jobs_db, self.norm)
        return {
            "schema": ad.TRACE_SCHEMA,
            "scenario": self.name,
            "description": self.description,
            "caller": self.calls,
            "engine_requests": self.recorder.engine,
            "gateway_sleeps": self.recorder.sleeps,
            "receipts": receipts,
            "media_jobs": jobs,
            "assets": ad.describe_assets(self.cache, self.norm),
            "notes": self.notes,
        }


class _Buffered:
    """The buffered result of a streamed call, with httpx.Response-like accessors."""

    def __init__(self, response: httpx.Response, body: bytes) -> None:
        self.status_code = response.status_code
        self.headers = response.headers
        self.content = body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return json.loads(self.content)


def _restore_dict(target: dict, saved: dict) -> None:
    target.clear()
    target.update(saved)


def play(name: str, tmp_path: Path, record_testsuite_property, setup, drive, *, description: str = "",
         runs: int = 2) -> dict[str, Any]:
    """Run one scenario ``runs`` times in this process, require identical traces, compare with the golden.

    ``setup(rig)`` registers fake engines and patches (synchronous); ``drive(rig)`` is the
    async body that calls the gateway. Each run has its own temp tree, stores, fakes and
    recorder. The trace of the first run is recorded as the junit suite property
    ``adapter_trace.<name>`` so the hermetic runner can carry it out (see the docs).
    """
    traces: list[dict[str, Any]] = []
    for run in range(runs):
        with GatewayRig(name, Path(tmp_path) / f"run{run}", description=description) as rig:
            setup(rig)
            asyncio.run(drive(rig))
            traces.append(json.loads(ad.dumps(rig.trace())))
    record_testsuite_property(f"{ad.JUNIT_PROPERTY}.{name}", json.dumps(traces[0], sort_keys=True))
    for later in traces[1:]:
        if later != traces[0]:
            pytest.fail("not deterministic: two runs in one process differ.\n"
                        + ad.readable_diff(name, traces[0], later), pytrace=False)
    problem = ad.compare_golden(name, traces[0])
    if problem:
        pytest.fail(problem, pytrace=False)
    return traces[0]

"""The node-graph adapter: every operation issues the call the gateway always made, and the interface holds."""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from gateway.engine_protocol import description, node_graph, reviewed
from gateway.engine_protocol.adapter import EngineAdapter
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.errors import (
    MalformedResponse, ProtocolError, TransportFailure, UnknownOperation, UnsupportedByAdapter, UpstreamError,
)
from gateway.engine_protocol.node_graph import GraphSession, NodeGraphAdapter
from gateway.engine_protocol.types import CanonicalRequest, Connection, JobRef, JobState, JobStatus, Timeouts
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture

HOST = "graph.test:7890"
CONN = Connection(address=f"http://{HOST}", timeouts=Timeouts(connect_s=10, first_byte_s=300, total_s=300))
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 20


def run(coro):
    return asyncio.run(coro)


class Response:
    def __init__(self, status: int = 200, payload: Any = None, content: bytes = b"", headers: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.content = content
        self.text = json.dumps(payload) if payload is not None else content.decode("utf-8", "replace")
        self.headers = headers or {}

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class RecordingClient:
    """A stand-in client that records the exact calls it receives."""

    def __init__(self, answer: Any = None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.answer = answer or (lambda method, url, kwargs: Response(200, {}))

    async def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(("GET", url, kwargs))
        return self.answer("GET", url, kwargs)

    async def post(self, url: str, **kwargs: Any) -> Response:
        self.calls.append(("POST", url, kwargs))
        return self.answer("POST", url, kwargs)

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: Any):
        self.calls.append((method, url, kwargs))
        yield self.answer(method, url, kwargs)


def session(client: Any) -> GraphSession:
    return GraphSession(CONN, client)


# --- each operation is the call the gateway always made --------------------------------------------------------


def test_each_operation_issues_the_exact_client_call() -> None:
    client = RecordingClient()
    s = session(client)
    headers = {"x-route": "a"}
    run(s.submit({"prompt": {"1": {}}}, headers=headers))
    run(s.history("p1", headers=headers))
    run(s.history("p1", headers=headers, timeout=15))
    run(s.history_page(200))
    run(s.queue())
    run(s.queue(headers=headers, timeout=10))
    run(s.view({"filename": "a.png", "type": "output"}, headers=headers))
    run(s.upload({"image": ("a.png", b"png", "image/png")}, headers=headers))
    run(s.cancel("p1", headers=dict(headers), timeout=5))
    run(s.cancel("p1", headers=headers))
    run(s.delete_history(["p1"], headers=headers))
    run(s.stats(timeout=5))
    run(s.free({"unload_models": True, "free_memory": True}, timeout=5))
    run(s.object_info("Load Image/x"))
    base = f"http://{HOST}"
    assert client.calls == [
        ("POST", f"{base}/prompt", {"headers": headers, "json": {"prompt": {"1": {}}}}),
        ("GET", f"{base}/history/p1", {"headers": headers}),
        ("GET", f"{base}/history/p1", {"headers": headers, "timeout": 15}),
        ("GET", f"{base}/history?max_items=200", {}),
        ("GET", f"{base}/queue", {}),
        ("GET", f"{base}/queue", {"headers": headers, "timeout": 10}),
        ("GET", f"{base}/view", {"headers": headers, "params": {"filename": "a.png", "type": "output"}}),
        ("POST", f"{base}/upload/image", {"headers": headers, "files": {"image": ("a.png", b"png", "image/png")}}),
        ("POST", f"{base}/api/jobs/p1/cancel", {"headers": headers, "timeout": 5}),
        ("POST", f"{base}/api/jobs/p1/cancel", {"headers": headers}),
        ("POST", f"{base}/history", {"headers": headers, "json": {"delete": ["p1"]}}),
        ("GET", f"{base}/system_stats", {"timeout": 5}),
        ("POST", f"{base}/free", {"json": {"unload_models": True, "free_memory": True}, "timeout": 5}),
        ("GET", f"{base}/object_info/Load%20Image%2Fx", {}),
    ]


def test_a_stream_read_passes_the_headers_and_query_it_was_given() -> None:
    client = RecordingClient(lambda m, u, k: Response(200, content=b"abc"))

    async def go():
        async with session(client).view_stream({"filename": "a.mp4"}, headers={"accept-encoding": "identity"}) as response:
            return response.content

    assert run(go()) == b"abc"
    assert client.calls == [("GET", f"http://{HOST}/view", {"headers": {"accept-encoding": "identity"}, "params": {"filename": "a.mp4"}})]


def test_replies_answer_json_status_text_and_headers_like_the_response() -> None:
    client = RecordingClient(lambda m, u, k: Response(500, content=b"boom", headers={"Content-Type": "text/plain"}))
    reply = run(session(client).submit({"prompt": {}}))
    assert (reply.status, reply.text, reply.headers, reply.raw) == (500, "boom", {"content-type": "text/plain"}, b"boom")
    with pytest.raises(ValueError):
        reply.json()  # the parse error is raised where json() is asked for, as the response did
    ok = run(session(RecordingClient(lambda m, u, k: Response(200, {"prompt_id": "p"}))).submit({}))
    assert (ok.json(), ok.text) == ({"prompt_id": "p"}, None)


def test_a_view_reply_never_parses_json() -> None:
    class NoJson(Response):
        def json(self):
            raise AssertionError("a file is not JSON")

    reply = run(session(RecordingClient(lambda m, u, k: NoJson(200, content=PNG))).view({"filename": "a.png"}))
    assert reply.raw == PNG and reply.body is None


@pytest.mark.parametrize("error,kind", [
    (httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectTimeout("slow"), "timeout"),
    (httpx.ConnectError("refused"), "unreachable"), (httpx.RemoteProtocolError("dropped"), "aborted"),
])
def test_transport_errors_are_typed_failures_with_the_original_text(error: Exception, kind: str) -> None:
    def fail(method, url, kwargs):
        raise error

    with pytest.raises(TransportFailure) as raised:
        run(session(RecordingClient(fail)).queue())
    assert raised.value.kind == kind and str(raised.value) == str(error) and raised.value.__cause__ is error


def test_an_error_that_is_not_from_httpx_passes_through_unchanged() -> None:
    def fail(method, url, kwargs):
        raise RuntimeError("odd")

    with pytest.raises(RuntimeError):
        run(session(RecordingClient(fail)).queue())


# --- the real client over a fake engine ----------------------------------------------------------------------------


def serve(fakes, handler):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request):
        seen.append(request)
        return handler(request)

    fakes.router.add_transport(HOST, HandlerTransport(record))
    return seen


def test_the_adapter_opens_a_client_with_the_connection_timeouts(fake_upstreams) -> None:
    seen = serve(fake_upstreams, lambda r: httpx.Response(200, json={"prompt_id": "p9"}))
    adapter = NodeGraphAdapter()
    ref = run(adapter.submit(CONN, "graph.submit", CanonicalRequest("graph.submit", body={"prompt": {"1": {}}})))
    assert ref == JobRef("graph.submit", "p9")
    [sent] = seen
    assert (sent.method, str(sent.url)) == ("POST", f"http://{HOST}/prompt")
    assert sent.content == b'{"prompt":{"1":{}}}'
    assert sent.extensions["timeout"] == {"connect": 10, "read": 300, "write": 300, "pool": 300}


def test_submit_names_a_refusal_and_a_missing_prompt_id() -> None:
    adapter = NodeGraphAdapter()

    async def go(client: RecordingClient):
        return await adapter.submit(CONN, "graph.submit", CanonicalRequest("graph.submit", body={}, params={"session": session(client)}))

    with pytest.raises(UpstreamError) as refused:
        run(go(RecordingClient(lambda m, u, k: Response(400, content=b"bad prompt"))))
    assert (refused.value.status, str(refused.value)) == (400, "bad prompt")
    with pytest.raises(MalformedResponse):
        run(go(RecordingClient(lambda m, u, k: Response(200, {"node_errors": {}}))))
    with pytest.raises(UnknownOperation):
        run(adapter.submit(CONN, "graph.queue", CanonicalRequest("graph.queue")))


def history(entry: dict | None, status: int = 200):
    def answer(method, url, kwargs):
        return Response(status, {"p1": entry} if entry is not None else {})
    return RecordingClient(answer)


def test_poll_maps_a_history_entry_to_a_job_state() -> None:
    adapter = NodeGraphAdapter(description.load_registry()["node-graph"])
    ref = JobRef("graph.submit", "p1")
    running = {"status": {"completed": False}}
    done = {"status": {"completed": True}, "outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "", "type": "output"}]}}}
    failed = {"status": {"messages": [["execution_error", {"node_type": "KSampler", "node_id": "7"}]]}}
    assert run(adapter.poll(CONN, ref, session=session(history(None)))).state is JobState.pending
    assert run(adapter.poll(CONN, ref, session=session(history(running)))).state is JobState.running
    status = run(adapter.poll(CONN, ref, session=session(history(done))))
    assert status.state is JobState.succeeded and len(status.assets) == 1
    bad = run(adapter.poll(CONN, ref, session=session(history(failed))))
    assert (bad.state, bad.detail) == (JobState.failed, "ComfyUI KSampler node 7: execution failed")
    with pytest.raises(UpstreamError):
        run(adapter.poll(CONN, ref, session=session(history(done, status=500))))


def test_the_label_that_names_the_engine_is_data_of_the_description() -> None:
    doc = description.load_registry()["node-graph"]
    assert doc.executor == "adapter:node-graph" and doc.label
    adapter = NodeGraphAdapter(doc)
    assert adapter.unavailable_message(CONN) == f"{doc.label} unavailable at http://{HOST}"
    assert NodeGraphAdapter().unavailable_message(CONN) == f"the engine unavailable at http://{HOST}"


def test_recover_reads_history_then_the_queue_and_says_none_when_neither_knows_the_prompt() -> None:
    adapter = NodeGraphAdapter()
    ref = JobRef("graph.submit", "p1")

    def answer(queue: dict):
        def reply(method, url, kwargs):
            return Response(200, {} if "/history/" in url else queue)
        return RecordingClient(reply)

    running = run(adapter.recover(CONN, ref, session=session(answer({"queue_running": [[0, "p1", {}]], "queue_pending": []}))))
    pending = run(adapter.recover(CONN, ref, session=session(answer({"queue_running": [], "queue_pending": [[1, "p1"]]}))))
    lost = run(adapter.recover(CONN, ref, session=session(answer({"queue_running": [], "queue_pending": []}))))
    assert (running.state, pending.state, lost) == (JobState.running, JobState.pending, None)


def test_outputs_are_read_in_node_order_and_an_unreadable_file_is_skipped() -> None:
    job = {"status": {"completed": True}, "outputs": {
        "9": {"images": [{"filename": "a.png", "subfolder": "s", "type": "output"}, {"filename": "gone.png"}]},
        "10": {"videos": [{"filename": "v.mp4"}], "images": [{"filename": "b.png", "type": "temp"}]},
    }}
    status = run(NodeGraphAdapter().poll(CONN, JobRef("x", "p1"), session=session(history(job))))
    assert len(status.assets) == 4

    def answer(method, url, kwargs):
        name = kwargs["params"]["filename"]
        if name == "gone.png":
            raise httpx.ReadTimeout("slow")
        return Response(200, content=name.encode())

    client = RecordingClient(answer)
    got = run(NodeGraphAdapter().fetch_outputs(CONN, status, headers={"h": "1"}, session=session(client)))
    assert got == [b"a.png", b"b.png"]
    assert [c[2]["params"] for c in client.calls] == [
        {"filename": "a.png", "type": "output", "subfolder": "s"},
        {"filename": "gone.png", "type": "output"},
        {"filename": "b.png", "type": "temp"},
    ]
    assert all(c[2]["headers"] == {"h": "1"} for c in client.calls)


def test_fetch_assets_needs_a_media_store() -> None:
    status = JobStatus(JobRef("x", "p1"), JobState.succeeded, assets=(node_graph.encode_output("images", {"filename": "a.png"}),))
    with pytest.raises(UnsupportedByAdapter):
        run(NodeGraphAdapter().fetch_assets(CONN, status.ref, status))


def test_fetch_assets_over_the_wire(fake_upstreams) -> None:
    serve(fake_upstreams, lambda r: httpx.Response(200, content=PNG, headers={"content-type": "image/png"}))
    store = MemoryAssetStore()
    adapter = NodeGraphAdapter(assets=store)
    status = JobStatus(JobRef("x", "p1"), JobState.succeeded, assets=(node_graph.encode_output("images", {"filename": "a.png"}),))
    [ref] = run(adapter.fetch_assets(CONN, status.ref, status))
    assert (ref.content_type, ref.size, ref.name, store.get(ref)) == ("image/png", len(PNG), "a.png", PNG)
    serve_html = {"content-type": "text/html"}
    fake_upstreams.router._routes[HOST] = HandlerTransport(lambda r: httpx.Response(200, content=b"<html>", headers=serve_html))
    with pytest.raises(MalformedResponse):
        run(adapter.fetch_assets(CONN, status.ref, status))


def test_cancel_raises_for_a_refusal_and_health_is_observed() -> None:
    adapter = NodeGraphAdapter()
    ref = JobRef("x", "p1")
    client = RecordingClient(lambda m, u, k: Response(200, content=b""))
    run(adapter.cancel(CONN, ref, headers={"h": "1"}, session=session(client)))
    assert client.calls == [("POST", f"http://{HOST}/api/jobs/p1/cancel", {"headers": {"h": "1"}, "timeout": node_graph.CONTROL_TIMEOUT_S})]
    with pytest.raises(UpstreamError):
        run(adapter.cancel(CONN, ref, session=session(RecordingClient(lambda m, u, k: Response(404, content=b"")))))


def test_health_and_inventory_are_read_from_the_engine(fake_upstreams) -> None:
    routes = {"/system_stats": httpx.Response(200, json={"devices": []}), "/object_info": httpx.Response(200, json={"B": {}, "A": {}})}
    serve(fake_upstreams, lambda r: routes[r.url.path])
    adapter = NodeGraphAdapter()
    assert run(adapter.health(CONN)).state == "ok"
    inventory = run(adapter.inventory(CONN))
    assert inventory.ids == ("A", "B") and inventory.evidence == "graph.object_info"
    routes["/system_stats"] = httpx.Response(503)
    assert run(adapter.health(CONN)).state == "failing"


def test_health_is_failing_when_the_engine_cannot_be_reached(fake_upstreams) -> None:
    def refuse(request):
        raise httpx.ConnectError("refused")

    serve(fake_upstreams, refuse)
    health = run(NodeGraphAdapter().health(CONN))
    assert health.state == "failing" and health.detail.startswith("unreachable")


# --- what the engine's answers mean ------------------------------------------------------------------------------


@pytest.mark.parametrize("job,expected", [
    (None, None),
    ({}, None),
    ({"status": {"status_str": "success"}}, None),
    ({"status": {"status_str": "error"}}, "L execution failed"),
    ({"status": {"messages": [["execution_error", {}]]}}, "L execution error: execution failed"),
    ({"status": {"messages": [["execution_error", {"node_id": " 4 "}]]}}, "L node 4: execution failed"),
    ({"status": {"messages": [["execution_error", {"node_type": "VAEDecode"}]]}}, "L VAEDecode: execution failed"),
    ({"status": {"messages": [["execution_error", {"node_type": "X" * 600, "node_id": "1"}]]}}, ("L " + "X" * 600 + " node 1: execution failed")[:500]),
    ({"status": "weird"}, None),
])
def test_execution_errors_read_as_they_always_did(job: Any, expected: str | None) -> None:
    assert node_graph.execution_error_message(job, "L") == expected


def test_error_node_state_queue_and_output_helpers() -> None:
    job = {"status": {"completed": True, "messages": [["execution_error", {"node_id": "12"}]]}}
    assert node_graph.execution_error_node_id(job) == "12"
    assert node_graph.execution_error_node_id({"status": {"messages": [["x"]]}}) is None
    assert node_graph.history_state({"status": {"completed": True}}) == ("succeeded", None)
    assert node_graph.history_state({"status": {}}) == ("running", None)
    assert node_graph.history_state(job, "L")[0] == "failed"
    queue = {"queue_running": [[0, "r"]], "queue_pending": [[1, "a"], [2, "p"], "junk", [3]]}
    assert node_graph.queue_position(queue, "p") == 2 and node_graph.queue_position(queue, "r") is None
    assert node_graph.queue_position({"queue_pending": "no"}, "p") is None
    assert node_graph.queue_prompt_ids([[0, "a"], [1, ""], [2, 3], "x", ["only"]]) == {"a"}
    assert node_graph.queue_prompt_ids("not a list") == set()
    video = {"outputs": {"1": {"images": [{"filename": "src.mp4", "type": "input"}]},
                          "2": {"videos": [{"filename": "out.webm", "subfolder": "v", "type": "output"}]}}}
    assert node_graph.first_video_output(video) == {"filename": "out.webm", "subfolder": "v", "type": "output"}
    assert node_graph.first_video_output({"outputs": {"1": {"images": [{"filename": "src.mp4", "type": "input"}]}}}) == \
        {"filename": "src.mp4", "subfolder": "", "type": "input"}
    assert node_graph.first_video_output({"outputs": {"1": {"images": [{"filename": "a.png"}]}}}) is None
    audio = {"outputs": {"3": {"audio": [{"filename": "a.flac", "type": "temp"}]}}}
    assert node_graph.first_audio_output(audio) == {"filename": "a.flac", "subfolder": "", "type": "temp"}
    assert node_graph.first_audio_output({}) is None
    assert node_graph.view_params({"filename": "a", "subfolder": "s"}) == {"filename": "a", "type": "output", "subfolder": "s"}
    assert node_graph.decode_output(node_graph.encode_output("images", {"filename": "a"}))["filename"] == "a"
    with pytest.raises(MalformedResponse):
        node_graph.decode_output("not json")


def test_the_submit_payload_derives_the_embedded_graph_and_falls_back_to_the_twin() -> None:
    workflow = {"1": {"class_type": "A"}, "__ui_workflow__": {"twin": True}}
    derived = node_graph.build_submit_payload(dict(workflow), lambda graph: {"derived": sorted(graph)}, client_id="c")
    assert derived == {"prompt": {"1": {"class_type": "A"}}, "extra_data": {"extra_pnginfo": {"workflow": {"derived": ["1"]}}}, "client_id": "c"}
    twin = node_graph.build_submit_payload(dict(workflow), lambda graph: None)
    assert twin["extra_data"]["extra_pnginfo"]["workflow"] == {"twin": True}
    bare = node_graph.build_submit_payload({"1": {}}, lambda graph: None)
    assert bare == {"prompt": {"1": {}}}


def test_pushed_messages_become_typed_events() -> None:
    ref = JobRef("x", "p1")
    event = node_graph.job_event_from
    assert event(ref, {"type": "execution_start", "data": {}}).state is JobState.running
    assert event(ref, {"type": "executing", "data": {"node": None}}).state is JobState.succeeded
    stepping = event(ref, {"type": "executing", "data": {"node": "5"}})
    assert (stepping.kind, stepping.detail) == ("progress", "5")
    assert event(ref, {"type": "progress", "data": {"value": 5, "max": 20, "node": "5"}}).progress == 0.25
    assert event(ref, {"type": "progress", "data": {"value": 0, "max": 0}}).progress is None
    assert event(ref, {"type": "execution_error", "data": {"exception_message": "boom"}}).detail == "boom"
    assert event(ref, {"type": "execution_interrupted", "data": {}}).state is JobState.cancelled
    assert event(ref, {"type": "status", "data": {}}) is None


# --- the interface ------------------------------------------------------------------------------------------------


def test_the_adapter_implements_the_versioned_interface() -> None:
    adapter = NodeGraphAdapter()
    assert isinstance(adapter, EngineAdapter) and adapter.interface_version == 1
    for name in ["health", "inventory", "invoke", "submit", "poll", "cancel", "recover", "fetch_assets"]:
        assert inspect.iscoroutinefunction(getattr(adapter, name)), name
    assert inspect.isasyncgenfunction(adapter.events)
    with pytest.raises(UnknownOperation):
        run(adapter.invoke(CONN, "graph.nope", CanonicalRequest("graph.nope")))


def test_invoke_dispatches_each_operation_to_its_call() -> None:
    client = RecordingClient()
    adapter = NodeGraphAdapter()
    s = session(client)

    def call(op, body=None, **params):
        run(adapter.invoke(CONN, op, CanonicalRequest(op, body=body or {}, params={"session": s, **params}, headers={"h": "1"})))

    call("graph.history", id="p1")
    call("graph.history_page", max_items=5)
    call("graph.queue")
    call("graph.view", query={"filename": "a"})
    call("graph.upload", files={"image": ("a", b"x", "image/png")})
    call("graph.cancel", id="p1", timeout=5)
    call("graph.delete_history", ids=["p1"])
    call("graph.stats", timeout=5)
    call("graph.free", {"unload_models": True}, timeout=5)
    call("graph.submit", {"prompt": {}})
    assert [(m, u.removeprefix(f"http://{HOST}")) for m, u, _ in client.calls] == [
        ("GET", "/history/p1"), ("GET", "/history?max_items=5"), ("GET", "/queue"), ("GET", "/view"), ("POST", "/upload/image"),
        ("POST", "/api/jobs/p1/cancel"), ("POST", "/history"), ("GET", "/system_stats"), ("POST", "/free"), ("POST", "/prompt"),
    ]


def test_events_follow_one_prompt_until_it_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = NodeGraphAdapter()
    frames = [
        {"type": "executing", "data": {"prompt_id": "other", "node": "1"}},
        {"type": "execution_start", "data": {"prompt_id": "p1"}},
        {"type": "progress", "data": {"prompt_id": "p1", "value": 1, "max": 4}},
        {"type": "execution_success", "data": {"prompt_id": "p1"}},
        {"type": "execution_start", "data": {"prompt_id": "p1"}},
    ]

    async def watch(conn, client_id=None):
        for frame in frames:
            yield frame

    monkeypatch.setattr(adapter, "watch", watch)

    async def go():
        return [event async for event in adapter.events(CONN, JobRef("x", "p1"))]

    events = run(go())
    assert [e.kind for e in events] == ["status", "progress", "status"] and events[-1].state is JobState.succeeded


# --- selection by data ---------------------------------------------------------------------------------------------


def test_a_record_that_names_no_protocol_is_answered_by_the_adapter_that_serves_its_capability() -> None:
    assert isinstance(reviewed.adapter_for_backend({"capabilities": ["images"]}, "images"), NodeGraphAdapter)
    assert reviewed.protocol_for_backend({}, "images").id == "node-graph"
    assert reviewed.protocol_for_backend({}, "speech").id == "inline-tts"
    assert reviewed.protocol_for_backend({}, "transcriptions").id == "chunked-stt"


def test_a_record_that_names_an_adapter_protocol_selects_it_in_either_place() -> None:
    assert reviewed.protocol_for_backend({"protocol": "node-graph"}, "videos").id == "node-graph"
    assert reviewed.protocol_for_backend({"connection": {"protocol": "node-graph"}}, "videos").id == "node-graph"
    # A protocol that is not an adapter does not stop the capability's adapter from answering.
    assert reviewed.protocol_for_backend({"protocol": "openai-compatible"}, "speech").id == "inline-tts"


def test_no_adapter_for_a_capability_is_a_named_refusal() -> None:
    with pytest.raises(ProtocolError) as raised:
        reviewed.adapter_for_backend({}, "teleportation")
    assert raised.value.code == "adapter_not_registered"


def test_a_registered_factory_replaces_the_builtin_one() -> None:
    sentinel = NodeGraphAdapter()
    reviewed.register("node-graph", lambda doc: sentinel)
    try:
        assert reviewed.adapter_for_backend({}, "images") is sentinel
    finally:
        reviewed._FACTORIES.pop("node-graph", None)
        reviewed._BUILT.clear()
    assert reviewed.adapter_for_backend({}, "images") is not sentinel


def test_serves_and_label_are_validated_data() -> None:
    doc = {"protocol_version": 1, "id": "x", "executor": "http-v1", "serves": ["images"], "operations": {
        "m": {"class": "inventory", "request": {"method": "GET", "path": "/m"}}}}
    with pytest.raises(ProtocolError) as raised:
        description.parse_description(doc, source="x.yml")
    assert "serves" in str(raised.value)
    adapter_doc = {"protocol_version": 1, "id": "y", "executor": "adapter:y", "serves": ["a.b"], "label": "Thing",
                   "operations": {"op": {"class": "generate"}}}
    parsed = description.parse_description(adapter_doc, source="y.yml")
    assert (parsed.serves, parsed.label) == (("a.b",), "Thing")

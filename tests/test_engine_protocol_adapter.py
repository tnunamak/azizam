"""The versioned adapter interface, the generic http-v1 adapter, and the in-tree speech client behind it."""

from __future__ import annotations

import asyncio
import inspect
import json

import httpx
import pytest

from gateway.engine_protocol import adapter, description
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.errors import ProtocolError, TransportFailure, UnknownOperation, UnsupportedByAdapter
from gateway.engine_protocol.http_adapter import HttpV1Adapter
from gateway.engine_protocol.executor import HttpV1Executor
from gateway.engine_protocol.inline_tts import InlineTtsAdapter
from gateway.engine_protocol.transport import HttpxTransport
from gateway.engine_protocol.types import CanonicalRequest, Connection, Credential, JobRef, JobState, Timeouts
from tests.support.fake_upstream import HandlerTransport, fake_upstreams  # noqa: F401 - fixture

BASE = "http://engine.test:8080"
INTERFACE = ["health", "inventory", "invoke", "submit", "poll", "events", "cancel", "recover", "fetch_assets"]


def run(coro):
    return asyncio.run(coro)


def http_adapter(operations, handler, **extra):
    doc = {"protocol_version": 1, "id": "t", "executor": "http-v1", "operations": operations, **extra}
    ex = HttpV1Executor(description.parse_description(doc, source="t.yml"), transport=HttpxTransport.for_tests(HandlerTransport(handler)), assets=MemoryAssetStore())
    return HttpV1Adapter(ex)


# --- the interface ---------------------------------------------------------------------------


def test_the_interface_is_versioned_and_complete() -> None:
    assert adapter.ADAPTER_INTERFACE_VERSION == 1
    for impl in (HttpV1Adapter, InlineTtsAdapter):
        assert impl.interface_version == adapter.ADAPTER_INTERFACE_VERSION
        for name in INTERFACE:
            assert inspect.iscoroutinefunction(getattr(impl, name)) or inspect.isasyncgenfunction(getattr(impl, name)) or name == "events", (impl, name)
    assert isinstance(HttpV1Adapter(HttpV1Executor(description.load_registry()["openai-compatible"])), adapter.EngineAdapter)


class _Old:
    interface_version = 0


def test_the_registry_resolves_http_v1_descriptions_and_reviewed_adapters_and_checks_versions() -> None:
    descriptions = description.load_registry()
    registry = adapter.AdapterRegistry(descriptions)
    assert isinstance(registry.adapter_for("openai-compatible"), HttpV1Adapter)
    with pytest.raises(UnknownOperation):
        registry.adapter_for("nope")

    adapter_doc = {"protocol_version": 1, "id": "rev", "executor": "adapter:rev-one", "operations": {"x": {"class": "generate"}}}
    registry = adapter.AdapterRegistry({"rev": description.parse_description(adapter_doc, source="rev.yml")})
    with pytest.raises(ProtocolError) as raised:
        registry.adapter_for("rev")
    assert "rev-one" in str(raised.value) and "not registered" in str(raised.value)
    registry.register("rev-one", lambda desc: _Old())
    with pytest.raises(ProtocolError) as raised:
        registry.adapter_for("rev")
    assert "interface version 0" in str(raised.value)
    good = InlineTtsAdapter()
    registry.register("rev-one", lambda desc: good)
    assert registry.adapter_for("rev") is good


# --- engine records ---------------------------------------------------------------------------


def test_a_connection_is_read_from_the_record_with_secrets_looked_up_by_reference() -> None:
    record = {
        "transport": "http", "address": "http://127.0.0.1:8080", "protocol": "openai-compatible",
        "credentials": {"inference": {"secret_ref": "ENGINE_KEY", "header": "X-Key", "scheme": ""}, "control": None},
        "headers": [{"name": "X-Client", "value": "home"}],
        "timeouts": {"connect_s": 3, "first_byte_s": 30, "total_s": 90},
    }
    conn = adapter.connection_from_record(record, lambda ref: {"ENGINE_KEY": "k-1"}.get(ref))
    assert conn.address == "http://127.0.0.1:8080"
    assert conn.credentials == {"inference": Credential("k-1", header="X-Key", scheme="")}
    assert conn.headers == {"X-Client": "home"}
    assert conn.timeouts == Timeouts(3, 30, 90)
    assert "k-1" not in repr(conn)


def test_a_missing_secret_means_no_credential_and_a_non_http_transport_needs_an_adapter() -> None:
    record = {"transport": "http", "address": BASE, "protocol": "p", "credentials": {"inference": {"secret_ref": "GONE"}}}
    assert adapter.connection_from_record(record, lambda ref: None).credentials == {}
    with pytest.raises(ProtocolError) as raised:
        adapter.connection_from_record({"transport": "tcp", "address": "tcp://h:1", "protocol": "p"}, lambda ref: None)
    assert "adapter" in str(raised.value)


# --- the generic adapter ---------------------------------------------------------------------

OPS = {
    "health": {"class": "inventory", "request": {"method": "GET", "path": "/health"}, "response": {"framing": "none"}},
    "inventory": {"class": "inventory", "request": {"method": "GET", "path": "/v1/models"}, "response": {"framing": "json", "select": {"ids": "/data/*/id"}}},
}


def test_health_is_what_the_health_operation_saw_and_failures_are_reported_not_raised() -> None:
    ok = http_adapter(OPS, lambda r: httpx.Response(200))
    assert run(ok.health(Connection(address=BASE))) == adapter.Health("ok", "", "health")
    down = http_adapter(OPS, lambda r: httpx.Response(503, text="loading"), errors=[{"status": 503, "code": "loading", "advice": "Wait."}])
    health = run(down.health(Connection(address=BASE)))
    assert health.state == "failing" and "503" in health.detail and "Wait." in health.detail

    def refuse(request):
        raise httpx.ConnectError("refused")
    unreachable = http_adapter(OPS, refuse)
    assert run(unreachable.health(Connection(address=BASE))).state == "failing"


def test_without_a_health_operation_an_answered_inventory_is_the_evidence_and_with_neither_it_is_unknown() -> None:
    only_inventory = http_adapter({"inventory": OPS["inventory"]}, lambda r: httpx.Response(200, json={"data": []}))
    assert run(only_inventory.health(Connection(address=BASE))) == adapter.Health("ok", "", "inventory")
    neither = http_adapter({"chat": {"class": "generate", "request": {"method": "POST", "path": "/c"}, "response": {"framing": "json"}}}, lambda r: httpx.Response(200))
    assert run(neither.health(Connection(address=BASE))).state == "unknown"


def test_inventory_lists_the_selected_ids() -> None:
    a = http_adapter(OPS, lambda r: httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]}))
    assert run(a.inventory(Connection(address=BASE))) == adapter.Inventory(("m1", "m2"), "inventory")
    b = http_adapter({"chat": OPS["health"]}, lambda r: httpx.Response(200))
    with pytest.raises(UnsupportedByAdapter):
        run(b.inventory(Connection(address=BASE)))


def test_the_seed_openai_description_lists_models_through_the_adapter() -> None:
    seed = description.load_registry()["openai-compatible"]
    ex = HttpV1Executor(seed, transport=HttpxTransport.for_tests(HandlerTransport(lambda r: httpx.Response(200, json={"data": [{"id": "x"}]}))))
    assert run(HttpV1Adapter(ex).inventory(Connection(address=BASE))).ids == ("x",)


JOB = {"render": {"class": "generate", "job": {
    "submit": {"request": {"method": "POST", "path": "/jobs", "body": {"encoding": "json"}}, "id": "/id"},
    "poll": {"request": {"method": "GET", "path": "/jobs/{id}"}, "status": {"path": "/s", "states": {"ok": "succeeded", "run": "running"}}, "interval_s": 1, "max_wait_s": 9},
    "cancel": {"request": {"method": "DELETE", "path": "/jobs/{id}"}},
}}}


def test_jobs_go_through_submit_poll_cancel_and_recover_tells_a_lost_job_from_a_live_one() -> None:
    state = {"gone": False}

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "POST":
            return httpx.Response(202, json={"id": "j1"})
        if state["gone"]:
            return httpx.Response(404, text="no such job")
        return httpx.Response(200, json={"s": "run"})
    a = http_adapter(JOB, handler)
    conn = Connection(address=BASE)

    async def go():
        ref = await a.submit(conn, "render", CanonicalRequest(op="ignored", body={"p": 1}))
        live = await a.recover(conn, ref)
        state["gone"] = True
        lost = await a.recover(conn, ref)
        return ref, live, lost
    ref, live, lost = run(go())
    assert ref == JobRef("render", "j1")
    assert live.state is JobState.running and lost is None


def test_events_are_not_part_of_the_http_pattern() -> None:
    a = http_adapter(JOB, lambda r: httpx.Response(200, json={}))

    async def go():
        async for _ in a.events(Connection(address=BASE), JobRef("render", "j1")):
            pass
    with pytest.raises(UnsupportedByAdapter):
        run(go())


def test_invoke_takes_the_operation_name_apart_from_the_request() -> None:
    seen = []
    a = http_adapter(OPS, lambda r: (seen.append(r), httpx.Response(200, json={"data": []}))[1])
    reply = run(a.invoke(Connection(address=BASE), "inventory", CanonicalRequest(op="something-else")))
    assert reply.selected == {"ids": []} and seen[0].url.path == "/v1/models"


# --- the in-tree speech client, behind the interface ---------------------------------------------

SPEECH_HOST = "speech.fk.test:8880"
SPEECH = f"http://{SPEECH_HOST}"


def _speech(body=None, headers=None):
    return CanonicalRequest(op="speech.synthesize", body=body if body is not None else {"input": "hi", "voice": "v", "model": "m"},
                            headers=headers if headers is not None else {"authorization": "Bearer abc", "content-type": "application/json"})


def _serve(fake_upstreams, handler) -> list:
    """Answer the speech address through a real ``httpx.AsyncClient``; return the requests it saw."""
    seen: list = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)
    fake_upstreams.router.add_transport(SPEECH_HOST, HandlerTransport(record))
    return seen


def test_speech_is_posted_exactly_as_the_proxy_always_did(fake_upstreams) -> None:
    seen = _serve(fake_upstreams, lambda r: httpx.Response(200, content=b"RIFFdata", headers={"content-type": "audio/wav"}))
    conn = Connection(address=SPEECH, timeouts=Timeouts(connect_s=10, first_byte_s=120, total_s=120))
    request = _speech({"input": "héllo", "voice": "v", "model": "m"})
    reply = run(InlineTtsAdapter().invoke(conn, "speech.synthesize", request))
    sent = seen[0]
    assert sent.extensions["timeout"] == {"connect": 10, "read": 120, "write": 120, "pool": 120}
    assert (sent.method, str(sent.url)) == ("POST", f"{SPEECH}/v1/audio/speech")
    assert (sent.headers["authorization"], sent.headers["content-type"]) == ("Bearer abc", "application/json")
    assert sent.content == json.dumps({"input": "héllo", "voice": "v", "model": "m"}).encode()  # json.dumps defaults, not compact
    assert (reply.status, reply.raw, reply.headers["content-type"]) == (200, b"RIFFdata", "audio/wav")


def test_a_body_the_caller_already_serialized_is_sent_untouched(fake_upstreams) -> None:
    seen = _serve(fake_upstreams, lambda r: httpx.Response(200, content=b"x"))
    raw = b'{"input":"compact"}'
    run(InlineTtsAdapter().invoke(Connection(address=SPEECH), "speech.synthesize", CanonicalRequest(op="x", raw_body=raw, headers={})))
    assert seen[0].content == raw


def test_speech_refusals_come_back_as_replies_and_transport_errors_as_typed_failures(fake_upstreams) -> None:
    errors: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if errors:
            raise errors[0]
        return httpx.Response(500, content=b"CUDA out of memory")
    _serve(fake_upstreams, handler)
    conn = Connection(address=SPEECH)
    reply = run(InlineTtsAdapter().invoke(conn, "speech.synthesize", _speech()))
    assert (reply.status, reply.raw) == (500, b"CUDA out of memory")

    for error, kind in ((httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectTimeout("slow"), "timeout"),
                        (httpx.ConnectError("refused"), "unreachable"), (RuntimeError("odd"), "aborted")):
        errors[:] = [error]
        with pytest.raises(TransportFailure) as raised:
            run(InlineTtsAdapter().invoke(conn, "speech.synthesize", _speech()))
        assert raised.value.kind == kind and str(raised.value) == str(error)


def test_speech_streams_raw_chunks_and_closes_upstream(fake_upstreams) -> None:
    closed = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"aa"
            yield b"bb"

        async def aclose(self):
            closed.append("upstream")
    _serve(fake_upstreams, lambda r: httpx.Response(200, stream=Chunks()))

    async def go():
        reply = await InlineTtsAdapter().invoke(Connection(address=SPEECH), "speech.stream", _speech())
        got = [chunk async for chunk in reply]
        await reply.aclose()
        return reply, got
    reply, got = run(go())
    assert reply.status == 200 and b"".join(got) == b"aabb" and closed == ["upstream"]


def test_speech_stream_refusal_is_a_buffered_reply(fake_upstreams) -> None:
    _serve(fake_upstreams, lambda r: httpx.Response(503, content=b"busy", headers={"retry-after": "9"}))
    reply = run(InlineTtsAdapter().invoke(Connection(address=SPEECH), "speech.stream", _speech()))
    assert (reply.status, reply.raw, reply.headers["retry-after"]) == (503, b"busy", "9")


def test_the_speech_adapter_declares_what_it_does_not_implement() -> None:
    a = InlineTtsAdapter()
    conn = Connection(address="http://127.0.0.1:8880")
    assert run(a.health(conn)).state == "unknown"
    with pytest.raises(UnknownOperation):
        run(a.invoke(conn, "nope", _speech()))
    for call in (a.inventory(conn), a.submit(conn, "x", _speech()), a.poll(conn, JobRef("x", "1")), a.cancel(conn, JobRef("x", "1")),
                 a.recover(conn, JobRef("x", "1")), a.fetch_assets(conn, JobRef("x", "1"), None)):
        with pytest.raises(UnsupportedByAdapter):
            run(call)

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
    from gateway.engine_protocol.chunked_stt import ChunkedSttAdapter
    for impl in (HttpV1Adapter, InlineTtsAdapter, ChunkedSttAdapter):
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
        # the message names the class of the error and not its text (decision D44)
        assert raised.value.kind == kind and type(error).__name__ in str(raised.value) and str(error) not in str(raised.value)


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


# --- the speech engine's voice operations ------------------------------------------------------


def _wire(request: httpx.Request) -> tuple:
    """What reached the engine, with a multipart boundary (random per request) replaced by one name."""
    content_type = request.headers.get("content-type", "")
    boundary = content_type.split("boundary=", 1)[1] if "boundary=" in content_type else None

    def same(value):
        if boundary is None:
            return value
        return value.replace(boundary.encode(), b"BOUNDARY") if isinstance(value, bytes) else value.replace(boundary, "BOUNDARY")
    return (request.method, str(request.url), [(k, same(v)) for k, v in request.headers.multi_items()], same(request.content),
            request.extensions["timeout"])


VOICE_OPERATIONS = [
    ("speech.voice_check", "/v1/audio/voice-check", {"voice_ref": "/data/voices/k é/reference.wav"}, 120),
    ("speech.design", "/v1/audio/speech", {"input": "Hé, there.", "instructions": "calm", "seed": 3}, 180),
]


@pytest.mark.parametrize("op, path, payload, read_s", VOICE_OPERATIONS)
def test_voice_operations_send_what_the_old_json_call_sent(fake_upstreams, op, path, payload, read_s) -> None:
    seen = _serve(fake_upstreams, lambda r: httpx.Response(200, json={"speaker_similarity": 0.5}))

    async def old_call():
        async with httpx.AsyncClient(timeout=httpx.Timeout(read_s, connect=10)) as client:
            return await client.post(f"{SPEECH}{path}", json=payload)
    old = run(old_call())
    conn = Connection(address=SPEECH, timeouts=Timeouts(connect_s=10, first_byte_s=read_s, total_s=read_s))
    reply = run(InlineTtsAdapter().invoke(conn, op, CanonicalRequest(op=op, body=payload)))
    assert _wire(seen[1]) == _wire(seen[0])
    assert b'", "' not in seen[1].content  # httpx json= is compact; speech.synthesize keeps json.dumps defaults
    assert (reply.status, reply.raw) == (old.status_code, old.content)
    assert reply.body is None  # nothing is parsed until it is asked for: a designed voice is audio and never is
    if op == "speech.voice_check":
        assert reply.json() == old.json()


def test_voice_operations_keep_httpx_failures_typed_and_pass_other_errors_through(fake_upstreams) -> None:
    errors: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise errors[0]
    _serve(fake_upstreams, handler)
    conn = Connection(address=SPEECH)
    for op, _path, payload, _read_s in VOICE_OPERATIONS:
        for error, kind in ((httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("refused"), "unreachable"),
                            (httpx.RemoteProtocolError("reset"), "aborted")):
            errors[:] = [error]
            with pytest.raises(TransportFailure) as raised:
                run(InlineTtsAdapter().invoke(conn, op, CanonicalRequest(op=op, body=payload)))
            assert (raised.value.kind, type(raised.value.__cause__)) == (kind, type(error))
            assert type(error).__name__ in str(raised.value) and str(error) not in str(raised.value)
        errors[:] = [KeyError("not httpx")]
        with pytest.raises(KeyError):
            run(InlineTtsAdapter().invoke(conn, op, CanonicalRequest(op=op, body=payload)))


def test_a_voice_check_answer_that_is_not_json_raises_where_it_is_read(fake_upstreams) -> None:
    _serve(fake_upstreams, lambda r: httpx.Response(200, content=b"not json"))
    reply = run(InlineTtsAdapter().invoke(Connection(address=SPEECH), "speech.voice_check", CanonicalRequest(op="x", body={})))
    with pytest.raises(ValueError):
        reply.json()


def test_each_reviewed_speech_adapter_implements_the_operations_its_description_names() -> None:
    from gateway.engine_protocol import chunked_stt, inline_tts
    docs = description.load_registry()
    assert set(inline_tts.OPERATIONS) == set(docs["inline-tts"].operations)
    assert set(chunked_stt.OPERATIONS) == set(docs["chunked-stt"].operations)


# --- the in-tree transcription client, behind the interface ------------------------------------

STT_HOST = "stt.fk.test:5092"
STT = f"http://{STT_HOST}"


def _serve_stt(fake_upstreams, handler) -> list:
    seen: list = []

    def record(request: httpx.Request) -> httpx.Response:
        request.read()
        seen.append(request)
        return handler(request)
    fake_upstreams.router.add_transport(STT_HOST, HandlerTransport(record))
    return seen


def _stt_adapter():
    from gateway.engine_protocol.chunked_stt import ChunkedSttAdapter
    return ChunkedSttAdapter(description.load_registry()["chunked-stt"])


def _stt_execution(**chunking):
    from pathlib import Path
    from gateway import stt_chunking
    record = {"host": "stt.fk.test", "port": 5092, "timeout": 30, "chunking": {"enabled": False, "spool_dir": "spool", **chunking}}
    return stt_chunking.SttExecutionConfig.from_service(service_id="stt", service_config=record, config_directory=Path("/nonexistent-root"))


class _Upload:
    def __init__(self, path, filename="clip.ogg", content_type="audio/ogg"):
        self.file = open(path, "rb")
        self.filename = filename
        self.content_type = content_type


def _stt_run(tmp_path, uploads, *, stream=False, op="stt.transcribe", **chunking):
    import time
    from gateway import stt_chunking
    from gateway.engine_protocol.chunked_stt import SttRun
    seen = {"outcomes": [], "attempts": [], "cancelled": 0}
    chunking = {"spool_dir": str(tmp_path.parent), **chunking}
    run_call = SttRun(
        op=op, headers={"authorization": "Bearer t", "x-request-id": "r1"}, execution=_stt_execution(**chunking),
        fields=[("model", "m-1"), ("stream", "true")] if stream else [("model", "m-1")],
        multipart_fields={"model": "m-1", "stream": "true"} if stream else {"model": "m-1"},
        uploads=uploads, workspace=tmp_path, cleanup=stt_chunking.SttCleanupOwner(), cancellation_waiters=(),
        deadline_at=time.monotonic() + 60, on_cancel=lambda: seen.__setitem__("cancelled", seen["cancelled"] + 1),
        on_attempt_started=seen["attempts"].append, on_outcome=seen["outcomes"].append, stream=stream,
    )
    return run_call, seen


STT_CONN = Connection(address=STT, timeouts=Timeouts(connect_s=10, first_byte_s=30, total_s=30))


def test_the_transcription_adapter_implements_the_interface_and_declares_what_it_does_not() -> None:
    a = _stt_adapter()
    assert a.interface_version == adapter.ADAPTER_INTERFACE_VERSION and isinstance(a, adapter.EngineAdapter)
    for name in INTERFACE:
        assert inspect.iscoroutinefunction(getattr(type(a), name)) or inspect.isasyncgenfunction(getattr(type(a), name)), name
    assert a.label == "STT backend"
    conn = Connection(address=STT)
    assert run(a.health(conn)).state == "unknown"
    with pytest.raises(UnknownOperation):
        run(a.invoke(conn, "stt.nope", CanonicalRequest(op="x")))
    with pytest.raises(UnsupportedByAdapter):
        run(a.invoke(conn, "stt.transcribe", CanonicalRequest(op="x")))

    async def events():
        async for _ in a.events(conn, JobRef("x", "1")):
            pass
    for call in (a.inventory(conn), a.submit(conn, "x", CanonicalRequest(op="x")), a.poll(conn, JobRef("x", "1")),
                 a.cancel(conn, JobRef("x", "1")), a.recover(conn, JobRef("x", "1")), a.fetch_assets(conn, JobRef("x", "1"), None),
                 events()):
        with pytest.raises(UnsupportedByAdapter):
            run(call)


def test_a_short_clip_is_posted_as_the_old_multipart_call_did(fake_upstreams) -> None:
    from gateway.engine_protocol.chunked_stt import SttUpload
    seen = _serve_stt(fake_upstreams, lambda r: httpx.Response(200, json={"text": "the words"}))
    files = {"file": ("reference.wav", b"RIFF\x00\x01wav-bytes", "audio/wav")}
    data = {"model": "m-1"}

    async def old_call():
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            return await client.post(f"{STT}/v1/audio/transcriptions", files=files, data=data)
    run(old_call())
    conn = Connection(address=STT, timeouts=Timeouts(connect_s=10, first_byte_s=120, total_s=120))
    reply = run(_stt_adapter().invoke(conn, "stt.transcribe", SttUpload(op="stt.transcribe", body=data, parts=files, execution=_stt_execution())))
    assert _wire(seen[1]) == _wire(seen[0])
    assert (reply.status, reply.json()) == (200, {"text": "the words"})

    def refuse(request):
        raise httpx.ConnectError("refused")
    fake_upstreams.router._routes[STT_HOST] = HandlerTransport(refuse)
    with pytest.raises(TransportFailure) as raised:
        run(_stt_adapter().invoke(conn, "stt.transcribe", SttUpload(op="stt.transcribe", body=data, parts=files, execution=_stt_execution())))
    assert raised.value.kind == "unreachable" and "ConnectError" in str(raised.value) and "refused" not in str(raised.value)


@pytest.mark.parametrize("op, route", [("stt.transcribe", "transcriptions"), ("stt.translate", "translations")])
def test_a_direct_upload_is_the_old_streamed_multipart_request(fake_upstreams, tmp_path, op, route) -> None:
    from gateway import stt_chunking
    audio = tmp_path / "upload.bin"
    audio.write_bytes(b"OggS" + bytes(range(256)) * 4)
    seen = _serve_stt(fake_upstreams, lambda r: httpx.Response(200, json={"text": "hi"}, headers={"x-engine": "e"}))

    async def go():
        call, notes = _stt_run(tmp_path, [("file", _Upload(audio))], op=op)
        old = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10.0))
        with audio.open("rb") as handle:
            outgoing = old.build_request("POST", f"{STT}/v1/audio/{route}", headers=call.headers, data=call.multipart_fields,
                                         files=[("file", ("clip.ogg", handle, "audio/ogg"))])
            answer = await old.send(outgoing, stream=True)
            await answer.aread()
            await answer.aclose()
        await old.aclose()
        reply = await _stt_adapter().invoke(STT_CONN, op, call)
        names = [name for name, _action in call.cleanup._actions]
        await call.cleanup.aclose()
        return reply, notes, names
    reply, notes, names = run(go())
    assert _wire(seen[1]) == _wire(seen[0])
    assert (reply.mode, reply.status, reply.raw, reply.headers["x-engine"]) == ("direct", 200, b'{"text":"hi"}', "e")
    assert notes["outcomes"] == [stt_chunking.ChunkingOutcome("passthrough", 0, "ineligible_envelope")]
    assert names == ["http_client"]  # every upload handle and the response were released in order


def test_several_uploads_skip_the_planner_and_go_in_one_request(fake_upstreams, tmp_path) -> None:
    from gateway import stt_chunking
    first, second = tmp_path / "a.bin", tmp_path / "b.bin"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    seen = _serve_stt(fake_upstreams, lambda r: httpx.Response(200, json={"text": "x"}))

    async def go():
        call, notes = _stt_run(tmp_path, [("file", _Upload(first, "a.wav", None)), ("extra", _Upload(second, None, "audio/wav"))])
        reply = await _stt_adapter().invoke(STT_CONN, "stt.transcribe", call)
        await call.cleanup.aclose()
        return reply, notes
    reply, notes = run(go())
    assert reply.mode == "direct" and notes["outcomes"] == [stt_chunking.ChunkingOutcome("passthrough", 0, "multiple_files")]
    body = seen[0].content
    assert b'name="file"; filename="a.wav"\r\nContent-Type: application/octet-stream\r\n\r\none' in body
    assert b'name="extra"; filename="audio"\r\nContent-Type: audio/wav\r\n\r\ntwo' in body


@pytest.mark.parametrize("error, kind, text", [
    (httpx.ReadTimeout("slow"), "SttBackendTimeout", "STT backend timed out"),
    (httpx.ConnectError("refused"), "SttBackendUnavailable", "STT backend is unavailable"),
    (httpx.RemoteProtocolError("reset"), "SttBackendConnectionLost", "STT backend dropped the connection"),
])
def test_transport_failures_of_an_upload_are_the_typed_stt_failures(fake_upstreams, tmp_path, error, kind, text) -> None:
    from gateway import stt_chunking
    audio = tmp_path / "upload.bin"
    audio.write_bytes(b"x")

    def handler(request):
        raise error
    _serve_stt(fake_upstreams, handler)

    async def go():
        call, _notes = _stt_run(tmp_path, [("file", _Upload(audio))])
        try:
            await _stt_adapter().invoke(STT_CONN, "stt.transcribe", call)
        finally:
            await call.cleanup.aclose()
    with pytest.raises(stt_chunking.SttExecutionError) as raised:
        run(go())
    assert (type(raised.value).__name__, str(raised.value), raised.value.__cause__) == (kind, text, error)


def test_a_native_stream_is_relayed_raw_within_the_byte_budget(fake_upstreams, tmp_path) -> None:
    from gateway import stt_chunking
    from gateway.engine_protocol.types import StreamReply
    audio = tmp_path / "upload.bin"
    audio.write_bytes(b"x")

    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: {\"delta\": \"he\"}\n\n"
            yield b"data: {\"delta\": \"llo\"}\n\n"
    seen = _serve_stt(fake_upstreams, lambda r: httpx.Response(200, stream=Events(), headers={"content-type": "text/event-stream"}))

    async def go(**chunking):
        call, _notes = _stt_run(tmp_path, [("file", _Upload(audio))], stream=True, **chunking)
        reply = await _stt_adapter().invoke(STT_CONN, "stt.transcribe", call)
        names = [name for name, _action in call.cleanup._actions]
        try:
            return reply, [chunk async for chunk in reply], names
        finally:
            await reply.aclose()
            await call.cleanup.aclose()
    reply, chunks, names = run(go())
    assert isinstance(reply, StreamReply) and reply.status == 200
    assert b"".join(chunks) == b'data: {"delta": "he"}\n\ndata: {"delta": "llo"}\n\n'
    assert names == ["http_client", "stream_upload_handle", "upstream_response"]  # core's cleanup closes them after the relay
    assert b'filename="clip.ogg"\r\nContent-Type: application/octet-stream' in seen[0].content
    with pytest.raises(stt_chunking.SttResponseLimitExceeded):
        run(go(max_response_bytes=30))


def test_a_refused_stream_is_a_buffered_reply(fake_upstreams, tmp_path) -> None:
    audio = tmp_path / "upload.bin"
    audio.write_bytes(b"x")
    _serve_stt(fake_upstreams, lambda r: httpx.Response(503, content=b'{"error":"busy"}'))

    async def go():
        call, _notes = _stt_run(tmp_path, [("file", _Upload(audio))], stream=True)
        try:
            return await _stt_adapter().invoke(STT_CONN, "stt.transcribe", call)
        finally:
            await call.cleanup.aclose()
    reply = run(go())
    assert (reply.mode, reply.status, reply.raw) == ("stream_refused", 503, b'{"error":"busy"}')

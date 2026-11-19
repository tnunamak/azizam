"""The reviewed speech and transcription adapters reach an engine on another computer only through its guarded transport.

``bound_wire`` is the one place those adapters get an HTTP client. These tests close the side doors:

- a socket opens only while ``BoundTransport.send`` is running (a spy on ``send``, and the recorder of the OS socket);
- with a remote connection, the ordinary ``engine_egress`` client is never built;
- a connection marked remote without authority, or with a capability that is not the transport's, opens no socket;
- a revoke stops the next request and an operation in flight;
- the two adapter modules hold no client constructor of their own.

Only the OS socket boundary is replaced (``SocketRecorder``); httpx, httpcore, the guard and the checked address are real.
Test addresses are documentation ranges.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from gateway import engine_egress
from gateway.engine_protocol import bound_wire, chunked_stt, inline_tts, reviewed
from gateway.engine_protocol.errors import ProtocolError
from gateway.engine_protocol.types import CanonicalRequest, Connection, Timeouts
from gateway.remote_engine import BoundTransport, ClientFactory
from gateway.remote_engine_transport import RemoteNetworkTransport
from tests.support.remote_protocol import SocketRecorder

ORIGIN = "https://engine.example"
PUBLIC = "93.184.216.34"
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24


def _answer(body: bytes, content_type: str = "application/json") -> bytes:
    return (b"HTTP/1.1 200 OK\r\nContent-Type: " + content_type.encode() + b"\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n" + body)


def _registry(protocol: str, task: str) -> dict:
    record = {
        "id": "voice-box", "enabled": True,
        "config": {
            "cost_class": "free-local",
            "connection": {"protocol": protocol, "address": ORIGIN, "transport": "tls",
                           "remote_dispatch": {"version": 1, "enabled": True, "tasks": [task]}},
        },
    }
    return {"services": {"voice-box": record}}


class Rig:
    def __init__(self, monkeypatch, protocol: str, task: str) -> None:
        self.registry = _registry(protocol, task)
        self.recorder = SocketRecorder(monkeypatch)

        async def resolve(host: str, port: int) -> list[str]:
            return [PUBLIC]
        self.factory = ClientFactory(lambda: self.registry,
                                     connector=lambda bound: RemoteNetworkTransport(bound, resolve=resolve))
        self.capability = self.factory.admit("voice-box")
        self.bound = self.factory.transport(self.capability)
        self.conn = Connection(address=ORIGIN, timeouts=Timeouts(connect_s=5, first_byte_s=20, total_s=30),
                               remote_capability=self.capability, remote_transport=self.bound)
        self.outside: list[tuple[str, int]] = []
        real_send = BoundTransport.send
        rig = self

        async def spied(bound, request, timeouts):
            rig.depth = getattr(rig, "depth", 0) + 1
            try:
                return await real_send(bound, request, timeouts)
            finally:
                rig.depth -= 1
        monkeypatch.setattr(BoundTransport, "send", spied)
        self.depth = 0
        self.recorder.on_connect = self._on_connect
        # The ordinary client may not be built for a remote connection: that is the side door.
        real_client = engine_egress.async_client

        def no_side_door(*args, **kwargs):
            if kwargs.get("_remote_bound") is None:     # the guarded transport builds its own with its binding
                self.outside.append(("async_client", 0))
            return real_client(*args, **kwargs)
        monkeypatch.setattr(engine_egress, "async_client", no_side_door)

    def _on_connect(self, host: str, port: int) -> None:
        if not self.depth:
            self.outside.append((host, port))

    def assert_guarded(self) -> None:
        assert not self.outside, f"the network was reached outside the guarded transport: {self.outside}"
        assert self.recorder.connections, "the test sent nothing"
        assert set(self.recorder.connections) == {(PUBLIC, 443)}
        assert set(self.recorder.tls) == {"engine.example"}


def run(coro):
    return asyncio.run(coro)


SPEECH_BODY = {"model": "m", "input": "hello", "voice": "alloy"}


@pytest.mark.parametrize("op, answer, route", [
    ("speech.synthesize", _answer(WAV, "audio/wav"), "/v1/audio/speech"),
    ("speech.voice_check", _answer(b'{"ok":true}'), "/v1/audio/voice-check"),
    ("speech.design", _answer(b'{"voice":"v"}'), "/v1/audio/speech"),
])
def test_a_speech_call_reaches_the_network_only_inside_a_guarded_send(monkeypatch, op, answer, route) -> None:
    rig = Rig(monkeypatch, "inline-tts", "speech")
    rig.recorder.responses = [answer]

    reply = run(inline_tts.InlineTtsAdapter().invoke(
        rig.conn, op, CanonicalRequest(op=op, body=SPEECH_BODY, headers={"content-type": "application/json"})))

    assert reply.status == 200
    assert rig.recorder.paths == [route]
    rig.assert_guarded()


def test_a_streamed_speech_call_is_guarded_too(monkeypatch) -> None:
    rig = Rig(monkeypatch, "inline-tts", "speech")
    rig.recorder.responses = [_answer(WAV, "audio/wav")]

    async def go() -> bytes:
        reply = await inline_tts.InlineTtsAdapter().invoke(
            rig.conn, "speech.stream", CanonicalRequest(op="speech.stream", body=SPEECH_BODY, headers={"content-type": "application/json"}))
        try:
            return b"".join([chunk async for chunk in reply])
        finally:
            await reply.aclose()
    assert run(go()) == WAV
    rig.assert_guarded()


def test_a_transcription_upload_reaches_the_network_only_inside_a_guarded_send(monkeypatch) -> None:
    from tests.test_engine_protocol_adapter import _stt_adapter, _stt_execution
    rig = Rig(monkeypatch, "chunked-stt", "transcription")
    rig.recorder.responses = [_answer(b'{"text":"the words"}')]
    files = {"file": ("clip.wav", b"RIFF-audio", "audio/wav")}

    reply = run(_stt_adapter().invoke(rig.conn, "stt.transcribe", chunked_stt.SttUpload(
        op="stt.transcribe", body={"model": "m"}, parts=files, execution=_stt_execution())))

    assert (reply.status, json.loads(reply.raw)) == (200, {"text": "the words"})
    assert rig.recorder.paths == ["/v1/audio/transcriptions"]
    rig.assert_guarded()


def test_a_transcription_run_reaches_the_network_only_inside_a_guarded_send(monkeypatch, tmp_path) -> None:
    from tests.test_engine_protocol_adapter import _stt_adapter, _stt_run
    rig = Rig(monkeypatch, "chunked-stt", "transcription")
    rig.recorder.responses = [_answer(b'{"text":"run words"}')]
    clip = tmp_path / "clip.wav"
    clip.write_bytes(b"RIFF-audio")

    class Upload:
        def __init__(self) -> None:
            self.file = open(clip, "rb")
            self.filename, self.content_type = "clip.wav", "audio/wav"
    request, _seen = _stt_run(tmp_path, [("file", Upload())])
    request.headers = {}

    reply = run(_stt_adapter().invoke(rig.conn, "stt.transcribe", request))

    assert reply.status == 200
    rig.assert_guarded()


# --- a connection without authority opens nothing ---------------------------------------------------------------------

def test_a_remote_connection_without_authority_opens_no_socket(monkeypatch) -> None:
    recorder = SocketRecorder(monkeypatch)
    monkeypatch.setattr(engine_egress, "is_remote_destination", lambda *args, **kwargs: True)
    conn = Connection(address=ORIGIN, timeouts=Timeouts(connect_s=1, first_byte_s=1, total_s=1))
    from tests.test_engine_protocol_adapter import _stt_adapter, _stt_execution
    with pytest.raises(ProtocolError):
        run(inline_tts.InlineTtsAdapter().invoke(conn, "speech.synthesize", CanonicalRequest(op="speech.synthesize", body=SPEECH_BODY)))
    with pytest.raises(ProtocolError):
        run(_stt_adapter().invoke(conn, "stt.transcribe", chunked_stt.SttUpload(
            op="stt.transcribe", body={}, parts={"file": ("c.wav", b"x", "audio/wav")}, execution=_stt_execution())))
    assert not recorder.connections and not recorder.writes


def test_a_capability_that_is_not_the_transports_is_refused_before_any_socket(monkeypatch) -> None:
    rig = Rig(monkeypatch, "inline-tts", "speech")
    other = rig.factory.admit("voice-box")
    conn = Connection(address=ORIGIN, remote_capability=other, remote_transport=rig.bound)
    with pytest.raises(ProtocolError):
        run(inline_tts.InlineTtsAdapter().invoke(conn, "speech.synthesize", CanonicalRequest(op="speech.synthesize", body=SPEECH_BODY)))
    assert not rig.recorder.connections


def test_a_capability_without_its_transport_is_refused_before_any_socket(monkeypatch) -> None:
    rig = Rig(monkeypatch, "inline-tts", "speech")
    conn = Connection(address=ORIGIN, remote_capability=rig.capability)
    with pytest.raises(ProtocolError):
        run(inline_tts.InlineTtsAdapter().invoke(conn, "speech.synthesize", CanonicalRequest(op="speech.synthesize", body=SPEECH_BODY)))
    assert not rig.recorder.connections


# --- the per-call checks of the http-v1 path --------------------------------------------------------------------------

def test_an_owner_change_between_binding_and_the_call_is_refused_before_any_socket(monkeypatch) -> None:
    rig = Rig(monkeypatch, "inline-tts", "speech")
    rig.registry["services"]["voice-box"]["config"]["connection"]["remote_dispatch"]["enabled"] = False

    with pytest.raises(ProtocolError):
        run(inline_tts.InlineTtsAdapter().invoke(rig.conn, "speech.synthesize", CanonicalRequest(op="speech.synthesize", body=SPEECH_BODY)))

    assert not rig.recorder.connections and not rig.recorder.writes


def test_a_revoke_stops_the_next_request(monkeypatch) -> None:
    rig = Rig(monkeypatch, "inline-tts", "speech")
    rig.recorder.responses = [_answer(WAV, "audio/wav"), _answer(WAV, "audio/wav")]
    adapter = inline_tts.InlineTtsAdapter()
    request = CanonicalRequest(op="speech.synthesize", body=SPEECH_BODY, headers={"content-type": "application/json"})
    assert run(adapter.invoke(rig.conn, "speech.synthesize", request)).status == 200
    before = len(rig.recorder.connections)

    rig.factory.invalidate()

    with pytest.raises((ProtocolError, asyncio.CancelledError)):
        run(adapter.invoke(rig.conn, "speech.synthesize", request))
    assert len(rig.recorder.connections) == before


def test_a_revoke_cancels_a_request_in_flight(monkeypatch) -> None:
    rig = Rig(monkeypatch, "inline-tts", "speech")
    rig.recorder.block_read = asyncio.Event()      # the engine never answers
    rig.recorder.responses = [_answer(WAV, "audio/wav")]
    request = CanonicalRequest(op="speech.synthesize", body=SPEECH_BODY, headers={"content-type": "application/json"})

    async def go() -> str:
        call = asyncio.ensure_future(inline_tts.InlineTtsAdapter().invoke(rig.conn, "speech.synthesize", request))
        for _ in range(200):
            if rig.recorder.writes:
                break
            await asyncio.sleep(0.01)
        assert rig.recorder.writes, "the request never reached the engine"
        rig.factory.invalidate()
        try:
            await asyncio.wait_for(call, 5)
        except asyncio.TimeoutError:
            return "still running"
        except BaseException as error:  # noqa: BLE001 - the kind of stop is not the point; that it stopped is
            return type(error).__name__
        return "answered"
    assert run(go()) not in ("still running", "answered")


def test_a_header_the_guard_does_not_allow_is_refused_before_any_socket(monkeypatch) -> None:
    rig = Rig(monkeypatch, "inline-tts", "speech")
    request = CanonicalRequest(op="speech.synthesize", body=SPEECH_BODY,
                               headers={"content-type": "application/json", "x-forwarded-for": "198.51.100.7"})

    with pytest.raises(Exception):
        run(inline_tts.InlineTtsAdapter().invoke(rig.conn, "speech.synthesize", request))

    assert not rig.recorder.connections and not rig.recorder.writes


def test_the_wire_shows_only_the_allowed_headers_and_the_request_body(monkeypatch) -> None:
    rig = Rig(monkeypatch, "inline-tts", "speech")
    rig.recorder.responses = [_answer(WAV, "audio/wav")]
    request = CanonicalRequest(op="speech.synthesize", body=SPEECH_BODY, headers={"content-type": "application/json"})

    run(inline_tts.InlineTtsAdapter().invoke(rig.conn, "speech.synthesize", request))

    head = rig.recorder.writes[0].decode().lower()
    names = {line.split(":", 1)[0] for line in head.split("\r\n")[1:] if ":" in line}
    assert names <= {"host", "content-type", "content-length", "accept", "accept-encoding", "user-agent", "connection"}
    assert json.loads(b"".join(rig.recorder.writes[1:]) or b"{}") == SPEECH_BODY


# --- the adapters hold no client of their own ------------------------------------------------------------------------

ADAPTERS = (inline_tts, chunked_stt)
CLIENT_CONSTRUCTORS = re.compile(r"httpx\.(?:Async)?Client\(|engine_egress\.|HTTPTransport\(|AsyncHTTPTransport\(|socket\.|urllib|requests\.|aiohttp")


@pytest.mark.parametrize("module", ADAPTERS, ids=lambda module: module.__name__)
def test_a_reviewed_adapter_builds_no_http_client_of_its_own(module) -> None:
    source = Path(module.__file__).read_text()
    hits = [line.strip() for line in source.splitlines() if CLIENT_CONSTRUCTORS.search(line) and not line.lstrip().startswith("#")]
    assert not hits, f"{module.__name__} reaches for the network itself: {hits}"
    assert "bound_wire.client(" in source


def test_the_bound_adapter_list_names_exactly_the_adapters_that_use_bound_wire() -> None:
    users = {name for name, module in (("inline_speech", inline_tts), ("chunked_transcription", chunked_stt))
             if "bound_wire.client(" in Path(module.__file__).read_text()}
    assert users == set(reviewed._REMOTE_BOUND_IMPLEMENTATIONS)


def test_node_graph_does_not_claim_the_guard() -> None:
    assert "graph_submission" not in reviewed._REMOTE_BOUND_IMPLEMENTATIONS
    assert bound_wire.client.__module__ == "gateway.engine_protocol.bound_wire"

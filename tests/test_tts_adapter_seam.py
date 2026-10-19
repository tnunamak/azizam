"""The speech dispatch reaches its engine through the adapter interface, and nothing it sends or answers changes."""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import httpx

import proxy
from gateway.engine_protocol import chunked_stt, inline_tts, reviewed
from gateway.engine_protocol.errors import TransportFailure
from gateway.engine_protocol.httpx_wire import reply_from
from gateway.engine_protocol.types import Reply

from tests.speech_tier_helpers import RouteCase, _Chunks, _wav_bytes

WAV = _wav_bytes(24000, 1)


class Recording(inline_tts.InlineTtsAdapter):
    def __init__(self) -> None:
        self.calls: list = []

    async def invoke(self, conn, op, request):
        self.calls.append((conn, op, request))
        return await super().invoke(conn, op, request)


class Refusing(inline_tts.InlineTtsAdapter):
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    async def invoke(self, conn, op, request):
        raise self.failure


class SpeechSeamTests(RouteCase):
    def _use(self, adapter, adapter_id: str = "inline-tts") -> None:
        """Answer every record that selects ``adapter_id`` with ``adapter``, as a reviewed factory would."""
        reviewed.register(adapter_id, lambda _doc: adapter)

        def restore() -> None:
            reviewed._FACTORIES.pop(adapter_id, None)
            reviewed._BUILT.clear()
        self.addCleanup(restore)

    def test_buffered_speech_is_one_adapter_call_that_sends_the_bytes_the_engine_receives(self) -> None:
        recording = Recording()
        self._use(recording)
        seen = self.use_upstream(lambda r: httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"}))
        resp = self.client.post("/v1/audio/speech", json={"model": "tts-fast", "voice": "shimmer", "input": "hello", "response_format": "wav"})
        self.assertEqual(resp.status_code, 200)
        [(conn, op, request)] = recording.calls
        self.assertEqual(op, "speech.synthesize")
        self.assertEqual(conn.address, "http://127.0.0.1:8010")
        self.assertEqual((conn.timeouts.connect_s, conn.timeouts.first_byte_s), (10, proxy.TTS_TIMEOUT))
        self.assertEqual(request.raw_body, seen[0].content)
        self.assertEqual(json.loads(request.raw_body)["input"], "hello")
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(str(seen[0].url), "http://127.0.0.1:8010/v1/audio/speech")

    def test_streamed_speech_is_the_stream_operation(self) -> None:
        recording = Recording()
        self._use(recording)
        pcm = struct.pack("<h", 2500) * 4410
        self.use_upstream(lambda r: httpx.Response(200, stream=_Chunks([pcm])))
        self.cfg["model_aliases"]["speech"]["engines"]["supertonic"]["native_sample_rate"] = 24000
        resp = self.client.post("/v1/audio/speech", json={"model": "supertonic", "voice": "alloy", "input": "hi", "response_format": "pcm"})
        self.assertEqual((resp.status_code, resp.content), (200, pcm))
        self.assertEqual([op for _c, op, _r in recording.calls], ["speech.stream"])

    def test_a_timeout_is_a_504_whichever_way_the_adapter_reports_it(self) -> None:
        self._use(Refusing(TransportFailure("timeout", "read timed out")))
        resp = self.client.post("/v1/audio/speech", json={"model": "tts-fast", "voice": "alloy", "input": "x", "response_format": "wav"})
        self.assertEqual((resp.status_code, resp.json()["error"]["message"]), (504, "TTS request timeout"))

    def test_an_unreachable_engine_is_a_502_naming_it(self) -> None:
        self._use(Refusing(TransportFailure("unreachable", "refused")))
        resp = self.client.post("/v1/audio/speech", json={"model": "tts-fast", "voice": "alloy", "input": "x", "response_format": "wav"})
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (502, "tts_backend_unavailable"))
        self.assertRegex(resp.json()["error"]["message"], r"^TTS backend unavailable at http://127\.0\.0\.1:\d+$")

    def test_any_other_failure_keeps_its_original_text(self) -> None:
        self._use(Refusing(TransportFailure("aborted", "weird failure")))
        resp = self.client.post("/v1/audio/speech", json={"model": "tts-fast", "voice": "alloy", "input": "x", "response_format": "wav"})
        self.assertEqual((resp.status_code, resp.json()["error"]["message"]), (502, "TTS proxy error: weird failure"))

    def test_a_refused_stream_start_keeps_status_body_and_retry_after(self) -> None:
        class Refused(inline_tts.InlineTtsAdapter):
            async def invoke(self, conn, op, request):
                return Reply(429, {"content-type": "application/json", "retry-after": "7"}, raw=b'{"error":"slow down"}')
        self._use(Refused())
        self.cfg["model_aliases"]["speech"]["engines"]["supertonic"]["native_sample_rate"] = 24000
        resp = self.client.post("/v1/audio/speech", json={"model": "supertonic", "voice": "alloy", "input": "x", "response_format": "pcm"})
        self.assertEqual((resp.status_code, resp.headers["retry-after"], resp.content), (429, "7", b'{"error":"slow down"}'))


class Answering(inline_tts.InlineTtsAdapter):
    """Records each call and answers it from ``reply`` without a wire."""

    def __init__(self, reply) -> None:
        self.calls: list = []
        self.reply = reply

    async def invoke(self, conn, op, request):
        self.calls.append((conn, op, request))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class SelectionByDataTests(RouteCase):
    _use = SpeechSeamTests._use

    def _selections(self) -> list:
        from unittest.mock import patch
        asked: list = []
        real = proxy.reviewed_adapters.adapter_for_backend

        def recording(record, capability):
            asked.append((record, capability))
            return real(record, capability)
        patcher = patch.object(proxy.reviewed_adapters, "adapter_for_backend", recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked

    def test_the_speech_route_takes_its_adapter_from_the_selected_services_record(self) -> None:
        asked = self._selections()
        self.use_upstream(lambda r: httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"}))
        resp = self.client.post("/v1/audio/speech", json={"model": "tts-fast", "voice": "shimmer", "input": "hello", "response_format": "wav"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(asked, [(self.cfg["backends"]["tts-supertonic"], "speech")])

    def test_a_record_that_names_another_reviewed_protocol_gets_that_adapter(self) -> None:
        recording = Recording()
        self._use(recording, "node-graph")
        self.cfg["backends"]["tts-supertonic"]["protocol"] = "node-graph"
        self.use_upstream(lambda r: httpx.Response(200, content=WAV, headers={"content-type": "audio/wav"}))
        resp = self.client.post("/v1/audio/speech", json={"model": "tts-fast", "voice": "shimmer", "input": "hello", "response_format": "wav"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([op for _c, op, _r in recording.calls], ["speech.synthesize"])


class VoiceOperationTests(RouteCase):
    """Voice checks, voice design and reference transcripts go through the adapter their service's record selects."""

    _use = SpeechSeamTests._use

    def test_the_quality_probe_is_the_voice_check_operation(self) -> None:
        engine = Answering(reply_from(httpx.Response(200, json={"speaker_similarity": 0.71, "encoder": "ecapa"})))
        self._use(engine)
        result = asyncio.run(proxy._probe_voice_quality(Path("/voices/a/reference.wav")))
        self.assertEqual(result, {"speaker_similarity": 0.71, "encoder": "ecapa", "engine": "chatterbox"})
        [(conn, op, request)] = engine.calls
        backend = self.cfg["backends"]["tts-chatterbox"]
        self.assertEqual((op, request.body, request.raw_body), ("speech.voice_check", {"voice_ref": "/voices/a/reference.wav"}, None))
        self.assertEqual(conn.address, f"http://{backend['host']}:{backend['port']}")
        self.assertEqual((conn.timeouts.connect_s, conn.timeouts.first_byte_s), (10, 120))

    def test_a_probe_that_cannot_reach_the_engine_names_the_original_error(self) -> None:
        from unittest.mock import AsyncMock, patch
        failure = TransportFailure("unreachable", "refused")
        failure.__cause__ = httpx.ConnectError("refused")  # as the adapter raises it: from the httpx error
        self._use(Answering(failure))
        with patch.object(proxy.asyncio, "sleep", AsyncMock()):
            result = asyncio.run(proxy._probe_voice_quality(Path("/voices/a/reference.wav")))
        self.assertEqual(result, {"error": "engine_error: ConnectError"})  # the reason the probe always stored

    def test_voice_design_is_the_design_operation(self) -> None:
        from gateway import voice_design
        self.cfg["voices"] = {"design_engine": "omnivoice"}
        engine = Answering(Reply(200, {"content-type": "audio/wav"}, raw=WAV))
        self._use(engine)
        self.assertEqual(asyncio.run(proxy._design_voice("calm", "Hello.", 7)), WAV)
        [(conn, op, request)] = engine.calls
        backend = self.cfg["backends"][self.cfg["model_aliases"]["speech"]["engines"]["omnivoice"]["service"]]
        expected = voice_design.design_request(proxy._voice_design_settings(), script="Hello.", instructions="calm", seed=7)
        self.assertEqual((op, request.body), ("speech.design", expected))
        self.assertEqual(conn.address, f"http://{backend['host']}:{backend['port']}")
        self.assertEqual((conn.timeouts.connect_s, conn.timeouts.first_byte_s), (10, 180))

    def test_voice_design_failures_keep_their_texts(self) -> None:
        from gateway import custom_voices
        self.cfg["voices"] = {"design_engine": "omnivoice"}
        for answer, expected in (
            (TransportFailure("timeout", "read timed out"), (502, "backend_error", "Voice design failed: read timed out")),
            (Reply(400, {}, raw=b"no"), (400, "invalid_prompt", "The voice design engine rejected this description.")),
            (Reply(500, {}, raw=b"no"), (502, "backend_error", "Voice design failed with HTTP 500.")),
        ):
            with self.subTest(answer=answer):
                reviewed._BUILT.clear()
                self._use(Answering(answer))
                with self.assertRaises(custom_voices.VoiceError) as raised:
                    asyncio.run(proxy._design_voice("calm", "Hello.", 7))
                self.assertEqual((raised.exception.status, raised.exception.code, raised.exception.message), expected)

    def test_a_reference_transcript_is_one_transcription_through_the_stt_records_adapter(self) -> None:
        class Transcribing(chunked_stt.ChunkedSttAdapter):
            calls: list = []

            async def invoke(self, conn, op, request):
                self.calls.append((conn, op, request))
                return reply_from(httpx.Response(200, json={"text": "  the exact words "}))
        self._use(Transcribing(), "chunked-stt")
        clip = Path(self.tmp.name) / "reference.wav"
        clip.write_bytes(WAV)
        stored: list = []
        from unittest.mock import patch
        with patch.object(proxy, "_voice_store", lambda: type("S", (), {"update_voice": lambda *a: stored.append(a[1:])})()):
            text = asyncio.run(proxy._reference_transcript("key-a", {"id": "voice_1"}, clip))
        self.assertEqual(text, "the exact words")
        [(conn, op, request)] = Transcribing.calls
        stt = self.cfg["backends"]["stt"]
        self.assertEqual(op, "stt.transcribe")
        self.assertEqual(conn.address, f"http://{stt['host']}:{stt['port']}")
        self.assertEqual((conn.timeouts.connect_s, conn.timeouts.first_byte_s), (10, 120))
        self.assertEqual(request.parts, {"file": ("reference.wav", WAV, "audio/wav")})
        self.assertEqual(request.body, {"model": str(stt.get("default_model") or "parakeet-tdt-0.6b-v3")})
        self.assertEqual(stored, [("key-a", "voice_1", {"transcript": "the exact words"})])

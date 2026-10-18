"""The speech dispatch reaches its engine through the adapter interface, and nothing it sends or answers changes."""

from __future__ import annotations

import json
import struct

import httpx

import proxy
from gateway.engine_protocol import inline_tts
from gateway.engine_protocol.errors import TransportFailure
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
    def _use(self, adapter) -> None:
        from unittest.mock import patch
        patcher = patch.object(proxy, "_TTS_ADAPTER", adapter)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.assertIn("http://127.0.0.1:8010", resp.json()["error"]["message"])

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

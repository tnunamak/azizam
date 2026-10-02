"""STT tiers: service choice by model, on-demand start, stream=true, WAV conversion."""

from __future__ import annotations

import asyncio
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.requests import Request

import proxy
from gateway import speech_stream

from tests.speech_tier_helpers import HAVE_FFMPEG, SttCase, _Chunks, _multipart, _wav_bytes


class SttServiceSelectionTests(SttCase):
    def test_engine_alias_selects_its_own_service_and_model(self) -> None:
        seen = self._json_upstream()
        resp = self._post([("model", "stt-fast")])
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(seen[0].url.port, 8012)
        # Plain requests use the offline-mode model id (fastest final text).
        self.assertIn(b"\r\n\r\nnemotron-3.5-asr-streaming-0.6b\r\n", seen[0].content)
        self.assertEqual(resp.json()["text"], "hello world")

    def test_openai_names_follow_the_alias_tiers(self) -> None:
        cases = {"whisper-1": 5092, "stt": 5092, "stt-quality": 5092, "gpt-4o-mini-transcribe": 8012, "canary": 8013}
        seen = self._json_upstream()
        for model, port in cases.items():
            with self.subTest(model=model):
                resp = self._post([("model", model)])
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(seen[-1].url.port, port)

    def test_unknown_model_uses_the_default_service(self) -> None:
        seen = self._json_upstream()
        self.assertEqual(self._post([("model", "parakeet-tdt-0.6b-v3")]).status_code, 200)
        self.assertEqual(seen[0].url.port, 5092)

    def test_model_after_a_small_file_still_selects_the_service(self) -> None:
        seen = self._json_upstream()
        self.assertEqual(self._post([("model", "stt-fast")], file_first=True).status_code, 200)
        self.assertEqual(seen[0].url.port, 8012)

    def test_model_hidden_behind_a_huge_file_is_a_clear_400_not_a_wrong_engine(self) -> None:
        self._json_upstream()
        body, ctype = _multipart([("model", "stt-fast")], b"A" * 20000, file_first=True)
        messages = [
            {"type": "http.request", "body": body[i : i + 1000], "more_body": i + 1000 < len(body)}
            for i in range(0, len(body), 1000)
        ]

        async def receive():
            if messages:
                return messages.pop(0)
            await asyncio.Future()

        request = Request(
            {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions", "headers": [(b"content-type", ctype.encode())]},
            receive,
        )
        with patch.object(speech_stream, "PEEK_LIMIT_BYTES", 2048):
            resp = asyncio.run(proxy._dispatch_selected_stt_route(request, proxy.NO_AUTH_CONTEXT, operation="transcriptions"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(json.loads(resp.body)["error"]["code"], "stt_model_field_order")

    def test_down_engine_is_started_through_its_lifecycle_driver(self) -> None:
        self.health.return_value = False
        self._json_upstream()
        self.assertEqual(self._post([("model", "stt-fast")]).status_code, 200)
        self.ensure.assert_awaited_once_with("stt-nemotron", None)

    def test_default_service_with_external_lifecycle_is_not_probed(self) -> None:
        self._json_upstream()
        self.assertEqual(self._post([("model", "whisper-1")]).status_code, 200)
        self.health.assert_not_awaited()

    def test_engine_that_will_not_start_is_a_503(self) -> None:
        self.health.return_value = False
        self.ensure.return_value = False
        resp = self._post([("model", "stt-fast")])
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "backend_unavailable")

    def test_undeployed_engine_by_name_is_still_a_coded_400(self) -> None:
        del self.cfg["backends"]["stt-canary"]
        resp = self._post([("model", "canary")])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "model_not_deployed")


class SttStreamingTests(SttCase):
    def test_native_streaming_engine_relays_events_as_they_arrive(self) -> None:
        events = [
            b'data: {"type":"transcript.text.delta","delta":"hello"}\n\n',
            b'data: {"type":"transcript.text.delta","delta":" world"}\n\n',
            b'data: {"type":"transcript.text.done","text":"hello world"}\n\n',
            b"data: [DONE]\n\n",
        ]
        seen = self.use_upstream(lambda r: httpx.Response(200, stream=_Chunks(events), headers={"content-type": "text/event-stream"}))
        resp = self._post([("model", "stt-fast"), ("stream", "true")])
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(resp.content, b"".join(events))
        self.assertIn(b'name="stream"', seen[0].content)
        self.assertEqual(seen[0].url.port, 8012)
        # stream=true selects the same weights loaded in streaming mode.
        self.assertIn(b"\r\n\r\nnemotron-3.5-asr-streaming-0.6b-stream\r\n", seen[0].content)

    def test_a_streamed_response_releases_its_workspace_and_lease_when_the_body_ends(self) -> None:
        self.use_upstream(lambda r: httpx.Response(200, stream=_Chunks([b'data: {"type":"transcript.text.done","text":"x"}\n\n'])))
        self.assertEqual(self._post([("model", "stt-fast"), ("stream", "true")]).status_code, 200)
        self.assertEqual(list(Path(self.tmp.name).glob("*")), [])

    def test_engine_error_before_streaming_is_a_normal_response(self) -> None:
        self.use_upstream(lambda r: httpx.Response(500, json={"error": {"message": "boom"}}))
        resp = self._post([("model", "stt-fast"), ("stream", "true")])
        self.assertEqual(resp.status_code, 500)

    def test_engine_without_streaming_answers_stream_true_as_one_delta(self) -> None:
        seen = self._json_upstream("quiet words")
        resp = self._post([("model", "whisper-1"), ("stream", "true")])
        self.assertTrue(resp.headers["content-type"].startswith("text/event-stream"))
        events = [json.loads(line[6:]) for line in resp.text.split("\n\n") if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events], ["transcript.text.delta", "transcript.text.done"])
        self.assertEqual(events[1]["text"], "quiet words")
        self.assertNotIn(b'name="stream"', seen[0].content)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
    def test_audio_cpp_engines_receive_16khz_mono_wav_whatever_the_upload(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(200, json={"text": "ok"}))
        resp = self._post([("model", "stt-fast")], audio=_wav_bytes(44100, 2, 0.6))
        self.assertEqual(resp.status_code, 200, resp.text)
        payload = seen[0].content
        riff = payload[payload.index(b"RIFF"):]
        channels, rate = struct.unpack("<HI", riff[22:28])
        self.assertEqual((channels, rate), (1, 16000))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
    def test_a_streamed_request_also_gets_the_converted_wav(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(200, stream=_Chunks([b'data: {"type":"transcript.text.done","text":"x"}\n\n'])))
        resp = self._post([("model", "stt-fast"), ("stream", "true")], audio=_wav_bytes(48000, 2, 0.4))
        self.assertEqual(resp.status_code, 200)
        riff = seen[0].content[seen[0].content.index(b"RIFF"):]
        self.assertEqual(struct.unpack("<HI", riff[22:28]), (1, 16000))

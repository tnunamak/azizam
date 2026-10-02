"""STT tiers: service choice by model, on-demand start, stream=true, WAV conversion."""

from __future__ import annotations

import asyncio
import json
import struct
import unittest
import unittest.mock
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
        # Parakeet on GPU0 (8017) leads stt and stt-quality; the CPU Parakeet (5092) is the engine id `parakeet`.
        cases = {"whisper-1": 8017, "stt": 8017, "stt-quality": 8017, "parakeet-tdt-0.6b": 8017, "gpt-4o-mini-transcribe": 8012,
                 "canary": 8013, "parakeet-gpu": 8017, "parakeet": 5092}
        seen = self._json_upstream()
        for model, port in cases.items():
            with self.subTest(model=model):
                resp = self._post([("model", model)])
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual(seen[-1].url.port, port)

    def test_unknown_model_uses_the_default_service(self) -> None:
        seen = self._json_upstream()
        self.assertEqual(self._post([("model", "some-other-model")]).status_code, 200)
        self.assertEqual(seen[0].url.port, 5092)

    def test_gpu_parakeet_gets_its_own_model_id_and_chunk_limits(self) -> None:
        seen = self._json_upstream()
        self.assertEqual(self._post([("model", "whisper-1")]).status_code, 200)
        self.assertIn(b"\r\n\r\nparakeet-tdt-0.6b-v3\r\n", seen[0].content)
        self.assertEqual(seen[0].url.port, 8017)

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

    def test_always_on_service_with_external_lifecycle_is_not_probed(self) -> None:
        self._json_upstream()
        self.assertEqual(self._post([("model", "parakeet")]).status_code, 200)
        self.health.assert_not_awaited()

    def test_engine_by_id_that_will_not_start_is_a_503(self) -> None:
        self.health.return_value = False
        self.ensure.return_value = False
        for model in ("nemotron-streaming", "parakeet-gpu"):
            with self.subTest(model=model):
                resp = self._post([("model", model)])
                self.assertEqual(resp.status_code, 503)
                self.assertEqual(resp.json()["error"]["code"], "backend_unavailable")

    def test_a_tier_falls_back_to_the_next_engine_when_the_first_will_not_start(self) -> None:
        self.health.return_value = False
        self.ensure.return_value = False        # GPU0 is full: no on-demand unit comes up
        seen = self._json_upstream()
        for model, first_port in (("whisper-1", 8017), ("stt-fast", 8012)):
            with self.subTest(model=model):
                seen.clear()
                resp = self._post([("model", model)])
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual([r.url.port for r in seen], [5092])  # the always-on CPU Parakeet answered
                # ... with the CPU engine's own model id, not the failed engine's.
                self.assertIn(b"\r\n\r\nparakeet-tdt-0.6b-v3\r\n", seen[0].content)
                self.ensure.assert_any_await({8017: "stt-parakeet-gpu", 8012: "stt-nemotron"}[first_port], None)

    def test_a_tier_with_no_working_engine_is_a_503(self) -> None:
        self.health.return_value = False
        self.ensure.return_value = False
        self.cfg["backends"]["stt"]["lifecycle"] = {"driver": "systemd", "driver_config": {"unit": "stt.service", "scope": "user"}}
        resp = self._post([("model", "whisper-1")])
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (503, "backend_unavailable"))

    def test_the_fallback_is_listed_in_the_alias_resolution(self) -> None:
        body = self.client.get("/gateway/v1/aliases/resolve", params={"operation": "transcriptions", "model": "stt"}).json()
        self.assertEqual((body["engine"], body["fallback_engines"]), ("parakeet-gpu", ["parakeet"]))

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


class SttUnreadableUploadTests(SttCase):
    """A file the decoder cannot read is the caller's mistake: 400 invalid_request_error, never 5xx."""

    def _assert_unreadable_audio_is_a_400(self, resp) -> None:
        self.assertEqual(resp.status_code, 400, resp.text)
        error = resp.json()["error"]
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["param"], "file")
        self.assertIn("corrupted or unsupported", error["message"])

    def test_a_decoder_refusal_is_a_400_not_a_502(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(200, json={"text": "never"}))
        refusal = stt_chunking_failure()
        with patch.object(proxy.stt_chunking, "run_media_command", side_effect=refusal):
            resp = self._post([("model", "stt-fast")], audio=b"this is not audio", filename="notes.txt")
        self._assert_unreadable_audio_is_a_400(resp)
        self.assertEqual(seen, [], "the engine must never see an upload ffmpeg could not read")
        self.assertNotIn("x-vivid-fallback", resp.headers)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
    def test_a_text_file_sent_as_whisper_1_audio_is_a_400(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(200, json={"text": "never"}))
        for model in ("whisper-1", "stt-fast"):
            with self.subTest(model=model):
                resp = self._post([("model", model)], audio=b"just some text, not audio\n" * 40, filename="notes.txt")
                self._assert_unreadable_audio_is_a_400(resp)
        self.assertEqual(seen, [])


def stt_chunking_failure() -> Exception:
    from gateway import stt_chunking

    return stt_chunking.SttMediaCommandFailed("STT media command failed")


class SttRuntimeFallbackTests(SttCase):
    """A tier falls back when the first engine fails while serving, not only when it will not start."""

    def _engines(self, gpu_response, cpu_response=None):
        seen = self.use_upstream(lambda r: gpu_response(r) if r.url.port == 8017 else (cpu_response or (lambda _r: httpx.Response(200, json={"text": "cpu"})))(r))
        return seen

    def test_gpu_allocation_failure_falls_back_to_the_cpu_engine(self) -> None:
        msg = {"error": {"message": "Failed to allocate Parakeet TDT encoder graph tensors", "type": "server_error"}}
        seen = self._engines(lambda r: httpx.Response(500, json=msg))
        resp = self._post([("model", "whisper-1")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu"), resp.text)
        self.assertEqual([r.url.port for r in seen], [8017, 5092])
        self.assertEqual(resp.headers["x-vivid-fallback"], "parakeet-gpu->parakeet (gpu_oom)")
        # The CPU engine gets the whole upload again, with its own model id.
        self.assertIn(b"\r\n\r\nparakeet-tdt-0.6b-v3\r\n", seen[1].content)
        self.assertIn(b'name="file"', seen[1].content)

    def test_the_replayed_upload_is_byte_identical(self) -> None:
        seen = self._engines(lambda r: httpx.Response(500, json={"error": {"message": "CUDA out of memory"}}))
        audio = _wav_bytes(16000, 1, 1.5)
        resp = self._post([("model", "stt")], audio=audio, file_first=True)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual([r.url.port for r in seen], [8017, 5092])
        self.assertIn(audio, seen[1].content)

    def test_the_serving_receipt_records_the_fallback(self) -> None:
        self._engines(lambda r: httpx.Response(503, json={"error": {"message": "busy"}}))
        proxy.receipt_store.begin.reset_mock()
        resp = self._post([("model", "whisper-1")])
        self.assertEqual(resp.status_code, 200)
        receipts = [call.args[0] for call in proxy.receipt_store.begin.call_args_list]
        self.assertEqual([r.backend for r in receipts], ["stt-parakeet-gpu", "stt"])
        self.assertNotIn("parakeet-gpu->parakeet (busy)", receipts[0].fallback_reasons)
        self.assertIn("parakeet-gpu->parakeet (busy)", receipts[1].fallback_reasons)

    def test_connection_refused_falls_back(self) -> None:
        def refuse(request):
            raise httpx.ConnectError("refused")
        seen = self._engines(refuse)
        resp = self._post([("model", "stt-quality")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu"), resp.text)
        self.assertEqual(resp.headers["x-vivid-fallback"], "parakeet-gpu->parakeet (connection_refused)")

    def test_a_unit_that_starts_but_answers_503_falls_back(self) -> None:
        # The live case: systemd starts the unit, /health answers, the model load then fails with 503.
        self.health.return_value = False
        self.ensure.return_value = True
        self._engines(lambda r: httpx.Response(503, json={"error": {"message": "loading"}}))
        resp = self._post([("model", "whisper-1")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu"))

    def test_start_failure_is_named_in_the_header(self) -> None:
        self.health.return_value = False
        self.ensure.return_value = False
        self._json_upstream()
        resp = self._post([("model", "whisper-1")])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["x-vivid-fallback"], "parakeet-gpu->parakeet (start_failed)")

    def test_a_client_error_does_not_fall_back(self) -> None:
        answer = {}
        seen = self._engines(lambda r: httpx.Response(answer["status"], json=answer["body"]))
        for status, body in ((400, {"error": {"message": "unsupported audio"}}), (422, {"error": {"message": "bad file"}}),
                             (500, {"error": {"message": "could not decode audio"}})):
            with self.subTest(status=status):
                seen.clear()
                answer.update(status=status, body=body)
                resp = self._post([("model", "whisper-1")])
                self.assertEqual(resp.status_code, status)
                self.assertNotIn("x-vivid-fallback", resp.headers)
                self.assertEqual([r.url.port for r in seen], [8017])

    def test_an_engine_id_by_name_never_falls_back(self) -> None:
        seen = self._engines(lambda r: httpx.Response(500, json={"error": {"message": "Failed to allocate tensors"}}))
        resp = self._post([("model", "parakeet-gpu")])
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("x-vivid-fallback", resp.headers)
        self.assertEqual([r.url.port for r in seen], [8017])

    def test_every_engine_failing_returns_the_last_error_with_the_header(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(503, json={"error": {"message": "busy"}}))
        resp = self._post([("model", "stt-quality")])
        self.assertEqual(resp.status_code, 503)
        self.assertEqual([r.url.port for r in seen], [8017, 5092, 8013])
        self.assertEqual(resp.headers["x-vivid-fallback"],
                         "parakeet-gpu->parakeet (busy), parakeet->canary (busy)")


class SttMidFileFallbackTests(SttCase):
    """An engine that dies part-way through a chunked long file hands the whole file to the tier's next engine."""

    def setUp(self) -> None:
        super().setUp()
        gpu = self.cfg["backends"]["stt-parakeet-gpu"]
        gpu["chunking"] = {
            "enabled": True, "spool_dir": self.tmp.name, "threshold_seconds": 30, "target_seconds": 20,
            "max_duration_seconds": 600, "max_segments": 8, "min_free_bytes": 1,
        }
        self.durations = [75.0]  # the GPU engine's probe sees a long file; the CPU engine's sees a short one
        from gateway import stt_chunking

        async def probe(*_args, **_kwargs):
            return stt_chunking.ProbedAudio(self.durations.pop(0) if self.durations else 10.0, 16000, 1)

        async def extract(_source, destination, **_kwargs):
            Path(destination).write_bytes(b"RIFFchunk")

        for patcher in (
            patch.object(stt_chunking, "probe_audio", side_effect=probe),
            patch.object(stt_chunking, "detect_silences", new=unittest.mock.AsyncMock(return_value=[(19, 21), (39, 41), (59, 61)])),
            patch.object(stt_chunking, "extract_segment", side_effect=extract),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _gpu_dies_on_second_chunk(self, failure):
        gpu_calls = []

        def handler(request):
            if request.url.port != 8017:
                return httpx.Response(200, json={"text": "cpu whole file"})
            gpu_calls.append(1)
            if len(gpu_calls) == 1:
                return httpx.Response(200, json={"text": "first chunk"})
            return failure(request)

        return self.use_upstream(handler)

    def test_a_dropped_connection_on_a_later_chunk_restarts_the_file_on_the_next_engine(self) -> None:
        def drop(request):
            raise httpx.ReadError("connection reset by peer", request=request)

        seen = self._gpu_dies_on_second_chunk(drop)
        proxy.receipt_store.begin.reset_mock()
        resp = self._post([("model", "whisper-1")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu whole file"), resp.text)
        self.assertEqual([r.url.port for r in seen], [8017, 8017, 5092])
        self.assertEqual(resp.headers["x-vivid-fallback"], "parakeet-gpu->parakeet (connection_lost@chunk2)")
        receipts = [call.args[0] for call in proxy.receipt_store.begin.call_args_list]
        self.assertIn("parakeet-gpu->parakeet (connection_lost@chunk2)", receipts[-1].fallback_reasons)

    def test_an_out_of_memory_answer_on_a_later_chunk_falls_back_and_names_the_chunk(self) -> None:
        seen = self._gpu_dies_on_second_chunk(
            lambda r: httpx.Response(500, json={"error": {"message": "Failed to allocate Parakeet TDT encoder graph tensors"}})
        )
        resp = self._post([("model", "whisper-1")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu whole file"), resp.text)
        self.assertEqual([r.url.port for r in seen], [8017, 8017, 5092])
        self.assertEqual(resp.headers["x-vivid-fallback"], "parakeet-gpu->parakeet (gpu_oom@chunk2)")

    def test_a_refused_connection_on_a_later_chunk_falls_back(self) -> None:
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        self._gpu_dies_on_second_chunk(refuse)
        resp = self._post([("model", "whisper-1")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu whole file"), resp.text)
        self.assertEqual(resp.headers["x-vivid-fallback"], "parakeet-gpu->parakeet (connection_refused@chunk2)")

    def test_a_client_error_on_a_later_chunk_does_not_fall_back(self) -> None:
        seen = self._gpu_dies_on_second_chunk(lambda r: httpx.Response(400, json={"error": {"message": "bad audio"}}))
        resp = self._post([("model", "whisper-1")])
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("x-vivid-fallback", resp.headers)
        self.assertEqual([r.url.port for r in seen], [8017, 8017])

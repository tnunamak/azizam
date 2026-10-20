"""STT tiers: service choice by model, on-demand start, stream=true, WAV conversion."""

from __future__ import annotations

import asyncio
import json
import struct
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.requests import Request

import proxy
from tests.support.access import access_context, plan_for
from gateway import speech_stream, stt_chunking

from tests.support.stt_pcm import planning_pcm
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

    def test_unknown_model_is_not_silently_routed_to_the_default_service(self) -> None:
        seen = self._json_upstream()
        response = self._post([("model", "some-other-model")])
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "model_not_found")
        self.assertEqual(response.json()["error"]["param"], "model")
        self.assertEqual(seen, [])

    def test_gpu_parakeet_gets_its_own_model_id_and_chunk_limits(self) -> None:
        seen = self._json_upstream()
        self.assertEqual(self._post([("model", "whisper-1")]).status_code, 200)
        self.assertIn(b"\r\n\r\nparakeet-tdt-0.6b-v3\r\n", seen[0].content)
        self.assertEqual(seen[0].url.port, 8017)

    def test_model_after_a_small_file_still_selects_the_service(self) -> None:
        seen = self._json_upstream()
        self.assertEqual(self._post([("model", "stt-fast")], file_first=True).status_code, 200)
        self.assertEqual(seen[0].url.port, 8012)

    def _post_file_first_past_peek(self, fields, *, audio=None, operation="transcriptions", auth_context=None):
        body, ctype = _multipart(
            fields,
            audio if audio is not None else _wav_bytes(16000, 1, 1.5),
            file_first=True,
        )
        messages = [
            {"type": "http.request", "body": body[index : index + 1000], "more_body": index + 1000 < len(body)}
            for index in range(0, len(body), 1000)
        ]

        async def receive():
            if messages:
                return messages.pop(0)
            await asyncio.Future()

        request = Request(
            {"type": "http", "method": "POST", "path": f"/v1/audio/{operation}", "headers": [(b"content-type", ctype.encode())]},
            receive,
        )

        async def dispatch_and_drain():
            response = await proxy._dispatch_selected_stt_route(request, auth_context or access_context(), operation=operation)
            captured = b""
            if getattr(response, "body_iterator", None) is not None:
                captured = b"".join([chunk async for chunk in response.body_iterator])
            else:
                captured = response.body
            return response, captured

        with patch.object(speech_stream, "PEEK_LIMIT_BYTES", 2048):
            return asyncio.run(dispatch_and_drain())

    def test_file_first_model_hidden_by_upload_dispatches_to_its_service(self) -> None:
        seen = self._json_upstream()
        response, body = self._post_file_first_past_peek([("model", "stt-fast")])
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual([request.url.port for request in seen], [8012])
        self.assertIn(b"\r\n\r\nnemotron-3.5-asr-streaming-0.6b\r\n", seen[0].content)

    def test_file_first_model_for_default_service_keeps_default_dispatch(self) -> None:
        seen = self._json_upstream()
        response, body = self._post_file_first_past_peek([("model", "parakeet")])
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual([request.url.port for request in seen], [5092])

    def test_file_first_target_policy_denial_applies_to_resolved_service(self) -> None:
        seen = self._json_upstream()

        class Decision:
            code = "not_in_access_plan"
            side = "person"
            sources: tuple[str, ...] = ()

            def __init__(self, denied):
                self.denied = denied
                self.reasons = ["target policy denied"] if denied else []

        evaluated = []

        def evaluate(receipt, _auth, *, operation, model=None, requested_model=None):
            evaluated.append((receipt.backend, operation))
            return Decision(receipt.backend == "stt-nemotron")

        with patch.object(proxy, "_apply_selected_stt_policy", side_effect=evaluate):
            response, _ = self._post_file_first_past_peek([("model", "stt-fast")])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(evaluated, [("stt-nemotron", "transcriptions")])
        self.assertEqual(seen, [])

    def test_file_first_handoff_releases_default_lease_before_target_admission(self) -> None:
        gate = proxy.stt_chunking.GLOBAL_STT_RESOURCE_GATE
        acquire = gate.acquire
        acquired = []
        release_count = 0
        initial_active = gate._active

        def count_release(lease):
            original_release = lease.release

            def release():
                nonlocal release_count
                if not lease._released:
                    release_count += 1
                original_release()

            lease.release = release
            return lease

        def tracked_acquire(*, request_id, config):
            if config.service_id == "stt-nemotron":
                self.assertTrue(acquired and acquired[0]._released)
            lease = count_release(acquire(request_id=request_id, config=config))
            acquired.append(lease)
            return lease

        seen = self._json_upstream()
        parse = proxy.stt_chunking.parse_bounded_multipart
        with patch.object(gate, "acquire", side_effect=tracked_acquire), patch.object(
            proxy.stt_chunking, "parse_bounded_multipart", wraps=parse
        ) as parse_multipart:
            response, _ = self._post_file_first_past_peek([("model", "stt-fast")])
        self.assertEqual(response.status_code, 200, response.body)
        parse_multipart.assert_awaited_once()
        self.assertEqual(len(acquired), 2)
        self.assertTrue(all(lease._released for lease in acquired))
        self.assertEqual(release_count, 2)
        self.assertEqual(gate._active, initial_active)
        self.assertEqual([request.url.port for request in seen], [8012])
        self.assertEqual(proxy.receipt_store.finalize.call_args.args[0].backend, "stt-nemotron")
        self.assertEqual(list(Path(self.tmp.name).glob("*")), [])

    def test_file_first_streaming_model_handoff_uses_target_service(self) -> None:
        events = [b'data: {"type":"transcript.text.done","text":"hello"}\n\n', b"data: [DONE]\n\n"]
        seen = self.use_upstream(lambda _request: httpx.Response(200, stream=_Chunks(events), headers={"content-type": "text/event-stream"}))
        response, stream_body = self._post_file_first_past_peek([("model", "stt-fast"), ("stream", "true")])
        self.assertEqual(response.status_code, 200, stream_body)
        self.assertEqual([request.url.port for request in seen], [8012])
        self.assertEqual(stream_body, b"".join(events))
        self.assertEqual(list(Path(self.tmp.name).glob("*")), [])

    def test_file_first_translation_model_dispatches_to_target_service(self) -> None:
        self.cfg["backends"]["stt"]["capabilities"].append("translations")
        self.cfg["backends"]["stt-nemotron"]["capabilities"].append("translations")
        seen = self._json_upstream()
        response, _ = self._post_file_first_past_peek([("model", "stt-fast")], operation="translations")
        self.assertEqual(response.status_code, 200, response.body)
        self.assertEqual([request.url.port for request in seen], [8012])
        self.assertEqual(seen[0].url.path, "/v1/audio/translations")

    def test_file_first_handoff_enforces_target_field_byte_limit(self) -> None:
        seen = self._json_upstream()
        self.cfg["backends"]["stt-nemotron"]["chunking"]["max_field_bytes"] = 9
        response, _ = self._post_file_first_past_peek([("model", "stt-fast"), ("prompt", "é" * 5)])
        self.assertEqual(response.status_code, 413)
        self.assertEqual(json.loads(response.body)["error"]["code"], "stt_upload_limit")
        self.assertEqual(seen, [])

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

    def test_undeployed_engine_by_name_is_a_coded_503(self) -> None:
        del self.cfg["backends"]["stt-canary"]
        resp = self._post([("model", "canary")])
        self.assertEqual(resp.status_code, 503)
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

    def test_connection_lost_before_the_first_event_falls_back_to_the_next_engine(self) -> None:
        failure: list[Exception] = []

        def handler(request):
            if request.url.port == 8012:
                raise failure[0]
            return httpx.Response(200, json={"text": "cpu"})

        seen = self.use_upstream(handler)
        for error in (httpx.RemoteProtocolError("server disconnected"), httpx.ReadError("reset")):
            with self.subTest(error=type(error).__name__):
                seen.clear()
                failure[:] = [error]
                resp = self._post([("model", "stt-fast"), ("stream", "true")])
                self.assertEqual(resp.status_code, 200, resp.text)
                self.assertEqual([r.url.port for r in seen], [8012, 5092])
                self.assertEqual(resp.headers["x-azizam-fallback"], "nemotron-streaming->parakeet (connection_lost)")
                self.assertIn('"delta":"cpu"', resp.text)

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
        self.assertNotIn("x-azizam-fallback", resp.headers)

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
        self.assertEqual(resp.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (gpu_oom)")
        self.assertEqual(resp.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (gpu_oom)")
        # The CPU engine gets the whole upload again, with its own model id.
        self.assertIn(b"\r\n\r\nparakeet-tdt-0.6b-v3\r\n", seen[1].content)
        self.assertIn(b'name="file"', seen[1].content)

    def test_gpu_allocation_failure_falls_back_when_the_model_field_follows_a_large_file(self) -> None:
        # A long recording is uploaded file first; its model field sits past the peek budget.
        msg = {"error": {"message": "Failed to allocate Parakeet TDT encoder graph tensors", "type": "server_error"}}
        seen = self._engines(lambda r: httpx.Response(500, json=msg))
        proxy.receipt_store.begin.reset_mock()
        response, body = SttServiceSelectionTests._post_file_first_past_peek(self, [("model", "whisper-1")])
        self.assertEqual((response.status_code, json.loads(body)["text"]), (200, "cpu"), body)
        self.assertEqual([r.url.port for r in seen], [8017, 5092])
        self.assertEqual(response.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (gpu_oom)")
        receipts = [call.args[0] for call in proxy.receipt_store.begin.call_args_list]
        self.assertIn("parakeet-gpu->parakeet (gpu_oom)", receipts[-1].fallback_reasons)

    def test_file_first_fallback_keeps_the_callers_alias_for_policy(self) -> None:
        seen = self._engines(lambda _r: httpx.Response(500, json={"error": {"message": "Failed to allocate tensors"}}))
        context = access_context(plan_for(models=["whisper-1"]))
        response, body = SttServiceSelectionTests._post_file_first_past_peek(
            self, [("model", "whisper-1")], auth_context=context,
        )
        self.assertEqual((response.status_code, json.loads(body).get("text")), (200, "cpu"), body)
        self.assertEqual([r.url.port for r in seen], [8017, 5092])

    def test_skipped_primary_engine_is_reported_without_runtime_retry(self) -> None:
        self.cfg["backends"]["stt-parakeet-gpu"]["enabled"] = False
        seen = self._json_upstream("cpu")
        proxy.receipt_store.begin.reset_mock()
        resp = self._post([("model", "whisper-1")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu"), resp.text)
        self.assertEqual([r.url.port for r in seen], [5092])
        self.assertEqual(resp.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (not_deployed)")
        receipts = [call.args[0] for call in proxy.receipt_store.begin.call_args_list]
        self.assertIn("parakeet-gpu->parakeet (not_deployed)", receipts[-1].fallback_reasons)

    def test_the_replayed_upload_is_byte_identical(self) -> None:
        seen = self._engines(lambda r: httpx.Response(500, json={"error": {"message": "CUDA out of memory"}}))
        audio = _wav_bytes(16000, 1, 1.5)
        resp = self._post([("model", "stt")], audio=audio, file_first=True)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual([r.url.port for r in seen], [8017, 5092])
        self.assertIn(audio, seen[1].content)

    def test_the_replay_copy_is_reserved_for_the_whole_dispatch_and_bounded(self) -> None:
        gate = stt_chunking.GLOBAL_STT_RESOURCE_GATE
        service = proxy._stt_execution_snapshot("transcriptions", "stt-parakeet-gpu")
        body, ctype = _multipart([("model", "whisper-1")], _wav_bytes(16000, 1))
        attempts: list[tuple[int, int]] = []
        written: list[int] = []

        def observe(request):
            attempts.append((gate._reserved_bytes, gate._active))
            written.append(sum(replay.written_bytes for replay in gate._replays))

        def busy(request):
            observe(request)
            return httpx.Response(503, json={"error": {"message": "busy"}})

        def cpu(request):
            observe(request)
            return httpx.Response(200, json={"text": "cpu"})

        with self.subTest("held beside each attempt's lease, one concurrency slot"):
            self._engines(busy, cpu)
            resp = self._post([("model", "whisper-1")])
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(len(attempts), 2)  # the final CPU attempt holds the replay copy as well
            for reserved, active in attempts:
                self.assertGreaterEqual(reserved, service.max_input_bytes + service.max_decoded_bytes + len(body))
                self.assertEqual(active, 1)
            self.assertTrue(all(count >= len(body) for count in written), written)
            self.assertEqual((gate._reserved_bytes, gate._active), (0, 0))
            self.assertEqual(len(gate._replays), 0)

        with self.subTest("a disk that only fits the attempt refuses the replay copy"):
            seen = self._engines(busy)
            fits_attempt_only = service.max_input_bytes + service.max_decoded_bytes + service.min_free_bytes + 1024 * 1024
            with patch.object(stt_chunking, "_disk_free_bytes", return_value=fits_attempt_only):
                resp = self._post([("model", "whisper-1")])
            self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (413, "stt_resource_limit"))
            self.assertEqual(seen, [])
            self.assertEqual((gate._reserved_bytes, gate._active), (0, 0))

        with self.subTest("a refused replay copy still leaves one terminal receipt"):
            # The refusal happens before any attempt exists, so the dispatcher records it.
            seen = self._engines(busy)
            proxy.receipt_store.begin.reset_mock()
            proxy.receipt_store.finalize.reset_mock()
            with patch.object(stt_chunking, "_disk_free_bytes", return_value=1):
                resp = self._post([("model", "whisper-1")])
            self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (413, "stt_resource_limit"))
            self.assertEqual(seen, [])
            self.assertEqual((proxy.receipt_store.begin.call_count, proxy.receipt_store.finalize.call_count), (1, 1))
            self.assertEqual(proxy.receipt_store.finalize.call_args.args[0].status, "failed")

        with self.subTest("a replay copy that fails to close still releases its reservation"):
            self._engines(lambda r: httpx.Response(200, json={"text": "ok"}))
            real_close = proxy._ReplayableBody.close

            def close_then_fail(replay_body):
                real_close(replay_body)
                raise OSError("replay close failed")

            with patch.object(proxy._ReplayableBody, "close", close_then_fail):
                with self.assertRaisesRegex(OSError, "replay close failed"):
                    self._post([("model", "whisper-1")])
            self.assertEqual((gate._reserved_bytes, gate._active), (0, 0))
            self.assertEqual(len(gate._replays), 0)

        with self.subTest("replay bytes already on disk are not subtracted twice, and nothing else is credited"):
            replay_limit = service.replay_limit_bytes
            attempt = service.max_input_bytes + service.max_decoded_bytes
            gate_free = [replay_limit + attempt + service.min_free_bytes]  # the disk fits both worst cases exactly
            materialized = replay_limit // 2

            def live_free(_path):
                return gate_free[0]

            with patch.object(stt_chunking, "_disk_free_bytes", side_effect=live_free):
                replay = gate.reserve_replay(request_id="r-replay", config=service)
                replay.record_written(materialized)
                gate_free[0] -= materialized  # the filesystem now reports the file's bytes as used
                lease = gate.acquire(request_id="r-attempt", config=service)
                self.assertEqual(gate._reserved_bytes, replay_limit + attempt)
                with self.assertRaises(stt_chunking.SttResourceLimitExceeded):
                    gate.acquire(request_id="r-concurrent", config=service)  # the unwritten claims still hold
                lease.release()
                self.assertEqual(gate._reserved_bytes, replay_limit)
                with patch.object(stt_chunking, "_device_of", return_value=-1):
                    with self.assertRaises(stt_chunking.SttResourceLimitExceeded):
                        gate.acquire(request_id="r-elsewhere", config=service)  # a replay on another filesystem earns no credit
                replay.release()
                replay.release()
            self.assertEqual((gate._reserved_bytes, gate._active, len(gate._replays)), (0, 0, 0))

        with self.subTest("a body past the reserved bound is refused"):
            self.cfg["backends"]["stt-parakeet-gpu"]["chunking"].update(
                max_input_bytes=200_000, max_files=1, max_fields=2, max_field_bytes=1024)
            seen = self._engines(busy)
            resp = self.client.post("/v1/audio/transcriptions", content=b"x" * 300_000 + body, headers={"content-type": ctype})
            self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (413, "stt_upload_limit"))
            self.assertEqual((gate._reserved_bytes, gate._active), (0, 0))

    def test_fallback_attempts_share_one_request_deadline(self) -> None:
        class Clock:
            offset = 0.0

            def monotonic(self) -> float:
                return time.monotonic() + self.offset

            def __getattr__(self, name):
                return getattr(time, name)

        clock = Clock()
        deadlines: list[float] = []
        real_await = stt_chunking.await_with_cancellation

        def recording_await(awaitable, **kwargs):
            if kwargs.get("deadline_at") is not None:
                deadlines.append(kwargs["deadline_at"])
            return real_await(awaitable, **kwargs)

        spent: list[float] = []

        real_reason = proxy._speech_fallback_reason

        def reason_after_the_time_passes(response):
            clock.offset += spent[0]  # the first engine's attempt took this long
            return real_reason(response)

        seen = self._engines(lambda request: httpx.Response(503, json={"error": {"message": "busy"}}))
        budget = {sid: proxy._stt_execution_snapshot("transcriptions", sid).deadline_seconds for sid in ("stt-parakeet-gpu", "stt")}
        self.assertLess(budget["stt-parakeet-gpu"], budget["stt"])

        def peeked():
            resp = self._post([("model", "whisper-1")])
            return resp, resp.json()

        def file_first():
            # The model sits past the peek budget: the upload is admitted under the larger default
            # service, and parsing then finds the model's shorter budget.
            resp, body = SttServiceSelectionTests._post_file_first_past_peek(self, [("model", "whisper-1")])
            return resp, json.loads(body)

        for journey, post in (("model first", peeked), ("file first", file_first)):
            for elapsed, served in ((250.0, True), (361.0, False)):
                with self.subTest(journey=journey, first_engine_spends=elapsed):
                    clock.offset = 0.0
                    deadlines.clear()
                    seen.clear()
                    spent[:] = [elapsed]
                    proxy.receipt_store.begin.reset_mock()
                    proxy.receipt_store.finalize.reset_mock()
                    with patch.object(proxy, "time", clock), patch.object(stt_chunking, "time", clock), \
                            patch.object(proxy, "_speech_fallback_reason", reason_after_the_time_passes), \
                            patch.object(stt_chunking, "await_with_cancellation", recording_await):
                        resp, payload = post()
                    # Each service's deadline is the request's one start plus that service's own budget,
                    # and once a shorter budget is known no later attempt may extend it.
                    start = min(deadlines) - budget["stt-parakeet-gpu"]
                    self.assertLessEqual(set(deadlines), {start + budget["stt-parakeet-gpu"], start + budget["stt"]})
                    self.assertEqual(deadlines, sorted(deadlines, reverse=True), deadlines)
                    if served:
                        self.assertEqual((resp.status_code, payload["text"]), (200, "cpu"), payload)
                        self.assertEqual([r.url.port for r in seen], [8017, 5092])
                        self.assertEqual(proxy.receipt_store.begin.call_count, 2)
                    else:
                        self.assertEqual((resp.status_code, payload["error"]["code"]), (504, "stt_request_deadline"))
                        self.assertEqual([r.url.port for r in seen], [8017])
                        self.assertEqual(resp.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (busy)")
                        # The expiry ends the request before the fallback attempt owns a receipt; it is still recorded.
                        self.assertEqual(proxy.receipt_store.begin.call_count, 2)
                        self.assertEqual(proxy.receipt_store.finalize.call_args.args[0].status, "timed_out")
                    self.assertEqual(proxy.receipt_store.finalize.call_count, proxy.receipt_store.begin.call_count)

    def test_the_serving_receipt_records_the_fallback(self) -> None:
        self._engines(lambda r: httpx.Response(503, json={"error": {"message": "busy"}}))
        proxy.receipt_store.begin.reset_mock()
        resp = self._post([("model", "whisper-1")])
        self.assertEqual(resp.status_code, 200)
        receipts = [call.args[0] for call in proxy.receipt_store.begin.call_args_list]
        self.assertEqual([r.backend for r in receipts], ["stt-parakeet-gpu", "stt"])
        self.assertEqual([r.metadata["stt"]["attempts_started"] for r in receipts], [1, 1])
        self.assertNotEqual(receipts[0].request_id, receipts[1].request_id)
        self.assertNotIn("parakeet-gpu->parakeet (busy)", receipts[0].fallback_reasons)
        self.assertIn("parakeet-gpu->parakeet (busy)", receipts[1].fallback_reasons)

    def test_connection_refused_falls_back(self) -> None:
        def refuse(request):
            raise httpx.ConnectError("refused")
        seen = self._engines(refuse)
        resp = self._post([("model", "stt-quality")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu"), resp.text)
        self.assertEqual(resp.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (connection_refused)")

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
        self.assertEqual(resp.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (start_failed)")

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
                self.assertNotIn("x-azizam-fallback", resp.headers)
                self.assertEqual([r.url.port for r in seen], [8017])

    def test_an_engine_id_by_name_never_falls_back(self) -> None:
        seen = self._engines(lambda r: httpx.Response(500, json={"error": {"message": "Failed to allocate tensors"}}))
        resp = self._post([("model", "parakeet-gpu")])
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("x-azizam-fallback", resp.headers)
        self.assertEqual([r.url.port for r in seen], [8017])

    def test_every_engine_failing_returns_the_last_error_with_the_header(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(503, json={"error": {"message": "busy"}}))
        resp = self._post([("model", "stt-quality")])
        self.assertEqual(resp.status_code, 503)
        self.assertEqual([r.url.port for r in seen], [8017, 5092, 8013])
        self.assertEqual(resp.headers["x-azizam-fallback"],
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
            planning_pcm(75, [20.0, 40.0, 60.0]),
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
        self.assertEqual(resp.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (connection_lost@chunk2)")
        receipts = [call.args[0] for call in proxy.receipt_store.begin.call_args_list]
        self.assertIn("parakeet-gpu->parakeet (connection_lost@chunk2)", receipts[-1].fallback_reasons)

    def test_mid_file_fallback_replays_the_exact_original_file_and_fields(self) -> None:
        from email import policy as email_policy
        from email.parser import BytesParser

        self.cfg["model_aliases"]["transcriptions"]["engines"][
            "parakeet-gpu"
        ].pop("upload_format", None)

        original = _wav_bytes(16000, 1, 1.5)

        def drop(request):
            raise httpx.ReadError("connection reset by peer", request=request)

        seen = self._gpu_dies_on_second_chunk(drop)
        response = self._post(
            [
                ("model", "whisper-1"),
                ("language", "en"),
                ("prompt", "preserve this exact prompt"),
                ("temperature", "0"),
                ("response_format", "json"),
            ],
            audio=original,
            file_first=True,
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"text": "cpu whole file"})
        self.assertEqual([request.url.port for request in seen], [8017, 8017, 5092])
        self.assertEqual(
            response.headers["x-azizam-fallback"],
            "parakeet-gpu->parakeet (connection_lost@chunk2)",
        )

        def parts(request):
            message = BytesParser(policy=email_policy.default).parsebytes(
                b"MIME-Version: 1.0\r\nContent-Type: "
                + request.headers["content-type"].encode("ascii")
                + b"\r\n\r\n"
                + request.content
            )
            self.assertTrue(message.is_multipart())
            return [
                (
                    part.get_param("name", header="content-disposition"),
                    part.get_filename(),
                    part.get_payload(decode=True),
                )
                for part in message.iter_parts()
            ]

        primary_files = [
            [body for name, filename, body in parts(request) if name == "file"]
            for request in seen[:2]
        ]
        self.assertEqual(primary_files, [[b"RIFFchunk"], [b"RIFFchunk"]])

        replay = parts(seen[2])
        replayed_files = [
            (filename, body) for name, filename, body in replay if name == "file"
        ]
        self.assertEqual(replayed_files, [("a.wav", original)])
        replayed_fields = [
            (name, body.decode("utf-8"))
            for name, filename, body in replay
            if filename is None
        ]
        self.assertCountEqual(
            replayed_fields,
            [
                ("model", "parakeet-tdt-0.6b-v3"),
                ("language", "en"),
                ("prompt", "preserve this exact prompt"),
                ("temperature", "0"),
                ("response_format", "json"),
            ],
        )
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_an_out_of_memory_answer_on_a_later_chunk_falls_back_and_names_the_chunk(self) -> None:
        seen = self._gpu_dies_on_second_chunk(
            lambda r: httpx.Response(500, json={"error": {"message": "Failed to allocate Parakeet TDT encoder graph tensors"}})
        )
        resp = self._post([("model", "whisper-1")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu whole file"), resp.text)
        self.assertEqual([r.url.port for r in seen], [8017, 8017, 5092])
        self.assertEqual(resp.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (gpu_oom@chunk2)")

    def test_a_refused_connection_on_a_later_chunk_falls_back(self) -> None:
        def refuse(request):
            raise httpx.ConnectError("refused", request=request)

        self._gpu_dies_on_second_chunk(refuse)
        resp = self._post([("model", "whisper-1")])
        self.assertEqual((resp.status_code, resp.json()["text"]), (200, "cpu whole file"), resp.text)
        self.assertEqual(resp.headers["x-azizam-fallback"], "parakeet-gpu->parakeet (connection_refused@chunk2)")

    def test_a_client_error_on_a_later_chunk_does_not_fall_back(self) -> None:
        seen = self._gpu_dies_on_second_chunk(lambda r: httpx.Response(400, json={"error": {"message": "bad audio"}}))
        resp = self._post([("model", "whisper-1")])
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("x-azizam-fallback", resp.headers)
        self.assertEqual([r.url.port for r in seen], [8017, 8017])

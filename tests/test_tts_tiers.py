"""TTS tiers: on-demand engine start, PCM/SSE streaming, engine voices."""

from __future__ import annotations

import base64
import json
import struct
import unittest
from unittest.mock import patch

import httpx

import proxy
from gateway import speech_stream

from tests.speech_tier_helpers import HAVE_FFMPEG, RouteCase, _Chunks, _wav_bytes


class TtsLifecycleTests(RouteCase):
    def _speak(self, **extra):
        payload = {"model": "tts-fast", "voice": "shimmer", "input": "hello", "response_format": "wav", **extra}
        return self.client.post("/v1/audio/speech", json=payload)

    def test_down_engine_is_started_through_its_lifecycle_driver(self) -> None:
        self.health.return_value = False
        self.use_upstream(lambda r: httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"}))
        self.assertEqual(self._speak().status_code, 200)
        self.ensure.assert_awaited_once_with("tts-supertonic", None)

    def test_healthy_engine_is_not_restarted(self) -> None:
        self.use_upstream(lambda r: httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"}))
        self.assertEqual(self._speak().status_code, 200)
        self.ensure.assert_not_awaited()

    def test_engine_that_will_not_start_is_a_503_with_retry_after(self) -> None:
        self.health.return_value = False
        self.ensure.return_value = False
        resp = self._speak()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "backend_unavailable")
        self.assertEqual(resp.headers["retry-after"], "5")

    def test_always_on_service_is_never_probed(self) -> None:
        self.cfg["backends"]["tts-supertonic"].pop("lifecycle", None)  # an always-on (external) service
        self.use_upstream(lambda r: httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"}))
        resp = self.client.post("/v1/audio/speech", json={"model": "tts", "voice": "nova", "input": "hi", "response_format": "wav"})
        self.assertEqual(resp.status_code, 200)
        self.health.assert_not_awaited()
        self.ensure.assert_not_awaited()

    def test_unknown_model_is_not_silently_routed_to_default_engine(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(200, content=b"RIFFwav"))
        response = self.client.post(
            "/v1/audio/speech",
            json={"model": "tts-omnivoice", "voice": "alloy", "input": "hello"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "model_not_found")
        self.assertEqual(response.json()["error"]["param"], "model")
        self.assertEqual(seen, [])

    def test_documented_openai_speech_model_alias_still_routes(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(200, content=b"RIFFwav"))
        response = self.client.post(
            "/v1/audio/speech",
            json={"model": "tts-1", "voice": "alloy", "input": "hello", "response_format": "wav"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(seen[0].content)["model"], "supertonic")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
class TtsDefaultFormatTests(RouteCase):
    def test_buffered_default_and_empty_format_return_mp3(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(
            200, content=_wav_bytes(24000, 1), headers={"content-type": "audio/wav"},
        ))
        for model in ("omnivoice", "tts-fast"):
            for format_fields in ({}, {"response_format": ""}, {"response_format": None}):
                with self.subTest(model=model, format_fields=format_fields):
                    resp = self.client.post("/v1/audio/speech", json={
                        "model": model, "voice": "alloy", "input": "hello", **format_fields,
                    })
                    self.assertEqual(resp.status_code, 200, resp.text)
                    self.assertEqual(resp.headers["content-type"], "audio/mpeg")
                    self.assertTrue(resp.content.startswith(b"ID3"))
                    self.assertGreater(len(resp.content), 100)
                    self.assertEqual(json.loads(seen[-1].content)["response_format"], "wav")

    def test_explicit_wav_is_returned_unchanged(self) -> None:
        wav = _wav_bytes(24000, 1)
        self.use_upstream(lambda r: httpx.Response(200, content=wav, headers={"content-type": "audio/wav"}))
        resp = self.client.post("/v1/audio/speech", json={
            "model": "omnivoice", "voice": "alloy", "input": "hello", "response_format": "wav",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers["content-type"], "audio/wav")
        self.assertEqual(resp.content, wav)

    def test_streaming_default_returns_mp3_audio_and_sse_deltas(self) -> None:
        pcm = struct.pack("<h", 2500) * 4410
        seen = self.use_upstream(lambda r: httpx.Response(200, stream=_Chunks([pcm])))
        for stream_format in ("audio", "sse"):
            with self.subTest(stream_format=stream_format):
                resp = self.client.post("/v1/audio/speech", json={
                    "model": "tts-fast", "voice": "alloy", "input": "hello", "stream_format": stream_format,
                })
                self.assertEqual(resp.status_code, 200, resp.text)
                if stream_format == "sse":
                    self.assertEqual(resp.headers["content-type"], "text/event-stream; charset=utf-8")
                    events = [json.loads(line[6:]) for line in resp.text.split("\n\n") if line.startswith("data: ")]
                    self.assertEqual(events[-1]["type"], "speech.audio.done")
                    self.assertTrue(all(e["type"] == "speech.audio.delta" for e in events[:-1]))
                    audio = b"".join(base64.b64decode(e["audio"]) for e in events[:-1])
                else:
                    self.assertEqual(resp.headers["content-type"], "audio/mpeg")
                    audio = resp.content
                self.assertTrue(audio.startswith(b"ID3"))
                self.assertGreater(len(audio), 100)
                sent = json.loads(seen[-1].content)
                self.assertEqual((sent["response_format"], sent["stream_format"]), ("pcm", "audio"))

    def test_buffered_default_sse_carries_mp3(self) -> None:
        self.use_upstream(lambda r: httpx.Response(200, content=_wav_bytes(24000, 1), headers={"content-type": "audio/wav"}))
        resp = self.client.post("/v1/audio/speech", json={
            "model": "omnivoice", "voice": "alloy", "input": "hello", "stream_format": "sse",
        })
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.headers["content-type"], "text/event-stream; charset=utf-8")
        events = [json.loads(line[6:]) for line in resp.text.split("\n\n") if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events], ["speech.audio.delta", "speech.audio.done"])
        self.assertTrue(base64.b64decode(events[0]["audio"]).startswith(b"ID3"))


class TtsRuntimeFallbackTests(RouteCase):
    """The TTS tiers fall back by the same rules as STT."""

    def _speak(self, model="tts", **extra):
        payload = {"model": model, "voice": "nova", "input": "hello", "response_format": "wav", **extra}
        return self.client.post("/v1/audio/speech", json=payload)

    def _ports(self):
        ports = {name: self.cfg["backends"][name]["port"] for name in ("tts", "tts-supertonic")}
        return ports["tts-supertonic"], ports["tts"]  # first engine of the `tts` tier, then its fallback

    def _upstream(self, first_response):
        first, _second = self._ports()
        return self.use_upstream(
            lambda r: first_response(r) if r.url.port == first else httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"})
        )

    def test_allocation_failure_falls_back_with_a_header(self) -> None:
        seen = self._upstream(lambda r: httpx.Response(500, json={"detail": "CUDA out of memory. Tried to allocate 4.70 GiB."}))
        resp = self._speak()
        self.assertEqual((resp.status_code, resp.content), (200, b"RIFFwav"))
        self.assertEqual([r.url.port for r in seen], list(self._ports()))
        self.assertEqual(resp.headers["x-azizam-fallback"], "supertonic->omnivoice (gpu_oom)")
        self.assertEqual(resp.headers["x-azizam-fallback"], "supertonic->omnivoice (gpu_oom)")
        self.assertEqual(json.loads(seen[1].content)["model"], "k2-fsa/OmniVoice")

    def test_last_engine_allocation_failure_is_retryable_503(self) -> None:
        self.cfg["backends"]["tts-supertonic"]["enabled"] = False
        seen = self.use_upstream(lambda r: httpx.Response(500, json={"detail": "CUDA out of memory. Tried to allocate 4.70 GiB."}))
        resp = self._speak()
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "gpu_memory_unavailable")
        self.assertIn("GPU", resp.json()["error"]["message"])
        self.assertIn("out of memory", resp.json()["error"]["message"].lower())
        self.assertEqual(resp.headers["retry-after"], "30")
        self.assertEqual(len(seen), 1)

    def test_explicit_engine_allocation_failure_does_not_fall_back(self) -> None:
        seen = self._upstream(lambda r: httpx.Response(500, json={"detail": "CUDA out of memory. Tried to allocate 4.70 GiB."}))
        resp = self._speak(model="supertonic")
        self.assertEqual(resp.status_code, 503, resp.text)
        self.assertEqual(resp.json()["error"]["code"], "gpu_memory_unavailable")
        self.assertEqual(resp.headers["retry-after"], "30")
        self.assertNotIn("x-azizam-fallback", resp.headers)
        self.assertEqual(len(seen), 1)

    def test_non_allocation_server_error_is_unchanged(self) -> None:
        seen = self._upstream(lambda r: httpx.Response(500, json={"detail": "synthesis failed"}))
        resp = self._speak()
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json()["error"]["code"], "tts_backend_error")
        self.assertEqual(resp.json()["error"]["message"], "TTS backend rejected the speech request.")
        self.assertNotIn("retry-after", resp.headers)
        self.assertNotIn("x-azizam-fallback", resp.headers)
        self.assertEqual(len(seen), 1)

    def test_skipped_primary_engine_is_reported_without_runtime_retry(self) -> None:
        self.cfg["backends"]["tts-supertonic"]["enabled"] = False
        seen = self.use_upstream(lambda r: httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"}))
        proxy.receipt_store.begin.reset_mock()
        resp = self._speak()
        self.assertEqual((resp.status_code, resp.content), (200, b"RIFFwav"))
        self.assertEqual([r.url.port for r in seen], [self.cfg["backends"]["tts"]["port"]])
        self.assertEqual(resp.headers["x-azizam-fallback"], "supertonic->omnivoice (not_deployed)")
        self.assertEqual(resp.headers["x-azizam-fallback"], "supertonic->omnivoice (not_deployed)")
        receipts = [call.args[0] for call in proxy.receipt_store.begin.call_args_list]
        self.assertIn("supertonic->omnivoice (not_deployed)", receipts[-1].fallback_reasons)

    def test_start_failure_falls_back(self) -> None:
        self.health.side_effect = lambda bcfg, _t: bcfg.get("port") != self._ports()[0]
        self.ensure.return_value = False
        self.use_upstream(lambda r: httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"}))
        resp = self._speak()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["x-azizam-fallback"], "supertonic->omnivoice (start_failed)")

    def test_connection_refused_falls_back(self) -> None:
        def refuse(request):
            raise httpx.ConnectError("refused")
        self._upstream(refuse)
        resp = self._speak()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["x-azizam-fallback"], "supertonic->omnivoice (connection_refused)")

    def test_a_client_error_does_not_fall_back(self) -> None:
        seen = self._upstream(lambda r: httpx.Response(400, json={"error": {"message": "bad voice"}}))
        resp = self._speak()
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("x-azizam-fallback", resp.headers)
        self.assertEqual(len(seen), 1)

    def test_an_engine_id_by_name_never_falls_back(self) -> None:
        seen = self._upstream(lambda r: httpx.Response(503, json={"error": {"message": "busy"}}))
        resp = self._speak(model="supertonic")
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn("x-azizam-fallback", resp.headers)
        self.assertEqual(len(seen), 1)


class TtsStreamingTests(RouteCase):
    def _post(self, **extra):
        payload = {"model": "tts-fast", "voice": "shimmer", "input": "hello", **extra}
        return self.client.post("/v1/audio/speech", json=payload)

    def test_pcm_is_resampled_and_chunked_with_the_pcm_content_type(self) -> None:
        if not HAVE_FFMPEG:
            self.skipTest("ffmpeg is required")
        pcm = struct.pack("<h", 2500) * 44100
        seen = self.use_upstream(
            lambda r: httpx.Response(200, stream=_Chunks([pcm[i : i + 3001] for i in range(0, len(pcm), 3001)]),
                                     headers={"content-type": "application/octet-stream"})
        )
        resp = self._post(response_format="pcm")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "audio/pcm")
        self.assertAlmostEqual(len(resp.content) / 2, 24000, delta=1200)
        sent = json.loads(seen[0].content)
        self.assertEqual(
            {k: sent[k] for k in ("model", "voice", "response_format", "stream_format")},
            {"model": "supertonic", "voice": "F4", "response_format": "pcm", "stream_format": "audio"},
        )

    def test_native_rate_pcm_is_forwarded_without_transcoding(self) -> None:
        self.cfg["model_aliases"]["speech"]["engines"]["supertonic"]["native_sample_rate"] = 24000
        chunks = [b"\x01\x00" * 100, b"\x02\x00" * 100]
        self.use_upstream(lambda r: httpx.Response(200, stream=_Chunks(chunks)))
        with patch.object(speech_stream, "transcode_pcm_stream") as transcode:
            resp = self._post(response_format="pcm")
        transcode.assert_not_called()
        self.assertEqual(resp.content, b"".join(chunks))

    def test_sse_stream_format_emits_audio_delta_events(self) -> None:
        if not HAVE_FFMPEG:
            self.skipTest("ffmpeg is required")
        pcm = struct.pack("<h", 2500) * 4410
        self.use_upstream(lambda r: httpx.Response(200, stream=_Chunks([pcm])))
        resp = self._post(response_format="pcm", stream_format="sse")
        self.assertEqual(resp.headers["content-type"], "text/event-stream; charset=utf-8")
        events = [json.loads(line[6:]) for line in resp.text.split("\n\n") if line.startswith("data: ")]
        self.assertEqual(events[0]["type"], "speech.audio.delta")
        self.assertEqual(events[-1]["type"], "speech.audio.done")
        audio = b"".join(base64.b64decode(e["audio"]) for e in events[:-1])
        self.assertAlmostEqual(len(audio) / 2, 2400, delta=300)

    def test_wav_without_stream_format_stays_buffered(self) -> None:
        seen = self.use_upstream(lambda r: httpx.Response(200, content=_wav_bytes(44100, 1), headers={"content-type": "audio/wav"}))
        resp = self._post(response_format="wav")
        self.assertEqual(resp.headers["content-type"], "audio/wav")
        sent = json.loads(seen[0].content)
        self.assertEqual((sent["response_format"], "stream_format" in sent), ("wav", False))
        self.assertIn("content-length", resp.headers)

    def _dying_stream(self, *chunks: bytes) -> httpx.Response:
        """A 200 chunked answer whose body ends with a protocol error, as an engine does when its lazy model load fails."""
        class Dies(httpx.AsyncByteStream):
            async def __aiter__(self_inner):
                for chunk in chunks:
                    yield chunk
                raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        return httpx.Response(200, stream=Dies())

    def test_stream_that_dies_before_any_audio_is_a_503_and_falls_back(self) -> None:
        engines = self.cfg["model_aliases"]["speech"]["engines"]
        engines["supertonic"]["native_sample_rate"] = 24000
        # The shipped fallback (OmniVoice) answers buffered audio; make it stream so the test sees raw PCM.
        engines["omnivoice"].update(streaming=True, native_sample_rate=24000)
        first_port = self.cfg["backends"]["tts-supertonic"]["port"]
        good = [b"\x01\x00" * 100]
        seen = self.use_upstream(lambda r: self._dying_stream() if r.url.port == first_port else httpx.Response(200, stream=_Chunks(good)))
        resp = self._post(response_format="pcm")
        self.assertEqual((resp.status_code, resp.content), (200, b"".join(good)))
        self.assertEqual(len(seen), 2)
        self.assertTrue(resp.headers["x-azizam-fallback"].startswith("supertonic->omnivoice (busy)"))

    def test_stream_that_dies_before_any_audio_is_not_an_empty_200_when_no_engine_is_left(self) -> None:
        self.cfg["model_aliases"]["speech"]["engines"]["supertonic"]["native_sample_rate"] = 24000
        self.use_upstream(lambda r: self._dying_stream())
        resp = self._post(response_format="pcm", model="supertonic")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["error"]["code"], "tts_engine_stream_failed")

    def test_sse_stream_that_dies_before_any_audio_is_a_503(self) -> None:
        self.use_upstream(lambda r: self._dying_stream())
        resp = self._post(response_format="pcm", stream_format="sse", model="supertonic")
        self.assertEqual(resp.status_code, 503)

    def test_engine_with_an_empty_stream_is_a_502_not_an_empty_200(self) -> None:
        self.use_upstream(lambda r: httpx.Response(200, stream=_Chunks([])))
        resp = self._post(response_format="pcm", model="supertonic")
        self.assertEqual((resp.status_code, resp.json()["error"]["code"]), (502, "tts_engine_empty_stream"))

    def test_engine_error_status_is_returned_before_any_streaming(self) -> None:
        self.use_upstream(lambda r: httpx.Response(400, json={"error": {"message": "bad voice"}}))
        resp = self._post(response_format="pcm")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["message"], "bad voice")

    def test_engine_that_cannot_stream_still_answers_an_sse_request(self) -> None:
        self.use_upstream(lambda r: httpx.Response(200, content=b"RIFFwav", headers={"content-type": "audio/wav"}))
        resp = self.client.post(
            "/v1/audio/speech",
            json={"model": "omnivoice", "voice": "nova", "input": "hi", "response_format": "wav", "stream_format": "sse"},
        )
        events = [json.loads(line[6:]) for line in resp.text.split("\n\n") if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events], ["speech.audio.delta", "speech.audio.done"])
        self.assertEqual(base64.b64decode(events[0]["audio"]), b"RIFFwav")

    def test_bad_stream_format_is_a_coded_400(self) -> None:
        resp = self._post(stream_format="carrier-pigeon")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"]["code"], "unsupported_stream_format")

    def test_voices_route_lists_deployed_engine_voices(self) -> None:
        body = self.client.get("/v1/audio/voices").json()
        engine = body["engines"]["supertonic"]
        self.assertEqual(engine["default_voice"], "F1")
        by_name = {v["voice_id"]: v for v in engine["voices"]}
        self.assertEqual(by_name["shimmer"]["mapped_to"], "F4")
        self.assertEqual(by_name["onyx"]["gender"], "male")
        self.assertEqual(engine["native_voices"][0], "F1")


if __name__ == "__main__":
    unittest.main()

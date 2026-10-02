"""Speech streaming helpers: multipart model peek, SSE framing, ffmpeg PCM transcode."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import unittest

from starlette.requests import Request

from gateway import speech_stream

from tests.speech_tier_helpers import HAVE_FFMPEG, _multipart


class MultipartPeekTests(unittest.IsolatedAsyncioTestCase):
    def test_finds_only_complete_text_fields(self) -> None:
        body, ctype = _multipart([("model", "stt-realtime"), ("language", "en")], b"\x00\x01" * 10)
        boundary = speech_stream.multipart_boundary(ctype)
        self.assertEqual(speech_stream.find_multipart_field(body, boundary, "model"), "stt-realtime")
        self.assertEqual(speech_stream.find_multipart_field(body, boundary, "language"), "en")
        self.assertIsNone(speech_stream.find_multipart_field(body, boundary, "temperature"))
        # A value cut off before its closing boundary is not returned.
        cut = body[: body.index(b"stt-realtime") + 5]
        self.assertIsNone(speech_stream.find_multipart_field(cut, boundary, "model"))

    def test_a_file_part_named_model_is_not_a_text_field(self) -> None:
        body = (
            b'--B\r\nContent-Disposition: form-data; name="model"; filename="m.bin"\r\n\r\nxx\r\n--B--\r\n'
        )
        self.assertIsNone(speech_stream.find_multipart_field(body, b"B", "model"))

    async def _peek(self, body: bytes, ctype: str, chunk: int) -> tuple[str | None, bytes]:
        messages = [
            {"type": "http.request", "body": body[i : i + chunk], "more_body": i + chunk < len(body)}
            for i in range(0, len(body), chunk)
        ]
        queue = list(messages)

        async def receive():
            if queue:
                return queue.pop(0)
            await asyncio.Future()

        request = Request(
            {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-type", ctype.encode())]}, receive
        )
        value, replay = await speech_stream.peek_multipart_field(request, "model")
        replayed = b"".join([chunk async for chunk in replay.stream()])
        return value, replayed

    async def test_peek_replays_the_whole_body_when_model_comes_first(self) -> None:
        body, ctype = _multipart([("model", "stt-realtime")], b"A" * 5000)
        value, replayed = await self._peek(body, ctype, 100)
        self.assertEqual((value, replayed), ("stt-realtime", body))

    async def test_peek_replays_the_whole_body_when_model_is_missing(self) -> None:
        body, ctype = _multipart([("language", "en")], b"A" * 5000)
        value, replayed = await self._peek(body, ctype, 700)
        self.assertEqual((value, replayed), (None, body))

    async def test_model_after_a_small_file_is_still_found(self) -> None:
        body, ctype = _multipart([("model", "stt-quality")], b"A" * 300, file_first=True)
        value, replayed = await self._peek(body, ctype, 64)
        self.assertEqual((value, replayed), ("stt-quality", body))


class StreamFramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_to_sse_frames_base64_deltas_then_done(self) -> None:
        async def source():
            yield b"\x01\x02\x03\x04"
            yield b"\x05\x06"

        events = [e async for e in speech_stream.pcm_to_sse(source())]
        decoded = [json.loads(e[len(b"data: "):]) for e in events]
        self.assertEqual([d["type"] for d in decoded], ["speech.audio.delta"] * 2 + ["speech.audio.done"])
        self.assertEqual(base64.b64decode(decoded[0]["audio"]), b"\x01\x02\x03\x04")
        self.assertTrue(all(e.endswith(b"\n\n") for e in events))

    async def test_align_samples_never_splits_a_sample(self) -> None:
        async def source():
            for chunk in (b"\x01", b"\x02\x03\x04", b"\x05"):
                yield chunk

        chunks = [c async for c in speech_stream.align_samples(source())]
        self.assertTrue(all(len(c) % 2 == 0 for c in chunks))
        self.assertEqual(b"".join(chunks), b"\x01\x02\x03\x04")

    def test_transcript_json_to_sse_is_one_delta_and_a_done(self) -> None:
        events = speech_stream.transcript_json_to_sse({"text": "hello there", "usage": {"type": "duration", "seconds": 1}})
        decoded = [json.loads(e[len(b"data: "):]) for e in events]
        self.assertEqual(decoded[0], {"type": "transcript.text.delta", "delta": "hello there"})
        self.assertEqual(decoded[1]["type"], "transcript.text.done")
        self.assertEqual(decoded[1]["text"], "hello there")
        self.assertEqual(len(speech_stream.transcript_json_to_sse({"text": ""})), 1)

    def test_transcode_is_skipped_only_for_native_openai_pcm(self) -> None:
        self.assertFalse(speech_stream.needs_transcode(24000, "pcm"))
        self.assertTrue(speech_stream.needs_transcode(44100, "pcm"))
        self.assertTrue(speech_stream.needs_transcode(24000, "mp3"))

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
    async def test_ffmpeg_resamples_a_stream_to_openai_pcm(self) -> None:
        samples = 44100
        pcm = struct.pack("<h", 3000) * samples

        async def source():
            for i in range(0, len(pcm), 4096):
                yield pcm[i : i + 4096]

        out = b"".join([c async for c in speech_stream.transcode_pcm_stream(source(), src_rate=44100, response_format="pcm")])
        self.assertAlmostEqual(len(out) / 2, 24000, delta=1200)

    @unittest.skipUnless(HAVE_FFMPEG, "ffmpeg is required")
    async def test_ffmpeg_encodes_mp3_while_streaming(self) -> None:
        async def source():
            yield struct.pack("<h", 2000) * 24000

        out = b"".join([c async for c in speech_stream.transcode_pcm_stream(source(), src_rate=24000, response_format="mp3")])
        self.assertTrue(out[:3] == b"ID3" or out[:2] == b"\xff\xfb" or out[:2] == b"\xff\xf3", out[:4])


if __name__ == "__main__":
    unittest.main()

"""Deterministic subprocess failure coverage; no ffmpeg executable required."""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from gateway import speech_stream


class FakeStdin:
    def __init__(self, *, broken: bool = False) -> None:
        self.written: list[bytes] = []
        self.closed = False
        self.broken = broken

    def write(self, data: bytes) -> None:
        if self.broken:
            raise BrokenPipeError
        self.written.append(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, status: int, *, exited: bool = False, broken: bool = False) -> None:
        self.status = status
        self.returncode = status if exited else None
        self.stdin = FakeStdin(broken=broken)
        self.stdout = AsyncMock()
        self.stdout.read.side_effect = [b"encoded-first", b"encoded-second", b""]
        self.wait_calls = 0
        self.killed = False

    async def wait(self) -> int:
        self.wait_calls += 1
        self.returncode = self.status
        return self.status

    def kill(self) -> None:
        self.killed = True
        self.status = -9
        self.returncode = -9


async def source():
    for chunk in (b"\x01\x02", b"", b"\x03\x04"):
        yield chunk


def transcode(chunks=None):
    return speech_stream.transcode_pcm_stream(
        source() if chunks is None else chunks, src_rate=24000, response_format="mp3"
    )


class SpeechStreamExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_zero_preserves_output_and_input(self) -> None:
        for exited in (False, True):
            with self.subTest(exited=exited):
                process = FakeProcess(0, exited=exited)
                with patch.object(speech_stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                    output = [chunk async for chunk in transcode()]
                self.assertEqual(output, [b"encoded-first", b"encoded-second"])
                self.assertEqual(process.stdin.written, [b"\x01\x02", b"\x03\x04"])
                self.assertTrue(process.stdin.closed)
                self.assertGreaterEqual(process.wait_calls, 1)
                self.assertFalse(process.killed)

    async def test_exit_one_preserves_partial_output_then_raises(self) -> None:
        for exited in (False, True):
            for broken in (False, True):
                with self.subTest(exited=exited, broken=broken):
                    process = FakeProcess(1, exited=exited, broken=broken)
                    output = []
                    with patch.object(speech_stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                        with self.assertRaisesRegex(RuntimeError, "ffmpeg exited with status 1"):
                            async for chunk in transcode():
                                output.append(chunk)
                    self.assertEqual(output, [b"encoded-first", b"encoded-second"])
                    self.assertTrue(process.stdin.closed)
                    self.assertGreaterEqual(process.wait_calls, 1)
                    self.assertFalse(process.killed)

    async def test_missing_binary_propagates_before_consuming_input(self) -> None:
        consumed = False

        async def untouched_source():
            nonlocal consumed
            consumed = True
            yield b"\x01\x02"

        error = FileNotFoundError("ffmpeg not found")
        with patch.object(speech_stream.asyncio, "create_subprocess_exec", AsyncMock(side_effect=error)) as spawn:
            with self.assertRaises(FileNotFoundError) as raised:
                await anext(transcode(untouched_source()))
        self.assertIs(raised.exception, error)
        self.assertFalse(consumed)
        spawn.assert_awaited_once()

    async def test_failed_transcode_does_not_emit_sse_done(self) -> None:
        process = FakeProcess(1)
        events = []
        with patch.object(speech_stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg exited with status 1"):
                async for event in speech_stream.pcm_to_sse(transcode()):
                    events.append(json.loads(event[len(b"data: "):]))
        self.assertEqual([event["type"] for event in events], ["speech.audio.delta"] * 2)

    async def test_source_error_is_not_masked_by_exit_status(self) -> None:
        async def failing_source():
            yield b"\x01\x02"
            raise ValueError("source failed")

        process = FakeProcess(1)
        with patch.object(speech_stream.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(ValueError, "source failed"):
                async for _ in transcode(failing_source()):
                    pass
        self.assertTrue(process.stdin.closed)
        self.assertGreaterEqual(process.wait_calls, 1)


if __name__ == "__main__":
    unittest.main()

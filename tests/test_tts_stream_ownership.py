"""The streaming TTS proxy releases its client and upstream body on every early exit."""

import asyncio
import unittest
from unittest.mock import patch

import httpx

import proxy


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, block=False):
        self.chunks = tuple(chunks)
        self.block = block
        self.entered = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        self.entered.set()
        if self.block:
            await asyncio.Future()
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class TrackedClient:
    def __init__(self, response, *, block_send=False):
        self.response = response
        self.block_send = block_send
        self.entered = asyncio.Event()
        self.closed = False

    def build_request(self, method, url, **kwargs):
        return httpx.Request(method, url, **kwargs)

    async def send(self, request, *, stream):
        assert stream is True
        self.entered.set()
        if self.block_send:
            await asyncio.Future()
        self.response.request = request
        return self.response

    async def aclose(self):
        self.closed = True


class TtsStreamOwnershipGaps(unittest.IsolatedAsyncioTestCase):
    async def open(self, client):
        with patch.object(proxy.httpx, "AsyncClient", return_value=client):
            return await proxy._stream_tts_upstream(
                url="http://fake.invalid/v1/audio/speech",
                headers={},
                body=b'{"input":"hello"}',
                stream_format="audio",
                response_format="pcm",
                native_rate=24000,
            )

    async def test_rejected_stream_closes_client(self):
        for status, raw in (
            (400, b"bad voice"),
            (503, b"busy"),
            (500, b"CUDA out of memory"),
        ):
            with self.subTest(status=status):
                stream = TrackedStream([raw])
                upstream = httpx.Response(status, stream=stream)
                client = TrackedClient(upstream)
                try:
                    response = await self.open(client)
                    self.assertGreaterEqual(response.status_code, 400)
                    self.assertTrue(stream.closed, "rejected body remains open")
                    self.assertTrue(client.closed, "rejected request leaked its client")
                finally:
                    await upstream.aclose()
                    await client.aclose()

    async def test_cancellation_before_handoff_closes_every_owned_resource(self):
        for phase in ("send", "first_chunk"):
            with self.subTest(phase=phase):
                stream = TrackedStream(block=True)
                upstream = httpx.Response(200, stream=stream)
                client = TrackedClient(upstream, block_send=phase == "send")
                task = asyncio.create_task(self.open(client))
                try:
                    entered = client.entered if phase == "send" else stream.entered
                    await asyncio.wait_for(entered.wait(), timeout=1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=1)
                    self.assertTrue(client.closed, "cancellation leaked HTTP client")
                    if phase == "first_chunk":
                        self.assertTrue(stream.closed, "prefetch cancellation leaked body")
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await upstream.aclose()
                    await client.aclose()

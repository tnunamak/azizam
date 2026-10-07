"""Speech regressions with in-process upstreams and controlled stream failures."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.requests import Request

import proxy
from gateway import speech_stream, stt_chunking
from gateway.backends.cancellation import CancellationRegistry
from tests.speech_tier_helpers import RouteCase, SttCase, _multipart


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, after_chunks=None):
        self.chunks = chunks
        self.after_chunks = after_chunks
        self.closed = False
        self.read_started = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        self.read_started.set()
        if self.after_chunks is not None:
            await self.after_chunks()

    async def aclose(self):
        self.closed = True


class NativeSttLifecycleTests(SttCase):
    def setUp(self):
        super().setUp()
        # These tests exercise relay ownership, without invoking an audio decoder.
        self.cfg["model_aliases"]["transcriptions"]["engines"]["nemotron-streaming"].pop("upload_format", None)
        self.registry = CancellationRegistry()
        patcher = patch.object(proxy, "cancellation_registry", return_value=self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def open_response(self, stream, *, max_bytes=None):
        self.use_upstream(lambda _request: httpx.Response(200, stream=stream))
        body, ctype = _multipart([("model", "stt-fast"), ("stream", "true")], b"audio")
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        self.disconnect = asyncio.Event()

        async def receive():
            if messages:
                return messages.pop(0)
            await self.disconnect.wait()
            return {"type": "http.disconnect"}

        request = Request({"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
                           "headers": [(b"content-type", ctype.encode())]}, receive)
        execution = proxy._stt_execution_snapshot("transcriptions", "stt-nemotron")
        if max_bytes is not None:
            execution = replace(execution, max_response_bytes=max_bytes)
        self.receipt = proxy._stt_receipt(request=request, auth_context=proxy.NO_AUTH_CONTEXT,
                                          operation="transcriptions", service_id=execution.service_id)
        self.active_before = stt_chunking.GLOBAL_STT_RESOURCE_GATE._active
        response = await proxy.proxy_stt_request(request, proxy.NO_AUTH_CONTEXT,
                                                 execution_snapshot=execution, route_receipt=self.receipt)
        self.assertEqual(response.status_code, 200, getattr(response, "body", b""))
        return response

    def assert_terminal(self, stream, status):
        self.assertTrue(stream.closed)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])
        self.assertEqual(stt_chunking.GLOBAL_STT_RESOURCE_GATE._active, self.active_before)
        proxy.receipt_store.finalize.assert_called_once()
        self.assertEqual(self.receipt.status, status)
        entry = self.registry._entries.get(self.receipt.request_id)
        self.assertTrue(entry is None or entry.live_handles == 0)

    def test_success_is_reported_only_after_body_completion(self):
        async def scenario():
            stream = ControlledStream([b"first", b"second"])
            response = await self.open_response(stream)
            try:
                proxy.receipt_store.finalize.assert_not_called()
                self.assertEqual(await anext(response.body_iterator), b"first")
                proxy.receipt_store.finalize.assert_not_called()
                self.assertEqual(b"".join([part async for part in response.body_iterator]), b"second")
                self.assert_terminal(stream, "succeeded")
            finally:
                _ = [part async for part in response.body_iterator]
                await response.body_iterator.aclose()
        asyncio.run(scenario())

    def test_body_transport_failure_has_one_failed_receipt(self):
        async def fail():
            raise httpx.ReadError("private upstream detail")

        async def scenario():
            stream = ControlledStream([b"partial"], after_chunks=fail)
            response = await self.open_response(stream)
            with self.assertRaises(httpx.ReadError):
                _ = [part async for part in response.body_iterator]
            self.assert_terminal(stream, "failed")
        asyncio.run(scenario())

    def test_response_budget_is_applied_before_yielding_the_excess_chunk(self):
        async def scenario():
            stream = ControlledStream([b"123", b"456"])
            response = await self.open_response(stream, max_bytes=5)
            try:
                self.assertEqual(await anext(response.body_iterator), b"123")
                with self.assertRaises(stt_chunking.SttResponseLimitExceeded):
                    await anext(response.body_iterator)
                self.assert_terminal(stream, "failed")
            finally:
                await response.body_iterator.aclose()
        asyncio.run(scenario())

    def test_deadline_is_checked_on_each_body_read(self):
        async def scenario():
            stream = ControlledStream([b"first", b"second"])
            response = await self.open_response(stream)
            try:
                self.assertEqual(await anext(response.body_iterator), b"first")
                # Advance past the fixed overall deadline after headers and first data.
                with patch.object(stt_chunking.time, "monotonic", return_value=10**12):
                    with self.assertRaises(stt_chunking.SttDeadlineExceeded):
                        await anext(response.body_iterator)
                self.assert_terminal(stream, "failed")
            finally:
                await response.body_iterator.aclose()
        asyncio.run(scenario())

    def test_registry_cancellation_interrupts_a_pending_body_read(self):
        self._cancel_pending_read("registry")

    def test_disconnect_interrupts_a_pending_body_read(self):
        self._cancel_pending_read("disconnect")

    def test_task_cancellation_signals_registry_and_closes_upstream(self):
        self._cancel_pending_read("task")

    def _cancel_pending_read(self, mode):
        async def block():
            await asyncio.Future()

        async def scenario():
            stream = ControlledStream([b"partial"], after_chunks=block)
            response = await self.open_response(stream)
            self.assertEqual(await anext(response.body_iterator), b"partial")
            read = asyncio.create_task(anext(response.body_iterator))
            await stream.read_started.wait()
            if mode == "registry":
                self.registry.signal(self.receipt.request_id)
            elif mode == "disconnect":
                self.disconnect.set()
            else:
                read.cancel()
            try:
                done, _ = await asyncio.wait({read}, timeout=0.5)
                self.assertIn(read, done, "body read ignored cancellation")
                with self.assertRaises(asyncio.CancelledError):
                    await read
                self.assertTrue(self.registry.is_cancelled(self.receipt.request_id))
                self.assert_terminal(stream, "cancelled")
            finally:
                read.cancel()
                await asyncio.gather(read, return_exceptions=True)
        asyncio.run(scenario())


class BufferedTtsErrorTests(RouteCase):
    def test_backend_errors_are_json_envelopes_without_upstream_internals(self):
        for status, body, headers in [
            (400, b"private upstream detail", {"content-type": "text/plain"}),
            (500, b"<html>private upstream detail</html>", {"content-type": "text/html"}),
            (503, b'{"detail":"private upstream detail"}', {}),
            (302, b"private upstream detail", {}),
        ]:
            with self.subTest(status=status):
                self.use_upstream(lambda _request: httpx.Response(status, content=body, headers=headers))
                response = self.client.post("/v1/audio/speech", json={
                    "model": "tts-fast", "voice": "shimmer", "input": "hello", "response_format": "wav",
                })
                self.assertEqual(response.status_code, status if status >= 400 else 502)
                self.assertEqual(response.headers["content-type"], "application/json")
                error = response.json()["error"]
                self.assertIsInstance(error["message"], str)
                self.assertEqual(error["type"], "invalid_request_error" if 400 <= status < 500 else "api_error")
                self.assertEqual(error["code"], "tts_backend_error")
                self.assertNotIn(b"private upstream detail", response.content)


class BufferedSttStreamValidationTests(SttCase):
    def test_stream_rejects_unsupported_formats_before_inference(self):
        seen = self._json_upstream()
        for model in ("stt-fast", "whisper-1"):
            for response_format in ("text", "srt", "vtt", "unknown"):
                with self.subTest(model=model, response_format=response_format):
                    response = self._post([("model", model), ("stream", "true"), ("response_format", response_format)])
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"]["param"], "response_format")
        self.assertEqual(seen, [])

    def test_invalid_sse_payloads_return_a_safe_backend_error(self):
        for body in (b"123", b"null", b"[]", b'"words"', b"not json", b"{}", b'{"text":123}'):
            with self.subTest(body=body):
                self.use_upstream(lambda _request: httpx.Response(200, content=body))
                response = self._post([("model", "whisper-1"), ("stream", "true")])
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["error"]["code"], "invalid_stt_stream_response")

    def test_json_formats_emit_sse_and_preserve_text_and_usage(self):
        self.use_upstream(lambda _request: httpx.Response(200, json={"text": "words", "usage": {"seconds": 1}}))
        for response_format in ("json", "verbose_json"):
            with self.subTest(response_format=response_format):
                response = self._post([("model", "whisper-1"), ("stream", "true"), ("response_format", response_format)])
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
                events = [json.loads(event[6:]) for event in response.content.split(b"\n\n") if event]
                self.assertEqual(events[-1], {"type": "transcript.text.done", "text": "words", "usage": {"seconds": 1}})

    def test_sse_helper_rejects_nonobjects_and_nonstring_text(self):
        for payload in (123, None, [], "words", {}, {"text": 123}, {"text": None}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                speech_stream.transcript_json_to_sse(payload)

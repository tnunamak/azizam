"""Fake-only regressions for speech/audio proxy handler seams."""

import asyncio
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import proxy
from tests.speech_tier_helpers import SttCase


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

    async def test_partial_close_releases_stream_and_client(self):
        stream = TrackedStream([b"\x01\x00", b"\x02\x00"])
        upstream = httpx.Response(200, stream=stream)
        client = TrackedClient(upstream)
        response = None
        try:
            response = await self.open(client)
            self.assertFalse(client.closed, "ownership ended before body delivery")
            self.assertEqual(await anext(response.body_iterator), b"\x01\x00")
            await response.body_iterator.aclose()
            self.assertTrue(stream.closed)
            self.assertTrue(client.closed)
        finally:
            if response is not None:
                await response.body_iterator.aclose()
            await upstream.aclose()
            await client.aclose()


class SttFallbackPolicyGap(SttCase):
    def test_denied_middle_fallback_is_never_dispatched(self):
        evaluated = []
        attempts = []

        def authorize(receipt, auth, *, operation):
            evaluated.append((receipt.backend, operation))
            return SimpleNamespace(denied=receipt.backend == "stt")

        async def attempt(request, auth, **kwargs):
            service = kwargs["execution_snapshot"].service_id
            attempts.append((service, await request.body()))
            if service == "stt-parakeet-gpu":
                return proxy.Response(
                    b'{"error":{"message":"busy"}}',
                    status_code=503,
                    media_type="application/json",
                )
            return proxy.Response(
                b'{"text":"allowed fallback"}',
                status_code=200,
                media_type="application/json",
            )

        with patch.object(
            proxy, "_apply_selected_stt_policy", side_effect=authorize
        ), patch.object(
            proxy, "_ensure_speech_service_ready", AsyncMock(return_value=None)
        ), patch.object(
            proxy, "_dispatch_admission_response", return_value=None
        ), patch.object(proxy, "proxy_stt_request", side_effect=attempt):
            response = self._post([("model", "stt-quality")], audio=b"fake audio")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"text": "allowed fallback"})
        self.assertEqual(
            evaluated,
            [
                ("stt-parakeet-gpu", "transcriptions"),
                ("stt", "transcriptions"),
                ("stt-canary", "transcriptions"),
            ],
        )
        self.assertEqual(
            [service for service, _ in attempts],
            ["stt-parakeet-gpu", "stt-canary"],
        )
        self.assertEqual(attempts[0][1], attempts[1][1])
        self.assertEqual(
            response.headers["x-azizam-fallback"],
            "parakeet-gpu->canary (busy)",
        )


class JsonRequest:
    headers = {"content-type": "application/json"}

    async def json(self):
        return {"model": "fake-audio", "prompt": "a tone", "duration": 8}


class FakeAudioClient:
    def __init__(self, case):
        self.case = case
        self.posts = 0
        self.history_reads = 0
        self.view_reads = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False

    async def post(self, url, **kwargs):
        self.posts += 1
        assert url.endswith("/prompt")
        if self.case == "submit_error":
            return httpx.Response(500, text="synthetic failure")
        if self.case == "missing_id":
            return httpx.Response(200, json={})
        if self.case == "transport_timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if self.case == "connect_error":
            raise httpx.ConnectError("synthetic refusal")
        return httpx.Response(200, json={"prompt_id": "p1"})

    async def get(self, url, **kwargs):
        if "/history/" in url:
            self.history_reads += 1
            if self.history_reads > 600:
                raise AssertionError("poll limit exceeded")
            if self.case == "exhausted":
                return httpx.Response(200, json={})
            if self.case == "transient_history":
                if self.history_reads == 1:
                    return httpx.Response(503, text="busy")
                if self.history_reads == 2:
                    return httpx.Response(200, json={})
            if self.case == "execution_error":
                job = {"status": {"status_str": "error", "messages": []}}
            elif self.case == "view_error":
                job = {
                    "status": {"status_str": "success"},
                    "outputs": {
                        "1": {"audio": [{"filename": "a.wav", "type": "output"}]}
                    },
                }
            else:
                job = {"status": {"status_str": "success"}, "outputs": {}}
            return httpx.Response(200, json={"p1": job})
        assert url.endswith("/view")
        self.view_reads += 1
        return httpx.Response(404, text="synthetic missing asset")


class AudioFailurePollingGaps(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, case):
        client = FakeAudioClient(case)
        workflow = {"_meta": {}, "1": {"inputs": {}}}
        with ExitStack() as stack:
            replacements = {
                "COMFY_ENABLED": True,
                "COMFY_HOST": "http://fake.invalid",
                "_dispatch_admission_response": lambda *a, **kw: None,
                "_service_availability_response": lambda *a, **kw: None,
                "_begin_media_delivery": lambda *a, **kw: None,
                "_note_media_metadata_body": lambda *a, **kw: None,
                "_resolve_audio_workflow": lambda model: (
                    "fake-audio",
                    {"default_duration": 8, "min_seconds": 1, "max_seconds": 30},
                ),
                "_load_audio_workflow_file": lambda cfg: workflow,
                "_load_audio_workflow_manifest": lambda name: None,
                "evaluate_and_apply": lambda *a, **kw: SimpleNamespace(denied=False),
                "_workflow_target_policy_decision": lambda *a, **kw: None,
                "configured_downstream_headers": lambda *a, **kw: {},
                "_build_comfy_submit_payload": lambda graph, **kw: {"prompt": graph},
            }
            for name, value in replacements.items():
                stack.enter_context(patch.object(proxy, name, value))
            stack.enter_context(
                patch.object(proxy.httpx, "AsyncClient", return_value=client)
            )
            sleep = stack.enter_context(patch.object(proxy.asyncio, "sleep", AsyncMock()))
            response = await proxy.proxy_audio_generate(
                JsonRequest(), proxy.NO_AUTH_CONTEXT
            )
        return response, client, sleep

    async def test_submission_and_terminal_failures_have_honest_statuses(self):
        cases = (
            ("submit_error", 502, 0, 0, "ComfyUI error"),
            ("missing_id", 502, 0, 0, "No prompt_id"),
            ("transport_timeout", 504, 0, 0, "timeout"),
            ("connect_error", 502, 0, 0, "unavailable"),
            ("execution_error", 502, 1, 0, "generation failed"),
            ("missing_audio", 502, 1, 0, "No audio output"),
            ("view_error", 502, 1, 1, "No audio output"),
            ("transient_history", 502, 3, 0, "No audio output"),
            ("exhausted", 504, 600, 0, "generation timeout"),
        )
        for case, status, histories, views, message in cases:
            with self.subTest(case=case):
                response, client, sleep = await self.run_case(case)
                self.assertEqual(response.status_code, status, response.body)
                self.assertEqual(
                    response.headers["content-type"], "application/json"
                )
                payload = json.loads(response.body)
                self.assertEqual(payload["error"]["type"], "api_error")
                self.assertIn(message.lower(), payload["error"]["message"].lower())
                self.assertEqual(client.posts, 1)
                self.assertEqual(client.history_reads, histories)
                self.assertEqual(client.view_reads, views)
                self.assertEqual(sleep.await_count, histories)
                self.assertTrue(client.closed)
                self.assertNotIn("x-audio-model", response.headers)

"""Regressions for speech/audio proxy handler seams.

The speech engine and ComfyUI are served by the FK fakes
(tests/support/fake_upstream/scenarios); the gateway's httpx clients are real.
``fake_upstreams.track_clients`` only records whether each client was closed.
"""

import asyncio
import json
import socket
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import proxy
from tests.speech_tier_helpers import SttCase
from tests.support.fake_upstream import fake_upstreams, load_scenario, read_deadline  # noqa: F401

HANG_BOUND_SECONDS = 10


class TtsStreamOwnershipGaps(unittest.IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, fake_upstreams):
        self.fakes = fake_upstreams

    async def test_partial_close_releases_stream_and_client(self):
        fake = self.fakes.asgi(load_scenario("openai-compatible/speech-stream-held"))
        clients = self.fakes.track_clients(proxy)
        response = None
        try:
            async with asyncio.timeout(HANG_BOUND_SECONDS):
                response = await proxy._stream_tts_upstream(
                    url=f"{fake.base_url}/v1/audio/speech",
                    headers={},
                    body=b'{"input":"hello"}',
                    stream_format="audio",
                    response_format="pcm",
                    native_rate=24000,
                )
                [client] = clients
                self.assertFalse(client.closed, "ownership ended before body delivery")
                self.assertEqual(await anext(response.body_iterator), b"\x01\x00")
                self.assertEqual(fake.client_closed, [], "stream closed before the caller finished")
                await response.body_iterator.aclose()
            # The engine still holds the stream open: only the gateway closing it ends the call.
            self.assertEqual(fake.client_closed, ["speech"])
            self.assertTrue(client.closed)
        finally:
            if response is not None:
                await response.body_iterator.aclose()


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


class AudioFailurePollingGaps(unittest.IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, fake_upstreams, monkeypatch):
        self.fakes = fake_upstreams
        self.monkeypatch = monkeypatch

    async def run_case(self, scenario, *, wire=False, refused=False, **values):
        fake = None
        if refused:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                host = f"http://127.0.0.1:{probe.getsockname()[1]}"
        else:
            loaded = load_scenario(f"comfyui/{scenario}", **values)
            fake = self.fakes.loopback(loaded) if wire else self.fakes.asgi(loaded)
            host = fake.base_url
        clients = self.fakes.track_clients(proxy)
        workflow = {"_meta": {}, "1": {"inputs": {}}}
        with ExitStack() as stack:
            replacements = {
                "COMFY_ENABLED": True,
                "COMFY_HOST": host,
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
            sleep = stack.enter_context(patch.object(proxy.asyncio, "sleep", AsyncMock()))
            response = await asyncio.wait_for(
                proxy.proxy_audio_generate(JsonRequest(), proxy.NO_AUTH_CONTEXT),
                timeout=HANG_BOUND_SECONDS,
            )
        return response, fake, clients, sleep

    async def test_submission_and_terminal_failures_have_honest_statuses(self):
        job_error = {"status": {"status_str": "error", "messages": []}}
        job_empty = {"status": {"status_str": "success"}, "outputs": {}}
        cases = (
            ("submit_error", "audio-submit-text-reply", {"status": 500, "text": "synthetic failure"}, 502, 0, 0, "ComfyUI error"),
            ("missing_id", "audio-submit-json-reply", {"status": 200, "reply": {}}, 502, 0, 0, "No prompt_id"),
            ("transport_timeout", "audio-submit-slow-first-byte", {"wire": True}, 504, 0, 0, "timeout"),
            ("connect_error", None, {"refused": True}, 502, 0, 0, "unavailable"),
            ("execution_error", "audio-job-reply", {"job": job_error}, 502, 1, 0, "generation failed"),
            ("missing_audio", "audio-job-reply", {"job": job_empty}, 502, 1, 0, "No audio output"),
            ("view_error", "audio-view-missing", {}, 502, 1, 1, "No audio output"),
            ("transient_history", "audio-history-transient", {}, 502, 3, 0, "No audio output"),
            ("exhausted", "audio-history-never-done", {}, 504, 600, 0, "generation timeout"),
        )
        for case, scenario, values, status, histories, views, message in cases:
            with self.subTest(case=case):
                deadline = (
                    read_deadline(self.monkeypatch, self.fakes.clock) if case == "transport_timeout" else None
                )
                response, fake, clients, sleep = await self.run_case(scenario, **values)
                self.assertEqual(response.status_code, status, response.body)
                self.assertEqual(
                    response.headers["content-type"], "application/json"
                )
                payload = json.loads(response.body)
                self.assertEqual(payload["error"]["type"], "api_error")
                self.assertIn(message.lower(), payload["error"]["message"].lower())
                if fake is not None:
                    self.assertEqual(len(fake.calls("prompt")), 1)
                    history_calls = [r for r in fake.requests if r.path.startswith("/history/")]
                    self.assertEqual(len(history_calls), histories)
                    self.assertEqual(len([r for r in fake.requests if r.path == "/view"]), views)
                if deadline is not None:
                    self.assertEqual(deadline.read_fired, 1)
                self.assertEqual(sleep.await_count, histories)
                self.assertTrue(all(client.closed for client in clients) and clients)
                self.assertNotIn("x-audio-model", response.headers)

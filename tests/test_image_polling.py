"""Image completion polling latency and wait-budget contracts."""

import json
import unittest
from unittest.mock import patch

import httpx

import proxy


class TestImagePolling(unittest.IsolatedAsyncioTestCase):
    async def _run_job(self, *, complete_at=None, transient_errors=False):
        elapsed = 0.0
        sleeps = []
        checks = []
        cancellations = []

        async def sleep(delay):
            nonlocal elapsed
            sleeps.append(delay)
            elapsed += delay

        def respond(request):
            path = request.url.path
            if path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "image-job"})
            if path == "/history/image-job":
                checks.append(elapsed)
                if transient_errors:
                    if len(checks) == 1:
                        raise httpx.ConnectError("unavailable", request=request)
                    if len(checks) == 2:
                        raise httpx.ReadTimeout("slow", request=request)
                    if len(checks) == 3:
                        return httpx.Response(503)
                if complete_at is not None and elapsed >= complete_at:
                    return httpx.Response(200, json={"image-job": {
                        "status": {"completed": True},
                        "outputs": {"1": {"images": [{"filename": "image.png"}]}},
                    }})
                return httpx.Response(200, json={})
            if path == "/view":
                return httpx.Response(200, content=b"generated image")
            if path == "/queue":
                return httpx.Response(200, json={
                    "queue_running": [], "queue_pending": [[1, "image-job"]],
                })
            if path == "/api/jobs/image-job/cancel":
                cancellations.append("image-job")
                return httpx.Response(200, json={"cancelled": True})
            raise AssertionError(path)

        with patch.object(proxy.asyncio, "sleep", sleep), patch.object(
            proxy, "COMFY_TIMEOUT", 100
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                error, images = await proxy._comfy_run_round(
                    client, {}, {}, "poll test", max_wait_s=2,
                )
        return error, images, elapsed, sleeps, checks, cancellations

    async def test_fast_completion_returns_without_sleep(self):
        error, images, elapsed, sleeps, checks, cancellations = await self._run_job(
            complete_at=0,
        )
        self.assertIsNone(error)
        self.assertEqual(images, [b"generated image"])
        self.assertEqual(elapsed, 0)
        self.assertEqual(sleeps, [])
        self.assertEqual(checks, [0])
        self.assertEqual(cancellations, [])

    async def test_slow_completion_keeps_polling(self):
        error, images, elapsed, sleeps, checks, cancellations = await self._run_job(
            complete_at=0.8,
        )
        self.assertIsNone(error)
        self.assertEqual(images, [b"generated image"])
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertLessEqual(elapsed, 1.3)
        self.assertGreater(len(checks), 1)
        self.assertLessEqual(max(sleeps), 0.5)
        self.assertEqual(cancellations, [])

    async def test_slow_job_times_out_with_original_wait_budget(self):
        error, images, elapsed, sleeps, checks, cancellations = await self._run_job(
            transient_errors=True,
        )
        self.assertEqual(images, [])
        self.assertIsNotNone(error)
        self.assertEqual(error.status_code, 504)
        body = json.loads(error.body)
        self.assertEqual(body["error"]["code"], "image_generation_timeout")
        self.assertEqual(body["error"]["queue_position"], 1)
        self.assertAlmostEqual(elapsed, 2)
        self.assertGreater(len(checks), 4)
        self.assertEqual(checks[0], 0)
        self.assertEqual(sleeps[:3], [0.05, 0.1, 0.2])
        self.assertLessEqual(max(sleeps), 0.5)
        self.assertEqual(cancellations, ["image-job"])

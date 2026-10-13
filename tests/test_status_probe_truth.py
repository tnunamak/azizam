"""The Status probe must not report a slow or sleeping engine as refusing connections."""

from __future__ import annotations

import asyncio
import socket
import ssl
import unittest
from unittest.mock import patch

import httpx

import status_endpoint as status


def _raising(exc: Exception) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _probe(client: httpx.AsyncClient, service: dict | None = None) -> dict:
    async with client:
        return await status._probe(client, "svc", "Svc", "http://127.0.0.1:1/health", "required", None, service=service)


class ProbeTruthTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_timeout_is_a_slow_answer_not_a_refusal(self):
        row = await _probe(_raising(httpx.ConnectTimeout("slow host")))
        self.assertEqual(row["last_error"], "timeout")
        self.assertNotIn("refused", str(row["last_error"]))
        self.assertEqual(row["live_status"], "degraded")

    async def test_connect_budget_tolerates_a_loaded_host(self):
        self.assertGreaterEqual(status._HTTP_TIMEOUT.connect, 3.0)

    async def test_refused_connection_still_reads_as_refused(self):
        row = await _probe(_raising(httpx.ConnectError("refused")))
        self.assertEqual(row["last_error"], "connect refused")
        self.assertEqual(row["live_status"], "down")

    async def test_really_closed_port_is_refused(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        async with httpx.AsyncClient(timeout=status._HTTP_TIMEOUT) as client:
            row = await status._probe(client, "svc", "Svc", f"http://127.0.0.1:{port}/health", "required", None)
        self.assertEqual(row["last_error"], "connect refused")
        self.assertEqual(row["live_status"], "down")

    async def test_on_demand_service_that_is_not_running_is_idle(self):
        row = await _probe(_raising(httpx.ConnectError("refused")), {"residency": "on_demand"})
        self.assertEqual(row["live_status"], "stopped")
        self.assertIsNone(row["last_error"])

    async def test_on_demand_declared_in_config_is_idle(self):
        row = await _probe(_raising(httpx.ConnectError("refused")), {"config": {"residency": "on_demand"}})
        self.assertEqual(row["live_status"], "stopped")

    async def test_on_demand_service_that_times_out_is_still_a_fault(self):
        row = await _probe(_raising(httpx.ReadTimeout("hung")), {"residency": "on_demand"})
        self.assertEqual(row["live_status"], "degraded")


class BackendGridLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_ssl_context_is_not_built_on_the_event_loop(self):
        on_loop: list[bool] = []
        real = ssl.create_default_context

        def spy(*args, **kwargs):
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:
                on_loop.append(False)
            return real(*args, **kwargs)

        from gateway import http_tls

        with patch.object(http_tls, "_CONTEXT", None), patch.object(ssl, "create_default_context", spy):
            await status._backend_grid({"backends": {}})
        self.assertTrue(on_loop, "no SSL context was built")
        self.assertFalse(any(on_loop), "SSL context was built on the event loop")


if __name__ == "__main__":
    unittest.main()

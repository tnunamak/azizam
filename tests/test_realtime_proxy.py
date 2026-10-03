"""/v1/realtime: auth and policy at the gateway, frames relayed to the Realtime service."""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import proxy
from gateway import realtime_proxy


class FakeUpstream:
    """The service side of a session: replays ``frames``, then stays open or ends with ``end_code``."""

    def __init__(self, frames=(), end_code=None):
        self.frames, self.end_code = list(frames), end_code
        self.sent: list = []
        self.close_code = None
        self.closed = False

    async def send(self, data) -> None:
        self.sent.append(data)

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        for frame in self.frames:
            yield frame
        if self.end_code is not None:
            self.close_code = self.end_code
            return
        while not self.closed:
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        self.closed = True


def settle(condition, timeout: float = 3.0) -> None:
    """Wait for the relay task. TestClient cancels the app the moment its ``with`` block exits."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the relay did not reach the expected state")
        time.sleep(0.01)


class RealtimeRouteCase(unittest.TestCase):
    def setUp(self) -> None:
        cfg = dict(proxy.config)
        cfg["backends"] = {**cfg["backends"], "realtime": {"host": "127.0.0.1", "port": 8765, "capabilities": ["realtime"]}}
        self.upstream = FakeUpstream()
        self.connect = AsyncMock(side_effect=lambda *a, **k: self.upstream)
        self.ensure = AsyncMock(return_value=None)
        self.authenticate = []
        for p in (
            patch.object(proxy, "config", cfg),
            patch.object(proxy, "ws_connect", self.connect),
            patch.object(proxy, "_ensure_speech_service_ready", self.ensure),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "AUTH_ENABLED", False),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(proxy.app)


class RelayTests(RealtimeRouteCase):
    def test_frames_flow_both_ways_and_the_query_is_forwarded_without_the_key(self) -> None:
        self.upstream = FakeUpstream(frames=['{"type":"session.created"}', b"\x00\x01"])
        with self.client.websocket_connect("/v1/realtime?model=gpt-realtime&api_key=sk-secret") as ws:
            self.assertEqual(ws.receive_text(), '{"type":"session.created"}')
            self.assertEqual(ws.receive_bytes(), b"\x00\x01")
            ws.send_text('{"type":"session.update"}')
            ws.send_bytes(b"\x02")
            settle(lambda: len(self.upstream.sent) == 2)
        self.assertEqual(self.upstream.sent, ['{"type":"session.update"}', b"\x02"])
        self.assertEqual(self.connect.await_args.args[0], "ws://127.0.0.1:8765/v1/realtime?model=gpt-realtime")
        settle(lambda: self.upstream.closed)

    def test_openai_prefixed_path_works(self) -> None:
        with self.client.websocket_connect("/openai/v1/realtime") as ws:
            ws.send_text("x")
            settle(lambda: self.upstream.sent == ["x"])
        self.connect.assert_awaited_once()

    def test_realtime_subprotocol_is_echoed_and_the_key_subprotocol_is_not(self) -> None:
        with self.client.websocket_connect(
            "/v1/realtime", subprotocols=["realtime", "openai-insecure-api-key.sk-x", "openai-beta.realtime-v1"]
        ) as ws:
            self.assertEqual(ws.accepted_subprotocol, "realtime")
            ws.send_text("x")
            settle(lambda: self.upstream.sent == ["x"])

    def test_upstream_close_code_reaches_the_client(self) -> None:
        self.upstream = FakeUpstream(frames=["bye"], end_code=1008)
        with self.client.websocket_connect("/v1/realtime") as ws:
            self.assertEqual(ws.receive_text(), "bye")
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_text()
        self.assertEqual(caught.exception.code, 1008)

    def test_service_is_started_on_demand(self) -> None:
        with self.client.websocket_connect("/v1/realtime") as ws:
            ws.send_text("x")
            settle(lambda: self.upstream.sent == ["x"])
        self.ensure.assert_awaited_once()
        self.assertEqual(self.ensure.await_args.args[0], "realtime")


class RefusalTests(RealtimeRouteCase):
    def _refused(self, path: str = "/v1/realtime") -> int:
        with self.assertRaises(Exception) as caught:
            with self.client.websocket_connect(path):
                self.fail("the upgrade was accepted")
        self.connect.assert_not_called()
        exc = caught.exception
        return getattr(exc, "status_code", None) or getattr(exc, "code", None)

    def test_missing_key_is_a_401_before_any_upstream_connection(self) -> None:
        with patch.object(proxy, "AUTH_ENABLED", True), patch.object(proxy, "_client_is_lan", return_value=False):
            self.assertEqual(self._refused(), 401)

    def test_a_key_in_the_subprotocol_is_checked_like_a_bearer_header(self) -> None:
        seen = {}

        def authenticate(request, path):
            seen["authorization"] = request.headers.get("authorization")
            return proxy.auth_error("Invalid API key.")

        with patch.object(proxy, "authenticate_request", authenticate):
            with self.assertRaises(Exception):
                with self.client.websocket_connect("/v1/realtime", subprotocols=["realtime", "openai-insecure-api-key.sk-abc"]):
                    pass
        self.assertEqual(seen["authorization"], "Bearer sk-abc")

    def test_a_profile_without_speech_chat_and_transcription_is_a_403(self) -> None:
        denied = proxy._tts_error("not allowed", 403, "policy_denied")
        with patch.object(proxy, "_enforce_runtime_route_policy", side_effect=[None, denied]):
            self.assertEqual(self._refused(), 403)

    def test_all_three_routes_the_service_calls_are_checked(self) -> None:
        with patch.object(proxy, "_enforce_runtime_route_policy", return_value=None) as enforce:
            with self.client.websocket_connect("/v1/realtime") as ws:
                ws.send_text("x")
                settle(lambda: self.upstream.sent == ["x"])
        self.assertEqual([c.args[1] for c in enforce.call_args_list], list(proxy._REALTIME_POLICY_PATHS))

    def test_unconfigured_service_is_a_503(self) -> None:
        cfg = {**proxy.config, "backends": {k: v for k, v in proxy.config["backends"].items() if k != "realtime"}}
        with patch.object(proxy, "config", cfg):
            self.assertEqual(self._refused(), 503)

    def test_service_that_will_not_start_is_a_503(self) -> None:
        self.ensure.return_value = proxy._tts_error("down", 503)
        self.assertEqual(self._refused(), 503)

    def test_service_that_refuses_the_connection_is_a_503(self) -> None:
        self.connect.side_effect = ConnectionRefusedError()
        with self.assertRaises(Exception) as caught:
            with self.client.websocket_connect("/v1/realtime"):
                pass
        self.assertEqual(getattr(caught.exception, "status_code", None), 503)


class HelperTests(unittest.TestCase):
    def test_key_and_subprotocol_helpers(self) -> None:
        self.assertEqual(realtime_proxy.key_from_subprotocols(["realtime", "openai-insecure-api-key.sk-1"]), "sk-1")
        self.assertEqual(realtime_proxy.key_from_subprotocols(["realtime"]), "")
        self.assertEqual(realtime_proxy.accepted_subprotocol(["x", "realtime"]), "realtime")
        self.assertIsNone(realtime_proxy.accepted_subprotocol([]))

    def test_upstream_url_drops_only_the_key(self) -> None:
        self.assertEqual(realtime_proxy.upstream_url("h", 1, {"model": "m", "api_key": "k"}), "ws://h:1/v1/realtime?model=m")
        self.assertEqual(realtime_proxy.upstream_url("h", 1, {}), "ws://h:1/v1/realtime")


if __name__ == "__main__":
    unittest.main()

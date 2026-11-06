"""/v1/realtime: auth and policy at the gateway, frames relayed to the Realtime service."""

from __future__ import annotations

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

import proxy
from tests.support.access import authenticated, access_context  # noqa: E402
from gateway import realtime_proxy


class FakeUpstream:
    """The service side of a session: replays frames, then stays open or ends."""

    def __init__(self, frames=(), end_code=None, *, end=False, error=None):
        self.frames, self.end_code = list(frames), end_code
        self.end, self.error = end, error
        self.sent: list = []
        self.close_code = None
        self.closed = False
        self.close_calls = 0
        self.started = asyncio.Event()
        self.iteration_ended = False

    async def send(self, data) -> None:
        self.sent.append(data)

    def __aiter__(self):
        return self._frames()

    async def _frames(self):
        self.started.set()
        try:
            for frame in self.frames:
                yield frame
            self.close_code = self.end_code
            if self.error is not None:
                raise self.error
            if self.end or self.end_code is not None:
                return
            while not self.closed:
                await asyncio.sleep(0.01)
        finally:
            self.iteration_ended = True

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def settle(condition, timeout: float = 3.0) -> None:
    """Wait for the relay task. TestClient cancels the app the moment its ``with`` block exits."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the relay did not reach the expected state")
        time.sleep(0.01)


def finish(ws, upstream) -> None:
    """Close from the client and wait for the relay to end, so the test client has nothing left to cancel."""
    ws.close()
    settle(lambda: upstream.closed)


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
        ):
            p.start()
            self.addCleanup(p.stop)
        # By default the caller holds a key of an App whose plans allow everything.
        self.auth = authenticated()
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.client = TestClient(proxy.app)


class RelayTests(RealtimeRouteCase):
    def _upstream_error_then_close(self, ws) -> dict:
        first = ws.receive()
        close = ws.receive() if first["type"] == "websocket.send" else first
        self.assertEqual(close["type"], "websocket.close")
        self.assertEqual(close["code"], 1011)
        self.assertEqual(first["type"], "websocket.send")
        event = json.loads(first["text"])
        self.assertEqual(event["type"], "error")
        self.assertRegex(event["event_id"], r"^event_[0-9a-f]{24}$")
        self.assertEqual(event["error"]["type"], "server_error")
        self.assertEqual(event["error"]["code"], "upstream_closed")
        self.assertTrue(event["error"]["message"])
        self.assertIsNone(event["error"]["param"])
        self.assertIsNone(event["error"]["event_id"])
        settle(lambda: self.upstream.closed)
        self.assertEqual(self.upstream.close_calls, 1)
        self.assertTrue(self.upstream.iteration_ended)
        return event

    def test_abnormal_upstream_end_is_an_error_then_1011(self) -> None:
        for code in (None, 1005, 1006):
            with self.subTest(code=code):
                self.upstream = FakeUpstream(end=True, end_code=code)
                with self.client.websocket_connect("/v1/realtime") as ws:
                    self._upstream_error_then_close(ws)

    def test_upstream_pump_exception_is_an_error_then_1011(self) -> None:
        self.upstream = FakeUpstream(error=RuntimeError("private service details"))
        with self.client.websocket_connect("/v1/realtime") as ws:
            event = self._upstream_error_then_close(ws)
        self.assertNotIn("private service details", event["error"]["message"])

    def test_client_pump_exception_is_an_error_then_1011(self) -> None:
        self.upstream.send = AsyncMock(side_effect=RuntimeError("send failed"))
        with self.client.websocket_connect("/v1/realtime") as ws:
            ws.send_text("x")
            self._upstream_error_then_close(ws)

    def test_frames_flow_both_ways_and_the_query_is_forwarded_without_the_key(self) -> None:
        self.upstream = FakeUpstream(frames=['{"type":"session.created"}', b"\x00\x01"])
        with self.client.websocket_connect("/v1/realtime?model=gpt-realtime&api_key=sk-secret") as ws:
            self.assertEqual(ws.receive_text(), '{"type":"session.created"}')
            self.assertEqual(ws.receive_bytes(), b"\x00\x01")
            ws.send_text('{"type":"session.update"}')
            ws.send_bytes(b"\x02")
            settle(lambda: len(self.upstream.sent) == 2)
            finish(ws, self.upstream)
        self.assertEqual(self.upstream.sent, ['{"type":"session.update"}', b"\x02"])
        self.assertEqual(self.connect.await_args.args[0], "ws://127.0.0.1:8765/v1/realtime?model=gpt-realtime")
        self.assertTrue(self.upstream.closed)
        self.assertEqual(self.upstream.close_calls, 1)
        self.assertTrue(self.upstream.iteration_ended)

    def test_openai_prefixed_path_works(self) -> None:
        with self.client.websocket_connect("/openai/v1/realtime") as ws:
            ws.send_text("x")
            settle(lambda: self.upstream.sent == ["x"])
            finish(ws, self.upstream)
        self.connect.assert_awaited_once()

    def test_realtime_subprotocol_is_echoed_and_the_key_subprotocol_is_not(self) -> None:
        with self.client.websocket_connect(
            "/v1/realtime", subprotocols=["realtime", "openai-insecure-api-key.sk-x", "openai-beta.realtime-v1"]
        ) as ws:
            self.assertEqual(ws.accepted_subprotocol, "realtime")
            ws.send_text("x")
            settle(lambda: self.upstream.sent == ["x"])
            finish(ws, self.upstream)

    def test_upstream_close_code_reaches_the_client(self) -> None:
        for code, error in (
            (1000, None),
            (1008, None),
            (1008, ConnectionClosedError(Close(1008, "policy"), Close(1008, "policy"), True)),
        ):
            with self.subTest(code=code, error=error):
                self.upstream = FakeUpstream(frames=["bye"], end_code=code, error=error)
                with self.client.websocket_connect("/v1/realtime") as ws:
                    self.assertEqual(ws.receive_text(), "bye")
                    with self.assertRaises(WebSocketDisconnect) as caught:
                        ws.receive_text()
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(self.upstream.close_calls, 1)
                self.assertTrue(self.upstream.iteration_ended)

    def test_service_is_started_on_demand(self) -> None:
        with self.client.websocket_connect("/v1/realtime") as ws:
            ws.send_text("x")
            settle(lambda: self.upstream.sent == ["x"])
            finish(ws, self.upstream)
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
        self.auth.stop()  # the real key check, with no key on the request
        self.assertEqual(self._refused(), 401)

    def test_a_placeholder_key_from_the_lan_is_a_401_before_any_upstream_connection(self) -> None:
        self.auth.stop()
        client = TestClient(proxy.app, client=("192.168.1.50", 4000))
        with self.assertRaises(Exception) as caught:
            with client.websocket_connect("/v1/realtime", subprotocols=["realtime", "openai-insecure-api-key.lan"]):
                self.fail("the upgrade was accepted")
        self.assertEqual(getattr(caught.exception, "status_code", None), 401)
        self.connect.assert_not_called()

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
                finish(ws, self.upstream)
        self.assertEqual([c.args[1] for c in enforce.call_args_list], list(proxy._REALTIME_POLICY_PATHS))

    def test_every_required_realtime_policy_is_independently_enforced(self) -> None:
        required = (
            "/v1/audio/transcriptions",
            "/v1/chat/completions",
            "/v1/audio/speech",
        )
        with patch.object(proxy, "_enforce_runtime_route_policy", return_value=None) as enforce:
            self.assertIsNone(proxy._realtime_policy_denial(access_context()))
        self.assertEqual([call.args[1] for call in enforce.call_args_list], list(required))

        for index, denied_path in enumerate(required):
            with self.subTest(denied_path=denied_path):
                self.connect.reset_mock()
                self.ensure.reset_mock()
                denied = proxy._tts_error("not allowed", 403, "policy_denied")

                def policy(_auth_context, path, **_kwargs):
                    return denied if path == denied_path else None

                with patch.object(proxy, "_enforce_runtime_route_policy", side_effect=policy) as enforce:
                    self.assertEqual(self._refused(), 403)

                self.assertEqual([call.args[1] for call in enforce.call_args_list], list(required[: index + 1]))
                self.ensure.assert_not_awaited()
                self.connect.assert_not_called()

    def test_unconfigured_service_is_a_503(self) -> None:
        cfg = {**proxy.config, "backends": {k: v for k, v in proxy.config["backends"].items() if k != "realtime"}}
        with patch.object(proxy, "config", cfg):
            self.assertEqual(self._refused(), 503)



class ColdStartTests(RealtimeRouteCase):
    """The handshake finishes before the service starts: a cold start outlasts the server's 10 s handshake limit."""

    def _error_event_then_close(self) -> dict:
        with self.client.websocket_connect("/v1/realtime") as ws:
            event = ws.receive_json()
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_text()
        self.assertEqual(caught.exception.code, 1011)
        self.assertEqual(event["type"], "error")
        self.assertEqual(event["error"]["type"], "server_error")
        self.assertIn("event_id", event)
        self.assertRegex(event["event_id"], r"^event_[0-9a-f]{24}$")
        self.assertIsInstance(event["error"]["message"], str)
        self.assertTrue(event["error"]["message"])
        self.assertIsNone(event["error"]["param"])
        self.assertIsNone(event["error"]["event_id"])
        return event["error"]

    def test_the_session_is_accepted_before_the_service_is_ready(self) -> None:
        accepted_first: list[bool] = []

        async def ensure(*_a, **_k):
            accepted_first.append(self._accepted)
            return None

        self._accepted = False
        real_accept = proxy.WebSocket.accept

        async def accept(ws, *a, **k):
            self._accepted = True
            return await real_accept(ws, *a, **k)

        self.ensure.side_effect = ensure
        with patch.object(proxy.WebSocket, "accept", accept):
            with self.client.websocket_connect("/v1/realtime") as ws:
                ws.send_text("x")
                settle(lambda: self.upstream.sent == ["x"])
                finish(ws, self.upstream)
        self.assertEqual(accepted_first, [True])

    def test_service_that_will_not_start_is_an_error_event(self) -> None:
        self.ensure.return_value = proxy._tts_error("down", 503)
        self.assertEqual(self._error_event_then_close()["code"], "backend_unavailable")
        self.connect.assert_not_called()

    def test_service_that_refuses_the_connection_is_an_error_event(self) -> None:
        self.connect.side_effect = ConnectionRefusedError()
        self.assertEqual(self._error_event_then_close()["code"], "realtime_unavailable")


class RelayCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self):
        incoming = asyncio.Queue()
        messages = []
        receiving = []

        async def receive():
            receiving.append(True)
            try:
                return await incoming.get()
            finally:
                receiving.pop()

        async def send(message):
            messages.append(message)

        client = WebSocket({"type": "websocket"}, receive, send)
        incoming.put_nowait({"type": "websocket.connect"})
        await client.accept()
        messages.clear()
        return client, incoming, messages, receiving

    async def test_cancellation_terminates_both_pumps_and_closes_upstream_once(self) -> None:
        client, _incoming, messages, receiving = await self._client()
        upstream = FakeUpstream()
        task = asyncio.create_task(realtime_proxy.relay(client, upstream))
        await asyncio.wait_for(upstream.started.wait(), timeout=3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        self.assertEqual(upstream.close_calls, 1)
        self.assertTrue(upstream.iteration_ended)
        self.assertEqual(receiving, [])
        self.assertEqual(messages, [{"type": "websocket.close", "code": 1001, "reason": ""}])

    async def test_client_disconnect_wins_over_a_simultaneous_upstream_failure(self) -> None:
        client, incoming, messages, receiving = await self._client()
        upstream = FakeUpstream(error=RuntimeError("service failed"))
        incoming.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(realtime_proxy.relay(client, upstream), timeout=3)
        self.assertEqual(upstream.close_calls, 1)
        self.assertTrue(upstream.iteration_ended)
        self.assertEqual(receiving, [])
        self.assertEqual(messages, [{"type": "websocket.close", "code": 1001, "reason": ""}])


    async def test_disconnect_during_client_send_is_not_an_upstream_error(self) -> None:
        for frame in ("hello", b"hello"):
            with self.subTest(frame=frame):
                client, _incoming, messages, receiving = await self._client()
                client.send_text = AsyncMock(side_effect=WebSocketDisconnect(1000))
                client.send_bytes = AsyncMock(side_effect=WebSocketDisconnect(1000))
                upstream = FakeUpstream(frames=[frame])
                await asyncio.wait_for(realtime_proxy.relay(client, upstream), timeout=3)
                self.assertEqual(upstream.close_calls, 1)
                self.assertEqual(receiving, [])
                self.assertEqual(messages, [{"type": "websocket.close", "code": 1001, "reason": ""}])

    async def test_error_event_send_failure_still_closes_both_sides(self) -> None:
        client, _incoming, messages, receiving = await self._client()
        client.send_text = AsyncMock(side_effect=RuntimeError("client send failed"))
        upstream = FakeUpstream(end_code=1006)
        await asyncio.wait_for(realtime_proxy.relay(client, upstream), timeout=3)
        self.assertEqual(messages, [{"type": "websocket.close", "code": 1011, "reason": ""}])
        client.send_text.assert_awaited_once()
        self.assertEqual(upstream.close_calls, 1)
        self.assertTrue(upstream.iteration_ended)
        self.assertEqual(receiving, [])


class HelperTests(unittest.TestCase):
    def test_error_event_has_a_unique_id_and_complete_error_fields(self) -> None:
        self.assertTrue(hasattr(realtime_proxy, "error_event"))
        first = realtime_proxy.error_event("backend_unavailable", "The speech service did not start.")
        second = realtime_proxy.error_event("backend_unavailable", "The speech service did not start.")
        self.assertEqual(first["type"], "error")
        self.assertRegex(first["event_id"], r"^event_[0-9a-f]{24}$")
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(first["error"], {
            "type": "server_error",
            "code": "backend_unavailable",
            "message": "The speech service did not start.",
            "param": None,
            "event_id": None,
        })

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

"""Restart drain: in-flight counters, the draining flag, the 503 shape and the admin control."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import admin_store, drain  # noqa: E402
from gateway.admin_runtime import create_admin_service_account_key  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class DrainStateTests(unittest.TestCase):
    def test_counts_and_streams(self) -> None:
        state = drain.DrainState()
        a = state.begin("POST", "/v1/chat/completions")
        b = state.begin("POST", "/v1/images/generations")
        state.mark_stream(a)
        snap = state.snapshot()
        self.assertEqual(snap["active"]["requests"], 2)
        self.assertEqual(snap["active"]["streams"], 1)
        self.assertEqual(snap["active"]["chat"], 1)
        self.assertEqual(snap["active"]["media"], 1)
        self.assertFalse(snap["idle"])
        state.end(a)
        state.end(b)
        state.end(b)  # ending twice is harmless
        self.assertTrue(state.snapshot()["idle"])

    def test_chat_quiet_window_tracking(self) -> None:
        clock = FakeClock()
        wall = FakeClock()
        wall.now = 5000.0
        state = drain.DrainState(clock=clock, wall_clock=wall)
        snap = state.snapshot()
        self.assertIsNone(snap["last_chat_activity_at"])
        self.assertIsNone(snap["seconds_since_chat_activity"])

        media = state.begin("POST", "/v1/images/generations")  # media is not chat activity
        clock.now += 5
        self.assertIsNone(state.snapshot()["seconds_since_chat_activity"])
        state.end(media)

        token = state.begin("POST", "/v1/chat/completions")
        clock.now += 40  # a long stream: still "active", never quiet
        wall.now += 40
        snap = state.snapshot()
        self.assertEqual(snap["seconds_since_chat_activity"], 0.0)
        self.assertEqual(snap["last_chat_activity_at"], 5040.0)
        state.end(token)
        clock.now += 7
        wall.now += 7
        snap = state.snapshot()
        self.assertEqual(snap["seconds_since_chat_activity"], 7.0)
        self.assertEqual(snap["last_chat_activity_at"], 5040.0)
        # The next step of the agent turn resets the window.
        state.begin("POST", "/v1/chat/completions")
        clock.now += 1
        self.assertEqual(state.snapshot()["seconds_since_chat_activity"], 0.0)

    def test_reads_do_not_count_as_chat_activity(self) -> None:
        state = drain.DrainState()
        state.end(state.begin("GET", "/v1/models"))
        self.assertIsNone(state.snapshot()["seconds_since_chat_activity"])

    def test_async_job_counters_count_as_in_flight(self) -> None:
        state = drain.DrainState()
        state.register_job_counter("video", lambda: 2)
        state.register_job_counter("broken", lambda: 1 / 0)
        snap = state.snapshot()
        self.assertEqual(snap["active"]["async_media_jobs"], {"video": 2, "broken": 0})
        self.assertEqual(snap["active"]["total"], 2)
        self.assertFalse(snap["idle"])

    def test_flag_expires_by_ttl(self) -> None:
        clock = FakeClock()
        state = drain.DrainState(clock=clock)
        state.set_draining(True, ttl_s=60, reason="restart")
        self.assertTrue(state.is_draining())
        self.assertEqual(state.snapshot()["reason"], "restart")
        clock.now += 59
        self.assertTrue(state.is_draining())
        clock.now += 2
        self.assertFalse(state.is_draining())
        self.assertIsNone(state.snapshot()["expires_in_s"])

    def test_ttl_is_clamped_and_flag_clears(self) -> None:
        clock = FakeClock()
        state = drain.DrainState(clock=clock)
        state.set_draining(True, ttl_s=10**9)
        self.assertLessEqual(state.snapshot()["expires_in_s"], drain.MAX_TTL_S)
        state.set_draining(False)
        self.assertFalse(state.is_draining())

    def test_path_rules(self) -> None:
        self.assertTrue(drain.is_exempt("/health"))
        self.assertTrue(drain.is_exempt("/admin/api/drain"))
        self.assertFalse(drain.is_exempt("/v1/chat/completions"))
        self.assertFalse(drain.is_exempt("/administrator"))
        self.assertTrue(drain.is_cancel_path("/v1/responses/abc/cancel"))
        self.assertEqual(drain.classify("/v1/chat/completions"), "chat")
        self.assertEqual(drain.classify("/v1/videos"), "media")
        self.assertEqual(drain.classify("/v1/models"), "other")


def _scope(method: str, path: str) -> dict:
    return {"type": "http", "method": method, "path": path, "headers": []}


async def _run(app, scope):
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


class InflightMiddlewareTests(unittest.TestCase):
    def test_sse_stays_counted_until_last_chunk(self) -> None:
        state = drain.DrainState()
        observed: list[dict] = []
        release = asyncio.Event()

        async def sse_app(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                }
            )
            await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
            observed.append(state.snapshot())  # headers and first chunk are out
            await release.wait()
            await send({"type": "http.response.body", "body": b"data: [DONE]\n\n"})

        async def scenario():
            app = drain.InflightMiddleware(sse_app, state)
            task = asyncio.create_task(_run(app, _scope("POST", "/v1/chat/completions")))
            await asyncio.sleep(0.01)
            mid = state.snapshot()
            release.set()
            await task
            return mid

        mid = asyncio.run(scenario())
        self.assertEqual(mid["active"]["requests"], 1)
        self.assertEqual(mid["active"]["streams"], 1)
        self.assertEqual(mid["inflight"][0]["path"], "/v1/chat/completions")
        self.assertTrue(mid["inflight"][0]["stream"])
        self.assertTrue(state.snapshot()["idle"])

    def test_counter_released_when_app_raises(self) -> None:
        state = drain.DrainState()

        async def boom(scope, receive, send):
            raise RuntimeError("backend exploded")

        async def scenario():
            app = drain.InflightMiddleware(boom, state)
            with self.assertRaises(RuntimeError):
                await _run(app, _scope("POST", "/v1/audio/speech"))

        asyncio.run(scenario())
        self.assertTrue(state.snapshot()["idle"])

    def test_exempt_paths_are_not_counted(self) -> None:
        state = drain.DrainState()
        seen = []

        async def app(scope, receive, send):
            seen.append(state.snapshot()["active"]["requests"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        asyncio.run(_run(drain.InflightMiddleware(app, state), _scope("GET", "/health")))
        self.assertEqual(seen, [0])

    def test_draining_rejects_new_work_with_openai_shaped_503(self) -> None:
        state = drain.DrainState()
        state.set_draining(True)
        reached = []

        async def app(scope, receive, send):
            reached.append(scope["path"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = drain.InflightMiddleware(app, state)
        sent = asyncio.run(_run(mw, _scope("POST", "/v1/chat/completions")))
        self.assertEqual(reached, [])
        self.assertEqual(sent[0]["status"], 503)
        headers = dict(sent[0]["headers"])
        self.assertEqual(headers[b"retry-after"], b"30")
        self.assertEqual(headers[b"content-type"], b"application/json")
        error = json.loads(sent[1]["body"])["error"]
        self.assertEqual(error["code"], "gateway_draining")
        self.assertEqual(error["type"], "server_error")
        self.assertIn("message", error)
        self.assertTrue(state.snapshot()["idle"], "a rejected request is not in flight")

    def test_draining_still_serves_reads_cancels_and_health(self) -> None:
        state = drain.DrainState()
        state.set_draining(True)
        reached = []

        async def app(scope, receive, send):
            reached.append((scope["method"], scope["path"]))
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = drain.InflightMiddleware(app, state)
        for method, path in (
            ("GET", "/v1/models"),
            ("GET", "/gateway/v1/media/jobs/job_1"),
            ("POST", "/gateway/v1/media/jobs/job_1/cancel"),
            ("GET", "/health"),
            ("POST", "/admin/api/drain"),
        ):
            sent = asyncio.run(_run(mw, _scope(method, path)))
            self.assertEqual(sent[0]["status"], 200, (method, path))
        self.assertEqual(len(reached), 5)

    def test_retry_after_env_override(self) -> None:
        state = drain.DrainState()
        state.set_draining(True)

        async def app(scope, receive, send):
            raise AssertionError("must not reach the app")

        with patch.dict(os.environ, {"AI_GATEWAY_DRAIN_RETRY_AFTER_S": "7"}):
            sent = asyncio.run(_run(drain.InflightMiddleware(app, state), _scope("POST", "/v1/completions")))
        self.assertEqual(dict(sent[0]["headers"])[b"retry-after"], b"7")


class DrainAdminRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "admin.db"
        self.old_path = proxy.ADMIN_STORE.path
        proxy.ADMIN_STORE.path = self.db
        admin_store.init_db(self.db)
        self.admin_key, _, _ = create_admin_service_account_key("drain-test", path=self.db)
        self.local = TestClient(proxy.app, client=("127.0.0.1", 50000))
        self.remote = TestClient(proxy.app, client=("192.168.1.50", 50000))
        proxy.DRAIN_STATE.set_draining(False)

    def tearDown(self) -> None:
        proxy.DRAIN_STATE.set_draining(False)
        proxy.ADMIN_STORE.path = self.old_path
        self.tmp.cleanup()

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_key}"}

    def test_requires_admin_credential(self) -> None:
        self.assertEqual(self.local.get("/admin/api/drain").status_code, 401)
        self.assertEqual(self.local.post("/admin/api/drain", json={}).status_code, 401)
        bad = {"Authorization": "Bearer not-a-key"}
        self.assertEqual(self.local.get("/admin/api/drain", headers=bad).status_code, 401)
        self.assertFalse(proxy.DRAIN_STATE.is_draining())

    def test_refuses_non_loopback_peer_even_with_admin_key(self) -> None:
        for resp in (
            self.remote.get("/admin/api/drain", headers=self._auth()),
            self.remote.post("/admin/api/drain", headers=self._auth(), json={}),
        ):
            self.assertEqual(resp.status_code, 403)
            self.assertEqual(resp.json()["error"]["code"], "drain_local_only")
        self.assertFalse(proxy.DRAIN_STATE.is_draining())

    def test_forwarded_for_header_cannot_fake_loopback(self) -> None:
        headers = {**self._auth(), "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}
        self.assertEqual(self.remote.get("/admin/api/drain", headers=headers).status_code, 403)

    def test_status_then_drain_then_undrain(self) -> None:
        status = self.local.get("/admin/api/drain", headers=self._auth())
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["draining"])
        self.assertIn("async_media_jobs", status.json()["active"])

        on = self.local.post(
            "/admin/api/drain", headers=self._auth(), json={"draining": True, "ttl_s": 120, "reason": "test"}
        )
        self.assertEqual(on.status_code, 200)
        self.assertTrue(on.json()["draining"])
        self.assertEqual(on.json()["reason"], "test")

        # health and the drain status keep working while draining
        self.assertEqual(self.local.get("/health").status_code, 200)
        self.assertTrue(self.local.get("/admin/api/drain", headers=self._auth()).json()["draining"])

        off = self.local.post("/admin/api/drain", headers=self._auth(), json={"draining": False})
        self.assertFalse(off.json()["draining"])

    def test_new_work_gets_503_while_draining(self) -> None:
        self.local.post("/admin/api/drain", headers=self._auth(), json={})
        resp = self.local.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers["retry-after"], "30")
        self.assertEqual(resp.json()["error"]["code"], "gateway_draining")

    def test_rejects_bad_bodies(self) -> None:
        for body in ({"draining": "yes"}, {"ttl_s": -1}, {"ttl_s": True}, {"reason": 5}):
            resp = self.local.post("/admin/api/drain", headers=self._auth(), json=body)
            self.assertEqual(resp.status_code, 400, body)
        resp = self.local.post(
            "/admin/api/drain", headers={**self._auth(), "content-type": "application/json"}, content=b"[1]"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(proxy.DRAIN_STATE.is_draining())

    @patch.object(proxy, "_current_loaded_llm_model_ids", AsyncMock(return_value=set()))
    def test_real_app_counts_in_flight_requests(self) -> None:
        seen = []
        original = proxy.DRAIN_STATE.begin

        def spy(method, path):
            token = original(method, path)
            seen.append(proxy.DRAIN_STATE.snapshot()["active"]["requests"])
            return token

        with patch.object(proxy.DRAIN_STATE, "begin", side_effect=spy):
            self.local.get("/v1/models")
        self.assertEqual(seen, [1])
        self.assertEqual(proxy.DRAIN_STATE.snapshot()["active"]["requests"], 0)


if __name__ == "__main__":
    unittest.main()

"""Tests for GET /v1/videos (list), DELETE /v1/videos/{id}, and the
POST /v1/videos/{id}/remix stub.

These drive the proxy_comfy_video_* handlers directly (not through
FastAPI's TestClient) with a fake httpx.AsyncClient, mirroring the style
used elsewhere in this file's sibling video tests -- no real ComfyUI.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


class _StubListRequest:
    def __init__(self, query: dict[str, str] | None = None) -> None:
        self.query_params = query or {}


class _StubHistoryResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self) -> Any:
        return self._payload


class _FakeVideoAsyncClient:
    """Fake httpx.AsyncClient for _comfy_history/_comfy_queue_status/delete.

    `history` maps prompt_id -> history entry (a job absent from this dict
    means "not in ComfyUI history yet", matching real ComfyUI behavior for a
    still-queued/running job). `queue` is the raw /queue response shape.
    `posts` records every POST call for assertions.
    """

    def __init__(self, history: dict[str, dict] | None = None, queue: dict | None = None) -> None:
        self.history = history or {}
        self.queue = queue or {"queue_running": [], "queue_pending": []}
        self.posts: list[tuple[str, dict]] = []

    async def __aenter__(self) -> "_FakeVideoAsyncClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def get(self, url: str, **_kwargs: Any) -> _StubHistoryResponse:
        if url.endswith("/queue"):
            return _StubHistoryResponse(200, self.queue)
        # url is f"{COMFY_HOST}/history/{prompt_id}"
        prompt_id = url.rsplit("/", 1)[-1]
        if prompt_id in self.history:
            return _StubHistoryResponse(200, {prompt_id: self.history[prompt_id]})
        return _StubHistoryResponse(200, {})

    async def post(self, url: str, **kwargs: Any) -> _StubHistoryResponse:
        self.posts.append((url, kwargs.get("json") or {}))
        return _StubHistoryResponse(200, {})


def _succeeded_history_entry() -> dict:
    return {"status": {"completed": True}, "outputs": {}}


def _running_history_entry() -> dict:
    # Present in history but not yet completed -> _job_status_from_history
    # treats this as "running".
    return {"status": {"completed": False}, "outputs": {}}


class VideoRegistryTestCase(unittest.IsolatedAsyncioTestCase):
    """Base class that snapshots/restores the module-level video registries
    (_video_job_records/_video_progress/_video_workflows are real mutable
    globals shared across the whole test process)."""

    def setUp(self) -> None:
        self._orig_records = dict(proxy._video_job_records)
        self._orig_progress = dict(proxy._video_progress)
        self._orig_workflows = dict(proxy._video_workflows)
        proxy._video_job_records.clear()
        proxy._video_progress.clear()
        proxy._video_workflows.clear()
        self.addCleanup(self._restore)
        self.enabled_patch = patch.object(proxy, "COMFY_ENABLED", True)
        self.enabled_patch.start()
        self.addCleanup(self.enabled_patch.stop)
        self.service_patch = patch.object(
            proxy, "_service_availability_response", lambda *_a, **_k: None
        )
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.headers_patch = patch.object(
            proxy, "configured_downstream_headers", lambda _ac, _b: {}
        )
        self.headers_patch.start()
        self.addCleanup(self.headers_patch.stop)

    def _restore(self) -> None:
        proxy._video_job_records.clear()
        proxy._video_job_records.update(self._orig_records)
        proxy._video_progress.clear()
        proxy._video_progress.update(self._orig_progress)
        proxy._video_workflows.clear()
        proxy._video_workflows.update(self._orig_workflows)

    def _seed(self, prompt_id: str, **fields: Any) -> None:
        record = {
            "model": "video-ltx-2.3-distilled",
            "size": "1280x720",
            "seconds": "5",
            "actual_seconds": 5.0,
            "created_at": 1000,
            "completed_at": None,
        }
        record.update(fields)
        proxy._video_job_records[prompt_id] = record


class TestVideosList(VideoRegistryTestCase):
    async def test_empty_list(self) -> None:
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: _FakeVideoAsyncClient()):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
            )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["object"], "list")
        self.assertEqual(body["data"], [])
        self.assertIsNone(body["first_id"])
        self.assertIsNone(body["last_id"])
        self.assertFalse(body["has_more"])

    async def test_default_order_is_newest_first(self) -> None:
        self._seed("p1", created_at=1000)
        self._seed("p2", created_at=2000)
        client = _FakeVideoAsyncClient(history={
            "p1": _succeeded_history_entry(),
            "p2": _succeeded_history_entry(),
        })
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
            )
        body = json.loads(response.body)
        ids = [item["id"] for item in body["data"]]
        self.assertEqual(ids, [proxy._video_id_from_prompt_id("p2"), proxy._video_id_from_prompt_id("p1")])

    async def test_order_asc(self) -> None:
        self._seed("p1", created_at=1000)
        self._seed("p2", created_at=2000)
        client = _FakeVideoAsyncClient(history={
            "p1": _succeeded_history_entry(),
            "p2": _succeeded_history_entry(),
        })
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest({"order": "asc"}), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
            )
        body = json.loads(response.body)
        ids = [item["id"] for item in body["data"]]
        self.assertEqual(ids, [proxy._video_id_from_prompt_id("p1"), proxy._video_id_from_prompt_id("p2")])

    async def test_limit_and_has_more(self) -> None:
        self._seed("p1", created_at=1000)
        self._seed("p2", created_at=2000)
        self._seed("p3", created_at=3000)
        client = _FakeVideoAsyncClient(history={
            "p1": _succeeded_history_entry(),
            "p2": _succeeded_history_entry(),
            "p3": _succeeded_history_entry(),
        })
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest({"limit": "2"}), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
            )
        body = json.loads(response.body)
        self.assertEqual(len(body["data"]), 2)
        self.assertTrue(body["has_more"])

    async def test_after_cursor_excludes_up_to_and_including(self) -> None:
        self._seed("p1", created_at=1000)
        self._seed("p2", created_at=2000)
        self._seed("p3", created_at=3000)
        client = _FakeVideoAsyncClient(history={
            "p1": _succeeded_history_entry(),
            "p2": _succeeded_history_entry(),
            "p3": _succeeded_history_entry(),
        })
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest({"after": proxy._video_id_from_prompt_id("p3")}),
                proxy.NO_AUTH_CONTEXT,  # type: ignore[arg-type]
            )
        body = json.loads(response.body)
        ids = [item["id"] for item in body["data"]]
        self.assertEqual(ids, [proxy._video_id_from_prompt_id("p2"), proxy._video_id_from_prompt_id("p1")])

    async def test_bad_limit_rejected(self) -> None:
        response = await proxy.proxy_comfy_video_list(
            _StubListRequest({"limit": "0"}), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "limit")

    async def test_bad_order_rejected(self) -> None:
        response = await proxy.proxy_comfy_video_list(
            _StubListRequest({"order": "sideways"}), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "order")

    async def test_bad_after_cursor_rejected(self) -> None:
        response = await proxy.proxy_comfy_video_list(
            _StubListRequest({"after": "not-a-video-id"}), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "after")

    async def test_entry_shape_includes_required_fields(self) -> None:
        self._seed("p1", created_at=1000, model="video-wan-2.2-ti2v-5b-q8", size="1280x704", seconds="3")
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
            )
        entry = json.loads(response.body)["data"][0]
        self.assertEqual(entry["object"], "video")
        self.assertEqual(entry["model"], "video-wan-2.2-ti2v-5b-q8")
        self.assertEqual(entry["size"], "1280x704")
        self.assertEqual(entry["seconds"], "3")
        self.assertEqual(entry["status"], "succeeded")
        self.assertEqual(entry["created_at"], 1000)
        self.assertIsNotNone(entry["completed_at"])

    async def test_failed_entry_includes_error(self) -> None:
        self._seed("p1", created_at=1000)
        with patch.object(
            proxy, "_comfy_execution_error_message", lambda _job: "CUDA out of memory"
        ):
            client = _FakeVideoAsyncClient(history={"p1": {"status": {"completed": True}, "outputs": {}}})
            # Force failed by patching _job_status_from_history for this test.
            with patch.object(
                proxy, "_job_status_from_history", lambda _job: ("failed", 100)
            ), patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
                response = await proxy.proxy_comfy_video_list(
                    _StubListRequest(), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
                )
        entry = json.loads(response.body)["data"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["error"], "CUDA out of memory")

    async def test_running_entry_not_yet_in_history_uses_queue(self) -> None:
        self._seed("p1", created_at=1000)
        client = _FakeVideoAsyncClient(queue={"queue_running": [[0, "p1"]], "queue_pending": []})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
            )
        entry = json.loads(response.body)["data"][0]
        self.assertEqual(entry["status"], "running")


class TestVideosDelete(VideoRegistryTestCase):
    async def test_unknown_id_is_404(self) -> None:
        response = await proxy.proxy_comfy_video_delete(
            "video_does-not-exist", proxy.NO_AUTH_CONTEXT
        )
        self.assertEqual(response.status_code, 404)

    async def test_invalid_id_is_400(self) -> None:
        response = await proxy.proxy_comfy_video_delete(
            "not-a-video-id", proxy.NO_AUTH_CONTEXT
        )
        self.assertEqual(response.status_code, 400)

    async def test_known_id_deletes_and_clears_tracking(self) -> None:
        self._seed("p1", created_at=1000)
        proxy._video_progress["p1"] = {"status": "queued", "updated_at": 1000}
        proxy._video_workflows["p1"] = {"6": {"inputs": {}}}
        client = _FakeVideoAsyncClient()
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_delete(
                proxy._video_id_from_prompt_id("p1"), proxy.NO_AUTH_CONTEXT
            )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["object"], "video")
        self.assertTrue(body["deleted"])
        self.assertNotIn("p1", proxy._video_job_records)
        self.assertNotIn("p1", proxy._video_progress)
        self.assertNotIn("p1", proxy._video_workflows)
        # Best-effort ComfyUI history delete was attempted.
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.posts[0][1], {"delete": ["p1"]})

    async def test_known_but_not_yet_in_history_still_deletable(self) -> None:
        """A still-queued job (no /history entry yet) is still a known,
        deletable id -- our own tracking is the source of truth, not
        ComfyUI's /history."""
        proxy._video_progress["p1"] = {"status": "queued", "updated_at": 1000}
        client = _FakeVideoAsyncClient()
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_delete(
                proxy._video_id_from_prompt_id("p1"), proxy.NO_AUTH_CONTEXT
            )
        self.assertEqual(response.status_code, 200)


class TestVideosRemix(VideoRegistryTestCase):
    async def test_unknown_id_is_404(self) -> None:
        response = await proxy.proxy_comfy_video_remix(
            "video_does-not-exist", proxy.NO_AUTH_CONTEXT
        )
        self.assertEqual(response.status_code, 404)

    async def test_unconfigured_returns_stable_501(self) -> None:
        self._seed("p1")
        with patch.object(proxy, "VIDEO_REMIX_WORKFLOW", None):
            response = await proxy.proxy_comfy_video_remix(
                proxy._video_id_from_prompt_id("p1"), proxy.NO_AUTH_CONTEXT
            )
        self.assertEqual(response.status_code, 501)
        self.assertEqual(
            json.loads(response.body)["error"]["code"], "video_remix_not_configured"
        )

    async def test_configured_but_unimplemented_returns_different_stable_code(self) -> None:
        self._seed("p1")
        with patch.object(proxy, "VIDEO_REMIX_WORKFLOW", "future-remix-workflow"):
            response = await proxy.proxy_comfy_video_remix(
                proxy._video_id_from_prompt_id("p1"), proxy.NO_AUTH_CONTEXT
            )
        self.assertEqual(response.status_code, 501)
        self.assertEqual(
            json.loads(response.body)["error"]["code"], "video_remix_not_implemented"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

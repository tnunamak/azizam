"""Tests for GET /v1/videos (list), DELETE /v1/videos/{id}, and the
POST /v1/videos/{id}/remix guard rails.

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
            "owner_key_hash": None,
            "deleted_at": None,
        }
        record.update(fields)
        proxy._video_job_records[prompt_id] = record


ALICE = proxy.AuthContext(identity=proxy.AuthIdentity(name="alice", key_hash="sha256:alice"))
BOB = proxy.AuthContext(identity=proxy.AuthIdentity(name="bob", key_hash="sha256:bob"))


class TestVideosStatusAndContent(VideoRegistryTestCase):
    async def test_status_maps_internal_running_to_openai_in_progress(self) -> None:
        self._seed(
            "p1",
            created_at=1000,
            model="sora-2",
            size="1280x720",
            seconds="5",
        )
        proxy._video_progress["p1"] = {
            "status": "running",
            "percent": 73,
            "step": 7,
            "total": 10,
            "node": "12",
            "phase": "sampling",
            "updated_at": 1100,
        }
        client = _FakeVideoAsyncClient()

        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_status(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["object"], "video")
        self.assertEqual(body["azizam_object"], "video.generation")
        self.assertEqual(body["model"], "sora-2")
        self.assertEqual(body["status"], "in_progress")
        self.assertEqual(body["azizam_status"], "running")
        self.assertEqual(body["progress"], 73)
        self.assertEqual(body["created_at"], 1000)
        self.assertEqual(body["updated_at"], 1100)
        self.assertEqual(body["size"], "1280x720")
        self.assertEqual(body["seconds"], "5")

    async def test_status_maps_history_success_to_openai_completed(self) -> None:
        self._seed("p1", created_at=1000, model="sora-2")
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})

        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_status(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["object"], "video")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["azizam_status"], "succeeded")
        self.assertEqual(body["progress"], 100)
        self.assertEqual(body["model"], "sora-2")
        self.assertIsNotNone(body["completed_at"])

    async def test_status_progress_is_monotonic_when_history_lags_live_progress(self) -> None:
        self._seed("p1", created_at=1000)
        proxy._video_progress["p1"] = {
            "status": "running",
            "percent": 80,
            "step": 8,
            "total": 10,
            "updated_at": 1100,
        }
        client = _FakeVideoAsyncClient(history={"p1": _running_history_entry()})

        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_status(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )

        body = json.loads(response.body)
        self.assertEqual(body["status"], "in_progress")
        self.assertEqual(body["progress"], 80)

    async def test_content_for_known_unfinished_job_returns_not_ready(self) -> None:
        self._seed("p1", created_at=1000)
        proxy._video_progress["p1"] = {"status": "queued", "updated_at": 1000}
        client = _FakeVideoAsyncClient(queue={"queue_running": [], "queue_pending": [[0, "p1"]]})

        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_content(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )

        self.assertEqual(response.status_code, 404)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["code"], "video_not_ready")
        self.assertIn("queued", body["error"]["message"])


class TestVideosList(VideoRegistryTestCase):
    async def test_empty_list(self) -> None:
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: _FakeVideoAsyncClient()):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), access_context()  # type: ignore[arg-type]
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
                _StubListRequest(), access_context()  # type: ignore[arg-type]
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
                _StubListRequest({"order": "asc"}), access_context()  # type: ignore[arg-type]
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
                _StubListRequest({"limit": "2"}), access_context()  # type: ignore[arg-type]
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
                access_context(),  # type: ignore[arg-type]
            )
        body = json.loads(response.body)
        ids = [item["id"] for item in body["data"]]
        self.assertEqual(ids, [proxy._video_id_from_prompt_id("p2"), proxy._video_id_from_prompt_id("p1")])

    async def test_bad_limit_rejected(self) -> None:
        response = await proxy.proxy_comfy_video_list(
            _StubListRequest({"limit": "0"}), access_context()  # type: ignore[arg-type]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "limit")

    async def test_bad_order_rejected(self) -> None:
        response = await proxy.proxy_comfy_video_list(
            _StubListRequest({"order": "sideways"}), access_context()  # type: ignore[arg-type]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "order")

    async def test_bad_after_cursor_rejected(self) -> None:
        response = await proxy.proxy_comfy_video_list(
            _StubListRequest({"after": "not-a-video-id"}), access_context()  # type: ignore[arg-type]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "after")

    async def test_entry_shape_includes_required_fields(self) -> None:
        self._seed("p1", created_at=1000, model="video-wan-2.2-ti2v-5b-q8", size="1280x704", seconds="3")
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), access_context()  # type: ignore[arg-type]
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
                    _StubListRequest(), access_context()  # type: ignore[arg-type]
                )
        entry = json.loads(response.body)["data"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["error"], "CUDA out of memory")

    async def test_running_entry_not_yet_in_history_uses_queue(self) -> None:
        self._seed("p1", created_at=1000)
        client = _FakeVideoAsyncClient(queue={"queue_running": [[0, "p1"]], "queue_pending": []})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), access_context()  # type: ignore[arg-type]
            )
        entry = json.loads(response.body)["data"][0]
        self.assertEqual(entry["status"], "running")

    async def test_evicted_after_cursor_errors_instead_of_restarting(self) -> None:
        """A cursor that no longer resolves (evicted/deleted/unknown) must
        not silently restart pagination from page one -- that looks like
        forward progress to a client while actually re-serving page one
        forever."""
        self._seed("p1", created_at=1000)
        self._seed("p2", created_at=2000)
        client = _FakeVideoAsyncClient(history={
            "p1": _succeeded_history_entry(),
            "p2": _succeeded_history_entry(),
        })
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest({"after": proxy._video_id_from_prompt_id("never-existed")}),
                access_context(),  # type: ignore[arg-type]
            )
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["code"], "cursor_not_found")
        self.assertEqual(body["error"]["param"], "after")

    async def test_deleted_job_excluded_from_list(self) -> None:
        self._seed("p1", created_at=1000)
        self._seed("p2", created_at=2000, deleted_at=1500)
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), access_context()  # type: ignore[arg-type]
            )
        ids = [item["id"] for item in json.loads(response.body)["data"]]
        self.assertEqual(ids, [proxy._video_id_from_prompt_id("p1")])

    async def test_list_scoped_to_owner(self) -> None:
        self._seed("p1", created_at=1000, owner_key_hash="sha256:alice")
        self._seed("p2", created_at=2000, owner_key_hash="sha256:bob")
        client = _FakeVideoAsyncClient(history={
            "p1": _succeeded_history_entry(),
            "p2": _succeeded_history_entry(),
        })
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            alice_response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), ALICE  # type: ignore[arg-type]
            )
            bob_response = await proxy.proxy_comfy_video_list(
                _StubListRequest(), BOB  # type: ignore[arg-type]
            )
        alice_ids = [item["id"] for item in json.loads(alice_response.body)["data"]]
        bob_ids = [item["id"] for item in json.loads(bob_response.body)["data"]]
        self.assertEqual(alice_ids, [proxy._video_id_from_prompt_id("p1")])
        self.assertEqual(bob_ids, [proxy._video_id_from_prompt_id("p2")])


class TestVideosDelete(VideoRegistryTestCase):
    async def test_unknown_id_is_404(self) -> None:
        response = await proxy.proxy_comfy_video_delete(
            "video_does-not-exist", access_context()
        )
        self.assertEqual(response.status_code, 404)

    async def test_invalid_id_is_400(self) -> None:
        response = await proxy.proxy_comfy_video_delete(
            "not-a-video-id", access_context()
        )
        self.assertEqual(response.status_code, 400)

    async def test_known_terminal_job_deletes_without_cancelling(self) -> None:
        """A job that's already succeeded has nothing to cancel -- only the
        best-effort ComfyUI history delete should fire, and the local record
        is tombstoned (kept, deleted_at set), not popped outright."""
        self._seed("p1", created_at=1000)
        proxy._video_progress["p1"] = {"status": "succeeded", "updated_at": 1000}
        proxy._video_workflows["p1"] = {"6": {"inputs": {}}}
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_delete(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["object"], "video")
        self.assertTrue(body["deleted"])
        # Tombstoned, not popped: the job record stays (so the GC/retention
        # story is coherent) but is no longer visible.
        self.assertIn("p1", proxy._video_job_records)
        self.assertIsNotNone(proxy._video_job_records["p1"]["deleted_at"])
        self.assertFalse(proxy._video_job_visible("p1", access_context()))
        self.assertNotIn("p1", proxy._video_progress)
        self.assertNotIn("p1", proxy._video_workflows)
        # No cancel call for an already-terminal job -- only history delete.
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.posts[0][0], f"{proxy.COMFY_HOST}/history")
        self.assertEqual(client.posts[0][1], {"delete": ["p1"]})

    async def test_known_but_not_yet_in_history_still_deletable(self) -> None:
        """A still-queued job (no /history entry yet) is still a known,
        deletable id -- our own tracking is the source of truth, not
        ComfyUI's /history."""
        proxy._video_progress["p1"] = {"status": "queued", "updated_at": 1000}
        client = _FakeVideoAsyncClient()
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_delete(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )
        self.assertEqual(response.status_code, 200)

    async def test_queued_job_is_cancelled_before_tombstoning(self) -> None:
        """Deleting a queued/running job must actively cancel it, not just
        stop tracking it locally while ComfyUI keeps rendering."""
        self._seed("p1", created_at=1000)
        proxy._video_progress["p1"] = {"status": "queued", "updated_at": 1000}
        client = _FakeVideoAsyncClient()
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_delete(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["deleted"])
        self.assertEqual(len(client.posts), 2)
        self.assertEqual(client.posts[0][0], f"{proxy.COMFY_HOST}/api/jobs/p1/cancel")
        self.assertEqual(client.posts[1][0], f"{proxy.COMFY_HOST}/history")

    async def test_cancel_failure_returns_409_not_deleted(self) -> None:
        """If ComfyUI can't confirm the cancel, we must not claim
        `deleted: true` for a job that may keep rendering to completion."""
        self._seed("p1", created_at=1000)
        proxy._video_progress["p1"] = {"status": "running", "updated_at": 1000}

        class _FailingCancelClient(_FakeVideoAsyncClient):
            async def post(self, url: str, **kwargs: Any) -> Any:
                if url.endswith("/cancel"):
                    raise ConnectionError("comfyui unreachable")
                return await super().post(url, **kwargs)

        client = _FailingCancelClient()
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_delete(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            json.loads(response.body)["error"]["code"], "video_cancel_failed"
        )
        # Not tombstoned -- the job is presumed still running.
        self.assertIsNone(proxy._video_job_records["p1"]["deleted_at"])

    async def test_cancel_reports_false_still_tombstones(self) -> None:
        """ComfyUI reporting {"cancelled": false} (the job finished in the
        race between our status check and the cancel call) is not a
        failure -- it's terminal now, so we proceed to tombstone."""
        self._seed("p1", created_at=1000)
        proxy._video_progress["p1"] = {"status": "running", "updated_at": 1000}

        class _NoopCancelClient(_FakeVideoAsyncClient):
            async def post(self, url: str, **kwargs: Any) -> Any:
                if url.endswith("/cancel"):
                    self.posts.append((url, {}))
                    return _StubHistoryResponse(200, {"cancelled": False})
                return await super().post(url, **kwargs)

        client = _NoopCancelClient()
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_delete(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(proxy._video_job_records["p1"]["deleted_at"])

    async def test_deleted_job_no_longer_visible_to_status_or_content(self) -> None:
        """The tombstone, not just dropping live-progress tracking, is what
        actually makes GET return 404 and content stop being served."""
        self._seed("p1", created_at=1000)
        proxy._video_progress["p1"] = {"status": "succeeded", "updated_at": 1000}
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            await proxy.proxy_comfy_video_delete(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )
            status_response = await proxy.proxy_comfy_video_status(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )
            content_response = await proxy.proxy_comfy_video_content(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )
        self.assertEqual(status_response.status_code, 404)
        self.assertEqual(content_response.status_code, 404)

    async def test_owner_cannot_be_deleted_by_a_different_credential(self) -> None:
        self._seed("p1", created_at=1000, owner_key_hash="sha256:alice")
        proxy._video_progress["p1"] = {"status": "succeeded", "updated_at": 1000}
        response = await proxy.proxy_comfy_video_delete(
            proxy._video_id_from_prompt_id("p1"), BOB
        )
        self.assertEqual(response.status_code, 404)
        # Not actually deleted -- Bob's 404 must not have side effects.
        self.assertIsNone(proxy._video_job_records["p1"]["deleted_at"])

    async def test_owner_can_delete_own_job(self) -> None:
        self._seed("p1", created_at=1000, owner_key_hash="sha256:alice")
        proxy._video_progress["p1"] = {"status": "succeeded", "updated_at": 1000}
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_delete(
                proxy._video_id_from_prompt_id("p1"), ALICE
            )
        self.assertEqual(response.status_code, 200)


class TestVideosOwnership(VideoRegistryTestCase):
    """Status/content must be scoped to the owning credential too -- not
    just list/delete (a video-enabled credential B must not be able to poll
    or download credential A's job, even knowing its id)."""

    async def test_status_hidden_from_non_owner(self) -> None:
        self._seed("p1", created_at=1000, owner_key_hash="sha256:alice")
        response = await proxy.proxy_comfy_video_status(
            proxy._video_id_from_prompt_id("p1"), BOB
        )
        self.assertEqual(response.status_code, 404)

    async def test_status_visible_to_owner(self) -> None:
        self._seed("p1", created_at=1000, owner_key_hash="sha256:alice")
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_status(
                proxy._video_id_from_prompt_id("p1"), ALICE
            )
        self.assertEqual(response.status_code, 200)

    async def test_content_hidden_from_non_owner(self) -> None:
        self._seed("p1", created_at=1000, owner_key_hash="sha256:alice")
        response = await proxy.proxy_comfy_video_content(
            proxy._video_id_from_prompt_id("p1"), BOB
        )
        self.assertEqual(response.status_code, 404)

    async def test_untracked_id_is_404_for_an_authenticated_key(self) -> None:
        """An id with no record (never tracked, or past its retention) has no
        owner. ComfyUI's /history is shared across keys, so falling through to
        it let any key read it. Authenticated callers get 404; auth-disabled
        deployments (nothing is owned) keep the history fallback."""
        client = _FakeVideoAsyncClient(history={"p1": _succeeded_history_entry()})
        with patch.object(proxy.httpx, "AsyncClient", lambda **_k: client):
            response = await proxy.proxy_comfy_video_status(
                proxy._video_id_from_prompt_id("p1"), BOB
            )
            open_response = await proxy.proxy_comfy_video_status(
                proxy._video_id_from_prompt_id("p1"), access_context()
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(open_response.status_code, 200)


class _JsonRemixRequest:
    headers = {"content-type": "application/json"}

    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body


class TestVideosRemix(VideoRegistryTestCase):
    """Guard rails around POST /v1/videos/{id}/remix. The full create path
    (graph binding, source upload) is covered in test_video_remix_replace."""

    async def test_unknown_id_is_404(self) -> None:
        from _fake_comfy_video import FakeComfy

        comfy = FakeComfy()
        with patch.object(proxy.httpx, "AsyncClient", comfy.client_class()):
            response = await proxy.proxy_comfy_video_remix(
                "video_does-not-exist",
                access_context(),
                _JsonRemixRequest({"prompt": "x"}),
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(comfy.submitted, [])

    async def test_unconfigured_returns_stable_501(self) -> None:
        self._seed("p1")
        with patch.object(proxy, "VIDEO_REMIX_WORKFLOW", None):
            response = await proxy.proxy_comfy_video_remix(
                proxy._video_id_from_prompt_id("p1"),
                access_context(),
                _JsonRemixRequest({"prompt": "x"}),
            )
        self.assertEqual(response.status_code, 501)
        self.assertEqual(
            json.loads(response.body)["error"]["code"], "video_remix_not_configured"
        )

    async def test_non_owner_gets_404_not_remix_error(self) -> None:
        self._seed("p1", owner_key_hash="sha256:alice")
        response = await proxy.proxy_comfy_video_remix(
            proxy._video_id_from_prompt_id("p1"),
            BOB,
            _JsonRemixRequest({"prompt": "x"}),
        )
        self.assertEqual(response.status_code, 404)

    async def test_shipped_config_enables_remix_with_a_matching_alias(self) -> None:
        self.assertEqual(proxy.VIDEO_REMIX_WORKFLOW, "video-bernini-r-remix")
        self.assertEqual(
            proxy._resolve_video_workflow("video-remix")[0], proxy.VIDEO_REMIX_WORKFLOW
        )


class TestVideoJobRecordGC(VideoRegistryTestCase):
    """_video_job_records must have its own retention, independent of
    _video_progress's much-shorter terminal-entry sweep -- that mismatch was
    the actual root cause of list/delete "forgetting" a job 5 minutes after
    completion while status/content (reading ComfyUI's own, separately
    retained /history) kept serving it."""

    def test_completed_job_outlives_the_old_five_minute_terminal_window(self) -> None:
        self._seed("p1", created_at=1000, completed_at=1000)
        # 10 minutes later -- well past the old 5-minute terminal retention,
        # well within the 6-hour full retention this now uses instead.
        now = 1000 + 600
        proxy._gc_video_job_records_locked(now)
        self.assertIn("p1", proxy._video_job_records)

    def test_completed_job_survives_indefinitely(self) -> None:
        self._seed("p1", created_at=1000, completed_at=1000)
        now = 1000 + 100 * 365 * 86400
        proxy._gc_video_job_records_locked(now)
        self.assertIn("p1", proxy._video_job_records)

    def test_tombstone_is_retained_briefly_then_reaped(self) -> None:
        self._seed("p1", created_at=1000, completed_at=1000, deleted_at=1000)
        proxy._gc_video_job_records_locked(1000 + 60)
        self.assertIn("p1", proxy._video_job_records)
        proxy._gc_video_job_records_locked(1000 + proxy._VIDEO_JOB_RECORD_DELETED_RETAIN + 1)
        self.assertNotIn("p1", proxy._video_job_records)

    def test_terminal_progress_gc_no_longer_touches_job_records(self) -> None:
        """Regression guard for the actual bug: the 5-minute terminal-entry
        sweep (_gc_video_progress_terminal_locked) must not reap
        _video_job_records at all anymore -- only the independent,
        longer-lived _gc_video_job_records_locked does."""
        self._seed("p1", created_at=1000, completed_at=1000)
        proxy._video_progress["p1"] = {
            "status": "succeeded",
            "ended_at": 1000,
            "updated_at": 1000,
        }
        proxy._gc_video_progress_terminal_locked(1000 + 600)  # 10 min later
        self.assertNotIn("p1", proxy._video_progress)  # the short sweep did its own job
        self.assertIn("p1", proxy._video_job_records)  # but left the record alone


if __name__ == "__main__":
    unittest.main(verbosity=2)

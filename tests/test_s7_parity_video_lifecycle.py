"""S7 video lifecycle parity: HTTP callers, persisted rows, assets and engine wire.

Items video-59..92 and video-121..128 from docs/design/c2d-s7-parity/video.md.
The gateway is real; only the engine's HTTP boundary is fake. No handler,
progress dictionary, scheduling function or result mapper is replaced here.
The tests intentionally pin today's public behaviour, including list/status
vocabulary differences and the fact that DELETE keeps the Library file.

Not claimed by this file (not skips or expected failures):
* video-60: the allowed HTTP rig has no live engine progress-event source;
  completed/history transitions do not prove live sampling progress is monotonic.
* video-82: a Python exception-class catch boundary is not a public wire contract.
* video-92, video-126, video-127: the rig does not restart the process or expose a
  public retention-clock fixture. Clearing private dictionaries/calling hydration
  or GC helpers would not be a black-box restart/retention test.
* video-124: the save/change/no-inline-prompt portion is tested, not startup
  hydration or scrubbing an old inline prompt on load.
* video-91: retained Library bytes are tested; maker prompt-row deletion needs a
  populated prompt-memory fixture (the supplied rig does not retain prompts).
None of these gaps is covered by a section-4 allowed delta, so none is xfailed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import proxy
from tests.media_test_bytes import mp4_bytes
from tests.support.access import authenticated, everything, identity
from tests.support.adapter_differential_rig import GatewayRig, STATS, history_done
from tests.support.fake_upstream import HandlerTransport

HOST = "video-lifecycle.fk.test"
MODEL = "ltx-2.5-t2v"
PLAN = everything(media_metadata="none", receipt_detail="full")
ALICE = identity(PLAN, name="alice-video", app_id="app_video_alice", key_hash="sha256:alice-video")
BOB = identity(PLAN, name="bob-video", app_id="app_video_bob", key_hash="sha256:bob-video")


class Engine:
    """Mutable upstream state, driven exclusively through HandlerTransport."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        self.clip = mp4_bytes()
        self.cancel_error = False
        self.cancelled = True
        self.history_mode: str | None = None
        self.view_error = False
        self.view_started: asyncio.Event | None = None
        self.view_release: asyncio.Event | None = None
        self.before_cancel = None

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == "POST" and path == "/prompt":
            pid = f"p{len(self.states) + 1}"
            self.states[pid] = "queued"
            return httpx.Response(200, json={"prompt_id": pid})
        if method == "GET" and path == "/system_stats":
            return httpx.Response(200, json=STATS)
        if method == "GET" and path.startswith("/history/"):
            pid = path.rsplit("/", 1)[-1]
            if self.history_mode == "malformed":
                return httpx.Response(200, content=b"{broken-json", headers={"content-type": "application/json"})
            if self.history_mode == "wrong_shape":
                return httpx.Response(200, json=["not a history object"])
            if self.history_mode == "unavailable":
                raise httpx.ConnectError("test engine unavailable", request=request)
            state = self.states.get(pid)
            if state == "completed":
                return httpx.Response(200, json=history_done("images", "clip.mp4", prompt_id=pid, node="60"))
            if state == "running":
                return httpx.Response(200, json={pid: {"status": {"status_str": "running", "completed": False,
                                                                  "messages": []}, "outputs": {}}})
            if state == "failed":
                return httpx.Response(200, json={pid: {"status": {"status_str": "error", "completed": False,
                    "messages": [["execution_error", {"node_type": "LTXVSampler", "node_id": "12",
                                                        "exception_message": "oom"}]]}, "outputs": {}}})
            return httpx.Response(200, json={})
        if method == "GET" and path == "/queue":
            if self.history_mode == "unavailable":
                raise httpx.ConnectError("test engine unavailable", request=request)
            return httpx.Response(200, json={
                "queue_running": [[0, pid, {}, {}, []] for pid, state in self.states.items() if state == "running"],
                "queue_pending": [[0, pid, {}, {}, []] for pid, state in self.states.items() if state == "queued"],
            })
        if method == "GET" and path == "/view":
            if self.view_started is not None:
                self.view_started.set()
                assert self.view_release is not None
                await self.view_release.wait()
            if self.view_error:
                return httpx.Response(404, content=b"missing output")
            return httpx.Response(200, content=self.clip, headers={"content-type": "video/mp4"})
        if method == "POST" and path.endswith("/cancel"):
            if self.before_cancel is not None:
                self.before_cancel()
            if self.cancel_error:
                raise httpx.ConnectError("test cancel unreachable", request=request)
            return httpx.Response(200, json={"cancelled": self.cancelled})
        if method == "POST" and path in {"/history", "/free", "/interrupt"}:
            return httpx.Response(200, json={})
        self.unexpected.append(f"{method} {path}")
        raise AssertionError(f"Unexpected engine call: {method} {path}")

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


@dataclass
class World:
    rig: GatewayRig
    engine: Engine
    now: float = 1_800_000_000.0

    async def call(self, method: str, path: str, *, owner=ALICE, **kwargs):
        with authenticated(owner):
            return await self.rig.call(method, path, **kwargs)

    async def create(self, *, owner=ALICE, prompt="lifecycle parity") -> str:
        response = await self.call("POST", "/v1/videos", owner=owner,
                                   json_body={"prompt": prompt, "model": MODEL})
        assert response.status_code == 200, response.text
        video_id = response.json()["id"]
        assert isinstance(video_id, str) and video_id.startswith("video_")
        return video_id

    def state(self, video_id: str, state: str) -> None:
        self.engine.states[video_id.removeprefix("video_")] = state

    async def status(self, video_id: str, *, owner=ALICE):
        return await self.call("GET", f"/v1/videos/{video_id}", owner=owner)

    async def content(self, video_id: str, *, owner=ALICE, **kwargs):
        return await self.call("GET", f"/v1/videos/{video_id}/content", owner=owner, **kwargs)

    async def delete(self, video_id: str, *, owner=ALICE):
        return await self.call("DELETE", f"/v1/videos/{video_id}", owner=owner)

    async def listing(self, *, owner=ALICE, **params):
        return await self.call("GET", "/v1/videos", owner=owner, params=params)

    def record(self, video_id: str) -> dict[str, Any]:
        # The schema and row are themselves inventory items, not a private helper.
        with sqlite3.connect(self.rig.media_jobs_db) as db:
            row = db.execute("SELECT record_json FROM video_job_records WHERE prompt_id = ?",
                             (video_id.removeprefix("video_"),)).fetchone()
        assert row is not None, f"No durable record for {video_id}"
        return json.loads(row[0])

    def video_files(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in self.rig.cache.glob("*.mp4")}


def run(tmp_path: Path, exercise) -> None:
    with GatewayRig(exercise.__name__, tmp_path) as rig:
        engine = Engine()
        rig.fakes.router.add_transport(HOST, HandlerTransport(engine.handle))
        rig.set(proxy, "COMFY_HOST", f"http://{HOST}")
        rig.monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), "comfy": {
            "enabled": True, "host": HOST, "port": 80, "protocol": "node-graph"}})
        world = World(rig, engine)
        rig.monkeypatch.setattr(time, "time", lambda: world.now)
        asyncio.run(exercise(world))
        assert engine.unexpected == [], engine.unexpected


def test_video_59_running_and_success_use_openai_status_words(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "running")
        running = await w.status(vid)
        assert running.status_code == 200
        assert running.json()["status"] == "in_progress"
        assert running.json()["azizam_status"] == "running"
        w.state(vid, "completed")
        done = await w.status(vid)
        assert done.status_code == 200
        assert done.json()["status"] == "completed"
        assert done.json()["azizam_status"] == "succeeded"
    run(tmp_path, exercise)


def test_video_61_terminal_error_is_served_while_history_answers(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "failed")
        first = await w.status(vid)
        assert first.status_code == 200
        assert first.json()["status"] == "failed"
        assert first.json()["error"]["code"] == "video_generation_failed"
        again_while_answering = await w.status(vid)
        assert again_while_answering.json() == first.json()
        # Code truth (proxy.py proxy_graph_video_status): a terminal entry in the live progress table is used only
        # for queued/running; the terminal answer comes from history. With history and /queue both unreachable,
        # the route answers 404. Inventory row video-61 says the terminal error "wins over fallback"; that holds
        # only while history answers.
        w.engine.history_mode = "unavailable"
        again = await w.status(vid)
        assert again.status_code == 404
        assert again.json()["error"]["message"] == "video_id not found"
    run(tmp_path, exercise)


@pytest.mark.skip(reason="orphan marking is written only by the background _reconcile_video_progress loop "
                         "(proxy.py:10753, started in the app lifespan every 15 s). The HTTP rig does not run the "
                         "lifespan, and GET /v1/videos/{id} answers a created id from the live progress table "
                         "(200 queued) without asking /queue, so no public request reaches the orphan branch. "
                         "The branch is pinned at unit level by test_video_handler_coverage_gaps.py::"
                         "test_reconcile_distinguishes_unavailable_active_terminal_and_orphan.")
def test_video_62_absent_engine_job_is_orphaned(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "absent")
        response = await w.status(vid)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"]["message"] == "orphaned: ComfyUI does not report this prompt_id"
        assert w.engine.calls("GET", "/queue")
    run(tmp_path, exercise)


@pytest.mark.parametrize("mode", ["malformed", "wrong_shape"])
def test_video_63_bad_history_does_not_orphan_live_job(tmp_path, mode):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "running")
        first = await w.status(vid)
        assert first.status_code == 200 and first.json()["status"] == "in_progress"
        w.engine.history_mode = mode
        response = await w.status(vid)
        assert response.status_code == 200
        assert response.json()["status"] == "in_progress"
        assert response.json()["error"] is None
        assert response.json()["progress"] >= first.json()["progress"]
    run(tmp_path, exercise)


def test_video_64_status_is_hidden_from_other_owner(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        before = len(w.engine.requests)
        other = await w.status(vid, owner=BOB)
        assert other.status_code == 404
        assert len(w.engine.requests) == before
        own = await w.status(vid)
        assert own.status_code == 200 and own.json()["status"] == "completed"
    run(tmp_path, exercise)


@pytest.mark.parametrize("video_id,status", [("not-a-video", 400), ("video_", 400), ("video_untracked", 404)])
def test_video_65_invalid_and_untracked_status_ids_are_refused(tmp_path, video_id, status):
    async def exercise(w):
        # Even a real engine job is not visible without an owned gateway record.
        w.engine.states["untracked"] = "completed"
        response = await w.status(video_id)
        assert response.status_code == status
        if status == 400:
            assert response.json()["error"]["message"] == "invalid video_id"
        assert not w.engine.requests
    run(tmp_path, exercise)


def test_video_66_deleted_video_is_hidden_from_status_and_content(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        assert (await w.delete(vid)).status_code == 200
        assert (await w.status(vid)).status_code == 404
        assert (await w.content(vid)).status_code == 404
    run(tmp_path, exercise)


def test_video_67_completed_status_has_terminal_fields(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        response = await w.status(vid)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == vid and body["object"] == "video"
        assert body["status"] == "completed" and body["azizam_status"] == "succeeded"
        assert body["progress"] == 100 and body["completed_at"] is not None
    run(tmp_path, exercise)


def test_video_68_running_status_exposes_progress(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "running")
        response = await w.status(vid)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "in_progress" and body["azizam_status"] == "running"
        assert body["progress"] == 50  # the recorded history-running wire value
        assert body["completed_at"] is None
    run(tmp_path, exercise)


def test_video_69_failed_status_carries_sanitized_error(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "failed")
        response = await w.status(vid)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed" and body["azizam_status"] == "failed"
        assert body["error"] == {"code": "video_generation_failed",
                                 "message": "ComfyUI LTXVConcatAVLatent node 12: execution failed"}
        assert "oom" not in response.text
    run(tmp_path, exercise)


def test_video_70_unfinished_content_is_not_ready(tmp_path):
    async def exercise(w):
        vid = await w.create()
        response = await w.content(vid)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "video_not_ready"
        assert "queued" in response.json()["error"]["message"]
        assert not w.engine.calls("GET", "/view")
        assert w.video_files() == {}
    run(tmp_path, exercise)


def test_video_71_content_is_hidden_from_other_owner(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        before = len(w.engine.requests)
        response = await w.content(vid, owner=BOB)
        assert response.status_code == 404 and len(w.engine.requests) == before
        assert w.video_files() == {}
    run(tmp_path, exercise)


def test_video_72_cancel_during_content_fetch_keeps_no_asset(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        w.engine.view_started, w.engine.view_release = asyncio.Event(), asyncio.Event()
        response = await w.content(vid, cancel_when=w.engine.view_started.wait)
        assert response is None
        w.engine.view_release.set()
        assert w.video_files() == {}
        assert not w.record(vid).get("asset_id")
        assert not list(w.rig.cache.glob("*.meta.json"))
    run(tmp_path, exercise)


def test_video_73_first_content_fetch_indexes_library_asset(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        assert w.video_files() == {}
        response = await w.content(vid)
        assert response.status_code == 200
        files = w.video_files()
        assert len(files) == 1
        stored = next(iter(files.values()))
        # The cached copy is not the engine's bytes: the caller's plan (media_metadata none) strips the encoder tag.
        assert b"Lavf" in w.engine.clip and b"Lavf" not in stored
        assert stored == response.content
        record = w.record(vid)
        assert record["asset_id"].startswith("vid_")
        metadata = [json.loads(p.read_text()) for p in w.rig.cache.glob("*.meta.json")]
        assert any(row.get("asset_id") == record["asset_id"] for row in metadata)
        with sqlite3.connect(w.rig.media_jobs_db) as db:
            jobs = db.execute("SELECT intent, job_json FROM media_jobs WHERE request_id = ?", (vid,)).fetchall()
        assert any(intent == "video.generate" for intent, _ in jobs)
    run(tmp_path, exercise)


def test_video_74_content_serves_the_stored_mp4_with_delivery_headers(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        response = await w.content(vid)
        assert response.status_code == 200
        # Served bytes are an mp4 remux of the engine clip with its encoder tag removed (media_metadata none).
        assert response.content[4:8] == b"ftyp" and b"Lavf" not in response.content
        assert response.content != w.engine.clip
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["content-disposition"] == 'inline; filename="clip.mp4"'
        assert int(response.headers["content-length"]) == len(response.content)
        views = w.engine.calls("GET", "/view")
        assert len(views) == 1
        # The gateway sends subfolder only when the output has one (proxy.py _load_completed_video_bytes; the
        # content route builds its params the same way); this output has none.
        assert dict(views[0].url.params) == {"filename": "clip.mp4", "type": "output"}
    run(tmp_path, exercise)


def test_video_75_missing_output_is_a_video_asset_error(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        w.engine.view_error = True
        response = await w.content(vid)
        assert response.status_code >= 400
        body = response.json()
        assert isinstance(body.get("error"), dict)
        assert isinstance(body["error"].get("message"), str) and body["error"]["message"]
        assert w.video_files() == {}
        assert not w.record(vid).get("asset_id")
        assert w.engine.calls("GET", "/view")
    run(tmp_path, exercise)


@pytest.mark.parametrize("params,param", [({"limit": "0"}, "limit"), ({"limit": "many"}, "limit"),
    ({"order": "sideways"}, "order"), ({"after": "not-a-video"}, "after")])
def test_video_76_bad_list_parameters_are_400(tmp_path, params, param):
    async def exercise(w):
        response = await w.listing(**params)
        assert response.status_code == 400
        assert response.json()["error"]["param"] == param
        assert not w.engine.requests
    run(tmp_path, exercise)


async def three_videos(w):
    ids = []
    for _ in range(3):
        vid = await w.create()
        w.state(vid, "completed")
        ids.append(vid)
        w.now += 10
    return ids


def test_video_77_list_order_and_has_more(tmp_path):
    async def exercise(w):
        ids = await three_videos(w)
        newest = await w.listing(limit="2")
        assert newest.status_code == 200
        assert [x["id"] for x in newest.json()["data"]] == ids[::-1][:2]
        assert newest.json()["has_more"] is True
        assert newest.json()["first_id"] == ids[-1] and newest.json()["last_id"] == ids[1]
        oldest = await w.listing(order="asc", limit="3")
        assert oldest.status_code == 200
        assert [x["id"] for x in oldest.json()["data"]] == ids
        assert oldest.json()["has_more"] is False
    run(tmp_path, exercise)


def test_video_78_after_excludes_cursor_and_earlier_page(tmp_path):
    async def exercise(w):
        ids = await three_videos(w)
        response = await w.listing(after=ids[-1])
        assert response.status_code == 200
        assert [x["id"] for x in response.json()["data"]] == ids[-2::-1]
        ascending = await w.listing(order="asc", after=ids[0])
        assert ascending.status_code == 200
        assert [x["id"] for x in ascending.json()["data"]] == ids[1:]
    run(tmp_path, exercise)


def test_video_79_missing_cursor_does_not_restart_pagination(tmp_path):
    async def exercise(w):
        await three_videos(w)
        response = await w.listing(after="video_evicted")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "cursor_not_found"
        assert response.json()["error"]["param"] == "after"
    run(tmp_path, exercise)


def test_video_80_list_entry_fields_error_and_queue_fallback(tmp_path):
    async def exercise(w):
        done, running, failed = await three_videos(w)
        w.state(running, "queued")  # no history: list must use the queue
        w.state(failed, "failed")
        response = await w.listing()
        assert response.status_code == 200
        entries = {x["id"]: x for x in response.json()["data"]}
        assert set(entries) == {done, running, failed}
        entry = entries[done]
        assert entry["object"] == "video" and entry["model"] == MODEL
        assert entry["size"] == "960x512" and entry["seconds"] == "5"
        assert entry["status"] == "succeeded" and entry["completed_at"] is not None
        assert entry["created_at"] == 1_800_000_000
        assert entries[running]["status"] == "queued"
        assert entries[failed]["status"] == "failed"
        assert entries[failed]["error"]
        # Code truth (proxy.py proxy_graph_video_list): a tracked job still in the live progress table answers from
        # it; /queue is asked only for a job with no live entry, which an in-process create never leaves. Inventory
        # row video-80 says "running entries use the queue"; that is true only without a live entry.
        assert not w.engine.calls("GET", "/queue")
    run(tmp_path, exercise)


def test_video_81_list_is_owner_scoped_and_excludes_deleted(tmp_path):
    async def exercise(w):
        alice = await w.create()
        bob = await w.create(owner=BOB)
        deleted = await w.create()
        for vid in (alice, bob, deleted):
            w.state(vid, "completed")
        assert (await w.delete(deleted)).status_code == 200
        own = await w.listing()
        other = await w.listing(owner=BOB)
        assert own.status_code == other.status_code == 200
        assert [x["id"] for x in own.json()["data"]] == [alice]
        assert [x["id"] for x in other.json()["data"]] == [bob]
    run(tmp_path, exercise)


def test_video_83_terminal_delete_tombstones_without_cancel(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        assert (await w.status(vid)).json()["status"] == "completed"
        response = await w.delete(vid)
        assert response.status_code == 200
        assert w.record(vid)["deleted_at"] is not None
        assert not [r for r in w.engine.requests if r.url.path.endswith("/cancel")]
        deletes = w.engine.calls("POST", "/history")
        assert len(deletes) == 1 and json.loads(deletes[0].content) == {"delete": [vid[6:]]}
    run(tmp_path, exercise)


def test_video_84_known_job_without_history_is_deletable(tmp_path):
    async def exercise(w):
        vid = await w.create()
        response = await w.delete(vid)
        assert response.status_code == 200 and response.json()["deleted"] is True
        assert w.record(vid)["deleted_at"] is not None
    run(tmp_path, exercise)


def test_video_85_cancel_precedes_durable_tombstone(tmp_path):
    async def exercise(w):
        vid = await w.create()
        observed = []
        w.engine.before_cancel = lambda: observed.append(w.record(vid).get("deleted_at"))
        response = await w.delete(vid)
        assert response.status_code == 200
        assert observed == [None]
        assert w.record(vid)["deleted_at"] is not None
        posts = [r.url.path for r in w.engine.requests if r.method == "POST"]
        assert posts.index(f"/api/jobs/{vid[6:]}/cancel") < posts.index("/history")
    run(tmp_path, exercise)


def test_video_86_failed_cancel_is_409_and_not_deleted(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.engine.cancel_error = True
        response = await w.delete(vid)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "video_cancel_failed"
        assert w.record(vid).get("deleted_at") is None
        assert not w.engine.calls("POST", "/history")
        assert (await w.status(vid)).status_code == 200
    run(tmp_path, exercise)


def test_video_87_cancel_false_still_tombstones(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.engine.cancelled = False
        response = await w.delete(vid)
        assert response.status_code == 200 and response.json()["deleted"] is True
        assert w.engine.calls("POST", f"/api/jobs/{vid[6:]}/cancel")
        assert w.record(vid)["deleted_at"] is not None
    run(tmp_path, exercise)


def test_video_88_other_owner_cannot_delete(tmp_path):
    async def exercise(w):
        vid = await w.create()
        before = len(w.engine.requests)
        denied = await w.delete(vid, owner=BOB)
        assert denied.status_code == 404 and len(w.engine.requests) == before
        assert w.record(vid).get("deleted_at") is None
        own = await w.delete(vid)
        assert own.status_code == 200 and w.record(vid)["deleted_at"] is not None
    run(tmp_path, exercise)


@pytest.mark.parametrize("video_id,status", [("other_id", 400), ("video_", 400), ("video_missing", 404)])
def test_video_89_delete_rejects_invalid_and_unknown_ids(tmp_path, video_id, status):
    async def exercise(w):
        response = await w.delete(video_id)
        assert response.status_code == status
        if status == 400:
            assert response.json()["error"]["message"] == "invalid video_id"
        assert not w.engine.requests
    run(tmp_path, exercise)


def test_video_90_delete_success_body_is_exact(tmp_path):
    async def exercise(w):
        vid = await w.create()
        response = await w.delete(vid)
        assert response.status_code == 200
        assert response.json() == {"id": vid, "object": "video", "deleted": True}
    run(tmp_path, exercise)


def test_video_91_delete_keeps_library_file_bytes(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.state(vid, "completed")
        assert (await w.content(vid)).status_code == 200
        before = w.video_files()
        assert len(before) == 1 and b"Lavf" not in next(iter(before.values()))
        assert (await w.delete(vid)).status_code == 200
        assert w.video_files() == before  # writers-54: prompt removal is not file deletion
        assert (await w.content(vid)).status_code == 404
    run(tmp_path, exercise)


def test_video_121_id_is_video_prefix_and_engine_id(tmp_path):
    async def exercise(w):
        vid = await w.create()
        assert vid == "video_p1"
        w.state(vid, "completed")
        response = await w.status(vid)
        assert response.status_code == 200 and response.json()["id"] == vid
        assert w.engine.calls("GET", "/history/p1")
        assert not w.engine.calls("GET", "/history/video_p1")
    run(tmp_path, exercise)


@pytest.mark.parametrize("video_id", ["p1", "video_", "image_p1"])
def test_video_122_malformed_prefix_is_invalid_video_id(tmp_path, video_id):
    async def exercise(w):
        response = await w.status(video_id)
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "invalid video_id"
        assert not w.engine.requests
    run(tmp_path, exercise)


def test_video_123_owner_identity_is_stored_without_raw_credential(tmp_path):
    async def exercise(w):
        raw = "parity-only-synthetic-credential"
        hashed = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        principal = identity(PLAN, name="hashed-video", app_id=None, key_hash=hashed)
        vid = await w.create(owner=principal)
        record = w.record(vid)
        assert record["owner_key_hash"] == hashed
        assert raw not in json.dumps(record)
        assert (await w.status(vid, owner=BOB)).status_code == 404
        assert (await w.status(vid, owner=principal)).status_code == 200
    run(tmp_path, exercise)


def test_video_124_http_changes_persist_without_inline_prompt(tmp_path):
    async def exercise(w):
        secret = "parity prompt that must not be inline in the video record"
        vid = await w.create(prompt=secret)
        created = w.record(vid)
        assert "prompt" not in created and secret not in json.dumps(created)
        assert created.get("deleted_at") is None
        w.state(vid, "completed")
        assert (await w.status(vid)).status_code == 200
        completed = w.record(vid)
        assert completed["completed_at"] is not None
        assert (await w.delete(vid)).status_code == 200
        deleted = w.record(vid)
        assert deleted["deleted_at"] is not None
        assert deleted["owner_key_hash"] == created["owner_key_hash"]
        assert "prompt" not in deleted and secret not in json.dumps(deleted)
    run(tmp_path, exercise)


def test_video_125_video_record_migration_is_recorded_once(tmp_path):
    async def exercise(w):
        await w.create()
        await w.create()
        with sqlite3.connect(w.rig.media_jobs_db) as db:
            rows = db.execute("SELECT name FROM media_jobs_migrations WHERE name = ?",
                              ("video_job_records_v1",)).fetchall()
        assert rows == [("video_job_records_v1",)]
    run(tmp_path, exercise)


@pytest.mark.parametrize("value", ["image_p1", "p1", "video_"])
def test_video_128_list_cursor_and_delete_require_video_id(tmp_path, value):
    async def exercise(w):
        response = await w.listing(after=value)
        assert response.status_code == 400 and response.json()["error"]["param"] == "after"
        deleted = await w.delete(value)
        assert deleted.status_code == 400 and deleted.json()["error"]["message"] == "invalid video_id"
        assert not w.engine.requests
    run(tmp_path, exercise)

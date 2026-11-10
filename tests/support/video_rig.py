"""The video parity rig: a fake ComfyUI, a ``World`` over the real app, and ``drive`` for job-backed videos.

A video made by ``POST /v1/videos`` is a run-core job. Its status moves only when the job's runner polls the engine, so a test
cannot flip the engine and read the status again (the old route read the engine on every GET). ``drive`` flips the engine and
lets the runner take its normal poll steps until the job store shows the matching state. It never writes the job store.

Ids are read from responses (``video_mj_<hex>`` for a job, ``video_<engine prompt id>`` for an old record). A test that
needs the engine prompt id asks ``engine_prompt_of``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

import proxy
from gateway import media_jobs, workflow_run
from gateway.workflow_runtime_types import RunJobView
from tests.media_test_bytes import mp4_bytes
from tests.support.access import authenticated, everything, identity
from tests.support.adapter_differential_rig import STATS, GatewayRig, history_done
from tests.support.fake_upstream import HandlerTransport

EngineState = Literal["queued", "running", "completed", "failed", "missing"]
HOST = "video-lifecycle.fk.test"
MODEL = "ltx-2.5-t2v"
PLAN = everything(media_metadata="none", receipt_detail="full")
ALICE = identity(PLAN, name="alice-video", app_id="app_video_alice", key_hash="sha256:alice-video")
BOB = identity(PLAN, name="bob-video", app_id="app_video_bob", key_hash="sha256:bob-video")
#: The runner's grace before it calls a prompt the engine does not report orphaned (design 3.6); one test-clock step per poll.
ORPHAN_GRACE_S = 15.0
JOB_PREFIX = "video_mj_"
# The engine-side word each rig state sets. ``absent`` is a prompt the engine knows in neither /history nor /queue.
_ENGINE_WORD = {"queued": "queued", "running": "running", "completed": "completed", "failed": "failed", "missing": "absent"}
# The job-store words each rig state may show.
_JOB_WORDS = {"queued": {"queued", "running"}, "running": {"running"}, "completed": {"succeeded"},
              "failed": {"failed"}, "missing": {"failed"}}

CANCEL_WAIT_S = 0.3        # the row's cancel_wait_s, cut for the rig


class Engine:
    """Mutable upstream state, driven exclusively through HandlerTransport."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.prompt_ids: list[str] = []        # the POST /prompt replies, in order
        self.requests: list[httpx.Request] = []
        self.unexpected: list[str] = []
        self.clip = mp4_bytes()
        self.cancel_error = False
        self.cancelled = True
        self.history_mode: str | None = None
        self.bad_history: list[str] = []         # the history answers the engine sent in a malformed or wrong-shape mode
        self.view_error = False
        self.view_started: asyncio.Event | None = None
        self.view_release: asyncio.Event | None = None
        self.before_cancel = None
        self.prompt_gate: asyncio.Event | None = None   # while set up, the engine takes a prompt and never answers it
        self.outputs: dict[str, Any] | None = None   # the outputs a finished prompt's history holds (default: one clip at node 60)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == "POST" and path == "/prompt":
            if self.prompt_gate is not None:
                await self.prompt_gate.wait()
            pid = f"p{len(self.states) + 1}"
            self.states[pid] = "queued"
            self.prompt_ids.append(pid)
            return httpx.Response(200, json={"prompt_id": pid})
        if method == "GET" and path == "/system_stats":
            return httpx.Response(200, json=STATS)
        if method == "GET" and path.startswith("/history/"):
            pid = path.rsplit("/", 1)[-1]
            if self.history_mode in {"malformed", "wrong_shape"}:
                self.bad_history.append(self.history_mode)
            if self.history_mode == "malformed":
                return httpx.Response(200, content=b"{broken-json", headers={"content-type": "application/json"})
            if self.history_mode == "wrong_shape":
                return httpx.Response(200, json=["not a history object"])
            if self.history_mode == "unavailable":
                raise httpx.ConnectError("test engine unavailable", request=request)
            state = self.states.get(pid)
            if state == "completed" and self.outputs is not None:
                return httpx.Response(200, json={pid: {"status": {"status_str": "success", "completed": True, "messages": []},
                                                       "outputs": self.outputs}})
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
    created: list[str] = field(default_factory=list)       # wire ids of job-backed videos, in creation order
    owners: dict[str, Any] = field(default_factory=dict)   # wire id -> the identity that made it

    # -- the public job service, as the caller sees it ------------------------------------------------------
    @property
    def jobs(self) -> workflow_run.WorkflowJobService:
        return proxy._workflow_job_service()

    def caller(self, video_id: str, owner=None) -> Any:
        """The caller a public read of ``video_id`` is made as: ``owner``, else the identity that made the video."""
        return proxy.AuthContext(identity=owner or self.owners.get(video_id, ALICE))

    # -- HTTP -----------------------------------------------------------------------------------------------
    async def call(self, method: str, path: str, *, owner=ALICE, **kwargs):
        with authenticated(owner):
            return await self.rig.call(method, path, **kwargs)

    async def create(self, *, owner=ALICE, prompt="lifecycle parity", **body) -> str:
        """Create one video and return the id the response carries. The test clock moves one second, so two videos
        never share a creation time."""
        response = await self.call("POST", "/v1/videos", owner=owner,
                                   json_body={"prompt": prompt, "model": MODEL, **body})
        assert response.status_code == 200, response.text
        video_id = self.track(response.json()["id"], owner=owner)
        # A create answers once the job is queued; the runner submits its prompt a moment later. Wait for that submission, so
        # the engine's prompts are in creation order (``engine_prompt_of`` maps by order).
        deadline = time.monotonic() + 5.0
        while len(self.engine.prompt_ids) < len(self.created):
            assert time.monotonic() < deadline, f"the runner of {video_id} never submitted its prompt"
            await asyncio.sleep(0.002)
        return video_id

    def track(self, video_id: str, *, owner=ALICE) -> str:
        """Remember a job-backed video from a response, for ``engine_prompt_of``. Old-record ids pass through."""
        assert isinstance(video_id, str) and video_id.startswith("video_"), video_id
        if video_id.startswith(JOB_PREFIX):
            self.created.append(video_id)
            self.owners[video_id] = owner
        self.now += 1.0
        return video_id

    async def status(self, video_id: str, *, owner=ALICE):
        return await self.call("GET", f"/v1/videos/{video_id}", owner=owner)

    async def content(self, video_id: str, *, owner=ALICE, **kwargs):
        return await self.call("GET", f"/v1/videos/{video_id}/content", owner=owner, **kwargs)

    async def delete(self, video_id: str, *, owner=ALICE):
        return await self.call("DELETE", f"/v1/videos/{video_id}", owner=owner)

    async def listing(self, *, owner=ALICE, **params):
        return await self.call("GET", "/v1/videos", owner=owner, params=params)

    # -- public job surface (rule 2 of the rewrite) ----------------------------------------------------------
    def view(self, video_id: str, *, owner=None) -> RunJobView:
        view = self.jobs.get(job_id_of(video_id), self.caller(video_id, owner))
        assert view is not None, f"no job for {video_id}"
        return view

    def deleted(self, video_id: str, *, owner=None) -> bool:
        """True once the job is tombstoned: a deleted job is gone from ``JobService.get`` for its owner."""
        return self.jobs.get(job_id_of(video_id), self.caller(video_id, owner)) is None

    def asset_id(self, video_id: str, *, owner=None) -> str | None:
        """The Library asset id of the job's one result, or None while it has no result."""
        results = self.view(video_id, owner=owner).results or ()
        return results[0].asset_id if len(results) == 1 else None

    def facts(self, video_id: str, *, owner=None):
        """The request facts the maker's reader sees (``JobService.request_facts``, design 1.2)."""
        facts = self.jobs.request_facts(job_id_of(video_id), self.caller(video_id, owner))
        assert facts is not None, f"no facts for {video_id}"
        return facts

    # -- legacy ids only: the old reader asks the engine on every GET ----------------------------------------
    def state(self, video_id: str, state: str) -> None:
        assert not video_id.startswith(JOB_PREFIX), f"{video_id} is a job: use drive()"
        self.engine.states[video_id.removeprefix("video_")] = state

    def video_files(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in self.rig.cache.glob("*.mp4")}

    async def sleep(self, delay: float = 0, result=None):
        """The runner's injected sleep. The test clock does not move: a runner that polls an engine the test has not driven yet
        must not run out its wait in no real time. ``drive`` moves the clock itself where a state needs it (``missing``)."""
        await asyncio.sleep(0.002)
        return result


def job_id_of(video_id: str) -> str:
    assert video_id.startswith(JOB_PREFIX), f"{video_id} is not a job-backed video id"
    return video_id.removeprefix("video_")


def engine_prompt_of(world: World, video_id: str) -> str:
    """The engine prompt id the job submitted: the engine's recorded POST /prompt reply, matched to the job by the order
    in which the world created its videos. The one place that knows the mapping. An old record's id is its prompt id."""
    if not video_id.startswith(JOB_PREFIX):
        return video_id.removeprefix("video_")
    assert video_id in world.created, f"{video_id} was not created through World.create/track"
    index = world.created.index(video_id)
    assert index < len(world.engine.prompt_ids), (
        f"the engine has {len(world.engine.prompt_ids)} prompts; the runner of {video_id} (creation #{index}) has not submitted")
    return world.engine.prompt_ids[index]


async def _submitted(world: World, video_id: str, deadline: float) -> str:
    index = world.created.index(video_id) if video_id in world.created else -1
    assert index >= 0, f"{video_id} was not created through World.create/track"
    while len(world.engine.prompt_ids) <= index:
        assert time.monotonic() < deadline, f"the runner of {video_id} never submitted its prompt"
        await asyncio.sleep(0.002)
    return engine_prompt_of(world, video_id)


async def drive(world: World, video_id: str, state: EngineState, *, wait: bool = True, timeout_s: float = 5.0) -> RunJobView:
    """Set the fake engine's state for the job behind ``video_id`` and run the job's runner poll until the job store shows
    the matching state; return that job's view. ``drive`` never writes the job store: only the engine changes, and the
    runner writes what it would write in production. ``missing`` advances the test clock one grace period per step.
    ``wait=False`` sets the state and lets the runner take a few steps. Raises AssertionError with the last view on timeout."""
    deadline = time.monotonic() + timeout_s
    job_id = job_id_of(video_id)
    prompt_id = await _submitted(world, video_id, deadline)
    world.engine.states[prompt_id] = _ENGINE_WORD[state]
    if not wait:
        for _ in range(8):
            await asyncio.sleep(0)
        return world.view(video_id)
    while True:
        view = world.view(video_id)
        if view.status in _JOB_WORDS[state]:
            return view
        assert not media_jobs.is_terminal(view.status), f"{job_id} ended as {view.status} while driving to {state}: {view}"
        assert time.monotonic() < deadline, f"{job_id} did not reach {state} in {timeout_s}s; last view: {view}"
        if state == "missing":
            world.now += ORPHAN_GRACE_S
        await asyncio.sleep(0.005)


async def settled(world: World, video_id: str, *, timeout_s: float = 5.0) -> RunJobView:
    """Wait (bounded) until the job behind ``video_id`` is terminal and return its view. Use it after
    ``drive(..., wait=False)`` for an engine state whose outcome is not the state's own (a /view the engine refuses)."""
    deadline = time.monotonic() + timeout_s
    while True:
        view = world.view(video_id)
        if media_jobs.is_terminal(view.status):
            return view
        assert time.monotonic() < deadline, f"{video_id} did not settle in {timeout_s}s; last view: {view}"
        await asyncio.sleep(0.005)


def install_runner_clock(world: World) -> None:
    """Build the run core over the world's test clock and sleep, so a runner step costs no real time."""
    core = workflow_run.WorkflowRunCore(
        snapshots=proxy.WORKFLOW_SNAPSHOTS, projections=proxy.WORKFLOW_PROJECTIONS, port=proxy._WORKFLOW_RUN_PORT,
        spool=proxy._workflow_job_spool(), path=media_jobs.DB_PATH, sleep=world.sleep, clock=lambda: world.now)
    world.rig.monkeypatch.setattr(proxy, "_workflow_core_cache", (proxy.WORKFLOW_SNAPSHOTS, proxy.WORKFLOW_PROJECTIONS, core))


def disable_service(rig) -> None:
    """Switch the video service off as an operator does: the service record says so, and the flag derived from it follows."""
    record = dict(proxy.config["backends"]["comfy"], enabled=False)
    rig.monkeypatch.setitem(proxy.config, "backends", {**proxy.config["backends"], "comfy": record})
    rig.set(proxy, "COMFY_ENABLED", False)


def run(tmp_path: Path, exercise) -> None:
    with GatewayRig(exercise.__name__, tmp_path) as rig:
        engine = Engine()
        rig.fakes.router.add_transport(HOST, HandlerTransport(engine.handle))
        rig.set(proxy, "COMFY_HOST", f"http://{HOST}")
        rig.monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), "comfy": {
            "enabled": True, "host": HOST, "port": 80, "protocol": "node-graph"}})
        # The rig's sleeps cost no time, so a runner polling an engine the test has not driven yet would spend its whole wait in
        # no real time. The wait is made so long that only a state the test sets ends a run.
        rig.monkeypatch.setitem(proxy.runtime().derived, "COMFY_TIMEOUT", 10 ** 9)
        # A delete waits for the run to stop for the row's ``cancel_wait_s``; a run the engine cannot cancel never stops, so
        # the wait is cut to a moment and the 409 comes at once.
        row = proxy._video_row()
        rig.monkeypatch.setattr(proxy, "_video_row", lambda: dataclasses.replace(row, cancel_wait_s=CANCEL_WAIT_S))
        # The run core holds one run at a time (``images.media_jobs.concurrency``, 1); the old video route handed every request to
        # the engine at once. The parity files keep several videos in flight, so the rig opens the slots (reported as a
        # difference of the move, not asserted away here).
        rig.monkeypatch.setattr(proxy, "_workflow_run_semaphore", asyncio.Semaphore(16))
        world = World(rig, engine)
        rig.monkeypatch.setattr(time, "time", lambda: world.now)
        rig.monkeypatch.setattr(proxy, "_WORKFLOW_JOB_SPOOL", None)
        install_runner_clock(world)
        asyncio.run(exercise(world))
        assert engine.unexpected == [], engine.unexpected


def video_job_rows(rig: GatewayRig) -> list[dict[str, Any]]:
    """Every video job the Library and History read, through the media-jobs listing API (not SQL): the video's one job
    and any delivery job a content fetch added. A second row for one video is a duplicate delivery (design 5.4)."""
    return [job for job in media_jobs.recent(1000, include_active=True, path=rig.media_jobs_db)
            if job.get("intent") == "video.generate"]

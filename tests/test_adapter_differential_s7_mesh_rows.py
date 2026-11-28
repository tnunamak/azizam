"""S7 acceptance goldens for the 3D rows: ``POST /v1/3d/generations`` and ``POST /v1/3d/generations/glb``.

Each cell runs the real app, the real run core and worker, the real job store and the real multipart adapter. Only the
engine HTTP is fake (``tests.support.mesh_harness.Harness``: the observer, the health read and ``/generate``). One cell is
one outcome of the S7 matrix; its record is in ``fixtures/s7_mesh_rows/cells.json``. To re-record,
run with ``--junit-xml-out`` and ``-o junit_logging=all`` (every run prints each cell after the ``S7-MESH-ROWS-RECORD``
marker).

Oracle for the engine side: ``fixtures/adapter_differential/s7_mr_cells.json`` holds the bytes today's
``POST /v1/meshes/generate`` sent to the engine. ``test_the_engine_request_equals_the_old_route_request`` requires the
same bytes from the new rows for the same input, so the engine saw no change.

Cell map (new cell -> the ``mr`` cell it replaces) and every difference from the old route. The old route was one
handler with its own lease, receipts and wire. The raw row is its closest wire (the answer is the GLB); the LocalAI row
answers JSON that carries the model. In every cell the engine saw the same request, parts and bytes, with its form fields in alphabetical order
(the oracle test below). Differences
that hold for every cell: (a) the old ``x-request-id`` and ``x-asset-id`` headers and the route's receipt rows are gone
(the run core keeps a job row and a Library asset; no per-request receipt); (b) a failed run's message is the run core's
generic text ("The job failed."), the status and code carry the reason (old: a sentence per case); the one exception is the
capacity refusal ``resource_unavailable``, which says "The graphics card is busy with other work. Try again soon."; (c) the job is a
workflow job (``intent: workflow.run``, ``workflow_id`` names the workflow), not a mesh job with its own mode.

* ``success_raw`` -> ``success``: status 200, ``model/gltf-binary`` and the exact model bytes, as before.
  ``success_sync`` is the LocalAI row (new wire; no ``mr`` cell): 200 JSON ``{created, data: [{b64_json, media_type}]}``.
* ``async_job`` -> the 202 part of ``admission_refusal`` and ``success``: ``Prefer: respond-async`` answers 202 with the
  run core's job object (``poll_url``, ``content_url``, ``status: queued``) and ``Location``. Old: ``{id, object, request_id,
  status: running}``. The job ends ``succeeded`` with one result, as before.
* ``queued_behind`` (no ``mr`` cell): the run core runs one job at a time by default (``images.media_jobs.concurrency``),
  so a second request waits in ``queued`` behind the first and then succeeds. Old: the second request was refused at once
  (see ``busy_lease``). This is the main behaviour change of the move.
* ``busy_lease`` -> ``admission_refusal``: with two runs allowed at once, a request for the pool the first holds waits its
  turn and then runs: the sync request answers 200 with the model and the ``respond-async`` job succeeds, one engine call
  each. Old: the second was refused 503 ``resource_unavailable`` with ``Retry-After: 30``. The pool is free afterwards.
* ``observer_refusal`` -> no ``mr`` cell: the observer answers 500, so capacity is unknown: 503 ``resource_unavailable``
  with ``Retry-After: 30`` and no engine call.
* ``health_503`` -> no ``mr`` cell: the engine's health read answers 503: 503 ``service_unavailable``, no engine call.
* ``service_problem`` -> no ``mr`` cell: a disabled service, an absent service and a service with no pool policy each
  answer 503 ``binding_unavailable`` before any job exists (``binding_problem``). Old: 503 ``service_unavailable``.
* ``engine_failure`` -> ``upstream_failure``: the engine's 502 answers 502 ``upstream_error`` (same). An unreachable
  engine answers 502 ``upstream_unavailable`` (old: ``upstream_error``). The job form ends ``failed`` with category
  ``upstream``.
* ``wait_timeout`` -> ``wait_timeout``: an engine that stalls past its configured time answers 504 ``upstream_timeout``
  (same). The job form fails the job ``upstream_timeout`` with category ``timeout``.
* ``cancel_running`` -> ``cancel_queued``: the cancel is accepted while the engine works (``cancel_requested`` true, job
  still ``running``), a second cancel answers the same, the run ends ``canceled`` with no result, and the pool is free
  afterwards. Same as before; the error text "Canceled by requester" is not carried.
* ``cancel_finished`` -> ``cancel_finished``: a finished job refuses the cancel ("This job has already finished"),
  twice, as before.
* ``replay_same`` / ``replay_conflict`` -> the same names: old route had no replay (two engine calls). The rows replay: the
  same key and body gives the first answer with one engine call; the same key with a different body answers 409
  ``idempotency_conflict`` with one engine call.
* ``disconnect`` -> ``disconnect``: a ``respond-async`` client that leaves keeps its job (it runs and succeeds). A sync
  caller that walks away while the engine works does not stop the engine call; the pool stays held until the engine
  returns and is then free. Same as before.
* ``policy_refusal`` -> ``policy_refusal``: 403, no engine call. Old code ``MODEL_NOT_IN_PLAN``; new ``not_in_access_plan``
  (the runtime route policy's own answer), with the plan details.
* ``validation_refusal`` -> ``validation_refusal``: a negative seed answers 422 ``invalid_inputs`` (old 400); a request
  without a picture answers 400 ``missing_input``; bytes that are not a picture answer 422 ``invalid_inputs`` (media type
  not accepted; the bytes decide the type); a body that is not multipart answers 400 ``invalid_request_body`` (old 400).
  No engine call in any of them.

Nothing masks a status, a code, a header named below, an engine request or a byte. Only the multipart boundary, job ids,
request ids and timestamps are replaced.
"""
from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import pytest

import proxy
from gateway import media_jobs, multipart_adapter, receipt_store, request_spool, workflow_adapters, workflow_run
from tests.support.access import authenticated, identity, plan_for
from tests.support.gateway import gateway_client, use_admin_db
from gateway.frontend_shapes import mesh as frontend_mesh_shapes, shapes as frontend_wire_shapes
from gateway.workflow_run import WorkflowRunCore
from tests.mesh_workflows import BUNDLED_ID
from tests.support.mesh_harness import GLB, PNG, TOTAL, Harness, install_owner_mesh

SYNC = "/v1/3d/generations"
RAW = "/v1/3d/generations/glb"
JOBS = "/gateway/v1/media/jobs"
FIXTURES = Path(__file__).parent / "fixtures" / "adapter_differential"
GOLDEN = Path(__file__).parent / "fixtures" / "s7_mesh_rows" / "cells.json"
OLD_CELLS = FIXTURES / "s7_mr_cells.json"
MARK = "S7-MESH-ROWS-RECORD"
ASYNC = {"Prefer": "respond-async"}
HANG_BOUND_SECONDS = 30
TERMINAL = {"succeeded", "failed", "canceled", "cancelled"}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    use_admin_db(monkeypatch, tmp_path)
    jobs = tmp_path / "media_jobs.db"
    media_jobs.init_db(jobs)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    receipt_store.init_db(receipt_store.DB_PATH)
    monkeypatch.setattr(proxy, "_WORKFLOW_RUN_SPOOL_ROOT", tmp_path / "spool")
    monkeypatch.setattr(proxy, "_WORKFLOW_JOB_SPOOL", request_spool.JobSpool(tmp_path / "spool"))
    monkeypatch.setattr(proxy, "_workflow_core_cache", None)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    harness = Harness()
    # Preserve wire goldens while bounding permanent observer/shortage fixtures.
    monkeypatch.setattr(workflow_run, "GPU_WAIT_S", 0.5)
    install_owner_mesh(monkeypatch)
    monkeypatch.setattr(proxy, "_workflow_core_cache", None)
    harness.retry_delays = []
    harness.on_retry_sleep = None
    original_init = WorkflowRunCore.__init__

    async def record_retry(delay):
        harness.retry_delays.append(delay)
        if harness.on_retry_sleep is not None:
            await harness.on_retry_sleep(delay)
        await asyncio.sleep(0)

    def init_with_recorded_retry(core, *args, **kwargs):
        if kwargs.get("sleep") is None:
            kwargs["sleep"] = record_retry
        original_init(core, *args, **kwargs)

    monkeypatch.setattr(WorkflowRunCore, "__init__", init_with_recorded_retry)
    # This rig stands in for the TRELLIS.2 service, so a request that names no model runs that workflow here.
    for shape in (frontend_mesh_shapes.LocalAI3D, frontend_mesh_shapes.RawGlb):
        monkeypatch.setitem(frontend_wire_shapes.DEFAULT_WORKFLOWS, shape.shape_id, lambda: BUNDLED_ID)
    # One run at a time is the gateway's default (images.media_jobs.concurrency); a cell that needs two at once raises it.
    monkeypatch.setattr(proxy, "_workflow_run_semaphore", asyncio.Semaphore(1))
    harness.run_slots = lambda n: monkeypatch.setattr(proxy, "_workflow_run_semaphore", asyncio.Semaphore(n))
    monkeypatch.setattr(proxy.MESH_DEPS, "services", lambda: harness.services)
    monkeypatch.setattr(proxy.MESH_DEPS, "client_factory", harness.deps().client_factory)
    real_release = proxy.MULTIPART_HOST.release_idle
    harness.release_calls = []
    harness.release_answer = None  # a test sets it to stand in for the gateway's release hook

    async def release_idle(spec):
        harness.release_calls.append(spec.pool_id)
        return await real_release(spec) if harness.release_answer is None else harness.release_answer

    host = dataclasses.replace(proxy.MULTIPART_HOST, leases=harness.leases, require_egress=lambda _service: None,
                               release_idle=release_idle)
    runner = multipart_adapter.runner(host)
    monkeypatch.setitem(workflow_adapters._RUNNERS, runner.binding_key, runner)
    return harness


# ------------------------------------------------------------------------------------------------ helpers
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def form(seed: str | None = "42") -> dict[str, Any]:
    out: dict[str, Any] = {"files": {"image": ("photo.png", PNG, "image/png")}}
    if seed is not None:
        out["data"] = {"seed": seed}
    return out


def _mask(value: Any) -> Any:
    text = json.dumps(value, sort_keys=True)
    text = re.sub(r"\b(?:mj|job|req)_[0-9a-f]{8,}\b", "ID", text)
    text = re.sub(r"\b(?:run|chatcmpl|wf|rid)[-_][0-9a-zA-Z-]{8,}\b", "ID", text)
    text = re.sub(r'"(created|created_at|updated_at)": \d+', r'"\1": 0', text)
    return json.loads(text)


def answer(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type")
    is_json = bool(content_type and content_type.startswith("application/json"))
    return _mask({"status": response.status_code, "content_type": content_type,
                  "retry_after": response.headers.get("retry-after"),
                  "location": response.headers.get("location"),
                  "json": json.loads(response.content) if is_json else None,
                  "body": None if is_json else _b64(response.content)})


def engine_requests(harness: Harness) -> list[dict[str, Any]]:
    out = []
    for request in harness.downstream:
        content_type = request.headers["content-type"]
        boundary = content_type.split("boundary=", 1)[1]
        out.append({"method": request.method, "url": str(request.url), "content_type": content_type.replace(boundary, "BOUNDARY"),
                    "body": _b64(request.content.replace(boundary.encode(), b"BOUNDARY"))})
    return out


def job_id_of(response: httpx.Response) -> str:
    return response.json()["id"]


def _providers():
    """The media-job routes' own getter and canceler, built over the test's store (the router binds the live store path
    at import, so the test builds them over its own, as the run-route tests do)."""
    from gateway import api

    _, getter, _lister, _events, _append, canceler = api._build_media_job_providers(
        media_job_creator=None, media_job_getter=None, media_job_lister=None, media_job_event_lister=None,
        media_job_event_appender=None, media_job_canceler=None, media_job_store_path=media_jobs.DB_PATH)
    return getter, canceler


async def job_facts(_client: httpx.AsyncClient, job_id: str) -> dict[str, Any]:
    row = _providers()[0](job_id, audience="client")
    return _mask({"status": row["status"], "error_code": row.get("error_code"), "error_category": row.get("error_category"),
                  "retryable": row.get("retryable"), "cancel_requested": row.get("cancel_requested"),
                  "results": row.get("results") or {}})


def cancel(job_id: str) -> dict[str, Any]:
    """The cancel route's own function: the new state, or why the job refuses."""
    try:
        done = _providers()[1](job_id, audience="client")
    except ValueError as error:
        return {"refused": str(error)}
    return {"status": done["status"], "cancel_requested": done.get("cancel_requested")}


async def settle(client: httpx.AsyncClient, job_id: str, timeout: float = 15.0) -> dict[str, Any]:
    async with asyncio.timeout(timeout):
        while True:
            facts = await job_facts(client, job_id)
            if facts["status"] in TERMINAL:
                return facts
            await asyncio.sleep(0.02)


def gpu_events(job_id: str, kind: str) -> list[dict[str, Any]]:
    return [e for e in media_jobs.events(job_id, audience="client", path=media_jobs.DB_PATH) if e["event_type"] == kind]


async def gpu_waits(count: int, timeout: float = 10.0) -> set[str]:
    """Wait until ``count`` unfinished jobs have started to wait for the GPU; their ids."""
    async with asyncio.timeout(timeout):
        while True:
            waiting = {job["job_id"] for job in media_jobs.unfinished(path=media_jobs.DB_PATH)
                       if gpu_events(job["job_id"], "waiting_for_gpu")}
            if len(waiting) >= count:
                return waiting
            await asyncio.sleep(0.02)


async def free_slots_after_runs() -> int:
    """The run slots free once every worker has ended (a job settles before its worker gives its slot back)."""
    await asyncio.gather(*list(proxy._workflow_run_tasks))
    return proxy._workflow_run_semaphore._value


class Gate:
    """Holds the engine's ``/generate`` call until released, and says when it started."""

    def __init__(self, harness: Harness) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

        async def hook(request: httpx.Request) -> None:
            if request.url.path == "/generate":
                self.started.set()
                await self.release.wait()

        harness.hook = hook


def run(harness: Harness, scenario: Callable[[httpx.AsyncClient], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        async with gateway_client() as client, asyncio.timeout(HANG_BOUND_SECONDS):
            facts = await scenario(client)
        return facts

    facts = asyncio.run(go())
    return {**facts, "engine_requests": engine_requests(harness), "claims_left": harness.leases.active_claims()}


# ------------------------------------------------------------------------------------------------ the cells
def cell_success_raw(h: Harness) -> dict[str, Any]:
    async def go(c):
        return {"answer": answer(await c.post(RAW, **form()))}
    return run(h, go)


def cell_success_sync(h: Harness) -> dict[str, Any]:
    async def go(c):
        return {"answer": answer(await c.post(SYNC, **form()))}
    return run(h, go)


def cell_async_job(h: Harness) -> dict[str, Any]:
    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        return {"created": answer(created), "job": await settle(c, job_id_of(created))}
    return run(h, go)


def cell_queued_behind(h: Harness) -> dict[str, Any]:
    """With the default of one run at a time, a second request waits for the first; it is not refused."""
    gate = Gate(h)

    async def go(c):
        first = await c.post(SYNC, headers=ASYNC, **form("1"))
        await gate.started.wait()
        second = await c.post(SYNC, headers=ASYNC, **form("2"))
        waiting = await job_facts(c, job_id_of(second))
        gate.release.set()
        return {"waiting": waiting, "first_job": await settle(c, job_id_of(first)), "second_job": await settle(c, job_id_of(second))}
    return run(h, go)


def cell_busy_lease(h: Harness) -> dict[str, Any]:
    h.run_slots(4)
    gate = Gate(h)

    async def go(c):
        first = await c.post(SYNC, headers=ASYNC, **form())
        await gate.started.wait()
        second = asyncio.create_task(c.post(RAW, **form()))
        await gpu_waits(1)
        third = await c.post(SYNC, headers=ASYNC, **form())
        await gpu_waits(2)
        gate.release.set()
        return {"first": answer(first), "second_sync": answer(await second), "third_async": answer(third),
                "third_job": await settle(c, job_id_of(third)), "first_job": await settle(c, job_id_of(first))}
    return run(h, go)


def cell_observer_refusal(h: Harness) -> dict[str, Any]:
    h.observer_status = 500

    async def go(c):
        return {"answer": answer(await c.post(RAW, **form()))}
    return run(h, go)


def cell_health_503(h: Harness) -> dict[str, Any]:
    h.health_status = 503

    async def go(c):
        return {"answer": answer(await c.post(RAW, **form()))}
    return run(h, go)


def cell_service_problem(h: Harness) -> dict[str, Any]:
    async def go(c):
        out = {}
        h.services["trellis"]["enabled"] = False
        out["disabled"] = answer(await c.post(RAW, **form()))
        h.services["trellis"]["enabled"] = True
        saved = h.services.pop("trellis")
        out["absent"] = answer(await c.post(RAW, **form()))
        h.services["trellis"] = saved
        saved_capacity = saved["config"].pop("resource_capacity")
        out["no_pool_policy"] = answer(await c.post(RAW, **form()))
        saved["config"]["resource_capacity"] = saved_capacity
        return out
    return run(h, go)


def cell_engine_failure(h: Harness) -> dict[str, Any]:
    async def go(c):
        h.downstream_mode = 502
        server = answer(await c.post(RAW, **form()))
        h.downstream_mode = httpx.ConnectError("golden engine unreachable")
        down = answer(await c.post(RAW, **form()))
        h.downstream_mode = 502
        job = await c.post(SYNC, headers=ASYNC, **form())
        return {"engine_502": server, "engine_unreachable": down, "job_after_502": await settle(c, job_id_of(job))}
    return run(h, go)


def cell_wait_timeout(h: Harness) -> dict[str, Any]:
    h.services["trellis"]["config"]["timeout"] = 0.3
    Gate(h)  # never released: the engine stalls past its time

    async def go(c):
        sync = answer(await c.post(RAW, **form()))
        job = await c.post(SYNC, headers=ASYNC, **form())
        return {"sync": sync, "job": await settle(c, job_id_of(job))}
    return run(h, go)


def cell_cancel_running(h: Harness) -> dict[str, Any]:
    gate = Gate(h)

    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        job_id = job_id_of(created)
        await gate.started.wait()
        first, again = cancel(job_id), cancel(job_id)
        during = await job_facts(c, job_id)
        gate.release.set()
        after = await settle(c, job_id)
        return {"created": answer(created), "cancel": first, "cancel_again": again, "during": during,
                "after": after, "pool_held_after": h.leases.active_claims() != 0}
    return run(h, go)


def cell_cancel_finished(h: Harness) -> dict[str, Any]:
    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        job_id = job_id_of(created)
        before = await settle(c, job_id)
        first, again = cancel(job_id), cancel(job_id)
        return {"created": answer(created), "before": before, "cancel": first, "cancel_again": again,
                "after": await job_facts(c, job_id)}
    return run(h, go)


KEY = {"Idempotency-Key": "golden-key-1"}


def cell_replay_same(h: Harness) -> dict[str, Any]:
    async def go(c):
        first = await c.post(RAW, headers=KEY, **form())
        second = await c.post(RAW, headers=KEY, **form())
        return {"first": answer(first), "second": answer(second)}
    return run(h, go)


def cell_replay_conflict(h: Harness) -> dict[str, Any]:
    async def go(c):
        first = await c.post(RAW, headers=KEY, **form("42"))
        second = await c.post(RAW, headers=KEY, **form("43"))
        return {"first": answer(first), "second": answer(second)}
    return run(h, go)


def cell_disconnect(h: Harness) -> dict[str, Any]:
    gate = Gate(h)

    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        job_id = job_id_of(created)
        await gate.started.wait()
        while_running = {"job": await job_facts(c, job_id), "pool_held": h.leases.active_claims() != 0}
        # A sync caller that walks away while the engine works (the pool is held, so this one is refused first);
        # then the engine returns and the first job finishes.
        gate.release.set()
        return {"created": answer(created), "while_running": while_running, "after": await settle(c, job_id)}

    facts = run(h, go)

    async def sync_walkaway() -> dict[str, Any]:
        gate2 = Gate(h)
        async with gateway_client() as client:
            task = asyncio.ensure_future(client.post(RAW, **form()))
            await gate2.started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            held = h.leases.active_claims() != 0
            gate2.release.set()
            for _ in range(500):
                if h.leases.active_claims() == 0:
                    break
                await asyncio.sleep(0.02)
        return {"pool_held_while_gone": held, "pool_free_after": h.leases.active_claims() == 0}

    facts["sync_walkaway"] = asyncio.run(sync_walkaway())
    facts["engine_requests"] = engine_requests(h)
    return facts


def cell_policy_refusal(h: Harness) -> dict[str, Any]:
    async def go(c):
        with authenticated(identity(plan_for(caps=("chat",)))):
            return {"answer": answer(await c.post(RAW, **form()))}
    return run(h, go)


def cell_validation_refusal(h: Harness) -> dict[str, Any]:
    async def go(c):
        return {
            "seed_negative": answer(await c.post(RAW, **form("-1"))),
            "missing_image": answer(await c.post(RAW, files={"seed": (None, "1")})),
            "not_multipart": answer(await c.post(RAW, content=b"not a multipart body",
                                                 headers={"content-type": "multipart/form-data; boundary=zz"})),
            "not_a_picture": answer(await c.post(RAW, files={"image": ("photo.png", b"plain text", "image/png")})),
        }
    return run(h, go)


CELLS: dict[str, Callable[[Harness], dict[str, Any]]] = {
    name[len("cell_"):]: fn for name, fn in sorted(globals().items()) if name.startswith("cell_")}


def _golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


@pytest.mark.parametrize("name", sorted(CELLS))
def test_the_cell_matches_its_recording(rig, name):
    facts = CELLS[name](rig)
    print(f"{MARK} {name} {json.dumps(facts, sort_keys=True)}")
    assert name in _golden(), f"{name} has no recording; record it (see the module docstring)"
    assert facts == _golden()[name]


def test_the_recording_holds_exactly_the_cells():
    assert set(_golden()) == set(CELLS)


# ------------------------------------------------------------------------------------------------ the oracle
def _old_engine_request() -> dict[str, Any]:
    return json.loads(OLD_CELLS.read_text(encoding="utf-8"))["success"]["engine_requests"][0]


def _parts(request: dict[str, Any]) -> list[tuple[str, ...]]:
    """The parts of a recorded multipart request, as a sorted list: name, filename, content type and bytes of each."""
    body = base64.b64decode(request["body"])
    parts = []
    for raw in body.split(b"--BOUNDARY")[1:-1]:
        head, _, payload = raw.strip(b"\r\n").partition(b"\r\n\r\n")
        parts.append((head.decode("latin-1"), _b64(payload)))
    return sorted(parts)


def test_the_engine_request_equals_the_old_route_request(rig):
    """Same method, URL, content type and parts (names, filenames, types and bytes). Only the order of the form fields
    differs: the old route sent them in the manifest's order (seed, resolution); the run pins the binding with sorted
    keys, so they go alphabetically (resolution, seed). The engine reads a form by field name."""
    (new,) = CELLS["success_raw"](rig)["engine_requests"]
    old = _old_engine_request()
    assert {key: new[key] for key in ("method", "url", "content_type")} == {key: old[key] for key in ("method", "url", "content_type")}
    assert _parts(new) == _parts(old)
    assert len(base64.b64decode(new["body"])) == len(base64.b64decode(old["body"]))


def test_the_model_bytes_equal_the_old_route_answer(rig):
    facts = CELLS["success_raw"](rig)
    assert facts["answer"]["body"] == json.loads(OLD_CELLS.read_text(encoding="utf-8"))["success"]["answer"]["body"]
    assert base64.b64decode(facts["answer"]["body"]) == GLB


# ------------------------------------------------------------------------------------------------ sensitivity controls
def _flip(value: Any) -> Any:
    """The same record with one fact changed."""
    if isinstance(value, dict):
        for key, inner in value.items():
            changed = _flip(inner)
            if changed is not None:
                return {**value, key: changed}
        return None
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str) and value:
        return value + "x"
    if isinstance(value, list) and value:
        changed = _flip(value[0])
        return None if changed is None else [changed, *value[1:]]
    return None


@pytest.mark.parametrize("name", sorted(CELLS))
def test_a_changed_fact_would_fail_the_cell(rig, name):
    """Control: one changed fact (a status, a code, an engine byte) is a different record, so the comparison can fail."""
    facts = CELLS[name](rig)
    assert facts == _golden()[name]
    changed = _flip(facts)
    assert changed is not None and changed != _golden()[name]


def test_a_changed_behavior_changes_the_record(rig):
    """Control on behavior: the same call answers differently when the pool's observer does not answer."""
    free = CELLS["success_raw"](rig)["answer"]["status"]
    unobserved = CELLS["observer_refusal"](rig)["answer"]["status"]
    assert (free, unobserved) == (200, 503)


def test_the_job_content_route_serves_the_model(rig):
    """The 202 job's ``content_url`` serves the finished model itself (the run's Library asset)."""
    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        await settle(c, job_id_of(created))
        content = await c.get(created.json()["content_url"])
        return {"status": content.status_code, "type": content.headers.get("content-type"), "bytes": content.content}
    got = run(rig, go)
    assert (got["status"], got["type"], got["bytes"]) == (200, "model/gltf-binary", GLB)


@pytest.mark.parametrize(("release", "message", "retry_steps"), [
    # A busy engine, a failed or absent release, or idle engines freed and memory still short: memory may still
    # free, and the next attempt is a new lease that asks the release hook again, so the busy copy and its timer stay.
    ("freed", "The graphics card is busy with other work. Try again soon.", [30] * 11),
    # These three keep their copy; since train 117 the job retries in place, so each sends one timer per attempt
    # (11 attempts: the manifest's binding retry block).
    ("engine_busy", "The graphics card is busy with other work. Try again soon.", [30] * 11),
    ("nothing_freed", "The graphics card is busy with other work. Try again soon.", [30] * 11),
    ("not_asked", "The graphics card is busy with other work. Try again soon.", [30] * 11),
])
def test_lease_free_capacity_refusal_reports_release_outcome(rig, monkeypatch, release, message, retry_steps):
    from gateway.exclusive_pool import PoolCapacityError
    from gateway.resource_claims import AdmissionStatus

    async def refuse(*_args, **_kwargs):
        raise PoolCapacityError(AdmissionStatus.DELAYED, lease_held=False, release=release)

    monkeypatch.setattr(multipart_adapter.MultipartRunner, "_lease_and_run", refuse)

    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        job_id = job_id_of(created)
        await settle(c, job_id)
        return {"row": _providers()[0](job_id, audience="client"),
                "events": media_jobs.events(job_id, audience="client", path=media_jobs.DB_PATH)}

    got = run(rig, go)
    row = got["row"]
    assert row["status"] == "failed"
    assert rig.retry_delays == ([30.0] * 10 if retry_steps else [])
    assert row["error"] == message
    assert (row["error_code"], row["error_category"], row["retryable"]) == ("resource_unavailable", "capacity", True)
    assert [e["payload"].get("step") for e in got["events"] if e["event_type"] == "retry_after"] == retry_steps


def test_freed_but_still_short_waits_in_one_attempt_until_its_deadline(rig):
    """Memory shortage stays in admission; the deadline settles the original attempt."""
    rig.observer_body = {"free": 1024 * (1 << 20), "total": TOTAL}  # below the claim, whatever is released
    rig.release_answer = True

    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        job_id = job_id_of(created)
        await settle(c, job_id)
        return {"row": _providers()[0](job_id, audience="client"),
                "events": media_jobs.events(job_id, audience="client", path=media_jobs.DB_PATH)}

    got = run(rig, go)
    assert got["row"]["error"] == "The graphics card is busy with other work. Try again soon."
    assert [e["payload"].get("step") for e in got["events"] if e["event_type"] == "retry_after"] == [30]
    assert len(rig.release_calls) == 1
    assert rig.retry_delays == []
    assert len([e for e in got["events"] if e["event_type"] == "attempt"]) == 1
    assert len([e for e in got["events"] if e["event_type"] == "waiting_for_gpu"]) == 1
    assert rig.downstream == []  # the engine was never called


def _seeds(engine_requests: list[dict[str, Any]]) -> list[str]:
    return [re.search(rb'name="seed"\r\n\r\n(\d+)', base64.b64decode(r["body"])).group(1).decode() for r in engine_requests]


def test_jobs_on_a_held_pool_wait_their_turn_and_run_in_order(rig):
    """Two runs allowed at once, so the jobs do not queue for a run slot: they meet the held pool. Each waits its turn
    (one ``waiting_for_gpu`` event, progress unchanged), gives its run slot back while it waits, and runs once the jobs
    before it end. No job fails, none uses a retry, and the engine sees one call per job in the order they came."""
    rig.run_slots(2)
    gate = Gate(rig)

    async def go(c):
        first = await c.post(SYNC, headers=ASYNC, **form("1"))
        await gate.started.wait()
        waiting: list[str] = []
        for seed in ("2", "3", "4"):
            waiting.append(job_id_of(await c.post(SYNC, headers=ASYNC, **form(seed))))
            assert waiting[-1] in await gpu_waits(len(waiting))
        free_slots = proxy._workflow_run_semaphore._value  # the waiters gave theirs back; the first still holds one
        progress = {job_id: _providers()[0](job_id, audience="client")["progress"] for job_id in waiting}
        gate.release.set()
        jobs = [await settle(c, job_id) for job_id in (job_id_of(first), *waiting)]
        events = {job_id: (gpu_events(job_id, "waiting_for_gpu"), gpu_events(job_id, "gpu_ready")) for job_id in waiting}
        return {"jobs": jobs, "free_slots": free_slots, "progress": progress, "events": events,
                "slots_after": await free_slots_after_runs()}

    got = run(rig, go)
    assert [job["status"] for job in got["jobs"]] == ["succeeded"] * 4
    assert _seeds(got["engine_requests"]) == ["1", "2", "3", "4"]
    assert rig.retry_delays == []
    assert (got["free_slots"], got["slots_after"]) == (1, 2)
    for job_id, (waits, readies) in got["events"].items():
        assert len(waits) == 1 and len(readies) == 1
        # A client reads the event's type and its phase, status and progress (the store keeps reason and wait_s inside).
        assert waits[0]["payload"] == {"phase": "waiting_for_gpu", "status": "running", "progress": got["progress"][job_id]}
        assert readies[0]["payload"]["progress"] == got["progress"][job_id]
    assert not got["claims_left"]


def test_a_wait_that_outlasts_its_budget_fails_the_job_as_busy(rig, monkeypatch):
    """The budget (``retry.gpu_wait_s``, here 0.1 s) ends while the pool is still held: the job fails with the busy copy,
    retryable by its caller, and does not wait again through the retry rule."""
    monkeypatch.setattr(workflow_run, "GPU_WAIT_S", 0.1)
    rig.run_slots(4)
    gate = Gate(rig)

    async def go(c):
        first = await c.post(SYNC, headers=ASYNC, **form())
        await gate.started.wait()
        second = job_id_of(await c.post(SYNC, headers=ASYNC, **form()))
        second_job = await settle(c, second)
        row = _providers()[0](second, audience="client")
        events = media_jobs.events(second, audience="client", path=media_jobs.DB_PATH)
        gate.release.set()
        await settle(c, job_id_of(first))
        return {"job": second_job, "row": row, "events": events, "slots_after": await free_slots_after_runs()}

    got = run(rig, go)
    row = got["row"]
    assert row["error"] == "The graphics card is busy with other work. Try again soon."
    assert (row["error_code"], row["error_category"], row["retryable"]) == ("resource_unavailable", "capacity", True)
    assert rig.retry_delays == []
    kinds = [e["event_type"] for e in got["events"]]
    assert (kinds.count("waiting_for_gpu"), kinds.count("gpu_ready"), kinds.count("retry")) == (1, 0, 0)
    assert [e["payload"].get("step") for e in got["events"] if e["event_type"] == "retry_after"] == [30]
    assert len(got["engine_requests"]) == 1
    assert got["slots_after"] == 4
    assert not got["claims_left"]


def test_cancel_while_waiting_for_the_gpu_ends_the_job_at_once_without_an_engine_call(rig):
    rig.run_slots(4)
    gate = Gate(rig)

    async def go(c):
        first = await c.post(SYNC, headers=ASYNC, **form())
        await gate.started.wait()
        second = job_id_of(await c.post(SYNC, headers=ASYNC, **form()))
        await gpu_waits(1)
        cancelled = cancel(second)
        second_job = await settle(c, second, timeout=2.0)  # ends while the first still holds the pool
        gate.release.set()
        return {"cancelled": cancelled, "job": second_job, "first_job": await settle(c, job_id_of(first)),
                "slots_after": await free_slots_after_runs()}

    got = run(rig, go)
    assert got["cancelled"]["cancel_requested"] is True
    assert got["job"]["status"] in {"canceled", "cancelled"}
    assert got["first_job"]["status"] == "succeeded"
    assert len(got["engine_requests"]) == 1
    assert rig.retry_delays == []
    assert got["slots_after"] == 4
    assert not got["claims_left"]


def short_sync_wait(monkeypatch) -> None:
    """A sync wire waits for its job at most 0.3 s here (its own bound is minutes)."""
    real_wait = workflow_run.WorkflowJobService.wait

    async def wait(self, job_id, caller, timeout_s):
        return await real_wait(self, job_id, caller, min(timeout_s, 0.3))

    monkeypatch.setattr(workflow_run.WorkflowJobService, "wait", wait)


def test_a_sync_caller_whose_wait_ends_while_its_job_waits_for_the_gpu_gets_503_and_the_job_is_cancelled(rig, monkeypatch):
    """The sync wire waits a bounded time (``short_sync_wait``). When it ends while the job still waits for the held pool, the
    caller is told the GPU is busy (503, ``Retry-After`` the pool's 30 s) at once, and the job is cancelled: it never
    reaches the engine, and a client that retries does not leave a second copy queued."""
    short_sync_wait(monkeypatch)
    rig.run_slots(2)
    gate = Gate(rig)

    async def go(c):
        first = await c.post(SYNC, headers=ASYNC, **form("1"))
        await gate.started.wait()
        sync = answer(await c.post(RAW, **form("2")))
        with media_jobs.connection(media_jobs.DB_PATH) as conn:
            (second,), = conn.execute("SELECT job_id FROM media_jobs WHERE job_id != ?", (job_id_of(first),)).fetchall()
        assert gpu_events(second, "waiting_for_gpu") and not gpu_events(second, "gpu_ready")
        second_job = await settle(c, second, timeout=2.0)  # ends while the first still holds the pool
        gate.release.set()
        return {"sync": sync, "second_job": second_job, "first_job": await settle(c, job_id_of(first)),
                "slots_after": await free_slots_after_runs()}

    got = run(rig, go)
    assert got["sync"]["status"] == 503 and got["sync"]["retry_after"] == "30"
    assert got["sync"]["json"]["error"]["code"] == "resource_unavailable"
    assert got["second_job"]["status"] in {"canceled", "cancelled"}
    assert got["first_job"]["status"] == "succeeded"
    assert _seeds(got["engine_requests"]) == ["1"]
    assert got["slots_after"] == 2
    assert not got["claims_left"]


def test_a_sync_caller_whose_wait_ends_after_its_job_took_the_gpu_gets_504_and_the_job_runs(rig, monkeypatch):
    """Once the job holds the GPU it may have sent: the caller gets the plain 504 and the job runs to its end."""
    short_sync_wait(monkeypatch)
    gate = Gate(rig)

    async def go(c):
        sync = answer(await c.post(RAW, **form("1")))
        job_id = next(job["job_id"] for job in media_jobs.unfinished(path=media_jobs.DB_PATH))
        gate.release.set()
        return {"sync": sync, "job": await settle(c, job_id)}

    got = run(rig, go)
    assert got["sync"]["status"] == 504 and got["sync"]["retry_after"] is None
    assert got["job"]["status"] == "succeeded"
    assert _seeds(got["engine_requests"]) == ["1"]


@pytest.mark.parametrize("upstream_status", [200, 400, 503])
def test_durable_multipart_dispatch_uses_one_bundle_and_refunds_only_completed_work(rig, monkeypatch, tmp_path, upstream_status):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler
    from gateway.arbitration.types import ResourceRef
    store = ArbitrationStore(tmp_path / "admission.db")
    scheduler = AdmissionScheduler(store, worker_capacity=1, poll_s=0.001)
    rig.downstream_mode = "ok" if upstream_status == 200 else upstream_status
    original = workflow_adapters._RUNNERS["http-multipart"]
    host = dataclasses.replace(original._host, admission=lambda: scheduler)
    runner = multipart_adapter.runner(host)
    monkeypatch.setitem(workflow_adapters._RUNNERS, runner.binding_key, runner)
    monkeypatch.setattr(proxy, "_GPU_ADMISSION_SCHEDULER", scheduler)
    for service in rig.services.values():
        capacity = (service.get("config") or {}).get("resource_capacity")
        if capacity and capacity.get("enabled"):
            capacity["physical_resources"] = ["card/a"]
    async def go(c):
        response = await c.post(RAW, **form())
        assert response.status_code == (200 if upstream_status == 200 else 502)
        return {}
    try:
        run(rig, go)
        tickets = store._conn.execute("SELECT request_id FROM admission_tickets").fetchall()
        assert len(tickets) == 1
        kinds = {event["event_type"] for event in media_jobs.events(tickets[0][0], audience="internal", path=media_jobs.DB_PATH)}
        assert not kinds & {"waiting_for_gpu", "gpu_ready", "retry_after"}
        assert store.ticket(tickets[0][0]).phase == ("completed" if upstream_status < 500 else "unknown")
        assert store.charge(ResourceRef("physical/card/a", "1")) == (0 if upstream_status < 500 else 1)
        assert len(rig.downstream) == 1
        assert rig.leases.active_claims() == 0
    finally:
        store.close()

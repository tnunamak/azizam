"""S7 acceptance goldens for the 3D rows: ``POST /v1/3d/generations`` and ``POST /v1/3d/generations/glb``.

Each cell runs the real app, the real run core and worker, the real job store and the real multipart adapter. Only the
engine HTTP is fake (``tests.support.mesh_harness.Harness``: the observer, the health read and ``/generate``). One cell is
one outcome of the S7 matrix; its record is in ``fixtures/adapter_differential/s7_mesh_rows_cells.json``. To re-record,
run with ``--junit-xml-out`` and ``-o junit_logging=all`` (every run prints each cell after the ``S7-MESH-ROWS-RECORD``
marker).

Oracle for the engine side: ``fixtures/adapter_differential/s7_mr_cells.json`` holds the bytes today's
``POST /v1/meshes/generate`` sent to the engine. ``test_the_engine_request_equals_the_old_route_request`` requires the
same bytes from the new rows for the same input, so the engine saw no change.

Cell map (new cell -> the ``mr`` cell it replaces) and every difference from the old route. The old route was one
handler; the raw row is its closest wire (the answer is the GLB), the LocalAI row answers JSON that carries the model.

* ``success_raw``, ``success_sync`` -> ``success``. Status 200 and the model bytes are the same on the raw row. The
  LocalAI row answers JSON ``{created, data:[{b64_json}], ...}``. Gone: the ``x-request-id`` and ``x-asset-id`` headers
  and the receipt row (the run core keeps a job row and Library asset; no request receipt for the route).
* ``async_job`` -> the async part of ``admission_refusal`` / ``success``: ``Prefer: respond-async`` answers 202 with a job
  object. Old: ``{id, object: media.job, request_id, status}`` plus ``Location``; new: the run core's job object. The job
  ends ``succeeded`` with one asset, as before.
* ``busy_lease`` -> ``admission_refusal``: the second request is refused while the first holds the pool. Status 503 and
  ``Retry-After: 30`` are the same; the code is ``resource_unavailable`` and the message is the run core's generic text
  (old: "The GPU is busy. Try again shortly."). Gone: the failed receipt row. New: a respond-async second request
  creates a job that fails ``resource_unavailable`` (old route had no such job).
* ``observer_refusal``, ``health_503``, ``service_problem`` -> no ``mr`` recording (the old unit tests held them). The
  old route answered 503 with its own codes; the rows answer by category (see the cell facts).
* ``engine_failure`` -> ``upstream_failure``: 502 ``upstream_error`` is the same; the message is the generic text.
  The unreachable engine answers 502 ``upstream_unavailable`` (old: 502 ``upstream_error``).
* ``wait_timeout`` -> ``wait_timeout``: the engine stalls past its configured time; status 504 ``upstream_timeout``.
  The old sync form answered its timeout the same way; the job form fails the job ``upstream_timeout``.
* ``cancel_running`` -> ``cancel_queued``: the cancel is accepted while the engine works, the job ends ``canceled``
  and the pool is free afterwards. The old error text "Canceled by requester" is now the run core's.
* ``cancel_finished`` -> ``cancel_finished``: the finished job refuses the cancel (409) in both worlds.
* ``replay_same`` / ``replay_conflict`` -> ``replay_same`` / ``replay_conflict``: the old route had no replay (two
  engine calls). The rows replay: the same key and body gives the first job and one engine call; the same key with a
  different body is refused 409.
* ``disconnect`` -> ``disconnect``: a respond-async client that leaves keeps its job (it finishes). A sync client that
  leaves cancels nothing in the engine: the engine call finishes and the pool is free.
* ``policy_refusal`` -> ``policy_refusal``: 403 ``MODEL_NOT_IN_PLAN``, no engine call (the same).
* ``validation_refusal`` -> ``validation_refusal``: 400 per case (the same status; codes and messages are the shape's).

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
from gateway import media_jobs, multipart_adapter, receipt_store, request_spool, workflow_adapters
from tests.support.access import authenticated, identity, plan_for
from tests.support.gateway import gateway_client, use_admin_db
from tests.support.mesh_harness import GLB, PNG, Harness

SYNC = "/v1/3d/generations"
RAW = "/v1/3d/generations/glb"
JOBS = "/gateway/v1/media/jobs"
FIXTURES = Path(__file__).parent / "fixtures" / "adapter_differential"
GOLDEN = FIXTURES / "s7_mesh_rows_cells.json"
OLD_CELLS = FIXTURES / "s7_mr_cells.json"
MARK = "S7-MESH-ROWS-RECORD"
ASYNC = {"Prefer": "respond-async"}
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
    monkeypatch.setattr(proxy.MESH_DEPS, "services", lambda: harness.services)
    monkeypatch.setattr(proxy.MESH_DEPS, "client_factory", harness.deps().client_factory)
    host = dataclasses.replace(proxy.MULTIPART_HOST, leases=harness.leases, require_egress=lambda _service: None)
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
    text = re.sub(r'"created": \d+', '"created": 0', text)
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
        async with gateway_client() as client:
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


def cell_busy_lease(h: Harness) -> dict[str, Any]:
    gate = Gate(h)

    async def go(c):
        first = await c.post(SYNC, headers=ASYNC, **form())
        await gate.started.wait()
        second = await c.post(RAW, **form())
        third = await c.post(SYNC, headers=ASYNC, **form())
        third_job = await settle(c, job_id_of(third))
        gate.release.set()
        return {"first": answer(first), "second_sync": answer(second), "third_async": answer(third),
                "third_job": third_job, "first_job": await settle(c, job_id_of(first))}
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
            "missing_image": answer(await c.post(RAW, data={"seed": "1"}, files={"other": ("x.txt", b"x", "text/plain")})),
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


def test_the_engine_request_equals_the_old_route_request(rig):
    facts = CELLS["success_raw"](rig)
    assert facts["engine_requests"] == [_old_engine_request()]


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
    """Control on behavior: the same call with the pool held answers differently from the free pool."""
    free = CELLS["success_raw"](rig)["answer"]["status"]
    busy = CELLS["busy_lease"](rig)["second_sync"]["status"]
    assert (free, busy) == (200, 503)

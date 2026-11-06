"""POST /v1/meshes/generate with ``Prefer: respond-async``: a media job a gateway task runs, not the client connection.

The route, the frontend and service records, the pool and the media-job store are the real ones (a temporary sqlite
file); only auth, the receipts, the library write and the engine are fakes. Everything runs in one event loop, so a
test holds the engine open, looks at the job and the lease, then lets the engine return.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gateway import media_jobs, mesh_api, receipt_store
from gateway.drain import DrainState
from gateway.exclusive_pool import AdmissionStatus, ExclusivePoolLeaseManager, PoolCapacityError
from gateway.mesh_api import MESH_PATH, prefers_async
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt
from tests.test_mesh_route import (
    GLB,
    PNG,
    TRELLIS_URL,
    Harness,
    _Engine,
    _call,
    _multipart_body,
    _others,
    _Upstream,
    _pool_held,
    _until,
)

ASSET = "ast_" + "c" * 64
PREFER = [(b"prefer", b"respond-async")]


class AsyncHarness:
    """A mesh route with the async form on, a job store in ``tmp_path``, and an engine the test holds open."""

    def __init__(self, tmp_path: Path) -> None:
        self.jobs = tmp_path / "jobs.db"
        media_jobs.init_db(self.jobs)
        self.route = Harness()
        self.route.jobs_path = self.jobs
        self.drain = DrainState()
        self.route.register_job_counter = self.drain.register_job_counter
        self.stored: list[bytes] = []
        self.outputs: list[Any] = []
        self.route.store_output = lambda auth, output: self.outputs.append(output) or self.stored.append(output.data) or ASSET
        self.engine = _Engine()
        self.hold_engine = True
        self.engine_answer: Any = "ok"
        answer = self.route.handler

        async def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == f"{TRELLIS_URL}/generate":
                if self.hold_engine:
                    await self.engine.stall("engine")
                if self.engine_answer != "ok":
                    self.route.downstream.append(request)
                    return httpx.Response(self.engine_answer, content=b'{"error":"internal secret"}')
            return answer(request)

        self.route.handler = handler  # type: ignore[method-assign]
        self.app: FastAPI = self.route.app()
        self.body, self.boundary = _multipart_body()

    @property
    def leases(self) -> ExclusivePoolLeaseManager:
        return self.route.leases

    @property
    def receipts(self) -> list[dict[str, Any]]:
        return self.route.receipts

    async def submit(self, *, prefer: bool = True, headers: dict[str, str] | None = None) -> httpx.Response:
        """One request through the real app, in this event loop."""
        extra = {"prefer": "respond-async"} if prefer else {}
        extra.update(headers or {})
        files = {"image": ("p.png", PNG, "image/png")}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://gateway.test") as client:
            return await client.post(MESH_PATH, files=files, headers=extra)

    async def engine_started(self) -> None:
        await _until(lambda: "engine-start" in self.engine.events)

    def let_engine_return(self) -> None:
        self.engine.release.set()

    async def idle(self) -> None:
        await _until(lambda: not _pool_held(self.route) and not _others())

    def job(self, job_id: str) -> dict[str, Any]:
        found = media_jobs.get(job_id, audience="internal", path=self.jobs)
        assert found is not None
        return found

    def all_jobs(self) -> list[dict[str, Any]]:
        return media_jobs.recent(path=self.jobs, limit=50)


def run(coroutine) -> Any:
    return asyncio.run(asyncio.wait_for(coroutine, timeout=30))


@pytest.fixture
def harness(tmp_path: Path) -> AsyncHarness:
    return AsyncHarness(tmp_path)


def job_id_of(response: httpx.Response) -> str:
    return response.json()["id"]


# --- the header ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("respond-async", True),
    ("Respond-Async", True),
    ("respond-async, wait=10", True),
    ("return=minimal; respond-async", False),  # a parameter of another preference, not the token
    ("return=minimal, respond-async", True),
    ("wait=10", False),
    ("respond-asyncs", False),
    ("", False),
    (None, False),
])
def test_prefers_async_reads_the_header_as_rfc_7240_tokens(value, expected) -> None:
    assert prefers_async(value) is expected


# --- submit: 202 and the job ---------------------------------------------------------------------------------------

def test_submit_answers_202_with_the_job_while_the_engine_is_still_running(harness: AsyncHarness) -> None:
    async def scenario() -> None:
        response = await harness.submit()
        assert response.status_code == 202
        job_id = job_id_of(response)
        assert response.headers["location"] == f"/gateway/v1/media/jobs/{job_id}"
        assert response.headers["preference-applied"] == "respond-async"
        assert response.headers["x-request-id"] == "req-test"
        assert response.json()["status"] == "running"
        job = harness.job(job_id)
        assert (job["intent"], job["workflow"], job["backend"]) == ("mesh.generate", "mesh.generate.trellis-2", "trellis")
        assert job["request_id"] == "req-test"
        assert job["metadata"]["mode"] == "async_mesh"
        assert job["app_id"] == "app-test"
        # The engine is called by the job's task, which holds the lease and the one open receipt.
        await harness.engine_started()
        assert _pool_held(harness.route)
        assert harness.receipts == []
        assert harness.stored == []
        harness.let_engine_return()
        await harness.idle()

    run(scenario())


def test_a_refused_egress_answers_the_refusal_and_makes_no_job(harness: AsyncHarness) -> None:
    from fastapi.responses import JSONResponse

    harness.route.egress_refusal = lambda service_id: JSONResponse(
        {"error": {"code": "transition_unsupported"}}, status_code=503)

    async def scenario() -> None:
        response = await harness.submit()
        assert response.status_code == 503 and response.json()["error"]["code"] == "transition_unsupported"
        assert harness.all_jobs() == [], "a refused send leaves no job behind"
        assert harness.engine.events == [], "nothing reaches the engine"
        assert harness.route.egress_asked == ["trellis"]
        assert harness.receipts == [{"request_id": "req-test", "outcome": ("failed", "not_admitting"), "exc": None}]
        assert not _pool_held(harness.route)

    run(scenario())


def test_the_job_is_made_by_the_person_and_credential_of_the_submitting_request(harness: AsyncHarness) -> None:
    """The job's task is detached, but it inherits the request's context, so the job is stamped with its maker and its
    owner can read it back through the generic job routes."""
    from gateway import request_source

    async def scenario() -> str:
        with request_source.acting_as(app_id="app-test", person_id="usr-maker", credential_id="cred-maker"):
            job_id = job_id_of(await harness.submit())
        await harness.engine_started()
        harness.let_engine_return()
        await harness.idle()
        return job_id

    job = harness.job(run(scenario()))
    assert (job["maker_person_id"], job["maker_credential_id"]) == ("usr-maker", "cred-maker")


def test_the_finished_job_holds_the_model_in_the_library_and_the_receipt_closes_once(harness: AsyncHarness) -> None:
    async def scenario() -> None:
        job_id = job_id_of(await harness.submit())
        await harness.engine_started()
        harness.let_engine_return()
        await harness.idle()
        job = harness.job(job_id)
        assert job["status"] == "succeeded" and job["progress"] == 100
        assert job["assets"] == [{"asset_id": ASSET, "media_type": "model/gltf-binary"}]
        assert harness.stored == [GLB]
        assert harness.receipts == [{"request_id": "req-test", "outcome": ("succeeded", "succeeded"), "exc": None}]
        assert harness.leases.active_claims() == 0

    run(scenario())


def test_the_async_job_stores_the_model_with_the_workflow_name_the_request_and_the_picture(harness: AsyncHarness) -> None:
    """The Library keeps the same provenance for the async form as for the sync form (WorkflowOutput)."""
    async def scenario() -> None:
        await harness.submit()
        await harness.engine_started()
        harness.let_engine_return()
        await harness.idle()

    run(scenario())
    [output] = harness.outputs
    assert output.data == GLB and output.suffix == ".glb" and output.request_id == "req-test"
    assert output.workflow_name == "TRELLIS.2 image to 3D" and output.takes_text is False
    [source] = output.inputs
    assert (source.data, source.name, source.media_type) == (PNG, "p.png", "image/png")


def test_without_the_header_the_sync_wire_is_unchanged_and_no_job_is_made(harness: AsyncHarness) -> None:
    harness.hold_engine = False

    async def scenario() -> httpx.Response:
        return await harness.submit(prefer=False)

    response = run(scenario())
    assert response.status_code == 200
    assert response.headers["content-type"] == "model/gltf-binary"
    assert response.content == GLB
    assert "location" not in response.headers and "preference-applied" not in response.headers
    assert harness.all_jobs() == []
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("succeeded", "succeeded"), "exc": None}]


@pytest.mark.parametrize("missing", ["jobs", "library"])
def test_the_header_is_ignored_when_the_gateway_has_no_job_store_or_no_library(tmp_path: Path, missing: str) -> None:
    harness = AsyncHarness(tmp_path)
    harness.hold_engine = False
    if missing == "jobs":
        harness.route.jobs_path = None
    else:
        harness.route.store_output = None
    harness.app = harness.route.app()
    response = run(harness.submit())
    assert response.status_code == 200 and response.content == GLB
    assert harness.all_jobs() == []


# --- refusals are the sync route's own answers, and leave no job ------------------------------------------------------

def refused(harness: AsyncHarness, response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "location" not in response.headers and "preference-applied" not in response.headers
    assert harness.all_jobs() == []
    assert harness.leases.active_claims() == 0
    assert not _pool_held(harness.route)


def test_auth_policy_and_input_refusals_stay_synchronous(harness: AsyncHarness) -> None:
    class Denied:
        denied = True
        code = "MODEL_NOT_IN_PLAN"
        reasons = ("This isn't in your access plan.",)

    harness.route.authenticated = False
    refused(harness, run(harness.submit()), 401, "unauthorized")
    harness.route.authenticated = True
    harness.route.route_policy = lambda auth: Denied()
    refused(harness, run(harness.submit()), 403, "MODEL_NOT_IN_PLAN")
    harness.route.route_policy = None

    async def bad() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url="http://g.test") as client:
            return await client.post(MESH_PATH, data={"seed": "1"}, headers={"prefer": "respond-async"})

    refused(harness, run(bad()), 400, "missing_image")
    assert harness.route.downstream == []
    assert [r["outcome"][0] for r in harness.receipts] == ["failed"]


def test_an_unhealthy_service_is_the_sync_503(harness: AsyncHarness) -> None:
    harness.route.health_status = 503
    refused(harness, run(harness.submit()), 503, "service_unavailable")
    assert harness.receipts[-1]["outcome"] == ("failed", "unhealthy")


@pytest.mark.parametrize("observer", [{"free": 1, "total": 24 * 1024 << 20}, {"other": 1}])
def test_a_pool_that_cannot_take_the_run_is_the_sync_503_with_retry_after(harness: AsyncHarness, observer) -> None:
    harness.route.observer_body = observer

    async def scenario() -> httpx.Response:
        return await harness.submit()

    response = run(scenario())
    refused(harness, response, 503, "resource_unavailable")
    assert response.headers["retry-after"] == "30"
    assert harness.route.downstream == []
    assert [r["outcome"][0] for r in harness.receipts] == ["failed"]


def test_a_busy_pool_is_refused_at_once_not_queued(harness: AsyncHarness) -> None:
    async def scenario() -> tuple[httpx.Response, str]:
        first = await harness.submit()
        await harness.engine_started()
        second = await harness.submit()  # the lease is held by the first run
        harness.let_engine_return()
        await harness.idle()
        return second, job_id_of(first)

    second, first_id = run(scenario())
    assert second.status_code == 503 and second.json()["error"]["code"] == "resource_unavailable"
    assert second.headers["retry-after"] == "30"
    assert [job["job_id"] for job in harness.all_jobs()] == [first_id]
    assert sorted(r["request_id"] for r in harness.receipts) == ["req-test", "req-test-2"]
    assert dict((r["request_id"], r["outcome"]) for r in harness.receipts)["req-test-2"] == ("failed", "capacity")


def test_lease_with_wait_false_refuses_a_held_pool_and_still_queues_by_default() -> None:
    async def scenario() -> None:
        from tests.test_mesh_route import GOOD_CAPACITY
        from gateway.exclusive_pool import parse_resource_capacity

        spec = parse_resource_capacity("trellis", GOOD_CAPACITY)
        leases = ExclusivePoolLeaseManager()
        total = 24 * 1024 << 20

        async def observe() -> Any:
            return {"free": total, "total": total}

        entered = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with leases.lease(spec, request_id="a", observe=observe):
                entered.set()
                await release.wait()

        task = asyncio.ensure_future(holder())
        await entered.wait()
        with pytest.raises(PoolCapacityError) as refused_error:
            async with leases.lease(spec, request_id="b", observe=observe, wait=False):
                raise AssertionError("must not enter")
        assert refused_error.value.status is AdmissionStatus.DELAYED
        queued = asyncio.ensure_future(_enter(leases, spec, observe))
        await asyncio.sleep(0.05)
        assert not queued.done()
        release.set()
        await task
        assert await asyncio.wait_for(queued, 5) == "entered"

    async def _enter(leases, spec, observe) -> str:
        async with leases.lease(spec, request_id="c", observe=observe):
            return "entered"

    run(scenario())


# --- the run's failures end the job and the one receipt ------------------------------------------------------------

@pytest.mark.parametrize("status, code, category, reason", [
    (500, "upstream_error", "upstream", "upstream"),
])
def test_an_engine_failure_fails_the_job_with_a_plain_reason(harness: AsyncHarness, status, code, category, reason) -> None:
    harness.hold_engine = False
    harness.engine_answer = status

    async def scenario() -> str:
        job_id = job_id_of(await harness.submit())
        await harness.idle()
        return job_id

    job = harness.job(run(scenario()))
    assert job["status"] == "failed"
    assert job["error"] == "The mesh service failed to produce a model."
    assert "internal secret" not in json.dumps(job)
    assert job["error_code"] == code and job["error_category"] == category
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("failed", reason), "exc": None}]
    assert harness.stored == [] and harness.leases.active_claims() == 0


def test_an_unreachable_engine_fails_the_job_as_unavailable(harness: AsyncHarness) -> None:
    harness.hold_engine = False
    answer = harness.route.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{TRELLIS_URL}/generate":
            raise httpx.ConnectError("refused")
        return answer(request)

    harness.route.handler = handler  # type: ignore[method-assign]

    async def scenario() -> str:
        job_id = job_id_of(await harness.submit())
        await harness.idle()
        return job_id

    job = harness.job(run(scenario()))
    assert job["status"] == "failed" and job["error"] == "The mesh service could not be reached."
    assert harness.receipts[-1]["outcome"][0] == "failed"


def test_a_model_the_library_cannot_keep_fails_the_job_and_the_receipt(harness: AsyncHarness) -> None:
    def broken(auth: Any, output: Any) -> str:
        raise OSError("disk full")

    harness.route.store_output = broken
    harness.hold_engine = False
    harness.app = harness.route.app()

    async def scenario() -> str:
        job_id = job_id_of(await harness.submit())
        await harness.idle()
        return job_id

    job = harness.job(run(scenario()))
    assert job["status"] == "failed"
    assert job["error"] == "The model was made but could not be saved; make it again."
    assert job["error_code"] == "store_failed" and job["assets"] == []
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("failed", "store_failed"), "exc": None}]
    assert harness.leases.active_claims() == 0


# --- safe restart sees the work ------------------------------------------------------------------------------------

def test_the_drain_snapshot_counts_a_running_mesh_job_until_it_ends(harness: AsyncHarness) -> None:
    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        assert harness.drain.snapshot()["idle"] is True
        await harness.submit()
        await harness.engine_started()
        running = harness.drain.snapshot()
        harness.let_engine_return()
        await harness.idle()
        # The run leaves the counted set through a done callback, one loop step after the task ends; a real
        # counter that never drops still times out here.
        await _until(lambda: harness.drain.snapshot()["active"]["async_media_jobs"] == {"mesh": 0})
        return running, harness.drain.snapshot()

    running, finished = run(scenario())
    assert running["active"]["async_media_jobs"] == {"mesh": 1}
    assert running["idle"] is False
    assert finished["active"]["async_media_jobs"] == {"mesh": 0}
    assert finished["idle"] is True


def test_the_drain_snapshot_counts_a_sync_run_whose_client_left_until_the_engine_returns(harness: AsyncHarness) -> None:
    async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
        await _call(harness.app, harness.body, harness.boundary,
                    disconnect_when=lambda: "engine-start" in harness.engine.events)
        detached = harness.drain.snapshot()  # the client is gone; the engine still holds the pool
        harness.let_engine_return()
        await harness.idle()
        return detached, harness.drain.snapshot()

    detached, finished = run(scenario())
    assert detached["active"]["async_media_jobs"] == {"mesh": 1} and detached["idle"] is False
    assert finished["idle"] is True


# --- the engine call has a wall-clock bound ------------------------------------------------------------------------

class _Dribble(httpx.AsyncByteStream):
    """A 200 whose body never ends but never goes quiet for long: each read is quicker than the per-read timeout."""

    async def __aiter__(self):
        while True:
            yield b"x"
            await asyncio.sleep(0.02)


def dribbling_engine(harness: AsyncHarness, *, timeout: float) -> None:
    harness.route.services["trellis"]["config"]["timeout"] = timeout
    answer = harness.route.handler

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{TRELLIS_URL}/generate":
            return httpx.Response(200, stream=_Dribble(), headers={"content-type": "model/gltf-binary"})
        return await answer(request) if asyncio.iscoroutinefunction(answer) else answer(request)

    harness.route.handler = handler  # type: ignore[method-assign]


def test_an_engine_that_dribbles_bytes_is_cut_off_at_the_configured_time_and_frees_the_lease(harness: AsyncHarness) -> None:
    dribbling_engine(harness, timeout=0.3)

    async def scenario() -> tuple[str, float]:
        started = asyncio.get_running_loop().time()
        job_id = job_id_of(await harness.submit())
        await harness.idle()
        return job_id, asyncio.get_running_loop().time() - started

    job_id, elapsed = run(scenario())
    job = harness.job(job_id)
    assert job["status"] == "failed" and job["error"] == "The mesh service did not answer in time."
    assert elapsed < 5
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("failed", "timeout"), "exc": None}]
    assert harness.leases.active_claims() == 0


def test_the_sync_form_answers_504_for_an_engine_that_dribbles(harness: AsyncHarness) -> None:
    dribbling_engine(harness, timeout=0.3)

    response = run(harness.submit(prefer=False))

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("failed", "timeout"), "exc": None}]
    assert harness.leases.active_claims() == 0


# --- the client connection owns nothing ---------------------------------------------------------------------------

def test_a_disconnect_after_the_202_does_not_cancel_the_job(harness: AsyncHarness) -> None:
    async def scenario() -> str:
        sends = await _call(harness.app, harness.body, harness.boundary, extra_headers=PREFER,
                            disconnect_when=lambda: "engine-start" in harness.engine.events)
        start = next(m for m in sends if m["type"] == "http.response.start")
        assert start["status"] == 202
        job_id = json.loads(b"".join(m.get("body", b"") for m in sends if m["type"] == "http.response.body"))["id"]
        await asyncio.sleep(0.1)  # the client is gone; the engine has not been told
        assert harness.engine.events == ["engine-start"]
        assert harness.job(job_id)["status"] == "running" and _pool_held(harness.route)
        assert harness.receipts == []
        harness.let_engine_return()
        await harness.idle()
        return job_id

    job = harness.job(run(scenario()))
    assert job["status"] == "succeeded"
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("succeeded", "succeeded"), "exc": None}]
    assert harness.stored == [GLB]


def test_a_request_cancelled_before_the_lease_is_held_leaves_no_job_and_closes_the_receipt(harness: AsyncHarness) -> None:
    """The submit task is cancelled while the job's task is still taking the lease (a slow observer)."""
    gate = asyncio.Event
    harness.hold_engine = False

    async def scenario() -> None:
        observer_gate = gate()
        answer = harness.route.handler

        async def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith("http://vram-observer.test"):
                harness.engine.events.append("observer-start")
                await observer_gate.wait()
            return await answer(request)

        harness.route.handler = handler  # type: ignore[method-assign]
        submit = asyncio.ensure_future(harness.submit())
        await _until(lambda: "observer-start" in harness.engine.events)
        submit.cancel()
        await asyncio.gather(submit, return_exceptions=True)
        observer_gate.set()
        await harness.idle()

    run(scenario())
    assert harness.all_jobs() == []
    assert len(harness.receipts) == 1 and harness.receipts[0]["exc"] == "CancelledError"
    assert harness.leases.active_claims() == 0


# --- cancel: the lease stays until the engine returns ---------------------------------------------------------------

def test_cancel_keeps_the_lease_until_the_engine_returns_and_then_ends_as_canceled(harness: AsyncHarness) -> None:
    import proxy

    async def scenario() -> tuple[str, dict[str, Any], httpx.Response]:
        job_id = job_id_of(await harness.submit())
        await harness.engine_started()
        media_jobs_path = proxy.media_jobs.DB_PATH
        proxy.media_jobs.DB_PATH = harness.jobs
        try:
            cancelled = proxy._cancel_media_job(job_id, audience="client")  # the cancel route's own function
        finally:
            proxy.media_jobs.DB_PATH = media_jobs_path
        assert cancelled["status"] == "running" and cancelled["cancel_requested"] is True
        await asyncio.sleep(0.1)
        # The engine cannot be told to stop: the pool stays busy, and a new run is refused while it is.
        assert harness.job(job_id)["status"] == "running" and _pool_held(harness.route)
        refused_run = await harness.submit()
        harness.let_engine_return()
        await harness.idle()
        return job_id, cancelled, refused_run

    job_id, _, refused_run = run(scenario())
    assert refused_run.status_code == 503
    job = harness.job(job_id)
    assert job["status"] == "canceled" and job["error"] == "Canceled by requester"
    assert job["assets"] == [] and harness.stored == []  # the answer is dropped, not kept
    outcomes = dict((r["request_id"], r["outcome"]) for r in harness.receipts)
    assert outcomes["req-test"] == ("cancelled", "cancelled")
    assert harness.leases.active_claims() == 0

    # With the engine free again, the next run is admitted.
    harness.hold_engine = False
    again = run(harness.submit())
    assert again.status_code == 202


def test_a_cancel_that_arrives_after_the_engine_returned_but_before_the_job_was_kept_wins(harness: AsyncHarness) -> None:
    harness.hold_engine = False
    kept: list[str] = []

    def store(auth: Any, output: Any) -> str:
        # The cancel lands while the model is being written to the Library.
        media_jobs.request_cancel(kept[0], path=harness.jobs)
        return ASSET

    discarded: list[str] = []
    harness.route.store_output = store
    harness.route.discard_output = discarded.append
    harness.app = harness.route.app()

    async def scenario() -> str:
        original = media_jobs.create

        def create(job, **kwargs):
            kept.append(job.job_id)
            return original(job, **kwargs)

        media_jobs.create = create  # type: ignore[assignment]
        try:
            job_id = job_id_of(await harness.submit())
            await harness.idle()
        finally:
            media_jobs.create = original  # type: ignore[assignment]
        return job_id

    job = harness.job(run(scenario()))
    assert job["status"] == "canceled" and job["assets"] == []
    assert harness.receipts[-1]["outcome"] == ("cancelled", "cancelled")
    assert discarded == [ASSET]  # the model written during the cancel does not stay in the Library


def test_a_job_canceled_and_then_failed_by_the_engine_is_canceled_in_the_job_and_the_receipt(harness: AsyncHarness) -> None:
    """The requester cancels while the engine runs; the engine then fails. The job and the receipt both say canceled."""
    async def scenario() -> str:
        job_id = job_id_of(await harness.submit())
        await harness.engine_started()
        media_jobs.request_cancel(job_id, path=harness.jobs)
        harness.engine_answer = 500
        harness.let_engine_return()
        await harness.idle()
        return job_id

    job = harness.job(run(scenario()))
    assert job["status"] == "canceled" and job["error"] == mesh_api.CANCELED_BY_REQUESTER
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("cancelled", "cancelled"), "exc": None}]


# --- the gateway stops: the job is failed at the next start, the receipt closed -----------------------------------------

def test_a_gateway_stop_mid_run_fails_the_job_and_closes_the_receipt(harness: AsyncHarness) -> None:
    """The task is cancelled with its loop: the job is failed with a plain reason and the receipt agrees: failed, with the
    reason startup recovery gives a job a dead gateway left."""
    async def scenario() -> str:
        job_id = job_id_of(await harness.submit())
        await harness.engine_started()
        for task in _others():
            task.cancel()
        await asyncio.gather(*_others(), return_exceptions=True)
        return job_id

    job = harness.job(run(scenario()))
    assert job["status"] == "failed" and job["error"] == mesh_api.JOB_STOPPED
    assert harness.receipts == [{"request_id": "req-test", "outcome": ("failed", "gateway_restarted"), "exc": None}]
    assert harness.leases.active_claims() == 0


def test_startup_recovery_fails_a_job_a_dead_gateway_left_and_closes_its_receipt(tmp_path: Path, monkeypatch) -> None:
    """Every orphaned job that names a request closes that request's receipt, whatever kind of job it was (the mode is
    not read), and a receipt that is already closed stays as it is."""
    import proxy

    jobs = tmp_path / "jobs.db"
    receipts = tmp_path / "receipts.db"
    media_jobs.init_db(jobs)
    receipt_store.init_db(receipts)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    real_finalize = receipt_store.finalize
    monkeypatch.setattr(receipt_store, "finalize", lambda receipt, **kw: real_finalize(receipt, path=receipts, **kw))
    for request_id in ("req-dead", "req-other"):
        receipt_store.begin(RouteReceipt(protocol="openai.mesh", intent="mesh.generate", path=MESH_PATH,
                                         request_id=request_id), path=receipts)
    closed = RouteReceipt(protocol="openai.images", intent="image.generate", path="/v1/images", request_id="req-done")
    receipt_store.begin(closed, path=receipts)
    closed.finish("succeeded")
    real_finalize(closed, path=receipts)

    mesh_job = media_jobs.MediaJob(intent="mesh.generate", workflow="mesh.generate.trellis-2", backend="trellis",
                                   request_id="req-dead", status=media_jobs.STATUS_RUNNING, app_id="app-test",
                                   metadata={"mode": mesh_api.JOB_MODE})
    other_kind = media_jobs.MediaJob(intent="scene.identity", request_id="req-other", status=media_jobs.STATUS_RUNNING,
                                     metadata={"mode": "async_something_else"})
    already_closed = media_jobs.MediaJob(intent="scene.identity", request_id="req-done",
                                         status=media_jobs.STATUS_RUNNING, metadata={"mode": "async_something_else"})
    no_request = media_jobs.MediaJob(intent="mesh.generate", request_id="req-never-begun",
                                     status=media_jobs.STATUS_RUNNING, metadata={"mode": mesh_api.JOB_MODE})
    for job in (mesh_job, other_kind, already_closed, no_request):
        media_jobs.create(job, path=jobs)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_RESUME_ON_START", True)

    proxy._recover_media_jobs()

    assert media_jobs.unfinished(path=jobs) == []
    failed = media_jobs.get(mesh_job.job_id, path=jobs)
    assert failed["status"] == "failed" and failed["error"] == "The gateway restarted; this job cannot be resumed"
    for request_id in ("req-dead", "req-other"):
        stored = receipt_store.get(request_id, path=receipts, now_ms=0)
        assert stored["status"] == "failed" and stored["metadata"]["terminal_reason"] == "gateway_restarted"
    assert receipt_store.get("req-done", path=receipts, now_ms=0)["status"] == "succeeded"  # closed before: unchanged
    # A second start changes nothing, and a receipt that was never begun, or no request id at all, is no fault.
    assert receipt_store.close_orphan("req-dead") is False
    assert receipt_store.close_orphan("req-never-begun") is False
    assert receipt_store.close_orphan("") is False


# --- the receipt owner's handoff -------------------------------------------------------------------------------------

def owner_with_log() -> tuple[ReceiptLifecycleOwner, list[tuple[str, str | None]]]:
    closed: list[tuple[str, str | None]] = []
    receipt = RouteReceipt(protocol="openai.mesh", intent="mesh.generate", path=MESH_PATH, request_id="req-o")
    owner = ReceiptLifecycleOwner(
        receipt, begin=lambda r: None,
        finalize=lambda r: closed.append((r.status, r.metadata.get("terminal_reason"))), log=lambda m: None)
    return owner, closed


def test_after_a_handoff_the_handlers_exit_does_not_close_the_receipt_and_settle_does_once() -> None:
    async def scenario() -> list[tuple[str, str | None]]:
        owner, closed = owner_with_log()
        async with owner:
            owner.handoff()
        assert closed == []
        owner.report(status="succeeded", reason="succeeded")
        await owner.settle()
        await owner.settle(RuntimeError("late"))  # a second close is a no-op
        return closed

    assert asyncio.run(scenario()) == [("succeeded", "succeeded")]


@pytest.mark.parametrize("exc, expected", [
    (asyncio.CancelledError(), ("cancelled", "cancelled")),
    (RuntimeError("x"), ("failed", "internal_error")),
    (None, ("failed", "internal_error")),  # nothing reported
])
def test_settle_closes_for_the_exception_it_is_given(exc, expected) -> None:
    async def scenario() -> list[tuple[str, str | None]]:
        owner, closed = owner_with_log()
        await owner.settle(exc)
        return closed

    assert asyncio.run(scenario()) == [expected]


def test_a_closed_receipt_cannot_be_handed_off() -> None:
    async def scenario() -> None:
        owner, _ = owner_with_log()
        await owner.settle()
        with pytest.raises(RuntimeError):
            owner.handoff()

    asyncio.run(scenario())


# --- the lease check in the client: health and observer reads are probes --------------------------------------------

SERVICE_DOMAIN, OBSERVER_DOMAIN = "mesh-dom", "observer-dom"


class _Closing:
    """The real egress client factory over the harness's fake transport; it notes each client and whether it was closed."""

    def __init__(self, harness: AsyncHarness) -> None:
        from gateway import engine_egress

        self.clients: list[httpx.AsyncClient] = []
        self.seen: list[tuple[str, str]] = []
        route_handler = harness.route.handler

        def handler(request: httpx.Request) -> Any:
            self.seen.append((request.method, request.url.path))
            return route_handler(request)

        self.factory = lambda: self.clients.append(
            engine_egress.async_client(transport=_Upstream(handler))) or self.clients[-1]


def _enforce_leases(harness: AsyncHarness, monkeypatch) -> _Closing:
    """Mark the service and its observer as two transition-enabled domains, project the map, and use the real client."""
    import dataclasses

    from fastapi.responses import JSONResponse

    from gateway import engine_egress, transition_gate

    for service_id, domain in (("trellis", SERVICE_DOMAIN), ("vram-observer", OBSERVER_DOMAIN)):
        harness.route.services[service_id]["residency"] = {"domain": domain, "transition": {"enabled": True, "activation": "pass_through"}}
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)
    engine_egress.publish(harness.route.services)

    def refusal(service_id: str) -> Any:
        try:
            transition_gate.require_egress_lease(transition_gate.policy_for(service_id, harness.route.services), service_id)
        except transition_gate.TransitionUnavailable:
            return JSONResponse({"error": {"code": "transition_unsupported"}}, status_code=503)
        return None

    harness.route.egress_refusal = refusal
    closing = _Closing(harness)
    deps = dataclasses.replace(harness.route.deps(), client_factory=closing.factory)
    harness.app = FastAPI()
    mesh_api.register_mesh_routes(harness.app, deps)
    return closing


def test_an_unleased_send_to_an_enabled_domain_is_refused_after_a_probed_health_read(harness: AsyncHarness, monkeypatch) -> None:
    """With the lease check in the client, the health read is a probe: it goes out, the send is then refused with the
    gateway's refusal, no job exists and the client is closed. Failed before the fix: the unmarked health read raised
    the gate's refusal inside ``_healthy``, which escaped as a bare 503 and left the client open."""
    from gateway import transition_gate

    closing = _enforce_leases(harness, monkeypatch)

    async def scenario() -> None:
        token = transition_gate._EGRESS_LEASES.set(())
        try:
            response = await harness.submit()
        finally:
            transition_gate._EGRESS_LEASES.reset(token)
        assert response.status_code == 503 and response.json()["error"]["code"] == "transition_unsupported"
        assert closing.seen == [("GET", "/health")], "the health read went out; nothing else did"
        assert harness.all_jobs() == [], "a refused send leaves no job behind"
        assert harness.engine.events == []
        assert harness.route.egress_asked == ["trellis"]
        assert [client.is_closed for client in closing.clients] == [True]

    run(scenario())


@pytest.mark.parametrize("prefer", [True, False], ids=["async", "sync"])
def test_the_observer_read_in_another_enabled_domain_needs_no_lease(harness: AsyncHarness, monkeypatch, prefer: bool) -> None:
    """The service's lease is held; the observer is in its own enabled domain, with none held. Its read is a probe of
    the path the service's ``resource_capacity.observer`` names, so it goes out, and the run reaches the engine."""
    from gateway import transition_gate

    harness.hold_engine = False
    closing = _enforce_leases(harness, monkeypatch)

    async def scenario() -> None:
        token = transition_gate._EGRESS_LEASES.set((SimpleNamespace(key=SERVICE_DOMAIN, released=False),))
        try:
            response = await harness.submit(prefer=prefer)
            assert response.status_code == (202 if prefer else 200), response.text
            await harness.idle()
        finally:
            transition_gate._EGRESS_LEASES.reset(token)
        assert ("GET", "/facts") in closing.seen, "the observer read went out"
        assert ("POST", "/generate") in closing.seen, "and the run reached the engine"
        assert all(client.is_closed for client in closing.clients)

    run(scenario())


# --- discovery: a client built from the catalog asks for the job ----------------------------------------------------

def test_a_request_built_from_the_async_catalog_entry_carries_the_prefer_header() -> None:
    import base64

    import proxy
    from gateway.workflow_call import build_request
    from gateway.workflow_openapi import _execution

    (record,) = [r for r in proxy.GATEWAY_WORKFLOW_REGISTRY.records() if r.id == "mesh.generate.trellis-2"]
    execution = _execution(record, None)
    assert execution["recommended"] == "async"
    picture = "data:image/png;base64," + base64.b64encode(PNG).decode()

    async def build(route: dict) -> Any:
        return await build_request(lambda *_a: None, None, None, {"intent": record.intent}, route, {"image": picture},
                                   wrap_intent=None)

    route = execution["async"]
    # As every client reads an async route: its submit, with the route's fields.
    submitted = asyncio.run(build({**route["submit"], "fields": route["fields"]}))
    assert (submitted.method, submitted.path) == ("POST", MESH_PATH)
    assert submitted.headers == {"Prefer": "respond-async"}
    assert [field for field, _ in submitted.files] == ["image"]
    # The sync entry is the same route without the header.
    plain = asyncio.run(build(execution["sync"]))
    assert plain.path == MESH_PATH and plain.headers is None

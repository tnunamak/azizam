"""Contract tests for five run-core defects found by a whole-diff review (builder K4).

Each test drives ``WorkflowRunCore`` over the real SQLite job store, with the in-memory port of tests/test_workflow_run.py.
While a defect stands, its test is a strict xfail; the fix removes the mark.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from gateway import generated_defaults, log_scrub, media_jobs, result_bundle
from gateway.request_spool import JobSpool
from gateway.result_bundle import ResultLimits
from gateway.workflow_run import WorkflowJobService, WorkflowRunCore, refusal_body, view_body
from gateway.workflow_runtime_types import (
    IDEMPOTENCY_CONFLICT, ContractError, RunJobView, RunRefused,
)
from tests.test_workflow_run import (  # noqa: F401 - the db fixture is used by name
    Caller, Port, Projections, db, job_rows, make_core, request, run, snapshot,
)

MARKER = "K4-SECRET-MARKER-7f3a91"


@pytest.fixture
def marker():
    log_scrub.remember(MARKER, "k4-test-source")
    yield MARKER
    log_scrub.forget("k4-test-source")


# ------------------------------------------------------------------------------------------------------ item 1


def test_an_expired_holder_cannot_replay_a_changed_identity(db, tmp_path):
    snap = snapshot()
    core, port, _ = make_core(db, tmp_path, snap, claim_lease_s=0.0, claim_wait_s=0.0)
    seen = {}

    async def scenario():
        original = core._decide_and_create

        async def paused(req, caller, snapshot_, fence):
            # Holder A (body A) is inside create when B (body B) takes the expired key over and commits.
            other = WorkflowRunCore(snapshots=core._snapshots, projections=Projections(), port=port, spool=JobSpool(tmp_path),
                                    path=db, generate=core._generate, claim_lease_s=0.0, claim_wait_s=0.0)
            await asyncio.sleep(0.01)
            seen["b"] = await other.submit_run(request(inputs={"prompt": "body B"}), Caller(), snapshot=snap)
            return await original(req, caller, snapshot_, fence)

        core._decide_and_create = paused
        return await core.submit_run(request(inputs={"prompt": "body A"}), Caller(), snapshot=snap)

    answer = run(scenario())
    assert isinstance(seen["b"], RunJobView)
    assert isinstance(answer, RunRefused) and answer.code == IDEMPOTENCY_CONFLICT
    assert len(job_rows(db)) == 1                                   # only B's job exists


# ------------------------------------------------------------------------------------------------------ item 2


class CountingPort(Port):
    """Admission by the number of queued or running jobs, as the real port does, with a limit of one."""

    limit = 1

    def admission_refusal(self, resource_class, caller, *, count_queue):
        if not count_queue:
            return None
        with sqlite3.connect(self.db) as conn:
            held = conn.execute("SELECT COUNT(*) FROM media_jobs WHERE status IN ('queued','running')").fetchone()[0]
        return RunRefused(503, "queue_full", "the queue is full", retry_after_s=1) if held >= self.limit else None


def test_concurrent_submits_respect_a_job_limit_of_one(db, tmp_path):
    snap = snapshot()
    port = CountingPort(db)
    entered, release = threading.Event(), threading.Event()
    first_call = []

    def generate(inputs, given, **kw):
        if not first_call:                       # the first create pauses inside its open write transaction
            first_call.append(1)
            entered.set()
            assert release.wait(10)
        return generated_defaults.resolve_generated(inputs, given, source=lambda low, high: 7, **kw)

    core = WorkflowRunCore(snapshots=type("P", (), {"current": lambda self: snap})(), projections=Projections(), port=port,
                           spool=JobSpool(tmp_path), path=db, generate=generate)
    answers = []

    def submit(name):
        answers.append(run(core.submit_run(request(idempotency_key=None, inputs={"prompt": name}), Caller(), snapshot=snap)))

    first = threading.Thread(target=submit, args=("one",))
    first.start()
    assert entered.wait(10)
    second = threading.Thread(target=submit, args=("two",))
    second.start()
    second.join(0.5)                             # the second has passed any early admission and waits on the lock
    release.set()
    first.join(20)
    second.join(20)
    assert len(answers) == 2
    with sqlite3.connect(db) as conn:
        held = conn.execute("SELECT COUNT(*) FROM media_jobs WHERE status IN ('queued','running')").fetchone()[0]
    assert held <= 1
    assert sum(isinstance(a, RunRefused) for a in answers) == 1


# ------------------------------------------------------------------------------------------------------ item 3


class ReceiptPort(Port):
    def __init__(self, db: Path) -> None:
        super().__init__(db)
        self.closed: list[tuple[str, str, str]] = []

    async def close_receipt(self, request_id, status, reason):
        self.closed.append((request_id, status, reason))


class Retryable(Exception):
    code, category, retry_reason, retry_after_s = "upstream_job_failed", "upstream", "busy", 0


def _break_caller(core, port):
    def caller_from_payload(payload):
        raise ValueError(f"caller rebuild failed {MARKER}")
    port.caller_from_payload = caller_from_payload


def _break_dispatch(core, port):
    def binding_problems(binding, available):
        raise RuntimeError(f"dispatch failed {MARKER}")
    core._projections.binding_problems = binding_problems


def _break_retry(core, port):
    async def failing(ctx):
        port.engine_calls += 1
        raise Retryable("engine busy")

    async def sleeping(seconds):
        raise RuntimeError(f"retry wait failed {MARKER}")

    port.run_adapter = failing
    core._sleep = sleeping


@pytest.mark.parametrize("breaker", [_break_caller, _break_dispatch, _break_retry],
                         ids=["caller_reconstruction", "dispatch", "retry_bookkeeping"])
def test_an_exception_outside_the_adapter_settles_the_job(db, tmp_path, marker, breaker):
    snap = snapshot()
    port = ReceiptPort(db)
    core, port, _ = make_core(db, tmp_path, snap, port)
    view = run(core.submit_run(request(request_id="req-k4"), Caller(), snapshot=snap))
    breaker(core, port)
    try:
        run(core.execute_run(view.job_id))
    except Exception:       # noqa: BLE001 - whether the worker re-raises is not the contract; the stored state is
        pass
    job = media_jobs.get(view.job_id, audience="internal", path=db)
    assert job["status"] == "failed"
    assert job["error_code"] == "internal_error"
    assert MARKER not in json.dumps(job)
    assert view.job_id not in port.payloads                         # the payload is released
    assert [c[:2] for c in port.closed] == [("req-k4", "failed")]    # and the receipt is closed


# ------------------------------------------------------------------------------------------------------ item 4


def test_a_committed_bundle_wins_over_a_pending_cancel_at_recovery(db, tmp_path):
    snap = snapshot()
    core, port, _ = make_core(db, tmp_path, snap)
    view = run(core.submit_run(request(), Caller(), snapshot=snap))
    media_jobs.update_status(view.job_id, "running", path=db)
    media_jobs.request_cancel(view.job_id, path=db)                  # running: only the flag is set
    assert media_jobs.get(view.job_id, audience="internal", path=db)["metadata"]["cancel_requested_at"]
    limits = ResultLimits()
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, view.job_id, 1, "result", b"{}", limits=limits)
        result_bundle.publish_in(conn, view.job_id, 1, limits=limits)
        conn.commit()
    port.engine_calls = 0
    assert core.recover_runs(0).counts == {"replay": 1}
    job = media_jobs.get(view.job_id, audience="internal", path=db)
    assert job["status"] == "succeeded"
    assert port.engine_calls == 0
    assert view.job_id not in port.payloads


# ------------------------------------------------------------------------------------------------------ item 5


def _stored_failure_and_view(db, tmp_path, port_change):
    snap = snapshot()
    core, port, _ = make_core(db, tmp_path, snap)
    view = run(core.submit_run(request(), Caller(), snapshot=snap))
    port_change(port)
    run(core.execute_run(view.job_id))
    stored = media_jobs.get(view.job_id, audience="internal", path=db)
    shown = WorkflowJobService(port=port, path=db).get(view.job_id, Caller())
    return json.dumps(stored), json.dumps(view_body(shown))


def test_a_dispatch_refusal_message_is_scrubbed(db, tmp_path, marker):
    stored, shown = _stored_failure_and_view(
        db, tmp_path, lambda port: setattr(port, "refusal", RunRefused(503, "queue_full", f"full {MARKER}")))
    assert MARKER not in stored and MARKER not in shown
    assert "queue_full" in stored                                   # the failure is still the dispatch refusal


def test_a_refusal_body_is_scrubbed(marker):
    body = refusal_body(RunRefused(422, "invalid_inputs", f"bad {MARKER}",
                                   problems=(ContractError(("inputs", "prompt"), "value_invalid", f"seen {MARKER}"),)))
    assert MARKER not in json.dumps(body)
    assert body["error"]["code"] == "invalid_inputs"


def test_a_generated_input_refusal_is_scrubbed(db, tmp_path, marker):
    snap = snapshot()
    core, _, _ = make_core(db, tmp_path, snap)

    def unavailable(inputs, given, **kw):
        raise generated_defaults.GeneratedValueUnavailable(f"cannot draw {MARKER}", erased=False)

    core._generate = unavailable
    refused = run(core.submit_run(request(idempotency_key="gen"), Caller(), snapshot=snap))
    assert isinstance(refused, RunRefused) and refused.code == "invalid_inputs"
    assert MARKER not in json.dumps(refusal_body(refused))

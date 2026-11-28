"""Contract tests for five run-core defects found by a whole-diff review (builder K4).

Each test drives ``WorkflowRunCore`` over the real SQLite job store, with the in-memory port of tests/test_workflow_run.py.
K4b turns the two reviews of these tests (and the K3 recheck) into contract tests: each overlap is proven by a signal, not
a sleep; each scrubbing sink has its own collected case; a wrong repair (a blanket catch, a global cancel change, a lock on
one core instance) fails one of them.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from gateway import generated_defaults, log_scrub, media_jobs, result_bundle
from gateway.request_spool import FileRequestSpool, JobSpool
from gateway.result_bundle import ResultLimits
from gateway.workflow_kernel import run_identity
from gateway.workflow_run import WorkflowJobService, WorkflowRunCore, job_view, refusal_body, view_body
from gateway.workflow_runtime_types import (
    IDEMPOTENCY_CONFLICT, IDEMPOTENCY_IN_PROGRESS, ContractError, RunFailure, RunJobView, RunRefused,
)
from tests.test_workflow_run import (  # noqa: F401 - the db fixture is used by name
    PNG_HEAD, Caller, Port, Projections, db, job_rows, make_core, request, run, snapshot,
)

MARKER = "K4-SECRET-MARKER-7f3a91"


@pytest.fixture
def marker():
    log_scrub.remember(MARKER, "k4-test-source")
    yield MARKER
    log_scrub.forget("k4-test-source")


# ------------------------------------------------------------------------------------------------------ item 1


def _takeover_scenario(db, tmp_path, *, b_inputs, b_creates):
    """Holder A (body A) is inside create when B takes the expired key over. B either commits its job (``b_creates``) or
    only holds the claim."""
    snap = snapshot()
    core, port, draws = make_core(db, tmp_path, snap, claim_lease_s=0.0, claim_wait_s=0.0)
    seen = {}

    async def scenario():
        original = core._decide_and_create

        async def paused(req, caller, snapshot_, fence):
            other = WorkflowRunCore(snapshots=core._snapshots, projections=Projections(), port=port, spool=JobSpool(tmp_path),
                                    path=db, generate=core._generate, claim_lease_s=0.0, claim_wait_s=0.0)
            await asyncio.sleep(0.01)
            b_request = request(inputs=b_inputs)
            if b_creates:
                seen["b"] = await other.submit_run(b_request, Caller(), snapshot=snap)
            else:   # B holds the key and has created nothing yet
                taken = media_jobs.claim_run_key(
                    owner_scope="app-1", maker="person-1", key="key-1", holder="holder-b", lease_s=0.0,
                    identity=run_identity("app-1", "person-1", b_request), now=time.time() + 5, path=db)
                seen["b"] = taken.outcome
            return await original(req, caller, snapshot_, fence)

        core._decide_and_create = paused
        return await core.submit_run(request(inputs={"prompt": "body A"}), Caller(), snapshot=snap)

    return run(scenario()), seen, draws


def test_an_expired_holder_replays_the_job_of_a_takeover_with_the_same_identity(db, tmp_path):
    answer, seen, draws = _takeover_scenario(db, tmp_path, b_inputs={"prompt": "body A"}, b_creates=True)
    assert isinstance(seen["b"], RunJobView) and seen["b"].disposition == "new"
    assert isinstance(answer, RunJobView) and answer.disposition == "replay"
    assert answer.job_id == seen["b"].job_id
    assert len(job_rows(db)) == 1 and len(draws) == 1                # one job, one draw: A drew and stored nothing
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT job_id FROM workflow_run_claim").fetchall() == [(seen["b"].job_id,)]


def test_an_expired_holder_cannot_replay_a_changed_identity(db, tmp_path):
    answer, seen, draws = _takeover_scenario(db, tmp_path, b_inputs={"prompt": "body B"}, b_creates=True)
    assert isinstance(seen["b"], RunJobView)
    assert isinstance(answer, RunRefused)
    assert (answer.http_status, answer.code) == (409, IDEMPOTENCY_CONFLICT)
    assert [row[0] for row in job_rows(db)] == [seen["b"].job_id] and len(draws) == 1     # only B's job and draw exist
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT job_id FROM workflow_run_claim").fetchall() == [(seen["b"].job_id,)]


def test_a_pending_takeover_with_a_changed_identity_is_a_conflict_not_in_progress(db, tmp_path):
    answer, seen, draws = _takeover_scenario(db, tmp_path, b_inputs={"prompt": "body B"}, b_creates=False)
    assert seen["b"] == "held"
    assert isinstance(answer, RunRefused) and (answer.http_status, answer.code) == (409, IDEMPOTENCY_CONFLICT)
    assert job_rows(db) == [] and draws == []                         # A drew and stored nothing
    with sqlite3.connect(db) as conn:                                 # B's claim is untouched
        assert conn.execute("SELECT holder, job_id FROM workflow_run_claim").fetchall() == [("holder-b", None)]


def test_a_pending_takeover_with_the_same_identity_is_in_progress(db, tmp_path):
    answer, seen, draws = _takeover_scenario(db, tmp_path, b_inputs={"prompt": "body A"}, b_creates=False)
    assert seen["b"] == "held"
    assert isinstance(answer, RunRefused) and (answer.http_status, answer.code) == (409, IDEMPOTENCY_IN_PROGRESS)
    assert answer.retry_after_s
    assert job_rows(db) == [] and draws == []


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


@pytest.fixture
def write_lock_attempts(monkeypatch):
    """Counts the ``BEGIN IMMEDIATE`` statements the job store starts. SQLite's trace hook fires when a statement starts
    running, before it waits for the write lock, so the second count is the second submit's attempt to take the lock."""
    attempts, second = [], threading.Event()
    real = media_jobs.connect

    def traced(*args, **kwargs):
        conn = real(*args, **kwargs)

        def trace(sql):
            if sql.startswith("BEGIN IMMEDIATE"):
                attempts.append(sql)
                if len(attempts) >= 2:
                    second.set()
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(media_jobs, "connect", traced)
    return second


@pytest.mark.parametrize("separate_cores", [False, True], ids=["one_core", "two_cores_one_database"])
def test_concurrent_submits_respect_a_job_limit_of_one(db, tmp_path, write_lock_attempts, separate_cores):
    snap = snapshot()
    ports = [CountingPort(db), CountingPort(db)]
    entered, release = threading.Event(), threading.Event()
    first_call = []

    def generate(inputs, given, **kw):
        if not first_call:                       # the first create pauses inside its open write transaction
            first_call.append(1)
            entered.set()
            assert release.wait(10)
        return generated_defaults.resolve_generated(inputs, given, source=lambda low, high: 7, **kw)

    def build(port):
        return WorkflowRunCore(snapshots=type("P", (), {"current": lambda self: snap})(), projections=Projections(), port=port,
                               spool=JobSpool(tmp_path), path=db, generate=generate)

    cores = [build(ports[0]), build(ports[1])] if separate_cores else [build(ports[0])] * 2
    answers, errors = {}, []

    def submit(index, name):
        try:
            answers[name] = run(cores[index].submit_run(
                request(idempotency_key=None, inputs={"prompt": name}), Caller(), snapshot=snap))
        except BaseException as exc:      # noqa: BLE001 - surfaced by the assertion below, never lost in a thread
            errors.append(exc)

    first = threading.Thread(target=submit, args=(0, "one"))
    second = threading.Thread(target=submit, args=(1, "two"))
    try:
        first.start()
        assert entered.wait(10)
        second.start()
        # The overlap is observed, not assumed: the second submit asked for the write lock the first one holds.
        assert write_lock_attempts.wait(10), "the second submit never reached the write lock"
        assert first.is_alive() and second.is_alive()
    finally:
        release.set()
        first.join(20)
        if second.ident is not None:
            second.join(20)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert isinstance(answers["one"], RunJobView)                      # the first holder of the lock wins
    refused = answers["two"]
    assert isinstance(refused, RunRefused)
    assert (refused.http_status, refused.code, refused.retry_after_s) == (503, "queue_full", 1)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT status FROM media_jobs").fetchall() == [("queued",)]
    assert sum(len(port.payloads) for port in ports) == 1              # the refused submit kept no payload or file
    assert [job for job in (tmp_path / "jobs").iterdir()] == [tmp_path / "jobs" / answers["one"].job_id] \
        if (tmp_path / "jobs").exists() else True


# ------------------------------------------------------------------------------------------------------ item 3


class ReceiptPort(Port):
    """Records every receipt the core closes: at the end of a worker (async) and by startup recovery (sync)."""

    def __init__(self, db: Path) -> None:
        super().__init__(db)
        self.closed: list[tuple[str, str, str]] = []
        self.recovered: list[tuple[str, str, str]] = []

    async def close_receipt(self, request_id, status, reason):
        self.closed.append((request_id, status, reason))

    def close_recovered_receipt(self, request_id, status, reason):
        self.recovered.append((request_id, status, reason))


class Retryable(Exception):
    code, category, retry_reason, retry_after_s = "upstream_job_failed", "upstream", "busy", 0


def _break_caller(core, port, reached):
    def caller_from_payload(payload):
        reached.append("caller")
        raise ValueError(f"caller rebuild failed {MARKER}")
    port.caller_from_payload = caller_from_payload


def _break_dispatch(core, port, reached):
    def binding_problems(binding, available):
        reached.append("dispatch")
        raise RuntimeError(f"dispatch failed {MARKER}")
    core._projections.binding_problems = binding_problems


def _break_retry(core, port, reached):
    async def failing(ctx):
        port.engine_calls += 1
        raise Retryable("engine busy")

    async def sleeping(seconds):
        reached.append("retry")
        raise RuntimeError(f"retry wait failed {MARKER}")

    port.run_adapter = failing
    core._sleep = sleeping


def _break_classify(core, port, reached):       # before the adapter envelope is even built
    def load_payload(job_id):
        reached.append("classify")
        raise RuntimeError(f"payload read failed {MARKER}")
    port.load_payload = load_payload


def _stage_upload(tmp_path):
    spool = FileRequestSpool(tmp_path, max_bytes=1000, request_id="reqk4upload")

    async def stage():
        async def chunks():
            yield PNG_HEAD + b"abcdef"
        return await spool.stage("photo", chunks(), filename="a.png", wire_media_type="image/png")

    return asyncio.run(stage())


@pytest.mark.parametrize("breaker", [_break_caller, _break_dispatch, _break_retry, _break_classify],
                         ids=["caller_reconstruction", "dispatch", "retry_bookkeeping", "before_the_adapter_envelope"])
def test_an_exception_outside_the_adapter_settles_the_job(db, tmp_path, marker, breaker):
    snap = snapshot()
    port = ReceiptPort(db)
    core, port, _ = make_core(db, tmp_path, snap, port)
    view = run(core.submit_run(request(request_id="req-k4", media={"photo": (_stage_upload(tmp_path),)}), Caller(), snapshot=snap))
    assert isinstance(view, RunJobView) and (tmp_path / "jobs" / view.job_id).is_dir()    # the upload is adopted
    reached: list[str] = []
    breaker(core, port, reached)
    try:
        run(core.execute_run(view.job_id))
    except Exception:       # noqa: BLE001 - whether the worker re-raises is not the contract; the stored state is
        pass
    assert len(reached) >= 1                                       # the fault was injected where the case says
    job = media_jobs.get(view.job_id, audience="internal", path=db)
    assert job["status"] == "failed"
    assert job["error_code"] == "internal_error"
    assert MARKER not in json.dumps(job)
    assert view.job_id not in port.payloads                         # the payload is released
    assert not (tmp_path / "jobs" / view.job_id).exists()           # and so is the adopted upload on disk
    assert port.closed == [("req-k4", "failed", "internal_error")]  # the receipt closes once, with the job's own reason


def test_a_cancelled_worker_leaves_the_job_unfinished_and_its_receipt_open(db, tmp_path):
    snap = snapshot()
    port = ReceiptPort(db)
    core, port, _ = make_core(db, tmp_path, snap, port)
    view = run(core.submit_run(request(request_id="req-k4", media={"photo": (_stage_upload(tmp_path),)}), Caller(), snapshot=snap))

    async def cancelled(ctx):
        port.engine_calls += 1
        raise asyncio.CancelledError

    port.run_adapter = cancelled
    with pytest.raises(asyncio.CancelledError):
        run(core.execute_run(view.job_id))
    assert port.engine_calls == 1
    assert media_jobs.get(view.job_id, audience="internal", path=db)["status"] == "running"
    assert view.job_id in port.payloads and (tmp_path / "jobs" / view.job_id).is_dir()    # recovery still needs both
    assert port.closed == []


# ------------------------------------------------------------------------------------------------------ item 4


def _commit_bundle(db, job_id):
    limits = ResultLimits()
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, job_id, 1, "result", b"{}", limits=limits)
        result_bundle.publish_in(conn, job_id, 1, limits=limits)
        conn.commit()


def test_a_committed_bundle_wins_over_a_pending_cancel_at_recovery(db, tmp_path):
    snap = snapshot()
    port = ReceiptPort(db)
    core, port, _ = make_core(db, tmp_path, snap, port)
    view = run(core.submit_run(request(request_id="req-k4", media={"photo": (_stage_upload(tmp_path),)}), Caller(), snapshot=snap))
    media_jobs.update_status(view.job_id, "running", path=db)
    media_jobs.request_cancel(view.job_id, path=db)                  # running: only the flag is set
    assert media_jobs.get(view.job_id, audience="internal", path=db)["metadata"]["cancel_requested_at"]
    _commit_bundle(db, view.job_id)
    port.engine_calls = 0
    port.scheduled.clear()
    assert core.recover_runs(0).counts == {"replay": 1}
    job = media_jobs.get(view.job_id, audience="internal", path=db)
    assert job["status"] == "succeeded"
    assert [(ref.output, ref.media_type) for ref in job_view(job, db).results] == [("result", result_bundle.JSON_MEDIA_TYPE)]
    assert port.engine_calls == 0 and port.scheduled == []           # no engine call, no new schedule
    assert view.job_id not in port.payloads
    assert not (tmp_path / "jobs" / view.job_id).exists()            # the adopted upload is gone from disk
    assert port.recovered == [("req-k4", "succeeded", "succeeded")]  # and the receipt closes as succeeded
    assert port.closed == []


def test_a_running_job_with_no_bundle_and_a_pending_cancel_does_not_succeed_at_recovery(db, tmp_path):
    snap = snapshot()
    port = ReceiptPort(db)
    core, port, _ = make_core(db, tmp_path, snap, port)
    view = run(core.submit_run(request(request_id="req-k4"), Caller(), snapshot=snap))
    media_jobs.update_status(view.job_id, "running", path=db)
    media_jobs.request_cancel(view.job_id, path=db)
    port.engine_calls = 0
    port.scheduled.clear()
    report = core.recover_runs(0)
    job = media_jobs.get(view.job_id, audience="internal", path=db)
    assert job["status"] != "succeeded" and job["status"] in ("canceled", "failed")
    assert report.failed_job_ids == (view.job_id,) and job["error_code"] == "cancel_unconfirmed"
    assert port.engine_calls == 0 and port.scheduled == []           # a pending cancel is never run again
    assert port.recovered == []                                       # recovery closes only what a result settled


# ------------------------------------------------------------------------------------------------------ item 5


def _stored_failure_and_view(db, tmp_path, port_change):
    snap = snapshot()
    core, port, _ = make_core(db, tmp_path, snap)
    view = run(core.submit_run(request(), Caller(), snapshot=snap))
    port_change(port)
    run(core.execute_run(view.job_id))
    stored = media_jobs.get(view.job_id, audience="internal", path=db)
    shown = WorkflowJobService(port=port, path=db).get(view.job_id, Caller())
    return stored, shown


def test_a_dispatch_refusal_message_is_scrubbed_in_the_stored_job_and_the_view(db, tmp_path, marker):
    stored, shown = _stored_failure_and_view(
        db, tmp_path, lambda port: setattr(port, "refusal", RunRefused(503, "queue_full", f"full {MARKER}")))
    assert stored["status"] == "failed" and stored["error_code"] == "queue_full"   # still the dispatch refusal
    assert MARKER not in json.dumps(stored) and MARKER not in json.dumps(view_body(shown))
    assert "full" in stored["error"] and "full" in shown.failure.message             # the useful words survive
    assert MARKER not in shown.failure.message                                       # the typed view is clean too


def test_a_job_view_body_scrubs_a_failure_message_it_is_handed(marker):
    view = RunJobView(job_id="j", status="failed", disposition="new", workflow_id="demo.run", record_revision=1,
                      binding_key="fake", created_at_s=1, updated_at_s=2,
                      failure=RunFailure(code="queue_full", category="capacity", message=f"engine said {MARKER} twice",
                                         retryable=True, upstream_status=None))
    body = view_body(view)
    assert MARKER not in json.dumps(body)
    assert body["failure"]["message"].startswith("engine said") and body["failure"]["message"].endswith("twice")
    assert (body["failure"]["code"], body["failure"]["retryable"]) == ("queue_full", True)


def _refusal(marker_in: str):
    problem = ContractError(("inputs", "prompt"), "value_invalid", f"seen {MARKER}" if marker_in == "problem" else "seen once")
    return RunRefused(422, "invalid_inputs", f"bad {MARKER}" if marker_in == "message" else "bad input", problems=(problem,))


@pytest.mark.parametrize("sink", ["message", "problem"])
def test_a_refusal_body_is_scrubbed_in_each_message_it_carries(marker, sink):
    body = refusal_body(_refusal(sink))
    assert MARKER not in json.dumps(body)
    error = body["error"]
    assert error["code"] == "invalid_inputs"
    assert error["message"].startswith("bad")
    [problem] = error["problems"]
    assert (problem["code"], problem["path"]) == ("value_invalid", "/inputs/prompt")
    assert problem["message"].startswith("seen")                     # the nonsecret words survive


def test_a_generated_input_refusal_is_scrubbed_in_the_typed_outcome_and_the_body(db, tmp_path, marker):
    snap = snapshot()
    core, _, _ = make_core(db, tmp_path, snap)
    drawn = []

    def unavailable(inputs, given, **kw):
        drawn.append(1)
        raise generated_defaults.GeneratedValueUnavailable(f"cannot draw {MARKER}", erased=False)

    core._generate = unavailable
    refused = run(core.submit_run(request(idempotency_key="gen"), Caller(), snapshot=snap))
    assert drawn == [1]                                             # the injected generator was reached
    assert isinstance(refused, RunRefused) and (refused.http_status, refused.code) == (422, "invalid_inputs")
    [problem] = refused.problems
    assert (problem.code, problem.pointer) == ("generated_value_invalid", "/inputs")
    assert MARKER not in refused.message and MARKER not in problem.message         # the typed outcome itself is clean
    assert "cannot draw" in problem.message                                          # and still says what went wrong
    assert MARKER not in json.dumps(refusal_body(refused))
    assert job_rows(db) == []


# ------------------------------------------------------------------------------------------------------ K3 recheck P2


def _status(path, request_id):
    with sqlite3.connect(path) as conn:      # the stored status: the read API shows an old open receipt as stale
        return conn.execute("SELECT status FROM receipts WHERE request_id=?", (request_id,)).fetchone()[0]


def _receipt(request_id, *, accepted_at_ms, path):
    from gateway import receipt_store
    from gateway.receipts import RouteReceipt
    receipt_store.init_db(path)
    receipt_store.begin(RouteReceipt(protocol="p", intent="i", path="/row", request_id=request_id), now_ms=accepted_at_ms, path=path)


def test_the_startup_sweep_closes_only_the_receipts_open_at_its_snapshot_and_that_request_finalizes_as_succeeded(tmp_path):
    from gateway import receipt_store
    path = tmp_path / "receipts.db"
    _receipt("req-old", accepted_at_ms=2_001_000, path=path)          # a timestamp above the clock below: no matter
    snapshot = receipt_store.open_request_ids(path=path)
    assert snapshot == {"req-old"}
    _receipt("req-live", accepted_at_ms=1_000, path=path)             # opened after the snapshot, with an older timestamp
    assert receipt_store.close_orphans_on_paths(["/row"], among=snapshot, path=path) == 1
    assert _status(path, "req-old") == "failed"              # the stopped gateway's receipt
    assert _status(path, "req-live") == "in_progress"        # the live request's is not swept
    assert receipt_store.close_orphan("req-live", among=snapshot, path=path) is False
    done = {"request_id": "req-live", "status": "succeeded", "updated_at_ms": 5_000,
            "metadata": {"terminal_reason": "succeeded"}}
    assert receipt_store.finalize(done, path=path) is True                           # its own finalize still wins
    assert _status(path, "req-live") == "succeeded"


def test_a_recovered_run_closes_its_receipt_with_the_job_outcome_and_the_snapshot_still_applies(tmp_path):
    from gateway import receipt_store
    path = tmp_path / "receipts.db"
    _receipt("req-old", accepted_at_ms=2_001_000, path=path)
    snapshot = receipt_store.open_request_ids(path=path)
    _receipt("req-live", accepted_at_ms=1_000, path=path)
    assert receipt_store.close_recovered("req-live", "succeeded", "succeeded", among=snapshot, path=path) is False
    assert receipt_store.close_recovered("req-old", "succeeded", "succeeded", among=snapshot, path=path) is True
    old = receipt_store.get("req-old", path=path)
    assert old["status"] == "succeeded" and old["metadata"]["terminal_reason"] == "succeeded"
    assert receipt_store.close_recovered("req-old", "failed", "gateway_restarted", among=snapshot, path=path) is False   # closed stays closed
    assert _status(path, "req-live") == "in_progress"


def test_the_snapshot_holds_in_progress_receipts_only(tmp_path):
    from gateway import receipt_store
    path = tmp_path / "receipts.db"
    _receipt("req-open", accepted_at_ms=1_000, path=path)
    _receipt("req-done", accepted_at_ms=1_000, path=path)
    assert receipt_store.close_orphan("req-done", path=path) is True
    assert receipt_store.open_request_ids(path=path) == {"req-open"}

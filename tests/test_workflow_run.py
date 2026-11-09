"""The run core, the job service and the request spool (CODE_TO_DATA_S4_S8 sections 5 and 7; builder C).

Every test goes through ``WorkflowRunCore`` / ``WorkflowJobService`` over a real job store, with a fake port whose engine
counter proves "no engine call". The four contract tests of section 7.2 are the first block.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from gateway import generated_defaults, media_jobs, request_source, result_bundle
from gateway.job_envelope import build_job_envelope
from gateway.ownership import stored_owner_key
from gateway.request_spool import FileRequestSpool, JobSpool
from gateway.result_bundle import Reader, ResultLimits
from gateway.workflow_kernel import run_identity
from gateway.workflow_run import (
    AdapterIdentity, BindingAvailability, RunContext, WorkflowJobService, WorkflowRunCore, queued_jobs_without_v2_envelope,
)
from gateway.workflow_runtime_types import (
    ExecutionProfile, JobNotFound, MediaAccept, MediaRef, ResultUnavailable, RunJobView, RunRefused, RunRequest,
)
from tests.workflow_runtime_fakes import FACTS, FakeSnapshot, entry

CONTRACT = {
    "schema_version": 2, "id": "demo.run", "version": "1", "intent": "demo.make",
    "inputs": {"prompt": {"type": "string", "required": True},
               "seed": {"type": "integer", "minimum": 0, "maximum": 1000,
                        "generate": {"generator": "random_integer", "minimum": 0, "maximum": 1000}},
               "photo": {"type": "asset", "accepts": ["image/png"]}},
    "outputs": {"result": {"media_type": "application/json"}},
    "bindings": {"fake": {}, "other": {}},
}


@dataclass(frozen=True)
class Caller:
    scope: str = "app-1"
    person: str = "person-1"
    owner: bool = False
    gone: bool = False


class Provider:
    def __init__(self, snap: FakeSnapshot) -> None:
        self.snap = snap

    def current(self) -> FakeSnapshot:
        return self.snap


class Projections:
    """A test double of I3: the two functions the core calls, over the entry's bindings."""

    def entry_problems(self, e, available, *, binding_key=None):
        if e.facts is None:
            return ("needs_policy",)
        key = binding_key or next(iter(e.bindings))
        if key not in e.bindings:
            return ("binding_unknown",)
        problem = available.binding_problem(e.bindings[key])
        return (problem,) if problem else ()

    def binding_problems(self, binding, available):
        problem = available.binding_problem(binding)
        return (problem,) if problem else ()

    def execution_profile(self, e, binding_key, *, limits):
        return ExecutionProfile(adapter_id=binding_key, adapter_facet="", resource_class="fake-class",
                                accepts={"photo": MediaAccept(("image/png",), max_bytes=1000, min_items=0, max_items=1)})


class Port:
    """The main-module side, in memory. ``engine_calls`` counts every call that would reach an engine."""

    def __init__(self, db: Path) -> None:
        self.db = db
        self.blocked: list[str] = []
        self.problem: str | None = None
        self.refusal: RunRefused | None = None
        self.target: RunRefused | None = None
        self.dispatch_denied: str | None = None
        self.caller_gone = False
        self.scheduled: list[str] = []
        self.schedule_error: Exception | None = None
        self.payloads: dict[str, dict] = {}
        self.engine_calls = 0
        self.resume = True
        self.ran: list[RunContext] = []
        self.available = BindingAvailability(lambda binding: self.problem)

    def owner_scope(self, caller): return caller.scope
    def maker(self, caller): return caller.person
    def is_owner(self, caller): return caller.owner
    def audience(self, caller): return "client"
    def reader(self, caller): return Reader(stored_owner_key("hash-1", app_id=caller.scope), caller.person)
    def can_read(self, job, caller): return job.get("app_id") == caller.scope
    def can_cancel(self, job, caller): return self.can_read(job, caller) and (caller.owner or True)
    def job_owner(self, caller): return "hash-1", caller.scope, "key-1"
    def target_refusal(self, e, caller): return self.target
    def dispatch_target_problem(self, e, caller): return self.dispatch_denied
    def result_limits(self): return ResultLimits()
    def resume_on_start(self): return self.resume

    def admission_refusal(self, resource_class, caller, *, count_queue): return self.refusal

    def adapter_identity(self, e, binding_key, binding):
        return AdapterIdentity("proto", 1, "adapter", 1, "svc", "http://engine.test:1")

    def template_bytes(self, binding): return b"{}"
    def caller_payload(self, caller): return {"scope": caller.scope, "person": caller.person}
    def save_payload(self, job_id, payload, caller): self.payloads[job_id] = dict(payload)
    def load_payload(self, job_id): return self.payloads.get(job_id)
    def drop_payload(self, job_id): self.payloads.pop(job_id, None)

    def caller_from_payload(self, payload):
        return None if self.caller_gone else Caller(payload["auth"]["scope"], payload["auth"]["person"])

    def schedule(self, job_id):
        if self.schedule_error is not None:
            raise self.schedule_error
        self.scheduled.append(job_id)

    async def run_adapter(self, ctx):
        self.engine_calls += 1
        self.ran.append(ctx)
        limits = ResultLimits()
        result_bundle.complete_job(
            ctx.job_id, lambda conn, attempt: result_bundle.stage_json_in(
                conn, ctx.job_id, attempt, "result", b'{"ok": true}', limits=limits),
            limits=limits, path=self.db)

    def read_asset(self, asset_id, caller): return b"asset-bytes" if asset_id == "asset-1" else None


async def _no_wait(seconds: float) -> None:
    """A retry between two attempts waits for no one in a test."""


def make_core(db: Path, tmp_path: Path, snap: FakeSnapshot, port: Port | None = None, **kwargs):
    kwargs.setdefault("sleep", _no_wait)
    port = port or Port(db)
    draws: list[int] = []

    def generate(inputs, given, **kw):
        draws.append(1)
        return generated_defaults.resolve_generated(inputs, given, source=lambda low, high: 7, **kw)

    core = WorkflowRunCore(snapshots=Provider(snap), projections=Projections(), port=port, spool=JobSpool(tmp_path),
                           path=db, generate=generate, **kwargs)
    return core, port, draws


def snapshot(**kw) -> FakeSnapshot:
    return FakeSnapshot(1, MappingProxyType({"demo.run": entry(CONTRACT, **kw)}))


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    return path


def request(**kw) -> RunRequest:
    base = dict(workflow_id="demo.run", inputs={"prompt": "hello"}, idempotency_key="key-1")
    return RunRequest(**{**base, **kw})


def run(coro):
    with request_source.acting_as(app_id="app-1", person_id="person-1"):
        return asyncio.run(coro)


def job_rows(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT job_id, status FROM media_jobs").fetchall()


def claim_rows(db: Path) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT idem_key, holder, job_id FROM workflow_run_claim").fetchall()


# -------------------------------------------------------------------------------- contract tests (section 7.2)


def test_two_real_submits_racing_on_one_key_make_one_job_and_one_draw(db, tmp_path):
    core, port, draws = make_core(db, tmp_path, snapshot())

    async def both():
        return await asyncio.gather(core.submit_run(request(), Caller(), snapshot=core._snapshots.current()),
                                    core.submit_run(request(), Caller(), snapshot=core._snapshots.current()))

    first, second = run(both())
    assert isinstance(first, RunJobView) and isinstance(second, RunJobView)
    assert first.job_id == second.job_id
    assert sorted([first.disposition, second.disposition]) == ["new", "replay"]
    assert len(job_rows(db)) == 1 and len(draws) == 1
    assert port.scheduled == [first.job_id]


def test_a_holder_paused_past_the_lease_is_taken_over_and_its_create_draws_nothing(db):
    claim = dict(owner_scope="app-1", maker="person-1", key="key-1", identity="id-1", lease_s=0.2, path=db)
    fence_a = media_jobs.RunClaimFence("app-1", "person-1", "key-1", "holder-a")
    assert media_jobs.claim_run_key(holder="holder-a", now=100.0, **claim).outcome == "held"
    assert media_jobs.claim_run_key(holder="holder-b", now=100.1, **claim).outcome == "busy"     # inside the lease
    assert media_jobs.claim_run_key(holder="holder-b", now=101.0, **claim).outcome == "held"     # taken over
    drawn: list[str] = []
    with pytest.raises(media_jobs.ClaimLost):
        media_jobs.create(media_jobs.MediaJob(intent="workflow.run"), path=db, claim=fence_a,
                          prepare=lambda record: drawn.append("draw"))
    assert drawn == [] and job_rows(db) == []
    assert claim_rows(db) == [("key-1", "holder-b", None)]


def test_a_holder_that_lost_its_claim_through_the_core_creates_nothing_and_answers_the_new_holders_job(db, tmp_path):
    snap = snapshot()
    core, port, draws = make_core(db, tmp_path, snap, claim_lease_s=0.0, claim_wait_s=0.0)
    seen = {}

    async def scenario():
        # Holder A is inside ``_decide_and_create`` when holder B takes the key over and creates the job.
        original = core._decide_and_create

        async def paused(req, caller, snapshot_, fence):
            seen["fence"] = fence
            other = WorkflowRunCore(snapshots=core._snapshots, projections=Projections(), port=port, spool=JobSpool(tmp_path),
                                    path=db, generate=core._generate, claim_lease_s=0.0, claim_wait_s=0.0)
            await asyncio.sleep(0.01)
            seen["b"] = await other.submit_run(request(), Caller(), snapshot=snap)
            return await original(req, caller, snapshot_, fence)

        core._decide_and_create = paused
        return await core.submit_run(request(), Caller(), snapshot=snap)

    answer = run(scenario())
    assert isinstance(answer, RunJobView) and answer.disposition == "replay"
    assert answer.job_id == seen["b"].job_id
    assert len(job_rows(db)) == 1 and len(draws) == 1


def test_that_holders_release_leaves_the_new_holders_claim(db, tmp_path):
    media_jobs.claim_run_key(owner_scope="s", maker="m", key="k", identity="i", holder="old", lease_s=1, now=0.0, path=db)
    media_jobs.claim_run_key(owner_scope="s", maker="m", key="k", identity="i", holder="new", lease_s=1, now=5.0, path=db)
    assert media_jobs.release_run_claim(media_jobs.RunClaimFence("s", "m", "k", "old"), path=db) is False
    assert claim_rows(db) == [("k", "new", None)]
    assert media_jobs.release_run_claim(media_jobs.RunClaimFence("s", "m", "k", "new"), path=db) is True
    assert claim_rows(db) == []


def test_a_scheduling_error_after_commit_keeps_the_job_and_the_claim_and_a_retry_replays(db, tmp_path):
    core, port, draws = make_core(db, tmp_path, snapshot())
    port.schedule_error = RuntimeError("scheduler down")
    first = run(core.submit_run(request(), Caller(), snapshot=core._snapshots.current()))
    assert isinstance(first, RunJobView) and first.disposition == "new" and first.status == "queued"
    assert len(job_rows(db)) == 1 and claim_rows(db)[0][2] == first.job_id
    port.schedule_error = None
    retry = run(core.submit_run(request(), Caller(), snapshot=core._snapshots.current()))
    assert retry.disposition == "replay" and retry.job_id == first.job_id
    assert len(draws) == 1 and len(job_rows(db)) == 1
    report = core.recover_runs(0)
    assert report.counts == {"resume": 1} and port.scheduled == [first.job_id]


# ------------------------------------------------------------------------------------------------ submit order


def test_the_same_key_with_another_request_is_a_conflict_and_the_same_request_replays_without_checks(db, tmp_path):
    snap = snapshot()
    core, port, draws = make_core(db, tmp_path, snap)
    first = run(core.submit_run(request(), Caller(), snapshot=snap))
    other = run(core.submit_run(request(inputs={"prompt": "different"}), Caller(), snapshot=snap))
    assert isinstance(other, RunRefused) and (other.http_status, other.code) == (409, "idempotency_conflict")
    # The workflow is disabled and the port would refuse: a replay is still the job, with nothing drawn or checked.
    disabled = FakeSnapshot(2, MappingProxyType({"demo.run": entry(CONTRACT, state="disabled")}))
    port.refusal = RunRefused(503, "busy", "busy")
    again = run(core.submit_run(request(), Caller(), snapshot=disabled))
    assert again.disposition == "replay" and again.job_id == first.job_id and len(draws) == 1


def test_a_second_request_while_the_first_is_undecided_answers_in_progress(db, tmp_path):
    media_jobs.claim_run_key(owner_scope="app-1", maker="person-1", key="key-1", identity="x", holder="other",
                             lease_s=1000, path=db)
    core, port, draws = make_core(db, tmp_path, snapshot(), claim_wait_s=0.1)
    media_jobs_identity = run_identity("app-1", "person-1", request())
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE workflow_run_claim SET identity=?", (media_jobs_identity,))
    answer = run(core.submit_run(request(), Caller(), snapshot=core._snapshots.current()))
    assert isinstance(answer, RunRefused) and (answer.http_status, answer.code) == (409, "idempotency_in_progress")
    assert answer.retry_after_s and draws == [] and len(job_rows(db)) == 0


@pytest.mark.parametrize("make,status,code", [
    (lambda: dict(snap=FakeSnapshot(1, MappingProxyType({}))), 404, "workflow_not_found"),
    (lambda: dict(snap=snapshot(state="disabled")), 409, "workflow_disabled"),
    (lambda: dict(snap=snapshot(published=False)), 404, "workflow_not_found"),
    (lambda: dict(snap=snapshot(facts=None)), 409, "needs_policy"),
    (lambda: dict(snap=snapshot(), req=request(binding_key="nope")), 422, "binding_unknown"),
    (lambda: dict(snap=snapshot(), problem="adapter_missing"), 503, "adapter_missing"),
    (lambda: dict(snap=snapshot(), target=RunRefused(403, "policy_denied", "no")), 403, "policy_denied"),
    (lambda: dict(snap=snapshot(), refusal=RunRefused(503, "queue_full", "full")), 503, "queue_full"),
])
def test_a_refusal_is_a_run_refused_and_releases_the_claim(db, tmp_path, make, status, code):
    spec = make()
    core, port, draws = make_core(db, tmp_path, spec["snap"])
    port.problem, port.target, port.refusal = spec.get("problem"), spec.get("target"), spec.get("refusal")
    answer = run(core.submit_run(spec.get("req") or request(), Caller(), snapshot=spec["snap"]))
    assert isinstance(answer, RunRefused) and (answer.http_status, answer.code) == (status, code)
    assert claim_rows(db) == [] and job_rows(db) == [] and draws == [] and port.scheduled == []


def test_every_input_and_media_problem_is_reported_at_once(db, tmp_path):
    core, port, _ = make_core(db, tmp_path, snapshot())
    photo = MediaRef("sha256:" + "a" * 64, "audio/wav", 5000, spool_key="x-y")
    answer = run(core.submit_run(
        request(inputs={"seed": "not-an-int", "stray": 1}, media={"photo": (photo,)}), Caller(),
        snapshot=core._snapshots.current()))
    assert isinstance(answer, RunRefused) and (answer.http_status, answer.code) == (422, "invalid_inputs")
    codes = {(p.path, p.code) for p in answer.problems}
    assert (("inputs", "prompt"), "required_missing") in codes
    assert (("inputs", "stray"), "input_unknown") in codes
    assert (("media", "photo", 0), "media_type") in codes and (("media", "photo", 0), "media_size") in codes
    assert claim_rows(db) == [] and job_rows(db) == []


def test_a_new_job_pins_its_definitions_and_records_the_registry_facts(db, tmp_path):
    snap = snapshot(revision=4, incarnation="inc-9")
    core, port, draws = make_core(db, tmp_path, snap)
    view = run(core.submit_run(request(binding_key="other"), Caller(), snapshot=snap))
    assert (view.status, view.disposition, view.workflow_id) == ("queued", "new", "demo.run")
    assert (view.record_revision, view.binding_key) == (4, "other")
    job = media_jobs.get(view.job_id, audience="internal", path=db)
    assert job["intent"] == "workflow.run" and job["metadata"]["resource_class"] == "fake-class"
    with sqlite3.connect(db) as conn:
        stored = json.loads(conn.execute("SELECT envelope_json FROM job_envelope WHERE job_id=?", (view.job_id,)).fetchone()[0])
    assert stored["envelope_version"] == 2
    assert stored["source"]["incarnation"] == "inc-9" and stored["source"]["binding_key"] == "other"
    assert stored["generated"]["seed"]["value"] == 7
    assert port.payloads[view.job_id]["body"] == {"inputs": {"prompt": "hello"}}


PNG_HEAD = b"\x89PNG\r\n\x1a\n"


def test_staged_files_are_adopted_by_a_new_job_and_deleted_when_the_job_does_not_commit(db, tmp_path):
    snap = snapshot()
    core, port, _ = make_core(db, tmp_path, snap)

    async def stage_one():
        spool = FileRequestSpool(tmp_path, max_bytes=1000, request_id="req1")

        async def chunks():
            yield PNG_HEAD + b"abcdef"

        ref = await spool.stage("photo", chunks(), filename="a.png", wire_media_type="image/png")
        return spool, ref

    spool, ref = asyncio.run(stage_one())
    view = run(core.submit_run(request(media={"photo": (ref,)}), Caller(), snapshot=snap))
    assert (tmp_path / "jobs" / view.job_id).is_dir()
    adopted = port.payloads[view.job_id]["media"]["photo"][0]
    assert (tmp_path / "jobs" / view.job_id / adopted["spool_key"]).read_bytes() == PNG_HEAD + b"abcdef"
    spool.release()
    assert not (tmp_path / "staging" / "req1").exists()
    assert (tmp_path / "jobs" / view.job_id / adopted["spool_key"]).exists()   # release never touches an adopted file

    # A create that does not commit leaves no job spool and no payload.
    core2, port2, _ = make_core(db, tmp_path, snap)
    spool2, ref2 = asyncio.run(stage_one())
    port2.save_payload = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    with pytest.raises(RuntimeError):
        run(core2.submit_run(request(idempotency_key="key-2", media={"photo": (ref2,)}), Caller(), snapshot=snap))
    assert len(job_rows(db)) == 1 and claim_rows(db)[0][0] == "key-1"
    assert [p.name for p in (tmp_path / "jobs").iterdir()] == [view.job_id]
    spool2.release()


def test_the_request_spool_refuses_oversize_and_unknown_types_and_keeps_no_bytes(tmp_path):
    spool = FileRequestSpool(tmp_path, max_bytes=10, request_id="r")

    async def chunks(data):
        yield data

    big = asyncio.run(spool.stage("a", chunks(b"x" * 11), filename=None, wire_media_type="image/png"))
    unknown = asyncio.run(spool.stage("a", chunks(b"xx"), filename=None, wire_media_type="image/png"))
    ok = asyncio.run(spool.stage("a", chunks(PNG_HEAD), filename="f", wire_media_type="image/jpeg; charset=x"))
    assert isinstance(big, RunRefused) and big.http_status == 413
    # The type is measured from the bytes: bytes of no known type are the unidentified type whatever the caller says,
    # and a picture is a picture whatever the caller says. The declared type is only kept for the wire.
    assert isinstance(unknown, MediaRef) and unknown.media_type == "application/octet-stream"
    assert unknown.wire_media_type == "image/png"
    assert isinstance(ok, MediaRef) and ok.media_type == "image/png" and ok.size == 8 and ok.digest.startswith("sha256:")
    assert ok.wire_media_type == "image/jpeg; charset=x"
    assert len(list((tmp_path / "staging" / "r").iterdir())) == 2
    spool.release()
    spool.release()
    assert not (tmp_path / "staging" / "r").exists()


# ---------------------------------------------------------------------------------------------------- dispatch


def submitted(db, tmp_path, snap=None, **kw):
    snap = snap or snapshot()
    core, port, draws = make_core(db, tmp_path, snap, **kw)
    view = run(core.submit_run(request(), Caller(), snapshot=snap))
    return core, port, view, snap


def test_a_queued_job_runs_from_its_pin_and_succeeds_with_references_only(db, tmp_path):
    core, port, view, snap = submitted(db, tmp_path)
    # The live workflow changes after accept: the run uses what the job pinned.
    core._snapshots.snap = FakeSnapshot(2, MappingProxyType({"demo.run": entry({**CONTRACT, "inputs": {}}, revision=2)}))
    run(core.execute_run(view.job_id))
    assert port.engine_calls == 1
    ctx = port.ran[0]
    assert "prompt" in ctx.pin.contract["inputs"] and ctx.inputs == {"prompt": "hello"} and ctx.generated == {"seed": 7}
    done = WorkflowJobService(port=port, path=db).get(view.job_id, Caller())
    assert done.status == "succeeded" and done.results[0].output == "result"
    assert not hasattr(done.results[0], "value")
    assert view.job_id not in port.payloads        # the execution payload ends with the job


@pytest.mark.parametrize("change,code", [
    (lambda core, port: setattr(port, "caller_gone", True), "caller_unavailable"),
    (lambda core, port: setattr(core._snapshots, "snap", FakeSnapshot(2, MappingProxyType({}))), "workflow_deleted"),
    (lambda core, port: setattr(core._snapshots, "snap", FakeSnapshot(2, MappingProxyType(
        {"demo.run": entry(CONTRACT, incarnation="inc-other")}))), "workflow_deleted"),
    (lambda core, port: setattr(core._snapshots, "snap", FakeSnapshot(2, MappingProxyType(
        {"demo.run": entry(CONTRACT, state="disabled")}))), "workflow_disabled"),
    (lambda core, port: setattr(core._snapshots, "snap", FakeSnapshot(2, MappingProxyType(
        {"demo.run": entry(CONTRACT, facts=None)}))), "needs_policy"),
    (lambda core, port: setattr(port, "dispatch_denied", "policy_denied"), "policy_denied"),
    (lambda core, port: setattr(port, "problem", "adapter_missing"), "adapter_missing"),
    (lambda core, port: setattr(port, "refusal", RunRefused(503, "queue_full", "full")), "queue_full"),
])
def test_each_dispatch_check_fails_the_job_with_its_code_and_makes_no_engine_call(db, tmp_path, change, code):
    core, port, view, _ = submitted(db, tmp_path)
    change(core, port)
    run(core.execute_run(view.job_id))
    job = media_jobs.get(view.job_id, audience="internal", path=db)
    assert (job["status"], job["error_code"]) == ("failed", code)
    assert port.engine_calls == 0


def test_published_is_not_asked_again_at_dispatch(db, tmp_path):
    core, port, view, _ = submitted(db, tmp_path)
    core._snapshots.snap = FakeSnapshot(2, MappingProxyType({"demo.run": entry(CONTRACT, published=False)}))
    run(core.execute_run(view.job_id))
    assert port.engine_calls == 1


def test_a_binding_removed_from_the_live_entry_does_not_stop_a_pinned_job(db, tmp_path):
    core, port, view, _ = submitted(db, tmp_path)
    live = {**CONTRACT, "bindings": {"unrelated": {}}}
    core._snapshots.snap = FakeSnapshot(2, MappingProxyType({"demo.run": entry(live)}))
    run(core.execute_run(view.job_id))
    assert port.engine_calls == 1


def test_a_failing_adapter_settles_the_job_failed_with_a_token_code_and_no_raw_text(db, tmp_path):
    core, port, view, _ = submitted(db, tmp_path)

    class Boom(Exception):
        code, category = "upstream_job_failed", "upstream"

    async def failing(ctx):
        raise Boom("secret prompt text")

    port.run_adapter = failing
    run(core.execute_run(view.job_id))
    job = media_jobs.get(view.job_id, audience="internal", path=db)
    assert (job["status"], job["error_code"], job["error_category"]) == ("failed", "upstream_job_failed", "upstream")
    assert "secret" not in json.dumps(job)


# ---------------------------------------------------------------------------------------------------- recovery


def _stamp(db: Path, job_id: str, **metadata: Any) -> None:
    media_jobs.update_status(job_id, "running", metadata_patch=metadata, path=db)


def test_recovery_decides_each_row_of_the_table(db, tmp_path):
    snap = snapshot()
    core, port, _ = make_core(db, tmp_path, snap)
    ids = {}
    for name in ("resume", "poll", "submitting", "interrupted"):
        view = run(core.submit_run(request(idempotency_key=name), Caller(), snapshot=snap))
        ids[name] = view.job_id
    _stamp(db, ids["poll"], protocol_job="up-1")
    _stamp(db, ids["submitting"], protocol_submitting=True)
    port.payloads.pop(ids["interrupted"])
    port.scheduled.clear()
    report = core.recover_runs(0)
    assert report.counts == {"resume": 1, "poll": 1, "submission_unknown": 1, "interrupted_before_submit": 1}
    assert set(port.scheduled) == {ids["resume"], ids["poll"]}
    assert set(report.failed_job_ids) == {ids["submitting"], ids["interrupted"]}
    codes = {name: media_jobs.get(job_id, audience="internal", path=db)["error_code"]
             for name, job_id in ids.items() if name in ("submitting", "interrupted")}
    assert codes == {"submitting": "submission_unknown", "interrupted": "spool_missing"}
    assert port.engine_calls == 0


def test_a_poll_skips_the_dispatch_checks_and_a_replay_makes_no_engine_call(db, tmp_path):
    snap = snapshot()
    core, port, _ = make_core(db, tmp_path, snap)
    view = run(core.submit_run(request(), Caller(), snapshot=snap))
    _stamp(db, view.job_id, protocol_job="up-1")
    core._snapshots.snap = FakeSnapshot(2, MappingProxyType({}))      # the workflow is gone: checks 2 to 6 would fail it
    port.caller_gone = True
    run(core.execute_run(view.job_id))
    assert port.engine_calls == 1                                      # polled through the pinned upstream identity

    other = run(core.submit_run(request(idempotency_key="k2"), Caller(), snapshot=snap))
    limits = ResultLimits()
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, other.job_id, 1, "result", b"{}", limits=limits)
        result_bundle.publish_in(conn, other.job_id, 1, limits=limits)
        conn.commit()
    port.engine_calls = 0
    assert core.recover_runs(0).counts["replay"] == 1
    assert media_jobs.get(other.job_id, audience="internal", path=db)["status"] == "succeeded"
    assert port.engine_calls == 0


def test_a_job_with_no_envelope_or_an_unknown_version_fails_at_recovery(db, tmp_path):
    bare = media_jobs.create(media_jobs.MediaJob(intent="workflow.run", workflow="demo.run"), path=db)
    odd = media_jobs.create(media_jobs.MediaJob(intent="workflow.run", workflow="demo.run"), path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO job_envelope (job_id, envelope_json, created) VALUES (?, ?, 0)",
                     (odd.job_id, json.dumps({"envelope_version": 9})))
    core, port, _ = make_core(db, tmp_path, snapshot())
    report = core.recover_runs(0)
    assert report.counts == {"pin_not_recorded": 1, "envelope_version_unsupported": 1}
    codes = {j: media_jobs.get(j, audience="internal", path=db)["error_code"] for j in (bare.job_id, odd.job_id)}
    assert codes == {bare.job_id: "pin_not_recorded", odd.job_id: "envelope_version_unsupported"}


def _damage_the_pin(db: Path, job_id: str, how: str) -> None:
    """Break one definition blob the job's envelope names, leaving the envelope row in place."""
    with sqlite3.connect(db) as conn:
        sha = json.loads(conn.execute("SELECT envelope_json FROM job_envelope WHERE job_id=?", (job_id,)).fetchone()[0])["definitions"]["contract"]
        if how == "deleted":
            conn.execute("DELETE FROM definition_blob WHERE sha=?", (sha,))
        else:
            conn.execute("UPDATE definition_blob SET body=? WHERE sha=?", (b'{"tampered": true}', sha))


@pytest.mark.parametrize("how", ["deleted", "tampered"])
@pytest.mark.parametrize("where", ["direct", "after_recovery"])
def test_an_unreadable_pin_fails_definition_unavailable_not_pin_not_recorded(db, tmp_path, how, where):
    """A job that DID record an envelope, whose blob is gone or changed, is not a job that recorded nothing: it settles
    failed, nonretryable, definition_unavailable, with no engine call and no use of the live definition."""
    core, port, view, snap = submitted(db, tmp_path)
    _damage_the_pin(db, view.job_id, how)
    if where == "after_recovery":
        report = core.recover_runs(0)
        assert "pin_not_recorded" not in report.counts
        port.scheduled.clear()
    run(core.execute_run(view.job_id))
    failed = media_jobs.get(view.job_id, audience="internal", path=db)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "definition_unavailable" and failed["retryable"] is False
    assert port.engine_calls == 0


@pytest.mark.parametrize("where", ["direct", "after_recovery"])
def test_a_supported_envelope_that_does_not_parse_fails_definition_unavailable(db, tmp_path, where):
    core, port, view, snap = submitted(db, tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE job_envelope SET envelope_json=? WHERE job_id=?",
                     (json.dumps({"envelope_version": 2}), view.job_id))
    if where == "after_recovery":
        assert "pin_not_recorded" not in core.recover_runs(0).counts
    run(core.execute_run(view.job_id))
    failed = media_jobs.get(view.job_id, audience="internal", path=db)
    assert failed["status"] == "failed" and failed["error_code"] == "definition_unavailable" and failed["retryable"] is False
    assert port.engine_calls == 0


def _run_a_bare_job(db, tmp_path, envelope_json=None):
    job = media_jobs.create(media_jobs.MediaJob(intent="workflow.run", workflow="demo.run"), path=db)
    if envelope_json is not None:
        with sqlite3.connect(db) as conn:
            conn.execute("INSERT INTO job_envelope (job_id, envelope_json, created) VALUES (?, ?, 0)", (job.job_id, envelope_json))
    core, port, _ = make_core(db, tmp_path, snapshot())
    port.payloads[job.job_id] = {"auth": {"scope": "app-1", "person": "person-1"}, "body": {}, "media": {}}
    run(core.execute_run(job.job_id))
    assert port.engine_calls == 0
    return media_jobs.get(job.job_id, audience="internal", path=db)


def test_a_job_with_no_envelope_row_is_pin_not_recorded_when_it_runs(db, tmp_path):
    """The control: absence of any envelope row stays pin_not_recorded on the direct path."""
    assert _run_a_bare_job(db, tmp_path)["error_code"] == "pin_not_recorded"


def test_a_job_with_an_unknown_envelope_version_is_envelope_version_unsupported_when_it_runs(db, tmp_path):
    """The version case has its own code on the direct path, as it has at recovery."""
    assert _run_a_bare_job(db, tmp_path, json.dumps({"envelope_version": 9}))["error_code"] == "envelope_version_unsupported"


def test_a_version_one_job_is_listed_by_the_pre_deploy_check_and_shows_unknown_facts(db, tmp_path):
    v1 = build_job_envelope(source_kind="workflow", source_id="demo.run", contract={"a": 1}, binding={"b": 1}, template=None,
                            protocol_id="p", adapter_id="a", interface_version=1, service_id="s", origin="http://e")
    old = json.loads(json.dumps(v1.envelope.to_json()))
    old["envelope_version"] = 1
    old["source"] = {k: old["source"][k] for k in ("kind", "id", "revision")}
    job = media_jobs.create(media_jobs.MediaJob(intent="image.generate", workflow="demo.run"), path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO job_envelope (job_id, envelope_json, created) VALUES (?, ?, 0)", (job.job_id, json.dumps(old)))
    current = media_jobs.create(media_jobs.MediaJob(intent="workflow.run"), path=db, envelope=v1)
    listed = queued_jobs_without_v2_envelope(db)
    assert [item["job_id"] for item in listed] == [job.job_id] and current.job_id not in {i["job_id"] for i in listed}
    assert listed[0]["record_revision"] is None and listed[0]["binding_key"] is None


# ---------------------------------------------------------------------------------------------- job service


def test_the_job_service_reads_waits_cancels_and_opens_results_for_its_caller(db, tmp_path):
    core, port, view, _ = submitted(db, tmp_path)
    service = WorkflowJobService(port=port, path=db)
    stranger = Caller(scope="app-2")
    assert service.get(view.job_id, stranger) is None and service.get("nope", Caller()) is None
    with pytest.raises(JobNotFound):
        service.cancel(view.job_id, stranger)
    with pytest.raises(JobNotFound):
        run(service.wait(view.job_id, stranger, 0))
    waited = run(service.wait(view.job_id, Caller(), 0.05))
    assert waited.status == "queued"
    run(core.execute_run(view.job_id))
    finished = run(service.wait(view.job_id, Caller(), 1))
    assert finished.status == "succeeded"
    content = service.open_result(view.job_id, finished.results[0], Caller())
    assert content.value == {"ok": True} and content.media_type == "application/json"
    forged = replace(finished.results[0], output="other")
    with pytest.raises(ResultUnavailable):
        service.open_result(view.job_id, forged, Caller())
    assert service.cancel(view.job_id, Caller()).status == "succeeded"      # a finished job is answered unchanged
    assert [e.type for e in service.events(view.job_id, Caller(), limit=10)][0] == "queued"


def test_cancel_stops_a_queued_job_before_it_runs(db, tmp_path):
    core, port, view, _ = submitted(db, tmp_path)
    service = WorkflowJobService(port=port, path=db)
    assert service.cancel(view.job_id, Caller()).status == "canceled"
    run(core.execute_run(view.job_id))
    assert port.engine_calls == 0
    assert view.job_id not in port.payloads


def test_a_result_the_reader_may_not_read_is_unavailable(db, tmp_path):
    core, port, view, _ = submitted(db, tmp_path)
    run(core.execute_run(view.job_id))
    service = WorkflowJobService(port=port, path=db)
    ref = service.get(view.job_id, Caller()).results[0]
    port.reader = lambda caller: Reader("another-scope", "person-1")
    with pytest.raises(ResultUnavailable):
        service.open_result(view.job_id, ref, Caller())


# ----------------------------------------------------------------------------------------------------- I7 limits


def test_run_limits_lower_the_code_ceiling_and_ignore_values_that_raise_it_or_are_not_numbers():
    from gateway.workflow_run import CONFIG_SECTION, RUN_CEILINGS, run_limits_from_config

    ceiling = run_limits_from_config({})
    assert (ceiling.max_wait_s, ceiling.history_depth) == (RUN_CEILINGS["max_wait_s"], RUN_CEILINGS["history_depth"])
    lowered = run_limits_from_config({CONFIG_SECTION: {"max_wait_s": 5, "history_depth": 3, "bounds": {"max_nodes": 10}}})
    assert (lowered.max_wait_s, lowered.history_depth, lowered.max_nodes) == (5, 3, 10)
    raised = run_limits_from_config({CONFIG_SECTION: {"max_wait_s": 10**6, "bounds": {"max_depth": 10**6}}})
    assert raised == ceiling
    garbage = {CONFIG_SECTION: {"max_wait_s": "9", "history_depth": True, "bounds": [1, 2]}}
    assert run_limits_from_config(garbage) == ceiling
    assert run_limits_from_config({CONFIG_SECTION: {"max_wait_s": -1, "history_depth": 0}}) == ceiling


# ---------------------------------------------------------------------------------------------- job service: list (S7 M7)


def listed_jobs(db, tmp_path, count, *, intent="demo.listing", scope="app-1"):
    """``count`` jobs filed under ``intent``, with created times 100, 200, ... so the order is the test's, not the clock's."""
    snap = snapshot()
    core, port, _ = make_core(db, tmp_path, snap)
    ids = []
    for index in range(count):
        view = run(core.submit_run(request(idempotency_key=f"{intent}-{scope}-{index}", intent=intent), Caller(scope=scope), snapshot=snap))
        ids.append(view.job_id)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE media_jobs SET created_at = ? WHERE job_id = ?", (100 * (index + 1), view.job_id))
    return port, ids


def test_list_pages_newest_first_by_default_and_oldest_first_on_request(db, tmp_path):
    port, ids = listed_jobs(db, tmp_path, 5)
    service = WorkflowJobService(port=port, path=db)
    views, more = service.list(Caller(), intent="demo.listing", limit=2)
    assert [v.job_id for v in views] == [ids[4], ids[3]] and more is True
    views, more = service.list(Caller(), intent="demo.listing", limit=2, after_id=ids[3])
    assert [v.job_id for v in views] == [ids[2], ids[1]] and more is True
    views, more = service.list(Caller(), intent="demo.listing", limit=2, after_id=ids[1])
    assert [v.job_id for v in views] == [ids[0]] and more is False
    views, more = service.list(Caller(), intent="demo.listing", limit=10, order="asc")
    assert [v.job_id for v in views] == ids and more is False
    assert all(v.disposition == "replay" for v in views)


def test_list_shows_only_the_intent_and_the_jobs_the_caller_may_read(db, tmp_path):
    port, mine = listed_jobs(db, tmp_path, 2)
    _, others = listed_jobs(db, tmp_path, 1, scope="app-2")
    _, elsewhere = listed_jobs(db, tmp_path, 1, intent="demo.other")
    service = WorkflowJobService(port=port, path=db)
    views, more = service.list(Caller(), intent="demo.listing", limit=10)
    assert {v.job_id for v in views} == set(mine) and more is False
    stranger, _ = service.list(Caller(scope="app-2"), intent="demo.listing", limit=10)
    assert {v.job_id for v in stranger} == set(others)
    assert {v.job_id for v in service.list(Caller(), intent="demo.other", limit=10)[0]} == set(elsewhere)
    assert not set(elsewhere) & {v.job_id for v in views}


def test_list_leaves_out_a_deleted_job_at_once_and_after_a_restart(db, tmp_path):
    port, ids = listed_jobs(db, tmp_path, 3)
    service = WorkflowJobService(port=port, path=db)
    service.cancel(ids[1], Caller())
    service.delete(ids[1], Caller())
    for fresh in (service, WorkflowJobService(port=port, path=db)):
        views, _ = fresh.list(Caller(), intent="demo.listing", limit=10, order="asc")
        assert [v.job_id for v in views] == [ids[0], ids[2]]


def test_list_refuses_a_cursor_that_is_not_one_of_the_callers_listed_jobs(db, tmp_path):
    port, ids = listed_jobs(db, tmp_path, 2)
    _, theirs = listed_jobs(db, tmp_path, 1, scope="app-2")
    service = WorkflowJobService(port=port, path=db)
    service.cancel(ids[0], Caller())
    service.delete(ids[0], Caller())
    for cursor in ("no-such-job", theirs[0], ids[0]):
        with pytest.raises(JobNotFound):
            service.list(Caller(), intent="demo.listing", limit=10, after_id=cursor)

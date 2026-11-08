"""S7 parity, core side (docs/design/CODE_TO_DATA_S7_PARITY.md, mechanisms M1 to M5 and M12; builder K).

One test per inventory item in categories 2, 5, 6, 7, 10 and 11 that the run core answers. The item ids are in the test
names or docstrings (docs/design/c2d-s7-parity/{images,audio,video,writers}.md). The route shapes, the node-graph adapter
and the row server answer the other items; their tests are in their builders' files.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from pathlib import Path

import pytest

from gateway import media_jobs, result_bundle
from gateway.engine_protocol.media import GatewayAssetStore
from gateway.registry_snapshot import frontend_row
from gateway.result_bundle import ResultLimits
from gateway import workflow_run
from gateway.workflow_run import BindingAvailability, WorkflowJobService
from gateway.workflow_runtime_types import RunJobView, RunRefused
from tests.test_workflow_run import CONTRACT, Caller, Port, db, make_core, request, run, snapshot  # noqa: F401
from tests.workflow_runtime_fakes import entry


def submit(core, **kw):
    return run(core.submit_run(request(**kw), Caller(), snapshot=core._snapshots.current()))


def job_error(db: Path, job_id: str) -> tuple[str, str | None, str | None, int | None]:
    job = media_jobs.get(job_id, audience="internal", path=db)
    return job["status"], job.get("error"), job.get("error_code"), job.get("upstream_http_status")


# ---------------------------------------------------------------------------------------------- M2: wire options


def test_options_are_part_of_the_request_identity_for_a_replay(db, tmp_path):
    """images-05, images-13: a replay with other wire options is a conflict, as it is on the old routes."""
    core, port, _ = make_core(db, tmp_path, snapshot())
    first = submit(core, options={"n": 2})
    assert submit(core, options={"n": 2}).job_id == first.job_id
    other = submit(core, options={"n": 3})
    assert isinstance(other, RunRefused) and other.http_status == 409 and other.code == "idempotency_conflict"


def test_options_reach_the_adapter_in_the_flat_body_under_options(db, tmp_path):
    """images-13: the saved payload body carries the options; a run with none keeps the body it had."""
    core, port, _ = make_core(db, tmp_path, snapshot())
    with_options = submit(core, options={"n": 2, "size": "512x512"}, idempotency_key="a")
    plain = submit(core, idempotency_key="b")
    assert port.payloads[with_options.job_id]["body"]["options"] == {"n": 2, "size": "512x512"}
    assert "options" not in port.payloads[plain.job_id]["body"]


# ------------------------------------------------------------------------------- M4: admission before the job exists


@pytest.mark.parametrize("reason,retry", [("service_disabled", None), ("service_unhealthy", 10), ("service_not_admitting", 30)])
def test_a_service_that_cannot_take_a_run_is_a_503_before_any_job_exists(db, tmp_path, reason, retry):
    """images-01, images-E01, audio-05, audio-06, video-01, video-42, video-49."""
    core, port, _ = make_core(db, tmp_path, snapshot())
    port.available = BindingAvailability(lambda binding: reason, lambda binding: retry)
    gate = core.target_gate("demo.run", Caller(), snapshot=core._snapshots.current())
    assert isinstance(gate, RunRefused) and gate.http_status == 503 and gate.code == reason and gate.retry_after_s == retry
    refused = submit(core)
    assert isinstance(refused, RunRefused) and (refused.http_status, refused.code) == (503, reason)
    with media_jobs.connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0] == 0
    assert port.engine_calls == 0


def test_a_binding_problem_that_is_not_a_service_state_keeps_its_own_answer(db, tmp_path):
    core, port, _ = make_core(db, tmp_path, snapshot())
    port.problem = "binding_unavailable"
    assert core.target_gate("demo.run", Caller(), snapshot=core._snapshots.current()) is None
    refused = submit(core)
    assert isinstance(refused, RunRefused) and refused.code == "binding_unavailable"


def test_the_gate_asks_the_binding_the_request_chose(db, tmp_path):
    core, port, _ = make_core(db, tmp_path, snapshot())
    port.available = BindingAvailability(lambda binding: "service_disabled" if binding.adapter_id == "other" else None)
    assert core.target_gate("demo.run", Caller(), snapshot=core._snapshots.current()) is None
    chosen = core.target_gate("demo.run", Caller(), snapshot=core._snapshots.current(), binding_key="other")
    assert isinstance(chosen, RunRefused) and chosen.code == "service_disabled"


def test_s7_same_key_replays_after_backend_becomes_unavailable(db, tmp_path):
    """S7 6.2 (M4), core side: a matching claim answers before the service-state refusal; a new key still gets the 503
    and no job."""
    core, port, _ = make_core(db, tmp_path, snapshot())
    first = submit(core, idempotency_key="same")
    port.available = BindingAvailability(lambda binding: "service_unhealthy", lambda binding: 10)
    again = submit(core, idempotency_key="same")
    assert isinstance(again, RunJobView) and again.disposition == "replay" and again.job_id == first.job_id
    fresh = submit(core, idempotency_key="new")
    assert isinstance(fresh, RunRefused) and (fresh.http_status, fresh.code) == (503, "service_unhealthy")
    with media_jobs.connection(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0] == 1


# ------------------------------------------------------------------------------------- M5: result metadata and failure


class MetadataPort(Port):
    async def run_adapter(self, ctx):
        ctx.record_result_metadata({"rounds": 3, "receipt": {"size_adjustment": {"requested": "a", "effective": "b"}}})
        await super().run_adapter(ctx)


def test_result_metadata_the_adapter_gives_is_on_the_finished_job_view(db, tmp_path):
    """images-14, images-16, images-31: the facts the old route put in headers and receipt metadata."""
    port = MetadataPort(db)
    core, port, _ = make_core(db, tmp_path, snapshot(), port)
    view = submit(core)
    run(core.execute_run(view.job_id))
    done = WorkflowJobService(port=port, path=db).get(view.job_id, Caller())
    assert done.status == "succeeded"
    assert done.result_metadata == {"rounds": 3, "receipt": {"size_adjustment": {"requested": "a", "effective": "b"}}}
    assert dataclasses.replace(done, result_metadata={}).result_metadata == {}


def failing_port(db, *, code, category, message, retryable, upstream_status):
    class EngineFailure(Exception):
        pass

    failure = EngineFailure("secret engine text http://engine.test:1")
    failure.code, failure.category, failure.retryable = code, category, retryable
    failure.upstream_status, failure.client_message = upstream_status, message

    class FailingPort(Port):
        async def run_adapter(self, ctx):
            raise failure

    return FailingPort(db)


@pytest.mark.parametrize("code,category,message,retryable,status", [
    ("image_generation_timeout", "timeout", "Image backend request timeout", False, None),
    ("upstream_out_of_memory", "upstream", "The engine ran out of memory", True, 502),
    ("image_client_disconnected", "internal", "Image backend connection dropped", False, None)])
def test_a_failed_job_keeps_the_adapters_message_status_and_code(db, tmp_path, code, category, message, retryable, status):
    """images-30, images-F01 to F07, audio-72, audio-73, video-23, writers-48, writers-55: the caller reads the old text."""
    port = failing_port(db, code=code, category=category, message=message, retryable=retryable, upstream_status=status)
    core, port, _ = make_core(db, tmp_path, snapshot(), port)
    view = submit(core)
    run(core.execute_run(view.job_id))
    failed = WorkflowJobService(port=port, path=db).get(view.job_id, Caller())
    assert failed.status == "failed"
    failure = failed.failure
    assert (failure.code, failure.category, failure.message, failure.retryable, failure.upstream_status) == (
        code, category, message, retryable, status)
    assert "engine.test" not in failure.message


def test_an_upstream_status_on_a_non_upstream_failure_is_dropped_and_the_failure_still_settles(db, tmp_path):
    """The job store keeps an HTTP status only for an upstream failure; a refused write must not leave the job running."""
    port = failing_port(db, code="Bad Code!", category="timeout", message="t", retryable=False, upstream_status=504)
    core, port, _ = make_core(db, tmp_path, snapshot(), port)
    view = submit(core)
    run(core.execute_run(view.job_id))
    assert job_error(db, view.job_id) == ("failed", "t", "internal_error", None)


def test_a_failure_without_a_client_message_says_the_fixed_sentence_and_never_the_exceptions_text(db, tmp_path):
    class Plain(Port):
        async def run_adapter(self, ctx):
            raise RuntimeError("http://engine.test:1 refused")

    core, port, _ = make_core(db, tmp_path, snapshot(), Plain(db))
    view = submit(core)
    run(core.execute_run(view.job_id))
    assert job_error(db, view.job_id)[:2] == ("failed", "The job failed.")


# ------------------------------------------------------------------------------------------ M12: restart recovery


def _queued_job(db, tmp_path, port=None):
    core, port, _ = make_core(db, tmp_path, snapshot(), port)
    view = submit(core)
    port.scheduled.clear()
    return core, port, view.job_id


def test_restart_with_the_request_not_kept_says_the_payload_is_missing(db, tmp_path):
    """images-R02, audio-78, video-97."""
    core, port, job_id = _queued_job(db, tmp_path)
    port.payloads.pop(job_id)
    core.recover_runs(0)
    assert job_error(db, job_id)[:2] == ("failed", "Image media job payload missing after proxy restart")


def test_restart_with_recovery_turned_off_says_so(db, tmp_path):
    """images-R03, audio-79, video-97."""
    core, port, job_id = _queued_job(db, tmp_path)
    port.resume = False
    core.recover_runs(0)
    assert job_error(db, job_id)[:2] == ("failed", "The gateway restarted with job recovery turned off")
    assert port.scheduled == []


def test_restart_after_a_cancel_was_asked_fails_the_job_not_cancels_it(db, tmp_path):
    """images-R04, audio-80, video-97."""
    core, port, job_id = _queued_job(db, tmp_path)
    media_jobs.update_status(job_id, "queued", metadata_patch={"cancel_requested_at": 1}, path=db)
    core.recover_runs(0)
    assert job_error(db, job_id)[:2] == ("failed", "The gateway restarted before cancellation could be confirmed")


def test_restart_during_a_run_fails_the_job_and_never_runs_it_again(db, tmp_path):
    """images-R05, audio-81, video-97."""
    core, port, job_id = _queued_job(db, tmp_path)
    media_jobs.update_status(job_id, "running", path=db)
    core.recover_runs(0)
    assert job_error(db, job_id)[:2] == (
        "failed", "The gateway restarted before completion could be confirmed; this job was not run again")
    assert port.scheduled == [] and port.engine_calls == 0


def test_restart_reschedules_a_queued_job(db, tmp_path):
    """audio-82, video-97."""
    core, port, job_id = _queued_job(db, tmp_path)
    core.recover_runs(0)
    assert port.scheduled == [job_id] and job_error(db, job_id)[0] == "queued"


def _commit_bundle(db, job_id):
    """A published bundle on a job that is not yet terminal: the crash window recovery's replay row covers."""
    limits = ResultLimits()
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, job_id, 1, "result", b"{}", limits=limits)
        result_bundle.publish_in(conn, job_id, 1, limits=limits)
        conn.commit()


@pytest.mark.parametrize("resume,cancel", [(True, False), (False, False), (True, True)])
def test_s7_recovery_preserves_poll_and_committed_bundle_precedence(db, tmp_path, resume, cancel):
    """S7 6.1 (M12): a committed bundle is replayed with no engine call, a recorded upstream id is polled with no new
    submit, and only a job with neither gets the restart text; recovery off or a waiting cancel changes text, not outcome."""
    core, port, _ = make_core(db, tmp_path, snapshot())
    replayed, polled, bare = (submit(core, idempotency_key=f"k-{n}").job_id for n in range(3))
    patch = {"cancel_requested_at": 1} if cancel else {}
    media_jobs.update_status(replayed, "running", path=db)
    media_jobs.update_status(polled, "running", path=db, metadata_patch={"protocol_job": "up-1", **patch})
    media_jobs.update_status(bare, "running", path=db, metadata_patch=patch)
    _commit_bundle(db, replayed)
    port.resume = resume
    port.scheduled.clear()
    core.recover_runs(0)
    assert job_error(db, replayed)[0] == "succeeded"
    assert job_error(db, polled)[0] == "running" and port.scheduled == [polled]
    assert job_error(db, bare)[0] == "failed"
    assert port.engine_calls == 0


# ------------------------------------------------------------------------------------------- M13: job retry


class Flaky(Exception):
    """An engine failure the adapter marks retryable. ``retry_after_s`` is the engine's Retry-After."""

    def __init__(self, *, retry_after_s=None, status=503, retryable=True, reason="engine_busy"):
        super().__init__("raw engine text http://engine.test:1")
        self.code, self.category, self.retryable = "upstream_unavailable", "upstream", retryable
        self.upstream_status, self.client_message = status, "The engine is busy"
        self.retry_after_s, self.retry_reason = retry_after_s, reason


def _retry_port(db, failures):
    class RetryPort(Port):
        def __init__(self, db):
            super().__init__(db)
            self.failures = list(failures)
            self.seen = []

        async def run_adapter(self, ctx):
            self.seen.append((ctx.pin, dict(ctx.generated), dict(ctx.inputs)))
            if self.failures:
                raise self.failures.pop(0)
            await super().run_adapter(ctx)

    return RetryPort(db)


def _retry_core(db, tmp_path, failures, **binding):
    contract = {**CONTRACT, "bindings": {"fake": binding, "other": {}}}
    port = _retry_port(db, failures)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    core, port, _ = make_core(db, tmp_path, workflow_snapshot(contract), port, sleep=sleep)
    return core, port, slept


def workflow_snapshot(contract):
    from types import MappingProxyType
    from tests.workflow_runtime_fakes import FakeSnapshot
    return FakeSnapshot(1, MappingProxyType({"demo.run": entry(contract)}))


def _events(db, job_id, kind):
    return [e["payload"] for e in media_jobs.events(job_id, audience="internal", limit=100, path=db) if e["event_type"] == kind]


def test_s7_image_job_retry_preserves_delay_events_and_attempt_limit(db, tmp_path):
    """S7 6.6 (M13), images-J22: a retryable failure is retried on the same job, with the same pin and generated values.
    The delay is base 5 s doubling to a 30 s cap; a Retry-After wins (capped at the max); each retry leaves the old
    event; the attempt limit is 3 and the last failure settles the job."""
    core, port, slept = _retry_core(db, tmp_path, [Flaky(), Flaky(retry_after_s=2)])
    view = submit(core)
    run(core.execute_run(view.job_id))
    job = media_jobs.get(view.job_id, path=db)
    assert job["status"] == "succeeded" and len(port.seen) == 3 and slept == [5, 2]
    assert all(seen[0] == port.seen[0][0] for seen in port.seen)
    assert all(seen[1:] == port.seen[0][1:] for seen in port.seen)
    retries = _events(db, view.job_id, "retry")
    assert retries == [
        {"phase": "retrying", "status": "running", "progress": 20, "attempt": 1, "total": 3, "http_status": 503,
         "reason": "engine_busy", "retry_after_s": 5},
        {"phase": "retrying", "status": "running", "progress": 30, "attempt": 2, "total": 3, "http_status": 503,
         "reason": "engine_busy", "retry_after_s": 2}]
    assert job["metadata"]["attempts"] == 2 and job["metadata"]["last_http_status"] == 503
    assert job["metadata"]["last_retry_reason"] == "engine_busy"

    core, port, slept = _retry_core(db, tmp_path, [Flaky(), Flaky(), Flaky(), Flaky()])
    again = submit(core, idempotency_key="exhaust")
    run(core.execute_run(again.job_id))
    assert len(port.seen) == 3 and slept == [5, 10]
    status, message, code, upstream = job_error(db, again.job_id)
    assert (status, message, code, upstream) == ("failed", "The engine is busy", "upstream_unavailable", 503)


def test_the_delay_doubles_to_the_cap_and_a_retry_after_is_capped(db, tmp_path):
    core, port, slept = _retry_core(db, tmp_path, [Flaky(), Flaky(), Flaky(retry_after_s=99), Flaky()],
                                    retry={"max_attempts": 5, "base_s": 5, "max_s": 12})
    view = submit(core)
    run(core.execute_run(view.job_id))
    assert slept == [5, 10, 12, 12] and len(port.seen) == 5


def test_the_attempt_limit_is_binding_data_and_a_failure_that_is_not_retryable_is_not_retried(db, tmp_path):
    core, port, slept = _retry_core(db, tmp_path, [Flaky(), Flaky(), Flaky()], retry={"max_attempts": 2})
    first = submit(core)
    run(core.execute_run(first.job_id))
    assert len(port.seen) == 2 and slept == [5]
    core, port, slept = _retry_core(db, tmp_path, [Flaky(retryable=False)])
    other = submit(core, idempotency_key="no-retry")
    run(core.execute_run(other.job_id))
    assert len(port.seen) == 1 and slept == [] and job_error(db, other.job_id)[0] == "failed"


def test_a_retry_stops_when_a_cancel_was_asked_or_the_caller_is_gone(db, tmp_path):
    core, port, slept = _retry_core(db, tmp_path, [Flaky(), Flaky()])
    view = submit(core)

    async def sleep(seconds):
        media_jobs.update_status(view.job_id, "running", path=db, metadata_patch={"cancel_requested_at": 1})

    core._sleep = sleep
    run(core.execute_run(view.job_id))
    assert len(port.seen) == 1
    assert job_error(db, view.job_id)[:2] == ("failed", "Cancellation was requested; the backend stop could not be confirmed")

    core, port, slept = _retry_core(db, tmp_path, [Flaky(), Flaky()])
    gone = submit(core, idempotency_key="gone")

    async def sleep_then_revoke(seconds):
        port.caller_gone = True

    core._sleep = sleep_then_revoke
    run(core.execute_run(gone.job_id))
    assert len(port.seen) == 1 and job_error(db, gone.job_id)[0] == "failed"


# ------------------------------------------------------------------------------------- M7: JobService.delete


def _service(port, db, **kw):
    return WorkflowJobService(port=port, path=db, **kw)


def _finished_job(db, tmp_path, key="d"):
    core, port, _ = make_core(db, tmp_path, snapshot())
    view = submit(core, idempotency_key=key)
    run(core.execute_run(view.job_id))
    return core, port, view.job_id


def test_delete_hides_a_finished_job_from_every_read_and_from_listing_after_a_restart(db, tmp_path):
    """S7 6.7: tombstone persisted; get, events, open_result, wait and listing no longer show it."""
    core, port, job_id = _finished_job(db, tmp_path)
    service = _service(port, db)
    ref = service.get(job_id, Caller()).results[0]
    assert service.open_result(job_id, ref, Caller()) is not None
    service.delete(job_id, Caller())
    for fresh in (service, _service(port, db)):        # the second is a restart: a new service over the same store
        assert fresh.get(job_id, Caller()) is None
        with pytest.raises(workflow_run.JobNotFound):
            fresh.events(job_id, Caller(), limit=10)
        with pytest.raises(workflow_run.JobNotFound):
            fresh.open_result(job_id, ref, Caller())
        with pytest.raises(workflow_run.JobNotFound):
            run(fresh.wait(job_id, Caller(), 0))
        with pytest.raises(workflow_run.JobNotFound):
            fresh.delete(job_id, Caller())
    assert job_id not in [j["job_id"] for j in media_jobs.recent(50, path=db)]


def test_delete_cancels_a_queued_job_first(db, tmp_path):
    core, port, _ = make_core(db, tmp_path, snapshot())
    job_id = submit(core).job_id
    service = _service(port, db)
    service.delete(job_id, Caller())
    assert media_jobs.get(job_id, path=db)["status"] == "canceled" and service.get(job_id, Caller()) is None


def test_a_cancel_that_is_not_confirmed_keeps_the_job_and_raises_a_conflict(db, tmp_path):
    core, port, _ = make_core(db, tmp_path, snapshot())
    job_id = submit(core).job_id
    media_jobs.update_status(job_id, "running", path=db)
    service = _service(port, db)
    with pytest.raises(workflow_run.JobDeleteConflict):
        service.delete(job_id, Caller())
    assert service.get(job_id, Caller()).status == "running" and not media_jobs.tombstoned(job_id, path=db)


def test_delete_by_a_caller_who_cannot_read_the_job_is_not_found_and_changes_nothing(db, tmp_path):
    core, port, job_id = _finished_job(db, tmp_path)
    with pytest.raises(workflow_run.JobNotFound):
        _service(port, db).delete(job_id, Caller(scope="someone-else"))
    assert not media_jobs.tombstoned(job_id, path=db)


def test_a_deleted_job_is_reaped_after_the_retention_window_with_its_events_and_claim(db, tmp_path):
    core, port, job_id = _finished_job(db, tmp_path)
    now = [1_000_000.0]
    service = _service(port, db, tombstone_retention_s=100, clock=lambda: now[0])
    service.delete(job_id, Caller())
    assert media_jobs.get(job_id, path=db) is not None                # hidden, not yet removed
    now[0] += 99
    media_jobs.reap_tombstones(before=int(now[0] - 100), path=db)
    assert media_jobs.get(job_id, path=db) is not None
    now[0] += 2
    assert media_jobs.reap_tombstones(before=int(now[0] - 100), path=db) == 1
    assert media_jobs.get(job_id, path=db) is None and not media_jobs.tombstoned(job_id, path=db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM media_job_events WHERE job_id=?", (job_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workflow_run_claim WHERE job_id=?", (job_id,)).fetchone()[0] == 0


def test_a_store_made_before_tombstones_gains_the_table_on_open(tmp_path):
    old = tmp_path / "old.db"
    media_jobs.init_db(old)
    with sqlite3.connect(old) as conn:
        conn.execute("DROP TABLE workflow_job_tombstone")
    media_jobs.init_db(old)
    media_jobs.tombstone_job("mj_x", path=old)
    assert media_jobs.tombstoned("mj_x", path=old)


def test_the_fake_job_service_deletes_like_the_real_one():
    from tests.workflow_runtime_fakes import FakeJobService, FakeRunCore
    from gateway.workflow_runtime_types import JobDeleteConflict, JobNotFound

    core = FakeRunCore()
    jobs = FakeJobService(core)
    done = RunJobView(job_id="a", status="succeeded", disposition="new", workflow_id="w", record_revision=1, binding_key="b", created_at_s=0, updated_at_s=0)
    live = RunJobView(job_id="b", status="running", disposition="new", workflow_id="w", record_revision=1, binding_key="b", created_at_s=0, updated_at_s=0)
    core.jobs.update({"a": done, "b": live})
    jobs.cancel_fails.add("b")
    with pytest.raises(JobDeleteConflict):
        jobs.delete("b", None)
    assert jobs.get("b", None) is not None
    jobs.delete("a", None)
    assert jobs.get("a", None) is None
    with pytest.raises(JobNotFound):
        jobs.events("a", None, limit=1)


# --------------------------------------------------------------------------------- M3: Library asset origin and names


class FakeStore:
    def __init__(self):
        self.calls = []

    def __call__(self, data, **kw):
        self.calls.append((data, kw))
        return "asset-1"


def _save(store, content_type, **kw):
    async def chunks():
        yield b"bytes"

    async def go():
        return await GatewayAssetStore(store=store, path_for=lambda _: None, owner="owner-1", **kw).save(
            chunks(), content_type=content_type, max_bytes=100)

    return asyncio.run(go())


@pytest.mark.parametrize("media_type,kind,suffix", [
    ("image/png", "img", ".png"), ("image/jpeg", "img", ".jpg"), ("image/webp", "img", ".webp"),
    ("audio/wav", "aud", ".wav"), ("audio/mpeg", "aud", ".mp3"), ("audio/flac", "aud", ".flac"),
    ("audio/ogg", "aud", ".ogg"), ("video/mp4", "vid", ".mp4"), ("video/webm", "vid", ".webm")])
def test_a_saved_file_gets_its_kind_prefix_and_its_extension_from_the_media_type(media_type, kind, suffix):
    """images-36, audio-31, audio-75, video-73, writers-36: never `.bin` when the type is known."""
    store = FakeStore()
    _save(store, media_type)
    assert (store.calls[0][1]["kind"], store.calls[0][1]["suffix"]) == (kind, suffix)


def test_a_saved_file_carries_the_origin_the_old_route_wrote():
    """images-36, writers-34: {request_id, takes_text, workflow_name}; the Library reads it."""
    store = FakeStore()
    _save(store, "image/png", origin={"workflow_name": "Krea", "request_id": "req-1", "takes_text": True})
    assert store.calls[0][1]["origin"] == {"workflow_name": "Krea", "request_id": "req-1", "takes_text": True}


def test_the_origin_is_built_from_the_pinned_contract_and_the_request_id():
    assert workflow_run.asset_origin({"display_name": "Krea", "inputs": {"prompt": {"type": "string"}}}, "req-1") == {
        "workflow_name": "Krea", "request_id": "req-1", "takes_text": True}
    assert workflow_run.asset_origin({"inputs": {"photo": {"type": "asset"}}}, None) == {"takes_text": False}


def test_a_run_gives_the_adapter_the_request_the_job_answers(db, tmp_path):
    """writers-34: the origin names the request, which is the job's request id."""
    core, port, job_id = _queued_job(db, tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE media_jobs SET request_id = 'req-7', job_json = json_set(job_json, '$.request_id', 'req-7') "
                     "WHERE job_id = ?", (job_id,))
    run(core.execute_run(job_id))
    assert port.ran[0].request_id == "req-7"


# ------------------------------------------------------------------------------------------- M1: receipt fields


def test_receipt_writes_name_the_inputs_the_workflow_and_the_adapters_receipt_facts():
    """writers-03, writers-04, writers-07, images-14, images-31, images-E21."""
    e = entry({**CONTRACT, "version": "0.1.0"})
    req = request(options={"size": "512x512", "n": 1})
    writes = workflow_run.receipt_writes(e, req, {"receipt": {"comfyui_models_freed_for_vram": True}, "rounds": 2})
    assert writes["accepted_inputs"] == ["size", "n", "prompt"]
    assert (writes["resolved_workflow"], writes["manifest_id"], writes["manifest_version"]) == ("demo.run", "demo.run", "0.1.0")
    assert writes["metadata"] == {"comfyui_models_freed_for_vram": True}


def test_a_shape_states_the_receipts_accepted_inputs_in_wire_order():
    """images-29, writers-03: the image golden lists wire fields that are neither inputs nor options (model,
    response_format); the shape states them, and they do not change run identity."""
    e = entry({**CONTRACT, "version": "0.1.0"})
    stated = request(options={"n": 1}, accepted_inputs=("model", "prompt", "n", "response_format"))
    writes = workflow_run.receipt_writes(e, stated, {})
    assert writes["accepted_inputs"] == ["model", "prompt", "n", "response_format"]
    from gateway.workflow_kernel import run_identity
    assert run_identity("o", "m", stated) == run_identity("o", "m", request(options={"n": 1}))


def test_a_row_says_where_its_receipt_opens():
    """images-29, images-07, images-E12: the image rows open it before the route policy; the default is after."""
    base = {"id": "r", "shape": "s", "mode": "sync", "receipt_protocol": "p", "routes": [{"methods": ["POST"], "canonical": "/x"}]}
    assert frontend_row(base).receipt_opens == "after_policy"
    assert frontend_row({**base, "receipt_opens": "before_policy"}).receipt_opens == "before_policy"
    with pytest.raises(Exception):
        frontend_row({**base, "receipt_opens": "never"})

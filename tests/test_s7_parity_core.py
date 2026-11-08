"""S7 parity, core side (docs/design/CODE_TO_DATA_S7_PARITY.md, mechanisms M1 to M5 and M12; builder K).

One test per inventory item in categories 2, 5, 6, 7, 10 and 11 that the run core answers. The item ids are in the test
names or docstrings (docs/design/c2d-s7-parity/{images,audio,video,writers}.md). The route shapes, the node-graph adapter
and the row server answer the other items; their tests are in their builders' files.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
    with media_jobs.connection(db) as conn:
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


def test_a_row_says_where_its_receipt_opens():
    """images-29, images-07, images-E12: the image rows open it before the route policy; the default is after."""
    base = {"id": "r", "shape": "s", "mode": "sync", "receipt_protocol": "p", "routes": [{"methods": ["POST"], "canonical": "/x"}]}
    assert frontend_row(base).receipt_opens == "after_policy"
    assert frontend_row({**base, "receipt_opens": "before_policy"}).receipt_opens == "before_policy"
    with pytest.raises(Exception):
        frontend_row({**base, "receipt_opens": "never"})

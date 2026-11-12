"""The node-graph round and runner: progress the engine reports, the orphan rule, and the two result stages that read
the request's own facts (video move design 1.3, 3.3 step 4, 3.6).

The round runs over a scripted adapter and session, with the sleep recorded instead of waited for, so the clock of a test
is the sum of the sleeps. Nothing here names an engine or a workflow."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from gateway import media_delivery, media_jobs, media_metadata
from gateway.engine_protocol import graph_round, node_validation
from gateway.engine_protocol.description import Poll
from gateway.engine_protocol.errors import TransportFailure, UpstreamError
from gateway.engine_protocol.types import JobRef, JobState, JobStatus
from gateway.node_graph_runner import GraphHost, NodeGraphRunner, Plan, RunFailed, Variant, _input_bindings, failure_of
from gateway.workflow_run import REQUEST_FACTS_KEY

CADENCE = Poll(ramp_s=(), interval_s=5.0, max_polls=None)


class Reply:
    def __init__(self, status: int = 200, body: Any = None, *, broken: bool = False) -> None:
        self.status, self._body, self._broken = status, body, broken

    def json(self) -> Any:
        if self._broken:
            raise ValueError("not json")
        return self._body


def queue_with(running=(), pending=()) -> Reply:
    return Reply(200, {"queue_running": [[1, p] for p in running], "queue_pending": [[2, p] for p in pending]})


class Session:
    conn = object()

    def __init__(self, looks) -> None:
        self.looks = list(looks)
        self.asked = 0

    async def queue(self, *, headers, timeout):
        self.asked += 1
        look = self.looks.pop(0) if len(self.looks) > 1 else self.looks[0]
        if isinstance(look, Exception):
            raise look
        return look


class Adapter:
    label = "Engine"

    def __init__(self, polls) -> None:
        self.polls = list(polls)       # each: a JobState, or an exception to raise; the last one repeats
        self.cancelled = 0

    async def submit(self, conn, op, request):
        return JobRef(op, "p1")

    async def poll(self, conn, ref, *, headers, session, graph):
        step = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        if isinstance(step, Exception):
            raise step
        return JobStatus(ref, step)

    async def fetch_selected(self, conn, finished, binding, outputs, **kw):
        return {"out": [b"file"]}

    async def cancel(self, *a, **kw):
        self.cancelled += 1


def run_round(monkeypatch, adapter, session, *, max_wait_s=100, on_progress=None):
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(graph_round.asyncio, "sleep", sleep)
    seen: list[int] = []

    async def connected() -> bool:
        return False

    async def go():
        return await graph_round.run_round(
            adapter, session, {}, {}, graph={}, label="t", max_wait_s=max_wait_s, control_timeout=1, cancel_connection=lambda: None,
            is_disconnected=connected, binding={}, contract_outputs={}, limits=None, cadence=CADENCE,
            on_progress=on_progress or seen.append)

    return sleeps, seen, go


def outcome(go):
    try:
        return asyncio.run(go())
    except graph_round.RoundFailure as failed:
        return failed


def test_a_prompt_in_neither_history_nor_queue_twice_fifteen_seconds_apart_is_orphaned(monkeypatch):
    adapter, session = Adapter([JobState.pending]), Session([queue_with()])
    sleeps, _, go = run_round(monkeypatch, adapter, session)
    failed = outcome(go)
    assert isinstance(failed, graph_round.RoundFailure) and failed.kind == graph_round.ORPHANED and failed.prompt_id == "p1"
    assert session.asked == 2 and sum(sleeps) == pytest.approx(30)       # looks at 15 s and at 30 s of waiting


def test_a_fast_engine_is_never_asked_for_its_queue(monkeypatch):
    adapter, session = Adapter([JobState.pending, JobState.pending, JobState.succeeded]), Session([queue_with()])
    _, _, go = run_round(monkeypatch, adapter, session)
    assert outcome(go).files == {"out": (b"file",)} and session.asked == 0


def test_a_prompt_the_queue_lists_is_not_orphaned_and_a_running_one_shows_progress(monkeypatch):
    adapter = Adapter([JobState.pending] * 8 + [JobState.succeeded])
    session = Session([queue_with(running=["p1"])])
    _, seen, go = run_round(monkeypatch, adapter, session)
    assert outcome(go).prompt_id == "p1"
    assert session.asked >= 2 and set(seen) == {graph_round.PROGRESS_IN_QUEUE}


def test_a_queued_prompt_is_not_orphaned_and_shows_no_progress(monkeypatch):
    adapter, session = Adapter([JobState.pending]), Session([queue_with(pending=["p1"])])
    _, seen, go = run_round(monkeypatch, adapter, session, max_wait_s=60)
    failed = outcome(go)
    assert failed.kind == graph_round.WAIT_EXCEEDED and seen == []


@pytest.mark.parametrize("look", [Reply(503, {}), Reply(200, [1, 2]), Reply(200, "text"), Reply(200, {"queue_running": 5, "queue_pending": []}),
                                  Reply(200, {"queue_running": []}), Reply(broken=True), TransportFailure("timeout", "no answer"),
                                  RuntimeError("boom")], ids=["status", "list", "text", "not-lists", "missing-half", "not-json", "transport", "other"])
def test_an_answer_that_is_not_the_queue_never_counts_towards_an_orphan(monkeypatch, look):
    adapter, session = Adapter([JobState.pending]), Session([look])
    _, _, go = run_round(monkeypatch, adapter, session, max_wait_s=120)
    failed = outcome(go)
    assert failed.kind == graph_round.WAIT_EXCEEDED and session.asked >= 3


def test_a_look_that_finds_the_prompt_ends_the_streak(monkeypatch):
    adapter = Adapter([JobState.pending])
    session = Session([queue_with(), queue_with(pending=["p1"]), queue_with()])      # absent, present, absent
    _, _, go = run_round(monkeypatch, adapter, session, max_wait_s=50)
    assert outcome(go).kind == graph_round.WAIT_EXCEEDED and session.asked >= 3


@pytest.mark.parametrize("bad", [ValueError("not json"), TypeError("not a mapping"), TransportFailure("unreachable", "down")])
def test_a_history_that_cannot_be_read_is_polled_past_and_never_orphans(monkeypatch, bad):
    adapter, session = Adapter([bad, bad, JobState.succeeded]), Session([queue_with()])
    _, _, go = run_round(monkeypatch, adapter, session)
    assert outcome(go).prompt_id == "p1" and session.asked == 0


def test_a_history_entry_that_is_not_finished_shows_the_old_running_value(monkeypatch):
    adapter, session = Adapter([JobState.running, JobState.running, JobState.succeeded]), Session([queue_with()])
    _, seen, go = run_round(monkeypatch, adapter, session)
    outcome(go)
    assert seen == [graph_round.PROGRESS_IN_HISTORY] * 2 and (graph_round.PROGRESS_IN_QUEUE, graph_round.PROGRESS_IN_HISTORY) == (25, 50)


def test_a_history_that_shows_the_prompt_again_clears_a_streak(monkeypatch):
    adapter = Adapter([JobState.pending] * 3 + [JobState.running] + [JobState.pending])
    session = Session([queue_with()])
    _, _, go = run_round(monkeypatch, adapter, session, max_wait_s=40)
    # a look at 15 s is absent, the history then shows the prompt running, and the next look (at 30 s) is a first one again
    failed = outcome(go)
    assert failed.kind == graph_round.WAIT_EXCEEDED


def test_an_orphaned_round_is_the_old_upstream_failure_with_the_old_text():
    failed = failure_of(graph_round.RoundFailure(graph_round.ORPHANED, prompt_id="p1"), SimpleNamespace(label="Engine"), None, "binding")
    assert isinstance(failed, RunFailed)
    assert (failed.code, failed.category, failed.upstream_status) == ("upstream_orphaned", "upstream", 502)
    assert failed.client_message == "orphaned: Engine does not report this prompt_id"
    assert failed.retry_reason is None


# ------------------------------------------------------------------------------------------------ the result stages


def runner(wire_id=None) -> NodeGraphRunner:
    host = GraphHost(engine=None, identity=None, problem=None, adapter=None, connection=None, limits=None,
                     control_timeout=None, headers=None, batching=None, stages={}, wire_id=wire_id)
    return NodeGraphRunner("b", "a", host)


def context(job_id="mj_a", options=None, stages=()):
    return SimpleNamespace(job_id=job_id, payload={"body": {"options": options or {}}}, pin=SimpleNamespace(binding={"result_processing": list(stages)}))


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 20


def test_the_strip_stage_runs_only_when_the_request_asked_for_silence(monkeypatch):
    calls: list[tuple[bytes, str]] = []
    monkeypatch.setattr(media_metadata, "strip_audio", lambda raw, kind: calls.append((raw, kind)) or b"silent")
    silent = asyncio.run(runner()._stages(context(options={"audio": False}, stages=["strip_audio"]), [MP4]))
    assert silent == [b"silent"] and calls == [(MP4, "video/mp4")]
    for options in ({}, {"audio": True}):
        kept = asyncio.run(runner()._stages(context(options=options, stages=["strip_audio"]), [MP4]))
        assert kept == [MP4]
    assert len(calls) == 1


def test_a_strip_that_fails_fails_the_run_with_the_old_sentence(monkeypatch):
    def broken(raw, kind):
        raise media_metadata.MediaMetadataError("ffmpeg said no")

    monkeypatch.setattr(media_metadata, "strip_audio", broken)
    with pytest.raises(RunFailed) as caught:
        asyncio.run(runner()._stages(context(options={"audio": False}, stages=["strip_audio"]), [MP4]))
    assert caught.value.client_message == "Could not remove the audio track from the video" and caught.value.upstream_status == 502


@pytest.fixture
def job_with_provenance(tmp_path, monkeypatch):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    job = media_jobs.create(media_jobs.MediaJob(intent="workflow.run", metadata={"mode": "workflow.run", REQUEST_FACTS_KEY: {
        "provenance": {"request_id": "{wire_id}", "nested": ["{wire_id}", {"seed": 7}]}}}), path=db)
    return job.job_id


def test_the_provenance_stage_stamps_each_file_with_the_record_the_request_kept(job_with_provenance, monkeypatch):
    stamped: list[tuple[bytes, str, Any]] = []
    monkeypatch.setattr(media_delivery, "attach_provenance", lambda raw, kind, record: stamped.append((raw, kind, record)) or b"stamped")
    out = asyncio.run(runner(wire_id=lambda job_id: f"wire_{job_id}")._stages(
        context(job_id=job_with_provenance, stages=["attach_provenance"]), [MP4]))
    assert out == [b"stamped"]
    (raw, kind, record), = stamped
    assert (raw, kind) == (MP4, "video/mp4")
    assert record == {"request_id": f"wire_{job_with_provenance}", "nested": [f"wire_{job_with_provenance}", {"seed": 7}]}


def test_the_provenance_stage_leaves_a_file_alone_when_the_request_kept_no_record(tmp_path, monkeypatch):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    job = media_jobs.create(media_jobs.MediaJob(intent="workflow.run", metadata={"mode": "workflow.run"}), path=db)
    monkeypatch.setattr(media_delivery, "attach_provenance", lambda *a: pytest.fail("nothing to stamp"))
    assert asyncio.run(runner()._stages(context(job_id=job.job_id, stages=["attach_provenance"]), [MP4])) == [MP4]


def test_a_stage_the_run_does_not_know_still_fails_the_run():
    with pytest.raises(RunFailed):
        asyncio.run(runner()._stages(context(stages=["no_such_stage"]), [MP4]))

# Declared output words come from the shared kind table; a run that declares no output gets the table's generic file words.
@pytest.mark.parametrize("media_type,label,noun,plural,article", [
    ("video/mp4", "Video", "video", "videos", "a"),
    ("image/png", "Image", "image", "images", "an"),
    (None, "File", "file", "files", "a"),
    ("audio/wav", "Audio", "audio", "audios", "an"),
    ("smell/example", "Smell", "smell", "smells", "a"),
])
@pytest.mark.parametrize("failure_kind", [graph_round.WAIT_EXCEEDED, graph_round.DROPPED])
def test_failure_words_follow_declared_output_without_changing_metadata(media_type, label, noun, plural, article, failure_kind):
    from gateway.node_graph_runner import output_noun

    outputs = {"result": {"media_type": media_type}} if media_type else {}
    failed = failure_of(graph_round.RoundFailure(failure_kind, queue_position=3),
                        SimpleNamespace(label="Engine"), None, "binding", outputs)
    assert output_noun(outputs) == plural
    if failure_kind == graph_round.WAIT_EXCEEDED:
        assert failed.client_message == f"{label} generation timed out before Engine returned {article} {noun} (queue_position=3)"
        assert (failed.code, failed.category, failed.upstream_status, failed.retryable, failed.retry_reason, failed.queue_position) == (
            "image_generation_timeout", "timeout", 504, True, "binding_timeout", 3)
    else:
        assert failed.client_message == f"{label} backend connection dropped"
        assert (failed.code, failed.category, failed.upstream_status, failed.retryable, failed.retry_reason, failed.queue_position) == (
            "upstream_dropped", "upstream", 502, True, None, None)


@pytest.mark.parametrize("failure_kind", [graph_round.WAIT_EXCEEDED, graph_round.DROPPED])
def test_one_round_passes_its_declared_video_output_to_failure_builder(monkeypatch, failure_kind):
    async def fail_round(*args, **kwargs):
        raise graph_round.RoundFailure(failure_kind)

    monkeypatch.setattr(graph_round, "run_round", fail_round)
    monkeypatch.setattr(media_jobs, "update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(media_jobs, "get", lambda *args, **kwargs: {})
    host = GraphHost(engine=None, identity=None, problem=None, adapter=None, connection=None, limits=None,
                     control_timeout=lambda: 1, headers=None, batching=None, stages={})
    graph = NodeGraphRunner("binding", "adapter", host)
    ctx = SimpleNamespace(job_id="test", inputs={}, generated={}, record_result_metadata=lambda value: None,
                          record_progress=lambda value: None)
    adapter = SimpleNamespace(label="Engine", compile=lambda *args, **kwargs: ({}, {}))
    with pytest.raises(RunFailed) as caught:
        asyncio.run(graph._one_round(
            ctx=ctx, adapter=adapter, session=None, runtime=SimpleNamespace(wait_s=1),
            op="generate", template_request=SimpleNamespace(params={}), manifest_inputs={}, offset=0,
            batching=None, plan=None, target=1, probe=None, headers={}, binding={},
            outputs={"result": {"media_type": "video/mp4"}}, limits=None, poll=CADENCE,
            label="test", failure_binding="binding", conn=None, retry_freed=lambda: False, staged={}))
    expected = ("Video generation timed out before Engine returned a video"
                if failure_kind == graph_round.WAIT_EXCEEDED else "Video backend connection dropped")
    assert caught.value.client_message == expected


def test_unknown_kind_words_use_the_shared_table_fallback():
    from gateway import asset_types
    from gateway.node_graph_runner import output_noun

    assert "smell" not in asset_types.KIND_NAMES
    assert output_noun({"out": {"media_type": "smell/example"}}) == "smells"


# The complete HTTP 400 response captured from the installed engine on 2026-11-12, without shortening its details.
REAL_INPUT_REFUSAL = (
    b'{"error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation", "details": "", '
    b'"extra_info": {}}, "node_errors": {"5": {"errors": [{"type": "value_not_in_list", "message": "Value not in list", '
    b'"details": "keyscale: \'Dm\' not in (list of length 34)", "extra_info": {"input_name": "keyscale", "input_config": null, '
    b'"received_value": "Dm"}}], "dependent_outputs": ["38"], "class_type": "TextEncodeAceStepAudio1.5"}}}'
)


def test_real_input_refusal_decodes_only_identifiers_and_validation_type():
    body = node_validation.parse_refusal_body(REAL_INPUT_REFUSAL)
    assert body == json.loads(REAL_INPUT_REFUSAL)
    facts = node_validation.input_refusals(body)
    assert facts == (node_validation.InputRefusal("5", "keyscale", "value_not_in_list"),)
    assert "Dm" not in repr(facts) and "TextEncode" not in repr(facts) and "length 34" not in repr(facts)


@pytest.mark.parametrize("raw", [
    b"", b"{}", b"null", b"[]", b"not json", b"\xff", REAL_INPUT_REFUSAL[:-1], REAL_INPUT_REFUSAL + b"{}",
    b'{"node_errors": {}, "node_errors": {}}', b'{"error": NaN}',
    REAL_INPUT_REFUSAL.replace(b'{"error":', b'{"error": {}, "error":', 1),
    REAL_INPUT_REFUSAL.replace(b'"received_value": "Dm"', b'"received_value": NaN'),
    b"[" * 1500 + b"0" + b"]" * 1500,
    REAL_INPUT_REFUSAL + b" " * node_validation.MAX_REFUSAL_BODY_BYTES,
])
def test_incomplete_ambiguous_or_oversize_refusals_supply_no_input_evidence(raw):
    assert node_validation.input_refusals(node_validation.parse_refusal_body(raw)) == ()


def test_refusal_body_bound_accepts_a_complete_body_without_reading_a_prefix():
    raw = REAL_INPUT_REFUSAL + b" " * (node_validation.MAX_REFUSAL_BODY_BYTES - len(REAL_INPUT_REFUSAL))
    assert node_validation.input_refusals(node_validation.parse_refusal_body(raw))
    assert node_validation.parse_refusal_body(raw + b" ") is None


@pytest.mark.parametrize("other", [
    None, {}, {"errors": []}, {"errors": [None]}, {"errors": [{"type": "unknown_validation"}]},
    {"errors": [{"type": "value_not_in_list", "extra_info": {}}]},
    {"errors": [{"type": "value_not_in_list", "extra_info": {"input_name": 3}}]},
    {"errors": [{"type": "value_not_in_list", "extra_info": {"input_name": " "}}]},
])
def test_one_unknown_node_or_error_discards_the_whole_refusal(other):
    body = json.loads(REAL_INPUT_REFUSAL)
    body["node_errors"]["6"] = other
    assert node_validation.input_refusals(body) == ()


def test_unknown_envelope_and_excessive_facts_are_not_input_evidence():
    body = json.loads(REAL_INPUT_REFUSAL)
    body["error"]["type"] = "unrecognized_failure"
    assert node_validation.input_refusals(body) == ()
    body = json.loads(REAL_INPUT_REFUSAL)
    body["node_errors"]["5"]["errors"] *= node_validation.MAX_REFUSAL_FACTS + 1
    assert node_validation.input_refusals(body) == ()


def _one_input_refusal(monkeypatch, *, inputs=None, media=None, generated=None, staged=None, binding=None,
                       status=400, raw=REAL_INPUT_REFUSAL, batching=None, edit=None, during_submit=None, excerpt=""):
    monkeypatch.setattr(media_jobs, "update_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(media_jobs, "get", lambda *args, **kwargs: {})
    ctx = SimpleNamespace(job_id="test", inputs=inputs or {}, generated=generated or {}, media=media or {},
                          record_result_metadata=lambda value: None, record_progress=lambda value: None)

    class RefusingAdapter:
        label = "Engine"

        def compile(self, op, request, *, edit=None):
            graph = {"5": {"class_type": "Encoder", "inputs": {"keyscale": request.body.get("scale", "Dm")}},
                     "9": {"class_type": "Output", "inputs": {"enabled": True}}}
            if edit is not None:
                edit(graph)
            return graph, {"prompt": graph}

        async def submit(self, conn, op, request):
            if during_submit is not None:
                during_submit(ctx)
            raise UpstreamError(status, "The engine refused the prompt.", body=raw, excerpt=excerpt)

    host = GraphHost(engine=None, identity=None, problem=None, adapter=None, connection=None, limits=None,
                     control_timeout=lambda: 1, headers=None, batching=None, stages={})
    with pytest.raises(RunFailed) as caught:
        asyncio.run(NodeGraphRunner("binding", "adapter", host)._one_round(
            ctx=ctx, adapter=RefusingAdapter(), session=SimpleNamespace(conn=None), runtime=SimpleNamespace(wait_s=1),
            op="generate", template_request=SimpleNamespace(params={}), manifest_inputs={}, offset=0,
            batching=batching, plan=None, target=1, probe=None, headers={},
            binding=binding if binding is not None else {"map": {"scale": [{"node": "5", "input": "keyscale"}]}},
            outputs={}, limits=None, poll=CADENCE, label="test", failure_binding="binding", conn=None,
            retry_freed=lambda: False, staged=staged or {}, edit=edit))
    return caught.value


@pytest.mark.parametrize("status", [400, 422])
def test_real_refusal_uses_pinned_public_name_and_actual_status_without_echo(monkeypatch, status):
    failed = _one_input_refusal(monkeypatch, inputs={"scale": "Dm"}, status=status)
    assert (failed.code, failed.category, failed.upstream_status, failed.retryable) == ("invalid_input", "invalid_input", status, False)
    assert failed.client_message == "Input 'scale' is not accepted. Choose a value from the workflow input schema."
    assert "Dm" not in failed.client_message and "keyscale" not in failed.client_message and "TextEncode" not in failed.client_message
    round_failure = failed.__cause__
    assert round_failure.refusal_body == json.loads(REAL_INPUT_REFUSAL)
    assert "Dm" not in str(round_failure) and "Dm" not in repr(round_failure)


@pytest.mark.parametrize("kwargs", [
    {}, {"generated": {"scale": "Dm"}}, {"inputs": {"scale": "Dm"}, "generated": {"scale": "Dm"}},
    {"inputs": {"scale": "Dm"}, "staged": {"scale": "Dm"}},
    {"inputs": {"scale": "Dm"}, "status": 500},
    {"inputs": {"scale": "Dm"}, "raw": REAL_INPUT_REFUSAL[:-1]},
    {"inputs": {"scale": "Dm"}, "binding": {}},
    {"inputs": {"scale": "Dm"}, "binding": {"map": {"scale": [{"path": "5.inputs.keyscale"}]}}},
    {"inputs": {"scale": "Dm"}, "binding": {"map": {
        "scale": [{"node": "5", "input": "keyscale"}], "other": [{"path": "5.inputs.keyscale"}]}}},
    {"inputs": {"scale": "Dm"}, "binding": {"map": {
        "scale": [{"node": "5", "input": "keyscale"}], "other": [{"node": "5", "input": "keyscale"}]}}},
])
def test_defaults_overrides_ambiguous_bindings_and_unproven_refusals_keep_upstream_failure(monkeypatch, kwargs):
    failed = _one_input_refusal(monkeypatch, **kwargs)
    assert (failed.code, failed.category, failed.upstream_status) == ("upstream_refused", "upstream", 502)
    assert failed.client_message == "Engine: The engine refused this request."


def test_caller_media_presence_can_attribute_the_staged_value(monkeypatch):
    failed = _one_input_refusal(monkeypatch, media={"scale": "caller asset"}, staged={"scale": "Dm"})
    assert failed.category == "invalid_input"


def test_caller_provenance_is_fixed_before_the_round_waits(monkeypatch):
    binding = {"map": {"scale": [{"node": "5", "input": "keyscale"}]}}

    def mutate(ctx):
        ctx.inputs.clear()
        binding["map"].clear()

    failed = _one_input_refusal(monkeypatch, inputs={"scale": "Dm"}, binding=binding, during_submit=mutate)
    assert failed.category == "invalid_input" and "'scale'" in failed.client_message


def test_late_caller_inputs_cannot_convert_a_default_refusal_to_caller_fault(monkeypatch):
    failed = _one_input_refusal(monkeypatch, during_submit=lambda ctx: ctx.inputs.update(scale="Dm"))
    assert failed.category == "upstream"


def test_a_variant_cannot_blame_a_caller_for_an_edited_node(monkeypatch):
    def edit(graph):
        graph["5"]["class_type"] = "DifferentEncoder"

    failed = _one_input_refusal(monkeypatch, inputs={"scale": "Dm"}, edit=edit)
    assert failed.category == "upstream"


def test_a_variant_cannot_blame_a_caller_for_a_changed_input_value(monkeypatch):
    def edit(graph):
        graph["5"]["inputs"]["keyscale"] = "different value"

    failed = _one_input_refusal(monkeypatch, inputs={"scale": "Dm"}, edit=edit)
    assert failed.category == "upstream"


def test_an_unrelated_variant_edit_preserves_provenance_and_input_refusal_skips_oom_retry(monkeypatch):
    class Batching:
        def round_inputs(self, plan, target):
            return {}

        async def free_after_refusal(self, plan, session):
            pytest.fail("an attributed input refusal must not free models or retry")

    def edit(graph):
        graph["9"]["inputs"]["enabled"] = False

    failed = _one_input_refusal(monkeypatch, inputs={"scale": "Dm"}, batching=Batching(), edit=edit,
                                excerpt="engine-echoed cuda out of memory")
    assert failed.category == "invalid_input"


def test_a_batching_override_is_not_the_callers_input(monkeypatch):
    batching = SimpleNamespace(round_inputs=lambda plan, target: {"scale": "Dm"})
    failed = _one_input_refusal(monkeypatch, inputs={"scale": "Dm"}, batching=batching)
    assert failed.category == "upstream"


@pytest.mark.parametrize("other_owner", [None, "defaulted", "second"])
def test_mixed_refusals_are_never_partly_attributed(other_owner):
    body = json.loads(REAL_INPUT_REFUSAL)
    body["node_errors"]["6"] = {"errors": [{"type": "value_not_in_list", "extra_info": {"input_name": "mode"}}]}
    bindings = {("5", "keyscale"): "scale"}
    if other_owner is not None:
        bindings[("6", "mode")] = other_owner
    failed = failure_of(graph_round.RoundFailure(graph_round.REFUSED, status=400, refusal_body=body),
                        SimpleNamespace(label="Engine"), None, "binding", input_bindings=bindings,
                        supplied_inputs=frozenset({"scale", "second"}))
    assert failed.category == "upstream" and failed.upstream_status == 502


def test_one_public_input_can_fan_out_to_multiple_refused_targets():
    body = json.loads(REAL_INPUT_REFUSAL)
    body["node_errors"]["6"] = body["node_errors"]["5"]
    bindings = _input_bindings({"map": {"scale": [{"node": node, "input": "keyscale"} for node in ("5", "6")]}})
    with pytest.raises(TypeError):
        bindings[("5", "keyscale")] = "changed"
    failed = failure_of(graph_round.RoundFailure(graph_round.REFUSED, status=400, refusal_body=body),
                        SimpleNamespace(label="Engine"), None, "binding", input_bindings=bindings,
                        supplied_inputs=frozenset({"scale"}))
    assert failed.category == "invalid_input"


@pytest.mark.parametrize("category,attempts", [("invalid_input", 1), ("upstream", 2)])
def test_only_non_input_failures_can_retry_a_variant(monkeypatch, category, attempts):
    calls = []
    variant = Variant(edit=lambda graph: None, facts={"chosen": True}, fallback_facts={"fallback": True})

    async def choose(ctx):
        return variant

    async def collect(*args, **kwargs):
        calls.append(True)
        raise RunFailed(category, category, "The request failed.", upstream_status=400 if category == "invalid_input" else 502)

    graph = NodeGraphRunner("binding", "adapter", SimpleNamespace(variant=choose))
    monkeypatch.setattr(graph, "_collect", collect)
    with pytest.raises(RunFailed):
        asyncio.run(graph._rounds(SimpleNamespace(pin=SimpleNamespace(contract={})), None, None, "generate", {}))
    assert len(calls) == attempts


@pytest.mark.parametrize("media_type,noun", [("audio/wav", "audios"), ("video/mp4", "videos"), ("image/png", "images")])
def test_repeated_empty_rounds_name_the_declared_output(monkeypatch, media_type, noun):
    class Batching:
        async def plan(self, *args):
            return Plan(total=1, max_rounds=2)

        async def target(self, *args):
            return 1

        def probe(self, *args):
            return None

    @asynccontextmanager
    async def opened(conn):
        yield None

    async def empty(*args, **kwargs):
        return None, []

    host = SimpleNamespace(batching=lambda *args: Batching(), headers=lambda ctx: {}, limits=lambda: None)
    graph = NodeGraphRunner("binding", "adapter", host)
    monkeypatch.setattr(graph, "_one_round", empty)
    ctx = SimpleNamespace(inputs={}, generated={}, payload={"body": {}},
                          pin=SimpleNamespace(contract={"inputs": {}, "outputs": {"result": {"media_type": media_type}}},
                                              binding={}, envelope=SimpleNamespace(binding_key="binding")))
    runtime = SimpleNamespace(request=lambda *args: None, connection_for=lambda op: None)
    adapter = SimpleNamespace(label="Engine", poll_for=lambda op: CADENCE, open=opened)
    with pytest.raises(RunFailed) as caught:
        asyncio.run(graph._collect(ctx, adapter, runtime, "generate", {}, None))
    assert caught.value.client_message == f"Engine returned no {noun}"
    assert (caught.value.code, caught.value.upstream_status) == ("upstream_empty", 502)

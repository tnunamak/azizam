"""The node-graph round and runner: progress the engine reports, the orphan rule, and the two result stages that read
the request's own facts (video move design 1.3, 3.3 step 4, 3.6).

The round runs over a scripted adapter and session, with the sleep recorded instead of waited for, so the clock of a test
is the sum of the sleeps. Nothing here names an engine or a workflow."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from gateway import media_delivery, media_jobs, media_metadata
from gateway.engine_protocol import graph_round
from gateway.engine_protocol.description import Poll
from gateway.engine_protocol.errors import TransportFailure
from gateway.engine_protocol.types import JobRef, JobState, JobStatus
from gateway.node_graph_runner import GraphHost, NodeGraphRunner, RunFailed, failure_of
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

    async def fetch_selected(self, conn, finished, binding, outputs, names=None, **kw):
        if names is not None:
            names["out"] = ["clip_00001_.mp4"]
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


def test_a_round_keeps_the_name_the_engine_gave_each_file(monkeypatch):
    adapter, session = Adapter([JobState.succeeded]), Session([queue_with()])
    _, _, go = run_round(monkeypatch, adapter, session)
    result = outcome(go)
    assert result.files == {"out": (b"file",)} and result.filenames == {"out": ("clip_00001_.mp4",)}


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
    host = GraphHost(engine=None, identity=None, problem=None, adapter=None, connection=None, limits=None, max_wait_s=None,
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

"""The video wire's job views (S7 parity M7): the id a caller sees, and the status, list, delete and content answers.

The shape renders ``RunJobView`` values from the run core's job service; these tests use the in-memory fakes of the frozen
interfaces, so they pin the wire and nothing of the engine. The routes that serve it are tested through HTTP in the video
parity files."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from gateway.frontend_shapes import bundled_rows, build_shape
from gateway.workflow_runtime_types import JobNotFound, RequestFacts, ResultRef, RunFailure, RunJobView
from tests.workflow_runtime_fakes import FakeJobService, FakeRunCore


@pytest.fixture
def shape():
    row = next(r for r in bundled_rows() if any(route.canonical == "/v1/videos" and "POST" in route.methods for route in r.routes))
    return build_shape(row)


def view(job_id="mj_a", status="queued", **kw) -> RunJobView:
    base = dict(job_id=job_id, status=status, disposition="replay", workflow_id="w", record_revision=1, binding_key="b",
                created_at_s=100, updated_at_s=130, finished_at_s=120 if status in ("succeeded", "failed", "canceled") else None)
    return RunJobView(**{**base, **kw})


def body(response):
    return json.loads(response.body)


def test_the_wire_id_is_the_wire_name_and_the_job_id_and_round_trips(shape):
    wire = shape.wire_id("mj_0123")
    assert wire == "video_mj_0123" and shape.job_id_of(wire) == "mj_0123"


@pytest.mark.parametrize("bad", ["", "video_", "mj_0123", "vid_mj_0123", "Video_mj_0123"])
def test_a_malformed_wire_id_names_no_job_and_is_a_400_invalid_id(shape, bad):
    assert shape.job_id_of(bad) is None
    refusal = shape.invalid_id()
    assert refusal.status_code == 400 and body(refusal)["error"]["message"] == "invalid video_id"


@pytest.mark.parametrize(("status", "wire", "own", "progress"), [
    ("queued", "queued", "queued", 0), ("running", "in_progress", "running", 0),
    ("succeeded", "completed", "succeeded", 100), ("failed", "failed", "failed", 0), ("canceled", "failed", "failed", 0)])
def test_status_uses_the_openai_word_beside_the_job_stores_own(shape, status, wire, own, progress):
    answer = body(shape.render_status(view(status=status)))
    assert (answer["id"], answer["object"], answer["status"], answer["azizam_status"], answer["progress"]) == (
        "video_mj_a", "video", wire, own, progress)
    assert answer["created_at"] == 100 and answer["error"] is None
    assert (answer["completed_at"] is not None) == (status != "queued" and status != "running")


def test_status_carries_the_failure_and_the_request_facts_the_run_kept(shape):
    failed = view(status="failed", failure=RunFailure(code="oom", category="upstream", message="out of memory"),
                  result_metadata={"model": "m", "size": "640x480", "seconds": 4, "unrelated": 1})
    answer = body(shape.render_status(failed))
    assert answer["error"] == {"code": "oom", "message": "out of memory"}
    assert (answer["model"], answer["size"], answer["seconds"]) == ("m", "640x480", 4) and "unrelated" not in answer


def test_the_create_answer_is_the_status_object_without_a_location(shape):
    answer = shape.render_job(view())
    assert body(answer)["id"] == "video_mj_a" and "location" not in answer.headers


def test_the_listing_wraps_the_pages_views_with_first_last_and_has_more(shape):
    page = body(shape.render_listing([view("mj_b"), view("mj_a")], True))
    assert (page["object"], page["first_id"], page["last_id"], page["has_more"]) == ("list", "video_mj_b", "video_mj_a", True)
    assert [item["id"] for item in page["data"]] == ["video_mj_b", "video_mj_a"]
    empty = body(shape.render_listing([], False))
    assert (empty["data"], empty["first_id"], empty["last_id"], empty["has_more"]) == ([], None, None, False)


def test_delete_answers_the_deleted_object_and_a_stuck_cancel_is_409(shape):
    assert body(shape.render_deleted("mj_a")) == {"id": "video_mj_a", "object": "video", "deleted": True}
    conflict = shape.render_delete_conflict()
    assert conflict.status_code == 409 and body(conflict)["error"]["code"] == "video_cancel_failed"
    assert shape.unknown_id().status_code == 404


@pytest.mark.parametrize(("status", "word"), [("queued", "queued"), ("running", "in_progress"), ("failed", "failed")])
def test_content_of_an_unfinished_job_is_404_not_ready_with_the_status_word(shape, status, word):
    answer = shape.render_bytes(view(status=status), None, None)
    assert answer.status_code == 404 and body(answer)["error"]["code"] == "video_not_ready"
    assert f"status={word}" in body(answer)["error"]["message"]


def test_content_of_a_finished_job_is_its_one_file(shape):
    core = FakeRunCore()
    jobs = FakeJobService(core)
    done = view(status="succeeded", results=(ResultRef("video", 0, "video/mp4", asset_id="a"),))
    core.jobs["mj_a"] = done
    core.facts["mj_a"] = RequestFacts({"metadata_mode": "none"})
    jobs.open_result = lambda job_id, ref, caller: type("C", (), {"body": b"mp4-bytes", "media_type": ref.media_type})()
    answer = shape.render_bytes(done, jobs, None, metadata_mode="full", caller_person_id="p1")
    assert answer.status_code == 403 and body(answer)["error"]["code"] == "prompt_not_accessible"
    answer = shape.render_bytes(done, jobs, None)             # the job's own choice (none) stands without a request's
    assert (answer.status_code, answer.body, answer.media_type) == (200, b"mp4-bytes", "video/mp4")


def test_the_fake_service_lists_what_the_shape_renders(shape):
    core = FakeRunCore()
    jobs = FakeJobService(core)
    for index, name in enumerate(("mj_a", "mj_b", "mj_c")):
        core.jobs[name] = view(name, created_at_s=100 + index, intent="x")
    views, more = jobs.list(None, intents=("x",), limit=2)
    assert [v.job_id for v in views] == ["mj_c", "mj_b"] and more is True
    with pytest.raises(JobNotFound):
        jobs.list(None, intents=("x",), limit=2, after_id="nope")


# ------------------------------------------------------------------------------------------------ facts and content (V5b)


def finished(core, jobs, *, facts=None, metadata=None, media_type="video/mp4", job_id="mj_a"):
    done = view(job_id, status="succeeded", results=(ResultRef("video", 0, media_type, asset_id="asset-1"),),
                result_metadata=metadata or {"filename": "clip.mp4"})
    core.jobs[job_id] = done
    core.facts[job_id] = facts if facts is not None else RequestFacts({}, prompt="a prompt", maker_person_id="p1")
    jobs.open_result = lambda job_id, ref, caller: type("C", (), {"body": b"mp4-bytes", "media_type": ref.media_type})()
    return done


@pytest.fixture
def served():
    core = FakeRunCore()
    return core, FakeJobService(core)


def test_status_shows_the_facts_the_request_kept_and_the_prompt_for_the_maker(shape):
    facts = RequestFacts({"model": "m", "size": "640x480", "seconds": 4, "actual_seconds": 3, "resolved_model": "m-1",
                          "warnings": ["w"], "extended_from_video_id": "video_x"}, prompt="draw", maker_person_id="p1")
    answer = body(shape.render_status(view(status="succeeded"), facts))
    assert (answer["model"], answer["size"], answer["seconds"], answer["actual_seconds"], answer["azizam_resolved_model"]) == (
        "m", "640x480", 4, 3, "m-1")
    assert (answer["prompt"], answer["warnings"], answer["extended_from_video_id"]) == ("draw", ["w"], "video_x")
    assert answer["expires_at"] == 100 + 86400 and "resolved_model" not in answer
    other = body(shape.render_status(view(), RequestFacts({"model": "m"})))
    assert other["prompt"] is None


def test_status_progress_is_the_views_while_running_and_100_when_done(shape):
    assert body(shape.render_status(view(status="running", progress=42)))["progress"] == 42
    assert body(shape.render_status(view(status="succeeded", progress=7)))["progress"] == 100


def test_a_listing_renders_ready_bodies_beside_views_in_the_given_order(shape):
    older = {"id": "video_p9", "object": "video", "status": "queued"}
    page = body(shape.render_listing([view("mj_b"), older], False, {"mj_b": RequestFacts({"model": "m"})}))
    assert [item["id"] for item in page["data"]] == ["video_mj_b", "video_p9"] and page["data"][0]["model"] == "m"
    assert page["last_id"] == "video_p9"


@pytest.mark.parametrize(("status", "word", "progress"), [("queued", "queued", 40), ("running", "running", 40),
                                                          ("succeeded", "succeeded", 100), ("failed", "failed", 40), ("canceled", "failed", 40)])
def test_a_listed_job_keeps_the_old_status_word_and_item_fields(shape, status, word, progress):
    failure = RunFailure(code="oom", category="upstream", message="out of memory") if status == "failed" else None
    item = body(shape.render_listing([view(status=status, failure=failure, progress=40)], False))["data"][0]
    assert (item["status"], item["progress"]) == (word, progress)
    assert set(item) == {"id", "object", "model", "size", "seconds", "created_at", "completed_at", "prompt", "status", "progress"} | (
        {"error"} if failure else set())
    assert item.get("error") in (None, "out of memory")


def test_the_maker_gets_full_metadata_with_the_files_headers_and_the_library_is_told_once(shape, served):
    core, jobs = served
    done = finished(core, jobs)
    told = []
    answer = shape.render_bytes(done, jobs, None, caller_person_id="p1", on_delivered=lambda *a: told.append(a))
    assert (answer.status_code, answer.body, answer.media_type) == (200, b"mp4-bytes", "video/mp4")
    assert answer.headers["content-disposition"] == 'inline; filename="clip.mp4"'
    (wire, asset, filename, media_type, facts), = told
    assert (wire, asset, filename, media_type, facts.maker_person_id) == ("video_mj_a", "asset-1", "clip.mp4", "video/mp4", "p1")


@pytest.mark.parametrize(("facts", "person", "never_keep"), [
    (RequestFacts({}, prompt="a prompt", maker_person_id="p1"), "p2", False),        # someone else
    (RequestFacts({}, prompt="a prompt", maker_person_id="p1"), None, False),        # no person
    (RequestFacts({}, prompt=None, maker_person_id="p1"), "p1", False),              # the prompt is no longer kept
    (RequestFacts({}, prompt="a prompt", maker_person_id="p1"), "p1", True),         # the household keeps no prompts
    (RequestFacts({}), "p1", False)], ids=["other", "anonymous", "prompt-gone", "never-keep", "no-maker"])
def test_full_metadata_is_refused_unless_the_maker_and_a_kept_prompt(shape, served, facts, person, never_keep):
    core, jobs = served
    done = finished(core, jobs, facts=facts)
    told = []
    answer = shape.render_bytes(done, jobs, None, caller_person_id=person, never_keep=never_keep, on_delivered=lambda *a: told.append(a))
    assert answer.status_code == 403 and body(answer)["error"]["code"] == "prompt_not_accessible" and told == []


def test_a_request_for_no_metadata_strips_the_bytes_sent_and_needs_no_maker(shape, served, monkeypatch):
    from gateway import media_delivery
    core, jobs = served
    done = finished(core, jobs, facts=RequestFacts({"metadata_mode": "full"}))
    seen = []
    monkeypatch.setattr(media_delivery, "deliver_bytes", lambda content, suffix, mode: seen.append((content, suffix, mode)) or b"clean")
    answer = shape.render_bytes(done, jobs, None, metadata_mode="none", caller_person_id="p9")
    assert (answer.status_code, answer.body) == (200, b"clean") and seen == [(b"mp4-bytes", ".mp4", "none")]


def test_a_strip_that_fails_is_a_502_and_never_sends_the_full_copy(shape, served, monkeypatch):
    from gateway import media_delivery, media_metadata
    core, jobs = served
    done = finished(core, jobs)

    def broken(*a):
        raise media_metadata.MediaMetadataError("no")

    monkeypatch.setattr(media_delivery, "deliver_bytes", broken)
    answer = shape.render_bytes(done, jobs, None, metadata_mode="none")
    assert answer.status_code == 502 and body(answer)["error"]["message"] == "Could not prepare video for delivery"


@pytest.mark.parametrize(("name", "media"), [("a.webm", "video/webm"), ("a.mov", "video/quicktime"), ("a.mkv", "video/x-matroska"),
                                             ("a.bin", "application/octet-stream")])
def test_the_media_type_follows_the_file_extension(shape, served, name, media):
    core, jobs = served
    done = finished(core, jobs, metadata={"filename": name})
    assert shape.render_bytes(done, jobs, None, caller_person_id="p1").media_type == media


def test_a_finished_job_without_exactly_one_video_is_a_500(shape, served):
    core, jobs = served
    done = finished(core, jobs, media_type="image/png")
    answer = shape.render_bytes(done, jobs, None, caller_person_id="p1")
    assert answer.status_code == 500 and body(answer)["error"]["message"] == "No video output found in job"


def test_not_ready_is_an_invalid_request_error(shape):
    assert body(shape.render_bytes(view(status="running"), None, None))["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled"])
def test_completed_at_is_the_finish_time_and_not_the_last_update(shape, status):
    answer = body(shape.render_status(view(status=status, updated_at_s=500)))
    assert answer["completed_at"] == 120 and answer["updated_at"] == 500


def test_a_job_canceled_while_its_file_was_read_delivers_and_publishes_nothing(shape, served):
    core, jobs = served
    done = finished(core, jobs)
    told = []

    def read_then_cancel(job_id, ref, caller):
        core.jobs[job_id] = replace(core.jobs[job_id], status="canceled")      # the cancel lands during the read
        return type("C", (), {"body": b"mp4-bytes", "media_type": ref.media_type})()

    jobs.open_result = read_then_cancel
    answer = shape.render_bytes(done, jobs, None, caller_person_id="p1", on_delivered=lambda *a: told.append(a))
    assert answer.status_code == 404 and told == []


def test_a_repeat_fetch_tells_the_library_again_and_leaves_reuse_to_it(shape, served):
    core, jobs = served
    done = finished(core, jobs)
    told = []
    for _ in range(2):
        shape.render_bytes(done, jobs, None, caller_person_id="p1", on_delivered=lambda *a: told.append(a[1]))
    assert told == ["asset-1", "asset-1"]            # the same asset id each time; the index keys on the wire id


def test_the_library_index_of_a_job_takes_the_maker_from_the_facts_and_a_repeat_reuses_the_row(tmp_path, monkeypatch):
    from gateway import media_ingestion, media_jobs
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    facts = RequestFacts({"model": "m"}, prompt="p", maker_person_id="person-1")
    first = media_ingestion.index_video_delivery(video_id="video_mj_a", asset_id="vid_" + "a" * 64, filename="c.mp4",
                                                 media_type="video/mp4", record=facts)
    again = media_ingestion.index_video_delivery(video_id="video_mj_a", asset_id="vid_" + "a" * 64, filename="c.mp4",
                                                 media_type="video/mp4", record=facts)
    assert first is not None and again is not None and first.job_id == again.job_id
    assert first.maker_person_id == "person-1"


def test_nothing_is_told_to_the_library_when_the_response_cannot_be_built(shape, served, monkeypatch):
    from gateway.frontend_shapes import video_views
    core, jobs = served
    done = finished(core, jobs)
    told = []

    def refuse(*args, **kwargs):
        raise UnicodeEncodeError("latin-1", "x", 0, 1, "cannot encode")

    monkeypatch.setattr(video_views, "Response", refuse)
    with pytest.raises(UnicodeEncodeError):
        shape.render_bytes(done, jobs, None, caller_person_id="p1", on_delivered=lambda *a: told.append(a))
    assert told == []


def test_a_recorded_empty_set_of_facts_shows_nothing_from_the_result_metadata(shape):
    older = view(status="succeeded", result_metadata={"model": "m", "size": "640x480"})
    assert "model" not in body(shape.render_status(older, RequestFacts({}, recorded=True)))
    assert body(shape.render_status(older, RequestFacts({}, recorded=False)))["model"] == "m"


@pytest.mark.parametrize("name", [".\t.", ".\x7f.", "\t\x01", "..", "."])
def test_a_name_that_cleans_to_a_reserved_one_is_replaced_before_the_header_and_the_library_note(shape, served, name):
    core, jobs = served
    done = finished(core, jobs, metadata={"filename": name})
    told = []
    answer = shape.render_bytes(done, jobs, None, caller_person_id="p1", on_delivered=lambda *a: told.append(a))
    assert answer.headers["content-disposition"] == 'inline; filename="video.mp4"'
    assert told[0][2] == "video.mp4"

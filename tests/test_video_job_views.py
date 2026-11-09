"""The video wire's job views (S7 parity M7): the id a caller sees, and the status, list, delete and content answers.

The shape renders ``RunJobView`` values from the run core's job service; these tests use the in-memory fakes of the frozen
interfaces, so they pin the wire and nothing of the engine. The routes that serve it are tested through HTTP in the video
parity files."""
from __future__ import annotations

import asyncio
import json

import pytest

from gateway.frontend_shapes import bundled_rows, build_shape
from gateway.workflow_runtime_types import JobNotFound, ResultRef, RunFailure, RunJobView
from tests.workflow_runtime_fakes import FakeJobService, FakeRunCore


@pytest.fixture
def shape():
    row = next(r for r in bundled_rows() if any(route.canonical == "/v1/videos" and "POST" in route.methods for route in r.routes))
    return build_shape(row)


def view(job_id="mj_a", status="queued", **kw) -> RunJobView:
    base = dict(job_id=job_id, status=status, disposition="replay", workflow_id="w", record_revision=1, binding_key="b",
                created_at_s=100, updated_at_s=130)
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
    jobs.open_result = lambda job_id, ref, caller: type("C", (), {"body": b"mp4-bytes", "media_type": ref.media_type})()
    answer = shape.render_bytes(done, jobs, None)
    assert (answer.status_code, answer.body, answer.media_type) == (200, b"mp4-bytes", "video/mp4")


def test_the_fake_service_lists_what_the_shape_renders(shape):
    core = FakeRunCore()
    jobs = FakeJobService(core)
    for index, name in enumerate(("mj_a", "mj_b", "mj_c")):
        core.jobs[name] = view(name, created_at_s=100 + index)
    views, more = jobs.list(None, intents=("x",), limit=2)
    assert [v.job_id for v in views] == ["mj_c", "mj_b"] and more is True
    with pytest.raises(JobNotFound):
        jobs.list(None, intents=("x",), limit=2, after_id="nope")

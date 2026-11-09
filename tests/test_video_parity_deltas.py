"""The five video-move delta kinds (CODE_TO_DATA_S7_VIDEO_MOVE.md section 5.4) and the paths they may never name."""
from __future__ import annotations

import pytest

from tests.support.adapter_differential_deltas import Delta, DeltaError, KINDS, apply_deltas

VIDEO_KINDS = ("video_id_namespace", "record_store_moved", "orphan_marked_by_runner", "receipt_names_job",
               "status_follows_runner")
GOLDEN = {"scenario": "video_lifecycle",
          "caller": [{"response": {"status": 200, "headers": {"x-request-id": "r1"},
                                   "body": {"text": '{"id": "video_p1", "status": "queued", "error": null}'}}}],
          "engine_requests": [{"method": "POST", "path": "/prompt"}],
          "assets": [{"asset_id": "vid_1"}],
          "receipts": [{"job_id": None, "workflow": "video.generate.video-ltx-2.5-t2v"}]}


def delta(kind, path, old, new):
    return Delta(kind, path, old, new, "the video moved to the job store")


ALLOWED = {
    "video_id_namespace": ("$.caller[0].response.body.text#$.id", "video_p1", "video_mj_x"),
    "record_store_moved": ("$.records[0].owner", "a", "b"),
    "orphan_marked_by_runner": ("$.records[0].marked_by", "reconcile", "runner"),
    "receipt_names_job": ("$.receipts[0].job_id", None, "mj_x"),
}


@pytest.mark.parametrize("kind", sorted(ALLOWED))
def test_each_wire_kind_is_an_allowed_difference_on_its_own_leaf(kind):
    assert kind in KINDS
    assert delta(kind, *ALLOWED[kind]).problems() == []


@pytest.mark.parametrize("kind", sorted(ALLOWED))
@pytest.mark.parametrize("path", ["$.caller[0].response.body.text#$.status", "$.caller[0].response.body.text#$.url",
                                  "$.receipts[0].accepted_inputs_count", "$.caller[0].response.body.text#$.created_at"])
def test_a_kind_cannot_change_a_leaf_of_another_family(kind, path):
    if path == ALLOWED[kind][0]:
        return
    assert delta(kind, path, "x", "y").problems(), (kind, path)


@pytest.mark.parametrize("path", [ALLOWED[k][0] for k in ALLOWED] + ["$.caller[0].response.body.text#$.status"])
def test_status_follows_runner_is_test_mechanics_and_authorizes_no_trace_change(path):
    problems = delta("status_follows_runner", path, "queued", "completed").problems()
    assert problems and any("test mechanics" in p for p in problems)
    with pytest.raises(DeltaError):
        apply_deltas(GOLDEN, [delta("status_follows_runner", "$.caller[0].response.body.text#$.status", "queued", "completed")])


def test_a_video_id_delta_changes_the_id_in_the_body_text_and_nothing_else():
    changed = apply_deltas(GOLDEN, [delta("video_id_namespace", "$.caller[0].response.body.text#$.id", "video_p1", "video_mj_x")])
    assert changed["caller"][0]["response"]["body"]["text"] == '{"id": "video_mj_x", "status": "queued", "error": null}'
    assert changed["engine_requests"] == GOLDEN["engine_requests"] and changed["assets"] == GOLDEN["assets"]


def test_a_receipt_delta_names_the_job_and_the_workflow():
    changed = apply_deltas(GOLDEN, [
        delta("receipt_names_job", "$.receipts[0].job_id", None, "mj_x"),
        delta("receipt_names_job", "$.receipts[0].workflow", "video.generate.video-ltx-2.5-t2v", "video.generate.ltx-2.5-t2v")])
    assert changed["receipts"][0] == {"job_id": "mj_x", "workflow": "video.generate.ltx-2.5-t2v"}


@pytest.mark.parametrize("kind", VIDEO_KINDS)
@pytest.mark.parametrize("path,old,new", [
    ("$.caller[0].response.status", 200, 404),                              # an HTTP status
    ("$.caller[0].response.body.text#$.error", None, "oops"),               # an error body
    ("$.caller[0].response.headers.x-request-id", "r1", "r2"),              # a header
    ("$.engine_requests[0].path", "/prompt", "/other"),                     # an engine request
    ("$.assets[0].asset_id", "vid_1", "vid_2"),                             # an asset row
])
def test_no_video_kind_may_hide_a_status_an_error_a_header_an_engine_request_or_an_asset(kind, path, old, new):
    assert delta(kind, path, old, new).problems(), path
    with pytest.raises(DeltaError):
        apply_deltas(GOLDEN, [delta(kind, path, old, new)])

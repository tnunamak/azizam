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
    "record_store_moved": ("$.records[0].store", "video_job_records", "request_facts"),
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


# ---- the guard holds for every kind, not only the video kinds (V5d recheck items 1, 2, 8) ----

OTHER_KINDS = ("sync_workflow_lifecycle", "envelope_v2", "workflow_run_metadata", "graph_template_meta",
               "mesh_route_replacement", "idempotency_acceptance", "terminal_cancel_noop")
ERRORED = {"scenario": "x", "caller": [{"response": {"status": 400, "headers": {"x-request-id": "r1"},
           "body": {"text": '{"error": {"message": "prompt is required", "type": "invalid_request_error"}}'}}}],
           "engine_requests": [], "assets": []}


@pytest.mark.parametrize("kind", OTHER_KINDS)
def test_no_kind_may_reword_an_error_body(kind):
    reword = Delta(kind, "$.caller[0].response.body.text#$.error.message", "prompt is required", "ok", "reworded")
    assert reword.problems()
    with pytest.raises(DeltaError):
        apply_deltas(ERRORED, [reword])


@pytest.mark.parametrize("kind", OTHER_KINDS + ("bounded_wait",))
@pytest.mark.parametrize("old,new", [(400, 500), (400, 200), (200, 500), (503, 200)])
def test_no_kind_may_move_a_refusal_status(kind, old, new):
    assert Delta(kind, "$.caller[0].response.status", old, new, "moved").problems()


def test_a_wait_may_still_answer_202_for_200():
    assert Delta("bounded_wait", "$.caller[0].response.status", 200, 202, "a bounded wait").problems() == []


@pytest.mark.parametrize("path,old", [
    ("$.caller[0]", {"response": {"status": 200}}),                                  # a whole caller observation
    ("$.caller[0].response", {"status": 200, "headers": {"x-request-id": "r1"}}),    # a response with a header inside
    ("$.engine_requests[0]", {"method": "POST"}),
    ("$.assets[0]", {"asset_id": "vid_1"}),
])
def test_a_remove_cannot_drop_an_observation_or_a_subtree(path, old):
    problems = Delta("remove", path, old, None, "dropped").problems()
    assert problems, path
    with pytest.raises(DeltaError):
        apply_deltas(GOLDEN, [Delta("remove", path, old, None, "dropped")])


def test_a_removed_list_item_may_not_hold_an_error_a_header_or_an_asset():
    for held in ({"id": "a", "error": {"code": "x"}}, {"id": "a", "headers": {"x": "y"}}, {"id": "a", "assets": []}):
        assert Delta("remove", "$.caller[0].response.body.text#$.items[1]", held, None, "dropped").problems(), held
    assert Delta("remove", "$.caller[0].response.body.text#$.items[1]", {"id": "a"}, None, "dropped").problems() == []


@pytest.mark.parametrize("kind", OTHER_KINDS + ("bounded_wait",) + tuple(VIDEO_KINDS))
def test_an_ancestor_subtree_cannot_carry_a_protected_header_or_status(kind):
    """The whole response replaced at once changes a header together with the status; no kind may name a subtree."""
    old = GOLDEN["caller"][0]["response"]
    new = {**old, "status": 202, "headers": {"x-request-id": "r2"}}
    delta_ = Delta(kind, "$.caller[0].response", old, new, "one change, many leaves")
    assert delta_.problems()
    with pytest.raises(DeltaError):
        apply_deltas(GOLDEN, [delta_])


def test_a_record_store_move_cannot_change_who_owns_the_record():
    for leaf, old, new in (("owner", "a", "b"), ("owner_key_hash", "h1", "h2"), ("person_id", "p1", "p2"),
                           ("model", "m1", "m2"), ("size", "1", "2")):
        assert delta("record_store_moved", f"$.records[0].{leaf}", old, new).problems(), leaf
        assert delta("record_store_moved", f"$.request_facts.{leaf}", old, new).problems(), leaf

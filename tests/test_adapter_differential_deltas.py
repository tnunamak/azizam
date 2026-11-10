"""The delta manifest: an unknown path, an unmatched delta or an unlisted difference fails (golden design section 4)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tests.support import adapter_differential as ad
from tests.support import adapter_differential_deltas as listed
from tests.support.adapter_differential_deltas import Delta, DeltaError

GOLDEN = {"scenario": "x", "caller": [{"response": {"status": 200, "body": {"text": '{"status": "queued", "n": 1}'}}}],
          "engine_requests": [{"method": "POST", "path": "/prompt"}]}


def delta(path, old, new, kind="envelope_v2", reason="the envelope is version 2"):
    return Delta(kind, path, old, new, reason)


def trace_with(**change):
    out = json.loads(json.dumps(GOLDEN))
    for path, value in change.items():
        out["caller"][0]["response"][path] = value
    return out



def _compare(actual, deltas):

    with tempfile.TemporaryDirectory() as directory:
        (Path(directory) / "x.json").write_text(json.dumps(GOLDEN), encoding="utf-8")
        return ad.compare_golden("x", actual, environ={}, fixture_dir=Path(directory), deltas=deltas)


def test_no_delta_and_no_difference_is_equal():
    assert _compare(GOLDEN, []) == ""


def test_a_listed_delta_makes_the_new_trace_equal():
    actual = trace_with(status=202)
    assert _compare(actual, []) != ""
    assert _compare(actual, [delta("$.caller[0].response.status", 200, 202, "bounded_wait", "a wait that expired")]) == ""


def test_a_delta_reaches_into_body_json_text():
    actual = trace_with(body={"text": '{"status": "running", "n": 1}'})
    path = "$.caller[0].response.body.text#$.status"
    assert _compare(actual, [delta(path, "queued", "running", "sync_workflow_lifecycle")]) == ""


def test_an_unlisted_difference_beside_a_listed_one_fails():
    actual = trace_with(status=202)
    actual["engine_requests"][0]["path"] = "/other"
    problem = _compare(actual, [delta("$.caller[0].response.status", 200, 202, "bounded_wait", "a wait that expired")])
    assert "engine_requests" in problem


def test_an_unknown_path_fails():
    assert "unknown path" in _compare(GOLDEN, [delta("$.caller[3].response.status", 200, 202)])
    assert "unknown path" in _compare(GOLDEN, [delta("$.caller[0].response.body.text#$.absent", 1, 2)])


def test_an_unmatched_old_value_fails():
    assert "unmatched" in _compare(trace_with(status=202), [delta("$.caller[0].response.status", 201, 202)])


def test_a_delta_that_the_new_trace_does_not_show_fails():
    assert _compare(GOLDEN, [delta("$.caller[0].response.status", 200, 202)]) != ""


@pytest.mark.parametrize("bad", [
    Delta("a_mask", "$.caller[0].response.status", 200, 202, "r"),  # kind outside the design's list
    Delta("envelope_v2", "$.caller[0].response.status", 200, 202, " "),  # no reason
    Delta("envelope_v2", "$.caller[0].response.status", 200, 200, "r"),  # does nothing
    Delta("envelope_v2", "caller", 1, 2, "r"),  # not a path
])
def test_a_malformed_delta_fails(bad):
    with pytest.raises(DeltaError):
        listed.apply_deltas(GOLDEN, [bad])


def test_a_remove_deletes_a_list_item_the_golden_holds():
    golden = {"caller": [{"response": {"body": {"text": '{"items": [{"id": "a"}, {"id": "b"}]}'}}}]}
    out = listed.apply_deltas(golden, [Delta("remove", "$.caller[0].response.body.text#$.items[1]",
                                             {"id": "b"}, None, "the second call is a replay with no engine call")])
    assert json.loads(out["caller"][0]["response"]["body"]["text"]) == {"items": [{"id": "a"}]}


def test_a_remove_deletes_a_key_and_checks_its_old_value():
    golden = {"caller": [{"response": {"status": 200, "body": {"extra": "x"}}}]}
    out = listed.apply_deltas(golden, [Delta("remove", "$.caller[0].response.body.extra", "x", None, "not on the new wire")])
    assert out["caller"][0]["response"] == {"status": 200, "body": {}}
    with pytest.raises(DeltaError, match="unmatched"):
        listed.apply_deltas(golden, [Delta("remove", "$.caller[0].response.body.extra", "y", None, "not on the new wire")])


def test_a_remove_with_a_new_value_is_malformed():
    problems = Delta("remove", "$.caller[0].response.status", 200, 202, "r").problems()
    assert any("no new value" in problem for problem in problems)


def test_a_path_cannot_name_the_whole_trace_or_a_free_form():
    for path in ("$", "$.caller[x]", "$..caller"):
        with pytest.raises(DeltaError):
            listed.apply_deltas(GOLDEN, [delta(path, 1, 2)])


def test_the_committed_manifest_is_well_formed_and_holds_against_its_goldens():
    for name, deltas in listed.MANIFEST.items():
        if name.startswith("s7_parity_images/"):       # applied by that file's cases to the trace they observe, not to a golden
            assert deltas and not [problem for delta in deltas for problem in delta.problems()], name
            continue
        golden = json.loads((ad.FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert deltas and listed.apply_deltas(golden, deltas)


def test_a_delta_with_a_missing_old_value_adds_a_key_and_only_where_the_golden_has_none():
    added = listed.apply_deltas(GOLDEN, [delta("$.caller[0].response.body.text#$.client_id", listed.MISSING, "c1")])
    assert json.loads(added["caller"][0]["response"]["body"]["text"])["client_id"] == "c1"
    with pytest.raises(DeltaError, match="the delta says it is missing"):
        listed.apply_deltas(GOLDEN, [delta("$.caller[0].response.body.text#$.n", listed.MISSING, 2)])
    with pytest.raises(DeltaError, match="unknown path"):
        listed.apply_deltas(GOLDEN, [delta("$.caller[0].response.nothing.key", listed.MISSING, 2)])


def test_an_object_value_needs_the_subtree_mark_and_the_old_value_must_still_match():
    plain = delta("$.scenario", "x", {"a": 1})
    assert any("not a subtree" in problem for problem in plain.problems())
    marked = Delta("envelope_v2", "$.scenario", "x", {"a": 1}, "r", subtree=True)
    assert listed.apply_deltas(GOLDEN, [marked])["scenario"] == {"a": 1}
    with pytest.raises(DeltaError, match="unmatched"):
        listed.apply_deltas(GOLDEN, [Delta("envelope_v2", "$.scenario", "y", {"a": 1}, "r", subtree=True)])


def test_an_engine_request_delta_names_the_prompt_session_id_and_nothing_else():
    ok = Delta("engine_session_id", "$.engine_requests[0].body.text#$.client_id", listed.MISSING, "c", "r")
    assert ok.problems() == []
    for path in ("$.engine_requests[0].body.text#$.prompt", "$.engine_requests[0].path", "$.engine_requests[0].body.text#$.client_id.x"):
        assert Delta("engine_session_id", path, listed.MISSING, "c", "r").problems(), path
    assert Delta("engine_session_id", ok.path, "a", "c", "r").problems()          # it adds a key; it never changes one
    other = Delta("envelope_v2", "$.engine_requests[0].body.text#$.client_id", listed.MISSING, "c", "r")
    assert any("kept identical" in problem for problem in other.problems())       # no other kind may reach an engine request


def test_the_image_case_entries_name_only_the_approved_kinds():
    approved = {"engine_session_id", "job_row_metadata", "idempotency_key_reuse_conflict", "job_create_failure_text", "executor_crash_text"}
    seen = {delta.kind for name, entries in listed.MANIFEST.items() if name.startswith("s7_parity_images/") for delta in entries}
    assert seen == approved
    assert not {"job_submit_refuses_unknown_field", "receipt_closes_with_job_outcome", "job_one_receipt", "masked_edit_source"} & listed.KINDS


def test_every_rerecorded_golden_exists_and_is_listed_with_a_reason():
    for name, reason in listed.RERECORDED.items():
        assert (ad.FIXTURE_DIR / f"{name}.json").is_file(), name
        assert len(reason) > 20, name
    assert listed.ENGINE_FACING_CHANGES <= set(listed.RERECORDED)
    assert not set(listed.RERECORDED) & set(listed.MANIFEST)


def test_no_golden_origin_block_holds_prompt_text():
    for path in sorted(ad.FIXTURE_DIR.glob("*.json")):
        for asset in json.loads(path.read_text(encoding="utf-8")).get("assets") or []:
            origin = (asset.get("json") or {}).get("origin")
            if origin is not None:
                assert set(origin) <= listed.ORIGIN_KEYS, (path.name, sorted(origin))


def test_the_named_exception_allows_exactly_the_key_reuse_conflict_and_the_two_failure_texts():
    ok = [
        Delta("idempotency_key_reuse_conflict", "$.caller[1].response.status", 202, 409, "a used key with another body answers 409"),
        Delta("idempotency_key_reuse_conflict", "$.caller[1].response.body.text#$.error", None, {"code": "idempotency_conflict"},
              "the conflict error", subtree=True),
        Delta("job_create_failure_text", "$.caller[0].response.body.text#$.error.message", "a: RuntimeError", "the job could not be created",
              "no exception class in client text"),
        Delta("executor_crash_text", "$.media_jobs.media_jobs[0].error", "a: RuntimeError", "The job failed.", "fixed sentence"),
    ]
    assert [problem for entry in ok for problem in entry.problems()] == []
    for name in ("J10", "J11", "R06"):
        assert listed.MANIFEST[f"s7_parity_images/{name}"], name


def test_the_guard_still_refuses_every_other_status_or_error_delta():
    refused = [
        Delta("idempotency_key_reuse_conflict", "$.caller[1].response.status", 202, 500, "a different status"),
        Delta("idempotency_key_reuse_conflict", "$.caller[1].response.status", 200, 409, "a different old status"),
        Delta("idempotency_key_reuse_conflict", "$.caller[1].response.body.text#$.error.message", "a", "b", "a path it may not touch"),
        Delta("job_create_failure_text", "$.caller[0].response.status", 500, 200, "a status change under a text kind"),
        Delta("envelope_v2", "$.caller[0].response.status", 202, 409, "another kind with the same status change"),
        Delta("envelope_v2", "$.caller[0].response.body.text#$.error", None, {"code": "x"}, "another kind, an error body", subtree=True),
        Delta("executor_crash_text", "$.caller[0].response.status", 200, 500, "a refusal status"),
    ]
    for entry in refused:
        assert entry.problems(), entry


def test_a_listed_exception_kind_exists():
    assert set(listed.ERROR_ALLOWED) <= listed.KINDS and "idempotency_conflict" not in listed.KINDS


def test_the_engine_facing_reruns_state_why_the_order_changed():
    for name in ("video_lifecycle", "video_extend"):
        assert name in listed.ENGINE_FACING_CHANGES
        assert "the prompt names the uploaded file, so upload-first is the causal order" in listed.RERECORDED[name]
    assert "create_answers_before_the_engine" in listed.RERECORDED["video_create_unreachable"]

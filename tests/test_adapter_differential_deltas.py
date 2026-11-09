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
    golden = {"caller": [{"response": {"status": 200, "extra": "x"}}]}
    out = listed.apply_deltas(golden, [Delta("remove", "$.caller[0].response.extra", "x", None, "not on the new wire")])
    assert out["caller"][0]["response"] == {"status": 200}
    with pytest.raises(DeltaError, match="unmatched"):
        listed.apply_deltas(golden, [Delta("remove", "$.caller[0].response.extra", "y", None, "not on the new wire")])


def test_a_remove_with_a_new_value_is_malformed():
    problems = Delta("remove", "$.caller[0].response.status", 200, 202, "r").problems()
    assert any("no new value" in problem for problem in problems)


def test_a_path_cannot_name_the_whole_trace_or_a_free_form():
    for path in ("$", "$.caller[x]", "$..caller"):
        with pytest.raises(DeltaError):
            listed.apply_deltas(GOLDEN, [delta(path, 1, 2)])


def test_the_committed_manifest_is_well_formed_and_holds_against_its_goldens():
    for name, deltas in listed.MANIFEST.items():
        golden = json.loads((ad.FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert deltas and listed.apply_deltas(golden, deltas)

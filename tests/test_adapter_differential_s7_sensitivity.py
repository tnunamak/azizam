"""Sensitivity controls for the S7 goldens (builder D; see ``tests/test_adapter_differential_s7.py``).

A golden that no change can fail proves nothing about a route move. Two controls:

* Every recorded cell: the committed golden is changed in three ways (a caller status, a caller body, an engine
  request) and ``compare_golden`` must name the change. The unchanged golden is the control and must compare equal.
* Live mutants: one small change to the running gateway (a policy check skipped, an admission check skipped, the
  idempotency lookup forced empty) is replayed against a cell's scenario, and the golden compare must fail.
"""
from __future__ import annotations

import copy
import json

import pytest

import proxy
from tests.support import adapter_differential as ad
from tests.support.adapter_differential_rig import play
from tests.test_adapter_differential_s7 import ROUTES, build, fixture_name, recorded_cells


def _golden(route: str, outcome: str) -> dict:
    return json.loads((ad.FIXTURE_DIR / f"{fixture_name(route, outcome)}.json").read_text(encoding="utf-8"))


def _compare(route: str, outcome: str, trace: dict) -> str:
    return ad.compare_golden(fixture_name(route, outcome), trace, environ={})


@pytest.mark.parametrize("route,outcome", recorded_cells(), ids=lambda v: v)
def test_a_changed_trace_fails_its_golden(route, outcome):
    golden = _golden(route, outcome)
    assert _compare(route, outcome, copy.deepcopy(golden)) == "", "control: the unchanged golden must match"

    status = copy.deepcopy(golden)
    caller = next((c for c in status["caller"] if c.get("response", {}).get("status") is not None), None)
    if caller is not None:
        caller["response"]["status"] += 1
    else:  # the caller walked away: the answer that "was not sent" must show when one appears
        status["caller"][0]["response"] = {"status": 200}
    assert _compare(route, outcome, status) != ""

    body = copy.deepcopy(golden)
    target = next((c for c in body["caller"] if (c.get("response", {}).get("body") or {}).get("text")), None)
    if target is not None:
        target["response"]["body"]["text"] += " "
        assert _compare(route, outcome, body) != ""

    engine = copy.deepcopy(golden)
    engine["engine_requests"] = [*engine["engine_requests"], {"method": "GET", "path": "/never-recorded"}]
    assert _compare(route, outcome, engine) != ""


def _replay_with(tmp_path, route: str, outcome: str, change) -> str:
    setup, drive, description = build(ROUTES[route], outcome)

    def mutated_setup(rig):
        setup(rig)
        change(rig)

    with pytest.raises(pytest.fail.Exception) as failure:
        play(fixture_name(route, outcome), tmp_path, lambda *_: None, mutated_setup, drive, description=description)
    return str(failure.value)


def test_the_unmutated_replay_matches(tmp_path):
    setup, drive, description = build(ROUTES["ig"], "policy_refusal")
    play(fixture_name("ig", "policy_refusal"), tmp_path, lambda *_: None, setup, drive, description=description)


def test_a_policy_denial_that_answers_nothing_is_caught(tmp_path):
    def silent(rig):
        rig.set(proxy, "_policy_denied_response", lambda *_args, **_kwargs: None)

    assert _replay_with(tmp_path, "ig", "policy_refusal", silent)


def test_skipping_the_admission_check_is_caught(tmp_path):
    def lenient(rig):
        rig.set(proxy, "_media_job_admission_response", lambda *_args, **_kwargs: None)

    assert _replay_with(tmp_path, "ij", "admission_refusal", lenient)


def test_forgetting_the_idempotency_lookup_is_caught(tmp_path):
    def forgetful(rig):
        rig.set(proxy, "_image_media_job_request_id", lambda *_args, **_kwargs: None)

    assert _replay_with(tmp_path, "ij", "replay_same", forgetful)

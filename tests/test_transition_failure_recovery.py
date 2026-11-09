"""Restart recovery keeps each effect's identity (S8b ship check): a reset is not a restore, and a dirty engine never
reopens. Crash states are built on the real operation store; a restart is the real startup reconcile."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from gateway.transition_ops import StepIntent
from tests.test_transition_declared_snapshot_ops import FakeInvoke, adapter_for, intent, run, snapshot_profile
from tests.test_transition_admin import Reply
from tests.test_transition_failure_semantics import _restart, attesting, wire


def _crashed_wake_at(runtime: Any, engine: Any, *, effect: str, recorded: Any = None) -> str:
    """A wake that stopped at its restore step, the target loaded. ``effect`` is ``restore`` or ``reset``: the effect whose
    outcome was not recorded (the reset's durable intent carries the step name ``restore``)."""
    executor = runtime.executor()
    begun = executor.store.begin(domain="dom", kind="wake", idempotency_key="wake-crash", target="alpha",
                                 incarnation="inc-1", config_revision=1)
    op_id = begun.operation.op_id
    executor.store.write_intent(op_id, StepIntent(step="restore", effect_key=f"{op_id}:{effect}",
                                                  expected_incarnation="inc-1"))
    engine.resident = "alpha"
    return op_id


# -- the adapter keeps the reset's identity ---------------------------------------------------------------------------


def reset_intent() -> StepIntent:
    return StepIntent(step="restore", effect_key="op-1:reset", expected_incarnation="inc-1")


@pytest.mark.parametrize(("restore", "reset", "reported"), [("unknown", "no_change", "no_change"),
                                                            ("no_change", "unknown", "partial")])
def test_a_query_of_a_reset_uses_the_resets_semantics_not_the_restores(tmp_path: Path, restore: str, reset: str,
                                                                        reported: str) -> None:
    invoke = FakeInvoke({"transition.query": Reply(200, {"outcome": "failed"})})
    adapter = adapter_for(attesting(restore=restore, reset=reset), invoke, tmp_path)
    assert run(adapter.query("dom", reset_intent())).outcome == reported
    assert run(adapter.query("dom", intent("restore"))).outcome == ("no_change" if restore == "no_change" else "partial")


# -- a crash at the reset: both attestation directions, and a dirty engine never reopens ----------------------------------


def test_a_reset_that_failed_unchanged_before_a_crash_verifies_clean_and_serves_cold(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True, restore="unknown", reset="no_change")
    engine.query_outcome, engine.clean = "failed", True
    op_id = _crashed_wake_at(runtime, engine, effect="reset")
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("succeeded", "open"), (done.state, done.step, done.step_outcome)


def test_a_reset_that_failed_unchanged_but_left_a_dirty_target_never_reopens(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True, restore="unknown", reset="no_change")
    engine.query_outcome, engine.clean = "failed", False
    op_id = _crashed_wake_at(runtime, engine, effect="reset")
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("failed_closed", "failed_closed")


def test_a_failed_reset_is_judged_by_the_reset_attestation_even_when_the_restore_attests_no_change(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True, restore="no_change", reset="unknown")
    engine.query_outcome, engine.clean, engine.servable = "failed", True, True
    op_id = _crashed_wake_at(runtime, engine, effect="reset")
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("failed_closed", "failed_closed"), "resident is not clean"


@pytest.mark.parametrize(("clean", "state"), [(True, "succeeded"), (False, "failed_closed"), (None, "reconciling")])
def test_a_reset_that_succeeded_before_a_crash_reopens_only_on_the_clean_verification(tmp_path, monkeypatch, clean, state) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    engine.query_outcome, engine.clean = "success", clean
    op_id = _crashed_wake_at(runtime, engine, effect="reset")
    done = _restart(runtime, op_id)
    assert done.state == state and gates.state("dom") == {"succeeded": "open"}.get(state, state)


def test_a_reset_that_failed_with_unknown_semantics_stays_held_after_the_restart(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    engine.query_outcome, engine.clean = "failed", True
    op_id = _crashed_wake_at(runtime, engine, effect="reset")
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("failed_closed", "failed_closed")


# -- a recorded no_change whose check is unreadable reconciles; it never becomes a terminal failure ---------------------

from gateway.transition_ops import EffectResult  # noqa: E402


def _crashed_recorded(runtime: Any, engine: Any, *, kind: str, step: str, effect: str, outcome: str,
                      evidence: str, target: Any = None) -> str:
    """An operation that stopped after ``step`` recorded ``outcome`` and before its gate verdict was written."""
    executor = runtime.executor()
    begun = executor.store.begin(domain="dom", kind=kind, idempotency_key="crash-recorded", target=target,
                                 incarnation="inc-1", config_revision=1)
    op_id = begun.operation.op_id
    executor.store.write_intent(op_id, StepIntent(step=step, effect_key=f"{op_id}:{effect}",
                                                  expected_incarnation="inc-1"))
    executor.store.write_outcome(op_id, step, EffectResult(outcome=outcome, evidence_ref=evidence))
    return op_id


def _unreadable(engine: Any, how: str) -> dict[str, Any]:
    """Make the servable observation absent, foreign, stale (by the profile's bound) or too slow."""
    if how == "absent":
        engine.servable = None
    elif how == "foreign":
        engine.servable_incarnation = "inc-other"
    elif how == "slow":
        engine.servable_delay = 5.0
    return {"fresh_within_s": 1e-9} if how == "stale" else {}


UNREADABLE = ["absent", "foreign", "stale", "slow"]


@pytest.mark.parametrize("how", UNREADABLE)
@pytest.mark.parametrize("recorded", [False, True])
def test_a_required_checkpoint_refusal_with_an_unreadable_servable_check_reconciles_after_the_restart(
        tmp_path, monkeypatch, recorded, how) -> None:
    from tests.test_transition_failure_semantics import _crashed_checkpoint
    holder: dict[str, Any] = {}
    wired = wire(tmp_path, monkeypatch, policy="required", checkpoint="no_change", **_unreadable_args(holder, how))
    gates, engine, runtime, store = wired
    _unreadable(engine, how)
    engine.query_outcome = "failed"
    op_id = _crashed_checkpoint(runtime, recorded=recorded)
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("reconciling", "reconciling"), (done.state, done.step_outcome)


def _unreadable_args(holder: dict[str, Any], how: str) -> dict[str, Any]:
    return {"fresh_within_s": 1e-9} if how == "stale" else {}


@pytest.mark.parametrize("how", UNREADABLE)
def test_a_recorded_unchanged_load_with_an_unreadable_servable_check_reconciles_after_the_restart(
        tmp_path, monkeypatch, how) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, load="no_change", **_unreadable_args({}, how))
    _unreadable(engine, how)
    op_id = _crashed_recorded(runtime, engine, kind="select", step="load", effect="load", outcome="no_change",
                              evidence="load_refused", target="beta")
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("reconciling", "reconciling")


def test_a_revalidation_hold_settles_when_the_evidence_returns(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, policy="required", checkpoint="no_change")
    engine.servable = None
    from tests.test_transition_failure_semantics import _crashed_checkpoint
    op_id = _crashed_checkpoint(runtime, recorded=True)
    assert _restart(runtime, op_id).state == "reconciling"
    engine.servable = True
    again = _restart(runtime, op_id)
    assert (again.state, gates.state("dom")) == ("aborted", "open")


# -- a verified cold restore or reset survives a crash --------------------------------------------------------------------

COLD = ["restore:reset:clean:serving_cold", "restore:declined:serving_cold"]


@pytest.mark.parametrize("evidence", COLD)
def test_a_verified_cold_start_recorded_before_a_crash_goes_on_to_open_with_the_shipped_policy(tmp_path, monkeypatch, evidence) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch)
    op_id = _crashed_recorded(runtime, engine, kind="wake", step="restore", effect="reset", outcome="no_change",
                              evidence=evidence, target="alpha")
    engine.resident = "alpha"
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("succeeded", "open"), (done.state, done.step, done.evidence_ref)


@pytest.mark.parametrize(("verify", "resident", "clean", "state"), [
    (True, "alpha", False, "failed_closed"), (True, "alpha", None, "reconciling"),
    (True, "beta", True, "reconciling"),   # clean, but the load is no longer the target: the open check holds it
    (False, "beta", True, "failed_closed"),  # the shipped policy checks the target by name
])
def test_a_recorded_cold_start_refreshes_the_clean_evidence_before_it_continues(
        tmp_path, monkeypatch, verify, resident, clean, state) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=verify)
    op_id = _crashed_recorded(runtime, engine, kind="wake", step="restore", effect="reset", outcome="no_change",
                              evidence="restore:reset:clean:serving_cold", target="alpha")
    engine.resident, engine.clean = resident, clean
    done = _restart(runtime, op_id)
    assert done.state == state and gates.state("dom") == state


def test_a_restore_recorded_partial_before_a_crash_still_runs_its_declared_reset(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True, reset="no_change")
    engine.reset_outcome, engine.clean = "failed", True
    op_id = _crashed_recorded(runtime, engine, kind="wake", step="restore", effect="restore", outcome="partial",
                              evidence="restore:failed:state_not_known_clean", target="alpha")
    engine.resident = "alpha"
    done = _restart(runtime, op_id)
    assert engine.resets == 1, "the declared reset runs after the restart"
    assert (done.state, gates.state("dom")) == ("succeeded", "open")


# -- the matrix: every effect, every declared word, every attestation, direct and by query ----------------------------------

import itertools  # noqa: E402

from tests.test_transition_failure_semantics import EFFECTS, _invoke_op  # noqa: E402

WORDS = {"failed": None, "partial": "partial", "no_change": "no_change", "success": "success",
         "succeeded": "unknown", "missing": "unknown"}  # an unlisted word and a missing one read as unknown
ATTEST = ["absent", "unknown", "no_change"]
CELLS = list(itertools.product(EFFECTS, WORDS, ATTEST, ["direct", "query"]))


def _expected(word: str, attest: str) -> str:
    if word == "failed":
        return "no_change" if attest == "no_change" else "partial"
    return WORDS[word]  # type: ignore[return-value]


@pytest.mark.parametrize(("op", "word", "attest", "via"), CELLS,
                         ids=[f"{op}-{word}-{attest}-{via}" for op, word, attest, via in CELLS])
def test_the_adapter_maps_every_declared_word_by_the_profile_for_every_effect(tmp_path, op, word, attest, via) -> None:
    body: dict[str, Any] = {"bytes": 1, "evidence_ref": "e"}
    if word != "missing":
        body["outcome"] = word
    key = "transition.query" if via == "query" else f"transition.{op}"
    profile = snapshot_profile() if attest == "absent" else attesting(**{op: attest})
    adapter = adapter_for(profile, FakeInvoke({key: Reply(200, body)}), tmp_path)
    if via == "direct":
        got = run(_invoke_op(adapter, op))
    else:
        got = run(adapter.query("dom", reset_intent() if op == "reset" else intent(op)))
    assert got.outcome == _expected(word, attest)

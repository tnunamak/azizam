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

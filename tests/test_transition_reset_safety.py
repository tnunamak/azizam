"""Regression coverage for owner-settled resets and the reset-to-open boundary."""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_transition_failure_semantics import Engine, Reply, run, wire


async def _held(runtime: Any, engine: Engine, *, effect: str = "reset") -> Any:
    executor = runtime.executor()
    park = await executor.submit(domain="dom", kind="park", idempotency_key="park-1")
    assert (await executor.wait(park.operation.op_id)).state == "succeeded"
    engine.restore_outcome = "partial" if effect == "reset" else "unknown"
    engine.reset_outcome = engine.query_outcome = "unknown"
    wake = await executor.submit(domain="dom", kind="wake", idempotency_key="wake-1")
    held = await executor.wait(wake.operation.op_id)
    assert (held.state, held.step, held.step_outcome) == ("reconciling", "restore", "unknown")
    assert held.step_intent.effect_key == f"{held.op_id}:{effect}"
    return held


@pytest.mark.parametrize(
    "restore,reset,clean,ready,state,evidence",
    [
        ("no_change", "unknown", False, False, "failed_closed", "reset:failed:state_unknown"),
        ("unknown", "no_change", False, False, "failed_closed", "reset:target_not_verified_clean"),
        ("unknown", "no_change", True, True, "succeeded", None),
        ("unknown", "no_change", True, False, "reconciling", "load_unverified_at_resume"),
        ("unknown", "no_change", None, False, "reconciling", "reset:target_not_verified_clean"),
    ],
    ids=["restore-attests-only", "reset-attests-dirty", "reset-attests-clean",
         "clean-but-not-ready", "clean-unreadable"],
)
def test_owner_reset_no_change_uses_reset_attestation_and_clean_check(
        tmp_path, monkeypatch, restore, reset, clean, ready, state, evidence) -> None:
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch, verify=True, restore=restore, reset=reset)

    async def scenario() -> None:
        held = await _held(runtime, engine)
        engine.clean, engine.ready, engine.servable = clean, ready, True
        calls_before = len(engine.calls)
        executor = runtime.executor()
        await executor.resolve(held.op_id, step="restore", outcome="no_change", evidence_ref="owner")
        done = await executor.wait(held.op_id)
        assert done.state == state
        assert gates.state("dom") == ("open" if state == "succeeded" else state)
        assert engine.resets == 1, "settling an existing reset must not dispatch another reset"
        assert any(op == "transition.clean" for op, _ in engine.calls[calls_before:]) == (reset == "no_change")
        if evidence is not None:
            assert done.evidence_ref == evidence
        if state != "succeeded":
            assert done.step != "open" or state == "reconciling", "OpenStep must not finish on an unsafe target"

    run(scenario())


def test_owner_reset_no_change_without_declared_clean_verification_stays_closed(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch, restore="no_change", reset="no_change")

    async def scenario() -> None:
        held = await _held(runtime, engine)
        engine.ready, engine.servable = False, True
        executor = runtime.executor()
        done = await executor.resolve(held.op_id, step="restore", outcome="no_change", evidence_ref="owner")
        assert (done.state, gates.state("dom")) == ("failed_closed", "failed_closed")
        assert done.evidence_ref == "reset:failed:no_verify_declared"
        assert engine.resets == 1

    run(scenario())


@pytest.mark.parametrize("ready", [True, False], ids=["ready", "not-ready"])
def test_owner_restore_no_change_serves_cold_only_through_the_open_readiness_check(
        tmp_path, monkeypatch, ready) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, restore="no_change", reset="unknown")

    async def scenario() -> None:
        held = await _held(runtime, engine, effect="restore")
        engine.ready, engine.servable = ready, True
        executor = runtime.executor()
        await executor.resolve(held.op_id, step="restore", outcome="no_change", evidence_ref="owner")
        done = await executor.wait(held.op_id)
        assert done.state == ("succeeded" if ready else "reconciling")
        assert gates.state("dom") == ("open" if ready else "reconciling")
        assert engine.resets == 0
        assert store.pointer("dom") is None, "a declined restore must retire its published snapshot"
        if not ready:
            assert done.evidence_ref == "load_unverified_at_resume"

    run(scenario())


def test_owner_partial_restore_runs_its_reset_before_continuing(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch, verify=True)

    async def scenario() -> None:
        held = await _held(runtime, engine, effect="restore")
        engine.reset_outcome, engine.clean = "success", True
        executor = runtime.executor()
        await executor.resolve(held.op_id, step="restore", outcome="partial", evidence_ref="owner")
        done = await executor.wait(held.op_id)
        assert (done.state, gates.state("dom")) == ("succeeded", "open")
        assert engine.resets == 1

    run(scenario())


def test_unknown_owner_restore_outcome_does_not_dispatch_a_reset(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch, verify=True)

    async def scenario() -> None:
        held = await _held(runtime, engine, effect="restore")
        executor = runtime.executor()
        done = await executor.resolve(held.op_id, step="restore", outcome="unknown", evidence_ref="owner")
        assert (done.state, done.step, done.step_outcome) == ("reconciling", "restore", "unknown")
        assert gates.state("dom") == "reconciling"
        assert engine.resets == 0
        assert done.step_intent == held.step_intent

    run(scenario())


@pytest.mark.parametrize(
    "detail,safe",
    [
        ({"offering": "alpha", "ready": False}, False),
        ({"offering": "alpha"}, False),
        ({"ready": True}, False),
        ({"offering": "beta", "ready": True}, False),
        ({"offering": "alpha", "ready": True}, True),
    ],
    ids=["not-ready", "missing-ready", "missing-offering", "wrong-offering", "exact-ready-target"],
)
def test_successful_reset_requires_the_exact_resident_target_and_ready(
        tmp_path, monkeypatch, detail, safe) -> None:
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch)
    original_call = Engine.__call__
    after_reset = False
    probes: list[dict[str, Any]] = []

    async def invoke(self, service_id, op, body):
        nonlocal after_reset
        if op == "transition.resident" and after_reset:
            self.calls.append((op, dict(body)))
            observed = {"resident": True, "incarnation": "inc-1", **detail}
            probes.append(observed)
            return Reply(200, observed)
        reply = await original_call(self, service_id, op, body)
        if op == "transition.reset":
            after_reset = True
        return reply

    monkeypatch.setattr(Engine, "__call__", invoke)

    async def scenario() -> None:
        executor = runtime.executor()
        park = await executor.submit(domain="dom", kind="park", idempotency_key="park-1")
        assert (await executor.wait(park.operation.op_id)).state == "succeeded"
        engine.restore_outcome, engine.reset_outcome = "partial", "success"
        wake = await executor.submit(domain="dom", kind="wake", idempotency_key="wake-1")
        done = await executor.wait(wake.operation.op_id)
        assert engine.resets == 1 and probes, "the malformed resident evidence is observed after reset, not during load"
        assert done.state == ("succeeded" if safe else "failed_closed")
        assert gates.state("dom") == ("open" if safe else "failed_closed")
        if not safe:
            assert (done.step, done.step_outcome) == ("restore", "partial")
            assert done.evidence_ref == "reset:target_not_verified_clean"

    run(scenario())

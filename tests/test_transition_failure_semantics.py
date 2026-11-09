"""Failure semantics are reviewed profile data (code to data, S8 part B).

A declared ``failed`` outcome means "no change" only when the reviewed profile attests it for that operation.
The adapter holds no per-operation constant; the executor then judges an attested no-change by the servable
re-check (decision G7). Live 2026-11-08: a refused checkpoint save held a gate failed_closed for hours.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest
import yaml

from gateway import transition_adapter, transition_profiles
from gateway.swap_drain import SwapDrain
from gateway.transition_gate import GateStore
from gateway.transition_ops import EffectResult, StepIntent
from gateway.transition_profiles import ProfileError, parse_profile
from gateway.transition_runtime import TransitionRuntime
from gateway.transition_snapshot_steps import snapshot_steps
from gateway.transition_snapshots import LazySnapshotStore, PrivateSnapshotDir, SnapshotBudget, SqliteSnapshotStore
from tests.test_transition_admin import Reply
from tests.test_transition_declared_snapshot_ops import (
    FakeInvoke, SNAPSHOT_PROFILE, SnapshotEngine, adapter_for, intent, raw_profile, run, snapshot_profile,
    snapshot_services,
)

EFFECTS = ["release", "load", "checkpoint", "restore", "reset"]


def attesting(**semantics: str) -> Any:
    """A snapshot profile whose blocks attest the given failure_semantics per operation."""
    blocks: dict[str, Any] = {}
    for op, value in semantics.items():
        if op == "reset":
            blocks.setdefault("restore", {})["reset"] = {"failure_semantics": value}
        else:
            blocks.setdefault(op, {}).setdefault("failure_semantics", value)
    return snapshot_profile(**blocks)


# -- profile data ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["release", "load"])
def test_release_and_load_may_declare_failure_semantics(op: str) -> None:
    profile = parse_profile(raw_profile(**{op: {"failure_semantics": "no_change"}}), source="p.yml")
    assert profile.declared[op] == {"failure_semantics": "no_change"}
    assert profile.as_policy()[op] == {"failure_semantics": "no_change"}


@pytest.mark.parametrize("op", ["release", "load", "checkpoint", "restore"])
def test_an_unknown_failure_semantics_value_is_refused(op: str) -> None:
    extra: dict[str, Any] = {}
    if op in ("checkpoint", "restore"):
        extra["operations"] = {**raw_profile()["operations"], "checkpoint": "transition.checkpoint",
                               "restore": "transition.restore", "compatibility": "transition.compatibility"}
    with pytest.raises(ProfileError, match="failure_semantics"):
        parse_profile(raw_profile(**{op: {"failure_semantics": "maybe"}}, **extra), source="p.yml")


def test_a_release_or_load_block_must_be_a_mapping() -> None:
    with pytest.raises(ProfileError, match="release is a mapping"):
        parse_profile(raw_profile(release="no_change"), source="p.yml")


# -- the adapter ----------------------------------------------------------------------------------------


async def _invoke_op(adapter: Any, op: str) -> Any:
    if op == "restore":
        return await adapter.restore("dom", "ckpt-a", intent("restore"))
    if op == "load":
        return await adapter.load("dom", "alpha", intent("load"))
    return await getattr(adapter, op)("dom", intent(op))


@pytest.mark.parametrize("op", EFFECTS)
def test_a_declared_failed_is_no_change_exactly_when_the_profile_attests_it(tmp_path: Path, op: str) -> None:
    reply = Reply(200, {"outcome": "failed", "evidence_ref": "refused"})
    table = {f"transition.{op}": reply}
    attested = adapter_for(attesting(**{op: "no_change"}), FakeInvoke(table), tmp_path)
    unknown = adapter_for(attesting(**{op: "unknown"}), FakeInvoke(table), tmp_path)
    absent = adapter_for(snapshot_profile(), FakeInvoke(table), tmp_path)
    assert run(_invoke_op(attested, op)).outcome == "no_change"
    assert run(_invoke_op(unknown, op)).outcome == "partial"
    assert run(_invoke_op(absent, op)).outcome == "partial"


@pytest.mark.parametrize("op", EFFECTS)
def test_a_declared_partial_is_partial_even_when_the_profile_attests_no_change(tmp_path: Path, op: str) -> None:
    table = {f"transition.{op}": Reply(200, {"outcome": "partial"})}
    adapter = adapter_for(attesting(**{op: "no_change"}), FakeInvoke(table), tmp_path)
    assert run(_invoke_op(adapter, op)).outcome == "partial"


@pytest.mark.parametrize("step", ["release", "load", "checkpoint", "restore"])
def test_a_query_of_a_step_uses_that_steps_semantics(tmp_path: Path, step: str) -> None:
    table = {"transition.query": Reply(200, {"outcome": "failed"})}
    attested = adapter_for(attesting(**{step: "no_change"}), FakeInvoke(table), tmp_path)
    other = "load" if step != "load" else "release"
    elsewhere = adapter_for(attesting(**{other: "no_change"}), FakeInvoke(table), tmp_path)
    assert run(attested.query("dom", intent(step))).outcome == "no_change"
    assert run(elsewhere.query("dom", intent(step))).outcome == "partial"


def test_the_adapter_has_no_per_operation_outcome_constant() -> None:
    source = Path(transition_adapter.__file__).read_text(encoding="utf-8")
    assert "failed_as" not in source
    assert not re.search(r'"failed"\s*:\s*"partial"', source), "a failed outcome maps by the profile, not by a table"
    assert not re.search(r'==\s*"(checkpoint|restore|load|reset)"', source), "no per-operation outcome branch"
    assert "failed" not in getattr(transition_adapter, "_OUTCOMES", {})


# -- end to end through the real executor, gate store and snapshot store --------------------------------


class Engine(SnapshotEngine):
    """The snapshot engine with refusable release, load and restore, and a servable observation."""

    def __init__(self) -> None:
        super().__init__()
        self.load_outcome = "success"
        self.release_outcome = "success"
        self.query_outcome = "unknown"
        self.servable: Optional[bool] = True
        self.clean: Optional[bool] = True

    async def __call__(self, service_id: str, op: str, body: Mapping[str, Any]) -> Reply:
        if op == "transition.servable":
            self.calls.append((op, dict(body)))
            return Reply(200, {"value": self.servable, "incarnation": "inc-1"})
        if op == "transition.clean":
            self.calls.append((op, dict(body)))
            return Reply(200, {"value": self.clean, "incarnation": "inc-1"})
        if op == "transition.load" and self.load_outcome != "success":
            self.calls.append((op, dict(body)))  # the engine refused the load and changed nothing
            return Reply(200, {"outcome": self.load_outcome, "evidence_ref": "load_refused"})
        if op == "transition.release" and self.release_outcome != "success":
            self.calls.append((op, dict(body)))  # the engine refused the release; the source stays resident
            return Reply(200, {"outcome": self.release_outcome, "evidence_ref": "release_refused",
                               "incarnation": "inc-1"})
        if op == "transition.query":
            self.calls.append((op, dict(body)))
            return Reply(200, {"outcome": self.query_outcome})
        return await super().__call__(service_id, op, body)


def wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, policy: str = "best_effort", verify: bool = False,
         **semantics: str):
    """The shipped snapshot profile with ``semantics`` attested and a ``servable`` observation, under the shipped id."""
    shipped = yaml.safe_load((transition_profiles.PROFILE_DIR / f"{SNAPSHOT_PROFILE}.yml").read_text(encoding="utf-8"))
    for op, value in semantics.items():
        if op == "reset":
            shipped["restore"]["reset"]["failure_semantics"] = value
        else:
            shipped.setdefault(op, {})["failure_semantics"] = value
    shipped["observations"] = {"servable": "transition.servable", "clean": "transition.clean"}
    shipped["servable_check"] = "servable"
    if verify:
        shipped["restore"]["reset"]["verify"] = "clean"
    directory = tmp_path / "profiles"
    directory.mkdir()
    (directory / f"{SNAPSHOT_PROFILE}.yml").write_text(yaml.safe_dump(shipped), encoding="utf-8")
    monkeypatch.setattr(transition_profiles, "PROFILE_DIR", directory)
    store = SqliteSnapshotStore(tmp_path / "transition.db", PrivateSnapshotDir(tmp_path / "snap"),
                                SnapshotBudget(max_bytes=10_000, max_count=4))
    lazy = LazySnapshotStore(lambda: store)
    gates = GateStore(tmp_path / "transition.db")
    gates.load()
    drain = SwapDrain(wall_clock=time.time)
    locks: dict[str, asyncio.Lock] = {}
    engine = Engine()
    def services() -> dict[str, Any]:
        found = snapshot_services()
        for record in found.values():
            if record:
                record["residency"]["transition"]["checkpoint_policy"] = policy
        return found

    runtime = TransitionRuntime(gates=lambda: gates, services=services,
                                lock_for=lambda d: locks.setdefault(d, asyncio.Lock()),
                                drain=lambda: drain, protocol_invoke=engine,
                                extra_steps=snapshot_steps(lazy), snapshots=lazy)
    return gates, engine, runtime, store


async def select(runtime: Any, key: str = "sel-1") -> Any:
    executor = runtime.executor()
    began = await executor.submit(domain="dom", kind="select", idempotency_key=key, target="beta")
    return await executor.wait(began.operation.op_id)


def test_a_failed_load_that_wrote_nothing_does_not_lock_the_gate(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, load="no_change")
    engine.load_outcome, engine.servable = "failed", True

    async def scenario() -> None:
        done = await select(runtime)
        assert (done.state, done.step, done.step_outcome) == ("aborted", "load", "no_change")
        assert gates.state("dom") == "open"

    run(scenario())


def test_a_failed_load_whose_source_is_not_servable_stays_failed_closed(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, load="no_change")
    engine.load_outcome, engine.servable = "failed", False

    async def scenario() -> None:
        done = await select(runtime)
        assert done.state == "failed_closed" and gates.state("dom") == "failed_closed"

    run(scenario())


def test_a_failed_load_with_an_unreadable_servable_check_reconciles(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, load="no_change")
    engine.load_outcome, engine.servable = "failed", None

    async def scenario() -> None:
        done = await select(runtime)
        assert done.state == "reconciling" and gates.state("dom") == "reconciling"

    run(scenario())


def test_the_same_failed_load_under_a_profile_that_does_not_attest_it_holds_the_gate(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch)
    engine.load_outcome, engine.servable = "failed", True

    async def scenario() -> None:
        done = await select(runtime)
        assert done.state == "failed_closed" and gates.state("dom") == "failed_closed"

    run(scenario())


def test_a_declared_partial_load_holds_the_gate_even_when_no_change_is_attested(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, load="no_change")
    engine.load_outcome, engine.servable = "partial", True

    async def scenario() -> None:
        done = await select(runtime)
        assert done.state == "failed_closed" and gates.state("dom") == "failed_closed"

    run(scenario())


def test_a_failed_release_that_changed_nothing_reopens_when_the_source_is_servable(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, release="no_change")
    engine.release_outcome, engine.servable = "failed", True

    async def scenario() -> None:
        executor = runtime.executor()
        began = await executor.submit(domain="dom", kind="park", idempotency_key="park-1")
        done = await executor.wait(began.operation.op_id)
        assert (done.state, done.step, done.step_outcome) == ("aborted", "release", "no_change")
        assert gates.state("dom") == "open"
        assert engine.resident == "alpha"

    run(scenario())


def test_a_failed_restore_that_changed_nothing_serves_cold_without_a_reset(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, restore="no_change")
    engine.reset_outcome = "failed"

    async def scenario() -> None:
        executor = runtime.executor()
        park = await executor.submit(domain="dom", kind="park", idempotency_key="park-1")
        assert (await executor.wait(park.operation.op_id)).state == "succeeded"
        engine.restore_outcome = "failed"
        wake = await executor.submit(domain="dom", kind="wake", idempotency_key="wake-1")
        done = await executor.wait(wake.operation.op_id)
        assert done.state == "succeeded" and gates.state("dom") == "open"
        assert engine.resets == 0, "the profile attests that a failed restore changed nothing"
        assert store.pointer("dom") is None, "the snapshot is not offered again"

    run(scenario())


def test_a_failed_restore_under_an_unattesting_profile_still_resets_and_a_failed_reset_holds(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch)
    engine.reset_outcome = "failed"

    async def scenario() -> None:
        executor = runtime.executor()
        park = await executor.submit(domain="dom", kind="park", idempotency_key="park-1")
        assert (await executor.wait(park.operation.op_id)).state == "succeeded"
        engine.restore_outcome = "failed"
        wake = await executor.submit(domain="dom", kind="wake", idempotency_key="wake-1")
        done = await executor.wait(wake.operation.op_id)
        assert done.state == "failed_closed" and engine.resets == 1

    run(scenario())


def test_a_refused_best_effort_checkpoint_still_goes_on_cold(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, checkpoint="no_change")
    engine.checkpoint_outcome = "failed"

    async def scenario() -> None:
        executor = runtime.executor()
        began = await executor.submit(domain="dom", kind="park", idempotency_key="park-1")
        done = await executor.wait(began.operation.op_id)
        assert done.state == "succeeded" and gates.state("dom") == "open"
        assert store.pointer("dom") is None

    run(scenario())


def _hold_load(runtime: Any, engine: Engine) -> Any:
    """Leave a select held reconciling at its load: the engine's answer and its query are both unknown."""
    engine.load_outcome, engine.query_outcome = "unknown", "unknown"

    async def hold() -> Any:
        done = await select(runtime)
        assert done.state == "reconciling" and done.step == "load"
        return done

    return run(hold())


def test_the_owner_resolving_an_attested_no_change_follows_the_servable_rule(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, load="no_change")
    held = _hold_load(runtime, engine)

    async def scenario() -> None:
        executor = runtime.executor()
        engine.servable = None
        still = await executor.resolve(held.op_id, step="load", outcome="no_change", evidence_ref="owner")
        assert still.state == "reconciling", "an unreadable check changes nothing"
        engine.servable = True
        done = await executor.resolve(held.op_id, step="load", outcome="no_change", evidence_ref="owner")
        assert done.state == "aborted" and gates.state("dom") == "open"

    run(scenario())


def test_the_owner_resolving_no_change_without_the_attestation_holds_failed_closed(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch)
    held = _hold_load(runtime, engine)

    async def scenario() -> None:
        engine.servable = True
        done = await runtime.executor().resolve(held.op_id, step="load", outcome="no_change", evidence_ref="owner")
        assert done.state == "failed_closed"

    run(scenario())


def test_startup_reconcile_settles_an_attested_failed_query_by_the_servable_rule(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, load="no_change")
    held = _hold_load(runtime, engine)

    async def scenario() -> None:
        engine.query_outcome, engine.servable = "failed", True
        executor = runtime.executor()
        await executor.reconcile_startup()
        done = await asyncio.to_thread(executor.store.get, held.op_id)
        assert done.state == "aborted" and gates.state("dom") == "open"

    run(scenario())


# -- restart recovery of a refused checkpoint (S8b review of train 35b) ------------------------------------------------


def _crashed_checkpoint(runtime: Any, *, recorded: bool) -> str:
    """A park that stopped at its checkpoint. ``recorded=False``: the engine refused the save and the process died before
    the refusal was written (an intent and no outcome). ``recorded=True``: the refusal was recorded and the process died
    before the next step."""
    executor = runtime.executor()
    begun = executor.store.begin(domain="dom", kind="park", idempotency_key="park-crash", target=None,
                                 incarnation="inc-1", config_revision=1)
    op_id = begun.operation.op_id
    step = StepIntent(step="checkpoint", effect_key=f"{op_id}:checkpoint", expected_incarnation="inc-1")
    executor.store.write_intent(op_id, step)
    if recorded:
        executor.store.write_outcome(op_id, "checkpoint", EffectResult(
            outcome="no_change", evidence_ref="checkpoint:engine_declined:cold_next_start"))
    return op_id


def _restart(runtime: Any, op_id: str) -> Any:
    async def go() -> Any:
        executor = runtime.executor()
        await executor.reconcile_startup()
        for _ in range(200):
            done = await asyncio.to_thread(executor.store.get, op_id)
            if done.state != "running":
                return done
            await asyncio.sleep(0.05)
        return done

    return run(go())


@pytest.mark.parametrize("recorded", [False, True])
def test_a_best_effort_checkpoint_refused_before_a_crash_goes_on_cold_after_the_restart(tmp_path, monkeypatch, recorded) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, checkpoint="no_change")
    engine.query_outcome = "failed"  # what the engine says about the refused save
    op_id = _crashed_checkpoint(runtime, recorded=recorded)
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("succeeded", "open"), (done.state, done.step, done.step_outcome)
    assert store.pointer("dom") is None and engine.resident is None, "the park went on cold and released"


@pytest.mark.parametrize("recorded", [False, True])
@pytest.mark.parametrize(("servable", "state"), [(True, "aborted"), (False, "failed_closed")])
def test_a_required_checkpoint_refused_before_a_crash_reopens_only_on_fresh_servable_evidence(
        tmp_path, monkeypatch, recorded, servable, state) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, policy="required", checkpoint="no_change")
    engine.query_outcome, engine.servable = "failed", servable
    op_id = _crashed_checkpoint(runtime, recorded=recorded)
    done = _restart(runtime, op_id)
    assert done.state == state and gates.state("dom") == ("open" if servable else "failed_closed")
    assert not any(op == "transition.release" for op, _ in engine.calls), "the source was not released"


def test_a_required_checkpoint_refused_before_a_crash_reconciles_when_servable_is_unknown(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, policy="required", checkpoint="no_change")
    engine.query_outcome, engine.servable = "failed", None
    op_id = _crashed_checkpoint(runtime, recorded=False)
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("reconciling", "reconciling")


@pytest.mark.parametrize("policy", ["best_effort", "required"])
def test_a_refused_checkpoint_the_profile_does_not_attest_stays_held_after_the_restart(tmp_path, monkeypatch, policy) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, policy=policy, checkpoint="unknown")
    engine.query_outcome, engine.servable = "failed", True
    op_id = _crashed_checkpoint(runtime, recorded=False)
    done = _restart(runtime, op_id)
    assert done.state in ("reconciling", "failed_closed") and gates.state("dom") != "open"
    assert not any(op == "transition.release" for op, _ in engine.calls)


def test_an_unattested_recorded_no_change_stays_held_after_the_restart(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, checkpoint="unknown")
    op_id = _crashed_checkpoint(runtime, recorded=True)
    done = _restart(runtime, op_id)
    assert done.state == "failed_closed" and gates.state("dom") == "failed_closed"

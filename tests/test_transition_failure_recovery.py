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


# -- a recovery that goes on to open the gate re-checks the load first (G14), by every continuation -----------------------

def _crash_for(runtime: Any, engine: Any, kind: str, case: str) -> str:
    """The three recoveries that continue to the open step: a query-settled restore, a query-settled reset, and a recorded
    cold settlement."""
    if case == "restore":
        engine.query_outcome = "success"
        executor = runtime.executor()
        begun = executor.store.begin(domain="dom", kind=kind, idempotency_key="g14", target="alpha",
                                     incarnation="inc-1", config_revision=1)
        op_id = begun.operation.op_id
        executor.store.write_intent(op_id, StepIntent(step="restore", effect_key=f"{op_id}:restore",
                                                      expected_incarnation="inc-1"))
        engine.resident = "alpha"
        return op_id
    if case == "reset":
        engine.query_outcome, engine.clean = "success", True
        executor = runtime.executor()
        begun = executor.store.begin(domain="dom", kind=kind, idempotency_key="g14", target="alpha",
                                     incarnation="inc-1", config_revision=1)
        op_id = begun.operation.op_id
        executor.store.write_intent(op_id, StepIntent(step="restore", effect_key=f"{op_id}:reset",
                                                      expected_incarnation="inc-1"))
        engine.resident = "alpha"
        return op_id
    op_id = _crashed_recorded(runtime, engine, kind=kind, step="restore", effect="reset", outcome="no_change",
                              evidence=case, target="alpha")
    engine.resident, engine.clean = "alpha", None  # the first restart cannot read the clean evidence
    return op_id


def _held_open(done: Any, gates: Any) -> None:
    assert (done.state, gates.state("dom")) == ("reconciling", "reconciling"), (done.state, done.step, done.evidence_ref)
    assert (done.step, done.step_outcome, done.evidence_ref) == ("open", "unknown", "load_unverified_at_resume")


G14_CASES = ["restore", "reset", *COLD]


@pytest.mark.parametrize("case", G14_CASES)
@pytest.mark.parametrize("kind", ["select", "wake"])
@pytest.mark.parametrize(("ready", "offering"), [(False, None), (True, "beta")])
def test_a_recovery_never_opens_the_gate_on_a_target_that_is_not_ready_or_not_the_offering(
        tmp_path, monkeypatch, kind, case, ready, offering) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    op_id = _crash_for(runtime, engine, kind, case)
    cold = case in COLD
    engine.ready, engine.offering = ready, offering
    done = _restart(runtime, op_id)
    if cold:
        assert done.state == "reconciling" and gates.state("dom") == "reconciling"  # the clean evidence is unreadable
        engine.clean = True
        done = _restart(runtime, op_id)
    _held_open(done, gates)


@pytest.mark.parametrize("how", ["absent", "stale"])
@pytest.mark.parametrize("case", G14_CASES)
def test_a_recovery_never_opens_the_gate_when_the_resident_evidence_is_absent_or_stale(tmp_path, monkeypatch, case, how) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True,
                                         fresh_within_s=1e-9 if how == "stale" else None)
    op_id = _crash_for(runtime, engine, "wake", case)
    if how == "absent":
        engine.resident = None
    done = _restart(runtime, op_id)
    if case in COLD:
        engine.clean = True
        done = _restart(runtime, op_id)
    assert gates.state("dom") != "open" and done.state != "succeeded", (done.state, done.step, done.evidence_ref)


@pytest.mark.parametrize("case", G14_CASES)
@pytest.mark.parametrize("kind", ["select", "wake"])
def test_a_recovery_opens_the_gate_when_the_target_is_resident_and_ready(tmp_path, monkeypatch, case, kind) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    op_id = _crash_for(runtime, engine, kind, case)
    done = _restart(runtime, op_id)
    if case in COLD:
        engine.clean = True
        done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("succeeded", "open"), (done.state, done.step, done.evidence_ref)


# -- the revalidation hold is one write, an old intent row still decodes, and the hold shows on the owner routes ------------

import json  # noqa: E402

from gateway.transition_store import _intent_from  # noqa: E402


def test_the_revalidation_hold_is_one_write_so_a_crash_between_intent_and_outcome_cannot_lose_the_recorded_evidence(
        tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    op_id = _crashed_recorded(runtime, engine, kind="wake", step="restore", effect="reset", outcome="no_change",
                              evidence=COLD[0], target="alpha")
    engine.resident, engine.clean = "alpha", None  # unreadable: the restart holds a revalidation
    executor = runtime.executor()

    def crash(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the process died between the intent and the outcome")

    monkeypatch.setattr(executor.store, "write_outcome", crash)

    async def go() -> None:
        try:
            await executor.reconcile_startup()
        except RuntimeError:
            pass
        await asyncio.sleep(0.2)

    run(go())
    held = executor.store.get(op_id)
    assert (held.evidence_ref or "").endswith(COLD[0]), "the hold carries the recorded evidence from its first write"
    monkeypatch.undo()
    engine.clean = True
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("succeeded", "open"), (done.state, done.evidence_ref)


@pytest.mark.parametrize("effect", ["restore", "reset", "revalidate"])
def test_a_step_intent_row_written_by_the_previous_release_still_decodes(effect) -> None:
    # The previous release stored exactly these three keys; the effect and block are derived, never stored.
    old = json.dumps({"step": "restore", "effect_key": f"op-1:{effect}", "expected_incarnation": "inc-1"}, sort_keys=True)
    decoded = _intent_from(old)
    assert (decoded.step, decoded.effect_key, decoded.expected_incarnation) == ("restore", f"op-1:{effect}", "inc-1")
    assert decoded.effect == ("reset" if effect == "reset" else "restore")


def test_the_hold_shows_on_the_owner_routes_without_a_retry_loop(tmp_path, monkeypatch) -> None:
    """GET gate reports ``reconciling`` and the operation id; GET the operation reports the step, the unknown outcome and
    the ``revalidate:`` evidence. Nothing retries on a timer: the next restart or the owner's resolve settles it."""
    import httpx
    from fastapi import APIRouter, FastAPI
    from types import SimpleNamespace
    from gateway.transition_admin import register_transition_routes

    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    op_id = _crashed_recorded(runtime, engine, kind="wake", step="restore", effect="reset", outcome="no_change",
                              evidence=COLD[0], target="alpha")
    engine.resident, engine.clean = "alpha", None
    _restart(runtime, op_id)
    router = APIRouter(prefix="/admin/api")

    async def read_json(request: Any) -> Any:
        return await request.json()

    register_transition_routes(router, runtime=lambda: runtime, guard=lambda _r: SimpleNamespace(roles=("owner",)),
                               read_json=read_json)
    app = FastAPI()
    app.include_router(router)

    async def go() -> tuple[Any, Any]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            gate = (await client.get("/admin/api/engines/domains/dom/gate")).json()
            op = (await client.get(f"/admin/api/engines/domains/dom/transitions/{op_id}")).json()["operation"]
            return gate, op

    gate, op = run(go())
    assert (gate["gate"], gate["op_id"]) == ("reconciling", op_id)
    assert (op["state"], op["step_outcome"]) == ("reconciling", "unknown")
    assert op["evidence_ref"] == f"revalidate:no_change:{COLD[0]}"


# -- the owner's continuation, a skipped restore and a crash after a settled hold (dot recheck 2299) --------------------------


def _resolve(runtime: Any, op_id: str, step: str) -> Any:
    """Resolve and wait for the resumed task in one loop (a task dies with the loop that started it)."""
    async def go() -> Any:
        executor = runtime.executor()
        await executor.resolve(op_id, step=step, outcome="success", evidence_ref="owner")
        for _ in range(200):
            done = await asyncio.to_thread(executor.store.get, op_id)
            if done.state != "running":
                return done
            await asyncio.sleep(0.05)
        return done

    return run(go())


def _held_at(runtime: Any, engine: Any, kind: str, step: str) -> str:
    """An operation held ``reconciling`` at a restore (its query is unknown) or at the open step (the load is unready)."""
    if step == "restore":
        op_id = _crash_for(runtime, engine, kind, "restore")
        engine.query_outcome = "unknown"
        done = _restart(runtime, op_id)
        assert (done.state, done.step) == ("reconciling", "restore")
    else:
        op_id = _crash_for(runtime, engine, kind, "restore")
        engine.ready = False
        done = _restart(runtime, op_id)
        assert (done.state, done.step) == ("reconciling", "open")
    return op_id


@pytest.mark.parametrize("step", ["restore", "open"])
@pytest.mark.parametrize("kind", ["select", "wake"])
def test_an_owner_resolution_never_opens_the_gate_on_a_target_that_is_not_ready(tmp_path, monkeypatch, kind, step) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    op_id = _held_at(runtime, engine, kind, step)
    engine.ready = False
    done = _resolve(runtime, op_id, step)
    assert (done.state, gates.state("dom")) == ("reconciling", "reconciling"), (done.state, done.step, done.evidence_ref)
    assert (done.step, done.evidence_ref) == ("open", "load_unverified_at_resume")
    engine.ready = True
    done = _resolve(runtime, op_id, "open")
    assert (done.state, gates.state("dom")) == ("succeeded", "open"), (done.state, done.step, done.evidence_ref)


def _crashed_after_load(runtime: Any, engine: Any, kind: str) -> str:
    executor = runtime.executor()
    begun = executor.store.begin(domain="dom", kind=kind, idempotency_key="skip", target="alpha",
                                 incarnation="inc-1", config_revision=1)
    op_id = begun.operation.op_id
    executor.store.write_intent(op_id, StepIntent(step="load", effect_key=f"{op_id}:load", expected_incarnation=None))
    executor.store.write_outcome(op_id, "load", EffectResult(outcome="success", evidence_ref="loaded"))
    engine.resident = "alpha"
    return op_id


@pytest.mark.parametrize("kind", ["select", "wake"])
@pytest.mark.parametrize(("ready", "state"), [(False, "reconciling"), (True, "succeeded")])
def test_a_restore_that_is_skipped_on_recovery_still_checks_the_load_before_open(tmp_path, monkeypatch, kind, ready, state) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    op_id = _crashed_after_load(runtime, engine, kind)
    engine.ready = ready
    done = _restart(runtime, op_id)
    assert done.state == state and gates.state("dom") == {"succeeded": "open"}.get(state, state), (done.state, done.step)
    if not ready:
        assert (done.step, done.evidence_ref) == ("open", "load_unverified_at_resume")


class Crash(BaseException):
    """The process died: nothing in the executor catches it."""


def _die_in(executor: Any, monkeypatch: Any, name: str) -> None:
    def crash(*_a: Any, **_k: Any) -> Any:
        raise Crash(name)

    monkeypatch.setattr(executor.store, name, crash)


def _restart_dying(runtime: Any) -> None:
    async def go() -> None:
        try:
            await runtime.executor().reconcile_startup()
        except Crash:
            pass

    run(go())


@pytest.mark.parametrize("kind", ["select", "wake"])
def test_a_crash_after_the_owner_settled_a_hold_recovers_on_the_next_start_without_another_owner_action(
        tmp_path, monkeypatch, kind) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    op_id = _held_at(runtime, engine, kind, "restore")
    restores = engine.restores[:]
    executor = runtime.executor()
    with monkeypatch.context() as patch:
        _die_in(executor, patch, "resume")
        with pytest.raises(Crash):
            run(executor.resolve(op_id, step="restore", outcome="success", evidence_ref="owner"))
    row = executor.store.get(op_id)
    assert (row.state, row.step_outcome) == ("reconciling", "success"), "the settled outcome is committed, the resume is not"
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("succeeded", "open"), (done.state, done.step, done.evidence_ref)
    assert engine.restores == restores and engine.resets == 0, "the effect was not replayed"


@pytest.mark.parametrize("kind", ["select", "wake"])
def test_a_crash_after_a_revalidation_settled_recovers_on_the_next_start(tmp_path, monkeypatch, kind) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True)
    op_id = _crashed_recorded(runtime, engine, kind=kind, step="restore", effect="reset", outcome="no_change",
                              evidence=COLD[0], target="alpha")
    engine.resident, engine.clean = "alpha", None
    done = _restart(runtime, op_id)
    assert done.state == "reconciling"
    engine.clean = True
    executor = runtime.executor()
    with monkeypatch.context() as patch:
        _die_in(executor, patch, "resume")
        _restart_dying(runtime)
    row = executor.store.get(op_id)
    assert (row.state, row.step_outcome) == ("reconciling", "no_change"), (row.state, row.step_outcome)
    done = _restart(runtime, op_id)
    assert (done.state, gates.state("dom")) == ("succeeded", "open"), (done.state, done.step, done.evidence_ref)


# -- a revalidation keeps the recorded outcome (recheck 3) ---------------------------------------------------------------


def test_revalidation_keeps_a_recorded_partial_restore(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True, reset="no_change")
    engine.reset_outcome, engine.clean = "failed", None  # the declared reset runs, but its clean check cannot be read
    op_id = _crashed_recorded(runtime, engine, kind="wake", step="restore", effect="restore", outcome="partial",
                              evidence="restore:failed:state_not_known_clean", target="alpha")
    engine.resident = "alpha"
    first = _restart(runtime, op_id)
    assert (first.state, gates.state("dom")) == ("reconciling", "reconciling"), (first.state, first.step)
    assert (first.evidence_ref or "").startswith("revalidate:"), first.evidence_ref
    assert engine.resets == 1
    engine.clean = True
    done = _restart(runtime, op_id)
    assert engine.resets == 1, "the declared reset ran exactly once across both restarts"
    assert (done.state, gates.state("dom")) == ("succeeded", "open"), (done.state, done.step, done.evidence_ref)


def test_a_revalidation_hold_carries_the_recorded_outcome_not_no_change(tmp_path, monkeypatch) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True, reset="no_change")
    engine.reset_outcome, engine.clean = "failed", None
    op_id = _crashed_recorded(runtime, engine, kind="wake", step="restore", effect="restore", outcome="partial",
                              evidence="restore:failed:state_not_known_clean", target="alpha")
    engine.resident = "alpha"
    _restart(runtime, op_id)
    executor = runtime.executor()
    seen: list[tuple[str, str | None]] = []
    real = executor.store.write_outcome

    def spy(op: str, step: Any, result: Any) -> Any:
        seen.append((result.outcome, result.evidence_ref))
        return real(op, step, result)

    monkeypatch.setattr(executor.store, "write_outcome", spy)
    engine.clean = True
    _restart(runtime, op_id)
    assert all(outcome != "no_change" or (evidence or "").startswith("restore:reset") for outcome, evidence in seen), seen


# -- an unknown reset is not run again; a hold from the previous format still re-reads the clean check (review 2327) -------


@pytest.mark.parametrize("crash_before_finish", [False, True])
def test_a_reset_whose_outcome_is_unknown_is_never_run_again_by_the_next_restart(tmp_path, monkeypatch, crash_before_finish) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True, reset="no_change")
    engine.reset_outcome, engine.query_outcome, engine.clean = "unknown", "unknown", True
    op_id = _crashed_recorded(runtime, engine, kind="wake", step="restore", effect="restore", outcome="partial",
                              evidence="restore:failed:state_not_known_clean", target="alpha")
    engine.resident = "alpha"
    executor = runtime.executor()
    if crash_before_finish:  # the process dies after the hold is written and before the operation is finished
        with monkeypatch.context() as patch:
            _die_in(executor, patch, "finish")
            _restart_dying(runtime)
    else:
        first = _restart(runtime, op_id)
        assert (first.state, gates.state("dom")) == ("reconciling", "reconciling")
    assert engine.resets == 1
    row = executor.store.get(op_id)
    second = _restart(runtime, op_id)
    assert engine.resets == 1, "the second restart queries the dispatched reset; it does not run it again"
    assert (second.state, gates.state("dom")) == ("reconciling", "reconciling")
    assert not (row.evidence_ref or "").startswith("revalidate:"), "the held reset is not rewritten into the stale partial"


@pytest.mark.parametrize("evidence", ["restore:failed:state_not_known_clean", "reset:target_not_verified_clean"])
@pytest.mark.parametrize("clean", [True, False])
def test_a_hold_written_in_the_previous_format_re_reads_the_clean_check(tmp_path, monkeypatch, evidence, clean) -> None:
    gates, engine, runtime, store = wire(tmp_path, monkeypatch, verify=True, reset="no_change")
    engine.clean = clean
    executor = runtime.executor()
    begun = executor.store.begin(domain="dom", kind="wake", idempotency_key="old-hold", target="alpha",
                                 incarnation="inc-1", config_revision=1)
    op_id = begun.operation.op_id
    executor.store.write_intent(op_id, StepIntent(step="restore", effect_key=f"{op_id}:restore:revalidate",
                                                  expected_incarnation="inc-1"),
                                EffectResult(outcome="unknown", evidence_ref=f"revalidate:{evidence}"))
    executor.store.finish(op_id, "reconciling", "reconciling")
    engine.resident = "alpha"
    done = _restart(runtime, op_id)
    want = ("succeeded", "open") if clean else ("failed_closed", "failed_closed")
    assert (done.state, gates.state("dom")) == want, (done.state, done.step, done.evidence_ref)
    assert engine.resets == 0, "the reset already ran; it is not run again"

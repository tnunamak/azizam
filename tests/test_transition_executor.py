"""The transition executor (T1.2): oracle items 2, 6, 7 and the load part of 8, on two differently shaped
fake adapters (tests/support/transition_fakes.py), plus the drain, open and recovery steps of D4.

Each test that names ``adapter`` runs once on ``DirectAdapter`` (replies itself) and once on
``LedgerAdapter`` (answers unknown to every effect and settles it later; only ``query`` learns the outcome).
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from gateway.transition_executor import InvalidTransition, TriggerLease
from gateway.transition_gate import TransitionPolicy, TransitionUnavailable, admit, policy_for
from gateway.transition_ops import Begun, Conflict, EffectResult, SameOperation, StepIntent
from gateway.transition_store import StaleOutcome
from tests.support.transition_fakes import DOMAIN, Harness

ADAPTERS = pytest.mark.parametrize("adapter", ["direct", "ledger"])


def run(scenario: Callable[[], Awaitable[Any]]) -> Any:
    return asyncio.run(scenario())


def refusal(gate_state: str, retry_after_s: float = 7.0) -> TransitionUnavailable:
    with pytest.raises(TransitionUnavailable) as caught:
        admit(TransitionPolicy(domain=DOMAIN, enabled=True, retry_after_s=retry_after_s), gate_state, {})
    return caught.value


# -- the success path (D4 steps 1 to 7) ------------------------------------------------------------------


@ADAPTERS
def test_select_drains_releases_loads_verifies_and_opens(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        lease = TriggerLease(lambda: h.drain.lease(DOMAIN, admit=False))
        began = await h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="k1", target="beta", trigger=lease.trigger)
        assert isinstance(began, Begun)
        assert h.gate() == "closed", "the gate is closed durably before the executor task has run"
        assert refusal(h.gate()).reason == "gate_closed"
        done = await h.executor.wait(began.operation.op_id)
        assert (done.state, h.gate(), h.sim.offering) == ("succeeded", "open", "beta")
        assert done.stock == "released", "an attested release counts as stock released"
        assert [name for name, _ in h.sim.calls if name in ("release", "load")] == ["release", "load"]
        assert lease.minted == 1 and h.drain.in_flight(DOMAIN) == 1, "the trigger's lease is minted once, by the open step"
        assert h.store.last_target(DOMAIN) == "beta"

    run(scenario)


@ADAPTERS
def test_park_leaves_nothing_resident_and_the_domain_parked_until_a_wake(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        parked = await h.run("park", "p1")
        assert (parked.state, h.gate(), h.sim.offering) == ("succeeded", "open", None)
        assert h.gates.parked(DOMAIN) == parked.op_id
        assert not h.steps_called("load")
        woke = await h.run("wake", "w1")
        assert (woke.state, h.sim.offering, woke.target) == ("succeeded", "alpha", "alpha"), "wake loads the last offering"
        assert h.gates.parked(DOMAIN) is None

    run(scenario)


def test_a_wake_with_no_known_offering_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        with pytest.raises(InvalidTransition):
            await h.executor.submit(domain=DOMAIN, kind="wake", idempotency_key="w")
        with pytest.raises(InvalidTransition):
            await h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="s")
        assert h.store.current(DOMAIN) is None and h.gate() == "open"

    run(scenario)


# -- drain (oracle 1 shape, decision G2) ----------------------------------------------------------------


@ADAPTERS
def test_no_effect_starts_until_the_leases_drain_and_the_engine_reports_idle(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter, policy={"drain_deadline_s": 5.0})
        lease = h.drain.lease(DOMAIN)
        h.sim.busy = 1
        began = await h.executor.submit(domain=DOMAIN, kind="park", idempotency_key="p")
        await asyncio.sleep(0.15)
        assert not h.steps_called("release"), "a lease is still held"
        lease.release()
        await asyncio.sleep(0.15)
        assert not h.steps_called("release"), "the leases drained but the engine does not report idle"
        h.sim.busy = 0
        done = await h.executor.wait(began.operation.op_id)
        assert done.state == "succeeded" and len(h.steps_called("release")) == 1

    run(scenario)


@ADAPTERS
def test_a_drain_that_misses_its_deadline_aborts_with_no_effect_and_opens(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.drain.lease(DOMAIN)  # never released
        done = await h.run("park", "p")
        assert (done.state, done.step, done.step_outcome, h.gate()) == ("aborted", "drain", "no_change", "open")
        assert not h.steps_called("release") and h.sim.offering == "alpha"

    run(scenario)


@ADAPTERS
@pytest.mark.parametrize("mode", ["stale", "other_incarnation", "unknown"])
def test_idle_evidence_that_is_stale_foreign_or_missing_is_not_idle(tmp_path: Path, adapter: str, mode: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.sim.arm_evidence(mode, after="idle", sticky=True)  # the engine is idle, but never says so in a usable way
        done = await h.run("park", "k")
        assert (done.state, done.step_outcome, h.gate()) == ("aborted", "no_change", "open")
        assert not h.steps_called("release")

    run(scenario)


def test_a_drain_with_nothing_resident_has_nothing_to_wait_for(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        h.sim.offering, h.sim.busy = None, 1  # an out-of-band unload: gone, and the idle probe says busy
        done = await h.run("select", "s", "beta")
        assert done.state == "succeeded" and h.sim.offering == "beta"

    run(scenario)


# -- oracle 6: concurrency, idempotency ----------------------------------------------------------------


@ADAPTERS
def test_concurrent_select_park_and_wake_one_wins_and_the_others_get_its_id(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        await h.run("select", "s0", "alpha")
        h.sim.calls.clear()
        h.sim.delay = 0.05
        results = await asyncio.gather(
            h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="a", target="beta"),
            h.executor.submit(domain=DOMAIN, kind="park", idempotency_key="b"),
            h.executor.submit(domain=DOMAIN, kind="wake", idempotency_key="c"))
        winners = [r for r in results if isinstance(r, Begun)]
        assert len(winners) == 1
        for result in results:
            if isinstance(result, Conflict):
                assert result.operation.op_id == winners[0].operation.op_id, "every 409 carries the holding op id"
        await h.executor.wait(winners[0].operation.op_id)
        assert len(h.steps_called("release")) <= 1 and len(h.steps_called("load")) <= 1

    run(scenario)


@ADAPTERS
def test_the_same_key_twice_in_flight_is_one_operation_with_one_set_of_effects(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.sim.delay = 0.05
        first, second = await asyncio.gather(
            h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="k", target="beta"),
            h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="k", target="beta"))
        assert isinstance(first, Begun) and isinstance(second, SameOperation)
        assert first.operation.op_id == second.operation.op_id
        done = await h.executor.wait(first.operation.op_id)
        # A retry after the operation finished is the same operation too, and repeats nothing.
        again = await h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="k", target="beta")
        assert isinstance(again, SameOperation) and again.operation.state == "succeeded" == done.state
        assert len(h.steps_called("release")) == 1 and len(h.steps_called("load")) == 1

    run(scenario)


def test_the_same_key_with_another_request_is_a_conflict_that_names_the_operation(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        first = await h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="k", target="beta")
        other = await h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="k", target="gamma")
        assert isinstance(other, Conflict) and other.operation.op_id == first.operation.op_id
        await h.executor.wait(first.operation.op_id)
        other = await h.executor.submit(domain=DOMAIN, kind="park", idempotency_key="k")
        assert isinstance(other, Conflict) and other.operation.op_id == first.operation.op_id
        assert len(h.steps_called("release")) == 1

    run(scenario)


def test_independent_domains_run_at_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        h.sim.delay = 0.05
        one, two = await asyncio.gather(
            h.executor.submit(domain="dom-1", kind="park", idempotency_key="k"),
            h.executor.submit(domain="dom-2", kind="park", idempotency_key="k"))
        assert isinstance(one, Begun) and isinstance(two, Begun)
        await asyncio.gather(h.executor.wait(one.operation.op_id), h.executor.wait(two.operation.op_id))

    run(scenario)


@ADAPTERS
def test_an_unload_outside_the_gate_does_not_stop_a_park_or_a_select(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.sim.offering, h.sim.ready, h.sim.stock = None, False, 0
        done = await h.run("park", "p")
        assert done.state == "succeeded"
        h.sim.offering, h.sim.ready = "intruder", True  # loaded outside the gate while parked
        done = await h.run("select", "s", "beta")
        assert done.state == "succeeded" and h.sim.offering == "beta"

    run(scenario)


# -- oracle 2: cancellation ---------------------------------------------------------------------------


@pytest.mark.parametrize("ticks", [0, 1, 2, 3, 5, 8, 13, 40])
def test_a_caller_cancelled_at_any_await_never_leaves_a_half_done_transition(tmp_path: Path, ticks: int) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)

        async def caller() -> None:
            began = await h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="k", target="beta")
            await h.executor.wait(began.operation.op_id)

        task = asyncio.get_running_loop().create_task(caller())
        for _ in range(ticks):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.4)
        found = h.store.by_idempotency_key(DOMAIN, "k")
        if found is None:
            assert h.gate() == "open" and not h.locks.get(DOMAIN, asyncio.Lock()).locked(), "cancelled before the lock was taken"
            return
        done = await h.executor.wait(found.op_id)
        assert (done.state, h.gate(), h.sim.offering) == ("succeeded", "open", "beta")
        assert len(h.steps_called("release")) == 1 and len(h.steps_called("load")) == 1
        assert not h.locks[DOMAIN].locked()

    run(scenario)


def test_a_caller_cancelled_while_it_waits_for_the_lock_leaves_no_operation(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        lock = h.locks.setdefault(DOMAIN, asyncio.Lock())
        await lock.acquire()
        task = asyncio.get_running_loop().create_task(
            h.executor.submit(domain=DOMAIN, kind="park", idempotency_key="k"))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert h.store.current(DOMAIN) is None and h.store.by_idempotency_key(DOMAIN, "k") is None
        assert h.gate() == "open"
        lock.release()
        assert not lock.locked()

    run(scenario)


@ADAPTERS
def test_a_lock_that_stays_busy_refuses_with_retry_after(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter, policy={"lock_wait_s": 0.05})
        await h.locks.setdefault(DOMAIN, asyncio.Lock()).acquire()
        with pytest.raises(TransitionUnavailable) as refused:
            await h.executor.submit(domain=DOMAIN, kind="park", idempotency_key="k")
        assert refused.value.headers == {"Retry-After": "7"} and h.store.current(DOMAIN) is None

    run(scenario)


def test_a_reply_lost_after_the_engine_accepted_is_resolved_by_query_not_repeated(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        h.sim.faults["release"] = "lost_reply"
        h.sim.faults["load"] = "lost_reply"
        done = await h.run("select", "k", "beta")
        assert (done.state, h.sim.offering, h.gate()) == ("succeeded", "beta", "open")
        assert len(h.steps_called("release")) == 1 and len(h.steps_called("load")) == 1, "no effect is repeated"
        assert h.steps_called("query"), "the lost reply was asked of query"

    run(scenario)


def test_a_reply_lost_with_no_way_to_ask_holds_reconciling_and_never_reopens(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, can_query=False)
        h.sim.faults["release"] = "lost_reply"
        done = await h.run("park", "k")
        assert (done.state, done.step, done.step_outcome, h.gate()) == ("reconciling", "release", "unknown", "reconciling")
        assert refusal(h.gate()).headers == {"Retry-After": "7"}
        # Reconciling refuses a new transition (G9) and cannot be finished unresolved.
        other = await h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="z", target="beta")
        assert isinstance(other, Conflict) and other.operation.op_id == done.op_id
        with pytest.raises(StaleOutcome):
            h.store.finish(done.op_id, "aborted", "open")
        assert h.gate() == "reconciling"

    run(scenario)


@pytest.mark.parametrize("fault", ["hang", "lost_reply"])
def test_a_load_that_times_out_stays_closed_until_resolved(tmp_path: Path, fault: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, can_query=False)
        h.sim.faults["load"] = fault
        done = await h.run("select", "k", "beta")
        assert (done.state, done.step, h.gate()) == ("reconciling", "load", "reconciling")

    run(scenario)


# -- the owner resolves a held step (G3) ----------------------------------------------------------------


def test_the_owner_resolves_a_held_release_and_the_operation_goes_on(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, can_query=False)
        h.sim.faults["release"] = "lost_reply"
        held = await h.run("select", "k", "beta")
        assert held.state == "reconciling"
        resolved = await h.executor.resolve(held.op_id, step="release", outcome="success", evidence_ref="owner:checked")
        assert resolved.state == "running"
        done = await h.executor.wait(held.op_id)
        assert (done.state, h.gate(), h.sim.offering) == ("succeeded", "open", "beta")
        assert done.stock == "observed_not_resident", "an owner's word is not an attestation"

    run(scenario)


def test_an_owner_resolution_that_says_failed_holds_failed_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, can_query=False)
        h.sim.faults["load"] = "lost_reply"
        held = await h.run("select", "k", "beta")
        resolved = await h.executor.resolve(held.op_id, step="load", outcome="partial", evidence_ref="owner")
        assert (resolved.state, h.gate()) == ("failed_closed", "failed_closed")

    run(scenario)


def test_a_resolution_for_the_wrong_step_or_a_finished_operation_is_refused(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, can_query=False)
        h.sim.faults["release"] = "lost_reply"
        held = await h.run("park", "k")
        with pytest.raises(StaleOutcome):
            await h.executor.resolve(held.op_id, step="load", outcome="success", evidence_ref=None)
        await h.executor.resolve(held.op_id, step="release", outcome="unknown", evidence_ref=None)
        assert h.store.get(held.op_id).state == "reconciling", "an unknown resolution changes nothing"
        done = await h.run("park", "k")  # same key: the same, still unfinished operation
        assert done.op_id == held.op_id
        await h.executor.resolve(held.op_id, step="release", outcome="success", evidence_ref="o")
        finished = await h.executor.wait(held.op_id)
        assert finished.state == "succeeded"
        with pytest.raises(StaleOutcome):
            await h.executor.resolve(held.op_id, step="release", outcome="success", evidence_ref="again")

    run(scenario)


# -- oracle 7: release ----------------------------------------------------------------------------------


@ADAPTERS
@pytest.mark.parametrize("mode", ["stale", "other_incarnation", "unknown"])
def test_release_evidence_that_is_stale_foreign_or_missing_settles_no_stock(tmp_path: Path, adapter: str, mode: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter, can_query=False) if adapter == "direct" else Harness(tmp_path, adapter=adapter)
        h.sim.arm_evidence(mode, after="release")
        done = await h.run("park", "k")
        assert (done.state, done.step, done.step_outcome) == ("reconciling", "release", "unknown")
        assert done.stock is None and h.gate() == "reconciling"
        assert h.gates.parked(DOMAIN) is None, "an unverified release does not park the domain"

    run(scenario)


@ADAPTERS
def test_a_residual_allocation_after_release_settles_no_stock(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.sim.faults["release"] = "residual"
        done = await h.run("park", "k")
        assert (done.state, done.step_outcome, done.stock, h.gate()) == ("failed_closed", "partial", None, "failed_closed")
        assert not h.steps_called("load")

    run(scenario)


def test_a_release_the_adapter_calls_a_success_is_not_believed_while_the_engine_still_holds_the_offering(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)

        async def liar(domain: str, intent: StepIntent) -> EffectResult:
            return EffectResult(outcome="success", attested=True)  # claims success; the engine did nothing

        h.adapter.release = liar  # type: ignore[method-assign]
        done = await h.run("park", "k")
        assert (done.state, done.step_outcome, done.stock) == ("failed_closed", "partial", None)

    run(scenario)


@ADAPTERS
def test_a_release_that_completes_after_its_deadline_settles_nothing_by_itself(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter, **({"can_query": False} if adapter == "direct" else {"settle_delay": 0.5, "query_wait": 0.05}))
        if adapter == "direct":
            h.sim.delay = 1.0  # answers after the 0.3 s deadline; the engine applies it after that
            h.sim.faults["release"] = "lost_reply"
        done = await h.run("park", "k")
        assert (done.state, done.step_outcome, done.stock) == ("reconciling", "unknown", None)
        await asyncio.sleep(0.8)  # the late completion lands; the record does not move by itself
        assert h.store.get(done.op_id).state == "reconciling" and h.gate() == "reconciling"

    run(scenario)


@pytest.mark.parametrize("attested, stock", [(True, "released"), (False, "observed_not_resident")])
def test_only_an_attested_release_counts_as_stock_released(tmp_path: Path, attested: bool, stock: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, attested=attested)
        done = await h.run("park", "k")
        assert (done.state, done.stock) == ("succeeded", stock)

    run(scenario)


@ADAPTERS
def test_a_refused_release_holds_failed_closed(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.sim.faults["release"] = "fail"
        done = await h.run("park", "k")
        assert (done.state, done.step_outcome, h.gate()) == ("failed_closed", "no_change", "failed_closed")
        assert refusal(h.gate()).reason == "gate_failed_closed"

    run(scenario)


# -- oracle 8, load part, and the compensating transition (D4 step 5) ----------------------------------


@ADAPTERS
@pytest.mark.parametrize("fault", ["fail", "partial", "wrong_target", "not_ready"])
def test_a_failed_partial_or_unverified_load_holds_failed_closed_with_no_lease_and_no_rollback(
        tmp_path: Path, adapter: str, fault: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.sim.faults["load"] = fault
        lease = TriggerLease(lambda: h.drain.lease(DOMAIN, admit=False))
        began = await h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="k", target="beta", trigger=lease.trigger)
        done = await h.executor.wait(began.operation.op_id)
        assert (done.state, done.step, h.gate()) == ("failed_closed", "load", "failed_closed")
        assert lease.minted == 0 and h.drain.in_flight(DOMAIN) == 0, "no lease for a transition that failed"
        loads = h.steps_called("load")
        assert len(loads) == 1 and loads[0].endswith(":beta"), "no rollback call to the previous offering"
        assert len(h.steps_called("release")) == 1

    run(scenario)


@ADAPTERS
def test_a_load_whose_readiness_evidence_is_stale_is_unknown_not_ready(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.sim.arm_evidence("stale", after="load")
        done = await h.run("select", "k", "beta")
        assert (done.state, done.step, done.step_outcome, h.gate()) == ("reconciling", "load", "unknown", "reconciling")

    run(scenario)


@ADAPTERS
def test_a_failed_load_is_recovered_by_a_new_compensating_operation(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.sim.faults["load"] = "fail"
        failed = await h.run("select", "k", "beta")
        assert failed.state == "failed_closed"
        assert h.sim.offering is None, "the release happened and the load did not"
        # G9: a failed_closed gate accepts a new owner transition; this one compensates the first.
        fixed = await h.run("select", "k2", "alpha", compensates=failed.op_id)
        assert (fixed.state, fixed.compensates, h.gate(), h.sim.offering) == ("succeeded", failed.op_id, "open", "alpha")
        assert h.store.get(failed.op_id).state == "failed_closed", "the failed operation is history, not rewritten"

    run(scenario)


@ADAPTERS
def test_a_compensating_operation_whose_load_is_refused_leaves_the_gate_failed_closed(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        h.sim.faults["load"] = "fail"
        failed = await h.run("select", "k", "beta")
        h.sim.faults["load"] = "fail"
        again = await h.run("select", "k2", "alpha", compensates=failed.op_id)
        assert (again.state, h.gate()) == ("failed_closed", "failed_closed")
        third = await h.run("select", "k3", "alpha", compensates=again.op_id)
        assert (third.state, h.gate()) == ("succeeded", "open")

    run(scenario)


def test_a_compensating_operation_must_name_a_failed_closed_operation_of_the_domain(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        good = await h.run("park", "p")
        for bad in (good.op_id, "no-such-op"):
            with pytest.raises(InvalidTransition):
                await h.executor.submit(domain=DOMAIN, kind="wake", idempotency_key="w", compensates=bad)

    run(scenario)


# -- the trigger lease (decision G6) -----------------------------------------------------------------------


def test_the_trigger_lease_is_minted_under_the_lock_while_the_gate_is_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        seen: list[tuple[str, bool]] = []

        def mint() -> Any:
            seen.append((h.gate(), h.locks[DOMAIN].locked()))
            return h.drain.lease(DOMAIN, admit=False)

        lease = TriggerLease(mint)
        done = await h.run("select", "k", "beta", trigger=lease.trigger)
        assert done.state == "succeeded" and seen == [("closed", True)]
        assert lease.take() is not None

    run(scenario)


def test_a_requester_that_left_before_the_open_step_mints_and_leaks_no_lease(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        lease = TriggerLease(lambda: h.drain.lease(DOMAIN, admit=False))
        began = await h.executor.submit(domain=DOMAIN, kind="select", idempotency_key="k", target="beta", trigger=lease.trigger)
        lease.abandon()
        await h.executor.wait(began.operation.op_id)
        assert lease.minted == 0 and h.drain.in_flight(DOMAIN) == 0

        h2_lease = TriggerLease(lambda: h.drain.lease(DOMAIN, admit=False))
        done = await h.run("select", "k2", "alpha", trigger=h2_lease.trigger)
        assert done.state == "succeeded" and h.drain.in_flight(DOMAIN) == 1
        h2_lease.abandon()
        assert h.drain.in_flight(DOMAIN) == 0, "abandoning after the mint releases the lease"

    run(scenario)


# -- a parked domain (D4 "After a park") ----------------------------------------------------------------


def test_requests_to_a_parked_domain_join_one_wake_and_only_the_first_gets_the_trigger_lease(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        await h.run("park", "p")
        h.sim.delay = 0.05
        leases = [TriggerLease(lambda: h.drain.lease(DOMAIN, admit=False)) for _ in range(3)]
        woke = await asyncio.gather(*(h.executor.ensure_awake(DOMAIN, wait_s=2.0, trigger=lease.trigger) for lease in leases))
        assert all(op is not None and op.state == "succeeded" for op in woke)
        assert len({op.op_id for op in woke}) == 1, "one wake for every request"
        assert len(h.steps_called("load")) == 1
        assert sum(lease.minted for lease in leases) == 1 and h.drain.in_flight(DOMAIN) == 1
        assert h.gates.parked(DOMAIN) is None and h.gate() == "open"
        assert await h.executor.ensure_awake(DOMAIN, wait_s=1.0) is None, "not parked any more"

    run(scenario)


def test_a_wake_that_is_not_done_in_time_answers_503_with_retry_after_and_keeps_going(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, policy={"load_deadline_s": 2.0})
        await h.run("park", "p")
        h.sim.delay = 0.4
        with pytest.raises(TransitionUnavailable) as late:
            await h.executor.ensure_awake(DOMAIN, wait_s=0.05)
        assert (late.value.status_code, late.value.reason, late.value.headers) == (503, "wake_in_progress", {"Retry-After": "7"})
        await asyncio.sleep(0.8)
        assert h.gate() == "open" and h.sim.offering == "alpha", "the wake finished without its waiter"

    run(scenario)


@pytest.mark.parametrize("fault", ["fail", "not_ready"])
def test_a_wake_that_does_not_verify_is_never_served(tmp_path: Path, fault: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        await h.run("park", "p")
        h.sim.faults["load"] = fault
        with pytest.raises(TransitionUnavailable) as refused:
            await h.executor.ensure_awake(DOMAIN, wait_s=2.0)
        assert refused.value.reason == "wake_failed_closed" and h.gate() == "failed_closed"
        assert h.gates.parked(DOMAIN) is not None or h.gate() != "open"

    run(scenario)


def test_a_waiter_cancelled_during_the_wake_does_not_stop_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        await h.run("park", "p")
        h.sim.delay = 0.05
        lease = TriggerLease(lambda: h.drain.lease(DOMAIN, admit=False))
        task = asyncio.get_running_loop().create_task(h.executor.ensure_awake(DOMAIN, wait_s=2.0, trigger=lease.trigger))
        await asyncio.sleep(0.02)
        task.cancel()
        lease.abandon()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0.4)
        assert (h.gate(), h.sim.offering, h.drain.in_flight(DOMAIN)) == ("open", "alpha", 0)

    run(scenario)


# -- restart: reconcile before any admission (D2 "Startup") ------------------------------------------------


def _begun(h: Harness, kind: str = "select", target: str | None = "beta", key: str = "k"):
    result = h.store.begin(domain=DOMAIN, kind=kind, idempotency_key=key, target=target,  # type: ignore[arg-type]
                           incarnation=h.sim.incarnation, config_revision=1)
    assert isinstance(result, Begun)
    return result.operation


def _intent(op_id: str, h: Harness, step: str) -> StepIntent:
    intent = StepIntent(step=step, effect_key=f"{op_id}:{step}", expected_incarnation=h.sim.incarnation)  # type: ignore[arg-type]
    h.store.write_intent(op_id, intent)
    return intent


@ADAPTERS
def test_the_gate_is_loaded_from_the_table_before_the_first_admission(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        _begun(h)
        fresh = h.restart(tmp_path)
        assert fresh.gate() == "closed", "a new process reads the closed gate before it reconciles anything"
        assert refusal(fresh.gate()).reason == "gate_closed"

    run(scenario)


@ADAPTERS
def test_a_restart_after_the_close_transaction_goes_on_from_the_drain(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        op = _begun(h)
        fresh = h.restart(tmp_path)
        await fresh.executor.reconcile_startup()
        done = await fresh.executor.wait(op.op_id)
        assert (done.state, fresh.gate(), fresh.sim.offering) == ("succeeded", "open", "beta")

    run(scenario)


def test_a_drain_resumed_after_a_restart_keeps_its_original_deadline(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        op = _begun(h)
        _intent(op.op_id, h, "drain")
        h.clock.now += 1000  # the deadline passed while the gateway was down
        fresh = h.restart(tmp_path)
        await fresh.executor.reconcile_startup()
        done = await fresh.executor.wait(op.op_id)
        assert (done.state, done.step_outcome, fresh.gate()) == ("aborted", "no_change", "open")
        assert not fresh.steps_called("release")

    run(scenario)


@ADAPTERS
def test_a_restart_with_a_release_intent_and_no_outcome_asks_query_and_goes_on(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        op = _begun(h)
        intent = _intent(op.op_id, h, "release")
        if adapter == "direct":
            h.sim.ledger[intent.effect_key] = "applied"
            h.sim.offering, h.sim.ready = None, False
        else:
            h.adapter._start(intent.effect_key, lambda: "applied")  # the engine accepted it before the crash
            h.sim.offering, h.sim.ready = None, False
        fresh = h.restart(tmp_path)
        fresh.adapter = h.adapter
        await fresh.executor.reconcile_startup()
        done = await fresh.executor.wait(op.op_id)
        assert (done.state, fresh.sim.offering, done.stock) == ("succeeded", "beta", "released")
        assert len(fresh.steps_called("release")) == 0, "the effect is never repeated after a restart"

    run(scenario)


@ADAPTERS
def test_a_restart_with_an_unresolved_intent_holds_reconciling_with_retry_after(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        op = _begun(h)
        _intent(op.op_id, h, "load")
        fresh = h.restart(tmp_path)
        await fresh.executor.reconcile_startup()
        held = fresh.store.get(op.op_id)
        assert (held.state, held.step_outcome, fresh.gate()) == ("reconciling", "unknown", "reconciling")
        assert refusal(fresh.gate()).headers == {"Retry-After": "7"}
        again = fresh.restart(tmp_path)
        await again.executor.reconcile_startup()
        assert again.gate() == "reconciling", "a second restart does not reopen it"

    run(scenario)


def test_a_restart_before_the_open_transaction_opens_without_minting_a_lease(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        op = _begun(h)
        d = _intent(op.op_id, h, "drain")
        h.store.write_outcome(op.op_id, "drain", EffectResult(outcome="success"))
        r = _intent(op.op_id, h, "release")
        h.store.write_outcome(op.op_id, "release", EffectResult(outcome="success", attested=True))
        h.sim.offering = None
        ld = _intent(op.op_id, h, "load")
        h.sim.offering, h.sim.ready = "beta", True
        h.store.write_outcome(op.op_id, "load", EffectResult(outcome="success"))
        _intent(op.op_id, h, "open")
        assert (d.step, r.step, ld.step) == ("drain", "release", "load")
        fresh = h.restart(tmp_path)
        await fresh.executor.reconcile_startup()
        done = await fresh.executor.wait(op.op_id)
        assert (done.state, fresh.gate(), fresh.drain.in_flight(DOMAIN)) == ("succeeded", "open", 0)
        assert not fresh.steps_called("release") and not fresh.steps_called("load")

    run(scenario)


def test_a_restart_after_the_open_transaction_has_nothing_to_reconcile(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        await h.run("select", "k", "beta")
        fresh = h.restart(tmp_path)
        assert fresh.store.unfinished() == []
        await fresh.executor.reconcile_startup()
        assert fresh.gate() == "open" and not fresh.executor.active()

    run(scenario)


def test_a_restart_with_no_adapter_for_the_domain_holds_the_gate(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        op = _begun(h)
        _intent(op.op_id, h, "release")
        fresh = h.restart(tmp_path)

        def gone(domain: str) -> Any:
            raise KeyError(domain)

        fresh.executor._adapter_for = gone  # type: ignore[assignment]
        await fresh.executor.reconcile_startup()
        assert fresh.gate() == "reconciling" and fresh.store.get(op.op_id).state == "reconciling"

    run(scenario)


def test_a_late_outcome_from_a_dead_executor_is_ignored(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, can_query=False)
        op = _begun(h)
        intent = _intent(op.op_id, h, "release")
        fresh = h.restart(tmp_path)
        await fresh.executor.reconcile_startup()
        held = fresh.store.get(op.op_id)
        assert held.state == "reconciling"
        # The dead executor's reply arrives now. It is for the same step, but it is not an owner resolution.
        late = EffectResult(outcome="success", attested=True, incarnation="inc-foreign")
        after = fresh.store.write_outcome(op.op_id, "release", late)
        assert after.step_outcome == "unknown", "a result from another incarnation never settles the step"
        wrong_step = fresh.store.write_outcome(op.op_id, "load", EffectResult(outcome="success"))
        assert wrong_step.step_outcome == "unknown" and intent.step == "release"

    run(scenario)


# -- the unexpected error path -------------------------------------------------------------------------


def test_an_adapter_that_raises_during_release_is_unknown_and_holds(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, can_query=False)

        async def boom(domain: str, intent: StepIntent) -> EffectResult:
            raise RuntimeError("adapter bug")

        h.adapter.release = boom  # type: ignore[method-assign]
        done = await h.run("park", "k")
        assert (done.state, done.step_outcome, h.gate()) == ("reconciling", "unknown", "reconciling")

    run(scenario)


def test_a_store_that_fails_before_an_effect_changes_nothing_and_the_operation_aborts(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        real = h.store.write_intent

        def break_at_release(op_id: str, intent: StepIntent):
            if intent.step == "release":
                raise sqlite3.OperationalError("disk I/O error")
            return real(op_id, intent)

        h.store.write_intent = break_at_release  # type: ignore[method-assign]
        done = await h.run("park", "k")
        assert not h.steps_called("release"), "no effect without a committed intent"
        assert (done.state, done.step_outcome, h.gate()) == ("aborted", "success", "open"), "only the drain ran"
        assert not h.locks[DOMAIN].locked()

    run(scenario)


def test_a_store_that_cannot_record_anything_holds_the_gate_closed_in_memory(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)

        def dead(*args: Any, **kwargs: Any) -> Any:
            raise sqlite3.OperationalError("disk I/O error")

        h.store.write_intent = dead  # type: ignore[method-assign]
        h.store.finish = dead  # type: ignore[method-assign]
        began = await h.executor.submit(domain=DOMAIN, kind="park", idempotency_key="k")
        await h.executor.wait(began.operation.op_id)
        assert h.gate() == "reconciling", "the durable gate row says closed; this process holds it reconciling"
        assert not h.steps_called("release") and not h.locks[DOMAIN].locked()

    run(scenario)


def test_policy_retry_after_comes_from_the_declared_transition() -> None:
    services = {"svc": {"residency": {"domain": DOMAIN, "transition": {"enabled": True, "retry_after_s": 7, "profile": "p"}}}}
    policy = policy_for("svc", services)
    assert (policy.retry_after_s, policy.profile, policy.lease_only) == (7.0, "p", True)
    assert refusal("reconciling", policy.retry_after_s).headers == {"Retry-After": "7"}
    assert policy_for("other", {"other": {}}).retry_after_s == 30.0


# -- review round: effects are never cancelled, resume re-checks the load, wake errors are 503 ----------------


@ADAPTERS
def test_a_deadline_never_cancels_a_dispatched_effect(tmp_path: Path, adapter: str, capsys: pytest.CaptureFixture[str]) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter, **({"can_query": False} if adapter == "direct" else {"settle_delay": 0.6, "query_wait": 0.05}))
        if adapter == "direct":
            h.sim.delay = 0.6  # the effect runs past the 0.3 s release deadline
        done = await h.run("park", "k")
        assert (done.state, done.step_outcome) == ("reconciling", "unknown"), "the deadline records unknown"
        await asyncio.sleep(0.8)  # the effect finishes by itself
        assert h.sim.cancelled == [], "no effect call was cancelled"
        assert h.sim.offering is None, "the engine finished the release the deadline gave up on"
        assert h.store.get(done.op_id).step_outcome == "unknown", "a late outcome writes nothing (G10)"
        if adapter == "direct":
            assert "late outcome success" in capsys.readouterr().out, "the late outcome is logged"

    run(scenario)


def test_the_fresh_window_comes_from_the_reviewed_profile_never_from_owner_config() -> None:
    from gateway.transition_executor import DEFAULTS, fresh_within

    assert fresh_within({"fresh_within_s": 999.0}) == DEFAULTS["fresh_within_s"], "an owner value is ignored"
    assert fresh_within({"profile": {"fresh_within_s": 9.0}, "fresh_within_s": 999.0}) == 9.0
    assert fresh_within({"profile": {"fresh_within_s": -1}}) == DEFAULTS["fresh_within_s"]


def test_a_stale_window_in_owner_config_does_not_make_old_evidence_fresh(tmp_path: Path) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, policy={"fresh_within_s": 1e9})
        h.sim.arm_evidence("stale", after="release", sticky=True)
        done = await h.run("park", "k")
        assert (done.state, done.step_outcome, done.stock) == ("reconciling", "unknown", None)

    run(scenario)


@ADAPTERS
def test_a_resume_at_open_checks_the_target_again_and_holds_when_the_engine_died(tmp_path: Path, adapter: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path, adapter=adapter)
        op = _begun(h)
        _intent(op.op_id, h, "drain")
        h.store.write_outcome(op.op_id, "drain", EffectResult(outcome="success"))
        _intent(op.op_id, h, "release")
        h.store.write_outcome(op.op_id, "release", EffectResult(outcome="success", attested=True))
        _intent(op.op_id, h, "load")
        h.sim.offering, h.sim.ready, h.sim.stock = "beta", True, 1
        h.store.write_outcome(op.op_id, "load", EffectResult(outcome="success"))
        # The process crashed here. While it was down the engine died.
        h.sim.offering, h.sim.ready, h.sim.stock = None, False, 0
        fresh = h.restart(tmp_path)
        await fresh.executor.reconcile_startup()
        held = await fresh.executor.wait(op.op_id)
        assert (held.state, fresh.gate()) == ("reconciling", "reconciling"), "the old load evidence never opens the gate"
        assert fresh.drain.in_flight(DOMAIN) == 0
        assert not fresh.steps_called("load"), "the recheck only reads"

    run(scenario)


@pytest.mark.parametrize("step_known", [True, False])
def test_a_resume_with_the_target_still_resident_opens_as_before(tmp_path: Path, step_known: bool) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        op = _begun(h)
        _intent(op.op_id, h, "drain")
        h.store.write_outcome(op.op_id, "drain", EffectResult(outcome="success"))
        _intent(op.op_id, h, "release")
        h.store.write_outcome(op.op_id, "release", EffectResult(outcome="success", attested=True))
        _intent(op.op_id, h, "load")
        h.sim.offering, h.sim.ready = "beta", True
        h.store.write_outcome(op.op_id, "load", EffectResult(outcome="success"))
        if step_known:
            _intent(op.op_id, h, "open")
        fresh = h.restart(tmp_path)
        await fresh.executor.reconcile_startup()
        done = await fresh.executor.wait(op.op_id)
        assert (done.state, fresh.gate()) == ("succeeded", "open")

    run(scenario)


@pytest.mark.parametrize("failure", ["no_offering", "store_error"])
def test_a_wake_that_cannot_start_is_a_503_never_a_raw_error(tmp_path: Path, failure: str) -> None:
    async def scenario() -> None:
        h = Harness(tmp_path)
        if failure == "no_offering":
            h.executor._gate_parked = lambda domain: "op-gone"  # type: ignore[method-assign]
        else:
            await h.run("park", "p")

            def broken(**kwargs: Any) -> Any:
                raise sqlite3.OperationalError("database is locked")

            h.store.begin = broken  # type: ignore[method-assign]
        with pytest.raises(TransitionUnavailable) as refused:
            await h.executor.ensure_awake(DOMAIN, wait_s=1.0)
        assert (refused.value.status_code, refused.value.reason) == (503, "wake_unavailable")
        assert refused.value.headers == {"Retry-After": "7"}
        assert not h.locks[DOMAIN].locked()

    run(scenario)

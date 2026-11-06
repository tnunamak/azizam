"""The transition executor wired into the gateway (proxy.py): a request for a parked domain wakes it, an admission
sees "parked", and unfinished operations are settled or held before the first admission."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import proxy
from gateway.transition_gate import GateStore, TransitionUnavailable
from gateway.transition_ops import EffectResult, StepIntent
from gateway.transition_runtime import TransitionRuntime
from tests.test_transition_admin import FAST, FakeEngine, PROFILE, services


@pytest.fixture
def wired(monkeypatch, tmp_path):
    backends = {**(proxy.config.get("backends") or {}), **{k: v for k, v in services().items() if v}}
    monkeypatch.setitem(proxy.config, "backends", backends)
    store = GateStore(tmp_path / "transition.db")
    store.load()
    monkeypatch.setattr(proxy, "TRANSITION_GATES", store)
    monkeypatch.setattr(proxy, "_swap_drain", proxy._new_swap_drain())
    monkeypatch.setattr(proxy, "_strict_service_availability_overrides", lambda: {})
    engine = FakeEngine()
    runtime = TransitionRuntime(gates=lambda: store, services=lambda: proxy.config.get("backends") or {},
                                lock_for=proxy.swap_lock_for, drain=lambda: proxy._swap_drain, protocol_invoke=engine)
    monkeypatch.setattr(proxy, "TRANSITION_RUNTIME", runtime)
    return store, engine, runtime


async def park(runtime: TransitionRuntime, key: str = "park-1") -> str:
    executor = runtime.executor()
    began = await executor.submit(domain="dom", kind="park", idempotency_key=key)
    done = await executor.wait(began.operation.op_id)
    assert done.state == "succeeded"
    return done.op_id


def test_a_request_for_a_parked_domain_wakes_it_before_it_is_admitted(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        park_id = await park(runtime)
        assert engine.resident is None and store.parked("dom") == park_id
        async with proxy._admission_lock("alpha"):
            lease = proxy._swap_drain.lease("alpha")
            assert engine.resident == "alpha", "the wake loaded the offering the park released"
            assert store.parked("dom") is None and store.state("dom") == "open"
            lease.release()
        loads = [call for call in engine.calls if call[0] == "transition.load"]
        async with proxy._admission_lock("beta"):
            pass
        assert [call for call in engine.calls if call[0] == "transition.load"] == loads, "an awake domain is not woken again"

    asyncio.run(scenario())


def test_requests_that_find_the_domain_parked_share_one_wake(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        await park(runtime)

        async def request(service: str) -> None:
            async with proxy._admission_lock(service):
                proxy._swap_drain.lease(service).release()

        await asyncio.gather(*(request(name) for name in ("alpha", "beta", "alpha", "beta")))
        assert len([call for call in engine.calls if call[0] == "transition.load"]) == 1

    asyncio.run(scenario())


def test_a_wake_that_does_not_succeed_refuses_the_request_with_retry_after(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        await park(runtime)
        engine.resident = "other-offering"   # something else is resident after the load: the load is not verified

        original = engine.__call__

        async def refuse_load(service_id: str, op: str, body: Any) -> Any:
            if op == "transition.load":
                engine.calls.append((op, dict(body)))
                from tests.test_transition_admin import Reply
                return Reply(200, {"outcome": "failed"})
            return await original(service_id, op, body)

        runtime._protocol_invoke = refuse_load
        with pytest.raises(TransitionUnavailable) as caught:
            async with proxy._admission_lock("alpha"):
                raise AssertionError("not admitted")
        assert caught.value.retry_after_s == 7 and caught.value.reason.startswith("wake_")
        assert store.state("dom") == "failed_closed"

    asyncio.run(scenario())


def test_a_lease_is_refused_while_the_domain_is_parked(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        await park(runtime)
        with pytest.raises(TransitionUnavailable) as caught:
            proxy._admit_serving_lease("alpha")
        assert caught.value.reason == "parked"

    asyncio.run(scenario())


def test_startup_reconcile_holds_an_unfinished_operation_before_any_admission(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        executor = runtime.executor()
        began = executor.store.begin(domain="dom", kind="park", idempotency_key="crashed", target="alpha",
                                     incarnation="inc-1", config_revision=0)
        op = began.operation
        executor.store.write_intent(op.op_id, StepIntent(step="release", effect_key=f"{op.op_id}:release",
                                                         expected_incarnation="inc-1"))
        # The process "restarted": the gate table says closed; the engine cannot say what became of the release.
        engine.query_outcome = "unknown"
        fresh = GateStore(store.path)
        fresh.load()
        assert fresh.state("dom") == "closed"
        await executor.reconcile_startup()
        held = executor.store.get(op.op_id)
        assert held is not None and held.state == "reconciling" and store.state("dom") == "reconciling"
        with pytest.raises(TransitionUnavailable) as caught:
            proxy._admit_serving_lease("alpha")
        assert caught.value.reason == "gate_reconciling"

    asyncio.run(scenario())


def test_the_profile_domain_only_leases_and_forwards(wired) -> None:
    policy = proxy._transition_policy("alpha")
    assert policy.profile == PROFILE and policy.lease_only and not policy.pass_through
    assert set(FAST) >= {"retry_after_s"}


class RecordingStep:
    """Stands in for a checkpoint step another lane supplies: records what the executor hands a merged step."""

    name = "checkpoint"

    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self.seen: list[Any] = []

    def applies(self, operation: Any, adapter: Any) -> bool:
        return operation.kind == "park"

    async def run(self, ctx: Any) -> str:
        self.seen.append((dict(ctx.policy), len(self.engine.calls)))
        return "continue"


def test_merged_steps_run_in_order_and_read_attested_fields_from_the_reviewed_profile_only(wired) -> None:
    store, engine, _ = wired
    step = RecordingStep(engine)
    runtime = TransitionRuntime(gates=lambda: store, services=lambda: proxy.config.get("backends") or {},
                                lock_for=proxy.swap_lock_for, drain=lambda: proxy._swap_drain, protocol_invoke=engine,
                                extra_steps={"checkpoint": step}, snapshots="store-handle")

    async def scenario() -> None:
        await park(runtime)
        (policy, calls_before), = step.seen
        assert policy["profile"]["id"] == PROFILE and policy["profile"]["attested"] is True
        assert policy["checkpoint_policy"] == "best_effort"
        assert policy["deadlines"]["release"] == 2.0 and policy["retry_after_s"] == 7
        assert not any(call[0] == "transition.release" for call in engine.calls[:calls_before]), "checkpoint runs before release"
        assert any(call[0] == "transition.release" for call in engine.calls[calls_before:])
        assert runtime.executor()._snapshots == "store-handle"

    asyncio.run(scenario())


def test_startup_hooks_run_before_the_executor_recovers_and_a_failing_hook_does_not_stop_it(wired) -> None:
    store, engine, _ = wired
    order: list[str] = []

    def failing() -> None:
        order.append("hook-failed")
        raise RuntimeError("snapshot store unreadable")

    runtime = TransitionRuntime(gates=lambda: store, services=lambda: proxy.config.get("backends") or {},
                                lock_for=proxy.swap_lock_for, drain=lambda: proxy._swap_drain, protocol_invoke=engine,
                                startup=[lambda: order.append("snapshots"), failing])

    async def scenario() -> None:
        executor = runtime.executor()
        original = executor.reconcile_startup

        async def recorded() -> None:
            order.append("executor")
            await original()

        executor.reconcile_startup = recorded  # type: ignore[method-assign]
        await runtime.reconcile_startup()

    asyncio.run(scenario())
    assert order == ["snapshots", "hook-failed", "executor"]


def test_an_owner_cannot_set_the_profile_blocks_the_snapshot_steps_trust(wired) -> None:
    from gateway.transition_gate import forbidden_profile_keys
    for key in ("servable_check", "placement_keys", "failure_semantics"):
        record = {"residency": {"domain": "dom", "transition": {"enabled": True, "profile": PROFILE, key: "x"}}}
        assert forbidden_profile_keys(record) == (key,)


def test_the_gateways_runtime_carries_the_snapshot_steps_and_opens_the_ledger_only_at_startup(wired, monkeypatch, tmp_path) -> None:
    """Building the runtime reads no path. The startup hook opens the ledger beside ``transition.db``. A domain whose
    adapter has no checkpoint (the generic adapter) never reaches the checkpoint or restore step, so a park and a wake
    send exactly the operations they sent before the snapshot steps were wired."""
    _, engine, _ = wired
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", tmp_path / "admin.db")
    runtime = proxy._new_transition_runtime()
    monkeypatch.setattr(runtime, "_protocol_invoke", engine)
    assert not (tmp_path / "transition-snapshots").exists(), "no directory is made when the runtime is built"
    assert runtime._startup and runtime._snapshots.__class__.__name__ == "LazySnapshotStore"

    async def scenario() -> None:
        await runtime.reconcile_startup()
        assert (tmp_path / "transition-snapshots").is_dir()
        steps = runtime.executor()._steps
        assert {"checkpoint", "restore"} <= set(steps)
        assert runtime._snapshots.opened().path == tmp_path / "transition.db"
        for kind, key in (("park", "park-1"), ("wake", "wake-1")):
            executor = runtime.executor()
            began = await executor.submit(domain="dom", kind=kind, idempotency_key=key, target="alpha" if kind == "wake" else None)
            assert (await executor.wait(began.operation.op_id)).state == "succeeded"
        ops = [call[0] for call in engine.calls]
        assert "transition.checkpoint" not in ops and "transition.restore" not in ops
        assert runtime._snapshots.rows("dom") == (), "the generic adapter wrote no snapshot"

    asyncio.run(scenario())

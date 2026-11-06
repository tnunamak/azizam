"""The executor with the checkpoint and restore steps merged into its step table.

The snapshot steps and their fakes come from the checkpoint lane (T1.3); this file runs only where that lane's modules
exist, so it is skipped on a branch that has not merged it.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("gateway.transition_snapshot_steps")
rig_module = pytest.importorskip("tests.support.transition_snapshot_rig")

from gateway.swap_drain import SwapDrain  # noqa: E402
from gateway.transition_ops import Evidence  # noqa: E402
from gateway.transition_executor import Executor, core_steps  # noqa: E402
from gateway.transition_gate import GateStore  # noqa: E402
from gateway.transition_snapshot_steps import snapshot_steps  # noqa: E402
from gateway.transition_store import SqliteOperationStore  # noqa: E402

FAST = {"drain_deadline_s": 1, "idle_poll_s": 0.01, "probe_deadline_s": 1, "release_deadline_s": 2, "load_deadline_s": 2,
        "query_deadline_s": 0.5, "lock_wait_s": 1, "retry_after_s": 7}


def build(tmp_path, shape: str):
    rig = rig_module.Rig(tmp_path, shape)
    resident = rig.adapter.resident

    async def resident_and_ready(domain: str):
        # The load step also requires the target to be ready (design D4 row 5); the rig's fake does not say so.
        evidence = await resident(domain)
        return Evidence(value=evidence.value, observed_at=evidence.observed_at, incarnation=evidence.incarnation,
                        detail={**evidence.detail, "ready": evidence.value is True})

    rig.adapter.resident = resident_and_ready  # type: ignore[method-assign]
    gates = GateStore(rig.db)
    gates.load()
    policy = {**FAST, **rig.policy()}
    executor = Executor(store=SqliteOperationStore(gates), steps={**core_steps(SwapDrain()), **snapshot_steps(rig.snapshots)},
                        adapter_for=lambda domain: rig.adapter, policy_of=lambda domain: policy,
                        lock_for=lambda domain: asyncio.Lock(), snapshots=rig.snapshots)
    return rig, gates, executor


async def run(executor: Executor, **kwargs):
    began = await executor.submit(domain="dom", **kwargs)
    return await executor.wait(began.operation.op_id)


@pytest.mark.parametrize("shape", ["sync", "deferred"])
def test_a_park_saves_before_it_releases_and_a_wake_loads_then_restores(tmp_path, shape: str) -> None:
    rig, gates, executor = build(tmp_path, shape)

    async def scenario() -> None:
        parked = await run(executor, kind="park", idempotency_key="park-1")
        assert parked.state == "succeeded", parked
        calls = [c for c in rig.engine.calls if c in {"checkpoint", "release", "load", "restore"}]
        assert calls == ["checkpoint", "release"], calls
        woken = await run(executor, kind="wake", idempotency_key="wake-1", target="model-a")
        assert woken.state == "succeeded", woken
        calls = [c for c in rig.engine.calls if c in {"checkpoint", "release", "load", "restore"}]
        assert calls == ["checkpoint", "release", "load", "restore"], calls
        assert gates.state("dom") == "open"

    asyncio.run(scenario())

"""T1.2's executor with T1.3's checkpoint and restore steps merged into its step table, on both rig shapes.

The rig's ``sync`` adapter answers each effect itself; its ``deferred`` adapter answers ``unknown`` and reveals the real
answer only through ``query``. Neither this file nor its imports may be skipped: both lanes are on this branch.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.swap_drain import SwapDrain
from gateway.transition_executor import DEFAULT_FRESH_WITHIN_S, Executor, core_steps, fresh_within
from gateway.transition_gate import GateStore
from gateway.transition_ops import Evidence
from gateway.transition_snapshot_steps import DEFAULT_FRESH_WITHIN_S as SNAPSHOT_DEFAULT_FRESH, parse_policy, snapshot_steps
from gateway.transition_store import SqliteOperationStore
from tests.support.transition_snapshot_rig import Rig

FAST = {"drain_deadline_s": 1, "idle_poll_s": 0.01, "probe_deadline_s": 1, "release_deadline_s": 2, "load_deadline_s": 2,
        "query_deadline_s": 0.5, "lock_wait_s": 1, "retry_after_s": 7}
CHECKPOINT_STEPS = {"checkpoint", "release", "load", "restore"}


def build(tmp_path, shape: str, **policy_kwargs):
    rig = Rig(tmp_path, shape)
    gates = GateStore(rig.db)
    gates.load()
    policy = {**FAST, **rig.policy(**policy_kwargs)}
    executor = Executor(store=SqliteOperationStore(gates), steps={**core_steps(SwapDrain()), **snapshot_steps(rig.snapshots)},
                        adapter_for=lambda domain: rig.adapter, policy_of=lambda domain: policy,
                        lock_for=lambda domain: asyncio.Lock(), snapshots=rig.snapshots)
    return rig, gates, executor


async def run(executor: Executor, **kwargs):
    began = await executor.submit(domain="dom", **kwargs)
    return await executor.wait(began.operation.op_id)


def effects(rig: Rig) -> list[str]:
    return [call for call in rig.engine.calls if call in CHECKPOINT_STEPS]


@pytest.mark.parametrize("shape", ["sync", "deferred"])
def test_a_park_saves_before_it_releases_and_a_wake_loads_then_restores(tmp_path, shape: str) -> None:
    rig, gates, executor = build(tmp_path, shape)

    async def scenario() -> None:
        parked = await run(executor, kind="park", idempotency_key="park-1")
        assert parked.state == "succeeded", parked
        assert effects(rig) == ["checkpoint", "release"]
        saved = rig.snapshots.pointer("dom")
        assert saved is not None and rig.snapshots.get(saved).state == "published"
        assert parked.snapshot_handle == saved
        woken = await run(executor, kind="wake", idempotency_key="wake-1", target="model-a")
        assert woken.state == "succeeded", woken
        assert effects(rig) == ["checkpoint", "release", "load", "restore"]
        assert rig.engine.restored == saved, "the wake restored the snapshot the park saved"
        assert gates.state("dom") == "open"

    asyncio.run(scenario())


@pytest.mark.parametrize("shape", ["sync", "deferred"])
def test_a_wake_after_a_failed_checkpoint_loads_cold(tmp_path, shape: str) -> None:
    rig, gates, executor = build(tmp_path, shape)
    rig.engine.script("checkpoint", "no_change")

    async def scenario() -> None:
        parked = await run(executor, kind="park", idempotency_key="park-1")
        assert parked.state == "succeeded", parked
        assert rig.snapshots.pointer("dom") is None
        woken = await run(executor, kind="wake", idempotency_key="wake-1", target="model-a")
        assert woken.state == "succeeded", woken
        assert effects(rig) == ["checkpoint", "release", "load"], "nothing was restored"
        assert rig.engine.restored is None

    asyncio.run(scenario())


def test_the_two_lanes_agree_on_the_default_freshness() -> None:
    assert DEFAULT_FRESH_WITHIN_S == SNAPSHOT_DEFAULT_FRESH


@pytest.mark.parametrize("policy, expected", [
    ({"profile": {"fresh_within_s": 7}}, 7.0),
    ({"fresh_within_s": 99, "profile": {"fresh_within_s": 7}}, 7.0),
    ({"fresh_within_s": 99}, DEFAULT_FRESH_WITHIN_S),
    ({"profile": {"fresh_within_s": 0}}, DEFAULT_FRESH_WITHIN_S),
    ({"profile": "not a mapping", "fresh_within_s": 99}, DEFAULT_FRESH_WITHIN_S),
])
def test_both_lanes_read_fresh_within_only_from_the_reviewed_profile(policy, expected: float) -> None:
    assert fresh_within(policy) == expected
    assert parse_policy(policy).fresh_within_s == expected


@pytest.mark.parametrize("shape", ["sync", "deferred"])
def test_the_rigs_resident_evidence_carries_readiness(tmp_path, shape: str) -> None:
    rig = Rig(tmp_path, shape)

    async def scenario() -> None:
        evidence = await rig.adapter.resident("dom")
        assert isinstance(evidence, Evidence) and evidence.detail["ready"] is True
        rig.engine.ready = False
        assert (await rig.adapter.resident("dom")).detail["ready"] is False
        rig.engine.resident = False
        assert (await rig.adapter.resident("dom")).detail["ready"] is False

    asyncio.run(scenario())

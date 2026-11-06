"""The checkpoint and restore steps (gateway/transition_snapshot_steps.py), run in STEP_ORDER by a test-only runner over
two differently shaped fake adapters: ``sync`` answers when the effect finishes, ``deferred`` answers ``unknown`` and
reveals the result through ``query``. Oracle items 8 (restore part) and 9; decisions G1 and G7."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from gateway.transition_ops import EffectResult
from gateway.transition_snapshot_steps import SnapshotPolicy, parse_policy, snapshot_steps
from gateway.transition_snapshots import SqliteSnapshotStore
from tests.support.transition_snapshot_rig import PAYLOAD, Rig
from tests.support.transition_step_runner import DirectStep, FakeOperationStore, run_steps

D = "d1"


@pytest.fixture(params=["sync", "deferred"])
def rig(request, tmp_path) -> Rig:
    r = Rig(tmp_path, request.param)
    r.ops = FakeOperationStore()
    return r


def build(rig: Rig, on_release=None) -> dict:
    steps = snapshot_steps(rig.snapshots)

    async def release(ctx, intent):
        return await rig.adapter.release(ctx.operation.domain, intent)

    async def load(ctx, intent):
        return await rig.adapter.load(ctx.operation.domain, ctx.operation.target or "model-a", intent)

    steps["release"] = DirectStep("release", release, ("park", "select"), on_release)
    steps["load"] = DirectStep("load", load, ("select", "wake"))
    return steps


def go(rig: Rig, kind: str, policy: dict, *, steps=None, target=None, settle: float = 0.0):
    """Run one operation. ``settle`` keeps the loop alive afterwards, so a late effect can finish inside it."""

    async def scenario():
        result = await run_steps(steps or build(rig), store=rig.ops, adapter=rig.adapter, snapshots=rig.snapshots,
                                 policy=policy, kind=kind, domain=D, target=target)
        if settle:
            await asyncio.sleep(settle)
        return result

    return asyncio.run(asyncio.wait_for(scenario(), 10))


def seed(rig: Rig, handle: str = "seed.snap", compat=None, body: bytes = PAYLOAD) -> str:
    """A published snapshot made earlier, outside the steps."""
    path = rig.files.path_for(D, handle)
    path.write_bytes(body)
    path.chmod(0o600)
    rig.snapshots.pending(domain=D, op_id="op-seed", handle=handle, compatibility=compat or dict(rig.engine.compat),
                          bytes=len(body))
    rig.snapshots.publish(handle)
    return handle


def states(rig: Rig) -> list[str]:
    return [state for _, _, state in rig.snapshots.rows(D)]


# -- policy: attestation comes from the reviewed profile only -----------------


def test_attestation_is_read_from_the_reviewed_profile_only() -> None:
    owner_only = parse_policy({"failure_semantics": "no_change", "owner": {"failure_semantics": "no_change"},
                               "restore": {"failure_semantics": "no_change", "reset": {"failure_semantics": "no_change"}},
                               "checkpoint": {"failure_semantics": "no_change"}, "placement_keys": ["placement"]})
    assert owner_only == SnapshotPolicy(required=False, deadlines={})
    reviewed = parse_policy({"profile": {"checkpoint": {"failure_semantics": "no_change"}}})
    assert reviewed.checkpoint_declared and reviewed.checkpoint_no_change
    assert parse_policy({"profile": {"checkpoint": {"failure_semantics": "bogus"}}}).checkpoint_no_change is False


def test_an_unknown_checkpoint_policy_counts_as_required() -> None:
    assert parse_policy({"checkpoint_policy": "whatever"}).required
    assert not parse_policy({}).required


def test_owner_config_cannot_raise_an_unattested_failure_to_no_change(rig) -> None:
    rig.engine.script("checkpoint", "no_change")
    policy = rig.policy(checkpoint={"failure_semantics": "unknown"})
    policy["failure_semantics"] = "no_change"
    policy["owner"] = {"checkpoint": {"failure_semantics": "no_change"}}
    result = go(rig, "park", policy)
    assert result.verdict == "reconcile" and result.stopped_at == "checkpoint"


# -- checkpoint: applicability -----------------------------------------------


def test_checkpoint_does_not_run_when_the_profile_does_not_declare_it(rig) -> None:
    policy = rig.policy()
    del policy["profile"]["checkpoint"]
    result = go(rig, "park", policy)
    assert result.verdict == "continue" and "checkpoint" not in rig.engine.calls
    assert rig.ops.outcomes and all(step != "checkpoint" for _, step, _ in rig.ops.outcomes)


def test_checkpoint_and_restore_are_skipped_for_an_adapter_that_cannot_checkpoint(rig) -> None:
    rig.adapter.supports_checkpoint = False
    result = go(rig, "park", rig.policy())
    assert result.executed == ["release"]
    result = go(rig, "wake", rig.policy())
    assert result.executed == ["load"]


def test_checkpoint_runs_for_park_and_select_and_restore_for_wake_and_select(rig) -> None:
    steps = snapshot_steps(rig.snapshots)
    from gateway.transition_ops import Operation

    def op(kind):
        return Operation(op_id="x", domain=D, kind=kind, idempotency_key="k", incarnation=None, config_revision=1,
                         state="running", step=None)

    assert [steps["checkpoint"].applies(op(k), rig.adapter) for k in ("park", "select", "wake")] == [True, True, False]
    assert [steps["restore"].applies(op(k), rig.adapter) for k in ("park", "select", "wake")] == [False, True, True]


def test_checkpoint_is_skipped_when_the_source_is_not_resident(rig) -> None:
    seed(rig)
    rig.engine.resident = False
    result = go(rig, "park", rig.policy())
    assert result.verdict == "continue" and "checkpoint" not in rig.engine.calls
    assert states(rig) == ["published"], "a snapshot made earlier stays the one to restore"


# -- checkpoint: success -----------------------------------------------------


def test_a_checkpoint_publishes_a_snapshot_before_release(rig) -> None:
    seen = {}

    def at_release() -> None:
        seen["states"] = states(rig)
        seen["pointer"] = rig.snapshots.pointer(D)

    result = go(rig, "park", rig.policy(), steps=build(rig, at_release))
    assert result.verdict == "continue" and result.executed == ["checkpoint", "release"]
    assert seen["states"] == ["published"] and seen["pointer"] == "op-1-checkpoint.snap"
    op = rig.ops.get("op-1")
    assert op.snapshot_handle == "op-1-checkpoint.snap" and op.step_outcome == "success"
    assert rig.engine.calls.index("checkpoint") < rig.engine.calls.index("release")
    assert rig.snapshots.latest_published(D, rig.engine.compat).bytes == len(PAYLOAD)


def test_a_new_checkpoint_supersedes_the_old_snapshot_and_deletes_its_file(rig) -> None:
    seed(rig)
    go(rig, "park", rig.policy())
    assert rig.snapshots.pointer(D) == "op-1-checkpoint.snap"
    assert states(rig) == ["deleted", "published"]
    assert not rig.files.path_for(D, "seed.snap").exists()


# -- checkpoint: failures (D4 row 3) -----------------------------------------


def test_best_effort_verified_no_change_skips_logs_and_records_a_cold_next_start(rig, capsys) -> None:
    seed(rig)
    rig.engine.script("checkpoint", "no_change")
    seen = {}
    result = go(rig, "park", rig.policy(), steps=build(rig, lambda: seen.update(states=states(rig))))
    assert result.verdict == "continue" and result.executed == ["checkpoint", "release"]
    assert "release" in rig.engine.calls, "release goes ahead"
    assert rig.snapshots.pointer(D) is None and rig.snapshots.latest_published(D, rig.engine.compat) is None
    assert any("cold_next_start" in (r.evidence_ref or "") for _, step, r in rig.ops.outcomes if step == "checkpoint")
    assert "next start is cold" in capsys.readouterr().out
    assert "published" not in seen["states"]
    # the next wake starts cold: nothing is restored
    again = go(rig, "wake", rig.policy())
    assert again.verdict == "continue" and "restore" not in rig.engine.calls


def test_a_no_change_the_profile_does_not_attest_is_unknown_and_stays_closed(rig) -> None:
    rig.engine.script("checkpoint", "no_change")
    result = go(rig, "park", rig.policy(checkpoint={"failure_semantics": "unknown"}))
    assert (result.verdict, result.stopped_at, result.gate) == ("reconcile", "checkpoint", "reconciling")
    assert "release" not in rig.engine.calls


@pytest.mark.parametrize("failure", ["unknown", "partial"])
@pytest.mark.parametrize("required", [False, True])
def test_an_unknown_or_partial_checkpoint_stays_closed_and_reconciles(rig, failure, required) -> None:
    rig.engine.script("checkpoint", failure)
    result = go(rig, "park", rig.policy(required=required))
    assert (result.verdict, result.gate) == ("reconcile", "reconciling")
    assert "release" not in rig.engine.calls
    assert "pending" not in states(rig) and rig.snapshots.pointer(D) is None


def test_a_required_checkpoint_that_declined_reopens_after_the_old_state_is_confirmed_servable(rig) -> None:
    rig.engine.script("checkpoint", "no_change")
    result = go(rig, "park", rig.policy(required=True))
    assert (result.verdict, result.gate, result.operation.state) == ("reopen", "open", "aborted")
    assert "release" not in rig.engine.calls
    assert rig.engine.calls.count("resident") == 2, "the source check, then the re-check"


def test_a_required_checkpoint_does_not_reopen_when_the_old_state_is_not_servable(rig) -> None:
    rig.engine.script("checkpoint", "no_change")
    rig.engine.observations["servable"] = False
    result = go(rig, "park", rig.policy(required=True, extra_profile={"servable_check": "servable"}))
    assert (result.verdict, result.gate) == ("fail_closed", "failed_closed")


@pytest.mark.parametrize("stale", [False, True])
def test_g7_the_recheck_uses_the_profiles_servable_observation_and_needs_fresh_evidence(rig, stale) -> None:
    rig.engine.script("checkpoint", "no_change")
    rig.engine.observations["servable"] = True
    rig.engine.observations["servable:stale"] = stale
    result = go(rig, "park", rig.policy(required=True, extra_profile={"servable_check": "servable"}))
    assert "observe:servable" in rig.engine.calls
    assert rig.engine.calls.count("resident") == 1, "the resident observation is not the re-check when one is declared"
    assert result.verdict == ("reconcile" if stale else "reopen")


def test_g7_the_default_recheck_is_the_sources_resident_observation(rig) -> None:
    rig.engine.script("checkpoint", "no_change")
    rig.engine.observations["servable"] = False  # declared nowhere, so ignored
    result = go(rig, "park", rig.policy(required=True))
    assert result.verdict == "reopen" and not any(c.startswith("observe:") for c in rig.engine.calls)


@pytest.mark.parametrize("outcome", ["half_written", "no_file"])
def test_a_half_written_or_missing_snapshot_is_never_published(rig, outcome) -> None:
    seed(rig)
    rig.engine.script("checkpoint", outcome)
    result = go(rig, "park", rig.policy())
    assert result.verdict == "continue"
    assert rig.snapshots.pointer(D) is None and "published" not in states(rig) and "pending" not in states(rig)
    assert not rig.files.names(D), "the unusable file is deleted through the ledger's ownership"


@pytest.mark.parametrize("outcome", ["half_written", "no_file"])
def test_a_required_checkpoint_with_an_unusable_snapshot_reopens(rig, outcome) -> None:
    rig.engine.script("checkpoint", outcome)
    result = go(rig, "park", rig.policy(required=True))
    assert (result.verdict, result.gate) == ("reopen", "open") and "release" not in rig.engine.calls


def test_a_snapshot_with_no_compatibility_token_is_not_published(rig) -> None:
    rig.engine.compat = {}
    result = go(rig, "park", rig.policy())
    assert result.verdict == "continue" and rig.snapshots.pointer(D) is None and "published" not in states(rig)


def test_an_out_of_storage_domain_refuses_a_new_checkpoint(tmp_path) -> None:
    small = Rig(tmp_path, "sync", max_bytes=len(PAYLOAD), max_count=4)
    small.ops = FakeOperationStore()
    orphan = small.files.path_for(D, "orphan.bin")
    orphan.write_bytes(b"x" * len(PAYLOAD))
    orphan.chmod(0o600)
    best_effort = go(small, "park", small.policy())
    assert best_effort.verdict == "continue" and "checkpoint" not in small.engine.calls
    small.engine.resident, small.engine.offering = True, "model-a"  # the release above parked it
    required = go(small, "park", small.policy(required=True))
    assert required.verdict == "reopen" and "checkpoint" not in small.engine.calls
    assert orphan.exists(), "an unowned file is never deleted to make room"


def test_a_slow_checkpoint_past_its_deadline_is_unknown_and_its_late_file_is_never_published(rig) -> None:
    rig.engine.script("checkpoint", "success", delay=0.3 if rig.shape == "sync" else 0.0)
    result = go(rig, "park", rig.policy(deadlines={"checkpoint": 0.05}, query=False), settle=0.4)
    assert (result.verdict, result.gate) == ("reconcile", "reconciling")
    assert rig.snapshots.orphans(D) == ("op-1-checkpoint.snap",), "the file arrived after the step gave up"
    assert rig.snapshots.pointer(D) is None and rig.snapshots.latest_published(D, rig.engine.compat) is None


def test_a_checkpoint_that_finishes_after_its_deadline_is_resolved_by_the_declared_query(rig) -> None:
    rig.engine.script("checkpoint", "success", delay=0.3 if rig.shape == "sync" else 0.0)
    result = go(rig, "park", rig.policy(deadlines={"checkpoint": 0.05}, query=True), settle=0.4)
    if rig.shape == "sync":
        # the query ran at the deadline, before the effect finished: still unknown, so the gate stays closed
        assert result.verdict == "reconcile" and rig.snapshots.pointer(D) is None
    else:
        assert result.verdict == "continue" and rig.snapshots.pointer(D) == "op-1-checkpoint.snap"


def test_pending_snapshots_of_a_failed_checkpoint_are_abandoned_before_release(rig) -> None:
    seen = {}
    rig.engine.script("checkpoint", "no_change")
    go(rig, "park", rig.policy(), steps=build(rig, lambda: seen.update(states=states(rig))))
    assert "pending" not in seen["states"]


# -- restore ------------------------------------------------------------------


def parked_then_wake(rig: Rig, policy=None):
    parked = go(rig, "park", rig.policy())
    assert parked.verdict == "continue" and rig.snapshots.pointer(D)
    return go(rig, "wake", policy or rig.policy())


def test_park_then_wake_restores_the_published_snapshot(rig) -> None:
    result = parked_then_wake(rig)
    assert result.verdict == "continue" and result.executed == ["load", "restore"]
    assert rig.engine.restored == "op-1-checkpoint.snap"
    assert rig.engine.calls.index("load") < rig.engine.calls.index("restore")
    assert rig.ops.get("op-2").step_outcome == "success"
    assert "reset" not in rig.engine.calls


def test_restore_is_skipped_when_no_snapshot_is_published(rig) -> None:
    result = go(rig, "wake", rig.policy())
    assert result.verdict == "continue" and "restore" not in rig.engine.calls and rig.engine.restored is None


@pytest.mark.parametrize("key,value", [("artifact", "a2"), ("tokenizer", "t2"), ("engine", "e2"),
                                       ("cache_format", "c2"), ("defaults", "d2"), ("extra", "x")])
def test_a_changed_identity_serves_cold_without_a_restore_attempt(rig, key, value) -> None:
    seed(rig)
    rig.engine.compat = {**rig.engine.compat, key: value}
    result = go(rig, "wake", rig.policy())
    assert result.verdict == "continue" and "restore" not in rig.engine.calls and rig.engine.restored is None


def test_a_missing_key_serves_cold(rig) -> None:
    seed(rig)
    rig.engine.compat = {k: v for k, v in rig.engine.compat.items() if k != "tokenizer"}
    assert go(rig, "wake", rig.policy()).verdict == "continue" and "restore" not in rig.engine.calls


def test_a_reviewed_placement_difference_still_restores(rig) -> None:
    seed(rig)
    rig.engine.compat = {**rig.engine.compat, "placement": "gpu7"}
    go(rig, "wake", rig.policy())
    assert rig.engine.restored == "seed.snap"


def test_a_placement_difference_not_in_the_reviewed_list_serves_cold(rig) -> None:
    seed(rig)
    rig.engine.compat = {**rig.engine.compat, "placement": "gpu7"}
    policy = rig.policy(extra_profile={"placement_keys": []})
    assert go(rig, "wake", policy).verdict == "continue" and rig.engine.restored is None


def test_an_unknown_token_at_restore_serves_cold(rig) -> None:
    seed(rig)
    rig.engine.compat = {}
    assert go(rig, "wake", rig.policy()).verdict == "continue" and rig.engine.restored is None


def test_the_wake_reconciles_pending_snapshots_and_restores_only_a_published_one(rig) -> None:
    seed(rig, "good.snap")
    path = rig.files.path_for(D, "half.snap")
    path.write_bytes(PAYLOAD)
    path.chmod(0o600)
    rig.snapshots.pending(domain=D, op_id="op-crashed", handle="half.snap", compatibility=dict(rig.engine.compat),
                          bytes=len(PAYLOAD))
    result = go(rig, "wake", rig.policy())
    assert result.verdict == "continue" and rig.engine.restored == "good.snap"
    assert "pending" not in states(rig) and not rig.files.path_for(D, "half.snap").exists()


def test_a_pending_snapshot_alone_is_never_restored(rig) -> None:
    path = rig.files.path_for(D, "half.snap")
    path.write_bytes(PAYLOAD)
    path.chmod(0o600)
    rig.snapshots.pending(domain=D, op_id="op-crashed", handle="half.snap", compatibility=dict(rig.engine.compat),
                          bytes=len(PAYLOAD))
    result = go(rig, "wake", rig.policy())
    assert result.verdict == "continue" and "restore" not in rig.engine.calls


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_a_damaged_published_file_serves_cold(rig, damage) -> None:
    handle = seed(rig)
    path = rig.files.path_for(D, handle)
    if damage == "corrupt":
        path.write_bytes(PAYLOAD[:5])
    else:
        path.unlink()
    result = go(rig, "wake", rig.policy())
    assert result.verdict == "continue" and "restore" not in rig.engine.calls
    assert rig.snapshots.pointer(D) is None


# -- restore: failures (D4 row 6) ---------------------------------------------


def test_restore_no_change_serves_cold_without_a_reset(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "no_change")
    result = go(rig, "wake", rig.policy())
    assert result.verdict == "continue" and "reset" not in rig.engine.calls and rig.engine.restored is None
    assert rig.snapshots.pointer(D) is None, "a snapshot that failed to restore is not offered again"


def test_a_partial_restore_runs_the_reset_verifies_a_clean_target_and_serves_cold(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "partial")
    policy = rig.policy(restore={"reset": {"failure_semantics": "unknown", "query": rig.shape == "deferred",
                                           "verify": "clean"}})
    result = go(rig, "wake", policy)
    assert result.verdict == "continue" and "reset" in rig.engine.calls and not rig.engine.dirty
    assert "observe:clean" in rig.engine.calls
    assert rig.engine.calls.index("restore") < rig.engine.calls.index("reset") < rig.engine.calls.index("observe:clean")


def test_a_failed_restore_with_no_reset_declared_stays_closed(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "partial")
    policy = rig.policy()
    del policy["profile"]["restore"]["reset"]
    result = go(rig, "wake", policy)
    assert (result.verdict, result.gate) == ("fail_closed", "failed_closed") and "reset" not in rig.engine.calls


def test_a_restore_with_unknown_outcome_stays_closed_and_runs_no_reset(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "unknown")
    result = go(rig, "wake", rig.policy())
    assert (result.verdict, result.gate) == ("reconcile", "reconciling") and "reset" not in rig.engine.calls


def test_a_no_change_the_profile_does_not_attest_takes_the_reset_path(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "no_change")
    result = go(rig, "wake", rig.policy(restore={"failure_semantics": "unknown"}))
    assert "reset" in rig.engine.calls and result.verdict == "continue"


def test_g1_a_failed_reset_keeps_the_gate_closed_by_default(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "partial")
    rig.engine.script("reset", "no_change")
    result = go(rig, "wake", rig.policy())  # reset.failure_semantics defaults to unknown
    assert (result.verdict, result.gate) == ("fail_closed", "failed_closed")
    assert "observe:clean" not in rig.engine.calls and rig.engine.calls.count("resident") == 0


def test_g1_a_failed_reset_that_is_known_to_change_nothing_may_serve_cold_once_the_target_verifies_clean(rig) -> None:
    seed(rig)
    # the restore failed but is not attested, and left nothing dirty; the reset then declined
    rig.engine.script("restore", "no_change")
    rig.engine.script("reset", "no_change")
    policy = rig.policy(restore={"failure_semantics": "unknown",
                                 "reset": {"failure_semantics": "no_change", "query": rig.shape == "deferred",
                                           "verify": "clean"}})
    result = go(rig, "wake", policy)
    assert result.verdict == "continue" and "observe:clean" in rig.engine.calls


def test_g1_a_failed_reset_declared_no_change_still_closes_when_the_target_is_not_clean(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "partial")  # leaves the engine dirty
    rig.engine.script("reset", "no_change")
    policy = rig.policy(restore={"reset": {"failure_semantics": "no_change", "query": rig.shape == "deferred",
                                           "verify": "clean"}})
    result = go(rig, "wake", policy)
    assert (result.verdict, result.gate) == ("fail_closed", "failed_closed") and rig.engine.dirty


@pytest.mark.parametrize("script", ["partial", "unknown"])
def test_a_reset_that_fails_or_is_unknown_stays_closed(rig, script) -> None:
    seed(rig)
    rig.engine.script("restore", "partial")
    rig.engine.script("reset", script)
    result = go(rig, "wake", rig.policy())
    assert result.verdict == ("reconcile" if script == "unknown" else "fail_closed")
    assert result.gate in ("reconciling", "failed_closed")


def test_a_reset_whose_target_cannot_be_verified_clean_stays_closed(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "partial")
    unclean = rig.policy(restore={"reset": {"failure_semantics": "unknown", "query": rig.shape == "deferred",
                                            "verify": "clean"}})
    rig.engine.observations["clean"] = False
    assert go(rig, "wake", unclean).verdict == "fail_closed"
    rig.engine.observations["clean"] = True
    rig.engine.observations["clean:stale"] = True
    rig.engine.dirty = False
    rig.snapshots.retire_published(D)
    seed(rig, "third.snap")
    rig.engine.script("restore", "partial")
    assert go(rig, "wake", unclean).verdict == "reconcile"


def test_a_restore_that_succeeds_after_its_timeout_is_not_served_cold_over(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "success", delay=0.2 if rig.shape == "sync" else 0.0)
    result = go(rig, "wake", rig.policy(deadlines={"restore": 0.05, "query": 1}, query=True), settle=0.3)
    assert "reset" not in rig.engine.calls, "an unknown outcome never runs the reset"
    assert rig.engine.restored == "seed.snap", "the late restore completed"
    if rig.shape == "sync":
        assert (result.verdict, result.gate) == ("reconcile", "reconciling"), "the query ran before it finished"
    else:
        assert result.verdict == "continue", "the declared query revealed the success"


def test_a_restore_that_succeeds_after_its_timeout_with_no_query_stays_closed(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "success", delay=0.2)
    policy = rig.policy(deadlines={"restore": 0.05}, query=False)
    result = go(rig, "wake", policy, settle=0.3)
    assert (result.verdict, result.gate) == ("reconcile", "reconciling") and "reset" not in rig.engine.calls


def test_the_restored_snapshot_is_pinned_while_it_restores_and_released_after(rig) -> None:
    handle = seed(rig)
    seen = {}
    original = rig.adapter.restore

    async def restore(domain, h, intent):
        rig.snapshots.retire_published(D)  # the pointer moves away mid-restore
        seen["collected"] = rig.snapshots.collect(D)
        seen["exists"] = rig.files.path_for(D, h).exists()
        return await original(domain, h, intent)

    rig.adapter.restore = restore
    go(rig, "wake", rig.policy())
    assert seen == {"collected": (), "exists": True}, "a snapshot being restored is never deleted"
    assert rig.snapshots.collect(D) == (handle,), "and it is deletable again afterwards"


def test_a_restore_past_its_deadline_keeps_its_pin_until_the_late_restore_ends(rig) -> None:
    """The step gave up and returned, but the engine is still reading the file: nothing may delete it yet. The pin is
    released by the late restore's own completion."""
    handle = seed(rig)
    original = rig.adapter.restore
    unpinned = threading.Event()
    real_unpin = rig.snapshots.unpin

    def unpin(h, domain=None):
        result = real_unpin(h, domain)
        unpinned.set()
        return result

    rig.snapshots.unpin = unpin  # type: ignore[method-assign]

    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()

        async def restore(domain, h, intent):
            started.set()
            await finish.wait()  # still running when the deadline passes
            return await original(domain, h, intent)

        rig.adapter.restore = restore  # type: ignore[method-assign]
        result = await run_steps(build(rig), store=rig.ops, adapter=rig.adapter, snapshots=rig.snapshots,
                                 policy=rig.policy(deadlines={"restore": 0.05, "query": 1}, query=False), kind="wake",
                                 domain=D, target="model-a")
        assert started.is_set() and (result.verdict, result.gate) == ("reconcile", "reconciling")
        assert not unpinned.is_set(), "the step returned, the late restore is still running"
        rig.snapshots.retire_published(D)  # the pointer moves away: the row is abandoned and collectable but for its pin
        assert rig.snapshots.collect(D) == () and rig.files.path_for(D, handle).exists()
        finish.set()
        await asyncio.wait_for(asyncio.to_thread(unpinned.wait, 5), 10)
        assert rig.snapshots.collect(D) == (handle,), "deletable once the late restore ended"

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_an_adapter_that_answers_every_checkpoint_with_one_handle_fails_its_second_checkpoint(rig) -> None:
    async def fixed(domain, intent):
        path = rig.files.path_for(domain, "fixed.snap")
        path.write_bytes(PAYLOAD)
        path.chmod(0o600)
        return EffectResult(outcome="success", handle="fixed.snap", bytes=len(PAYLOAD), evidence_ref="saved")

    rig.adapter.checkpoint = fixed  # type: ignore[method-assign]
    policy = rig.policy(query=False)
    first = go(rig, "park", policy)
    assert first.verdict == "continue" and rig.snapshots.pointer(D) == "fixed.snap"
    second = go(rig, "park", policy)
    print("OUTCOMES", [(o, s_, r.outcome, r.evidence_ref) for o, s_, r in rig.ops.outcomes]); print(rig.snapshots.rows(D))
    outcome = next(r for _, step, r in rig.ops.outcomes if step == "checkpoint" and r.evidence_ref.startswith("checkpoint:snapshot_not_published"))
    assert "earlier snapshot of this domain" in outcome.evidence_ref or "live snapshot" in outcome.evidence_ref
    assert rig.snapshots.pointer(D) is None, "no snapshot is offered; the next start is cold"
    assert second.verdict == "continue" and second.gate == "open", "best effort: the park went on without a snapshot"
    required = go(rig, "park", rig.policy(query=False, required=True))
    assert required.verdict in ("reopen", "fail_closed", "reconcile"), "required: the park does not go on"


def test_restore_attestation_in_owner_config_is_ignored(rig) -> None:
    seed(rig)
    rig.engine.script("restore", "no_change")
    policy = rig.policy(restore={"failure_semantics": "unknown"})
    policy["owner"] = {"restore": {"failure_semantics": "no_change"}}
    go(rig, "wake", policy)
    assert "reset" in rig.engine.calls, "an owner-supplied no_change did not attest the restore"


# -- crashes around the pointer, through the steps ----------------------------


class Crash(BaseException):
    pass


@pytest.mark.parametrize("point,expected", [("publish:verified", "seed.snap"), ("publish:in_transaction", "seed.snap"),
                                            ("publish:committed", "op-1-checkpoint.snap")])
def test_a_crash_during_checkpoint_publish_restores_whichever_snapshot_the_pointer_names(rig, point, expected) -> None:
    seed(rig)

    def hook(name: str) -> None:
        if name == point:
            raise Crash(name)

    crashing = SqliteSnapshotStore(rig.db, rig.files, rig.budget, crash_hook=hook)
    rig.snapshots = crashing
    with pytest.raises(Crash):
        go(rig, "park", rig.policy())
    rig.reopen().startup_reconcile()  # the gateway restarts
    rig.ops = FakeOperationStore()
    result = go(rig, "wake", rig.policy())
    assert result.verdict == "continue" and rig.engine.restored == expected
    assert "pending" not in states(rig)

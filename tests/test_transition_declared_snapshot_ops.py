"""The declared snapshot operations (T1.3): profile validation, the adapter's checkpoint, restore, reset,
compatibility and observe, and one end-to-end park and wake through the real executor and snapshot store."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

from gateway import transition_profiles
from gateway.swap_drain import SwapDrain
from gateway.transition_adapter import DeclaredOperationAdapter
from gateway.transition_gate import GateStore
from gateway.transition_ops import StepIntent
from gateway.transition_profiles import Profile, ProfileError, parse_profile
from gateway.transition_runtime import TransitionRuntime
from gateway.transition_snapshot_steps import snapshot_steps
from gateway.transition_snapshots import (
    LazySnapshotStore,
    PrivateSnapshotDir,
    SnapshotBudget,
    SqliteSnapshotStore,
)
from tests.test_transition_admin import FAST, Reply

SNAPSHOT_PROFILE = "declared-snapshot-ops-v1"
HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def snapshot_services() -> dict[str, Any]:
    def record() -> dict[str, Any]:
        return {"residency": {"domain": "dom", "transition": {"enabled": True, "profile": SNAPSHOT_PROFILE, **FAST}}}
    return {"alpha": record(), "beta": record(), "other": {}}


def snapshot_operations(**extra: str) -> dict[str, str]:
    operations = {"idle": "transition.idle", "resident": "transition.resident", "release": "transition.release",
                  "load": "transition.load", "query": "transition.query"}
    operations.update(extra)
    return operations


def raw_profile(**overrides: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {"profile_version": 1, "id": "bad", "attested": False,
                           "operations": snapshot_operations()}
    raw.update(overrides)
    return raw


def snapshot_profile(**overrides: Any) -> Profile:
    raw: dict[str, Any] = {"profile_version": 1, "id": "snapshot-test", "attested": True,
                           "operations": snapshot_operations(checkpoint="transition.checkpoint",
                                                             restore="transition.restore",
                                                             reset="transition.reset",
                                                             compatibility="transition.compatibility"),
                           "observations": {"clean": "transition.clean"}}
    raw.update(overrides)
    return parse_profile(raw, source="snapshot-test.yml")


def intent(step: str = "checkpoint") -> StepIntent:
    return StepIntent(step=step, effect_key=f"op-1:{step}", expected_incarnation="inc-1")  # type: ignore[arg-type]


class FakeInvoke:
    """Answers adapter calls from a table; records every call."""

    def __init__(self, replies: Mapping[str, Any]) -> None:
        self.replies = dict(replies)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, domain: str, op: str, body: Mapping[str, Any]) -> Reply:
        self.calls.append((op, dict(body)))
        reply = self.replies.get(op)
        if isinstance(reply, Exception):
            raise reply
        assert reply is not None, op
        return reply


def adapter_for(profile: Profile, invoke: FakeInvoke, tmp_path: Path, *, with_path: bool = True
                ) -> DeclaredOperationAdapter:
    snapshot_path = (lambda domain, handle: tmp_path / handle) if with_path else None
    return DeclaredOperationAdapter(profile, invoke, snapshot_path=snapshot_path)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- profile validation ---------------------------------------------------------------------------------


def test_checkpoint_and_restore_are_declared_together() -> None:
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(operations=snapshot_operations(checkpoint="transition.checkpoint",
                                                                 compatibility="transition.compatibility")),
                      source="bad.yml")
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(operations=snapshot_operations(restore="transition.restore")),
                      source="bad.yml")
    parse_profile(raw_profile(operations=snapshot_operations(checkpoint="transition.checkpoint",
                                                             restore="transition.restore",
                                                             compatibility="transition.compatibility")),
                  source="bad.yml")


def test_reset_needs_restore() -> None:
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(operations=snapshot_operations(reset="transition.reset")), source="bad.yml")


def test_compatibility_is_required_when_checkpoint_is_declared() -> None:
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(operations=snapshot_operations(checkpoint="transition.checkpoint",
                                                                 restore="transition.restore")),
                      source="bad.yml")


def test_a_reviewed_checkpoint_block_needs_the_checkpoint_operation() -> None:
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(checkpoint={"failure_semantics": "no_change", "query": True}), source="bad.yml")


def test_a_reviewed_restore_block_needs_the_restore_operation() -> None:
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(restore={"failure_semantics": "unknown", "query": True}), source="bad.yml")


def test_a_reviewed_restore_reset_block_needs_the_reset_operation() -> None:
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(operations=snapshot_operations(checkpoint="transition.checkpoint",
                                                                 restore="transition.restore",
                                                                 compatibility="transition.compatibility"),
                                  restore={"failure_semantics": "unknown", "query": True,
                                           "reset": {"failure_semantics": "unknown", "query": True}}),
                      source="bad.yml")


def test_servable_check_must_name_an_observation() -> None:
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(servable_check="clean"), source="bad.yml")
    parse_profile(raw_profile(servable_check="clean", observations={"clean": "transition.clean"}),
                  source="bad.yml")


def test_restore_reset_verify_must_name_an_observation() -> None:
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(operations=snapshot_operations(checkpoint="transition.checkpoint",
                                                                 restore="transition.restore",
                                                                 reset="transition.reset",
                                                                 compatibility="transition.compatibility"),
                                  restore={"failure_semantics": "unknown", "query": True,
                                           "reset": {"failure_semantics": "unknown", "query": True,
                                                     "verify": "clean"}}),
                      source="bad.yml")


@pytest.mark.parametrize("bad", [123, "", "  ", ["clean"]])
def test_a_named_observation_that_is_not_a_name_is_refused(bad: Any) -> None:
    with pytest.raises(ProfileError, match="servable_check"):
        parse_profile(raw_profile(servable_check=bad, observations={"clean": "transition.clean"}), source="bad.yml")
    with pytest.raises(ProfileError, match="restore.reset.verify"):
        snapshot_profile(restore={"failure_semantics": "unknown", "query": True,
                                  "reset": {"failure_semantics": "unknown", "query": True, "verify": bad}})


@pytest.mark.parametrize("bad", ["no_change", ["no_change"], True])
def test_a_reviewed_block_that_is_not_a_mapping_is_refused(bad: Any) -> None:
    with pytest.raises(ProfileError, match="checkpoint is a mapping"):
        snapshot_profile(checkpoint=bad)
    with pytest.raises(ProfileError, match="restore is a mapping"):
        snapshot_profile(restore=bad)
    with pytest.raises(ProfileError, match="restore.reset is a mapping"):
        snapshot_profile(restore={"failure_semantics": "unknown", "query": True, "reset": bad})


def test_observations_need_non_empty_names_on_both_sides() -> None:
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(observations={"clean": ""}), source="bad.yml")
    with pytest.raises(ProfileError):
        parse_profile(raw_profile(observations={"": "transition.clean"}), source="bad.yml")
    profile = snapshot_profile()
    assert profile.observations == {"clean": "transition.clean"}


def test_the_shipped_snapshot_profile_declares_all_nine_operations() -> None:
    profile = transition_profiles.get(SNAPSHOT_PROFILE)
    assert profile is not None
    assert set(profile.operations) == {"idle", "resident", "release", "load", "query", "checkpoint", "restore",
                                       "reset", "compatibility"}
    assert profile.attested is True
    assert profile.declared["checkpoint"] == {"failure_semantics": "no_change", "query": True}
    assert profile.declared["restore"] == {"failure_semantics": "unknown", "query": True,
                                           "reset": {"failure_semantics": "unknown", "query": True}}
    assert profile.declared["placement_keys"] == []


# -- adapter units --------------------------------------------------------------------------------------


def test_supports_checkpoint_needs_the_operations_and_a_snapshot_path(tmp_path: Path) -> None:
    no_ops = adapter_for(parse_profile(raw_profile(), source="bad.yml"), FakeInvoke({}), tmp_path)
    assert no_ops.supports_checkpoint is False
    no_path = adapter_for(snapshot_profile(), FakeInvoke({}), tmp_path, with_path=False)
    assert no_path.supports_checkpoint is False
    both = adapter_for(snapshot_profile(), FakeInvoke({}), tmp_path)
    assert both.supports_checkpoint is True


def test_checkpoint_mints_a_unique_valid_handle_and_sends_it_with_the_path(tmp_path: Path) -> None:
    invoke = FakeInvoke({"transition.checkpoint": Reply(200, {"outcome": "success", "bytes": 7,
                                                             "evidence_ref": "ckpt", "incarnation": "inc-1"})})
    adapter = adapter_for(snapshot_profile(), invoke, tmp_path)

    async def scenario() -> tuple[Any, Any]:
        return await adapter.checkpoint("dom", intent()), await adapter.checkpoint("dom", intent())

    first, second = run(scenario())
    assert first.handle is not None and HANDLE.fullmatch(first.handle)
    assert second.handle is not None and first.handle != second.handle, "one handle per checkpoint effect"
    assert first.bytes == 7 and first.outcome == "success" and first.attested is False
    (op, first_body), (op2, second_body) = invoke.calls
    assert op == op2 == "transition.checkpoint"
    assert first_body["handle"] == first.handle and first_body["path"] == str(tmp_path / first.handle)
    assert second_body["handle"] == second.handle and second_body["path"] == str(tmp_path / second.handle)
    assert first_body["effect_key"] == "op-1:checkpoint"


def test_checkpoint_falls_back_to_the_file_size(tmp_path: Path) -> None:
    class WritingInvoke(FakeInvoke):
        async def __call__(self, domain: str, op: str, body: Mapping[str, Any]) -> Reply:
            self.calls.append((op, dict(body)))
            Path(str(body["path"])).write_bytes(b"snapshot-bytes")
            return Reply(200, {"outcome": "success", "evidence_ref": "ckpt"})

    adapter = adapter_for(snapshot_profile(), WritingInvoke({}), tmp_path)
    result = run(adapter.checkpoint("dom", intent()))
    assert result.outcome == "success" and result.bytes == len(b"snapshot-bytes")


def test_checkpoint_without_a_size_is_unknown(tmp_path: Path) -> None:
    invoke = FakeInvoke({"transition.checkpoint": Reply(200, {"outcome": "success", "evidence_ref": "ckpt"})})
    adapter = adapter_for(snapshot_profile(), invoke, tmp_path)
    result = run(adapter.checkpoint("dom", intent()))
    assert result.outcome == "unknown" and result.evidence_ref == "checkpoint_no_size"


def test_checkpoint_transport_error_and_non_2xx_are_unknown(tmp_path: Path) -> None:
    error = adapter_for(snapshot_profile(), FakeInvoke({"transition.checkpoint": RuntimeError("boom")}), tmp_path)
    assert run(error.checkpoint("dom", intent())).outcome == "unknown"
    bad_status = adapter_for(snapshot_profile(),
                             FakeInvoke({"transition.checkpoint": Reply(503, {"outcome": "success", "bytes": 7})}),
                             tmp_path)
    assert run(bad_status.checkpoint("dom", intent())).outcome == "unknown"


@pytest.mark.parametrize(("declared", "reported"), [("failed", "no_change"), ("partial", "partial")])
def test_a_declared_failed_checkpoint_is_the_profiles_failed_checkpoint(tmp_path: Path, declared: str,
                                                                         reported: str) -> None:
    # Live 2026-11-08: the engine refused every save (HTTP 400) and declared "failed". Read as "partial", the
    # best-effort checkpoint held the domain failed_closed for hours. The profile's checkpoint failure_semantics
    # speaks about exactly this failure; a declared "partial" stays partial (unknown to the step).
    invoke = FakeInvoke({"transition.checkpoint": Reply(200, {"outcome": declared, "evidence_ref": "save_refused"}),
                         "transition.query": Reply(200, {"outcome": declared})})
    adapter = adapter_for(snapshot_profile(checkpoint={"failure_semantics": "no_change"}), invoke, tmp_path)
    result = run(adapter.checkpoint("dom", intent()))
    assert (result.outcome, result.evidence_ref) == (reported, "save_refused") and result.handle is not None
    assert run(adapter.query("dom", intent("checkpoint"))).outcome == reported


def test_a_declared_failed_effect_the_profile_does_not_attest_stays_partial(tmp_path: Path) -> None:
    invoke = FakeInvoke({"transition.release": Reply(200, {"outcome": "failed"}),
                         "transition.query": Reply(200, {"outcome": "failed"})})
    adapter = adapter_for(snapshot_profile(), invoke, tmp_path)
    assert run(adapter.release("dom", intent("release"))).outcome == "partial"
    assert run(adapter.query("dom", intent("release"))).outcome == "partial"


def test_checkpoint_success_is_never_attested(tmp_path: Path) -> None:
    invoke = FakeInvoke({"transition.checkpoint": Reply(200, {"outcome": "success", "bytes": 1})})
    adapter = adapter_for(snapshot_profile(), invoke, tmp_path)
    assert run(adapter.checkpoint("dom", intent())).attested is False


def test_a_refused_snapshot_path_sends_nothing_and_changes_nothing(tmp_path: Path) -> None:
    def refuse(domain: str, handle: str) -> Path:
        raise ValueError("refused")
    invoke = FakeInvoke({"transition.checkpoint": Reply(200, {"outcome": "success", "bytes": 1}),
                         "transition.restore": Reply(200, {"outcome": "success"})})
    adapter = DeclaredOperationAdapter(snapshot_profile(), invoke, snapshot_path=refuse)
    checkpoint = run(adapter.checkpoint("dom", intent()))
    restore = run(adapter.restore("dom", "ckpt-a", intent("restore")))
    assert (checkpoint.outcome, checkpoint.evidence_ref) == ("no_change", "snapshot_path_error")
    assert (restore.outcome, restore.evidence_ref) == ("no_change", "snapshot_path_error")
    assert invoke.calls == [], "nothing is sent when the store refuses the path"


def test_undeclared_operations_are_unsupported(tmp_path: Path) -> None:
    invoke = FakeInvoke({})
    adapter = adapter_for(parse_profile(raw_profile(), source="bad.yml"), invoke, tmp_path)
    assert run(adapter.checkpoint("dom", intent())).evidence_ref == "checkpoint_unsupported"
    assert run(adapter.restore("dom", "ckpt-1", intent("restore"))).evidence_ref == "restore_unsupported"
    assert run(adapter.reset("dom", intent("restore"))).evidence_ref == "reset_unsupported"
    assert run(adapter.compatibility("dom")) == {}
    assert invoke.calls == [], "an undeclared operation is never sent"


def test_compatibility_stringifies_scalars_and_drops_the_rest(tmp_path: Path) -> None:
    invoke = FakeInvoke({"transition.compatibility": Reply(200, {"version": "1", "count": 3, "ratio": 1.5,
                                                                 "flag": True, "off": False, "nothing": None,
                                                                 "items": [1], "nested": {"a": 1}})})
    adapter = adapter_for(snapshot_profile(), invoke, tmp_path)
    assert run(adapter.compatibility("dom")) == {"version": "1", "count": "3", "ratio": "1.5",
                                                 "flag": "true", "off": "false"}


def test_compatibility_is_empty_on_error(tmp_path: Path) -> None:
    error = adapter_for(snapshot_profile(), FakeInvoke({"transition.compatibility": RuntimeError("boom")}), tmp_path)
    assert run(error.compatibility("dom")) == {}
    bad_status = adapter_for(snapshot_profile(),
                             FakeInvoke({"transition.compatibility": Reply(503, {"version": "1"})}), tmp_path)
    assert run(bad_status.compatibility("dom")) == {}


def test_observe_reads_the_named_value(tmp_path: Path) -> None:
    invoke = FakeInvoke({"transition.clean": Reply(200, {"value": True, "incarnation": "inc-1"})})
    adapter = adapter_for(snapshot_profile(), invoke, tmp_path)
    evidence = run(adapter.observe("dom", "clean"))
    assert evidence.value is True and evidence.incarnation == "inc-1"
    assert invoke.calls == [("transition.clean", {})]


def test_observe_of_an_unknown_name_observes_nothing(tmp_path: Path) -> None:
    invoke = FakeInvoke({})
    adapter = adapter_for(snapshot_profile(), invoke, tmp_path)
    assert run(adapter.observe("dom", "missing")).value is None
    assert invoke.calls == []


# -- end to end -----------------------------------------------------------------------------------------


class SnapshotEngine:
    """A fake engine that writes snapshots to the path it is given and records restores."""

    def __init__(self) -> None:
        self.resident: Optional[str] = "alpha"
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.checkpoint_body = b"snapshot-bytes"
        self.checkpoint_outcome = "success"
        self.restore_outcome = "success"
        self.reset_outcome = "success"
        self.restores: list[tuple[str, str]] = []
        self.resets = 0

    async def __call__(self, service_id: str, op: str, body: Mapping[str, Any]) -> Reply:
        self.calls.append((op, dict(body)))
        if op == "transition.resident":
            return Reply(200, {"resident": self.resident is not None, "offering": self.resident,
                               "ready": True, "incarnation": "inc-1"})
        if op == "transition.idle":
            return Reply(200, {"idle": True, "incarnation": "inc-1"})
        if op == "transition.release":
            self.resident = None
            return Reply(200, {"outcome": "success", "incarnation": "inc-1"})
        if op == "transition.load":
            self.resident = str(body["target"])
            return Reply(200, {"outcome": "success", "evidence_ref": "loaded"})
        if op == "transition.query":
            return Reply(200, {"outcome": "unknown"})
        if op == "transition.checkpoint":
            if self.checkpoint_outcome != "success":  # the engine refused the save and wrote nothing
                return Reply(200, {"outcome": self.checkpoint_outcome, "evidence_ref": "save_refused_http_400",
                                   "incarnation": "inc-1"})
            Path(str(body["path"])).write_bytes(self.checkpoint_body)
            return Reply(200, {"outcome": "success", "bytes": len(self.checkpoint_body),
                               "evidence_ref": "ckpt", "incarnation": "inc-1"})
        if op == "transition.restore":
            self.restores.append((str(body["handle"]), str(body["path"])))
            return Reply(200, {"outcome": self.restore_outcome, "evidence_ref": "restored",
                               "incarnation": "inc-1"})
        if op == "transition.reset":
            self.resets += 1
            return Reply(200, {"outcome": self.reset_outcome, "evidence_ref": "reset", "incarnation": "inc-1"})
        if op == "transition.compatibility":
            return Reply(200, {"version": "1", "mode": "fast"})
        raise AssertionError(op)


@pytest.fixture
def snapshot_wired(tmp_path: Path):
    store = SqliteSnapshotStore(tmp_path / "transition.db", PrivateSnapshotDir(tmp_path / "snap"),
                                SnapshotBudget(max_bytes=10_000, max_count=4))
    lazy = LazySnapshotStore(lambda: store)
    gates = GateStore(tmp_path / "transition.db")
    gates.load()
    drain = SwapDrain(wall_clock=time.time)
    locks: dict[str, asyncio.Lock] = {}
    engine = SnapshotEngine()
    runtime = TransitionRuntime(gates=lambda: gates, services=snapshot_services,
                                lock_for=lambda d: locks.setdefault(d, asyncio.Lock()),
                                drain=lambda: drain, protocol_invoke=engine,
                                extra_steps=snapshot_steps(lazy), snapshots=lazy)
    return gates, engine, runtime, store


def test_park_publishes_a_snapshot_and_wake_restores_the_same_handle(snapshot_wired) -> None:
    gates, engine, runtime, store = snapshot_wired

    async def scenario() -> None:
        executor = runtime.executor()
        park = await executor.submit(domain="dom", kind="park", idempotency_key="park-1")
        done = await executor.wait(park.operation.op_id)
        assert done.state == "succeeded"
        assert store.pointer("dom") is not None, "the park published a snapshot"
        (op, checkpoint_body), = [call for call in engine.calls if call[0] == "transition.checkpoint"]
        handle, path = str(checkpoint_body["handle"]), str(checkpoint_body["path"])
        assert Path(path).exists() and Path(path).read_bytes() == engine.checkpoint_body
        wake = await executor.submit(domain="dom", kind="wake", idempotency_key="wake-1")
        done = await executor.wait(wake.operation.op_id)
        assert done.state == "succeeded"
        assert engine.restores == [(handle, path)], "the wake restores the snapshot the park published"
        assert engine.resident == "alpha"
        assert store.pointer("dom") is not None

    run(scenario())


def test_a_failed_restore_resets_serves_cold_and_retires_the_snapshot(snapshot_wired) -> None:
    gates, engine, runtime, store = snapshot_wired

    async def scenario() -> None:
        executor = runtime.executor()
        park = await executor.submit(domain="dom", kind="park", idempotency_key="park-1")
        assert (await executor.wait(park.operation.op_id)).state == "succeeded"
        assert store.pointer("dom") is not None
        engine.restore_outcome = "failed"
        wake = await executor.submit(domain="dom", kind="wake", idempotency_key="wake-1")
        done = await executor.wait(wake.operation.op_id)
        assert done.state == "succeeded"
        assert engine.resets == 1, "the failed restore runs the declared reset"
        assert engine.resident == "alpha", "the domain serves cold"
        assert store.pointer("dom") is None, "the snapshot is retired, not offered again"
        assert store.latest_published("dom", {"version": "1", "mode": "fast"}) is None
        assert len(engine.restores) == 1

    run(scenario())


@pytest.mark.parametrize("kind", ["park", "select"])
def test_a_refused_best_effort_checkpoint_goes_on_cold_and_leaves_the_gate_open(snapshot_wired, kind) -> None:
    # The shipped profile attests that a failed checkpoint changes nothing, and best_effort is the default:
    # the transition goes on with no snapshot instead of holding the domain failed_closed.
    gates, engine, runtime, store = snapshot_wired
    engine.checkpoint_outcome = "failed"

    async def scenario() -> None:
        executor = runtime.executor()
        began = await executor.submit(domain="dom", kind=kind, idempotency_key=f"{kind}-1",
                                      **({"target": "alpha"} if kind == "select" else {}))
        done = await executor.wait(began.operation.op_id)
        assert done.state == "succeeded", (done.state, done.step, done.step_outcome, done.evidence_ref)
        assert gates.state("dom") == "open"
        assert store.pointer("dom") is None, "no snapshot is published from a refused save"
        assert any(op == "transition.release" for op, _ in engine.calls), "the release goes ahead"

    run(scenario())

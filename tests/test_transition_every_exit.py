"""D2-D4 exit matrix. No private patches; SQLite prefixes model interrupted writes.

The crash rows are durable-state reconstructions, not killed-process tests. Cells with
no injection seam (store-write deadlines, process death) are listed in NOT_COVERED and
are not collected: a skipped case proves nothing and the suite policy refuses it.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.swap_drain import SwapDrain
from gateway.transition_adapter import DeclaredOperationAdapter
from gateway.transition_executor import Executor, core_steps
from gateway.transition_gate import GateStore
from gateway.transition_ops import Begun, Conflict, EffectResult, StepIntent
from gateway.transition_snapshot_steps import snapshot_steps
from gateway.transition_store import SqliteOperationStore


PAIRS = {"O": ("open", "succeeded"), "A": ("open", "aborted"),
         "F": ("failed_closed", "failed_closed"), "R": ("reconciling", "reconciling")}
EFFECTS = ("checkpoint", "release", "load", "restore", "reset")


@dataclass(frozen=True)
class Cell:
    stage: str
    fault: str
    oracle: str
    policy: str = "best_effort"
    value: bool | None = True
    reset: bool = True
    verifier: bool = True
    mode: str = "live"
    mark: str = ""
    # What the owner-resolved reset meets once the engine is healthy again.
    late: str = "clean"

    @property
    def id(self):
        base = ":".join(map(str, (self.mode, self.stage, self.fault, self.policy,
                                  self.value, self.reset, self.verifier)))
        return base if self.late == "clean" else f"{base}:late-{self.late}"


def cells():
    rows = []
    for stage, oracle in (("close", "S"), ("drain", "R"), ("open", "F"), ("trigger", "R")):
        rows.append(Cell(stage, "E", oracle, mark="design_silent"))
    for stage in ("close", "open", "trigger"):
        rows.append(Cell(stage, "D", "S", mark="design_silent_no_seam"))
    rows += [Cell("drain", fault, "A") for fault in ("D", "Q")]
    for policy in ("best_effort", "required"):
        rows += [Cell("checkpoint", fault, "R", policy) for fault in ("E", "D", "U", "Q")]
        rows += ([Cell("checkpoint", "N", "O", policy)] if policy == "best_effort" else
                 [Cell("checkpoint", "N", oracle, policy, value)
                  for value, oracle in ((True, "A"), (False, "F"), (None, "R"))])
        for fault in ("E", "D"):
            rows += ([Cell("publication", fault, "O", policy)] if policy == "best_effort" else
                     [Cell("publication", fault, oracle, policy, value)
                      for value, oracle in ((True, "A"), (False, "F"), (None, "R"))])
    for stage in ("release", "load"):
        rows += [Cell(stage, fault, oracle) for fault, oracle in
                 (("E", "R"), ("D", "R"), ("U", "F"), ("Q", "R"))]
        rows += [Cell(stage, "N", oracle, value=value)
                 for value, oracle in ((True, "A"), (False, "F"), (None, "R"))]
    rows += [Cell("restore", fault, oracle) for fault, oracle in
             (("E", "R"), ("D", "R"), ("N", "O"), ("Q", "R"))]
    rows += [Cell("restore", "U", oracle, value=value)
             for value, oracle in ((True, "O"), (False, "F"), (None, "R"))]
    rows.append(Cell("restore", "U", "F", reset=False))
    # A held restore the owner resolves partial: no declared reset, a reset that fails, or a target
    # that verifies not clean all stay failed_closed; only a clean, ready target serves cold.
    rows += [Cell("restore", "E", "R", reset=False),
             Cell("restore", "E", "R", late="reset_fails"),
             Cell("restore", "E", "R", late="dirty")]
    rows += [Cell("reset", fault, oracle) for fault, oracle in
             (("E", "R"), ("D", "R"), ("U", "F"), ("Q", "R"))]
    rows += [Cell("reset", "N", oracle, value=value)
             for value, oracle in ((True, "O"), (False, "F"), (None, "R"))]
    rows.append(Cell("reset", "N", "F", verifier=False))
    for stage in ("release", "load", "reset"):
        rows += [Cell("verify_" + stage, fault, oracle)
                 for fault, oracle in (("E", "R"), ("D", "R"), ("Q", "R"), ("false", "F"))]
    rows += [Cell("resumed_load", "false", "R", mode="restart"),
             Cell("double_finish", "E", "R")]
    # One literal success prefix after every durable write; reset has its own
    # intent/outcome despite sharing the restore step name.
    rows += [Cell(stage, "success", "O", mode="prefix") for stage in
             ("begin", "drain_intent", "drain_outcome", "open_intent", "finish")]
    rows += [Cell("drain_outcome", fault, "A", mode="prefix") for fault in ("D", "Q")]
    rows += [Cell(stage, "unknown", "R", mode="intent") for stage in EFFECTS]
    rows.append(Cell("snapshot_handle", "unknown", "R", mode="intent"))
    rows += [Cell(stage, "success", "O", mode="outcome") for stage in EFFECTS]
    # The same independent oracle is applied to committed effect outcomes.
    rows += [replace(row, mode="outcome") for row in rows[:]
             if row.mode == "live" and row.stage in EFFECTS]
    for stage in ("begin", "drain_intent", "drain_outcome", "checkpoint_intent",
                  "snapshot_handle", "checkpoint_outcome", "release_intent", "release_outcome",
                  "load_intent", "load_outcome", "restore_intent", "restore_outcome",
                  "reset_intent", "reset_outcome", "open_intent", "finish"):
        rows.append(Cell(stage, "D", "S", mode="store_write", mark="no_seam"))
    rows.append(Cell("process", "crash", "S", mode="subprocess", mark="no_seam"))
    return rows


def parameter(cell):
    return pytest.param(cell, id=cell.id)


# Exits the matrix names but cannot reach without a seam the executor does not have yet.
NOT_COVERED = {cell.id for cell in cells() if "no_seam" in cell.mark}

# Independent collection contract: changing a row's mark cannot silently drop it.
EXPECTED_NOT_COVERED = {
    "live:close:D:best_effort:True:True:True",
    "live:open:D:best_effort:True:True:True",
    "live:trigger:D:best_effort:True:True:True",
    "store_write:begin:D:best_effort:True:True:True",
    "store_write:drain_intent:D:best_effort:True:True:True",
    "store_write:drain_outcome:D:best_effort:True:True:True",
    "store_write:checkpoint_intent:D:best_effort:True:True:True",
    "store_write:snapshot_handle:D:best_effort:True:True:True",
    "store_write:checkpoint_outcome:D:best_effort:True:True:True",
    "store_write:release_intent:D:best_effort:True:True:True",
    "store_write:release_outcome:D:best_effort:True:True:True",
    "store_write:load_intent:D:best_effort:True:True:True",
    "store_write:load_outcome:D:best_effort:True:True:True",
    "store_write:restore_intent:D:best_effort:True:True:True",
    "store_write:restore_outcome:D:best_effort:True:True:True",
    "store_write:reset_intent:D:best_effort:True:True:True",
    "store_write:reset_outcome:D:best_effort:True:True:True",
    "store_write:open_intent:D:best_effort:True:True:True",
    "store_write:finish:D:best_effort:True:True:True",
    "subprocess:process:crash:best_effort:True:True:True",
}


def test_every_exit_matrix_contract():
    assert NOT_COVERED == EXPECTED_NOT_COVERED
    assert {
        cell.stage: cell.oracle for cell in cells()
        if cell.mode == "live" and cell.fault == "E"
        and cell.stage in ("drain", "open", "trigger")
    } == {"drain": "R", "open": "F", "trigger": "R"}


class InjectedFailure(OSError):
    pass


class ReviewedProfile:
    """Profile collaborator; the real adapter interprets the declared failed reply."""

    attested = True
    operations = {name: name for name in (*EFFECTS, "query", "idle", "resident", "compatibility")}
    observations = {name: name for name in ("servable", "clean")}

    def __init__(self, policy):
        self.policy = policy

    def attests_no_change(self, block):
        value = self.policy
        for part in block.split("."):
            value = value.get(part, {})
        return value.get("failure_semantics") == "no_change"


def policy_for(cell):
    profile = {name: {"failure_semantics": "unknown", "query": True}
               for name in ("release", "load", "checkpoint", "restore")}
    # Publication failure is a known no-change independently of the save reply.
    profile["checkpoint"]["failure_semantics"] = "no_change"
    if cell.reset:
        profile["restore"]["reset"] = {"failure_semantics": "unknown", "query": True}
        if cell.verifier:
            profile["restore"]["reset"]["verify"] = "clean"
    if cell.stage in EFFECTS and cell.fault in ("N", "U"):
        block = profile["restore"]["reset"] if cell.stage == "reset" else profile[cell.stage]
        block["failure_semantics"] = "no_change" if cell.fault == "N" else "unknown"
    profile["servable_check"] = "servable"
    # A deadline is short only where the cell injects a fault. With no fault nothing should time out, and 10 ms is less
    # than one stalled step on a loaded host: gate t65 run 2 saw a success cell abort.
    d = 5.0 if cell.fault == "success" else .01
    return {"profile": profile, "checkpoint_policy": cell.policy,
            "drain_deadline_s": d, "idle_poll_s": .001,
            "release_deadline_s": d, "load_deadline_s": d,
            "probe_deadline_s": d, "query_deadline_s": d,
            "deadlines": {name: d for name in (*EFFECTS, "query")}}


class Engine:
    def __init__(self, cell):
        self.cell = cell
        self.enabled = True
        self.offering = "alpha"
        self.last_effect = None
        self.calls = Counter()
        self.queries = []
        self.blocked = asyncio.Event()
        self.pending = set()

    async def deadline(self):
        task = asyncio.current_task()
        self.pending.add(task)
        try:
            await self.blocked.wait()
        finally:
            self.pending.discard(task)

    async def __call__(self, domain, name, body):
        cell = self.cell
        live = self.enabled and cell.mode == "live"
        if name in EFFECTS:
            self.calls[name, body["effect_key"]] += 1
            self.last_effect = name
            # Reset's surrounding restore must reach the fallback, not succeed.
            wants_reset = cell.stage in ("reset", "verify_reset")
            if self.enabled and wants_reset and name == "restore":
                return SimpleNamespace(status=200, selected={"outcome": "failed"})
            if not self.enabled and name == "reset" and cell.late == "reset_fails":
                return SimpleNamespace(status=200, selected={"outcome": "failed"})
            if live and cell.stage == name:
                if cell.fault == "E":
                    raise InjectedFailure(name)
                if cell.fault == "D":
                    await self.deadline()
                    return SimpleNamespace(status=200, selected={"outcome": "unknown"})
                if cell.fault in ("N", "U", "Q"):
                    return SimpleNamespace(status=200, selected={
                        "outcome": "unknown" if cell.fault == "Q" else "failed"})
            if live and cell.stage == "double_finish" and name == "release":
                return SimpleNamespace(status=200, selected={"outcome": "unknown"})
            if name == "release":
                self.offering = None
            elif name == "load":
                self.offering = body["target"]
            return SimpleNamespace(status=200, selected={"outcome": "success", "bytes": 1,
                                                       "incarnation": "inc-1"})
        if name == "query":
            self.queries.append(dict(body))
            if live and cell.fault == "Q":
                raise InjectedFailure("unreadable query")
            return SimpleNamespace(status=200, selected={"outcome": "unknown"})
        if name == "compatibility":
            if live and cell.stage == "publication" and cell.fault == "D":
                await self.deadline()
            return SimpleNamespace(status=200, selected={"format": "one"})
        if live and cell.stage == "drain" and cell.fault == "Q" and name in ("resident", "idle"):
            raise InjectedFailure("unreadable drain evidence")
        verification = (live and cell.stage == "verify_" + str(self.last_effect)
                        and name == ("clean" if self.last_effect == "reset" else "resident"))
        if verification:
            if cell.fault == "E":
                raise InjectedFailure("verification")
            if cell.fault == "D":
                await self.deadline()
            if cell.fault == "Q":
                return SimpleNamespace(status=200, selected={})
        if name == "resident":
            resident = self.offering is not None
            if verification and cell.fault == "false":
                resident = self.last_effect == "release"
            if self.enabled and cell.stage == "resumed_load":
                resident = False
            return SimpleNamespace(status=200, selected={"resident": resident,
                "offering": self.offering, "ready": True, "incarnation": "inc-1"})
        if name == "idle":
            return SimpleNamespace(status=200, selected={"idle": True, "incarnation": "inc-1"})
        value = cell.value if self.enabled else cell.late != "dirty"
        if verification and cell.fault == "false":
            value = False
        return SimpleNamespace(status=200, selected={"value": value, "incarnation": "inc-1"})

    async def healthy(self):
        self.enabled = False
        self.blocked.set()
        if self.pending:
            await asyncio.gather(*tuple(self.pending))
        await asyncio.sleep(0)  # let late-effect observers consume their result


class Snapshots:
    """Small public lifecycle collaborator; operation durability remains real SQLite."""

    def __init__(self, engine):
        self.engine = engine
        self.published = SimpleNamespace(handle="seed", bytes=1)
        self.pending_row = None
        self.pins = set()

    def pending(self, **values):
        self.pending_row = SimpleNamespace(**values)
        return self.pending_row

    def publish(self, handle, *, domain=None):
        cell = self.engine.cell
        if self.engine.enabled and cell.mode == "live" and cell.stage == "publication" and cell.fault == "E":
            raise InjectedFailure("publication")
        self.published = self.pending_row
        return self.published

    def retire_published(self, domain):
        self.published = None

    def abandon_pending(self, domain, op_id):
        self.pending_row = None

    def has_room(self, domain):
        return True

    def latest_published(self, domain, compatibility, placement_keys):
        return self.published

    def collect(self, domain):
        return None

    def pin(self, handle, *, domain=None):
        self.pins.add(handle)
        return True

    def unpin(self, handle, *, domain=None):
        self.pins.discard(handle)
        return True


class StoreFaults:
    def __init__(self, store, cell):
        self.store, self.cell = store, cell
        self.enabled = True
        self.finish_failures = 0
        self.open_failures = 0

    def __getattr__(self, name):
        return getattr(self.store, name)

    def write_intent(self, op_id, intent, held=None):
        if self.enabled and self.cell.stage == "open" and intent.step == "open":
            self.open_failures += 1
            raise InjectedFailure("open intent")
        return self.store.write_intent(op_id, intent, held)

    def finish(self, op_id, state, gate):
        if self.enabled and self.cell.stage == "double_finish" and self.finish_failures < 2:
            self.finish_failures += 1
            raise InjectedFailure("finalization storage outage")
        return self.store.finish(op_id, state, gate)


class DrainFaults:
    def __init__(self, drain, cell):
        self.drain, self.cell = drain, cell
        self.enabled = True
        self.failures = 0

    async def clear_for_swap(self, domain, settings):
        if self.enabled and self.cell.stage == "drain" and self.cell.fault == "E":
            self.failures += 1
            raise InjectedFailure("drain")
        return await self.drain.clear_for_swap(domain, settings)


class Rig:
    def __init__(self, path, cell):
        self.path, self.cell = path, cell
        self.gates = GateStore(path / "transition.db")
        self.gates.load()
        self.close_armed = cell.stage == "close" and cell.fault == "E"
        self.serial = 0
        self.trigger_failures = 0

        def new_id():
            if self.close_armed:
                self.close_armed = False
                raise InjectedFailure("close transaction")
            self.serial += 1
            return f"op-{self.serial}"

        self.raw = SqliteOperationStore(self.gates, new_id=new_id)
        self.store = StoreFaults(self.raw, cell)
        self.lock = asyncio.Lock()
        self.policy = policy_for(cell)
        self.engine = Engine(cell)
        self.snapshots = Snapshots(self.engine)
        self.adapter = DeclaredOperationAdapter(ReviewedProfile(self.policy["profile"]), self.engine,
                                                snapshot_path=lambda domain, handle: path / handle)
        self.drain = SwapDrain()
        self.drain_faults = DrainFaults(self.drain, cell)
        self.lease = self.drain.lease("dom", admit=False) if cell.stage == "drain" and cell.fault == "D" else None
        self.executor = self.new_executor()

    def new_executor(self):
        return Executor(store=self.store,
                        steps={**core_steps(self.drain_faults), **snapshot_steps(self.snapshots)},
                        adapter_for=lambda domain: self.adapter, policy_of=lambda domain: self.policy,
                        lock_for=lambda domain: self.lock, snapshots=self.snapshots)

    async def submit(self, key, *, healthy=False):
        kind = "wake" if not healthy and self.cell.stage in ("restore", "reset", "verify_reset") else "select"

        def trigger():
            if not healthy and self.cell.stage == "trigger":
                self.trigger_failures += 1
                raise InjectedFailure("trigger")

        begun = await self.executor.submit(domain="dom", kind=kind, idempotency_key=key,
                                           target="beta", trigger=trigger)
        assert isinstance(begun, Begun)
        return await self.executor.wait(begun.operation.op_id)

    async def healthy(self):
        self.store.enabled = self.drain_faults.enabled = False
        if self.lease is not None:
            self.lease.release()
            self.lease = None
        await self.engine.healthy()


async def unlocked(lock):
    assert lock.locked() is False
    await asyncio.wait_for(lock.acquire(), .5)
    try:
        assert lock.locked() is True
    finally:
        lock.release()


def committed_result(cell):
    """Literal durable records from D4, not production verdict helpers."""
    stage, fault = cell.stage, cell.fault
    if fault == "success":
        return EffectResult(outcome="success", attested=stage == "release")
    if fault in ("E", "D", "Q"):
        return EffectResult(outcome="unknown", evidence_ref="interrupted_effect")
    if stage == "checkpoint":
        if fault == "U" or (cell.policy == "required" and cell.value is None):
            return EffectResult(outcome="unknown", evidence_ref="checkpoint:unknown")
        return EffectResult(outcome="no_change", evidence_ref="checkpoint:engine_declined:cold_next_start")
    if stage in ("release", "load"):
        return EffectResult(outcome="unknown" if fault == "N" and cell.value is None else
                            "no_change" if fault == "N" else "partial")
    if stage == "restore":
        return EffectResult(outcome="no_change" if fault == "N" else "partial",
                            evidence_ref="restore:declined:serving_cold" if fault == "N" else
                            "restore:failed:state_not_known_clean")
    if fault == "U" or not cell.verifier:
        return EffectResult(outcome="partial", evidence_ref="reset:failed:state_unknown")
    if cell.value is True:
        return EffectResult(outcome="no_change", evidence_ref="restore:reset:clean:serving_cold")
    return EffectResult(outcome="partial" if cell.value is False else "unknown",
                        evidence_ref="reset:target_not_verified_clean")


def committed_prefix(rig, cell):
    """Stop after one public SQLite write, with no old executor task to race."""
    begun = rig.raw.begin(domain="dom", kind="select", idempotency_key="interrupted",
                          target="beta", incarnation="inc-1", config_revision=1)
    assert isinstance(begun, Begun)
    op_id = begun.operation.op_id
    settled = Counter()

    def intent(effect):
        step = "restore" if effect == "reset" else effect
        rig.raw.write_intent(op_id, StepIntent(step=step, effect_key=f"{op_id}:{effect}",
                                               expected_incarnation=None if effect == "load" else "inc-1"))

    def outcome(effect, result):
        step = "restore" if effect == "reset" else effect
        rig.raw.write_outcome(op_id, step, result)
        if effect in EFFECTS:
            key = (effect, f"{op_id}:{effect}")
            rig.engine.calls[key] += 1  # the pre-crash effect, not an executor replay
            if result.outcome != "unknown":
                settled[key] = rig.engine.calls[key]

    if cell.stage == "begin":
        return op_id, settled
    intent("drain")
    if cell.stage == "drain_intent":
        return op_id, settled
    outcome("drain", EffectResult(outcome="no_change" if cell.stage == "drain_outcome" and
                                  cell.fault in ("D", "Q") else "success"))
    if cell.stage == "drain_outcome":
        return op_id, settled

    for effect in EFFECTS:
        # A successful restore has no reset. Include reset only on that path.
        if effect == "reset" and cell.stage != "reset":
            continue
        if effect == "checkpoint":
            rig.snapshots.retire_published("dom")
        intent(effect)
        if cell.mode == "intent" and cell.stage == effect:
            return op_id, settled
        if effect == "checkpoint":
            rig.raw.set_snapshot_handle(op_id, "prefix-snapshot")
            if cell.stage == "snapshot_handle":
                rig.engine.calls[effect, f"{op_id}:{effect}"] += 1
                return op_id, settled
        target = cell.mode == "outcome" and cell.stage == effect
        result = committed_result(cell) if target else EffectResult(outcome="success", attested=effect == "release")
        if effect == "restore" and cell.stage == "reset":
            result = EffectResult(outcome="partial", evidence_ref="restore:failed:state_not_known_clean")
        if effect == "restore" and result.outcome in ("no_change", "partial"):
            rig.snapshots.retire_published("dom")
        if result.outcome == "success":
            if effect == "checkpoint":
                rig.snapshots.published = SimpleNamespace(handle="prefix-snapshot", bytes=1)
            elif effect == "release":
                rig.engine.offering = None
            elif effect == "load":
                rig.engine.offering = "beta"
        outcome(effect, result)
        if target or (cell.stage == "resumed_load" and effect == "load"):
            return op_id, settled
    intent("open")
    if cell.stage == "finish":
        rig.raw.finish(op_id, "succeeded", "open")
    return op_id, settled


async def restart_at_prefix(rig, cell):
    op_id, settled = committed_prefix(rig, cell)
    before = rig.engine.calls.copy()
    # New objects reload the same real database; no executor internals are changed.
    gates = GateStore(rig.path / "transition.db")
    gates.load()
    rig.gates = gates
    rig.raw = SqliteOperationStore(gates)  # fresh IDs cannot collide with the seeded row
    rig.store = StoreFaults(rig.raw, cell)
    rig.executor = rig.new_executor()
    await rig.executor.ensure_reconciled()
    done = await rig.executor.wait(op_id)
    for key, count in settled.items():
        assert rig.engine.calls[key] == count, f"settled effect replayed: {key}"
    if cell.mode == "intent":
        effect = "checkpoint" if cell.stage == "snapshot_handle" else cell.stage
        key = (effect, f"{op_id}:{effect}")
        assert rig.engine.calls[key] == before[key], "an unresolved effect is queried, not replayed"
        assert any(query["effect_key"] == key[1] for query in rig.engine.queries)
    return done


@pytest.mark.parametrize("cell", [parameter(cell) for cell in cells() if cell.id not in EXPECTED_NOT_COVERED])
def test_every_exit_releases_lock_and_remains_finishable(tmp_path: Path, cell: Cell):
    async def scenario():
        rig = Rig(tmp_path, cell)
        try:
            if cell.mode != "live":
                done = await restart_at_prefix(rig, cell)
            elif cell.stage == "close":
                with pytest.raises(InjectedFailure, match="close transaction"):
                    await rig.submit("failed")
                done = None
            else:
                done = await rig.submit("failed")

            # The order matters: lock release/reacquisition before state or row checks.
            await unlocked(rig.lock)
            if cell.mode == "live" and cell.fault == "E" and cell.stage in ("drain", "open", "trigger"):
                reached = {"drain": rig.drain_faults.failures, "open": rig.store.open_failures,
                           "trigger": rig.trigger_failures}
                assert reached[cell.stage] == 1, f"{cell.id}: injected fault was not reached exactly once"
            if cell.stage == "double_finish":
                assert rig.store.finish_failures == 2
                assert (rig.gates.state("dom"), done.state, done.step_outcome) == ("reconciling", "running", "unknown")
                assert len(rig.raw.history("dom")) == 1
                assert rig.executor.reconciled() is False
                rig.store.enabled = False  # storage recovers, this executor does not restart
                refused = await rig.executor.submit(domain="dom", kind="select", idempotency_key="refused", target="beta")
                assert isinstance(refused, Conflict)
                assert refused.operation.op_id == done.op_id
                done = rig.raw.get(done.op_id)
                await unlocked(rig.lock)
                assert (rig.gates.state("dom"), done.state) == PAIRS["R"]
                assert len(rig.raw.history("dom")) == 1, "refusal created no new row"
            elif done is None:
                assert rig.gates.state("dom") == "open"
            elif cell.oracle == "S":
                assert (rig.gates.state("dom"), done.state) in PAIRS.values()
            else:
                assert (rig.gates.state("dom"), done.state) == PAIRS[cell.oracle]
            assert len(rig.raw.history("dom")) == (0 if done is None else 1)

            await rig.healthy()
            if done is not None and done.state == "reconciling":
                if cell.stage == "double_finish":
                    rig.engine.offering = None  # owner's evidence says the release completed
                    await rig.executor.resolve(done.op_id, step=done.step, outcome="success", evidence_ref="owner:released")
                    resolved = await rig.executor.wait(done.op_id)
                    assert (rig.gates.state("dom"), resolved.state) == PAIRS["O"]
                else:
                    # A known partial outcome is owner resolution, not a production rollback.
                    resolved = await rig.executor.resolve(done.op_id, step=done.step, outcome="partial", evidence_ref="owner:known")
                    # A recorded partial restore runs its declared reset (SERVICE_TRANSITION_GATE.md, restore
                    # partial); only a verified clean, ready target serves cold. Without a reset, or without a
                    # clean verification, the gate stays failed_closed.
                    # A reset that already ran and could not be read (restore:U with no answer) is not run again.
                    resets_clean = (cell.stage == "restore" and cell.reset and cell.late == "clean"
                                    and done.evidence_ref != "reset:target_not_verified_clean")
                    if resets_clean:
                        resolved = await rig.executor.wait(done.op_id)
                    assert (rig.gates.state("dom"), resolved.state) == PAIRS["O" if resets_clean else "F"]
            await unlocked(rig.lock)
            follow_up = await rig.submit("healthy", healthy=True)
            await unlocked(rig.lock)
            assert (rig.gates.state("dom"), follow_up.state) == PAIRS["O"]
            assert len(rig.raw.history("dom")) == (1 if done is None else 2)
        finally:
            await rig.healthy()

    asyncio.run(scenario())

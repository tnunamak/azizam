"""The checkpoint and restore steps of the transition executor (design D3, D4 rows 3 and 6, D5).

``snapshot_steps(snapshots)`` returns the two entries the gateway merges into the executor's step
table. This module does not run an executor and does not edit one.

Where the policy comes from. ``StepContext.policy`` carries two kinds of data:

- ``policy["profile"]``: the reviewed transition profile. Only this mapping can attest anything:
  ``failure_semantics`` of checkpoint, restore and reset, the integrity and verification
  observations, the placement keys that may differ between save and restore, and the freshness
  bound. A value anywhere else in ``policy`` (owner-editable config) is never read for these.
- the rest of ``policy``: plain owner arguments. ``checkpoint_policy`` (``best_effort`` or
  ``required``; an unknown value counts as ``required``) and ``deadlines`` (seconds per effect).

Profile shape (every key optional; an absent key is the safe reading)::

    {"checkpoint": {"failure_semantics": "no_change" | "unknown", "query": bool},
     "restore":    {"failure_semantics": ..., "query": bool,
                    "reset": {"failure_semantics": ..., "query": bool, "verify": <observation>}},
                    # a reset that failed with an attested no_change needs "verify"; without it the gate stays closed
     "servable_check": <observation>,   # default: the source's resident observation (G7)
     "placement_keys": [<compatibility key>, ...],
     "fresh_within_s": <seconds>}

The step declares an effect "declared" when its profile block is present. ``failure_semantics``
says whether a failed call is known to leave the engine unchanged (``no_change``) or not
(``unknown``, the default). A ``no_change`` that the profile does not attest counts as a failure of
unknown effect.

Engine-agnostic: observations are opaque names the adapter answers; compatibility is a map of
opaque strings; no engine, model or vendor is named here.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Collection, Mapping, Optional, Protocol, TypeVar

from gateway.transition_ops import (
    EffectResult,
    Evidence,
    Operation,
    Snapshot,
    SnapshotStore,
    Step,
    StepContext,
    StepIntent,
    StepName,
    StepVerdict,
    TransitionAdapter,
)
from gateway.transition_snapshots import SnapshotRejected

_T = TypeVar("_T")
DEFAULT_FRESH_WITHIN_S = 2.0
DEFAULT_DEADLINE_S = 60.0
_SEMANTICS = ("no_change", "unknown")


class SnapshotAdapter(TransitionAdapter, Protocol):
    """What the snapshot steps need beyond ``TransitionAdapter`` (interface addition, T1.3).

    Handle contract: the ``handle`` a checkpoint reports must be unique per checkpoint effect, never
    reused while a snapshot with that handle is live in the domain. An adapter that writes every
    checkpoint to one fixed handle breaks the contract. ``SnapshotStore.pending`` refuses a reused
    handle, and the step treats the refusal as a failed checkpoint (no snapshot, cold next start).
    """

    async def compatibility(self, domain: str) -> Mapping[str, str]:
        """The opaque compatibility token for the resolved effective state (D5); empty when unknown."""
        ...

    async def observe(self, domain: str, name: str) -> Evidence:
        """Answer one named observation from the reviewed profile."""
        ...


class SnapshotLifecycle(SnapshotStore, Protocol):
    """What the snapshot steps need beyond ``SnapshotStore`` (interface addition, T1.3)."""

    def retire_published(self, domain: str) -> object: ...

    def has_room(self, domain: str, expect_bytes: int = 0) -> bool: ...

    def latest_published(self, domain: str, compatibility: Mapping[str, str],
                         ignore: Collection[str] = ()) -> Optional[Snapshot]: ...

    def collect(self, domain: str) -> object: ...

    def publish(self, handle: str, *, domain: Optional[str] = None) -> Snapshot: ...

    def pin(self, handle: str, *, domain: Optional[str] = None) -> bool: ...

    def unpin(self, handle: str, *, domain: Optional[str] = None) -> bool: ...


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _semantics(block: Mapping[str, object]) -> str:
    value = block.get("failure_semantics")
    return value if value in _SEMANTICS else "unknown"


def _positive(value: object, default: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


@dataclass(frozen=True)
class SnapshotPolicy:
    checkpoint_declared: bool = False
    checkpoint_no_change: bool = False
    checkpoint_query: bool = False
    restore_declared: bool = False
    restore_no_change: bool = False
    restore_query: bool = False
    reset_declared: bool = False
    reset_no_change: bool = False
    reset_query: bool = False
    reset_verify: Optional[str] = None
    servable_check: Optional[str] = None
    placement_keys: frozenset[str] = frozenset()
    fresh_within_s: float = DEFAULT_FRESH_WITHIN_S
    required: bool = False
    deadlines: Mapping[str, float] = field(default_factory=dict)

    def deadline(self, name: str) -> float:
        return self.deadlines.get(name, DEFAULT_DEADLINE_S)


def parse_policy(policy: Mapping[str, object]) -> SnapshotPolicy:
    """Read the step policy. Attested fields come from ``policy["profile"]`` only."""
    profile = _mapping(policy.get("profile"))
    checkpoint = _mapping(profile.get("checkpoint"))
    restore = _mapping(profile.get("restore"))
    reset = _mapping(restore.get("reset"))
    name = lambda value: value if isinstance(value, str) and value else None  # noqa: E731
    keys = profile.get("placement_keys")
    deadlines = {key: _positive(value, DEFAULT_DEADLINE_S) for key, value in _mapping(policy.get("deadlines")).items()}
    return SnapshotPolicy(
        checkpoint_declared=bool(checkpoint),
        checkpoint_no_change=_semantics(checkpoint) == "no_change",
        checkpoint_query=checkpoint.get("query") is True,
        restore_declared=bool(restore),
        restore_no_change=_semantics(restore) == "no_change",
        restore_query=restore.get("query") is True,
        reset_declared=bool(reset),
        reset_no_change=_semantics(reset) == "no_change",
        reset_query=reset.get("query") is True,
        reset_verify=name(reset.get("verify")),
        servable_check=name(profile.get("servable_check")),
        placement_keys=frozenset(k for k in keys if isinstance(k, str)) if isinstance(keys, (list, tuple, set, frozenset)) else frozenset(),
        fresh_within_s=_positive(profile.get("fresh_within_s"), DEFAULT_FRESH_WITHIN_S),
        required=policy.get("checkpoint_policy", "best_effort") != "best_effort",
        deadlines=deadlines,
    )


def _log(message: str) -> None:
    print(f"[transition-snapshot] {message}", flush=True)


async def _blocking(function: Callable[..., _T], *args: object) -> _T:
    return await asyncio.to_thread(function, *args)


_LATE_EFFECTS: set["asyncio.Future[EffectResult]"] = set()
"""Strong references to effects still running after their deadline passed (the loop keeps only weak ones)."""


def _log_late_outcome(name: str, future: "asyncio.Future[EffectResult]") -> None:
    """The done-callback of an effect that outlived its deadline: its outcome is stale, so it is
    logged and ignored (decision G10)."""
    _LATE_EFFECTS.discard(future)
    if future.cancelled():
        _log(f"late {name} effect was cancelled after its deadline; outcome ignored")
    elif future.exception() is not None:
        _log(f"late {name} effect failed after its deadline ({type(future.exception()).__name__}); outcome ignored")
    else:
        _log(f"late {name} effect finished after its deadline ({future.result().outcome}); outcome ignored")


async def _effect(call: Awaitable[EffectResult], deadline_s: float, name: str = "effect", *,
                  late: Optional[list["asyncio.Future[EffectResult]"]] = None) -> EffectResult:
    """Run one adapter effect under its deadline. A timeout or an error is ``unknown``: the
    effect was dispatched and its outcome is not known. A dispatched effect is never cancelled
    (transition_ops): on a timeout the call keeps running and its late outcome is logged and ignored.

    A caller that must wait for the running effect (the restore holds a pin on the file it reads) passes
    ``late``; the still-running task is appended to it on a timeout or when the caller is cancelled."""
    task = asyncio.ensure_future(call)
    try:
        done, _ = await asyncio.wait({task}, timeout=deadline_s)
    except asyncio.CancelledError:
        if not task.done():
            _hold_late(task, name, late)
        raise
    if not done:
        _hold_late(task, name, late)
        return EffectResult(outcome="unknown", evidence_ref="deadline_passed")
    try:
        return task.result()
    except Exception as exc:  # noqa: BLE001 - an adapter failure is an unknown outcome
        return EffectResult(outcome="unknown", evidence_ref=f"adapter_error:{type(exc).__name__}")


def _hold_late(task: "asyncio.Future[EffectResult]", name: str, late: Optional[list["asyncio.Future[EffectResult]"]]) -> None:
    _LATE_EFFECTS.add(task)
    task.add_done_callback(lambda finished: _log_late_outcome(name, finished))
    if late is not None:
        late.append(task)


async def _evidence(call: Awaitable[Evidence], deadline_s: float) -> Optional[Evidence]:
    try:
        return await asyncio.wait_for(call, deadline_s)
    except asyncio.TimeoutError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _observed(evidence: Optional[Evidence], operation: Operation, sp: SnapshotPolicy,
              clock: Callable[[], float]) -> Optional[bool]:
    """The observed value, or ``None`` when the evidence is missing, stale or for another incarnation."""
    if evidence is None or evidence.value is None:
        return None
    if clock() - evidence.observed_at > sp.fresh_within_s:
        return None
    if evidence.incarnation is not None and operation.incarnation is not None \
            and evidence.incarnation != operation.incarnation:
        return None
    return evidence.value


class CheckpointStep:
    """D4 row 3. Runs before release, only when declared and the source is resident."""

    name: StepName = "checkpoint"

    def __init__(self, snapshots: SnapshotLifecycle, clock: Callable[[], float]) -> None:
        self._snapshots = snapshots
        self._clock = clock

    def applies(self, operation: Operation, adapter: TransitionAdapter) -> bool:
        return adapter.supports_checkpoint and operation.kind in ("park", "select")

    async def run(self, ctx: StepContext) -> StepVerdict:
        sp = parse_policy(ctx.policy)
        if not sp.checkpoint_declared:
            return "skip"
        operation, domain = ctx.operation, ctx.operation.domain
        adapter: SnapshotAdapter = ctx.adapter  # type: ignore[assignment]
        resident = _observed(await _evidence(adapter.resident(domain), sp.deadline("query")), operation, sp, self._clock)
        if resident is False:
            return "skip"
        intent = StepIntent(step="checkpoint", effect_key=f"{operation.op_id}:checkpoint",
                            expected_incarnation=operation.incarnation)
        await _blocking(ctx.store.write_intent, operation.op_id, intent)
        # Any attempted checkpoint makes the next start cold unless it publishes a new snapshot. The
        # old snapshot is retired here, not after the effect: a crash in between must not leave a
        # published snapshot that predates the attempt (startup_reconcile never touches published rows).
        await _blocking(self._snapshots.retire_published, domain)
        if resident is None:
            return await self._not_taken(ctx, sp, "source_residency_unknown")
        await _blocking(self._snapshots.collect, domain)
        if not await _blocking(self._snapshots.has_room, domain):
            return await self._not_taken(ctx, sp, "out_of_snapshot_storage")
        result = await _effect(adapter.checkpoint(domain, intent), sp.deadline("checkpoint"), "checkpoint")
        if result.outcome == "unknown" and sp.checkpoint_query:
            result = await _effect(adapter.query(domain, intent), sp.deadline("query"), "query")
        if result.outcome == "success":
            return await self._publish(ctx, sp, adapter, result)
        if result.outcome == "no_change" and sp.checkpoint_no_change:
            return await self._not_taken(ctx, sp, "engine_declined")
        # unknown, partial, or a no_change the profile does not attest: the effect is unknown.
        return await self._not_taken(ctx, sp, result.evidence_ref or result.outcome, unknown=True)

    async def _publish(self, ctx: StepContext, sp: SnapshotPolicy, adapter: SnapshotAdapter,
                       result: EffectResult) -> StepVerdict:
        operation, domain = ctx.operation, ctx.operation.domain
        try:
            if not result.handle or result.bytes is None:
                raise SnapshotRejected("the checkpoint reply has no handle or byte count")
            compatibility = await asyncio.wait_for(adapter.compatibility(domain), sp.deadline("query"))
            handle = result.handle
            snapshot = await _blocking(lambda: self._snapshots.pending(
                domain=domain, op_id=operation.op_id, handle=handle, compatibility=compatibility,
                bytes=result.bytes))
            await _blocking(ctx.store.set_snapshot_handle, operation.op_id, snapshot.handle)
            await _blocking(lambda: self._snapshots.publish(snapshot.handle, domain=domain))
        except Exception as exc:  # noqa: BLE001 - the engine is unchanged; the file is not trustworthy
            return await self._not_taken(ctx, sp, f"snapshot_not_published: {exc}")
        await _blocking(ctx.store.write_outcome, operation.op_id, "checkpoint", EffectResult(
            outcome="success", evidence_ref=f"checkpoint:published:{result.handle}", handle=result.handle,
            bytes=result.bytes, correlation_id=result.correlation_id))
        try:
            await _blocking(self._snapshots.collect, domain)
        except Exception as exc:  # noqa: BLE001 - tidying a superseded file never fails the checkpoint
            _log(f"collect after publish failed: {exc}")
        return "continue"

    async def _not_taken(self, ctx: StepContext, sp: SnapshotPolicy, reason: str, *,
                         unknown: bool = False) -> StepVerdict:
        """No usable snapshot was made. Abandon any pending one before release (D4). The published
        pointer was already cleared before the checkpoint was attempted; clearing it again is a no-op
        that keeps the next start cold whatever failed."""
        operation, domain = ctx.operation, ctx.operation.domain
        await _blocking(self._snapshots.abandon_pending, domain, operation.op_id)
        await _blocking(self._snapshots.retire_published, domain)
        try:
            await _blocking(self._snapshots.collect, domain)
        except Exception as exc:  # noqa: BLE001
            _log(f"collect after a failed checkpoint failed: {exc}")
        if unknown:
            await _blocking(ctx.store.write_outcome, operation.op_id, "checkpoint",
                            EffectResult(outcome="unknown", evidence_ref=f"checkpoint:{reason}"))
            return "reconcile"
        if not sp.required:
            await _blocking(ctx.store.write_outcome, operation.op_id, "checkpoint",
                            EffectResult(outcome="no_change", evidence_ref=f"checkpoint:{reason}:cold_next_start"))
            _log(f"checkpoint skipped for {domain} ({reason}); the next start is cold")
            return "continue"
        servable = await self._servable(ctx, sp)
        if servable is None:  # only an unknown outcome may hold reconciling
            await _blocking(ctx.store.write_outcome, operation.op_id, "checkpoint",
                            EffectResult(outcome="unknown", evidence_ref=f"checkpoint:{reason}:servable_unknown"))
            return "reconcile"
        await _blocking(ctx.store.write_outcome, operation.op_id, "checkpoint",
                        EffectResult(outcome="no_change", evidence_ref=f"checkpoint:{reason}:cold_next_start"))
        return "reopen" if servable else "fail_closed"

    async def settle(self, ctx: StepContext, intent: StepIntent, result: EffectResult) -> StepVerdict:
        """Settle a checkpoint outcome the executor learned outside ``run`` (the startup reconcile asked ``query``). It is
        the live path's own settlement: a profile-attested ``no_change`` is a refused save (cleanup, then cold or the
        servable re-check by ``checkpoint_policy``); anything else is an effect of unknown outcome. A success is recorded
        and the operation goes on; no snapshot is published late."""
        sp = parse_policy(ctx.policy)
        if result.outcome == "success":
            await _blocking(ctx.store.write_outcome, ctx.operation.op_id, "checkpoint", result)
            return "continue"
        if result.outcome == "no_change" and sp.checkpoint_no_change:
            return await self._not_taken(ctx, sp, "engine_declined")
        return await self._not_taken(ctx, sp, result.evidence_ref or result.outcome, unknown=True)

    async def recovered(self, ctx: StepContext) -> StepVerdict:
        """The verdict for a checkpoint whose ``no_change`` was recorded and whose operation stopped before the next step
        (the gate write was lost). The record was made after the cleanup. An attested refusal goes on cold under
        ``best_effort`` and is judged by fresh servable evidence under ``required``; an unattested one stays held."""
        sp = parse_policy(ctx.policy)
        if not sp.checkpoint_no_change:
            return "fail_closed"
        if not sp.required:
            return "continue"
        servable = await self._servable(ctx, sp)
        return "reopen" if servable is True else "fail_closed"

    async def _servable(self, ctx: StepContext, sp: SnapshotPolicy) -> Optional[bool]:
        """G7: the profile's ``servable_check``, else the source's resident observation."""
        adapter: SnapshotAdapter = ctx.adapter  # type: ignore[assignment]
        domain = ctx.operation.domain
        call = adapter.observe(domain, sp.servable_check) if sp.servable_check else adapter.resident(domain)
        return _observed(await _evidence(call, sp.deadline("query")), ctx.operation, sp, self._clock)


class RestoreStep:
    """D4 row 6. Runs after load, only when a compatible published snapshot exists."""

    name: StepName = "restore"

    def __init__(self, snapshots: SnapshotLifecycle, clock: Callable[[], float]) -> None:
        self._snapshots = snapshots
        self._clock = clock

    def applies(self, operation: Operation, adapter: TransitionAdapter) -> bool:
        return adapter.supports_checkpoint and operation.kind in ("select", "wake")

    async def run(self, ctx: StepContext) -> StepVerdict:
        sp = parse_policy(ctx.policy)
        if not sp.restore_declared:
            return "skip"
        operation, domain = ctx.operation, ctx.operation.domain
        adapter: SnapshotAdapter = ctx.adapter  # type: ignore[assignment]
        # D4 "After a park": reconcile pending snapshots first; only a published one is restored.
        await _blocking(self._snapshots.abandon_pending, domain, None)
        try:
            await _blocking(self._snapshots.collect, domain)
        except Exception as exc:  # noqa: BLE001
            _log(f"collect before restore failed: {exc}")
        try:
            compatibility = await asyncio.wait_for(adapter.compatibility(domain), sp.deadline("query"))
        except Exception:  # noqa: BLE001 - an unknown token means cold
            return "skip"
        snapshot = await _blocking(lambda: self._snapshots.latest_published(domain, compatibility, sp.placement_keys))
        if snapshot is None:
            return "skip"
        if not await _blocking(lambda: self._snapshots.pin(snapshot.handle, domain=domain)):
            return "skip"  # the snapshot went away after it was read: an unpinned file could be collected mid-restore
        late: list["asyncio.Future[EffectResult]"] = []
        try:
            return await self._restore(ctx, sp, adapter, snapshot.handle, late)
        finally:
            await self._unpin_when_idle(domain, snapshot.handle, late)

    async def _unpin_when_idle(self, domain: str, handle: str, late: list["asyncio.Future[EffectResult]"]) -> None:
        """Release the pin once no restore effect is running. A restore that passed its deadline may still be
        reading the file, so its pin is released from the done-callback of the late task, never before."""
        running = [task for task in late if not task.done()]
        if not running:
            await _blocking(lambda: self._snapshots.unpin(handle, domain=domain))
            return
        loop = asyncio.get_running_loop()
        remaining = len(running)

        def finished(_task: "asyncio.Future[EffectResult]") -> None:
            nonlocal remaining
            remaining -= 1
            if remaining == 0:
                loop.run_in_executor(None, lambda: self._snapshots.unpin(handle, domain=domain))

        for task in running:
            task.add_done_callback(finished)

    async def _restore(self, ctx: StepContext, sp: SnapshotPolicy, adapter: SnapshotAdapter, handle: str,
                       late: list["asyncio.Future[EffectResult]"]) -> StepVerdict:
        operation, domain = ctx.operation, ctx.operation.domain
        intent = StepIntent(step="restore", effect_key=f"{operation.op_id}:restore",
                            expected_incarnation=operation.incarnation)
        await _blocking(ctx.store.write_intent, operation.op_id, intent)
        result = await _effect(adapter.restore(domain, handle, intent), sp.deadline("restore"), "restore", late=late)
        if result.outcome == "unknown" and sp.restore_query:
            result = await _effect(adapter.query(domain, intent), sp.deadline("query"), "query")
        if result.outcome == "success":
            await _blocking(ctx.store.write_outcome, operation.op_id, "restore", EffectResult(
                outcome="success", evidence_ref=f"restore:{handle}", handle=handle,
                correlation_id=result.correlation_id))
            return "continue"
        if result.outcome == "unknown":
            await _blocking(ctx.store.write_outcome, operation.op_id, "restore",
                            EffectResult(outcome="unknown", evidence_ref=f"restore:{result.evidence_ref or 'unknown'}"))
            return "reconcile"
        # The restore failed. The snapshot is not offered again.
        await _blocking(self._snapshots.retire_published, domain)
        if result.outcome == "no_change" and sp.restore_no_change:
            await _blocking(ctx.store.write_outcome, operation.op_id, "restore",
                            EffectResult(outcome="no_change", evidence_ref="restore:declined:serving_cold"))
            _log(f"restore declined for {domain}; serving cold")
            return "continue"
        await _blocking(ctx.store.write_outcome, operation.op_id, "restore",
                        EffectResult(outcome="partial", evidence_ref="restore:failed:state_not_known_clean"))
        return await self._reset(ctx, sp, adapter)

    async def _reset(self, ctx: StepContext, sp: SnapshotPolicy, adapter: SnapshotAdapter) -> StepVerdict:
        """Run the declared reset, verify a clean target, serve cold. No reset declared: stay closed."""
        domain = ctx.operation.domain
        operation = ctx.operation
        if not sp.reset_declared:
            return "fail_closed"
        intent = StepIntent(step="restore", effect_key=f"{operation.op_id}:reset",
                            expected_incarnation=operation.incarnation)
        await _blocking(ctx.store.write_intent, operation.op_id, intent)
        result = await _effect(adapter.reset(domain, intent), sp.deadline("reset"), "reset")
        if result.outcome == "unknown" and sp.reset_query:
            result = await _effect(adapter.query(domain, intent), sp.deadline("query"), "query")
        return await self._after_reset(ctx, sp, adapter, result)

    async def _after_reset(self, ctx: StepContext, sp: SnapshotPolicy, adapter: SnapshotAdapter,
                           result: EffectResult) -> StepVerdict:
        """Judge a reset's outcome: the live path and the startup reconcile share it. Only a clean verification serves cold."""
        operation, domain = ctx.operation, ctx.operation.domain
        if result.outcome == "unknown":
            await _blocking(ctx.store.write_outcome, operation.op_id, "restore",
                            EffectResult(outcome="unknown", evidence_ref="reset:unknown"))
            return "reconcile"
        failed_unchanged = result.outcome == "no_change" and sp.reset_no_change  # G1
        if result.outcome != "success" and not failed_unchanged:
            await _blocking(ctx.store.write_outcome, operation.op_id, "restore",
                            EffectResult(outcome="partial", evidence_ref="reset:failed:state_unknown"))
            return "fail_closed"
        if failed_unchanged and not sp.reset_verify:
            # The restore left the engine dirty and the reset did nothing. Only the profile's own
            # verification can show a clean target; "resident" shows that something is loaded, not that it is clean.
            await _blocking(ctx.store.write_outcome, operation.op_id, "restore",
                            EffectResult(outcome="partial", evidence_ref="reset:failed:no_verify_declared"))
            return "fail_closed"
        clean = await self._clean(ctx, sp, adapter)
        if clean is True:
            await _blocking(ctx.store.write_outcome, operation.op_id, "restore",
                            EffectResult(outcome="no_change", evidence_ref="restore:reset:clean:serving_cold"))
            _log(f"restore failed for {domain}; reset verified clean, serving cold")
            return "continue"
        await _blocking(ctx.store.write_outcome, operation.op_id, "restore", EffectResult(
            outcome="unknown" if clean is None else "partial", evidence_ref="reset:target_not_verified_clean"))
        return "reconcile" if clean is None else "fail_closed"

    async def settle(self, ctx: StepContext, intent: StepIntent, result: EffectResult) -> StepVerdict:
        """Settle a restore or reset outcome the executor learned outside ``run`` (the startup reconcile asked ``query``).
        It is the live path's own settlement, keyed by the effect's identity: a reset is judged by the reset block and its
        clean verification (a dirty or unreachable target never reopens); a restore by the restore block."""
        sp = parse_policy(ctx.policy)
        adapter: SnapshotAdapter = ctx.adapter  # type: ignore[assignment]
        if intent.effect == "reset":
            return await self._after_reset(ctx, sp, adapter, result)
        operation, domain = ctx.operation, ctx.operation.domain
        if result.outcome == "success":
            await _blocking(ctx.store.write_outcome, operation.op_id, "restore", EffectResult(
                outcome="success", evidence_ref=f"restore:{result.evidence_ref or 'recovered'}",
                correlation_id=result.correlation_id))
            return "continue"
        await _blocking(self._snapshots.retire_published, domain)  # a failed restore: the snapshot is not offered again
        if result.outcome == "no_change" and sp.restore_no_change:
            await _blocking(ctx.store.write_outcome, operation.op_id, "restore",
                            EffectResult(outcome="no_change", evidence_ref="restore:declined:serving_cold"))
            return "continue"
        await _blocking(ctx.store.write_outcome, operation.op_id, "restore",
                        EffectResult(outcome="partial", evidence_ref="restore:failed:state_not_known_clean"))
        return await self._reset(ctx, sp, adapter)

    async def _clean(self, ctx: StepContext, sp: SnapshotPolicy, adapter: SnapshotAdapter) -> Optional[bool]:
        """Is the target clean and serving? The profile's reset ``verify`` observation, else (only
        after a reset that succeeded) the resident observation naming the target."""
        domain, target = ctx.operation.domain, ctx.operation.target
        call = adapter.observe(domain, sp.reset_verify) if sp.reset_verify else adapter.resident(domain)
        evidence = await _evidence(call, sp.deadline("query"))
        value = _observed(evidence, ctx.operation, sp, self._clock)
        if value is True and not sp.reset_verify and evidence is not None and target is not None:
            offering = evidence.detail.get("offering")
            if offering is not None and offering != target:
                return False
        return value


def snapshot_steps(snapshots: SnapshotLifecycle, *,
                   clock: Callable[[], float] = time.time) -> dict[StepName, Step]:
    """The step-table entries for checkpoint and restore. The gateway wiring merges them with T1.2's."""
    return {"checkpoint": CheckpointStep(snapshots, clock), "restore": RestoreStep(snapshots, clock)}

"""The transition executor and its steps: close, drain, release, load, open (D2 and D4, step T1.2).

``TransitionExecutor`` runs one operation on a task the caller does not own, under the domain's swap lock.
``submit`` takes the lock, then the store's compare-and-set closes the gate; the task keeps the lock until the
operation ends in a state that leaves the gate ``open``, ``failed_closed`` or ``reconciling``. A caller that
is cancelled after the effect started changes nothing.

Rules the steps keep:

- Write-ahead. An effect step writes its intent in one transaction, calls the adapter, and writes its
  outcome in another. The outcome is written once, after the step verified it.
- An unknown outcome never reopens. A timeout, an adapter error or an ``unknown`` result goes to
  ``adapter.query``; a result that stays unknown holds the operation ``reconciling`` (503 + Retry-After),
  until a query, a restart reconcile or the owner resolves it.
- Release counts as stock released only when the result is ``attested``. The executor does not trust the
  adapter's word for the engine state: after a ``success`` it asks ``adapter.resident`` and needs fresh
  evidence, for the intent's incarnation, that nothing is resident. Stale, missing or foreign evidence is
  ``unknown``; resident evidence is ``partial`` (a residual allocation). Neither settles stock.
- Load verifies the target: fresh evidence that the target itself is resident and ready.
- A ``no_change`` outcome of a step the reviewed profile attests (``failure_semantics: no_change``) means the engine is
  as it was before the step. The step then asks the servable re-check (decision G7: the profile's ``servable_check``
  observation, else the source's resident observation): servable reopens the gate (the operation is ``aborted``), not
  servable holds ``failed_closed``, unknown holds ``reconciling``. A ``no_change`` the profile does not attest holds
  ``failed_closed``. Settle, the startup reconcile and the owner's resolve all use this one rule.
- Recovery from a failed load is a new operation (``compensates=``), never a rollback call.
- Leases live in memory. The open step mints the trigger's lease under the lock and then finishes the
  operation and opens the gate in one transaction (decision G6). Reconcile never mints a lease.

Engine-agnostic: the executor knows domains, targets and adapters, never what an engine is.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from gateway import log_scrub
from gateway.swap_drain import SwapDeferred, SwapDrain, SwapDrainSettings
from gateway.transition_gate import TransitionUnavailable
from gateway.transition_ops import (
    FINISHED_STATES, STEP_ORDER, BeginResult, Begun, Conflict, EffectResult, Evidence, Operation, OperationStore,
    SameOperation, Step, StepContext, StepIntent, StepName, StepVerdict, TransitionAdapter, TransitionKind,
)
from gateway.transition_profiles import attests_no_change
from gateway.transition_store import StaleOutcome, request_hash

REVALIDATE = "revalidate:"
"""Prefix of the evidence a revalidation hold keeps from the outcome it re-judges."""

EFFECT_STEPS: frozenset[str] = frozenset({"checkpoint", "release", "load", "restore"})

DEFAULTS: dict[str, float] = {
    "drain_deadline_s": 60.0,
    "release_deadline_s": 120.0,
    "load_deadline_s": 300.0,
    "checkpoint_deadline_s": 120.0,
    "restore_deadline_s": 300.0,
    "query_deadline_s": 30.0,
    "probe_deadline_s": 5.0,
    "idle_poll_s": 0.5,
    "lock_wait_s": 30.0,
    "retry_after_s": 30.0,
}


DEFAULT_FRESH_WITHIN_S = 2.0
"""How old evidence may be when the reviewed profile does not say. It is not an owner setting."""


def fresh_within(policy: Mapping[str, object]) -> float:
    """``fresh_within_s`` of the reviewed profile, ``policy["profile"]`` only. The snapshot steps read the same key the
    same way, so the two lanes never judge one observation by two ages. A value in the owner's settings is never read."""
    profile = policy.get("profile")
    try:
        value = float(profile.get("fresh_within_s")) if isinstance(profile, Mapping) else DEFAULT_FRESH_WITHIN_S  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_FRESH_WITHIN_S
    return value if value > 0 else DEFAULT_FRESH_WITHIN_S


def setting(policy: Mapping[str, object], key: str) -> float:
    try:
        value = float(policy.get(key, DEFAULTS[key]))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULTS[key]
    return value if value > 0 else DEFAULTS[key]


class InvalidTransition(Exception):
    """The request names a transition that cannot be started (HTTP 422): no offering to wake, a missing target."""


class UnknownDomain(InvalidTransition):
    """The name is not a residency domain key (a service id that resolves to its domain is not one). HTTP 404."""


def evidence_state(evidence: Optional[Evidence], *, now: float, fresh_within_s: float,
                   incarnation: Optional[str]) -> Optional[bool]:
    """``evidence.value`` when it is fresh and from the expected incarnation, else None (unknown).

    D3: evidence older than ``fresh_within_s``, or for another incarnation, counts as unknown.
    """
    if evidence is None or evidence.value is None:
        return None
    if now - evidence.observed_at > fresh_within_s:
        return None
    if incarnation is not None and evidence.incarnation is not None and evidence.incarnation != incarnation:
        return None
    return evidence.value


class TriggerLease:
    """The triggering request's lease, minted by the open step on the executor's task (decision G6).

    The requester passes ``trigger`` to ``submit`` and, once the operation succeeds, ``take()`` returns
    the lease for its own task to record and release. If the requester goes away first, ``abandon()``
    releases a lease already minted and makes a later mint do nothing, so no lease is ever orphaned.
    """

    def __init__(self, mint: Callable[[], Any]) -> None:
        self._mint = mint
        self._lease: Any = None
        self._abandoned = False
        self.minted = 0

    def trigger(self) -> None:
        if self._abandoned or self._lease is not None:
            return
        self._lease = self._mint()
        self.minted += 1

    def take(self) -> Any:
        lease, self._lease = self._lease, None
        return lease

    def abandon(self) -> None:
        self._abandoned = True
        lease = self.take()
        if lease is not None:
            lease.release()


# -- the steps ------------------------------------------------------------------------------------

_LATE_EFFECTS: set["asyncio.Future[Any]"] = set()
"""Effect calls that outlived their deadline. A strong reference keeps each task alive until it ends."""


def _detach(call: "asyncio.Future[Any]", intent: StepIntent) -> None:
    """Let an effect call that is not done finish on its own task; log what it said when it does, and write nothing."""
    if call.done():
        return
    _LATE_EFFECTS.add(call)

    def finished(task: "asyncio.Future[Any]") -> None:
        _LATE_EFFECTS.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        said = f"error {type(error).__name__}" if error is not None else f"outcome {getattr(task.result(), 'outcome', '?')}"
        print(f"[transition-gate] late {said} of {intent.effect_key} ignored (the step settled without it)", flush=True)

    call.add_done_callback(finished)


class _EffectStep:
    """Shared by release and load: intent, call, query on unknown, verify, outcome."""

    name: StepName

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock

    def applies(self, operation: Operation, adapter: TransitionAdapter) -> bool:
        raise NotImplementedError

    def _intent(self, ctx: StepContext) -> StepIntent:
        return StepIntent(step=self.name, effect_key=f"{ctx.operation.op_id}:{self.name}",
                          expected_incarnation=ctx.operation.incarnation)

    async def _call(self, ctx: StepContext, intent: StepIntent) -> EffectResult:
        raise NotImplementedError

    async def _verify(self, ctx: StepContext, intent: StepIntent, result: EffectResult) -> EffectResult:
        """Turn a ``success`` into ``success``, ``partial`` or ``unknown`` by fresh evidence."""
        raise NotImplementedError

    def _deadline(self, ctx: StepContext) -> float:
        return setting(ctx.policy, f"{self.name}_deadline_s")

    async def run(self, ctx: StepContext) -> StepVerdict:
        intent = self._intent(ctx)
        await asyncio.to_thread(ctx.store.write_intent, ctx.operation.op_id, intent)
        # The deadline never cancels a dispatched effect: a cancelled call may be half applied, and the engine's
        # answer arrives anyway. The call keeps running on its own task; its late outcome is only logged (G10).
        call = asyncio.ensure_future(self._call(ctx, intent))
        try:
            done, _ = await asyncio.wait({call}, timeout=self._deadline(ctx))
        except BaseException:
            _detach(call, intent)  # the executor task was cancelled (shutdown): the effect is left alone
            raise
        if not done:
            _detach(call, intent)
            result = EffectResult(outcome="unknown", evidence_ref="deadline")
        else:
            try:
                result = call.result()
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - a cancelled child is unknown, too
                result = EffectResult(outcome="unknown", evidence_ref=f"adapter_error:{type(exc).__name__}")
        if result.outcome == "unknown":
            result = await query_effect(ctx, intent, result)
        return await self.settle(ctx, intent, result)

    async def settle(self, ctx: StepContext, intent: StepIntent, result: EffectResult) -> StepVerdict:
        """Verify ``result``, write the step's outcome once, and say what the executor does next."""
        if result.outcome == "success":
            try:
                result = await self._verify(ctx, intent, result)
            except Exception as exc:  # noqa: BLE001 - evidence that cannot be read is unknown
                result = EffectResult(outcome="unknown", evidence_ref=f"verify_error:{type(exc).__name__}")
        result, verdict = await judge_outcome(ctx, self.name, result, clock=self._clock)
        written = await asyncio.to_thread(ctx.store.write_outcome, ctx.operation.op_id, self.name, result)
        if written.step_outcome != result.outcome:
            # The store ignored the outcome as stale (decision G10): another incarnation, or a settled step.
            # Nothing is learned from it, so the step is unknown.
            result = EffectResult(outcome="unknown", evidence_ref="stale_outcome")
            await asyncio.to_thread(ctx.store.write_outcome, ctx.operation.op_id, self.name, result)
            return verdict_for(result.outcome)
        return verdict


async def no_change_verdict(ctx: StepContext, step: str, *, clock: Callable[[], float] = time.time) -> StepVerdict:
    """What a ``no_change`` outcome of ``step`` holds. Only a no_change the reviewed profile attests is believed; then the
    servable re-check decides: ``reopen`` when the source is confirmed servable, ``fail_closed`` when it is confirmed
    not servable, ``reconcile`` when that is unknown. Anything else is ``fail_closed``."""
    profile = ctx.policy.get("profile")
    if not isinstance(profile, Mapping) or not attests_no_change(profile, step):
        return "fail_closed"
    observation = profile.get("servable_check")
    observe = getattr(ctx.adapter, "observe", None)
    domain = ctx.operation.domain
    try:
        call = observe(domain, observation) if observation and observe is not None else ctx.adapter.resident(domain)
        evidence = await asyncio.wait_for(call, setting(ctx.policy, "probe_deadline_s"))
    except Exception:  # noqa: BLE001 - evidence that cannot be read is unknown
        return "reconcile"
    servable = evidence_state(evidence, now=clock(), fresh_within_s=fresh_within(ctx.policy),
                              incarnation=ctx.operation.incarnation)
    return "reopen" if servable is True else "fail_closed" if servable is False else "reconcile"


async def judge_outcome(ctx: StepContext, step: str, result: EffectResult, *,
                        clock: Callable[[], float] = time.time) -> tuple[EffectResult, StepVerdict]:
    """The outcome to write and the verdict for a settled ``result``. A ``no_change`` whose servable check cannot be read is
    written as ``unknown``: only an unknown outcome may hold ``reconciling``."""
    if result.outcome != "no_change":
        return result, verdict_for(result.outcome)
    verdict = await no_change_verdict(ctx, step, clock=clock)
    if verdict == "reconcile":
        return EffectResult(outcome="unknown", evidence_ref="no_change_servable_unknown",
                            correlation_id=result.correlation_id), verdict
    return result, verdict


def verdict_for(outcome: str) -> StepVerdict:
    """Success goes on; an unknown outcome holds ``reconciling``; anything else holds ``failed_closed``."""
    if outcome == "success":
        return "continue"
    if outcome == "unknown":
        return "reconcile"
    return "fail_closed"


async def query_effect(ctx: StepContext, intent: StepIntent, first: EffectResult) -> EffectResult:
    """Ask the adapter what became of an effect whose outcome is unknown. Still unknown stays unknown."""
    try:
        resolved = await asyncio.wait_for(ctx.adapter.query(ctx.operation.domain, intent),
                                          setting(ctx.policy, "query_deadline_s"))
    except asyncio.TimeoutError:
        return first
    except Exception:  # noqa: BLE001
        return first
    return resolved if resolved.outcome != "unknown" else first


class ReleaseStep(_EffectStep):
    name: StepName = "release"

    def applies(self, operation: Operation, adapter: TransitionAdapter) -> bool:
        return operation.kind in ("park", "select")

    async def _call(self, ctx: StepContext, intent: StepIntent) -> EffectResult:
        return await ctx.adapter.release(ctx.operation.domain, intent)

    async def _verify(self, ctx: StepContext, intent: StepIntent, result: EffectResult) -> EffectResult:
        evidence = await asyncio.wait_for(ctx.adapter.resident(ctx.operation.domain), setting(ctx.policy, "probe_deadline_s"))
        seen = evidence_state(evidence, now=self._clock(), fresh_within_s=fresh_within(ctx.policy),
                              incarnation=intent.expected_incarnation)
        if seen is False:
            return result
        if seen is True:
            return EffectResult(outcome="partial", evidence_ref=result.evidence_ref or "still_resident",
                                correlation_id=result.correlation_id)
        return EffectResult(outcome="unknown", evidence_ref="release_unverified", correlation_id=result.correlation_id)


class LoadStep(_EffectStep):
    name: StepName = "load"

    def applies(self, operation: Operation, adapter: TransitionAdapter) -> bool:
        return operation.kind in ("select", "wake")

    def _intent(self, ctx: StepContext) -> StepIntent:
        # The load may start a new process, so no incarnation is expected before it.
        return StepIntent(step="load", effect_key=f"{ctx.operation.op_id}:load", expected_incarnation=None)

    async def _call(self, ctx: StepContext, intent: StepIntent) -> EffectResult:
        assert ctx.operation.target is not None
        return await ctx.adapter.load(ctx.operation.domain, ctx.operation.target, intent)

    async def _verify(self, ctx: StepContext, intent: StepIntent, result: EffectResult) -> EffectResult:
        evidence = await asyncio.wait_for(ctx.adapter.resident(ctx.operation.domain), setting(ctx.policy, "probe_deadline_s"))
        seen = evidence_state(evidence, now=self._clock(), fresh_within_s=fresh_within(ctx.policy),
                              incarnation=None)
        if seen is None:
            return EffectResult(outcome="unknown", evidence_ref="load_unverified", correlation_id=result.correlation_id)
        detail = evidence.detail if evidence is not None else {}
        if seen is True and detail.get("offering") == ctx.operation.target and detail.get("ready") is True:
            return result
        # Something else is resident, or the target is not ready: the load is partial, never a success.
        return EffectResult(outcome="partial", evidence_ref=result.evidence_ref or "target_not_verified",
                            correlation_id=result.correlation_id)


class DrainStep:
    name: StepName = "drain"

    def __init__(self, drain: SwapDrain, *, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._drain = drain
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep

    def applies(self, operation: Operation, adapter: TransitionAdapter) -> bool:
        return operation.kind in ("park", "select")

    async def run(self, ctx: StepContext) -> StepVerdict:
        operation = ctx.operation
        deadline = setting(ctx.policy, "drain_deadline_s")
        remaining = deadline
        if operation.step == "drain" and operation.step_intent is not None and operation.step_outcome is None:
            # Resumed after a restart: the deadline counts from the first intent, not from now.
            remaining = deadline - (self._clock() - operation.updated)
        intent = StepIntent(step="drain", effect_key=f"{operation.op_id}:drain", expected_incarnation=operation.incarnation)
        await asyncio.to_thread(ctx.store.write_intent, operation.op_id, intent)
        if remaining > 0 and await self._drained(ctx, remaining):
            await asyncio.to_thread(ctx.store.write_outcome, operation.op_id, "drain", EffectResult(outcome="success"))
            return "continue"
        # Decision G2: the deadline passed with no effect started. Nothing changed; the operation aborts, the gate opens.
        await asyncio.to_thread(ctx.store.write_outcome, operation.op_id, "drain",
                                EffectResult(outcome="no_change", evidence_ref="drain_deadline"))
        return "reopen"

    async def _drained(self, ctx: StepContext, remaining: float) -> bool:
        domain = ctx.operation.domain
        end = self._monotonic() + remaining
        try:
            await self._drain.clear_for_swap(domain, SwapDrainSettings(enabled=True, timeout_s=remaining))
        except SwapDeferred:
            return False
        probe = setting(ctx.policy, "probe_deadline_s")
        fresh = fresh_within(ctx.policy)
        while True:
            if await self._nothing_resident(ctx, probe, fresh):
                return True
            if await self._idle(ctx, probe, fresh):
                return True
            left = end - self._monotonic()
            if left <= 0:
                return False
            await self._sleep(min(setting(ctx.policy, "idle_poll_s"), left))

    async def _idle(self, ctx: StepContext, probe: float, fresh: float) -> bool:
        try:
            evidence = await asyncio.wait_for(ctx.adapter.idle(ctx.operation.domain), probe)
        except Exception:  # noqa: BLE001 - unknown idle evidence is not idle
            return False
        return evidence_state(evidence, now=self._clock(), fresh_within_s=fresh,
                              incarnation=ctx.operation.incarnation) is True

    async def _nothing_resident(self, ctx: StepContext, probe: float, fresh: float) -> bool:
        """Fresh evidence that no offering is resident: there is nothing to drain (e.g. the first step of a compensating select)."""
        try:
            evidence = await asyncio.wait_for(ctx.adapter.resident(ctx.operation.domain), probe)
        except Exception:  # noqa: BLE001
            return False
        return evidence_state(evidence, now=self._clock(), fresh_within_s=fresh,
                              incarnation=ctx.operation.incarnation) is False


class OpenStep:
    name: StepName = "open"

    def applies(self, operation: Operation, adapter: TransitionAdapter) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepVerdict:
        operation = ctx.operation
        intent = StepIntent(step="open", effect_key=f"{operation.op_id}:open", expected_incarnation=None)
        await asyncio.to_thread(ctx.store.write_intent, operation.op_id, intent)
        if ctx.trigger is not None and operation.kind in ("select", "wake"):
            ctx.trigger()  # in memory, under the lock, while the gate is still closed
        await asyncio.to_thread(ctx.store.finish, operation.op_id, "succeeded", "open")
        return "continue"


def core_steps(drain: SwapDrain, *, clock: Callable[[], float] = time.time,
               monotonic: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> dict[StepName, Step]:
    """The T1.2 step table: drain, release, load, open. Close is the executor's own ``submit``.
    T1.3 merges its checkpoint and restore steps into this dict."""
    return {"drain": DrainStep(drain, clock=clock, monotonic=monotonic, sleep=sleep),
            "release": ReleaseStep(clock=clock), "load": LoadStep(clock=clock), "open": OpenStep()}


# -- the executor ---------------------------------------------------------------------------------


def _retrieve(task: "asyncio.Future[Any]") -> None:
    """Read a finished task's exception so an abandoned requester's failure is never "never retrieved"."""
    if not task.cancelled():
        task.exception()


@dataclass
class _Running:
    task: "asyncio.Task[None]"


class Executor:
    """Implements ``TransitionExecutor`` (gateway/transition_ops.py)."""

    def __init__(self, *, store: OperationStore, steps: Mapping[StepName, Step],
                 adapter_for: Callable[[str], TransitionAdapter], policy_of: Callable[[str], Mapping[str, object]],
                 lock_for: Callable[[str], asyncio.Lock], config_revision: Callable[[], int] = lambda: 0,
                 snapshots: Any = None, gate_hold: Optional[Callable[[str, str], None]] = None,
                 gate_parked: Optional[Callable[[str], Optional[str]]] = None,
                 is_domain: Callable[[str], bool] = lambda name: True) -> None:
        self.store = store
        self._is_domain = is_domain
        self._steps = steps
        self._adapter_for = adapter_for
        self._policy_of = policy_of
        self._lock_for = lock_for
        self._config_revision = config_revision
        self._snapshots = snapshots
        gates = getattr(store, "gates", None)
        self._gate_hold = gate_hold or (gates.force if gates is not None else (lambda domain, state: None))
        self._gate_parked = gate_parked or (gates.parked if gates is not None else (lambda domain: None))
        self._submit_locks: dict[str, asyncio.Lock] = {}
        self._tasks: dict[str, "asyncio.Task[None]"] = {}
        self._resolve_units: dict["asyncio.Task[Operation]", str] = {}
        self._ready = False
        self._recovery_pending = False  # a durable hold failed: the next admission reconciles again
        self._reconcile_lock: Optional[asyncio.Lock] = None

    # -- submit -------------------------------------------------------------

    async def submit(self, *, domain: str, kind: TransitionKind, idempotency_key: str, target: Optional[str] = None,
                     compensates: Optional[str] = None, trigger: Optional[Callable[[], None]] = None) -> BeginResult:
        try:
            await self.ensure_reconciled()
        except Exception as exc:  # noqa: BLE001 - the table read and then failed mid-reconcile: refuse, never a 500
            print(f"[transition-gate] reconcile before {domain!r} submit failed: {type(exc).__name__}", flush=True)
        if not self._ready or self._recovery_pending:
            # Nothing begins before the leftover operations are settled: the reconcile must never find an
            # operation that a task of this process is driving.
            raise TransitionUnavailable(domain, reason="reconciling", retry_after_s=5)
        mutex = self._submit_locks.setdefault(domain, asyncio.Lock())
        async with mutex:
            return await self._submit_serialized(domain, kind, idempotency_key, target, compensates, trigger)

    async def _submit_serialized(self, domain: str, kind: TransitionKind, key: str, target: Optional[str],
                                 compensates: Optional[str], trigger: Optional[Callable[[], None]]) -> BeginResult:
        if not self._is_domain(domain):
            raise UnknownDomain(f"{domain!r} is not a residency domain key")
        policy = self._policy_of(domain)
        target = await self._resolve_target(domain, kind, target, compensates)
        # A contender is answered without the domain lock: a second POST waits only for the first one's begin
        # (the submit mutex), never for the first transition.
        existing = await asyncio.to_thread(self.store.by_idempotency_key, domain, key)
        if existing is not None:
            same = existing.request_hash == request_hash(kind, target, compensates)
            return SameOperation(existing) if same else Conflict(existing)
        held = await asyncio.to_thread(self.store.current, domain)
        if held is not None:
            return Conflict(held)
        lock = self._lock_for(domain)
        try:
            await asyncio.wait_for(lock.acquire(), setting(policy, "lock_wait_s"))
        except asyncio.TimeoutError:
            raise TransitionUnavailable(domain, reason="lock_busy", retry_after_s=int(setting(policy, "retry_after_s"))) from None
        # Everything from the incarnation read to the task hand-off is one unit on a task of its own. A requester that
        # is cancelled in the middle (a client that hung up) leaves the unit running: it ends with the operation begun
        # and its task owning the lock, or with the lock released. It never ends with a row and no task.
        unit = asyncio.get_running_loop().create_task(
            self._begin_and_hand_off(domain, kind, key, target, compensates, trigger, policy, lock))
        unit.add_done_callback(_retrieve)
        return await asyncio.shield(unit)

    async def _begin_and_hand_off(self, domain: str, kind: TransitionKind, key: str, target: Optional[str],
                                  compensates: Optional[str], trigger: Optional[Callable[[], None]],
                                  policy: Mapping[str, object], lock: asyncio.Lock) -> BeginResult:
        """Runs with ``lock`` held. A new operation's task takes the lock over; in every other end it is released here."""
        handed_off = False
        try:
            incarnation = await self._incarnation(domain, policy)
            result = await asyncio.to_thread(
                self.store.begin, domain=domain, kind=kind, idempotency_key=key, target=target,
                incarnation=incarnation, config_revision=self._config_revision(), compensates=compensates)
            if isinstance(result, Begun):
                task = asyncio.get_running_loop().create_task(self._run(result.operation, lock, trigger, after=None))
                self._tasks[result.operation.op_id] = task
                task.add_done_callback(lambda _t, op_id=result.operation.op_id: self._tasks.pop(op_id, None))
                handed_off = True
            return result
        finally:
            if not handed_off:
                lock.release()

    async def _resolve_target(self, domain: str, kind: TransitionKind, target: Optional[str],
                              compensates: Optional[str]) -> Optional[str]:
        if kind == "park":
            return await self._resident_offering(domain)
        if kind == "select" and not target:
            raise InvalidTransition("a select needs a target offering")
        if kind == "wake":
            target = target or await asyncio.to_thread(self.store.last_target, domain)
            if not target:
                raise InvalidTransition(f"domain {domain!r} has no offering to wake")
        if compensates is not None:
            origin = await asyncio.to_thread(self.store.get, compensates)
            if origin is None or origin.domain != domain or origin.state != "failed_closed":
                raise InvalidTransition(f"{compensates!r} is not a failed_closed operation of domain {domain!r}")
        return target

    async def _incarnation(self, domain: str, policy: Mapping[str, object]) -> Optional[str]:
        try:
            evidence = await asyncio.wait_for(self._adapter_for(domain).resident(domain), setting(policy, "probe_deadline_s"))
        except Exception:  # noqa: BLE001 - no incarnation known; the evidence checks then need none
            return None
        return evidence.incarnation

    async def _resident_offering(self, domain: str) -> Optional[str]:
        """The offering resident now, as the engine reports it (what a park releases), else None."""
        try:
            evidence = await asyncio.wait_for(self._adapter_for(domain).resident(domain),
                                              setting(self._policy_of(domain), "probe_deadline_s"))
        except Exception:  # noqa: BLE001
            return None
        offering = evidence.detail.get("offering") if evidence.value else None
        return offering if isinstance(offering, str) and offering else None

    # -- running ------------------------------------------------------------

    async def wait(self, op_id: str) -> Operation:
        """Wait for the operation's task (if it runs here), then return its record."""
        task = self._tasks.get(op_id)
        if task is not None:
            await asyncio.shield(task)
        found = await asyncio.to_thread(self.store.get, op_id)
        assert found is not None
        return found

    async def _run(self, operation: Operation, lock: asyncio.Lock, trigger: Optional[Callable[[], None]],
                   after: Optional[StepName]) -> None:
        try:
            await self._drive(operation, trigger, after)
        except asyncio.CancelledError:
            # Shutdown: the next admission in this process must reconcile the still-running durable row, too.
            self._recovery_pending = True
            self._gate_hold(operation.domain, "reconciling")
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[transition-gate] operation {operation.op_id} stopped by {log_scrub.describe_error(exc)}", flush=True)
            await self._hold(operation)
        finally:
            lock.release()

    async def _hold(self, operation: Operation) -> None:
        """An unexpected error: keep the gate closed, and say what is known.

        An effect with no outcome is unknown (``reconciling``). A failure after an effect settled leaves the
        engine changed and the operation unfinished: ``failed_closed``, for a compensating operation. A failure
        before any effect changed nothing: the operation aborts and the gate opens.
        """
        try:
            current = await asyncio.to_thread(self.store.get, operation.op_id)
            if current is None or current.state in FINISHED_STATES:
                return
            outstanding = current.step_intent is not None and current.step_outcome in (None, "unknown")
            if outstanding:
                if current.step_outcome is None:
                    await asyncio.to_thread(self.store.write_outcome, current.op_id, current.step_intent.step,  # type: ignore[union-attr]
                                            EffectResult(outcome="unknown", evidence_ref="executor_error"))
                if current.state != "reconciling":
                    await asyncio.to_thread(self.store.finish, current.op_id, "reconciling", "reconciling")
            elif current.step in EFFECT_STEPS or current.step == "open":
                await asyncio.to_thread(self.store.finish, current.op_id, "failed_closed", "failed_closed")
            else:
                await asyncio.to_thread(self.store.finish, current.op_id, "aborted", "open")
        except Exception as exc:  # noqa: BLE001 - the table cannot be written: hold the gate in memory
            print(f"[transition-gate] cannot record the hold of {operation.op_id}: {type(exc).__name__}", flush=True)
            self._gate_hold(operation.domain, "reconciling")
            # The row still says running. Keep the in-memory hold until a durable write succeeds, and have the next
            # admission reconcile before it accepts work, so storage coming back never leaves a wedged domain.
            self._recovery_pending = True

    def _order(self) -> list[StepName]:
        return [name for name in STEP_ORDER if name in self._steps]

    async def _drive(self, operation: Operation, trigger: Optional[Callable[[], None]], after: Optional[StepName]) -> None:
        adapter = self._adapter_for(operation.domain)
        policy = self._policy_of(operation.domain)
        order = [name for name in self._order() if after is None or STEP_ORDER.index(name) > STEP_ORDER.index(after)]
        current = operation
        # A resume past the load did not run the load's own check: the open boundary looks at the load itself, after
        # any skipped or intermediate step, and also when the owner resolved the open step (nothing is left to run).
        unchecked = (after is not None and operation.kind in ("select", "wake")
                     and STEP_ORDER.index(after) >= STEP_ORDER.index("load"))
        for name in order:
            step = self._steps[name]
            if not step.applies(current, adapter):
                continue
            if name == "open" and unchecked and await self._hold_unloaded(current, adapter, policy):
                return
            ctx = StepContext(operation=current, store=self.store, adapter=adapter, snapshots=self._snapshots,
                              policy=policy, trigger=trigger)
            verdict = await step.run(ctx)
            refreshed = await asyncio.to_thread(self.store.get, current.op_id)
            assert refreshed is not None
            current = refreshed
            if verdict in ("continue", "skip"):
                if current.state != "running":
                    return
                continue
            await self._apply_verdict(current, verdict)
            return
        if current.state == "running":
            if unchecked and "open" not in order and await self._hold_unloaded(current, adapter, policy):
                return
            await asyncio.to_thread(self.store.finish, current.op_id, "succeeded", "open")

    async def _apply_verdict(self, operation: Operation, verdict: StepVerdict) -> None:
        state, gate = {"reopen": ("aborted", "open"), "fail_closed": ("failed_closed", "failed_closed"),
                       "reconcile": ("reconciling", "reconciling")}[verdict]
        await asyncio.to_thread(self.store.finish, operation.op_id, state, gate)  # type: ignore[arg-type]

    # -- recovery -----------------------------------------------------------

    def reconciled(self) -> bool:
        return self._ready and not self._recovery_pending

    async def ensure_reconciled(self) -> None:
        """Run ``reconcile_startup`` once, the first time the transition table reads as readable.

        The gateway's start-up hook skips the reconcile while the table is unreadable and nothing else would run
        it later: an operation left ``running`` by the last process would hold its domain closed for good. Every
        entry (``submit``, ``ensure_awake``, and the gateway's admission) calls this; it is a flag read once the
        reconcile has run, and a no-op while the table still cannot be read. One lock makes it run once.
        """
        if self._ready and not self._recovery_pending:
            return
        gates = getattr(self.store, "gates", None)
        if gates is not None and not gates.readable():
            await asyncio.to_thread(gates.refresh_if_due)
            if not gates.readable():
                return
        if self._reconcile_lock is None:
            self._reconcile_lock = asyncio.Lock()
        async with self._reconcile_lock:
            if not self._ready or self._recovery_pending:
                await self.reconcile_startup()

    async def reconcile_startup(self) -> None:
        """Before any admission: settle or hold every unfinished operation (D2 "Startup").

        An effect with an intent and no outcome is asked of ``adapter.query``. A resolved outcome is
        written and the operation goes on with its next step; an unresolved one holds ``reconciling``.
        An operation that stopped between steps, with no effect outstanding, goes on from where it was
        (the drain counts its deadline from its first intent). The gate of every such domain is already
        ``closed`` or ``reconciling`` in the table, so nothing is admitted while this runs.
        """
        self._ready = False  # no admission may observe a partially completed reconciliation
        try:
            leftover = await asyncio.to_thread(self.store.unfinished)
            self._recovery_pending = False  # a hold that cannot be written sets it again
            for operation in leftover:
                if operation.op_id in self._tasks or operation.op_id in self._resolve_units.values():
                    continue  # a task of this process drives it; only an operation nothing drives is recovered
                try:
                    await self._recover(operation)
                except Exception as exc:  # noqa: BLE001
                    print(f"[transition-gate] cannot recover {operation.op_id}: {log_scrub.describe_error(exc)}", flush=True)
                    await self._hold(operation)
        except BaseException:
            # Cancellation (or an error escaping recovery) did not finish the pass.
            self._recovery_pending = True
            raise
        self._ready = True

    async def _recover(self, operation: Operation) -> None:
        domain = operation.domain
        try:
            adapter = self._adapter_for(domain)
        except Exception as exc:  # noqa: BLE001 - no adapter for the domain any more: hold, never open
            print(f"[transition-gate] {domain!r} has no adapter ({type(exc).__name__}); holding {operation.op_id}", flush=True)
            await self._hold(operation)
            return
        policy = self._policy_of(domain)
        intent = operation.step_intent
        outstanding = intent is not None and operation.step_outcome in (None, "unknown") and operation.step in EFFECT_STEPS
        after: Optional[StepName] = None
        if outstanding and intent is not None and intent.effect_key.endswith(":revalidate"):
            await self._revalidate(operation, adapter, policy)
            return
        if outstanding:
            assert intent is not None
            step = self._steps.get(operation.step)  # type: ignore[arg-type]
            result = await query_effect(
                StepContext(operation=operation, store=self.store, adapter=adapter, snapshots=self._snapshots, policy=policy),
                intent, EffectResult(outcome="unknown", evidence_ref="restart"))
            if operation.state == "running" and result.outcome == "unknown":
                # The effect may have happened: write the unknown outcome so the record says so, then hold.
                await asyncio.to_thread(self.store.write_outcome, operation.op_id, intent.step, result)
                await asyncio.to_thread(self.store.finish, operation.op_id, "reconciling", "reconciling")
                return
            if result.outcome == "unknown":
                return  # already reconciling, still unknown
            if operation.state == "reconciling":
                # The step may write a new intent (a restore that goes on to its reset): only a running operation takes one.
                operation = await asyncio.to_thread(self.store.resume, operation.op_id)
            verdict = await self._settle(step, operation, adapter, policy, intent, result)
            if verdict not in ("continue", "skip"):
                await self._finish_for(operation, verdict)
                return
            after = intent.step
        elif operation.state == "reconciling":
            if operation.step_outcome in (None, "unknown") or operation.step_intent is None:
                return  # reconciling without a settled outcome: only the owner resolves it
            # The outcome was settled (by the owner or a revalidation) and the process died before the resume. Go on as
            # the live path would have, through the same checks.
            verdict = "continue" if operation.step_outcome == "success" else (
                "fail_closed" if operation.step_outcome == "partial" else
                await self._settled_verdict(operation, adapter, policy))
            if verdict == "reconcile":
                resumed = await asyncio.to_thread(self.store.resume, operation.op_id)
                await self._hold_revalidation(resumed)
                await asyncio.to_thread(self.store.finish, operation.op_id, "reconciling", "reconciling")
                return
            if verdict != "continue":
                await self._finish_for(operation, verdict)
                return
            after = operation.step_intent.step
        else:
            if operation.step_outcome in ("no_change", "partial"):
                # The step ended badly and the gate write was lost: judge it as the live path would have.
                verdict = await self._lost_gate_write_verdict(operation, adapter, policy)
                if verdict != "continue":
                    await self._finish_for(operation, verdict)
                    return
            after = self._resume_point(operation)
        await self._resume_from(operation.op_id, adapter, policy, after)

    async def _resume_from(self, op_id: str, adapter: TransitionAdapter, policy: Mapping[str, object],
                           after: Optional[StepName]) -> None:
        """Every recovery that goes on runs through here, on the stored row. The open-boundary check is in ``_drive``."""
        current = await asyncio.to_thread(self.store.get, op_id)
        assert current is not None
        if current.state == "reconciling":
            current = await asyncio.to_thread(self.store.resume, op_id)
        self._launch_resume(current, after)

    async def _hold_unloaded(self, operation: Operation, adapter: TransitionAdapter, policy: Mapping[str, object]) -> bool:
        """At the open boundary of a resumed select or wake, look at the load again (G14): the target must be resident,
        the offering and ready now. Otherwise hold ``reconciling`` at the open step and return True. The load was verified
        before the restart, or never seen at all; the engine may have died or not become ready since."""
        if await self._load_still_verified(operation, adapter, policy):
            return False
        print(f"[transition-gate] {operation.op_id} resumes at open but the target is not resident and ready now; holding",
              flush=True)
        held = StepIntent(step="open", effect_key=f"{operation.op_id}:open", expected_incarnation=None)
        await asyncio.to_thread(self.store.write_intent, operation.op_id, held,
                                EffectResult(outcome="unknown", evidence_ref="load_unverified_at_resume"))
        await asyncio.to_thread(self.store.finish, operation.op_id, "reconciling", "reconciling")
        return True

    async def _settled_verdict(self, operation: Operation, adapter: TransitionAdapter, policy: Mapping[str, object]) -> StepVerdict:
        """The verdict for a step whose ``no_change`` or ``partial`` outcome is recorded but whose gate verdict was not
        applied. A drain reopens. A step with a ``recovered`` rule (checkpoint, restore) judges it itself. Another effect
        step's ``no_change`` follows the profile's attestation and the servable rule. ``reconcile`` means the check could
        not be read: the caller holds a revalidation, it never closes the gate on a missing answer."""
        if operation.step == "drain":
            return "reopen"
        ctx = StepContext(operation=operation, store=self.store, adapter=adapter, snapshots=self._snapshots, policy=policy)
        recovered = getattr(self._steps.get(operation.step), "recovered", None)  # type: ignore[arg-type]
        if recovered is not None:
            return await recovered(ctx)
        if operation.step_outcome == "no_change" and operation.step in EFFECT_STEPS:
            return await no_change_verdict(ctx, operation.step)
        return "fail_closed"

    async def _lost_gate_write_verdict(self, operation: Operation, adapter: TransitionAdapter,
                                       policy: Mapping[str, object]) -> StepVerdict:
        verdict = await self._settled_verdict(operation, adapter, policy)
        if verdict == "reconcile":
            # The step's own recovery may have gone further (a restore that ran its declared reset): hold what is owed now,
            # never the outcome it started from, or the effect would run again on the next restart.
            now = await asyncio.to_thread(self.store.get, operation.op_id)
            owed = getattr(self._steps.get(operation.step), "revalidation_owed", None)  # type: ignore[arg-type]
            judged = owed(now) if owed is not None and now is not None else None
            if judged is None and now is not None and now.step_outcome == "unknown":
                # The step's recovery dispatched an effect and its outcome is unknown: the row already holds exactly that.
                # The next restart queries that intent; a revalidation hold built from the old outcome would run it again.
                return verdict
            await self._hold_revalidation(operation, judged)
        return verdict

    async def _hold_revalidation(self, operation: Operation, judged: Optional[EffectResult] = None) -> None:
        """Hold ``reconciling`` over a recorded outcome whose servability could not be read. A settled outcome is never
        overwritten: a revalidation intent for the same step, with an unknown outcome, carries the hold. The recorded
        outcome and evidence are kept in the new record (``revalidate:<outcome>:<evidence>``), so a later restart judges
        the same outcome again under the same rule. ``judged`` replaces the recorded pair when the step owes a different one."""
        assert operation.step_intent is not None
        revalidate = StepIntent(step=operation.step_intent.step,  # type: ignore[arg-type]
                                effect_key=f"{operation.op_id}:{operation.step_intent.step}:revalidate",
                                expected_incarnation=operation.step_intent.expected_incarnation)
        # One transaction: a crash between two writes would lose the recorded evidence the next judgment needs.
        outcome = judged.outcome if judged is not None else (operation.step_outcome or "no_change")
        evidence = judged.evidence_ref if judged is not None else operation.evidence_ref
        await asyncio.to_thread(self.store.write_intent, operation.op_id, revalidate, EffectResult(
            outcome="unknown", evidence_ref=f"{REVALIDATE}{outcome}:{evidence or ''}"))

    async def _revalidate(self, operation: Operation, adapter: TransitionAdapter, policy: Mapping[str, object]) -> None:
        """Judge a recorded outcome again after a restart. Fresh evidence settles it as the live path would have; an
        unreadable check keeps the hold."""
        held_outcome, _, recorded = (operation.evidence_ref or "").removeprefix(REVALIDATE).partition(":")
        if held_outcome not in ("no_change", "partial"):
            # A hold from the format before the outcome was recorded in it: the recorded pair is the evidence alone.
            # (Typed holds, ``revalidate:<outcome>:...``, were never released: no store holds an older typed form.)
            recorded = (operation.evidence_ref or "").removeprefix(REVALIDATE)
            owed = getattr(self._steps.get(operation.step), "revalidation_owed", None)  # type: ignore[arg-type]
            legacy = owed(dataclasses.replace(operation, step_outcome="unknown", evidence_ref=recorded)) if owed else None
            held_outcome, recorded = (legacy.outcome, legacy.evidence_ref or "") if legacy is not None else ("no_change", recorded)
        kept = dataclasses.replace(operation, step_outcome=held_outcome, evidence_ref=recorded)
        verdict = await self._settled_verdict(kept, adapter, policy)
        if verdict == "reconcile":
            if operation.state == "running":
                await self._finish_for(operation, "reconcile")
            return
        step = operation.step_intent.step  # type: ignore[union-attr]
        await asyncio.to_thread(self.store.write_outcome, operation.op_id, step,
                                EffectResult(outcome=held_outcome, evidence_ref=recorded or None))  # type: ignore[arg-type]
        if verdict != "continue":
            await self._finish_for(operation, verdict)
            return
        await self._resume_from(operation.op_id, adapter, policy, step)

    async def _load_still_verified(self, operation: Operation, adapter: TransitionAdapter,
                                   policy: Mapping[str, object]) -> bool:
        """The load step's own check, run again now with a fresh ``resident`` probe: the target is resident and ready."""
        load = self._steps.get("load")
        verify = getattr(load, "_verify", None)
        if verify is None:
            return True  # a merged step table without a load step has nothing to check
        ctx = StepContext(operation=operation, store=self.store, adapter=adapter, snapshots=self._snapshots, policy=policy)
        try:
            checked = await verify(ctx, load._intent(ctx), EffectResult(outcome="success", evidence_ref="resume"))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - evidence that cannot be read is not success
            return False
        return checked.outcome == "success"

    async def _settle(self, step: Optional[Step], operation: Operation, adapter: TransitionAdapter,
                      policy: Mapping[str, object], intent: StepIntent, result: EffectResult) -> StepVerdict:
        ctx = StepContext(operation=operation, store=self.store, adapter=adapter, snapshots=self._snapshots, policy=policy)
        settle = getattr(step, "settle", None)
        if settle is not None:
            return await settle(ctx, intent, result)
        result, verdict = await judge_outcome(ctx, intent.step, result)
        written = await asyncio.to_thread(self.store.write_outcome, operation.op_id, intent.step, result)
        return verdict if written.step_outcome == result.outcome else verdict_for(written.step_outcome or "unknown")

    def _resume_point(self, operation: Operation) -> Optional[StepName]:
        """The step to go on after: the last one that ended, or None to start from the first.

        A drain or open step with an intent and no outcome starts again: it has no effect to repeat.
        """
        if operation.step in ("close", None):
            return None
        if operation.step_outcome is None:
            earlier = [name for name in self._order() if STEP_ORDER.index(name) < STEP_ORDER.index(operation.step)]  # type: ignore[arg-type]
            return earlier[-1] if earlier else None
        return operation.step

    def _launch_resume(self, operation: Operation, after: Optional[StepName]) -> None:
        lock = self._lock_for(operation.domain)

        async def go() -> None:
            await lock.acquire()  # at start-up nobody holds it; later, the resolve waits its turn
            await self._run(operation, lock, None, after)

        task = asyncio.get_running_loop().create_task(go())
        self._tasks[operation.op_id] = task
        task.add_done_callback(lambda _t, op_id=operation.op_id: self._tasks.pop(op_id, None))

    async def _finish_for(self, operation: Operation, verdict: StepVerdict) -> None:
        current = await asyncio.to_thread(self.store.get, operation.op_id)
        assert current is not None
        if current.state in FINISHED_STATES:
            return
        await self._apply_verdict(current, verdict)

    async def resolve(self, op_id: str, *, step: StepName, outcome: str, evidence_ref: Optional[str]) -> Operation:
        """The owner settles a held step (decision G3). Only a reconciling operation, at that step, takes it.

        ``success`` lets the operation go on with its next step; ``unknown`` changes nothing; ``no_change``
        follows the servable rule (see the module notes) when the profile attests it for the step, and holds
        ``failed_closed`` otherwise; any other outcome holds ``failed_closed``. A stale call raises ``StaleOutcome`` and changes nothing.
        """
        operation = await asyncio.to_thread(self.store.get, op_id)
        if operation is None:
            raise KeyError(op_id)
        if operation.state != "reconciling" or operation.step != step or operation.step_intent is None:
            raise StaleOutcome(f"operation {op_id} is {operation.state} at step {operation.step!r}; nothing to resolve at {step!r}")
        # A caller can go away after any store commit. Settlement and task hand-off belong to the executor,
        # just like begin: cancellation of the caller must not abandon an unfinished operation.
        unit = asyncio.get_running_loop().create_task(
            self._resolve_and_hand_off(operation, step, outcome, evidence_ref))
        self._resolve_units[unit] = op_id

        def finished(task: "asyncio.Task[Operation]") -> None:
            self._resolve_units.pop(task, None)
            _retrieve(task)

        unit.add_done_callback(finished)
        return await asyncio.shield(unit)

    async def _resolve_and_hand_off(self, operation: Operation, step: StepName, outcome: str,
                                    evidence_ref: Optional[str]) -> Operation:
        """Settle the owner answer and hand off any resumed row without a caller-owned cancellation gap."""
        op_id = operation.op_id
        try:
            result = EffectResult(outcome=outcome, evidence_ref=evidence_ref)  # type: ignore[arg-type]
            ctx = StepContext(operation=operation, store=self.store, adapter=self._adapter_for(operation.domain),
                              snapshots=self._snapshots, policy=self._policy_of(operation.domain))
            verdict = verdict_for(outcome)
            if outcome == "no_change":
                # The owner says the step changed nothing: believe it as a profile-attested no_change is believed. A check
                # that cannot be read changes nothing, as an "unknown" resolve does.
                verdict = await no_change_verdict(ctx, step)
                if verdict == "reconcile":
                    return operation
            written = await asyncio.to_thread(self.store.write_outcome, op_id, step, result)
            if written.step_outcome != outcome:
                raise StaleOutcome(f"operation {op_id}: the outcome was refused as stale")
            if verdict == "reconcile":
                return written
            if verdict != "continue":
                await self._apply_verdict(written, verdict)
                return (await asyncio.to_thread(self.store.get, op_id))  # type: ignore[return-value]
            resumed = await asyncio.to_thread(self.store.resume, op_id)
            self._launch_resume(resumed, step)
            return resumed
        except StaleOutcome:
            raise
        except BaseException:
            # A store call may have committed before raising, or task creation may have failed. The next
            # admission must recover any row left without a driver, even if this process was already ready.
            self._recovery_pending = True
            self._gate_hold(operation.domain, "reconciling")
            raise

    # -- a parked domain ----------------------------------------------------

    async def ensure_awake(self, domain: str, *, wait_s: float, trigger: Optional[Callable[[], None]] = None) -> Optional[Operation]:
        """If ``domain`` is parked, wake it and wait up to ``wait_s`` (D4 "After a park").

        The wake key is ``wake-<park op id>``, so every request that finds the domain parked joins one
        wake. Only the request whose submit began the wake passes its ``trigger``; the wake mints that
        lease under the lock before the gate opens. Returns the finished wake, or None when the domain
        was not parked. Raises ``TransitionUnavailable`` when the wake is not done in time or did not succeed.
        """
        await self.ensure_reconciled()
        try:
            parked_by = self._gate_parked(domain)
            if parked_by is None:
                return None
            policy = self._policy_of(domain)
            retry = int(setting(policy, "retry_after_s"))
        except Exception as exc:  # noqa: BLE001 - an unreadable gate or policy is a refusal, never a 500
            print(f"[transition-gate] wake of {domain!r} not checkable: {log_scrub.describe_error(exc)}", flush=True)
            raise TransitionUnavailable(domain, reason="wake_unavailable", retry_after_s=5) from None
        try:
            began = await self.submit(domain=domain, kind="wake", idempotency_key=f"wake-{parked_by}", trigger=trigger)
        except TransitionUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - an invalid wake or an unreadable store is a refusal, never a 500
            print(f"[transition-gate] wake of {domain!r} refused: {log_scrub.describe_error(exc)}", flush=True)
            raise TransitionUnavailable(domain, reason="wake_unavailable", retry_after_s=retry) from None
        if isinstance(began, Conflict):
            raise TransitionUnavailable(domain, reason="transition_in_progress", retry_after_s=retry)
        try:
            finished = await asyncio.wait_for(self.wait(began.operation.op_id), wait_s)
        except asyncio.TimeoutError:
            raise TransitionUnavailable(domain, reason="wake_in_progress", retry_after_s=retry) from None
        except Exception as exc:  # noqa: BLE001 - the read of the finished record failed: a refusal, never a 500
            print(f"[transition-gate] wake of {domain!r} not readable: {log_scrub.describe_error(exc)}", flush=True)
            raise TransitionUnavailable(domain, reason="wake_unavailable", retry_after_s=retry) from None
        if finished.state != "succeeded":
            raise TransitionUnavailable(domain, reason=f"wake_{finished.state}", retry_after_s=retry)
        return finished

    def active(self) -> Sequence[str]:
        return tuple(self._tasks)

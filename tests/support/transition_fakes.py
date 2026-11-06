"""Two differently shaped fake transition adapters and a harness for the executor tests (T1.2).

Both adapters drive one simulated engine (``EngineSim``) and implement ``TransitionAdapter``:

- ``DirectAdapter`` returns each result itself, after a delay. Its engine flags are read straight out
  into ``Evidence``. Its ``query`` reads an effect ledger and says ``unknown`` when it was never seen.
- ``LedgerAdapter`` never returns a result from ``release`` or ``load``: it answers ``unknown`` at once and
  settles the effect in the background. Only ``query`` (a long poll on the ledger) learns the outcome.
  Its engine answers are wrapped (``{"result": ..., "meta": {...}}``), readiness lags the load, and a
  release reports stock as a count that must reach zero.

Neither names an engine; the domain, the offerings and the handles are opaque strings.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from gateway.swap_drain import SwapDrain
from gateway.transition_executor import Executor, core_steps
from gateway.transition_gate import GateStore
from gateway.transition_ops import EffectResult, Evidence, StepIntent
from gateway.transition_store import SqliteOperationStore


class Clock:
    """Wall time the tests move by hand; the executor's evidence checks read it."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@dataclass
class EngineSim:
    """The simulated engine process behind a domain."""

    clock: Clock
    offering: Optional[str] = "alpha"
    ready: bool = True
    incarnation: str = "inc-1"
    busy: int = 0
    stock: int = 1
    ledger: dict[str, str] = field(default_factory=dict)       # effect key -> applied | pending | refused
    faults: dict[str, str] = field(default_factory=dict)       # step -> one-shot fault mode
    evidence_fault: Optional[str] = None                       # one-shot: stale | other_incarnation | unknown
    fault_after: Optional[str] = None                          # the fault arms after this effect was called
    evidence_sticky: bool = False                              # keep the evidence fault on every read
    calls: list[tuple[str, str]] = field(default_factory=list)
    delay: float = 0.0
    cancelled: list[str] = field(default_factory=list)         # effect calls that were cancelled while they ran

    async def wait(self, seconds: float, what: str) -> None:
        """The time an effect takes inside the engine. A cancellation here is recorded: no deadline may cancel an effect."""
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            self.cancelled.append(what)
            raise

    def take_fault(self, step: str) -> Optional[str]:
        return self.faults.pop(step, None)

    def arm_evidence(self, mode: str, after: str = "release", *, sticky: bool = False) -> None:
        """The next evidence read after ``after`` was called is stale, from another incarnation, or unknown.
        ``sticky`` keeps every later read faulty too."""
        self.evidence_fault, self.fault_after, self.evidence_sticky = mode, after, sticky

    def evidence(self, *, value: Optional[bool], detail: Mapping[str, object]) -> Evidence:
        mode = None
        if self.evidence_fault and (self.fault_after is None or any(name == self.fault_after for name, _ in self.calls)):
            mode = self.evidence_fault
            if not self.evidence_sticky:
                self.evidence_fault = None
        observed = self.clock()
        incarnation: Optional[str] = self.incarnation
        if mode == "stale":
            observed -= 3600
        elif mode == "other_incarnation":
            incarnation = "inc-other"
        elif mode == "unknown":
            return Evidence(value=None, observed_at=observed, incarnation=incarnation, detail=detail)
        return Evidence(value=value, observed_at=observed, incarnation=incarnation, detail=detail)


class DirectAdapter:
    supports_checkpoint = False

    def __init__(self, sim: EngineSim, *, attested: bool = True, can_query: bool = True) -> None:
        self.sim = sim
        self.attested = attested
        self.can_query = can_query

    async def idle(self, domain: str) -> Evidence:
        self.sim.calls.append(("idle", ""))
        return self.sim.evidence(value=self.sim.busy == 0, detail={})

    async def resident(self, domain: str) -> Evidence:
        self.sim.calls.append(("resident", ""))
        sim = self.sim
        return sim.evidence(value=sim.offering is not None, detail={"offering": sim.offering, "ready": sim.ready})

    async def release(self, domain: str, intent: StepIntent) -> EffectResult:
        sim = self.sim
        sim.calls.append(("release", intent.effect_key))
        fault = sim.take_fault("release")
        await sim.wait(sim.delay, intent.effect_key)
        if fault == "hang":
            await sim.wait(3600, intent.effect_key)
        if fault == "fail":
            sim.ledger[intent.effect_key] = "refused"
            return EffectResult(outcome="no_change", evidence_ref="release-refused")
        if fault == "residual":
            sim.ledger[intent.effect_key] = "applied"
            return EffectResult(outcome="partial", evidence_ref="residual", attested=self.attested)
        sim.offering, sim.ready = None, False
        sim.ledger[intent.effect_key] = "applied"
        if fault == "lost_reply":
            await sim.wait(3600, intent.effect_key)
        return EffectResult(outcome="success", evidence_ref=f"release:{intent.effect_key}", attested=self.attested)

    async def load(self, domain: str, target: str, intent: StepIntent) -> EffectResult:
        sim = self.sim
        sim.calls.append(("load", f"{intent.effect_key}:{target}"))
        fault = sim.take_fault("load")
        await sim.wait(sim.delay, intent.effect_key)
        if fault == "hang":
            await sim.wait(3600, intent.effect_key)
        if fault == "fail":
            sim.ledger[intent.effect_key] = "refused"
            return EffectResult(outcome="no_change", evidence_ref="load-refused")
        if fault == "partial":
            sim.offering, sim.ready = target, False
            sim.ledger[intent.effect_key] = "applied"
            return EffectResult(outcome="partial", evidence_ref="load-partial")
        if fault == "wrong_target":
            sim.offering, sim.ready = "intruder", True
        elif fault == "not_ready":
            sim.offering, sim.ready = target, False
        else:
            sim.offering, sim.ready = target, True
        sim.incarnation = f"inc-after-{intent.effect_key}"
        sim.ledger[intent.effect_key] = "applied"
        if fault == "lost_reply":
            await sim.wait(3600, intent.effect_key)
        # A lying adapter: it reports success even for a wrong target or a target that is not ready.
        return EffectResult(outcome="success", evidence_ref=f"load:{intent.effect_key}")

    async def query(self, domain: str, intent: StepIntent) -> EffectResult:
        self.sim.calls.append(("query", intent.effect_key))
        if not self.can_query:
            return EffectResult(outcome="unknown")
        seen = self.sim.ledger.get(intent.effect_key)
        if seen == "applied":
            return EffectResult(outcome="success", evidence_ref=f"query:{intent.effect_key}", attested=self.attested)
        if seen == "refused":
            return EffectResult(outcome="no_change", evidence_ref="query-refused")
        return EffectResult(outcome="unknown")

    async def checkpoint(self, domain: str, intent: StepIntent) -> EffectResult:  # pragma: no cover - T1.3
        raise NotImplementedError

    async def restore(self, domain: str, handle: str, intent: StepIntent) -> EffectResult:  # pragma: no cover
        raise NotImplementedError

    async def reset(self, domain: str, intent: StepIntent) -> EffectResult:  # pragma: no cover
        raise NotImplementedError


class LedgerAdapter:
    """Answers ``unknown`` to every effect at once and settles it later; only ``query`` learns the outcome."""

    supports_checkpoint = False

    def __init__(self, sim: EngineSim, *, attested: bool = True, settle_delay: float = 0.01, query_wait: float = 1.0) -> None:
        self.sim = sim
        self.attested = attested
        self.settle_delay = settle_delay
        self.query_wait = query_wait
        self._done: dict[str, asyncio.Event] = {}
        self._tasks: list[asyncio.Task[None]] = []

    # -- the engine, as a wrapped answer ------------------------------------

    def _ask(self) -> dict[str, Any]:
        sim = self.sim
        return {"result": {"resident": sim.stock > 0 and sim.offering is not None, "offering": sim.offering,
                           "ready": sim.ready, "stock": sim.stock},
                "meta": {"observed_at": sim.clock(), "incarnation": sim.incarnation, "correlation_id": "c-1"}}

    def _evidence(self, *, want: str) -> Evidence:
        answer = self._ask()
        value = answer["result"]["resident"] if want == "resident" else sim_idle(self.sim)
        return self.sim.evidence(value=value, detail={"offering": answer["result"]["offering"], "ready": answer["result"]["ready"],
                                                      "stock": answer["result"]["stock"]})

    async def idle(self, domain: str) -> Evidence:
        self.sim.calls.append(("idle", ""))
        return self._evidence(want="idle")

    async def resident(self, domain: str) -> Evidence:
        self.sim.calls.append(("resident", ""))
        return self._evidence(want="resident")

    # -- effects: unknown at once, settled in the background ----------------

    def _start(self, key: str, apply: Any) -> None:
        self._done[key] = asyncio.Event()
        self.sim.ledger[key] = "pending"

        async def settle() -> None:
            await self.sim.wait(self.settle_delay, key)
            self.sim.ledger[key] = apply()
            self._done[key].set()

        self._tasks.append(asyncio.get_running_loop().create_task(settle()))

    async def release(self, domain: str, intent: StepIntent) -> EffectResult:
        sim = self.sim
        sim.calls.append(("release", intent.effect_key))
        fault = sim.take_fault("release")

        def apply() -> str:
            if fault == "fail":
                return "refused"
            if fault == "residual":
                sim.stock = 2
                return "partial"
            sim.offering, sim.ready, sim.stock = None, False, 0
            return "applied"

        if fault != "hang":
            self._start(intent.effect_key, apply)
        return EffectResult(outcome="unknown", evidence_ref="accepted")

    async def load(self, domain: str, target: str, intent: StepIntent) -> EffectResult:
        sim = self.sim
        sim.calls.append(("load", f"{intent.effect_key}:{target}"))
        fault = sim.take_fault("load")

        def apply() -> str:
            if fault == "fail":
                return "refused"
            sim.incarnation = f"inc-after-{intent.effect_key}"
            sim.stock = 1
            if fault == "partial":
                sim.offering, sim.ready = target, False
                return "partial"
            sim.offering, sim.ready = ("intruder", True) if fault == "wrong_target" else (target, fault != "not_ready")
            return "applied"

        if fault != "hang":
            self._start(intent.effect_key, apply)
        return EffectResult(outcome="unknown", evidence_ref="accepted")

    async def query(self, domain: str, intent: StepIntent) -> EffectResult:
        self.sim.calls.append(("query", intent.effect_key))
        done = self._done.get(intent.effect_key)
        if done is None:
            return EffectResult(outcome="unknown")
        try:
            await asyncio.wait_for(done.wait(), self.query_wait)
        except asyncio.TimeoutError:
            return EffectResult(outcome="unknown")
        landed = self.sim.ledger[intent.effect_key]
        if landed == "applied":
            return EffectResult(outcome="success", evidence_ref=f"ledger:{intent.effect_key}", attested=self.attested)
        return EffectResult(outcome="no_change" if landed == "refused" else "partial", evidence_ref=f"ledger:{landed}")

    async def checkpoint(self, domain: str, intent: StepIntent) -> EffectResult:  # pragma: no cover - T1.3
        raise NotImplementedError

    async def restore(self, domain: str, handle: str, intent: StepIntent) -> EffectResult:  # pragma: no cover
        raise NotImplementedError

    async def reset(self, domain: str, intent: StepIntent) -> EffectResult:  # pragma: no cover
        raise NotImplementedError


def sim_idle(sim: EngineSim) -> bool:
    return sim.busy == 0


ADAPTERS = {"direct": DirectAdapter, "ledger": LedgerAdapter}

DOMAIN = "dom-a"
FAST: dict[str, Any] = {
    "drain_deadline_s": 0.4, "release_deadline_s": 0.3, "load_deadline_s": 0.3, "query_deadline_s": 0.3,
    "probe_deadline_s": 0.2, "profile": {"fresh_within_s": 2.0}, "idle_poll_s": 0.01, "lock_wait_s": 1.0, "retry_after_s": 7.0,
}


class Harness:
    """A store, a gate cache, a drain and an executor on a temporary database."""

    def __init__(self, tmp_path: Path, *, adapter: str = "direct", policy: Optional[Mapping[str, float]] = None,
                 db: Optional[Path] = None, sim: Optional[EngineSim] = None, clock: Optional[Clock] = None,
                 **adapter_kwargs: Any) -> None:
        self.clock = clock or Clock()
        self.sim = sim or EngineSim(clock=self.clock)
        self.db = db or (tmp_path / "transition.db")
        self.gates = GateStore(self.db)
        self.gates.load()
        self.ids = itertools.count(1)
        self.store = SqliteOperationStore(self.gates, clock=self.clock, new_id=lambda: f"op-{next(self.ids)}")
        self.drain = SwapDrain()
        self.locks: dict[str, asyncio.Lock] = {}
        self.adapter = ADAPTERS[adapter](self.sim, **adapter_kwargs)
        self.policy = {**FAST, **(policy or {})}
        self.executor = Executor(
            store=self.store, steps=core_steps(self.drain, clock=self.clock),
            adapter_for=lambda domain: self.adapter, policy_of=lambda domain: self.policy,
            lock_for=lambda domain: self.locks.setdefault(domain, asyncio.Lock()))

    async def run(self, kind: str, key: str, target: Optional[str] = None, **kwargs: Any):
        """Submit and wait for the operation; returns the final record."""
        began = await self.executor.submit(domain=DOMAIN, kind=kind, idempotency_key=key, target=target, **kwargs)  # type: ignore[arg-type]
        return await self.executor.wait(began.operation.op_id)

    def gate(self) -> str:
        return self.gates.state(DOMAIN)

    def steps_called(self, name: str) -> list[str]:
        return [key for call, key in self.sim.calls if call == name]

    def restart(self, tmp_path: Path) -> "Harness":
        """A new gateway process over the same database and the same engine."""
        return Harness(tmp_path, adapter=("ledger" if isinstance(self.adapter, LedgerAdapter) else "direct"),
                       policy=self.policy, db=self.db, sim=self.sim, clock=self.clock)

"""A minimal test-only runner for transition steps: an in-memory ``OperationStore`` and a walk over ``STEP_ORDER``.

It is not an executor. T1.2 owns the production executor (gateway/transition_executor*.py). This runner exists so the
checkpoint and restore steps can be run in their real order, with the write-ahead rule of D2 and decision G10 (an
outcome is written only when the operation's state, current step and incarnation match), without that executor.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from typing import Callable, Mapping, Optional

from gateway.transition_ops import (
    STEP_ORDER,
    Begun,
    BeginResult,
    EffectResult,
    GateState,
    Operation,
    OperationState,
    StepContext,
    StepIntent,
    StepName,
    Step,
    StepVerdict,
    TransitionKind,
    FINISHED_STATES,
)


class FakeOperationStore:
    """The ``OperationStore`` protocol over dicts. One current operation per domain."""

    def __init__(self) -> None:
        self.ops: dict[str, Operation] = {}
        self.gates: dict[str, GateState] = {}
        self.ignored_outcomes: list[tuple[str, str]] = []
        self.outcomes: list[tuple[str, str, EffectResult]] = []
        self._ids = itertools.count(1)

    def begin(self, *, domain, kind, idempotency_key, target, incarnation, config_revision, compensates=None) -> BeginResult:
        op = Operation(op_id=f"op-{next(self._ids)}", domain=domain, kind=kind, idempotency_key=idempotency_key,
                       incarnation=incarnation, config_revision=config_revision, state="running", step="close",
                       target=target, compensates=compensates)
        self.ops[op.op_id] = op
        self.gates[domain] = "closed"
        return Begun(op)

    def write_intent(self, op_id: str, intent: StepIntent) -> Operation:
        op = self.ops[op_id]
        self.ops[op_id] = replace(op, step=intent.step, step_intent=intent, step_outcome=None)
        return self.ops[op_id]

    def write_outcome(self, op_id: str, step: StepName, result: EffectResult) -> Operation:
        op = self.ops[op_id]
        if op.state != "running" or op.step != step or op.step_intent is None:
            self.ignored_outcomes.append((op_id, step))
            return op
        self.outcomes.append((op_id, step, result))
        self.ops[op_id] = replace(op, step_outcome=result.outcome, evidence_ref=result.evidence_ref)
        return self.ops[op_id]

    def set_snapshot_handle(self, op_id: str, handle: Optional[str]) -> Operation:
        self.ops[op_id] = replace(self.ops[op_id], snapshot_handle=handle)
        return self.ops[op_id]

    def finish(self, op_id: str, state: OperationState, gate: GateState) -> Operation:
        op = self.ops[op_id]
        self.ops[op_id] = replace(op, state=state)
        self.gates[op.domain] = gate
        return self.ops[op_id]

    def get(self, op_id: str) -> Optional[Operation]:
        return self.ops.get(op_id)

    def by_idempotency_key(self, domain: str, idempotency_key: str) -> Optional[Operation]:
        return next((o for o in self.ops.values() if o.domain == domain and o.idempotency_key == idempotency_key), None)

    def unfinished(self):
        return tuple(o for o in self.ops.values() if o.state not in FINISHED_STATES)


class DirectStep:
    """A test step for release or load: one effect, written ahead, ``fail_closed`` unless it succeeds."""

    def __init__(self, name: StepName, call: Callable, kinds: tuple[TransitionKind, ...], on_run: Optional[Callable[[], None]] = None) -> None:
        self.name = name
        self._call = call
        self._kinds = kinds
        self._on_run = on_run

    def applies(self, operation, adapter) -> bool:
        return operation.kind in self._kinds

    async def run(self, ctx: StepContext) -> StepVerdict:
        if self._on_run:
            self._on_run()
        op = ctx.operation
        intent = StepIntent(step=self.name, effect_key=f"{op.op_id}:{self.name}", expected_incarnation=op.incarnation)
        ctx.store.write_intent(op.op_id, intent)
        result = await self._call(ctx, intent)
        ctx.store.write_outcome(op.op_id, self.name, result)
        return "continue" if result.outcome == "success" else "fail_closed"


@dataclass
class RunResult:
    verdict: StepVerdict
    stopped_at: Optional[StepName]
    executed: list[StepName] = field(default_factory=list)
    operation: Optional[Operation] = None
    gate: Optional[GateState] = None


async def run_steps(steps: Mapping[StepName, Step], *, store: FakeOperationStore, adapter, snapshots, policy: Mapping[str, object],
                    kind: TransitionKind, domain: str = "d1", target: Optional[str] = None, incarnation: str = "inc-1") -> RunResult:
    """Walk ``STEP_ORDER``: run each present step whose ``applies`` is true; stop on a verdict other than continue or skip."""
    begun = store.begin(domain=domain, kind=kind, idempotency_key=f"key-{kind}", target=target,
                        incarnation=incarnation, config_revision=1)
    assert isinstance(begun, Begun)
    op: Operation = begun.operation
    result = RunResult(verdict="continue", stopped_at=None)
    for name in STEP_ORDER:
        step = steps.get(name)
        if step is None or not step.applies(op, adapter):
            continue
        result.executed.append(name)
        ctx = StepContext(operation=op, store=store, adapter=adapter, snapshots=snapshots, policy=policy)
        verdict = await step.run(ctx)
        refreshed = store.get(op.op_id)
        assert refreshed is not None
        op = refreshed
        if verdict not in ("continue", "skip"):
            result.verdict, result.stopped_at = verdict, name
            break
    final: tuple[OperationState, GateState] = {
        "continue": ("succeeded", "open"), "skip": ("succeeded", "open"), "reopen": ("aborted", "open"),
        "fail_closed": ("failed_closed", "failed_closed"), "reconcile": ("reconciling", "reconciling")}[result.verdict]
    result.operation = store.finish(op.op_id, final[0], final[1])
    result.gate = store.gates[domain]
    return result

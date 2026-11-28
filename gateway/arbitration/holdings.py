"""Pure in-memory holding transitions, used by tests to check the invariants (design §3.3, §4.4, B1, M2).

A state is a tuple of Holding records. Every transition returns a new state. It is a test double for the invariants, not a ledger.
Invariants the tests check:
- timeout, transport_done and duplicate never release a holding.
- settle needs STOCK_RELEASED evidence for the holding's ref and operation key. An attempt ending is not a release.
- a resource's unsettled holdings share one unit.
- a residency is charged once, however many members it has.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from gateway.arbitration.types import (
    AttemptId,
    HoldingId,
    OperationId,
    Quantity,
    ResourceRef,
)


class OwnerKind(Enum):
    ATTEMPT = "attempt"
    RESIDENCY = "residency"


class Lifecycle(Enum):
    OPEN = "open"
    RECONCILING = "reconciling"
    SETTLED = "settled"


class Authority(Enum):
    HELD = "held"
    LOST = "lost"


class Health(Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Holding:
    id: HoldingId
    owner_kind: OwnerKind
    ref: ResourceRef
    quantity: Quantity
    operation: OperationId
    members: frozenset[AttemptId] = frozenset()
    lifecycle: Lifecycle = Lifecycle.OPEN
    authority: Authority = Authority.HELD
    health: Health = Health.FRESH


class SettlementKind(Enum):
    EXECUTION_ENDED = "execution_ended"
    STOCK_RELEASED = "stock_released"


@dataclass(frozen=True)
class SettlementEvidence:
    """Holder-scoped evidence. `source` names the adapter's recovery evidence, for example a process exit."""

    holding: HoldingId
    operation: OperationId
    kind: SettlementKind
    ref: ResourceRef
    source: str


State = tuple[Holding, ...]


def grant(state: State, holding: Holding) -> State:
    if any(item.id == holding.id for item in state):
        return state
    for item in state:
        same_ref_unsettled = item.ref == holding.ref and item.lifecycle is not Lifecycle.SETTLED
        if same_ref_unsettled and item.quantity.unit != holding.quantity.unit:
            raise ValueError(f"resource {holding.ref.id!r} is held in {item.quantity.unit.name!r}, "
                             f"not {holding.quantity.unit.name!r}")
    return (*state, holding)


def settle(state: State, evidence: SettlementEvidence) -> State:
    holding = _find(state, evidence.holding)
    if holding is None or not _settleable(holding):
        return state
    if evidence.kind is not SettlementKind.STOCK_RELEASED:
        return state  # an execution ending proves no release of stock (design B1, M2)
    if evidence.ref != holding.ref:
        return state  # evidence about another incarnation never releases this holding
    if evidence.operation != holding.operation:
        return state  # evidence about another operation never releases this holding
    if holding.owner_kind is OwnerKind.RESIDENCY and holding.members:
        return state  # the residency stays while any member remains
    return _update(state, holding, lifecycle=Lifecycle.SETTLED)


def transfer_to_residency(state: State, holding_id: HoldingId, attempt: AttemptId) -> State:
    """The attempt's holding becomes a residency. Its charge is unchanged."""
    holding = _find(state, holding_id)
    if holding is None or not _open(holding) or holding.owner_kind is not OwnerKind.ATTEMPT:
        return state
    return _update(state, holding, owner_kind=OwnerKind.RESIDENCY, members=frozenset({attempt}))


def join(state: State, holding_id: HoldingId, attempt: AttemptId) -> State:
    holding = _find(state, holding_id)
    if holding is None or not _open(holding) or holding.owner_kind is not OwnerKind.RESIDENCY:
        return state
    return _update(state, holding, members=holding.members | {attempt})


def leave(state: State, holding_id: HoldingId, attempt: AttemptId) -> State:
    """A member leaves. The residency stays charged until settle."""
    holding = _find(state, holding_id)
    if holding is None or attempt not in holding.members:
        return state
    return _update(state, holding, members=holding.members - {attempt})


def timeout(state: State, holding_id: HoldingId) -> State:
    """A failure detector. It loses authority and marks evidence stale. It never settles."""
    holding = _find(state, holding_id)
    if holding is None or not _open(holding):
        return state
    return _update(state, holding, authority=Authority.LOST, health=Health.STALE)


def transport_done(state: State, holding_id: HoldingId) -> State:
    """A transport completion is not resource settlement. The state does not change."""
    return state


def duplicate(state: State, holding_id: HoldingId) -> State:
    """A repeated event changes nothing."""
    return state


def charged(state: State, ref: ResourceRef) -> int:
    """Total charge on one resource incarnation. Each holding counts once, whatever its member count."""
    return sum(item.quantity.value for item in state
               if item.ref == ref and item.lifecycle is not Lifecycle.SETTLED)


def _find(state: State, holding_id: HoldingId) -> Holding | None:
    return next((item for item in state if item.id == holding_id), None)


def _open(holding: Holding) -> bool:
    return holding.lifecycle is Lifecycle.OPEN


def _settleable(holding: Holding) -> bool:
    """Settlement accepts an open or reconciling holding. A settled one is final: a second settle changes nothing."""
    return holding.lifecycle in (Lifecycle.OPEN, Lifecycle.RECONCILING)


def _update(state: State, holding: Holding, **changes: Any) -> State:
    updated = replace(holding, **changes)
    return tuple(updated if item.id == holding.id else item for item in state)

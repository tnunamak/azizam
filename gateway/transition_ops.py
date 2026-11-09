"""The transition operation interface (T1.2 and T1.3 build against this file).

This module holds types and protocols only. It has no behaviour. T1.2 implements
``OperationStore`` and ``TransitionExecutor`` with the release and load steps; T1.3
implements ``SnapshotStore`` and the checkpoint and restore steps. Both plug into the
same executor through ``Step``. The design is docs/design/SERVICE_TRANSITION_GATE.md
(D2 to D5); section "Operation interface" there is the prose form of this file.

Rules that every implementation keeps:

- The gate core is engine-agnostic. Engine-specific work lives in a ``TransitionAdapter``
  that the reviewed transition profile selects; no engine, model or domain name appears here.
- The operation record is the authority. A step writes its intent before the effect and its
  outcome after it (write-ahead). An ``unknown`` outcome never reopens the gate.
- The executor runs on a task the caller does not own. Cancelling the caller does not
  cancel a dispatched effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Collection, Literal, Mapping, Optional, Protocol, Sequence, Union

TransitionKind = Literal["select", "park", "wake"]
"""``select``: make ``target`` the resident offering. ``park``: release, leave nothing resident.
``wake``: load the domain's last offering (and restore, T1.3)."""

GateState = Literal["open", "closed", "failed_closed", "reconciling"]

OperationState = Literal["running", "succeeded", "aborted", "failed_closed", "reconciling"]
"""``succeeded``, ``aborted`` and ``failed_closed`` are finished. ``running`` and ``reconciling``
are unfinished and hold the domain. A ``failed_closed`` operation leaves the gate
``failed_closed`` (requests get 503) but accepts a new owner transition, e.g. the compensating
one (decision G9). ``reconciling`` refuses new transitions until resolved."""

FINISHED_STATES: frozenset[str] = frozenset({"succeeded", "aborted", "failed_closed"})

StepName = Literal["close", "drain", "checkpoint", "release", "load", "restore", "open"]

Outcome = Literal["success", "no_change", "partial", "unknown"]


@dataclass(frozen=True)
class Evidence:
    """One observation. Older than the profile's ``fresh_within_s``, or for another
    incarnation, counts as unknown."""

    value: Optional[bool]
    """True or False when observed; None when the engine gave no answer."""
    observed_at: float
    incarnation: Optional[str] = None
    correlation_id: Optional[str] = None
    detail: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class EffectResult:
    """A bounded, selected result of one effect (D3 "Results")."""

    outcome: Outcome
    evidence_ref: Optional[str] = None
    handle: Optional[str] = None
    """Checkpoint: the snapshot handle the engine wrote. Otherwise None."""
    bytes: Optional[int] = None
    correlation_id: Optional[str] = None
    attested: bool = False
    """Release only: True when the profile attests the verify predicate (D3 "Release").
    Without it the release is recorded as ``observed_not_resident`` and stays charged."""
    incarnation: Optional[str] = None
    """Interface addition (T1.2): the incarnation the adapter's evidence came from. A result for another
    incarnation than the intent expected is ignored as stale (decision G10)."""


@dataclass(frozen=True)
class StepIntent:
    """Written in its own transaction before an effect is dispatched."""

    step: StepName
    effect_key: str
    """Stable per operation and step, so a recovery query can find the effect."""
    expected_incarnation: Optional[str]

    @property
    def effect(self) -> str:
        """The effect this intent stands for. A reset is recorded under the step ``restore`` with an effect key ending in
        ``:reset``; its identity decides which profile block speaks for it, so a query and a restart never read a reset
        by the restore's failure semantics."""
        return "reset" if self.step == "restore" and self.effect_key.endswith(":reset") else self.step

    @property
    def profile_block(self) -> str:
        """The reviewed profile block that speaks for this effect's failure semantics."""
        return "restore.reset" if self.effect == "reset" else self.step


@dataclass(frozen=True)
class Operation:
    op_id: str
    domain: str
    kind: TransitionKind
    idempotency_key: str
    incarnation: Optional[str]
    config_revision: int
    state: OperationState
    step: Optional[StepName]
    target: Optional[str] = None
    """The offering (service id) to load, for ``select`` and ``wake``. A ``park`` records the offering it
    released (read from the engine when the operation began), so a later ``wake`` knows what to load."""
    step_intent: Optional[StepIntent] = None
    step_outcome: Optional[Outcome] = None
    evidence_ref: Optional[str] = None
    snapshot_handle: Optional[str] = None
    compensates: Optional[str] = None
    """The op_id this operation compensates (D4 step 5), else None."""
    created: float = 0.0
    updated: float = 0.0
    request_hash: Optional[str] = None
    """Interface addition (T1.2, decision G4): hash of kind, target and compensates. The same key with
    another hash is a conflict."""
    stock: Optional[str] = None
    """Interface addition (T1.2): after a release, ``released`` when the profile attests it, else
    ``observed_not_resident`` (the stock stays charged in shadow). None before a release outcome."""


@dataclass(frozen=True)
class Begun:
    """``begin`` inserted a new operation; the gate is now ``closed``."""

    operation: Operation


@dataclass(frozen=True)
class SameOperation:
    """``begin`` found an unfinished operation with the same idempotency key (a retry)."""

    operation: Operation


@dataclass(frozen=True)
class Conflict:
    """``begin`` found another unfinished operation for the domain (HTTP 409 with its id)."""

    operation: Operation


BeginResult = Union[Begun, SameOperation, Conflict]


class OperationStore(Protocol):
    """Durable operation record and gate table (D2). T1.2 implements it on data/transition.db.

    Every method is one transaction and blocking (sqlite); async callers run it off the event
    loop. Each gate write refreshes ``GateStore``'s cache in the same process before it returns.
    """

    def begin(self, *, domain: str, kind: TransitionKind, idempotency_key: str, target: Optional[str],
              incarnation: Optional[str], config_revision: int, compensates: Optional[str] = None) -> BeginResult: ...

    def write_intent(self, op_id: str, intent: StepIntent, held: Optional[EffectResult] = None) -> Operation:
        """Record the intent. ``held`` (an ``unknown`` result) is recorded with it in the same transaction, so a record
        that carries a hold never exists without it."""
        ...

    def write_outcome(self, op_id: str, step: StepName, result: EffectResult) -> Operation: ...

    def set_snapshot_handle(self, op_id: str, handle: Optional[str]) -> Operation: ...

    def finish(self, op_id: str, state: OperationState, gate: GateState) -> Operation:
        """Set the operation's final or holding state and the domain's gate in one transaction."""
        ...

    def get(self, op_id: str) -> Optional[Operation]: ...

    def by_idempotency_key(self, domain: str, idempotency_key: str) -> Optional[Operation]: ...

    def unfinished(self) -> Sequence[Operation]:
        """Every operation not in FINISHED_STATES; startup reconciles these before admission."""
        ...

    # Interface additions (T1.2):
    def current(self, domain: str) -> Optional[Operation]:
        """The domain's unfinished operation (``running`` or ``reconciling``), else None."""
        ...

    def resume(self, op_id: str) -> Operation:
        """``reconciling`` back to ``running`` (gate ``closed``) after the held step was resolved."""
        ...

    def last_target(self, domain: str) -> Optional[str]:
        """The offering of the domain's latest successful operation (the one it loaded, or a park released)."""
        ...


class TransitionAdapter(Protocol):
    """Engine-side effects for one domain, selected by the reviewed transition profile (D3).

    Every method has its own deadline from the profile. A timeout returns ``unknown`` unless
    ``query`` resolves it. T1.2 needs ``idle``, ``resident``, ``release``, ``load``, ``query``.
    T1.3 adds ``checkpoint``, ``restore`` and ``reset``; an adapter without them declares
    ``supports_checkpoint = False`` and the executor skips those steps.
    """

    supports_checkpoint: bool

    async def idle(self, domain: str) -> Evidence: ...

    async def resident(self, domain: str) -> Evidence:
        """Is any offering of the domain resident? ``detail["offering"]`` names it when known."""
        ...

    async def release(self, domain: str, intent: StepIntent) -> EffectResult: ...

    async def load(self, domain: str, target: str, intent: StepIntent) -> EffectResult:
        """Load ``target``; success only after target identity AND readiness are verified."""
        ...

    async def query(self, domain: str, intent: StepIntent) -> EffectResult:
        """Resolve an ``unknown`` effect after the fact; ``unknown`` again if it cannot."""
        ...

    async def checkpoint(self, domain: str, intent: StepIntent) -> EffectResult: ...

    async def restore(self, domain: str, handle: str, intent: StepIntent) -> EffectResult: ...

    async def reset(self, domain: str, intent: StepIntent) -> EffectResult: ...


SnapshotState = Literal["pending", "published", "abandoned", "deleted"]


@dataclass(frozen=True)
class Snapshot:
    handle: str
    domain: str
    op_id: str
    state: SnapshotState
    compatibility: Mapping[str, str]
    """The identities the restore must match (D5): model, tokenizer, engine, cache format,
    context and any declared engine settings. Only a reviewed placement difference may differ."""
    bytes: Optional[int] = None
    created: float = 0.0


class SnapshotStore(Protocol):
    """Snapshot lifecycle (D5). T1.3 implements it. T1.2 never calls it."""

    def pending(self, *, domain: str, op_id: str, handle: str, compatibility: Mapping[str, str],
                bytes: Optional[int]) -> Snapshot: ...

    def publish(self, handle: str, *, domain: Optional[str] = None) -> Snapshot: ...

    def abandon_pending(self, domain: str, op_id: Optional[str] = None) -> Sequence[Snapshot]: ...

    def latest_published(self, domain: str, compatibility: Mapping[str, str],
                         ignore: Collection[str] = ()) -> Optional[Snapshot]: ...


StepVerdict = Literal["continue", "skip", "reopen", "fail_closed", "reconcile"]
"""What the executor does after a step: go on; go on (step did not apply); abort and reopen
with nothing changed; hold ``failed_closed``; hold ``reconciling``."""


@dataclass
class StepContext:
    operation: Operation
    store: OperationStore
    adapter: TransitionAdapter
    snapshots: Optional[SnapshotStore]
    policy: Mapping[str, object]
    """The domain's reviewed transition profile plus its owner arguments (deadlines, policy)."""
    trigger: Optional[Callable[[], None]] = None
    """Interface addition (T1.2): mints the triggering request's lease. The open step calls it under the
    lock, before the gate opens (decision G6). None when the caller wants no lease."""


class Step(Protocol):
    """One ordered step of D4. The executor runs ``steps_for(kind)`` in order under the
    domain's swap lock. T1.2 provides close, drain, release, load and open. T1.3 inserts
    checkpoint (before release) and restore (after load) by registering them in the step
    table; it does not edit the executor."""

    name: StepName

    def applies(self, operation: Operation, adapter: TransitionAdapter) -> bool: ...

    async def run(self, ctx: StepContext) -> StepVerdict: ...


STEP_ORDER: tuple[StepName, ...] = ("close", "drain", "checkpoint", "release", "load", "restore", "open")


class TransitionExecutor(Protocol):
    """Runs one operation to a finished or holding state (T1.2)."""

    async def submit(self, *, domain: str, kind: TransitionKind, idempotency_key: str,
                     target: Optional[str] = None, compensates: Optional[str] = None,
                     trigger: Optional[Callable[[], None]] = None) -> BeginResult:
        """Begin the operation and start it on an executor-owned task; return at once."""
        ...

    async def reconcile_startup(self) -> None:
        """Before any admission: for each unfinished operation, ``query`` its open intent and
        set the gate to ``reconciling`` or ``failed_closed`` unless the outcome is resolved."""
        ...


# HTTP surface (T1.2), owner only, on the admin router (prefix /admin/api, gateway/transition_admin.py):
#   POST /admin/api/engines/domains/{domain}/transitions
#        body {"kind": "select"|"park"|"wake", "target"?: str, "idempotency_key": str}
#        202 {"op_id", "state", "gate"}   new or same key (retry)
#        409 {"op_id", "state"}           another unfinished operation holds the domain
#        404                              unknown domain, or a domain without transition enabled
#   GET  /admin/api/engines/domains/{domain}/transitions/{op_id}   {"operation": the Operation as JSON}
#   GET  /admin/api/engines/domains/{domain}/gate                  {"gate", "op_id", ...}
# A refused request while the gate is not open is TransitionUnavailable (503 + Retry-After).
# A request to a parked domain triggers ``wake`` through ``submit`` (key: "wake-<incarnation>")
# and waits on the lock up to admission_wait_s; the wake mints that request's lease under the
# lock before it reopens (D4 step 7).

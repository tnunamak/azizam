"""Immutable value types for resource arbitration (design §3, §11 slice 0).

Everything here is data. No clock, no IO, no domain vocabulary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import NewType

SCHEMA_VERSION = 1
# Capability tags this slice implements. Anything else a claim requires fails closed.
SUPPORTED_CAPABILITIES = frozenset({"accounting:stock"})
_QUANTITY_LIMIT = 2**63

AttemptId = NewType("AttemptId", str)
HoldingId = NewType("HoldingId", str)
OperationId = NewType("OperationId", str)


class AccountingKind(Enum):
    STOCK = "stock"
    WINDOW_BUDGET = "window_budget"
    BALANCE = "balance"
    OPAQUE_BUDGET = "opaque_budget"


class AuthorityMode(Enum):
    EXCLUSIVE = "exclusive"
    PARTITIONED = "partitioned"
    SHARED = "shared"


class GuaranteeLevel(Enum):
    ENFORCED = "enforced"
    VALIDATED = "validated"
    ADVISORY = "advisory"


class BoundType(Enum):
    ENFORCED = "enforced"
    CONSERVATIVE = "conservative"
    ESTIMATE = "estimate"
    UNKNOWN = "unknown"


class EvidenceQuality(Enum):
    MEASURED = "measured"
    DECLARED = "declared"
    ESTIMATED = "estimated"


class ObservationMeaning(Enum):
    TOTAL = "total"
    FREE = "free"
    USED = "used"
    REMAINING = "remaining"
    CUMULATIVE_CONSUMED = "cumulative_consumed"
    HOLDER_USAGE = "holder_usage"
    HEALTH = "health"


class Outcome(Enum):
    FITS = "fits"
    NO_FIT = "no_fit"
    UNKNOWN = "unknown"
    INCOMPATIBLE = "incompatible"
    UNKNOWN_CLASS = "unknown_class"
    UNSUPPORTED = "unsupported"


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_count(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True)
class ResourceRef:
    """A resource id plus the incarnation of the physical thing behind it."""

    id: str
    incarnation: str

    def __post_init__(self) -> None:
        _require_text("ResourceRef.id", self.id)
        _require_text("ResourceRef.incarnation", self.incarnation)


@dataclass(frozen=True)
class Unit:
    name: str
    granularity: int

    def __post_init__(self) -> None:
        _require_text("Unit.name", self.name)
        if isinstance(self.granularity, bool) or not isinstance(self.granularity, int) or self.granularity <= 0:
            raise ValueError("Unit.granularity must be a positive int")


@dataclass(frozen=True)
class Quantity:
    """An exact integer count in the smallest unit of `unit`."""

    unit: Unit
    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.unit, Unit):
            raise ValueError("Quantity.unit must be a Unit")
        _require_count("Quantity.value", self.value)
        if self.value >= _QUANTITY_LIMIT:
            raise ValueError("Quantity.value overflows the 2**63 bound")


@dataclass(frozen=True)
class Known:
    quantity: Quantity

    def __post_init__(self) -> None:
        if not isinstance(self.quantity, Quantity):
            raise ValueError("Known.quantity must be a Quantity")


@dataclass(frozen=True)
class Unknown:
    reason: str

    def __post_init__(self) -> None:
        _require_text("Unknown.reason", self.reason)


@dataclass(frozen=True)
class NotApplicable:
    """No quantity. Used for an availability predicate, which is not a zero demand."""


Amount = Known | Unknown | NotApplicable


@dataclass(frozen=True)
class EvidenceRef:
    """Where a fact came from. Times are whole Unix seconds. validity_s None means no declared horizon."""

    source: str
    source_generation: int
    sequence: int
    observed_at: int
    validity_s: int | None
    quality: EvidenceQuality

    def __post_init__(self) -> None:
        _require_text("EvidenceRef.source", self.source)
        _require_count("EvidenceRef.source_generation", self.source_generation)
        _require_count("EvidenceRef.sequence", self.sequence)
        _require_count("EvidenceRef.observed_at", self.observed_at)
        if self.validity_s is not None:
            _require_count("EvidenceRef.validity_s", self.validity_s)
        if not isinstance(self.quality, EvidenceQuality):
            raise ValueError("EvidenceRef.quality must be an EvidenceQuality")


@dataclass(frozen=True)
class Observation:
    """A typed fact about one resource. Uncertainty is added to the balance exactly once."""

    ref: ResourceRef
    meaning: ObservationMeaning
    amount: Amount
    uncertainty: Amount
    # A lower bound on managed use that this reading already reflects (design §14 B2), attested by
    # holder-scoped evidence. It is a usage amount, not a reservation ceiling: a holding reserved at
    # 80 that has materialized 20 contributes 20. Unknown or NotApplicable proves nothing (V = 0).
    included_managed: Amount
    evidence: EvidenceRef

    def __post_init__(self) -> None:
        if not isinstance(self.meaning, ObservationMeaning):
            raise ValueError("Observation.meaning must be an ObservationMeaning")


@dataclass(frozen=True)
class Demand:
    """What a claim needs from one resource. A NotApplicable amount is an availability predicate."""

    ref: ResourceRef
    amount: Amount
    bound_type: BoundType
    evidence: EvidenceRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.bound_type, BoundType):
            raise ValueError("Demand.bound_type must be a BoundType")


@dataclass(frozen=True)
class Claim:
    """One attempt's demand vector. One phase only in slice 0. `required` lists capability tags."""

    attempt: AttemptId
    demands: tuple[Demand, ...]
    class_id: str
    required: frozenset[str] = frozenset()
    # Reserved for phase graphs (§3.4). Slice 0 implements one phase, so any of these set is Unsupported.
    peak: tuple[Demand, ...] = ()
    result: tuple[Demand, ...] = ()
    transfers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "demands", tuple(self.demands))
        object.__setattr__(self, "required", frozenset(self.required))
        object.__setattr__(self, "peak", tuple(self.peak))
        object.__setattr__(self, "result", tuple(self.result))
        object.__setattr__(self, "transfers", tuple(self.transfers))
        _require_text("Claim.attempt", self.attempt)
        _require_text("Claim.class_id", self.class_id)
        ids = [demand.ref.id for demand in self.demands]
        if len(ids) != len(set(ids)):
            raise ValueError("a claim names each resource at most once")


@dataclass(frozen=True)
class Resource:
    """A declared resource: its identity, unit, accounting kind and authority mode."""

    ref: ResourceRef
    unit: Unit
    accounting: AccountingKind
    authority: AuthorityMode


@dataclass(frozen=True)
class Liability:
    """A managed holding: its full reserved ceiling. `sequence` orders grants for later slices."""

    quantity: Quantity
    sequence: int

    def __post_init__(self) -> None:
        _require_count("Liability.sequence", self.sequence)


@dataclass(frozen=True)
class PolicySnapshot:
    """Opaque classes, a pairwise compatibility relation (not transitive), a self rule, and per-resource reserves.

    `limits` maps a resource id to its reserve: headroom kept back, subtracted once.
    """

    revision: int
    classes: frozenset[str]
    compatible: frozenset[frozenset[str]]
    self_compatible: frozenset[str]
    limits: Mapping[str, Quantity] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_count("PolicySnapshot.revision", self.revision)
        object.__setattr__(self, "classes", frozenset(self.classes))
        object.__setattr__(self, "compatible", frozenset(frozenset(pair) for pair in self.compatible))
        object.__setattr__(self, "self_compatible", frozenset(self.self_compatible))
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))
        for pair in self.compatible:
            if len(pair) != 2:
                raise ValueError("a compatibility pair names two distinct classes")
            if not pair <= self.classes:
                raise ValueError("compatibility refers to an undeclared class")
        if not self.self_compatible <= self.classes:
            raise ValueError("self_compatible refers to an undeclared class")
        for reserve in self.limits.values():
            if not isinstance(reserve, Quantity):
                raise ValueError("a limit must be a Quantity")


@dataclass(frozen=True)
class ResourceResult:
    ref: ResourceRef
    outcome: Outcome
    required: int | None
    residual: int | None
    guarantee: GuaranteeLevel
    reason: str


@dataclass(frozen=True)
class DecisionEnvelope:
    """Versioned, private advice. A value only: it reserves nothing and is never a grant."""

    schema_version: int
    policy_revision: int
    attempt: AttemptId
    outcome: Outcome
    guarantee: GuaranteeLevel
    resources: tuple[ResourceResult, ...]
    failing: tuple[str, ...]
    reasons: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]

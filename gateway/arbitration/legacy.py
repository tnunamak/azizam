"""Wrappers that read the legacy admission code through the new types. The old modules are unchanged.

This is the only module in gateway/arbitration that may use domain words. The rules it keeps:
- gpu_required with vram_mb == 0 is an availability predicate, not a zero demand.
- None is not 0: an unknown total or used value is an Unknown reading, not a zero.
- active_exclusive_locks None is not an empty set: it makes every lock reading Unknown.
- a float cpu_cores value is not converted: the claim becomes LegacyOnly.
- a case that cannot be converted without loss becomes LegacyOnly. It is never guessed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from gateway.arbitration.kernel import evaluate_stock
from gateway.arbitration.types import (
    AccountingKind,
    Amount,
    AttemptId,
    AuthorityMode,
    BoundType,
    Claim,
    DecisionEnvelope,
    Demand,
    EvidenceQuality,
    EvidenceRef,
    Known,
    NotApplicable,
    Observation,
    ObservationMeaning,
    Outcome,
    PolicySnapshot,
    Quantity,
    Resource,
    ResourceRef,
    Unit,
    Unknown,
)
from gateway.resource_admission import (
    AdmissionConstraints,
    CurrentLoad,
    ResourceEstimate,
    ServiceAvailability,
    advise_resource_admission,
)
from gateway.resource_claims import AdmissionDecision, CapacitySnapshot, ResourceClaim

MIB = Unit("mib", 1)
LOCK = Unit("lock", 1)
LEGACY_CLASS = "legacy"
LEGACY_SOURCE = "legacy-snapshot"
LEGACY_POLICY = PolicySnapshot(
    revision=0,
    classes=frozenset({LEGACY_CLASS}),
    compatible=frozenset(),
    self_compatible=frozenset({LEGACY_CLASS}),
)
# Legacy field name -> (total field, used field) on CapacitySnapshot. Used already includes any reserve.
NUMERIC_FIELDS: Mapping[str, tuple[str, str]] = {
    "vram_mb": ("total_vram_mb", "used_vram_mb"),
    "ram_mb": ("total_ram_mb", "used_ram_mb"),
    "disk_mb": ("total_disk_mb", "used_disk_mb"),
}
_QUALITY = {
    "measured": EvidenceQuality.MEASURED,
    "declared": EvidenceQuality.DECLARED,
    "estimated": EvidenceQuality.ESTIMATED,
    "unavailable": EvidenceQuality.ESTIMATED,
}

# Inventory: legacy field names and lock keys -> the ResourceRef that stands for them.
Inventory = Mapping[str, ResourceRef]


@dataclass(frozen=True)
class LegacyOnly:
    """This case cannot be converted without loss. It stays on the legacy path."""

    reason: str


@dataclass(frozen=True)
class RouteView:
    """The route-admission decision in neutral form. It has no stock-kernel counterpart in slice 0."""

    allowed: bool
    degraded: bool
    reason_codes: tuple[str, ...]


def legacy_resources(inventory: Inventory, *, authority: AuthorityMode) -> dict[str, Resource]:
    """Resource declarations for every inventory entry, keyed by resource id. Authority is the caller's declaration."""
    resources: dict[str, Resource] = {}
    for key, ref in inventory.items():
        unit = MIB if key in NUMERIC_FIELDS else LOCK
        resources[ref.id] = Resource(ref, unit, AccountingKind.STOCK, authority)
    return resources


def legacy_claim_view(claim: ResourceClaim, inventory: Inventory, *,
                      attempt: str = "legacy") -> Claim | LegacyOnly:
    if claim.cpu_cores is not None:
        return LegacyOnly("cpu_cores is a float: not converted")
    if claim.profile_id is not None:
        return LegacyOnly("profile concurrency is not in the stock kernel")

    demands: list[Demand] = []
    if claim.vram_mb > 0 or claim.gpu_required:
        ref = inventory.get("vram_mb")
        if ref is None:
            return LegacyOnly("no inventory entry for vram_mb")
        if claim.vram_mb > 0:
            quantity = _quantity(claim.vram_mb, MIB)
            if quantity is None:
                return LegacyOnly("vram_mb is beyond the exact Quantity range")
            amount: Amount = Known(quantity)
        else:
            amount = NotApplicable()
        demands.append(Demand(ref, amount, BoundType.ESTIMATE))
    for key, value in (("ram_mb", claim.ram_mb), ("disk_mb", claim.disk_mb)):
        if not value:  # None and 0 both mean no demand, as in the legacy code
            continue
        ref = inventory.get(key)
        if ref is None:
            return LegacyOnly(f"no inventory entry for {key}")
        quantity = _quantity(value, MIB)
        if quantity is None:
            return LegacyOnly(f"{key} is beyond the exact Quantity range")
        demands.append(Demand(ref, Known(quantity), BoundType.ESTIMATE))
    for lock in dict.fromkeys(claim.exclusive_locks):
        if lock in NUMERIC_FIELDS:
            # Its inventory entry is a MiB resource, so the lock would mix units on one resource.
            return LegacyOnly(f"lock {lock} collides with a numeric inventory key")
        ref = inventory.get(lock)
        if ref is None:
            return LegacyOnly(f"no inventory entry for lock {lock}")
        demands.append(Demand(ref, Known(Quantity(LOCK, 1)), BoundType.ESTIMATE))
    return Claim(AttemptId(attempt), tuple(demands), LEGACY_CLASS)


def legacy_capacity_view(snapshot: CapacitySnapshot, inventory: Inventory, *,
                         validity_s: int | None,
                         dimensions: frozenset[str] | None = None) -> tuple[Observation, ...] | LegacyOnly:
    """Readings for the inventory. `dimensions` names the numeric fields to convert; None converts all of them."""
    if snapshot.observed_at is None or validity_s is None:
        return LegacyOnly("no observation time or validity horizon: freshness is not declared")
    if snapshot.queue_limits or snapshot.profile_limits:
        return LegacyOnly("queue and profile limits are not in the stock kernel")
    evidence = EvidenceRef(LEGACY_SOURCE, 0, 0, math.floor(snapshot.observed_at), validity_s,
                           _QUALITY[snapshot.quality])
    observations: list[Observation] = []
    for key, (total_field, used_field) in NUMERIC_FIELDS.items():
        ref = inventory.get(key)
        if ref is None or (dimensions is not None and key not in dimensions):
            continue
        for meaning, field in ((ObservationMeaning.TOTAL, total_field), (ObservationMeaning.USED, used_field)):
            amount = _amount(meaning, getattr(snapshot, field), MIB)
            if amount is None:
                return LegacyOnly(f"{field} is beyond the exact Quantity range")
            observations.append(Observation(ref, meaning, amount, NotApplicable(), NotApplicable(), evidence))
    for key, ref in inventory.items():
        if key in NUMERIC_FIELDS:
            continue
        observations.append(_reading(ref, ObservationMeaning.TOTAL, 1, LOCK, evidence))
        held = snapshot.active_exclusive_locks
        used = None if held is None else (1 if key in held else 0)
        observations.append(_reading(ref, ObservationMeaning.USED, used, LOCK, evidence))
    return tuple(observations)


def legacy_advice(claim: ResourceClaim, snapshot: CapacitySnapshot, inventory: Inventory, *,
                  now: int, validity_s: int | None, authority: AuthorityMode,
                  attempt: str = "legacy") -> DecisionEnvelope | LegacyOnly:
    """Advice from the stock kernel for a legacy claim and snapshot, or LegacyOnly."""
    view = legacy_claim_view(claim, inventory, attempt=attempt)
    if isinstance(view, LegacyOnly):
        return view
    if snapshot.observed_at is not None and validity_s is not None:
        # The legacy evaluator never expires observed_at, so its answer cannot be compared with a kernel answer.
        age = now - math.floor(snapshot.observed_at)
        if age < 0 or age > validity_s:
            return LegacyOnly("freshness not preserved: the legacy evaluator never expires observed_at")
    claimed = {demand.ref.id for demand in view.demands}
    dimensions = frozenset(key for key, ref in inventory.items() if key in NUMERIC_FIELDS and ref.id in claimed)
    observations = legacy_capacity_view(snapshot, inventory, validity_s=validity_s, dimensions=dimensions)
    if isinstance(observations, LegacyOnly):
        return observations
    return evaluate_stock(
        now=now,
        policy=LEGACY_POLICY,
        claim=view,
        resources=legacy_resources(inventory, authority=authority),
        observations=observations,
        liabilities={},
        active_classes=(),
    )


def shadow_pool_advice(pair: tuple[CapacitySnapshot, ResourceClaim], inventory: Inventory, *,
                       now: int, validity_s: int | None, authority: AuthorityMode) -> DecisionEnvelope | LegacyOnly:
    """Shadow view of exclusive_pool.snapshot_and_claim's output. The reserve is already in used, so policy has no reserve."""
    snapshot, claim = pair
    return legacy_advice(claim, snapshot, inventory, now=now, validity_s=validity_s,
                         authority=authority, attempt="pool")


def legacy_route_view(service: ServiceAvailability, estimate: ResourceEstimate,
                      constraints: AdmissionConstraints | None = None,
                      load: CurrentLoad | None = None) -> RouteView:
    """Wrap advise_resource_admission unchanged. Route rules have no stock-kernel counterpart in slice 0."""
    decision = advise_resource_admission(service, estimate, constraints, load)
    return RouteView(allowed=decision.allowed, degraded=decision.degraded, reason_codes=decision.reason_codes)


@dataclass(frozen=True)
class ShadowReport:
    status: str  # "agree", "disagree", "legacy-only" or "unreadable"
    old_admits: bool | None
    new_outcome: str | None
    detail: str


def shadow_compare(old_decision: object, new_advice: object) -> ShadowReport:
    """Compare an old decision with new advice. Admission only. It never raises."""
    try:
        if isinstance(new_advice, LegacyOnly):
            return ShadowReport("legacy-only", None, None, new_advice.reason)
        if not isinstance(new_advice, DecisionEnvelope):
            return ShadowReport("unreadable", None, None, "new advice is not a decision envelope")
        old_admits, old_detail = _old_admits(old_decision)
        if old_admits is None:
            return ShadowReport("unreadable", None, new_advice.outcome.value, old_detail)
        new_admits = new_advice.outcome is Outcome.FITS
        status = "agree" if old_admits == new_admits else "disagree"
        return ShadowReport(status, old_admits, new_advice.outcome.value, old_detail)
    except Exception as error:  # noqa: BLE001 - a shadow comparison must never break the live path
        return ShadowReport("unreadable", None, None, f"{type(error).__name__}")


def _old_admits(old: object) -> tuple[bool | None, str]:
    if isinstance(old, AdmissionDecision):
        return old.accepted, f"legacy status {old.status.value}"
    allowed = getattr(old, "allowed", None)
    if isinstance(allowed, bool):
        return allowed, "legacy route outcome"
    return None, "old decision is not a known legacy type"


def _reading(ref: ResourceRef, meaning: ObservationMeaning, value: int | None, unit: Unit,
             evidence: EvidenceRef) -> Observation:
    amount = _amount(meaning, value, unit)
    assert amount is not None, "lock readings are 0 or 1 and always convert"
    return Observation(ref, meaning, amount, NotApplicable(), NotApplicable(), evidence)


def _amount(meaning: ObservationMeaning, value: int | None, unit: Unit) -> Amount | None:
    """None is not 0: an absent value stays Unknown and can never fit. None means the value is not representable."""
    if value is None:
        return Unknown(f"{meaning.value} not advertised")
    quantity = _quantity(value, unit)
    return None if quantity is None else Known(quantity)


def _quantity(value: int, unit: Unit) -> Quantity | None:
    """The value as an exact Quantity, or None when Quantity cannot hold it. Quantity's own rules decide."""
    try:
        return Quantity(unit, value)
    except ValueError:
        return None

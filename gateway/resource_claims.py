"""Pure resource claims and admission decisions for scheduler boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Mapping

from gateway.resource_decisions import CapacityFact, DecisionDetails, DecisionReason, Quality


class QueueClass(str, Enum):
    """Scheduling class for resource admission."""

    INTERACTIVE = "interactive"
    BATCH = "batch"
    BACKGROUND = "background"


class AdmissionStatus(str, Enum):
    """Outcome for a resource admission check."""

    ACCEPTED = "accepted"
    DELAYED = "delayed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ResourceClaim:
    """Resources a request expects to hold while running."""

    gpu_required: bool = False
    vram_mb: int = 0
    cpu_cores: float | None = None
    ram_mb: int | None = None
    disk_mb: int | None = None
    exclusive_locks: tuple[str, ...] = ()
    queue_class: QueueClass = QueueClass.INTERACTIVE
    profile_id: str | None = None
    reasons: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_negative_int("vram_mb", self.vram_mb)
        if self.cpu_cores is not None and self.cpu_cores < 0:
            raise ValueError("cpu_cores must be non-negative")
        for name in ("ram_mb", "disk_mb"):
            value = getattr(self, name)
            if value is not None:
                _require_non_negative_int(name, value)

        object.__setattr__(self, "exclusive_locks", tuple(self.exclusive_locks))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "caveats", tuple(self.caveats))
        if not isinstance(self.queue_class, QueueClass):
            object.__setattr__(self, "queue_class", QueueClass(self.queue_class))


@dataclass(frozen=True)
class CapacitySnapshot:
    """Explicit caller-supplied capacity and current load."""

    total_vram_mb: int | None = None
    used_vram_mb: int | None = None
    total_cpu_cores: float | None = None
    used_cpu_cores: float | None = None
    total_ram_mb: int | None = None
    used_ram_mb: int | None = None
    total_disk_mb: int | None = None
    used_disk_mb: int | None = None
    active_exclusive_locks: frozenset[str] | None = None
    active_by_queue: Mapping[QueueClass, int] = field(default_factory=dict)
    queue_limits: Mapping[QueueClass, int] = field(default_factory=dict)
    active_by_profile: Mapping[str, int] = field(default_factory=dict)
    profile_limits: Mapping[str, int] = field(default_factory=dict)

    observed_at: float | None = None
    quality: Quality = "declared"
    # Optional per-field provenance, e.g. measured disk and declared reservations.
    fact_qualities: Mapping[str, Quality] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("total_vram_mb", "used_vram_mb", "used_ram_mb", "used_disk_mb"):
            value = getattr(self, name)
            if value is not None:
                _require_non_negative_int(name, value)
        for name in ("total_ram_mb", "total_disk_mb"):
            value = getattr(self, name)
            if value is not None:
                _require_non_negative_int(name, value)
        if self.total_cpu_cores is not None and self.total_cpu_cores < 0:
            raise ValueError("total_cpu_cores must be non-negative")
        if self.used_cpu_cores is not None and self.used_cpu_cores < 0:
            raise ValueError("used_cpu_cores must be non-negative")

        if self.active_exclusive_locks is not None:
            object.__setattr__(self, "active_exclusive_locks", frozenset(self.active_exclusive_locks))
        object.__setattr__(
            self,
            "active_by_queue",
            {QueueClass(queue): count for queue, count in self.active_by_queue.items()},
        )
        object.__setattr__(
            self,
            "queue_limits",
            {QueueClass(queue): limit for queue, limit in self.queue_limits.items()},
        )
        object.__setattr__(self, "active_by_profile", dict(self.active_by_profile))
        object.__setattr__(self, "profile_limits", dict(self.profile_limits))


@dataclass(frozen=True)
class AdmissionRequest:
    """A claim evaluated for a scheduler-visible request."""

    request_id: str
    claim: ResourceClaim


@dataclass(frozen=True)
class AdmissionDecision:
    """Deterministic admission result with caller-safe context."""

    status: AdmissionStatus
    request_id: str
    reasons: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()

    details: DecisionDetails = field(default_factory=DecisionDetails)

    @property
    def accepted(self) -> bool:
        return self.status is AdmissionStatus.ACCEPTED


def evaluate_admission(
    request: AdmissionRequest,
    capacity: CapacitySnapshot,
) -> AdmissionDecision:
    """Evaluate admission without observing live system state."""

    claim = request.claim
    reasons: list[DecisionReason] = []
    facts: list[CapacityFact] = []
    rejections: list[str] = []
    delays: list[str] = []

    def fact(name: str, resource: str, value: object, *, declared: bool = False) -> object:
        quality = "declared" if declared else capacity.fact_qualities.get(name, capacity.quality)
        item = CapacityFact(name, resource, value, capacity.observed_at, quality)
        facts.append(item)
        return item.value

    def reason(code: str, effect: str, resource: str, rule: str, *, unknown: bool = False) -> None:
        (rejections if effect == "rejection" else delays).append(rule)
        reasons.append(DecisionReason(code, effect, resource, rule, capacity.observed_at,
                                      "unavailable" if unknown else "declared"))

    for resource, key, limits, active in (
        ("queue", claim.queue_class, capacity.queue_limits, capacity.active_by_queue),
        ("profile", claim.profile_id, capacity.profile_limits, capacity.active_by_profile),
    ):
        if key is None or key not in limits:
            continue
        limit = fact(f"{resource}_limit", resource, limits[key], declared=True)
        count = fact(f"{resource}_active", resource, active.get(key))
        if count is None:
            reason(f"{resource}_load_unknown", "rejection", resource,
                   f"{resource} concurrency load is not advertised", unknown=True)
        elif count >= limit:
            rule = (f"queue concurrency limit reached for {claim.queue_class.value}"
                    if resource == "queue" else "profile concurrency limit reached")
            reason("queue_full" if resource == "queue" else "profile_concurrency_full",
                   "rejection", resource, rule)

    if claim.exclusive_locks:
        fact("requested_locks", "lock", claim.exclusive_locks, declared=True)
        active = fact("active_locks", "lock", sorted(capacity.active_exclusive_locks)
                      if capacity.active_exclusive_locks is not None else None)
        if active is None:
            reason("locks_unknown", "delay", "lock", "exclusive lock availability is not advertised", unknown=True)
        else:
            blocked = sorted(set(active).intersection(claim.exclusive_locks))
            fact("conflicting_locks", "lock", blocked)
            if blocked:
                reason("lock_conflict", "delay", "lock", f"{len(blocked)} exclusive lock conflict")

    for name, resource in (("vram_mb", "vram"), ("cpu_cores", "cpu"),
                           ("ram_mb", "ram"), ("disk_mb", "disk")):
        claimed = getattr(claim, name)
        if not claimed and not (name == "vram_mb" and claim.gpu_required):
            continue
        fact(f"requested_{name}", resource, claimed, declared=True)
        total = fact(f"total_{name}", resource, getattr(capacity, f"total_{name}"))
        used = fact(f"used_{name}", resource, getattr(capacity, f"used_{name}"))
        available = None if total is None or used is None else total - used
        # This is arithmetic over supplied facts, not a new measurement.
        qualities = {f.quality for f in facts[-2:]}
        quality = ("unavailable" if available is None else "estimated" if "estimated" in qualities
                   else "declared" if "declared" in qualities else "measured")
        facts.append(CapacityFact(f"available_{name}", resource, available, capacity.observed_at, quality))
        if claimed:
            if available is None:
                reason("capacity_unknown", "delay", resource, f"{name} capacity is not advertised", unknown=True)
            elif claimed > available:
                reason("capacity_insufficient", "delay", resource,
                       f"not enough {name}: requested {claimed}, available {available}")
        if name == "vram_mb" and claim.gpu_required:
            if total is None:
                reason("capacity_unknown", "delay", "gpu", "gpu capacity is not advertised", unknown=True)
            elif total <= 0:
                reason("gpu_unavailable", "delay", "gpu", "gpu required but no gpu capacity is advertised")

    details = DecisionDetails(tuple(reasons), tuple(facts))
    if rejections:
        return AdmissionDecision(AdmissionStatus.REJECTED, request.request_id,
                                 tuple(rejections), claim.caveats, details)
    if delays:
        return AdmissionDecision(AdmissionStatus.DELAYED, request.request_id,
                                 tuple(delays), claim.caveats, details)
    return AdmissionDecision(AdmissionStatus.ACCEPTED, request.request_id,
                             claim.reasons or ("capacity available",), claim.caveats, details)


def summarize_claim(claim: ResourceClaim) -> dict[str, object]:
    """Return a redacted, log-safe summary of a claim."""

    return {
        "gpu_required": claim.gpu_required,
        "vram_mb": claim.vram_mb,
        "cpu_cores": claim.cpu_cores,
        "ram_mb": claim.ram_mb,
        "disk_mb": claim.disk_mb,
        "exclusive_lock_count": len(claim.exclusive_locks),
        "exclusive_lock_refs": tuple(_redact_ref(lock) for lock in claim.exclusive_locks),
        "queue_class": claim.queue_class.value,
        "profile_ref": _redact_ref(claim.profile_id) if claim.profile_id else None,
        "reasons": claim.reasons,
        "caveats": claim.caveats,
    }


def summarize_decision(decision: AdmissionDecision) -> dict[str, object]:
    """Return a stable, log-safe summary of an admission decision."""

    return {
        "request_ref": _redact_ref(decision.request_id),
        "status": decision.status.value,
        "accepted": decision.accepted,
        "reasons": decision.reasons,
        "caveats": decision.caveats,
    }


def _redact_ref(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


def _require_non_negative_int(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")

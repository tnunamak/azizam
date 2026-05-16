"""Pure resource claims and admission decisions for scheduler boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from hashlib import sha256
from typing import Mapping


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

    total_vram_mb: int = 0
    used_vram_mb: int = 0
    total_cpu_cores: float | None = None
    used_cpu_cores: float = 0
    total_ram_mb: int | None = None
    used_ram_mb: int = 0
    total_disk_mb: int | None = None
    used_disk_mb: int = 0
    active_exclusive_locks: frozenset[str] = frozenset()
    active_by_queue: Mapping[QueueClass, int] = field(default_factory=dict)
    queue_limits: Mapping[QueueClass, int] = field(default_factory=dict)
    active_by_profile: Mapping[str, int] = field(default_factory=dict)
    profile_limits: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("total_vram_mb", "used_vram_mb", "used_ram_mb", "used_disk_mb"):
            _require_non_negative_int(name, getattr(self, name))
        for name in ("total_ram_mb", "total_disk_mb"):
            value = getattr(self, name)
            if value is not None:
                _require_non_negative_int(name, value)
        if self.total_cpu_cores is not None and self.total_cpu_cores < 0:
            raise ValueError("total_cpu_cores must be non-negative")
        if self.used_cpu_cores < 0:
            raise ValueError("used_cpu_cores must be non-negative")

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

    @property
    def accepted(self) -> bool:
        return self.status is AdmissionStatus.ACCEPTED


def evaluate_admission(
    request: AdmissionRequest,
    capacity: CapacitySnapshot,
) -> AdmissionDecision:
    """Evaluate admission without observing live system state."""

    claim = request.claim
    rejections: list[str] = []
    delays: list[str] = []

    _check_queue_limit(claim, capacity, rejections)
    _check_profile_limit(claim, capacity, rejections)
    _check_locks(claim, capacity, delays)
    _check_capacity("vram_mb", claim.vram_mb, capacity.total_vram_mb, capacity.used_vram_mb, delays)

    if claim.gpu_required and capacity.total_vram_mb <= 0:
        delays.append("gpu required but no gpu capacity is advertised")

    _check_optional_capacity(
        "cpu_cores",
        claim.cpu_cores,
        capacity.total_cpu_cores,
        capacity.used_cpu_cores,
        delays,
    )
    _check_optional_capacity("ram_mb", claim.ram_mb, capacity.total_ram_mb, capacity.used_ram_mb, delays)
    _check_optional_capacity(
        "disk_mb",
        claim.disk_mb,
        capacity.total_disk_mb,
        capacity.used_disk_mb,
        delays,
    )

    caveats = claim.caveats
    if rejections:
        return AdmissionDecision(
            AdmissionStatus.REJECTED,
            request.request_id,
            tuple(rejections),
            caveats,
        )
    if delays:
        return AdmissionDecision(
            AdmissionStatus.DELAYED,
            request.request_id,
            tuple(delays),
            caveats,
        )
    return AdmissionDecision(
        AdmissionStatus.ACCEPTED,
        request.request_id,
        claim.reasons or ("capacity available",),
        caveats,
    )


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


def _check_queue_limit(
    claim: ResourceClaim,
    capacity: CapacitySnapshot,
    rejections: list[str],
) -> None:
    limit = capacity.queue_limits.get(claim.queue_class)
    if limit is None:
        return
    active = capacity.active_by_queue.get(claim.queue_class, 0)
    if active >= limit:
        rejections.append(f"queue concurrency limit reached for {claim.queue_class.value}")


def _check_profile_limit(
    claim: ResourceClaim,
    capacity: CapacitySnapshot,
    rejections: list[str],
) -> None:
    if claim.profile_id is None:
        return
    limit = capacity.profile_limits.get(claim.profile_id)
    if limit is None:
        return
    active = capacity.active_by_profile.get(claim.profile_id, 0)
    if active >= limit:
        rejections.append("profile concurrency limit reached")


def _check_locks(
    claim: ResourceClaim,
    capacity: CapacitySnapshot,
    delays: list[str],
) -> None:
    blocked = capacity.active_exclusive_locks.intersection(claim.exclusive_locks)
    if blocked:
        delays.append(f"{len(blocked)} exclusive lock conflict")


def _check_capacity(
    name: str,
    claimed: int | float,
    total: int | float,
    used: int | float,
    delays: list[str],
) -> None:
    if claimed <= 0:
        return
    available = total - used
    if claimed > available:
        delays.append(f"not enough {name}: requested {claimed}, available {available}")


def _check_optional_capacity(
    name: str,
    claimed: int | float | None,
    total: int | float | None,
    used: int | float,
    delays: list[str],
) -> None:
    if claimed is None or claimed <= 0:
        return
    if total is None:
        delays.append(f"{name} capacity is not advertised")
        return
    _check_capacity(name, claimed, total, used, delays)


def _redact_ref(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


def _require_non_negative_int(name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")

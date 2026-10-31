"""Process-local serialization of one exclusive route pool, with an explicit VRAM claim.

One ``ExclusivePoolLeaseManager`` lives in each gateway process. For each pool it holds an
``asyncio.Lock`` from before the observer is read until the downstream call and its cleanup end.
Only requests of this process are serialized, so the declaration is valid only when exactly one
gateway process serves the physical pool. Nothing here caches, queues, samples or learns a peak.
"""

from __future__ import annotations

import asyncio
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Awaitable, Callable, Mapping

from gateway.resource_claims import (
    AdmissionRequest,
    AdmissionStatus,
    CapacitySnapshot,
    QueueClass,
    ResourceClaim,
    evaluate_admission,
)

MIB = 1 << 20
MODE_EXCLUSIVE = "exclusive-route-pool"


class PoolObserverError(Exception):
    """The observer answered, but not with usable facts, or did not answer. Maps to 503 resource_unavailable."""


class PoolCapacityError(Exception):
    """The pool cannot hold the claim plus its reserve right now."""

    def __init__(self, status: AdmissionStatus) -> None:
        super().__init__(status.value)
        self.status = status


@dataclass(frozen=True)
class ExclusivePoolSpec:
    """The validated, closed ``resource_capacity`` block of an enabled exclusive pool."""

    pool_id: str
    max_claim_bytes: int
    reserve_bytes: int
    retry_after_seconds: int
    timeout_seconds: int
    observer_service: str
    observer_path: str
    free_pointer: str
    total_pointer: str


_CAPACITY_KEYS = frozenset({
    "enabled", "mode", "pool_id", "max_claim_bytes", "reserve_bytes", "retry_after_seconds",
    "timeout_seconds", "observer",
})
_OBSERVER_KEYS = frozenset({"service", "path", "free_pointer", "total_pointer"})


def parse_resource_capacity(service_id: str, block: Any) -> ExclusivePoolSpec | None:
    """Return the spec when capacity is enabled, None when it is disabled. Raises ValueError when malformed.

    ``enabled: false`` is the durable tombstone for the whole block. Enabled capacity must be the
    complete closed exclusive schema; there is no default for any value. An absent block is not enabled either.
    """
    if block is None:
        return None
    if not isinstance(block, Mapping):
        raise ValueError(f"service {service_id!r}: resource_capacity must be an object")
    if block.get("enabled") is False:
        if set(block) - {"enabled"}:
            raise ValueError(f"service {service_id!r}: a disabled resource_capacity holds only enabled: false")
        return None
    unknown = set(block) - _CAPACITY_KEYS
    if unknown:
        raise ValueError(f"service {service_id!r}: resource_capacity has unknown keys {sorted(unknown)}")
    if block.get("enabled") is not True or block.get("mode") != MODE_EXCLUSIVE:
        raise ValueError(f"service {service_id!r}: enabled capacity must be mode {MODE_EXCLUSIVE!r}")
    pool_id = block.get("pool_id")
    if not isinstance(pool_id, str) or not pool_id.strip():
        raise ValueError(f"service {service_id!r}: pool_id must be a non-empty string")
    ints = {}
    for key in ("max_claim_bytes", "reserve_bytes", "retry_after_seconds", "timeout_seconds"):
        value = block.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"service {service_id!r}: {key} must be a positive integer")
        ints[key] = value
    observer = block.get("observer")
    if not isinstance(observer, Mapping) or set(observer) != _OBSERVER_KEYS:
        raise ValueError(f"service {service_id!r}: observer must hold exactly {sorted(_OBSERVER_KEYS)}")
    path = observer["path"]
    if not isinstance(path, str) or not path.startswith("/") or "://" in path or "?" in path or "#" in path:
        raise ValueError(f"service {service_id!r}: observer path must be a canonical relative path")
    for key in ("free_pointer", "total_pointer"):
        if not valid_pointer(observer[key]):
            raise ValueError(f"service {service_id!r}: {key} must be a valid RFC 6901 pointer")
    if not isinstance(observer["service"], str) or not observer["service"].strip():
        raise ValueError(f"service {service_id!r}: observer service must be a non-empty service id")
    return ExclusivePoolSpec(
        pool_id=pool_id.strip(),
        max_claim_bytes=ints["max_claim_bytes"],
        reserve_bytes=ints["reserve_bytes"],
        retry_after_seconds=ints["retry_after_seconds"],
        timeout_seconds=ints["timeout_seconds"],
        observer_service=observer["service"].strip(),
        observer_path=path,
        free_pointer=observer["free_pointer"],
        total_pointer=observer["total_pointer"],
    )


def valid_pointer(pointer: Any) -> bool:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        return False
    return all(_valid_escapes(token) for token in pointer.split("/")[1:]) if pointer else True


def _valid_escapes(token: str) -> bool:
    index = 0
    while index < len(token):
        if token[index] == "~":
            if index + 1 >= len(token) or token[index + 1] not in "01":
                return False
            index += 2
        else:
            index += 1
    return True


def resolve_pointer(document: Any, pointer: str) -> Any:
    """RFC 6901 lookup. Raises KeyError or IndexError when the pointer does not name a value."""
    node = document
    if not pointer:
        return node
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            node = node[token]
        elif isinstance(node, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise KeyError(token)
            node = node[int(token)]
        else:
            raise KeyError(token)
    return node


@dataclass(frozen=True)
class PoolObservation:
    free_bytes: int
    total_bytes: int


def observation_from_document(document: Any, spec: ExclusivePoolSpec) -> PoolObservation:
    """Read the free and total pointers. Both must be integers (not booleans), with total > 0 and 0 <= free <= total."""
    try:
        free = resolve_pointer(document, spec.free_pointer)
        total = resolve_pointer(document, spec.total_pointer)
    except (KeyError, IndexError, TypeError) as error:
        raise PoolObserverError("observer facts are missing") from error
    for value in (free, total):
        if isinstance(value, bool) or not isinstance(value, int):
            raise PoolObserverError("observer facts must be integers")
    if total <= 0 or free < 0 or free > total:
        raise PoolObserverError("observer facts are out of range")
    return PoolObservation(free_bytes=free, total_bytes=total)


def snapshot_and_claim(spec: ExclusivePoolSpec, observation: PoolObservation, *,
                       observed_at: float | None) -> tuple[CapacitySnapshot, ResourceClaim]:
    """The design's exact arithmetic, expressed as the existing capacity and claim types."""
    reserve_mb = math.ceil(spec.reserve_bytes / MIB)
    claim_mb = math.ceil(spec.max_claim_bytes / MIB)
    total_mb = observation.total_bytes // MIB
    used_mb = math.ceil((observation.total_bytes - observation.free_bytes) / MIB) + reserve_mb
    snapshot = CapacitySnapshot(
        total_vram_mb=total_mb,
        used_vram_mb=used_mb,
        active_exclusive_locks=frozenset(),
        observed_at=observed_at,
        quality="measured",
    )
    claim = ResourceClaim(
        gpu_required=True,
        vram_mb=claim_mb,
        exclusive_locks=(spec.pool_id,),
        queue_class=QueueClass.INTERACTIVE,
        profile_id=None,
        reasons=(f"exclusive pool {spec.pool_id}",),
    )
    return snapshot, claim


class ExclusivePoolLeaseManager:
    """One per gateway process. Per-pool locks; different pools never wait on each other."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._claims: dict[tuple[str, str], ResourceClaim] = {}

    def active_claims(self) -> int:
        return len(self._claims)

    @asynccontextmanager
    async def lease(
        self,
        spec: ExclusivePoolSpec,
        *,
        request_id: str,
        observe: Callable[[], Awaitable[Any]],
        observed_at: Callable[[], float] | None = None,
    ) -> AsyncGenerator[ResourceClaim]:
        lock = self._locks.setdefault(spec.pool_id, asyncio.Lock())
        async with lock:
            document = await observe()
            observation = observation_from_document(document, spec)
            snapshot, claim = snapshot_and_claim(
                spec, observation, observed_at=observed_at() if observed_at else None,
            )
            decision = evaluate_admission(AdmissionRequest(request_id, claim), snapshot)
            if decision.status is not AdmissionStatus.ACCEPTED:
                raise PoolCapacityError(decision.status)
            key = (spec.pool_id, request_id)
            self._claims[key] = claim
            try:
                yield claim
            finally:
                if self._claims.get(key) is claim:
                    del self._claims[key]

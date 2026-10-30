"""Opt-in, process-local GPU admission around complete lifecycle transactions.

Device locks are reservations: overlapping loads cannot use the same capacity
sample. They remain held through verification and recovery. No resident-memory
estimate, eviction, or engine-specific policy is inferred here.
"""
from __future__ import annotations

import asyncio
import math
import time
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from gateway.engine_observations import Observation
from gateway.resource_awareness import ResourceObserver
from gateway.resource_claims import AdmissionRequest, CapacitySnapshot, ResourceClaim, evaluate_admission
from gateway.engine_switch import SwitchUnavailable


# Nested prepare/load operations belong to the outer selection transaction.
_ACTIVE: ContextVar[tuple[object, str] | None] = ContextVar("resource_transaction", default=None)


def management_policy(service: Mapping[str, Any]) -> Mapping[str, Any] | None:
    resources = service.get("resources")
    if not isinstance(resources, Mapping):
        resources = (service.get("config") or {}).get("resources") or {}
    policy = resources.get("management")
    if policy is None:
        return None
    if not isinstance(policy, Mapping) or policy.get("mode") != "capacity":
        raise SwitchUnavailable("invalid resource management policy")
    return policy


def _mb(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


async def live_devices() -> list[dict] | None:
    # Reuse the device observation source, never the aged owner-status cache.
    from status_endpoint import _gpus_raw
    return await asyncio.to_thread(_gpus_raw)


@dataclass
class _Transaction:
    settled: bool = False


class ResourceCoordinator:
    """Serialize multi-device transactions in UUID order; independent cards run concurrently.

    Capacity includes all device memory use, including external consumers. Claims
    specify *additional* peak allocation, without credit for an expected unload.
    A new sample is taken only after all requested device locks have been acquired.
    """

    def __init__(self, *, sample: Callable[[], Awaitable[list[dict] | None]] = live_devices,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sample = sample
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[object, dict] = {}
        self._uncertain: dict[str, set[str]] = {}

    def snapshot(self) -> list[dict]:
        return [dict(row, devices=dict(row["devices"])) for row in self._pending.values()] + [
            {"state": "uncertain", "device": uid, "services": sorted(services)}
            for uid, services in sorted(self._uncertain.items())]

    async def reconcile(self, service: Mapping[str, Any], *, observe) -> bool:
        """Clear only this service's uncertainty after its declared quiescence proof.

        The fact means no outstanding control work can allocate later, not merely
        that a process is healthy, idle, or currently uses zero device memory.
        """
        policy = management_policy(service)
        sid = str(service.get("id") or "")
        name = (policy or {}).get("observe_quiescent")
        if not isinstance(name, str) or not name:
            return False
        devices = sorted(uid for uid, owners in self._uncertain.items() if sid in owners)
        async with AsyncExitStack() as stack:
            for uid in devices:
                await stack.enter_async_context(self._locks.setdefault(uid, asyncio.Lock()))
            proof = await observe(name)
            if not isinstance(proof, Observation) or not proof.known or proof.value is not True:
                return False
            for uid in devices:
                owners = self._uncertain.get(uid)
                if owners is not None:
                    owners.discard(sid)
                    if not owners:
                        del self._uncertain[uid]
        return True

    @asynccontextmanager
    async def transaction(self, service: Mapping[str, Any], *, observe, offering: Mapping[str, Any] | None = None):
        policy = management_policy(service)
        sid = str(service.get("id") or "")
        if policy is None or _ACTIVE.get() == (self, sid):
            yield _Transaction()
            return
        if not sid:
            raise SwitchUnavailable("resource management requires a service id")
        devices = policy.get("devices")
        # An offering can provide model-specific requirements through the same
        # resources block. It cannot change the owner's device placement policy.
        offering_resources = (offering or {}).get("resources") or {}
        if not isinstance(offering_resources, Mapping):
            raise SwitchUnavailable("invalid offering resource demand")
        requirements = offering_resources.get("devices", devices)
        if (not isinstance(devices, Mapping) or not devices
                or not isinstance(requirements, Mapping) or set(requirements) != set(devices)
                or any(not isinstance(uid, str) or not uid for uid in devices)):
            raise SwitchUnavailable("resource management requires explicit device UUIDs and claims")
        headroom = policy.get("headroom_mb", 0)
        wait_s = policy.get("queue_wait_s", 60)
        if (not _mb(headroom) or isinstance(wait_s, bool) or not isinstance(wait_s, (int, float))
                or not math.isfinite(wait_s) or not 0 < wait_s <= 900):
            raise SwitchUnavailable("invalid resource management limits")
        # Bound the whole queue wait, including partial multi-card acquisition.
        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(wait_s):
                    for uid in sorted(devices):
                        await stack.enter_async_context(self._locks.setdefault(uid, asyncio.Lock()))
            except TimeoutError as exc:
                raise SwitchUnavailable("resource queue wait expired") from exc
            if set(self._uncertain).intersection(devices):
                raise SwitchUnavailable("previous resource operation is unresolved; operator reconciliation required")
            claims = {}
            for uid, spec in requirements.items():
                if not isinstance(spec, Mapping):
                    raise SwitchUnavailable("resource demand is unknown")
                if "observe_mb" in spec:
                    result = await observe(str(spec["observe_mb"]))
                    demand = result.value if isinstance(result, Observation) and result.known else None
                else:
                    demand = spec.get("additional_peak_mb")
                if not _mb(demand):
                    raise SwitchUnavailable("resource demand is unknown")
                claims[uid] = demand + headroom
            sampled_at = self._clock()
            try:
                async with asyncio.timeout(5):
                    gpus = await self._sample()
            except Exception as exc:
                raise SwitchUnavailable("device capacity is unavailable") from exc
            view = ResourceObserver(fresh_s=5).observe(
                gpus, None, {}, now=self._clock(), sampled_at=sampled_at)
            capacity = {row["uuid"]: row for row in view["devices"]}
            for uid, demand in claims.items():
                row = capacity.get(uid) or {}
                decision = evaluate_admission(AdmissionRequest(sid, ResourceClaim(gpu_required=True, vram_mb=demand)),
                    CapacitySnapshot(total_vram_mb=row.get("total_mb"), used_vram_mb=row.get("used_mb"),
                                     quality=row.get("quality", "unavailable")))
                if not decision.accepted:
                    raise SwitchUnavailable("; ".join(decision.reasons))
            key = object()
            self._pending[key] = {"service_id": sid, "devices": claims}
            token = _ACTIVE.set((self, sid))
            transaction = _Transaction()
            try:
                yield transaction
            except BaseException:
                # A failed HTTP operation may still be allocating remotely.
                # A later free-memory sample cannot prove it has stopped.
                if not transaction.settled:
                    for uid in devices:
                        self._uncertain.setdefault(uid, set()).add(sid)
                raise
            finally:
                _ACTIVE.reset(token)
                self._pending.pop(key, None)


RESOURCE_COORDINATOR = ResourceCoordinator()


@asynccontextmanager
async def resource_transaction(service, *, observe, offering=None):
    async with RESOURCE_COORDINATOR.transaction(service, observe=observe, offering=offering) as transaction:
        yield transaction


async def managed_operation(service, operation, *, observe, offering=None):
    """Keep reservations until the operation settles, even after caller cancellation."""
    from gateway.lifecycle_drivers import _finish_even_if_cancelled

    # Queue cancellation must stop before mutation. Shield only after admission.
    async with resource_transaction(service, observe=observe, offering=offering) as transaction:
        async def change():
            result = await operation()
            if result is False:
                raise SwitchUnavailable("resource operation could not be verified")
            transaction.settled = True
            return result

        return await _finish_even_if_cancelled(change())

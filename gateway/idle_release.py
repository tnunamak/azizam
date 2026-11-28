"""Free the memory idle engines keep on a pool's GPUs, using only what each service record declares.

An engine declares how to free its memory with the residency contract: ``residency.unload`` names an operation that
``engine_control`` sends to the engine's control address. It declares which GPUs it stands on in
``resources.gpu.devices``. A pool that lists ``devices`` in its ``resource_capacity`` asks every engine whose devices
overlap. Nothing here names an engine, an operation or a path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Mapping, Sequence

from gateway.engine_contract_view import residency_config
from gateway.exclusive_pool import ENGINE_BUSY

logger = logging.getLogger(__name__)

# What one member of the release answers: True (freed), ENGINE_BUSY (running work), False (nothing freed).
Answer = bool | str


def _declared_devices(service: Mapping[str, Any]) -> frozenset[int]:
    """``resources.gpu.devices`` of the record, or nothing when it is absent or malformed."""
    resources = service.get("resources")
    if not isinstance(resources, Mapping):
        config = service.get("config")
        resources = config.get("resources") if isinstance(config, Mapping) else None
    gpu = resources.get("gpu") if isinstance(resources, Mapping) else None
    devices = gpu.get("devices") if isinstance(gpu, Mapping) else None
    if (not isinstance(devices, list) or not devices
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in devices)):
        return frozenset()
    return frozenset(devices)


def declared_unloaders(services: Mapping[str, Any], devices: Sequence[int]) -> list[str]:
    """The ids of the enabled services that declare an unload and stand on at least one of ``devices``, sorted."""
    wanted = frozenset(devices)
    found = []
    for service_id, service in services.items():
        if not isinstance(service, Mapping) or service.get("enabled") is False:
            continue
        unload = residency_config(service).get("unload")
        if not isinstance(unload, Mapping) or not unload.get("op"):
            continue
        if wanted & _declared_devices(service):
            found.append(str(service_id))
    return sorted(found)


async def release_engine(
    service_id: str, *, begin_unload: Callable[[str], bool], end_unload: Callable[[str], None],
    unload: Callable[[str], Awaitable[bool]],
) -> Answer:
    """Unload one engine unless it is serving. An engine that is serving answers ENGINE_BUSY and is left alone.

    ``begin_unload`` is the exclusion, not a reading: it refuses when the engine serves, and once it answers True no new
    request starts on the engine until ``end_unload`` (a request that arrives waits, then runs).

    ``unload`` must send its request from this task (await it directly; do not hand it to a new task). The lease
    belongs to this task, so a request sent from another task waits on the lease's own gate until ``end_unload``.
    """
    if not begin_unload(service_id):
        return ENGINE_BUSY
    try:
        return await unload(service_id) is True
    finally:
        end_unload(service_id)


async def release_together(members: Sequence[Callable[[], Awaitable[Answer]]]) -> Answer:
    """Ask every member at once. One that raises is logged and counts as nothing freed; it never stops the others.

    Any member freed memory: True. Otherwise any member is busy: ENGINE_BUSY. Otherwise False.
    """
    answers = await asyncio.gather(*(member() for member in members), return_exceptions=True)
    for answer in answers:
        if isinstance(answer, BaseException):
            if not isinstance(answer, Exception):
                raise answer
            logger.warning("idle release member failed", exc_info=answer)
    if any(answer is True for answer in answers):
        return True
    return ENGINE_BUSY if any(answer == ENGINE_BUSY for answer in answers) else False

"""Declared load, unload, sleep and wake operations with separate facts."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Mapping

from gateway.engine_observations import Observation


def _value(result: Any) -> Any:
    return result.value if isinstance(result, Observation) and result.known else (None if isinstance(result, Observation) else result)


class ResidencyController:
    def __init__(
        self,
        *,
        invoke: Callable[[str, Mapping[str, Any]], Awaitable[Any]],
        observe: Callable[[str], Awaitable[Any]],
    ) -> None:
        self._invoke = invoke
        self._observe = observe

    async def load(self, service: Mapping[str, Any]) -> bool:
        config = service.get("residency") or {}
        operation = config.get("load")
        if not isinstance(operation, Mapping) or not operation.get("op"):
            return False
        deadline = config.get("load_deadline") or {}
        stall_s = max(0.01, float(deadline.get("stall_s", 120)))
        max_s = max(stall_s, float(deadline.get("max_s", 900)))
        progress_name = deadline.get("progress")
        started = time.monotonic()
        stalled_at = started
        previous_progress = None
        async def within_deadline(awaitable):
            remaining = min(started + max_s, stalled_at + stall_s) - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError
            return await asyncio.wait_for(awaitable, timeout=remaining)

        try:
            await within_deadline(self._invoke(str(operation["op"]), operation.get("args") or {}))
            while time.monotonic() - started < max_s and time.monotonic() - stalled_at < stall_s:
                if _value(await within_deadline(self._observe("resident"))) is True:
                    return True
                if progress_name and progress_name != "none":
                    progress = _value(await within_deadline(self._observe(str(progress_name))))
                    if progress is not None and progress != previous_progress:
                        previous_progress = progress
                        stalled_at = time.monotonic()
                await asyncio.sleep(min(0.05, stall_s / 4))
        except asyncio.TimeoutError:
            pass
        return False

    async def unload(self, service: Mapping[str, Any]) -> bool:
        return await self._operate_and_verify(service, "unload", "resident", False)

    async def sleep(self, service: Mapping[str, Any]) -> bool:
        return await self._operate_and_verify(service, "sleep", "sleep_state", True)

    async def wake(self, service: Mapping[str, Any]) -> bool:
        return await self._operate_and_verify(service, "wake", "sleep_state", False)

    async def _operate_and_verify(
        self, service: Mapping[str, Any], operation_name: str, observation_name: str, expected: bool
    ) -> bool:
        config = service.get("residency") or {}
        operation = config.get(operation_name)
        if not isinstance(operation, Mapping) or not operation.get("op"):
            return False
        await self._invoke(str(operation["op"]), operation.get("args") or {})
        return _value(await self._observe(observation_name)) is expected

"""Exclusive selection transaction for an engine with one selected offering."""

from __future__ import annotations

import asyncio
import weakref
from contextlib import aclosing
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from gateway.engine_observations import Observation
from gateway.swap_drain import ServingLease, SwapDeferred, SwapDrain, SwapDrainSettings


class SwitchUnavailable(Exception):
    retry_after_s = 30


def _value(result: Any) -> Any:
    return result.value if isinstance(result, Observation) and result.known else (None if isinstance(result, Observation) else result)


class SwitchLease:
    """Keep selection locked until the response has finished or been closed."""

    def __init__(self, lock: asyncio.Lock, serving: ServingLease) -> None:
        self._lock = lock
        self._serving = serving
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._serving.release()
        self._lock.release()

    def hand_off(self, stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async def owned() -> AsyncIterator[bytes]:
            try:
                async with aclosing(stream) as source:
                    async for chunk in source:
                        yield chunk
            finally:
                self.release()

        wrapped = owned()
        weakref.finalize(wrapped, self.release)
        return wrapped


class SwitchController:
    """Call owner-declared selection operations behind the existing swap drain."""

    def __init__(
        self,
        *,
        drain: SwapDrain,
        invoke: Callable[[str, Mapping[str, Any]], Awaitable[Any]],
        observe: Callable[[str], Awaitable[Any]],
        lock_for: Callable[[str], asyncio.Lock] | None = None,
    ) -> None:
        self._drain = drain
        self._invoke = invoke
        self._observe = observe
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_for = lock_for or (lambda scope: self._locks.setdefault(scope, asyncio.Lock()))

    async def select(
        self, service: Mapping[str, Any], offering_id: str, selection: Mapping[str, Any]
    ) -> SwitchLease:
        offerings = service.get("offerings") or []
        if offering_id not in {row.get("id") for row in offerings if isinstance(row, Mapping)}:
            raise ValueError("unknown offering")
        if selection.get("mode") != "switch":
            raise ValueError("selection.mode must be switch")
        spec = selection.get("switch") or {}
        op = spec.get("call")
        observation = spec.get("observe_selected")
        args = spec.get("args")
        if not isinstance(op, str) or not isinstance(observation, str) or not isinstance(args, Mapping):
            raise ValueError("switch requires call, args and observe_selected")
        scope = str(service.get("id") or "") if spec.get("exclusive_scope", "engine") == "engine" else str(spec["exclusive_scope"])
        if not scope:
            raise ValueError("switch requires a scope")
        lock = self._lock_for(scope)
        queue_wait = max(0.01, float(spec.get("queue_wait_s", 60)))
        try:
            await asyncio.wait_for(lock.acquire(), timeout=queue_wait)
        except asyncio.TimeoutError as exc:
            raise SwitchUnavailable("selection queue full") from exc
        handed_off = False
        try:
            drain_timeout = max(0.01, float(spec.get("drain_timeout_s", 30)))
            await self._drain.clear_for_swap(scope, SwapDrainSettings(enabled=True, timeout_s=drain_timeout))
            previous = _value(await self._observe(observation))
            await self._invoke(op, args)
            selected = _value(await self._observe(observation))
            ready_name = spec.get("observe_ready")
            ready = True if not ready_name else _value(await self._observe(str(ready_name))) is True
            if selected != offering_id or not ready:
                if spec.get("on_failure", "fail") == "restore" and isinstance(previous, str):
                    restore_args = {key: previous if value == offering_id else value for key, value in args.items()}
                    try:
                        await self._invoke(op, restore_args)
                        if _value(await self._observe(observation)) != previous:
                            raise SwitchUnavailable("previous selection could not be verified")
                    except Exception as exc:
                        raise SwitchUnavailable("selection failed and restore failed") from exc
                raise SwitchUnavailable("selection could not be verified")
            lease = SwitchLease(lock, self._drain.lease(scope))
            handed_off = True
            return lease
        except SwapDeferred as exc:
            raise SwitchUnavailable(str(exc)) from exc
        finally:
            if not handed_off:
                lock.release()

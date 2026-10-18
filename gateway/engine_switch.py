"""Exclusive selection transaction for an engine with one selected offering."""

from __future__ import annotations

import asyncio
import weakref
from contextlib import aclosing
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from fastapi import HTTPException

from gateway.engine_observations import Observation
from gateway.swap_drain import ServingLease, SwapDeferred, SwapDrain, SwapDrainSettings


class SwitchUnavailable(HTTPException):
    def __init__(self, detail: str, *, retry_after_s: int = 30) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(status_code=503, detail=detail, headers={"Retry-After": str(retry_after_s)})


def _value(result: Any) -> Any:
    return result.value if isinstance(result, Observation) and result.known else (None if isinstance(result, Observation) else result)


class SwitchLease:
    """Keep selection locked until the response has finished or been closed."""

    def __init__(self, lock: asyncio.Lock, serving: ServingLease) -> None:
        self._lock = lock
        self._serving = serving
        self._released = False
        self.handed_off = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._serving.release()
        self._lock.release()

    def hand_off(self, stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        self.handed_off = True
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
        self, service: Mapping[str, Any], offering_id: str, selection: Mapping[str, Any],
        *, on_acquired: Callable[[], None] | None = None,
        on_release: Callable[[], None] | None = None,
        prepare: Callable[[], Awaitable[bool]] | None = None,
    ) -> SwitchLease:
        offerings = service.get("offerings") or []
        offering = next((row for row in offerings if isinstance(row, Mapping) and row.get("id") == offering_id), None)
        if offering is None:
            raise ValueError("unknown offering")
        selected_identity = offering.get("upstream") or offering_id
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
        activity_started = False
        try:
            if on_acquired is not None:
                on_acquired()
                activity_started = True
            drain_timeout = max(0.01, float(spec.get("drain_timeout_s", 30)))
            await self._drain.clear_for_swap(scope, SwapDrainSettings(enabled=True, timeout_s=drain_timeout))
            if prepare is not None and not await prepare():
                raise SwitchUnavailable("residency could not be prepared")
            previous = _value(await self._observe(observation))
            try:
                await self._invoke(op, args)
                selected = _value(await self._observe(observation))
                declared = service.get("observations") or {}
                ready_name = spec.get("observe_ready") or ("offering_ready" if "offering_ready" in declared else None)
                ready = True if not ready_name else _value(await self._observe(str(ready_name))) is True
                if selected != selected_identity or not ready:
                    raise SwitchUnavailable("selection could not be verified")
            except BaseException as exc:
                if spec.get("on_failure", "fail") == "restore" and isinstance(previous, str):
                    restore_args = {
                        key: previous if value == selected_identity else value for key, value in args.items()
                    }
                    try:
                        from gateway.lifecycle_drivers import _finish_even_if_cancelled

                        await _finish_even_if_cancelled(self._restore(op, restore_args, observation, previous))
                    except asyncio.CancelledError:
                        raise
                    except BaseException as recovery_exc:
                        raise SwitchUnavailable("selection failed and restore failed") from recovery_exc
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise SwitchUnavailable("selection failed or is unknown") from exc
            lease = SwitchLease(lock, self._drain.lease(scope, on_release=on_release))
            handed_off = True
            return lease
        except SwapDeferred as exc:
            raise SwitchUnavailable(str(exc)) from exc
        finally:
            if not handed_off:
                if activity_started and on_release is not None:
                    on_release()
                lock.release()

    async def _restore(self, op: str, args: Mapping[str, Any], observation: str, previous: str) -> None:
        await self._invoke(op, args)
        if _value(await self._observe(observation)) != previous:
            raise SwitchUnavailable("previous selection could not be verified")

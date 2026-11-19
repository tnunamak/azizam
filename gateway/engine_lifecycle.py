"""The owner's start, stop and restart of an engine, over the lifecycle driver the request path uses.

One entry point, ``EngineLifecycle.apply``. It picks the driver with ``driver_for_service`` (the lookup
``ensure_target`` makes), so it has no branch per engine and no engine name. It answers one of five results:

* ``done``        the action took effect and the evidence says so (a stopped engine's port refuses; a started
                  engine passed its readiness check).
* ``not_managed`` Azizam does not own this server, so it did nothing.
* ``busy``        a request is in flight on the engine; nothing was stopped (an owner action must not kill a
                  member's job; the engine's own idle stop waits for the same fact).
* ``unverified``  the action ran but its effect could not be confirmed. The state is ``unknown``, never green.
* ``refused``     Azizam may not do this now (host commands are off, or the engine's residency is not ours).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from gateway import host_commands, service_registry
from gateway.engine_contract_view import process_config
from gateway.engine_protocol.errors import ProtocolError
from gateway.lifecycle_drivers import ProcessState, driver_for_service
from gateway.transition_gate import TransitionUnavailable, policy_for, require_supported

ACTIONS = ("start", "stop", "restart")

NOT_MANAGED_WHY = "Azizam does not own this server; stop it where it runs."
COMMANDS_OFF_WHY = "Allow Azizam to run commands on this computer is off"

# How long a start may wait for readiness when the record declares no budget, and the bounds on a declared one.
_DEFAULT_START_S = 30.0
_MIN_START_S = 5.0
_MAX_START_S = 120.0
# How long a stop is checked when the record declares no stop timeout (the driver's own default and cap).
_DEFAULT_STOP_S = 5.0
_MAX_STOP_S = 30.0
_CONNECT_S = 0.25
# How long a stop waits for the engine's residency lock (a request making its target ready, or a swap) before it answers busy.
_LOCK_WAIT_S = 5.0
_POLL_S = 0.05


@dataclass(frozen=True)
class LifecycleAnswer:
    service_id: str
    action: str
    result: str
    state: str
    why: str

    @property
    def status_code(self) -> int:
        return 409 if self.result in ("busy", "refused") else 200

    def public(self) -> dict[str, str]:
        return {"service_id": self.service_id, "action": self.action, "result": self.result,
                "state": self.state, "why": self.why}


def _number(value: Any, default: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return min(max(float(value), low), high)


class EngineLifecycle:
    """Owner lifecycle actions. ``backends`` is the live ``config["backends"]``; ``in_flight`` counts the serving
    leases the request path holds on an engine (``SwapDrain.in_flight``); ``lock_for`` is the residency domain's swap
    lock (``proxy.swap_lock_for``). The request path mints its lease inside a hold of that lock, so a stop that holds
    it from the busy check to the end of the stop cannot cut off a request that arrived after the check."""

    def __init__(self, *, backends: Callable[[], Mapping[str, Any]], in_flight: Callable[[str], int],
                 lock_for: Callable[[str], asyncio.Lock] | None = None,
                 commands_allowed: Callable[[], bool] | None = None, verify_s: float | None = None) -> None:
        self._backends = backends
        self.in_flight = in_flight
        self._lock_for = lock_for
        self._commands_allowed = commands_allowed or host_commands.commands_allowed
        self._verify_s = verify_s
        self._locks: dict[str, asyncio.Lock] = {}

    async def apply(self, service_id: str, action: str) -> LifecycleAnswer:
        if action not in ACTIONS:
            raise ValueError(f"unknown lifecycle action {action!r}")
        # One owner action at a time per engine: a start never interleaves with a stop.
        async with self._locks.setdefault(service_id, asyncio.Lock()):
            return await self._apply(service_id, action)

    # -- the decision ---------------------------------------------------------------------------

    async def _apply(self, service_id: str, action: str) -> LifecycleAnswer:
        def answer(result: str, state: str, why: str) -> LifecycleAnswer:
            return LifecycleAnswer(service_id, action, result, state, why)

        backends = self._backends() or {}
        svc = {**(backends.get(service_id) or {}), "id": service_id}
        if policy_for(service_id, backends).lease_only:
            # Another owner runs this process, or the transition executor does: never start or stop it here.
            return answer("not_managed", ProcessState.UNMANAGED, NOT_MANAGED_WHY)
        try:
            driver = driver_for_service(service_id, backends)
        except ProtocolError:
            # A record bound to a remote destination has no process on this computer to manage.
            return answer("not_managed", ProcessState.UNMANAGED, NOT_MANAGED_WHY)
        if not getattr(driver, "owns_process", True):
            return answer("not_managed", ProcessState.UNMANAGED, NOT_MANAGED_WHY)
        try:
            require_supported(policy_for(service_id, backends), service_id)
        except TransitionUnavailable as exc:
            return answer("refused", ProcessState.UNKNOWN, f"This engine's residency domain is not admitting changes: {exc.reason}")
        starts = action in ("start", "restart")
        if starts and str(process_config(svc).get("driver") or "").strip().lower() in host_commands.LEGACY_DRIVERS and not self._commands_allowed():
            # Refuse a restart before it stops anything: an engine Azizam cannot start again is not left down.
            return answer("refused", await self._state(driver, svc), COMMANDS_OFF_WHY)
        if action in ("stop", "restart"):
            lock = self._lock_for(service_id) if self._lock_for is not None else None
            if lock is not None:
                try:
                    await asyncio.wait_for(lock.acquire(), timeout=_LOCK_WAIT_S)
                except asyncio.TimeoutError:
                    return answer("busy", await self._state(driver, svc),
                                  "This engine is busy making a model ready; stop it when that finishes.")
            try:
                running = self.in_flight(service_id)
                if running > 0:
                    return answer("busy", await self._state(driver, svc),
                                  f"{running} request{'s are' if running != 1 else ' is'} still running on this engine; "
                                  "stop it when they finish.")
                stopped = await self._stop(driver, svc, answer)
            finally:
                if lock is not None:
                    lock.release()
            if stopped.result != "done" or action == "stop":
                return stopped
        return await self._start(driver, svc, answer)

    # -- the two effects ---------------------------------------------------------------------------

    async def _stop(self, driver: Any, svc: Mapping[str, Any], answer: Callable[..., LifecycleAnswer]) -> LifecycleAnswer:
        stop_async = getattr(driver, "stop_async", None)
        if stop_async is not None:
            await stop_async(svc)
        else:
            await asyncio.to_thread(driver.stop, svc)
        closed = await self._port_closes(svc)
        if closed is True:
            return answer("done", ProcessState.STOPPED, "The engine stopped and its address no longer answers.")
        if closed is None:
            return answer("unverified", ProcessState.UNKNOWN,
                          "The engine names no address Azizam can check, so it cannot confirm the stop.")
        return answer("unverified", ProcessState.UNKNOWN,
                      "The engine still answers on its address after the stop, so Azizam cannot confirm it stopped.")

    async def _start(self, driver: Any, svc: Mapping[str, Any], answer: Callable[..., LifecycleAnswer]) -> LifecycleAnswer:
        budget = self._start_budget(svc)
        try:
            ready = await asyncio.wait_for(driver.ensure_ready(svc, timeout_s=budget), timeout=budget + 10)
        except asyncio.TimeoutError:
            ready = False
        if ready:
            return answer("done", ProcessState.RUNNING, "The engine started and passed its readiness check.")
        return answer("unverified", ProcessState.UNKNOWN, "The engine did not pass its readiness check in time.")

    @staticmethod
    def _start_budget(svc: Mapping[str, Any]) -> float:
        declared = process_config(svc).get("ready_timeout_s")
        if declared is None:
            declared = ((svc.get("lifecycle") or {}).get("driver_config") or {}).get("health_timeout_s")
        return _number(declared, _DEFAULT_START_S, _MIN_START_S, _MAX_START_S)

    @staticmethod
    async def _state(driver: Any, svc: Mapping[str, Any]) -> str:
        try:
            return str(await driver.status(svc))
        except Exception:  # noqa: BLE001 - a status that cannot be read is unknown, never an error for the owner
            return ProcessState.UNKNOWN

    # -- evidence ------------------------------------------------------------------------------------

    async def _port_closes(self, svc: Mapping[str, Any]) -> bool | None:
        """True once the engine's address refuses a connection; False if it still answers at the deadline; None
        when the record names no address to check."""
        from urllib.parse import urlsplit

        parsed = urlsplit(service_registry.engine_origin(svc))
        try:
            host, port = parsed.hostname, parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
        except ValueError:
            return None
        if not host or not port:
            return None
        window = self._verify_s
        if window is None:
            window = _number(process_config(svc).get("stop_timeout_s"), _DEFAULT_STOP_S, 0.01, _MAX_STOP_S)
        deadline = time.monotonic() + window
        while True:
            try:
                _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=_CONNECT_S)
            except ConnectionRefusedError:
                return True
            except (OSError, asyncio.TimeoutError):
                # A timeout or an unreachable host is not proof that the engine is gone.
                pass
            else:
                writer.close()
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_POLL_S)

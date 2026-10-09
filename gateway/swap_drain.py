"""Let a model swap wait for the requests the loaded model is still serving.

A swap-capable backend (one engine process that serves one loaded target at a
time, e.g. a llama.cpp unit restarted with a new drop-in) loses every request
in flight when it swaps. This module counts those requests and lets the swap
wait for them, with hard bounds so a stuck or leaked request cannot block
swaps forever.

Two roles share one per-backend swap lock (``proxy.swap_lock_for``):

- The forwarding hop takes a ``ServingLease`` while holding the lock, after
  it made its target ready, and releases it after the final upstream byte,
  on error, or on cancellation. A streaming response outlives its route
  handler, so the stream itself releases the lease (``ServingLease.hand_off``).
- The swapper, holding the lock, calls ``clear_for_swap`` before a swap that
  would interrupt the backend. It returns when no lease is live, or raises
  ``SwapDeferred`` when ``timeout_s`` passes first. On success it does not
  release the lock, so no new lease can start until the swap is done: requests
  that arrive meanwhile queue behind the swap instead of starving it.

Deadlock rule: a request holds a lease only around its own upstream forward,
never while it waits for the lock. A request that is between its outer and
inner ``ensure_target`` calls holds no lease, so no swap waits for it.

A lease older than ``lease_max_age_s`` no longer counts. That bounds the cost
of a lease that a code path forgot to release.

Engine-agnostic: backends and targets are opaque strings.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from contextlib import aclosing
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Mapping, Optional

DEFAULT_TIMEOUT_S = 120.0
DEFAULT_LEASE_MAX_AGE_S = 1800.0
# The drain cannot predict when the requests in flight end. 30 s is a
# polite first retry for a client; it is not a promise.
RETRY_AFTER_S = 30


def _positive_float(value: object, default: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


@dataclass(frozen=True)
class SwapDrainSettings:
    """``llm.swap_drain`` in the gateway config.

    enabled          -- off by default: a swap then restarts at once, as before.
    timeout_s        -- longest a swap waits for requests in flight.
    lease_max_age_s  -- age after which a lease stops counting (leak bound).
    """

    enabled: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S
    lease_max_age_s: float = DEFAULT_LEASE_MAX_AGE_S

    @classmethod
    def from_llm_config(cls, llm_cfg: Optional[Mapping[str, object]]) -> "SwapDrainSettings":
        block = (llm_cfg or {}).get("swap_drain") if isinstance(llm_cfg, Mapping) else None
        if not isinstance(block, Mapping):
            return cls()
        return cls(
            enabled=bool(block.get("enabled", False)),
            timeout_s=_positive_float(block.get("timeout_s"), DEFAULT_TIMEOUT_S),
            lease_max_age_s=_positive_float(block.get("lease_max_age_s"), DEFAULT_LEASE_MAX_AGE_S),
        )


class SwapDeferred(Exception):
    """A swap did not happen because the backend is still serving requests.

    Nothing was changed: the loaded target, its drop-in and its requests are
    as they were. Callers answer 503 with ``Retry-After``.
    """

    def __init__(self, backend: str, *, in_flight: int, waited_s: float) -> None:
        self.backend = backend
        self.in_flight = in_flight
        self.waited_s = waited_s
        self.retry_after_s = RETRY_AFTER_S
        super().__init__(
            f"backend {backend!r} is still serving {in_flight} request(s) on its "
            f"loaded model after {waited_s:.0f}s; swap deferred"
        )


class ServingLease:
    """One request in flight on one backend. ``release`` is idempotent."""

    def __init__(self, drain: "SwapDrain", backend: str, target: Optional[str], started_at: float) -> None:
        self._drain = drain
        self.backend = backend
        self.target = target
        self.started_at = started_at
        self.handed_off = False
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._drain._release(self)

    def hand_off(self, stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Give the lease to a response stream.

        The returned stream releases the lease after the final byte, on error,
        on cancellation, when it is closed early (it closes ``stream`` first),
        or when it is dropped without ever being started.
        """
        self.handed_off = True

        async def owned() -> AsyncIterator[bytes]:
            try:
                async with aclosing(stream) as source:
                    async for chunk in source:
                        yield chunk
            finally:
                self.release()

        wrapped = owned()
        # A StreamingResponse that never starts its body never runs ``finally``.
        weakref.finalize(wrapped, self.release)
        return wrapped


class SwapDrain:
    """Serving leases per backend, and the bounded wait a swap does on them."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._leases: dict[str, set[ServingLease]] = {}
        self._ready_target: dict[str, str] = {}
        self._waiters: dict[str, set[asyncio.Event]] = {}

    # -- forwarding side ----------------------------------------------------

    def lease(self, backend: str, target: Optional[str]) -> Optional[ServingLease]:
        """Count one request in flight. Call while holding the swap lock.

        ``target`` is the target the request made ready. If the backend was
        made ready for a different target since (another request swapped it),
        return None: the caller must make its target ready again. ``None``
        means "whatever is loaded" and is never refused.
        """
        ready = self._ready_target.get(backend)
        if target is not None and ready is not None and ready != target:
            return None
        lease = ServingLease(self, backend, target, self._clock())
        self._leases.setdefault(backend, set()).add(lease)
        return lease

    def mark_ready(self, backend: str, target: str) -> None:
        """Record the target the backend was last made ready for (under the lock)."""
        self._ready_target[backend] = target

    def in_flight(self, backend: str, *, max_age_s: float) -> int:
        return len(self._live(backend, max_age_s))

    # -- swapping side ------------------------------------------------------

    async def clear_for_swap(self, backend: str, settings: SwapDrainSettings) -> None:
        """Wait until no live lease remains on ``backend``. Call holding the lock.

        Raises ``SwapDeferred`` after ``settings.timeout_s``.
        """
        start = self._clock()
        deadline = start + settings.timeout_s
        live = self._live(backend, settings.lease_max_age_s)
        if live:
            print(
                f"[swap-drain] {backend}: swap waits for {len(live)} request(s) in flight "
                f"(timeout {settings.timeout_s:.0f}s)",
                flush=True,
            )
        while live:
            now = self._clock()
            remaining = deadline - now
            if remaining <= 0:
                print(f"[swap-drain] {backend}: swap deferred, {len(live)} still in flight", flush=True)
                raise SwapDeferred(backend, in_flight=len(live), waited_s=now - start)
            oldest_expires_in = min(lease.started_at for lease in live) + settings.lease_max_age_s - now
            changed = asyncio.Event()
            waiters = self._waiters.setdefault(backend, set())
            waiters.add(changed)
            try:
                await asyncio.wait_for(changed.wait(), timeout=max(0.0, min(remaining, oldest_expires_in)))
            except asyncio.TimeoutError:
                pass
            finally:
                waiters.discard(changed)
            live = self._live(backend, settings.lease_max_age_s)
        if self._clock() - start > 0.001:
            print(f"[swap-drain] {backend}: drained after {self._clock() - start:.1f}s", flush=True)

    # -- internals ----------------------------------------------------------

    def _live(self, backend: str, max_age_s: float) -> list[ServingLease]:
        now = self._clock()
        leases = self._leases.get(backend, set())
        expired = [lease for lease in leases if now - lease.started_at >= max_age_s]
        for lease in expired:
            leases.discard(lease)
            print(
                f"[swap-drain] {backend}: lease for {lease.target!r} expired after "
                f"{now - lease.started_at:.0f}s without release; no longer counted",
                flush=True,
            )
        return list(leases)

    def _release(self, lease: ServingLease) -> None:
        self._leases.get(lease.backend, set()).discard(lease)
        for changed in list(self._waiters.get(lease.backend, ())):
            changed.set()

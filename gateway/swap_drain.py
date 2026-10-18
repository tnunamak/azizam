"""Let a model swap wait for the requests the loaded model is still serving.

A swap-capable backend (one engine process that serves one loaded target at a
time, e.g. a llama.cpp unit restarted with a new drop-in) loses every request
in flight when it swaps. This module counts those requests and lets the swap
wait for them, for at most ``timeout_s``; then the swap is deferred, and the
requests in flight keep running.

Two roles share one per-backend swap lock (``proxy.swap_lock_for``):

- The forwarding hop takes a ``ServingLease`` in the same hold of the lock
  in which it made its target ready (or, for ``current``, read the loaded
  model), and releases it after the final upstream byte, on error, or on
  cancellation. So no swap can land between "this target is loaded" and
  "this request counts". A streaming response outlives its route handler, so
  the stream itself releases the lease (``ServingLease.hand_off``).
- The swapper, holding the lock, calls ``clear_for_swap`` before a swap that
  would interrupt the backend. It returns when no lease remains, or raises
  ``SwapDeferred`` when ``timeout_s`` passes first. On success it does not
  release the lock, so no new lease can start until the swap is done: requests
  that arrive meanwhile queue behind the swap instead of starving it.

Deadlock rule: a request holds a lease only around its own upstream forward,
never while it waits for the lock. A request that is between its outer and
inner ``ensure_target`` calls holds no lease, so no swap waits for it.

A lease never expires: the gateway cannot tell a leaked lease from a long,
healthy stream, and dropping a live one would let a swap cut that stream off.
``timeout_s`` bounds what a lease can cost: the swap is deferred, never
forced. A deferral logs the age of the oldest lease, so a lease that a code
path forgot to release shows up as a deferral with an implausible age.

Engine-agnostic: backends are opaque strings.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from contextlib import aclosing
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Mapping, Optional

DEFAULT_TIMEOUT_S = 120.0
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

    enabled    -- off by default: a swap then restarts at once, as before.
    timeout_s  -- longest a swap waits for requests in flight; then it is deferred.
    """

    enabled: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S

    @classmethod
    def from_llm_config(cls, llm_cfg: Optional[Mapping[str, object]]) -> "SwapDrainSettings":
        block = (llm_cfg or {}).get("swap_drain") if isinstance(llm_cfg, Mapping) else None
        if not isinstance(block, Mapping):
            return cls()
        return cls(
            enabled=bool(block.get("enabled", False)),
            timeout_s=_positive_float(block.get("timeout_s"), DEFAULT_TIMEOUT_S),
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

    def __init__(self, drain: "SwapDrain", backend: str, started_at: float,
                 on_release: Callable[[], None] | None = None) -> None:
        self._drain = drain
        self.backend = backend
        self.started_at = started_at
        self.handed_off = False
        self._released = False
        self._on_release = on_release

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._drain._release(self)
        if self._on_release is not None:
            self._on_release()

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
        self._waiters: dict[str, set[asyncio.Event]] = {}

    # -- forwarding side ----------------------------------------------------

    def lease(self, backend: str, *, on_release: Callable[[], None] | None = None) -> ServingLease:
        """Count one request in flight on whatever ``backend`` has loaded.

        Call under the swap lock, in the same hold in which the caller made
        its target ready or read the loaded model.
        """
        lease = ServingLease(self, backend, self._clock(), on_release)
        self._leases.setdefault(backend, set()).add(lease)
        return lease

    def in_flight(self, backend: str) -> int:
        return len(self._leases.get(backend, ()))

    def snapshot(self) -> dict[str, dict[str, float]]:
        """Read-only diagnostics: in-flight count and oldest lease age per busy backend.

        A lease whose age is far beyond any real request is leaked; see the
        recovery note at ``llm.swap_drain`` in proxy_config.yml.
        """
        now = self._clock()
        return {
            backend: {
                "in_flight": len(leases),
                "oldest_lease_age_s": round(now - min(lease.started_at for lease in leases), 1),
            }
            for backend, leases in sorted(self._leases.items())
            if leases
        }

    # -- swapping side ------------------------------------------------------

    async def clear_for_swap(self, backend: str, settings: SwapDrainSettings) -> None:
        """Wait until no lease remains on ``backend``. Call holding the lock.

        Raises ``SwapDeferred`` after ``settings.timeout_s``.
        """
        start = self._clock()
        leases = self._leases.setdefault(backend, set())
        if leases:
            print(
                f"[swap-drain] {backend}: swap waits for {len(leases)} request(s) in flight "
                f"(timeout {settings.timeout_s:.0f}s)",
                flush=True,
            )
        while leases:
            now = self._clock()
            remaining = start + settings.timeout_s - now
            if remaining <= 0:
                oldest_s = now - min(lease.started_at for lease in leases)
                print(
                    f"[swap-drain] {backend}: swap deferred, {len(leases)} still in flight "
                    f"(oldest {oldest_s:.0f}s)",
                    flush=True,
                )
                raise SwapDeferred(backend, in_flight=len(leases), waited_s=now - start)
            changed = asyncio.Event()
            waiters = self._waiters.setdefault(backend, set())
            waiters.add(changed)
            try:
                await asyncio.wait_for(changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
            finally:
                waiters.discard(changed)
        if self._clock() - start > 0.001:
            print(f"[swap-drain] {backend}: drained after {self._clock() - start:.1f}s", flush=True)

    # -- internals ----------------------------------------------------------

    def _release(self, lease: ServingLease) -> None:
        self._leases.get(lease.backend, set()).discard(lease)
        for changed in list(self._waiters.get(lease.backend, ())):
            changed.set()

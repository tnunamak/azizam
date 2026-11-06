"""Engine-agnostic, gateway-owned cancellation correlation.

LiteLLM 1.86.2 runs as a separate sidecar process, so a client abort at the
outer gateway does not by itself stop the sidecar's outbound call to a
backend, nor the internal gateway hop behind it (see
``docs/GATEWAY_STREAM_LIVENESS_AUDIT_2026-07-13.md``).
LiteLLM's own non-stream disconnect canceller is dead code in this version,
so this module does not rely on it.

This registry is the single cross-hop cancellation signal: the outer gateway
registers a ``request_id`` before dispatch and signals it on any of its own
disconnect paths (pre-header, streaming generator teardown, non-stream
delayed disconnect). The internal gateway route — reached through LiteLLM,
which forwards ``X-Azizam-Request-Id`` via
``general_settings.forward_client_headers_to_llm_api`` — races its own
backend read against the same id. Direct/native routes that never go through
LiteLLM use the same registry with an id minted at the outer hop.

Ownership-safe by construction:

* ``register`` returns a ``CancellationHandle`` — a distinct object per call,
  even for the same ``request_id`` (outer + internal registration for the
  same id is expected and must not let one caller's ``release()`` delete or
  detach the other's waiter). A handle carries the entry's *generation*: an
  integer bumped every time an id's entry is replaced (fresh register after
  full release, or TTL sweep). ``release`` only removes the entry if the
  handle's generation still matches the live entry's generation, and only
  after decrementing the entry's live-handle refcount to zero. A stale
  handle's ``release`` is a safe no-op.
* ``register`` and ``signal`` commute — a cancel that arrives before any
  ``register`` (fast client abort, slow LiteLLM dispatch) is remembered as a
  bounded-TTL tombstone and observed immediately by the token the late
  ``register`` returns. ``signal`` wakes every handle currently registered
  for that id (there can be more than one: outer + internal both hold a
  live handle on the same id). A signalled entry also survives its *last*
  handle's ``release`` as the same kind of tombstone — the outer route
  commonly signals and releases in the same breath (dispatch races
  disconnect, then closes its span), and a slower internal-hop ``register``
  for that id must still observe the cancellation.
* Entries are TTL-bounded and swept on every ``register``/``signal`` call so
  the table cannot grow unbounded from ids that are never explicitly
  released (crashed callers, forgotten ``release``).
* The table enforces a hard maximum entry count. Once at capacity, a
  ``register`` for an id with no existing entry raises
  ``CancellationRegistryFull`` rather than evicting a live entry or aliasing
  a reused id onto stale state — silently reusing capacity from a live
  request would let an unrelated request observe (or fail to observe) the
  wrong signal.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

DEFAULT_ENTRY_TTL_SECONDS = 900.0
DEFAULT_MAX_ENTRIES = 4096


class CancellationRegistryFull(RuntimeError):
    """Raised when ``register`` cannot allocate a new entry at capacity."""


@dataclass
class CancellationToken:
    """Handle a caller holds to observe one request id's cancellation signal."""

    request_id: str
    _event: asyncio.Event

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class _Entry:
    __slots__ = ("event", "expires_at", "generation", "live_handles", "signalled_without_registration")

    def __init__(self, *, expires_at: float, generation: int) -> None:
        self.event = asyncio.Event()
        self.expires_at = expires_at
        self.generation = generation
        self.live_handles = 0
        self.signalled_without_registration = False


@dataclass
class CancellationHandle:
    """Ownership-scoped handle returned by ``register``.

    Distinct per ``register`` call so duplicate outer/internal registration
    of the same ``request_id`` cannot let one owner's ``release`` delete or
    detach the other's waiter. ``release`` is idempotent and safe to call
    more than once or after the underlying entry has already been replaced.
    """

    request_id: str
    token: CancellationToken
    _registry: "CancellationRegistry"
    _generation: int
    _released: bool = False

    async def aclose(self) -> None:
        self.release()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._registry._release(self.request_id, self._generation)


class CancellationRegistry:
    """Bounded in-process map from request id to a cancellation signal."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_ENTRY_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[str, _Entry] = {}
        # A process-wide, never-reset counter — NOT "generation 0 for this
        # id's first entry, 1 for its second, ...". If generation reset per
        # id, a handle from entry N (fully deleted, e.g. an unsignalled
        # release) and a handle from entry N+2 of the *same* id could later
        # collide on the same small integer once the table cycles back
        # through 0/1/2. A monotonic, globally-unique value per entry means
        # a stale handle's generation can never match a later entry for the
        # same id, no matter how many times that id is deleted and recreated.
        self._next_generation = 0

    def register(self, request_id: str) -> CancellationHandle:
        """Return an owned handle for ``request_id``, creating or reusing its entry.

        If ``signal`` already ran for this id (cancel-before-register race),
        the returned token is already cancelled. Raises
        ``CancellationRegistryFull`` if the table is at capacity and
        ``request_id`` has no existing entry.
        """
        self._sweep_expired()
        entry = self._entries.get(request_id)
        if entry is None:
            if len(self._entries) >= self._max_entries:
                raise CancellationRegistryFull(
                    f"cancellation registry is at capacity ({self._max_entries} entries)"
                )
            entry = _Entry(expires_at=self._deadline(), generation=self._new_generation())
            self._entries[request_id] = entry
        else:
            entry.expires_at = self._deadline()
        entry.live_handles += 1
        token = CancellationToken(request_id=request_id, _event=entry.event)
        return CancellationHandle(
            request_id=request_id,
            token=token,
            _registry=self,
            _generation=entry.generation,
        )

    def _new_generation(self) -> int:
        value = self._next_generation
        self._next_generation += 1
        return value

    def signal(self, request_id: str) -> None:
        """Mark ``request_id`` cancelled. Idempotent; safe before registration.

        An id with no existing entry only gets a new cancel-before-register
        tombstone if the table has room. At capacity, the signal for a
        never-registered id is dropped rather than growing the table
        unboundedly — a caller that always registers its own id before it
        might signal it (every production span in this codebase does) is
        unaffected, since its id already has a live entry by the time any
        signal for that id can fire. Only signals for ids nobody ever
        registers (forged/garbage correlation headers) can hit this bound,
        and there is no live waiter to reach for those regardless.
        """
        if not request_id:
            return
        self._sweep_expired()
        entry = self._entries.get(request_id)
        if entry is None:
            if len(self._entries) >= self._max_entries:
                return
            entry = _Entry(expires_at=self._deadline(), generation=self._new_generation())
            entry.signalled_without_registration = True
            self._entries[request_id] = entry
        entry.expires_at = self._deadline()
        entry.event.set()

    def is_cancelled(self, request_id: str) -> bool:
        entry = self._entries.get(request_id)
        return entry is not None and entry.event.is_set()

    def _release(self, request_id: str, generation: int) -> None:
        entry = self._entries.get(request_id)
        if entry is None or entry.generation != generation:
            # Entry already replaced (TTL sweep, or fully released and
            # re-registered) since this handle was issued. Nothing to do.
            return
        if entry.live_handles > 0:
            entry.live_handles -= 1
        if entry.live_handles > 0:
            return
        if entry.event.is_set():
            # A signalled entry survives its last handle's release as a
            # bounded-TTL tombstone — identical to the cancel-before-register
            # case. Without this, a fast local disconnect that signals then
            # immediately releases (e.g. the outer route's own pre-header or
            # nonstream teardown) would erase the very signal a slower
            # internal-hop registration is racing to observe.
            return
        self._entries.pop(request_id, None)

    def _deadline(self) -> float:
        return time.monotonic() + self._ttl_seconds

    def _sweep_expired(self) -> None:
        """Drop entries past their TTL deadline — but never a live one.

        ``expires_at`` is a deadline for an *unclaimed* signal (cancel-before-
        register, or a signalled tombstone surviving its last release), not a
        wall-clock cap on a request's own duration. A live entry
        (``live_handles > 0``) is still owned by an in-flight caller no
        matter how long ago its deadline passed — an upstream read can
        legitimately run past the configured TTL (see
        ``UPSTREAM_READ_TIMEOUT``), and sweeping it here would silently
        detach the very waiter a slow cross-hop ``signal()`` is racing to
        reach, indistinguishable from the cancellation simply never having
        happened. Only a zero-handle entry — nothing left to signal — is
        actually expired.
        """
        if not self._entries:
            return
        now = time.monotonic()
        expired = [
            key for key, entry in self._entries.items()
            if entry.expires_at <= now and entry.live_handles == 0
        ]
        for key in expired:
            self._entries.pop(key, None)


_REGISTRY = CancellationRegistry()


def cancellation_registry() -> CancellationRegistry:
    """Return the process-wide registry singleton.

    A singleton (rather than dependency-injected instance) matches the
    existing module-level state pattern in this codebase (e.g. receipt
    stores) and keeps every call site — outer routes, internal routes, and
    tests that patch ``gateway.backends.cancellation._REGISTRY`` — talking to
    the same table without threading it through every function signature.
    """
    return _REGISTRY

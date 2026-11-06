"""Process-local serialization of one exclusive route pool, with an explicit VRAM claim.

One ``ExclusivePoolLeaseManager`` lives in each gateway process. For each pool it holds an
``asyncio.Lock`` from before the observer is read until the downstream call and its cleanup end.
Only requests of this process are serialized, so the declaration is valid only when exactly one
gateway process serves the physical pool. Nothing here caches, queues, samples or learns a peak.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator, Awaitable, Callable, Mapping

from gateway.arbitration.holdings import SettlementKind
from gateway.arbitration.legacy import shadow_compare, shadow_pool_advice
from gateway.arbitration.store import STALE_GENERATION, ArbitrationStore, default_path
from gateway.arbitration.types import AuthorityMode, ResourceRef
from gateway.resource_claims import (
    AdmissionDecision,
    AdmissionRequest,
    AdmissionStatus,
    CapacitySnapshot,
    QueueClass,
    ResourceClaim,
    evaluate_admission,
)

logger = logging.getLogger(__name__)

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


SHADOW_VALIDITY_S = 5
# The shadow's admissions that may wait for the worker. An exit is never dropped: it is queued behind its admission.
SHADOW_QUEUE_LIMIT = 256
# After a failed store open the worker waits this long before it opens again.
SHADOW_OPEN_RETRY_S = 60.0
# The shadow's exclusion token: a capacity-1 stock whose unit is a token, not MiB (design §16 criterion 6).
TOKEN_QUANTITY = 1
TOKEN_UNIT = "token"


def pool_inventory(spec: ExclusivePoolSpec) -> dict[str, ResourceRef]:
    """The shadow's resource ids for one pool: its VRAM reading and its exclusion token. Legacy keys map to them."""
    return {
        "vram_mb": ResourceRef(spec.pool_id, "1"),
        spec.pool_id: ResourceRef(f"{spec.pool_id}/lock", "1"),
    }


class ShadowQueueFull(Exception):
    """The shadow's admission queue is full. The admission and its exit are both dropped."""


class _ShadowWorker:
    """One daemon thread that runs shadow jobs in the order they were queued. Enqueueing never blocks."""

    def __init__(self, *, queue_limit: int, on_error: Callable[[BaseException], None]) -> None:
        self._limit = queue_limit
        self._on_error = on_error
        self._cond = threading.Condition()
        self._jobs: deque[tuple[bool, Callable[..., None], tuple[Any, ...]]] = deque()
        # Lifecycles in flight. An admission that will get an exit keeps its slot until that exit has run,
        # so the limit bounds every queued job, exits included, across all pools.
        self._outstanding = 0
        self._busy = False
        self._thread: threading.Thread | None = None

    def submit_admission(self, function: Callable[..., None], *args: Any, holds_exit: bool) -> bool:
        """Queue an admission. With holds_exit, its slot stays taken until submit_exit's job has run."""
        with self._cond:
            if self._outstanding >= self._limit:
                return False
            self._start()  # may raise; then nothing was queued or counted
            self._outstanding += 1
            self._push(not holds_exit, function, args)
            return True

    def submit_exit(self, function: Callable[..., None], *args: Any) -> None:
        """Queue the exit of an admission queued with holds_exit. Its slot is already reserved."""
        with self._cond:
            self._start()
            self._push(True, function, args)

    def drain(self, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: not self._jobs and not self._busy, timeout)

    def _start(self) -> None:
        if self._thread is None:
            thread = threading.Thread(target=self._run, name="arbitration-shadow", daemon=True)
            thread.start()  # a failed start leaves _thread None, so the next submit tries again
            self._thread = thread

    def _push(self, frees_slot: bool, function: Callable[..., None], args: tuple[Any, ...]) -> None:
        self._jobs.append((frees_slot, function, args))
        self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._jobs)
                frees_slot, function, args = self._jobs.popleft()
                self._busy = True
            try:
                function(*args)
            except Exception as error:  # noqa: BLE001 - a store error must never stop the worker
                self._on_error(error)
            finally:
                with self._cond:
                    if frees_slot:
                        self._outstanding -= 1
                    self._busy = False
                    self._cond.notify_all()


class _PoolShadow:
    """Durable shadow of each lease in the arbitration store (design §16 slice 1). Never authoritative.

    The lease calls admit() and release() synchronously. Both only enqueue onto one worker thread, which
    owns the store. So a lease never waits for the store, and nothing here is a cancellation point.

    Per admitted lease the worker acquires the pool's exclusion token and records the comparison. The lease
    exit is queued behind it: a normal return settles the token with stock-release evidence, and a body that
    raised or was cancelled records that the execution ended, which keeps the token charged. A dropped
    admission is remembered by key, so its exit is dropped too. Errors are logged once per kind. The store is
    opened on the worker when first needed; if that fails, jobs are dropped and the open is retried at most
    once per SHADOW_OPEN_RETRY_S.
    """

    def __init__(self, store: ArbitrationStore | None, *, queue_limit: int = SHADOW_QUEUE_LIMIT) -> None:
        self._store = store
        self._queue_limit = queue_limit
        self._open_failed_at: float | None = None
        self._generations: dict[str, int] = {}  # touched only by the worker
        self._dropped: set[str] = set()  # attempt keys whose admission was dropped; touched only by the lease loop
        self._logged: set[str] = set()
        self._log_lock = threading.Lock()
        self._worker = _ShadowWorker(queue_limit=queue_limit, on_error=self._log_once)

    def admit(self, spec: ExclusivePoolSpec, *, request_id: str, decision: AdmissionDecision,
              snapshot: CapacitySnapshot, claim: ResourceClaim, observed_at: float) -> str | None:
        """Queue the comparison, and the token acquire when the legacy path admitted.

        Returns the attempt key when the lease must be given an exit: an admitted lease whose admission is queued
        or was dropped. Returns None otherwise.
        """
        attempt = f"{spec.pool_id}/{request_id}"
        try:
            advice = shadow_pool_advice(
                (replace(snapshot, observed_at=observed_at), claim), pool_inventory(spec),
                now=int(time.time()), validity_s=SHADOW_VALIDITY_S, authority=AuthorityMode.EXCLUSIVE,
            )
            report = shadow_compare(decision, advice)
        except Exception as error:  # noqa: BLE001 - the shadow must never break a lease
            self._log_once(error)
            return None
        accepted = decision.status is AdmissionStatus.ACCEPTED
        try:
            queued = self._worker.submit_admission(self._admission_job, spec, attempt, accepted, report,
                                                   holds_exit=accepted)
        except Exception as error:  # noqa: BLE001 - e.g. the worker thread could not start
            self._log_once(error)
            return None
        if not queued:
            if accepted:
                self._dropped.add(attempt)  # its exit must be dropped too; the lease will ask for one
            self._log_once(ShadowQueueFull(f"shadow admission queue is full ({self._queue_limit} waiting)"))
        return attempt if accepted else None

    def release(self, spec: ExclusivePoolSpec, op_key: str | None, *, ended: bool) -> None:
        """Queue the lease's exit: stock-release evidence when the body returned, EXECUTION_ENDED when it did not."""
        if op_key is None:
            return
        if op_key in self._dropped:
            self._dropped.discard(op_key)
            return
        kind, source = (SettlementKind.EXECUTION_ENDED, "lease_ended") if ended else (
            SettlementKind.STOCK_RELEASED, "lease_completed")
        try:
            self._worker.submit_exit(self._exit_job, spec, op_key, kind, source)
        except Exception as error:  # noqa: BLE001 - see admit()
            self._log_once(error)

    def drain(self, timeout: float) -> None:
        """Wait until every queued job has run. Tests only."""
        if not self._worker.drain(timeout):
            raise TimeoutError("the shadow worker did not drain")

    def _ensure_store(self) -> ArbitrationStore | None:
        """Runs on the worker only."""
        if self._store is not None:
            return self._store
        if self._open_failed_at is not None and time.monotonic() - self._open_failed_at < SHADOW_OPEN_RETRY_S:
            return None
        try:
            self._store = ArbitrationStore(default_path())
        except Exception as error:  # noqa: BLE001 - a store that cannot open must not break a lease
            self._open_failed_at = time.monotonic()
            self._log_once(error)
            return None
        self._open_failed_at = None
        return self._store

    def _admission_job(self, spec: ExclusivePoolSpec, attempt: str, accepted: bool, report: Any) -> None:
        """Runs on the worker only. Reads the open holdings before the acquire, then records the comparison."""
        store = self._ensure_store()
        if store is None:
            return
        token = pool_inventory(spec)[spec.pool_id]
        if token.id not in self._generations:
            self._generations[token.id] = store.take_over(token.id)
        open_holdings = store.open_holdings(token)
        if accepted:
            status = store.acquire(op_key=attempt, ref=token, quantity=TOKEN_QUANTITY, unit=TOKEN_UNIT,
                                   generation=self._generations[token.id]).status
            if status == STALE_GENERATION:
                # Another process took this token over. Acquires with this generation stay refused until restart.
                self._log_once(RuntimeError(f"pool {spec.pool_id} was taken over by another process; "
                                            "shadow acquires are refused until restart"))
        store.record_comparison(attempt_key=attempt, status=report.status, old_admits=report.old_admits,
                                new_outcome=report.new_outcome, detail=report.detail, open_holdings=open_holdings)

    def _exit_job(self, spec: ExclusivePoolSpec, op_key: str, kind: SettlementKind, source: str) -> None:
        """Runs on the worker only."""
        store = self._ensure_store()
        if store is None:
            return
        store.settle(op_key=op_key, ref=pool_inventory(spec)[spec.pool_id], kind=kind, source=source)

    def _log_once(self, error: BaseException) -> None:
        kind = type(error).__name__
        with self._log_lock:
            if kind in self._logged:
                return
            self._logged.add(kind)
        logger.warning("arbitration shadow ledger error (%s); the lease continues without it: %s", kind, error)


class ExclusivePoolLeaseManager:
    """One per gateway process. Per-pool locks; different pools never wait on each other.

    `shadow_store` is the arbitration store for the shadow. None opens the default store on the shadow's worker.
    """

    def __init__(self, shadow_store: ArbitrationStore | None = None, *,
                 shadow_queue_limit: int = SHADOW_QUEUE_LIMIT) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._claims: dict[tuple[str, str], ResourceClaim] = {}
        self._shadow = _PoolShadow(shadow_store, queue_limit=shadow_queue_limit)

    def active_claims(self) -> int:
        return len(self._claims)

    def drain_shadow(self, timeout: float = 10.0) -> None:
        """Wait until the shadow worker has run every queued job. For tests."""
        self._shadow.drain(timeout)

    @asynccontextmanager
    async def lease(
        self,
        spec: ExclusivePoolSpec,
        *,
        request_id: str,
        observe: Callable[[], Awaitable[Any]],
        observed_at: Callable[[], float] | None = None,
        wait: bool = True,
    ) -> AsyncGenerator[ResourceClaim]:
        """Hold the pool's one lease. With ``wait`` false, a pool whose lease is held (a run that is still going,
        even one whose caller left) is refused at once with ``PoolCapacityError(DELAYED)`` instead of queued."""
        lock = self._locks.setdefault(spec.pool_id, asyncio.Lock())
        if not wait and lock.locked():
            raise PoolCapacityError(AdmissionStatus.DELAYED)
        async with lock:
            started = time.time()
            document = await observe()
            observation = observation_from_document(document, spec)
            snapshot, claim = snapshot_and_claim(
                spec, observation, observed_at=observed_at() if observed_at else None,
            )
            decision = evaluate_admission(AdmissionRequest(request_id, claim), snapshot)
            # The shadow never changes the legacy decision. It only enqueues work, so it adds no await.
            op_key = self._shadow.admit(spec, request_id=request_id, decision=decision, snapshot=snapshot,
                                        claim=claim, observed_at=started)
            if decision.status is not AdmissionStatus.ACCEPTED:
                raise PoolCapacityError(decision.status)
            key = (spec.pool_id, request_id)
            self._claims[key] = claim
            ended = False
            try:
                yield claim
            except BaseException:
                ended = True  # the engine may still run; record that the execution ended, then re-raise unchanged
                raise
            finally:
                if self._claims.get(key) is claim:
                    del self._claims[key]
                self._shadow.release(spec, op_key, ended=ended)

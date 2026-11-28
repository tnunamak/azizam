"""Durable admission over physical exclusion tokens and execution slots.

This controller has no engine knowledge. Protocol callers supply readiness and
completion evidence. It is not installed on live dispatch until coverage permits
one authority to replace the legacy pool manager for a resource.
"""
from __future__ import annotations

import asyncio
import time
import weakref
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

from gateway.lease_stream import LeaseStream
from gateway.arbitration.store import AdmissionTicket, ArbitrationStore


async def _database(call, *args, **kwargs):
    """Join an in-flight SQLite transaction even when its caller is cancelled.

    A late successful grant must be observed by admission cleanup, never left
    running on a detached thread after the caller thinks it withdrew.
    """
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    interrupted = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            interrupted = True
    if interrupted:
        raise asyncio.CancelledError()
    return result


class AdmissionRequired(Exception):
    """A managed resource needs a live, compatible ticket before engine egress."""


class AdmissionInProgress(Exception):
    """This ticket already has an executor or an unsettled holding."""


class AdmissionExpired(Exception):
    """The original admission deadline expired before send intent."""


class AdmissionWithdrawn(Exception):
    """Unsent work was withdrawn; it must never dispatch."""


@dataclass
class AdmissionLease:
    store: ArbitrationStore
    ticket: AdmissionTicket
    evidence: str | None = None
    recovering: bool = False

    def send_intent(self) -> bool:
        """Commit send intent in the same transaction that excludes withdrawal."""
        if self.recovering:
            return False
        sent = self.store.transition_ticket(self.ticket.request_id, "sending", now=time.time())
        if sent:
            self.evidence = None  # a new protocol operation needs its own completion evidence
        return sent

    async def commit_send(self) -> bool:
        return await _database(self.send_intent)

    def completed(self, evidence: str) -> None:
        """Record protocol completion/absence evidence, never transport cleanup."""
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("completion requires named evidence")
        self.evidence = evidence


@dataclass(frozen=True)
class AdmissionPlan:
    scheduler: AdmissionScheduler
    ticket: AdmissionTicket
    ready: Callable[[], Awaitable[bool]]
    stop: Callable[[], Awaitable[None]] | None = None
    recover: bool = False
    on_wait: Callable[[], None] | None = None
    on_ready: Callable[[float], None] | None = None

    def grant(self) -> AbstractAsyncContextManager[AdmissionLease]:
        ticket = self.ticket
        return self.scheduler.admit(request_id=ticket.request_id, caller=ticket.caller,
                                    policy_revision=ticket.policy_revision, service_id=ticket.service_id,
                                    resources=ticket.resources, claim_envelope=ticket.claim_envelope,
                                    deadline=ticket.deadline, ready=self.ready, stop=self.stop, recover=self.recover, no_wait=ticket.no_wait,
                                    on_wait=self.on_wait, on_ready=self.on_ready)


class AdmissionScheduler:
    """Controller over an already-open ledger.

    The process startup owner must call store.recover_startup(now=...) once,
    after the old process has ended and before creating any live executors.
    Construction never recovers: another controller must not refund live work.
    """
    def __init__(self, store: ArbitrationStore, *, worker_capacity: int, poll_s: float = 0.25):
        if worker_capacity < 1 or poll_s <= 0:
            raise ValueError("positive worker capacity and polling cadence required")
        self.store = store
        self.worker_capacity = worker_capacity
        self.poll_s = poll_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: dict[str, asyncio.Event] = {}
        self._executors: set[str] = set()
        self._pending_cleanup: dict[str, str | None] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()

    def _wake_waiters(self) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is not None and running is not self._loop:
            if not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._wake_waiters)
            return
        for event in self._waiters.values():
            event.set()

    def pin(self, *, ready: Callable[[], Awaitable[bool]],
                stop: Callable[[], Awaitable[None]] | None = None, recover: bool = False,
                on_wait: Callable[[], None] | None = None, on_ready: Callable[[float], None] | None = None,
                **facts: Any) -> AdmissionPlan:
        """Pin order before host preparation, without taking any execution holding."""
        ticket = self.store.enqueue_ticket(**facts)
        if (ticket.request_id in self._executors or ticket.phase == "granted"
                or (ticket.phase in {"sending", "unknown"} and not recover)):
            raise AdmissionInProgress(ticket.request_id)
        return AdmissionPlan(self, ticket, ready, stop, recover, on_wait, on_ready)

    def withdraw(self, request_id: str) -> bool:
        withdrawn = self.store.transition_ticket(request_id, "withdrawn", now=time.time())
        self._wake_waiters()
        return withdrawn

    async def drain_cleanup(self) -> None:
        """Join response-finalizer cleanup before closing the scheduler's ledger."""
        while self._cleanup_tasks:
            tasks = tuple(self._cleanup_tasks)
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._cleanup_tasks.difference_update(task for task in tasks if task.done())

    async def retry_cleanup(self) -> None:
        """Retry known executor exits; a write failure cannot silently consume a worker forever."""
        for request_id, evidence in tuple(self._pending_cleanup.items()):
            await _database(self.store.finish_ticket, request_id, now=time.time(), evidence=evidence)
            self._pending_cleanup.pop(request_id, None)
            self._wake_waiters()

    @asynccontextmanager
    async def admit(self, *, request_id: str, caller: str, policy_revision: str, service_id: str,
                    resources: tuple[str, ...], claim_envelope: dict[str, Any], deadline: float,
                    ready: Callable[[], Awaitable[bool]],
                    stop: Callable[[], Awaitable[None]] | None = None, recover: bool = False,
                    no_wait: bool = False, on_wait: Callable[[], None] | None = None,
                    on_ready: Callable[[float], None] | None = None) -> AsyncIterator[AdmissionLease]:
        await self.retry_cleanup()
        if request_id in self._executors:
            raise AdmissionInProgress(request_id)
        self._loop = asyncio.get_running_loop()
        self._executors.add(request_id)
        changed = asyncio.Event()
        self._waiters[request_id] = changed
        enrolled = []
        granted = False
        lease = None
        wait_started = None

        def enqueue():
            ticket = self.store.enqueue_ticket(request_id=request_id, caller=caller, policy_revision=policy_revision,
                                               service_id=service_id, resources=resources,
                                               claim_envelope=claim_envelope, deadline=deadline, no_wait=no_wait)
            if ticket.phase == "waiting":
                enrolled.append(ticket)
            return ticket

        try:
            ticket = await _database(enqueue)
            adopting = recover and ticket.phase in {"sending", "unknown"}
            if ticket.phase == "granted" or (ticket.phase in {"sending", "unknown"} and not adopting):
                raise AdmissionInProgress(request_id)
            lease = AdmissionLease(self.store, ticket, recovering=adopting)
            if adopting:
                granted = True
                yield lease
                return
            while True:
                changed.clear()
                current = await _database(self.store.ticket, request_id)
                if current.phase != "waiting":
                    if current.phase == "expired":
                        raise AdmissionExpired()
                    raise AdmissionWithdrawn(current.phase)
                remaining = ticket.deadline - time.time()
                if remaining <= 0 and not ticket.no_wait:
                    await _database(self.store.transition_ticket, request_id, "expired", now=time.time())
                    raise AdmissionExpired()
                available = await self._interruptible(ready(), stop, remaining if remaining > 0 else 30.0)
                if available and await _database(self.store.grant_ticket, request_id,
                                                 worker_capacity=self.worker_capacity, now=time.time()):
                    granted = True
                    if wait_started is not None and on_ready is not None:
                        await asyncio.to_thread(on_ready, time.monotonic() - wait_started)
                    yield lease
                    return
                if ticket.no_wait:
                    await _database(self.store.transition_ticket, request_id, "expired", now=time.time(),
                                    reason="immediate capacity unavailable")
                    raise AdmissionExpired()
                if wait_started is None:
                    wait_started = time.monotonic()
                    if on_wait is not None:
                        await asyncio.to_thread(on_wait)
                await self._interruptible(changed.wait(), stop, min(remaining, self.poll_s), pause=True)
        finally:
            try:
                if granted:
                    try:
                        await _database(self.store.finish_ticket, request_id, now=time.time(), evidence=lease.evidence)
                    except BaseException:
                        self._pending_cleanup[request_id] = lease.evidence
                        raise
                elif enrolled:
                    await _database(self.store.transition_ticket, request_id, "withdrawn", now=time.time())
            finally:
                self._executors.discard(request_id)
                self._waiters.pop(request_id, None)
                self._wake_waiters()

    async def reconcile_unknown(self, inspect: Callable[[AdmissionTicket], Awaitable[str | None]],
                                *, timeout_s: float = 30.0) -> list[str]:
        """Inspect persisted unknown work through its protocol; settle only named evidence.

        A failed/absent observation retains the charge. Call on startup/recovery
        and periodically while authority is enabled; this does not replay work.
        """
        await self.retry_cleanup()
        settled = []
        for ticket in await asyncio.to_thread(self.store.unknown_tickets):
            if ticket.request_id in self._executors:
                continue
            try:
                async with asyncio.timeout(timeout_s):
                    evidence = await inspect(ticket)
            except Exception:
                continue
            if not isinstance(evidence, str) or not evidence.strip():
                continue
            try:
                await asyncio.to_thread(self.store.finish_ticket, ticket.request_id, now=time.time(), evidence=evidence)
                settled.append(ticket.request_id)
            finally:
                self._wake_waiters()
        return settled

    @staticmethod
    async def _interruptible(operation: Awaitable[Any], stop: Callable[[], Awaitable[None]] | None,
                             timeout: float, *, pause: bool = False) -> Any:
        task = asyncio.ensure_future(operation)
        halt = asyncio.ensure_future(stop()) if stop is not None else None
        tasks = (task, halt) if halt is not None else (task,)
        try:
            await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            # Cancellation wins a simultaneous readiness wake; send intent must also
            # recheck persisted withdrawal at the dispatch boundary.
            if halt is not None and halt.done():
                raise AdmissionWithdrawn() from (None if halt.cancelled() else halt.exception())
            if task.done():
                return task.result()
            if not pause:
                raise AdmissionExpired()
            return False
        finally:
            for pending in tasks:
                pending.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


_CURRENT_SCOPE: ContextVar[AdmissionScope | None] = ContextVar("gpu_admission_scope", default=None)


async def pin_plan(factory: Callable[..., AdmissionPlan | None], *args: Any) -> AdmissionPlan | None:
    """Join a pin transaction on cancellation and withdraw its unsent result."""
    pinned = []
    def pin():
        plan = factory(*args)
        pinned.append(plan)
        return plan
    try:
        return await _database(pin)
    except asyncio.CancelledError:
        if pinned and pinned[0] is not None:
            plan = pinned[0]
            await _database(plan.scheduler.withdraw, plan.ticket.request_id)
        raise


class AdmissionScope:
    """Lazy protocol lifetime: host preparation precedes the first resource grant.

    All rounds of one attempt share the same holding. Only protocol terminal
    evidence can settle a sent operation; a submit acknowledgement cannot.
    """
    def __init__(self, plan: AdmissionPlan):
        self._plan = plan
        self._stack = AsyncExitStack()
        self.lease: AdmissionLease | None = None
        self._token = None

    async def __aenter__(self):
        self._token = _CURRENT_SCOPE.set(self)
        return self

    async def __aexit__(self, *args):
        try:
            await self._stack.aclose()
            if self.lease is None:
                await _database(self._plan.scheduler.withdraw, self._plan.ticket.request_id)
        finally:
            _CURRENT_SCOPE.reset(self._token)

    async def prepare_send(self) -> None:
        if self.lease is None:
            self.lease = await self._stack.enter_async_context(self._plan.grant())
        if not await self.lease.commit_send():
            phase = (await _database(self.lease.store.ticket, self.lease.ticket.request_id)).phase
            if phase == "expired":
                raise AdmissionExpired()
            raise AdmissionWithdrawn()

    async def prepare_resume(self) -> None:
        if self.lease is None:
            self.lease = await self._stack.enter_async_context(self._plan.grant())
        if not self.lease.recovering:
            raise AdmissionRequired("resume requires adoption of an existing operation")

    def completed(self, evidence: str) -> None:
        if self.lease is not None:
            self.lease.completed(evidence)


def current_scope() -> AdmissionScope | None:
    return _CURRENT_SCOPE.get()


_BOUND_LEASE: ContextVar[AdmissionLease | None] = ContextVar("gpu_bound_lease", default=None)


@contextmanager
def bind_lease(lease: AdmissionLease | None):
    token = _BOUND_LEASE.set(lease)
    try:
        yield
    finally:
        _BOUND_LEASE.reset(token)


def require_holding(resources: tuple[str, ...]) -> None:
    scope = current_scope()
    lease = _BOUND_LEASE.get() or (scope.lease if scope is not None else None)
    if lease is None or not set(resources) <= set(lease.ticket.resources):
        raise AdmissionRequired("physical resource bundle is not held")
    current = lease.store.ticket(lease.ticket.request_id)
    if current is None or (current.phase != "sending" and not (lease.recovering and current.phase == "unknown")):
        raise AdmissionRequired("ticket has no committed send intent")


class AdmissionOwner:
    """Explicit ownership for a response that outlives its forwarding handler.

    The admission context carries no task-bound context token. Each send/read
    binds the lease locally, so handing a stream to another task is safe.
    """
    def __init__(self, plan: AdmissionPlan, context, lease: AdmissionLease):
        self.plan, self._context, self.lease = plan, context, lease
        self.handed_off = False
        self._closed = False
        self._close_lock = asyncio.Lock()

    @classmethod
    async def open(cls, plan: AdmissionPlan) -> AdmissionOwner:
        context = plan.grant()
        lease = await context.__aenter__()
        return cls(plan, context, lease)

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self._context.__aexit__(None, None, None)

    def abandon(self, source=None) -> None:
        if self._closed:
            return
        scheduler = self.plan.scheduler
        loop = scheduler._loop
        if loop is None or loop.is_closed():
            return  # startup recovery will return the dead worker; no evidence refunds physical stock

        async def cleanup():
            try:
                if source is not None:
                    with bind_lease(self.lease):
                        await source.aclose()
            finally:
                await self.aclose()

        def schedule():
            task = loop.create_task(cleanup())
            scheduler._cleanup_tasks.add(task)
            def done(finished):
                scheduler._cleanup_tasks.discard(finished)
                if not finished.cancelled():
                    finished.exception()  # cleanup faults remain charged and enter retry_cleanup
            task.add_done_callback(done)
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            schedule()
        else:
            loop.call_soon_threadsafe(schedule)

    def hand_off(self, source: AsyncIterator[bytes], *, eof_evidence: str | None = None) -> AsyncIterator[bytes]:
        if self.handed_off:
            raise ValueError("admission already belongs to a stream")
        if eof_evidence is not None and (not isinstance(eof_evidence, str) or not eof_evidence.strip()):
            raise ValueError("EOF evidence must name the declared protocol postcondition")
        self.handed_off = True
        bound = _AdmissionSource(source, self.lease)
        stream = LeaseStream(bound, self.aclose,
                             on_exhausted=(lambda: self.lease.completed(eof_evidence)) if eof_evidence is not None else None)
        weakref.finalize(stream, self.abandon, source)
        return stream


class _AdmissionSource:
    def __init__(self, source, lease: AdmissionLease):
        self._source, self._lease = source, lease

    async def __anext__(self):
        with bind_lease(self._lease):
            return await self._source.__anext__()

    async def aclose(self):
        with bind_lease(self._lease):
            await self._source.aclose()

"""Which job holds a run slot, so a job that waits for a busy GPU pool can give its slot back while it waits.

The run core runs at most a set number of jobs at once (one semaphore, ``images.media_jobs.concurrency``). A job that waits
for another job's GPU pool does no work, but a held slot would still count it: with two slots, a job queued behind a busy
pool would keep a job for a free pool, or for the other GPU, from starting. ``RunSlots.released`` gives the job's slot back
for the wait and takes a slot again before the job goes on, so the cap still counts only jobs that work.

The rule that keeps slots and GPU pools from deadlocking: a job never waits for a pool's lock while it holds a run slot.
``ExclusivePoolLeaseManager.lease`` waits for a lock only inside the ``LeaseWait`` bracket, which gives the slot back
first, and it chooses that path from a count of the pool's holders and waiters, not from ``lock.locked()`` (which reads
free in the turn between a release and the woken waiter). So every slot holder is a job that runs or is on its way out;
none waits on a pool. The other order does happen: a job that has won the pool's lock takes its slot again while it holds
the lock. That wait has a bound, because the jobs it waits for hold slots, wait for no pool, and end within their own run
bounds; the wait graph has no cycle. A cancel ends this wait too (``stop``): the job gives the pool back to the next
waiter, in order. The wait budget does not: the budget is for the pool, and the pool is won by then.

While that job waits for a slot, its pool is held and idle. That is the cap at work, not a lost turn: the pool can run
only a job that holds a slot, and the slots are in use. The jobs ahead of it in the slot queue either run on another
pool (real work the cap allows) or meet a busy pool and give their slot straight back. Taking the slot before the lock
would not shorten this (the pool would wait, reserved, for the same slot) and would put a slot holder in a lock wait.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable

from gateway.exclusive_pool import LeaseWaitStopped


class RunSlots:
    """The slots held now, by key (the job id). One process, one event loop."""

    def __init__(self) -> None:
        self._held: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def hold(self, key: str, slots: asyncio.Semaphore) -> AsyncIterator[None]:
        """Take one of ``slots`` for ``key`` for the block. It is given back at the end unless a wait gave it back."""
        await slots.acquire()
        self._held[key] = slots
        try:
            yield
        finally:
            if self._held.get(key) is slots:
                del self._held[key]
                slots.release()

    @asynccontextmanager
    async def released(self, key: str, *, stop: Callable[[], Awaitable[None]] | None = None) -> AsyncIterator[None]:
        """Give back the slot ``key`` holds for the block, and take one again after it. A block that raises (the wait was
        stopped or ran out) does not take one again: the job ends without work. A key that holds no slot waits as is.

        ``stop`` returns when the wait for the slot after the block must end (a cancel): the block's caller, which holds
        the pool by then, gets ``LeaseWaitStopped`` and gives the pool back. A slot taken in the turn ``stop`` returns is kept."""
        slots = self._held.pop(key, None)
        if slots is None:
            yield
            return
        slots.release()
        yield
        await _take(slots, stop)  # a cancel or a stop here leaves no slot held, so ``hold`` gives none back
        self._held[key] = slots


async def _take(slots: asyncio.Semaphore, stop: Callable[[], Awaitable[None]] | None) -> None:
    if stop is None:
        await slots.acquire()
        return
    acquire, halt = asyncio.ensure_future(slots.acquire()), asyncio.ensure_future(stop())
    got = False
    try:
        await asyncio.wait((acquire, halt), return_when=asyncio.FIRST_COMPLETED)
        got = acquire.done() and not acquire.cancelled() and acquire.exception() is None
        if not got:
            raise LeaseWaitStopped() from (halt.exception() if halt.done() and not halt.cancelled() else None)
    finally:
        halt.cancel()
        if not got:
            acquire.cancel()
            if acquire.done() and not acquire.cancelled() and acquire.exception() is None:
                slots.release()  # it got the slot in the turn the wait ended

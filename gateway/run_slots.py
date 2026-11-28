"""Legacy workflow semaphore ownership while admission waits.

``released`` returns a job's slot throughout lock contention and memory recovery,
then reacquires a slot before the pool lease yields. The legacy pool can hold its
FIFO lock during that reacquisition; the pool's remaining admission budget now
bounds the whole preparation, including reacquisition.

The durable scheduler grants execution slots and physical resources together.
After dispatch coverage permits its activation, workflow dispatch bypasses this
legacy semaphore rather than running two worker authorities.
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

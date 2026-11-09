"""In-process wake-up for a job's waiters.

The writer that moves a job to a terminal state calls ``wake(job_id)``; a waiter parked in ``woken(job_id)`` resumes on
the next loop turn. A job another process finishes sends no wake, so a waiter also re-reads on a bounded fallback
interval (``FALLBACK_S``). This module holds no job facts: it only says "look again".
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Iterator

FALLBACK_S = 1.0

_lock = threading.Lock()
_waiters: dict[str, set["_Waiter"]] = {}


class _Waiter:
    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()

    def set(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:        # the waiter's loop is closed: nobody is left to wake
            pass

    async def wait(self, timeout_s: float) -> None:
        """Return when woken, or after ``timeout_s``. A wake that came before the call is not lost."""
        try:
            await asyncio.wait_for(self.event.wait(), timeout=max(0.0, timeout_s))
        except asyncio.TimeoutError:
            pass
        self.event.clear()


@contextlib.contextmanager
def watch(job_id: str) -> Iterator[_Waiter]:
    """Register before the first read of the job, so a terminal write between the read and the wait is not missed."""
    waiter = _Waiter()
    with _lock:
        _waiters.setdefault(job_id, set()).add(waiter)
    try:
        yield waiter
    finally:
        with _lock:
            group = _waiters.get(job_id)
            if group is not None:
                group.discard(waiter)
                if not group:
                    del _waiters[job_id]


def wake(job_id: str) -> None:
    with _lock:
        group = list(_waiters.get(job_id, ()))
    for waiter in group:
        waiter.set()

"""Virtual clock shared by a test and its fake upstreams.

Fake delays are waits on this clock. Only the test moves it, so a delay
never costs wall time and never races the code under test. The clock is
thread-safe because loopback fakes run their event loop in another thread.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass


class ClockClosed(Exception):
    """Raised in a waiter when the test tears the clock down."""


@dataclass(eq=False)
class _Waiter:
    deadline: float
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future


class VirtualClock:
    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()
        self._waiters: list[_Waiter] = []
        self._blocked_listeners: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
        self._closed = False

    def now(self) -> float:
        with self._lock:
            return self._now

    @property
    def waiting(self) -> int:
        """Number of fake steps blocked on this clock right now."""
        with self._lock:
            return len(self._waiters)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("the virtual clock cannot go backwards")
        with self._lock:
            self._now += seconds
            due = [w for w in self._waiters if w.deadline <= self._now]
            self._waiters = [w for w in self._waiters if w.deadline > self._now]
        for waiter in due:
            _call_soon(waiter.loop, _resolve, waiter.future, None)

    async def sleep(self, seconds: float) -> None:
        """Block until the test advances the clock by ``seconds`` (inf: never)."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            if self._closed:
                raise ClockClosed()
            if seconds <= 0:
                return
            deadline = self._now + seconds
            waiter = _Waiter(deadline, loop, future)
            self._waiters.append(waiter)
            listeners, self._blocked_listeners = self._blocked_listeners, []
        for listener_loop, listener in listeners:
            _call_soon(listener_loop, _resolve, listener, None)
        try:
            await future
        finally:
            with self._lock:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)

    async def until_waiting(self, count: int = 1) -> None:
        """Return once at least ``count`` fake steps are blocked on the clock."""
        loop = asyncio.get_running_loop()
        while True:
            with self._lock:
                if len(self._waiters) >= count:
                    return
                future = loop.create_future()
                self._blocked_listeners.append((loop, future))
            await future

    def close(self) -> None:
        """Wake every waiter with ``ClockClosed`` so fake servers can stop."""
        with self._lock:
            self._closed = True
            waiters, self._waiters = self._waiters, []
            listeners, self._blocked_listeners = self._blocked_listeners, []
        for waiter in waiters:
            _call_soon(waiter.loop, _fail, waiter.future, ClockClosed())
        for listener_loop, listener in listeners:
            _call_soon(listener_loop, _fail, listener, ClockClosed())


def _call_soon(loop: asyncio.AbstractEventLoop, callback, *args) -> None:
    # A test that already finished closed its loop; nothing waits there any more.
    try:
        loop.call_soon_threadsafe(callback, *args)
    except RuntimeError:
        if not loop.is_closed():
            raise


def _resolve(future: asyncio.Future, value: object) -> None:
    if not future.done():
        future.set_result(value)


def _fail(future: asyncio.Future, error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)

"""An asyncio clock for in-process fakes, with no socket or worker-thread I/O.

Ready callbacks run normally. Once all tasks are waiting, jump to the next
scheduled timer instead of sleeping on the host clock. asyncio's real timeout
and cancellation machinery therefore runs at exact virtual deadlines.
"""
from __future__ import annotations

import asyncio
import signal
import threading


class VirtualLoopTimeout(BaseException):
    """Escape asyncio's callback exception handler when tested code blocks."""


class VirtualEventLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__()
        self.now = 0.0
        self._stationary_steps = 0

    def time(self):
        return self.now

    def _run_once(self):
        # This test-only adapter depends on the pinned CPython asyncio loop.
        # No real I/O may be pending: advancing past it would change ordering.
        before = self.now
        if not self._ready:
            timers = [handle.when() for handle in self._scheduled if not handle.cancelled()]
            if not timers:
                raise AssertionError("virtual tasks stalled without a timer")
            self.now = max(self.now, min(timers))
        super()._run_once()
        self._stationary_steps = self._stationary_steps + 1 if self.now == before else 0
        if self._stationary_steps > 1000:
            raise AssertionError("virtual tasks ran 1000 steps without advancing time")


def run_virtual(awaitable):
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("virtual loop watchdog requires the main thread")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def timed_out(_signum, _frame):
        raise VirtualLoopTimeout("virtual loop blocked for 5 real seconds")

    signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, 5, 5)
    try:
        with asyncio.Runner(loop_factory=VirtualEventLoop) as runner:
            return runner.run(awaitable)
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)

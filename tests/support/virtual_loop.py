"""An asyncio clock for in-process fakes, with no socket or worker-thread I/O.

Ready callbacks run normally. Once all tasks are waiting, jump to the next
scheduled timer instead of sleeping on the host clock. asyncio's real timeout
and cancellation machinery therefore runs at exact virtual deadlines.
"""
from __future__ import annotations

import asyncio


class VirtualEventLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__()
        self.now = 0.0

    def time(self):
        return self.now

    def _run_once(self):
        # This test-only adapter depends on the pinned CPython asyncio loop.
        # No real I/O may be pending: advancing past it would change ordering.
        if not self._ready:
            timers = [handle.when() for handle in self._scheduled if not handle.cancelled()]
            if not timers:
                raise AssertionError("virtual tasks stalled without a timer")
            self.now = max(self.now, min(timers))
        super()._run_once()


def run_virtual(awaitable):
    with asyncio.Runner(loop_factory=VirtualEventLoop) as runner:
        return runner.run(awaitable)

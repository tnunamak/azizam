"""Report where the event loop thread is when the loop stops answering.

One blocking call on the loop thread delays every request, including
``/health``. The reverse proxy then marks the gateway down. This watchdog
runs in a daemon thread. It asks the loop to answer a heartbeat. When the
answer is late by more than the threshold, it samples the loop thread's
Python stack until the loop answers, and logs the most frequent stacks.

It reads ``sys._current_frames()``, so it needs no ptrace or root access.
"""

from __future__ import annotations

import asyncio
import collections
import sys
import threading
import time
import traceback
from typing import Callable

_STACK_DEPTH = 14
_TOP_STACKS = 2


def _stack_key(frame) -> tuple[str, ...]:
    entries = traceback.extract_stack(frame)[-_STACK_DEPTH:]
    return tuple(f"{e.filename}:{e.lineno} {e.name}" for e in entries)


class LoopWatchdog:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        loop_thread_id: int,
        *,
        threshold_s: float = 1.0,
        interval_s: float = 0.25,
        sample_s: float = 0.05,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._loop = loop
        self._loop_thread_id = loop_thread_id
        self._threshold_s = threshold_s
        self._interval_s = interval_s
        self._sample_s = sample_s
        self._log = log or (lambda line: print(line, flush=True))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loop-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            answered = threading.Event()
            sent = time.monotonic()
            try:
                self._loop.call_soon_threadsafe(answered.set)
            except RuntimeError:  # loop closed
                return
            if not answered.wait(self._threshold_s):
                self._report(answered, sent)
            self._stop.wait(self._interval_s)

    def _report(self, answered: threading.Event, sent: float) -> None:
        stacks: collections.Counter[tuple[str, ...]] = collections.Counter()
        samples = 0
        while not answered.wait(self._sample_s):
            if self._stop.is_set():
                return
            frame = sys._current_frames().get(self._loop_thread_id)
            if frame is not None:
                stacks[_stack_key(frame)] += 1
                samples += 1
        blocked_s = time.monotonic() - sent
        lines = [f"[loop-watchdog] event loop blocked {blocked_s:.2f}s ({samples} stack samples)"]
        for stack, count in stacks.most_common(_TOP_STACKS):
            lines.append(f"[loop-watchdog]   {count}/{samples} samples in:")
            lines.extend(f"[loop-watchdog]     {entry}" for entry in stack)
        self._log("\n".join(lines))


def start(threshold_s: float) -> LoopWatchdog | None:
    """Start a watchdog for the running loop; a threshold <= 0 disables it."""
    if threshold_s <= 0:
        return None
    watchdog = LoopWatchdog(
        asyncio.get_running_loop(), threading.get_ident(), threshold_s=threshold_s
    )
    watchdog.start()
    return watchdog

"""One closeable iterator owns source cleanup and a synchronous or async release.

Concurrent closes share one cleanup task; closing waits for an active read and
suppresses its late chunk. Cancellation of one closer does not cancel cleanup.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, AsyncIterator, Callable


class LeaseStream:
    """A closeable iterator whose lease outlives reads and source cleanup."""

    def __init__(self, source: AsyncIterator[bytes], release: Callable[[], Any], *, on_exhausted: Callable[[], None] | None = None) -> None:
        self._source = source
        self._release = release
        self._on_exhausted = on_exhausted
        self._closed = False
        self._closing = False
        self._reading = False
        self._read_done = asyncio.Event()
        self._read_done.set()
        self._close_task: asyncio.Task[None] | None = None

    def __aiter__(self) -> LeaseStream:
        return self

    async def __anext__(self) -> bytes:
        if self._closing or self._closed:
            raise StopAsyncIteration
        if self._reading:
            raise RuntimeError("stream read is already running")
        self._reading = True
        self._read_done.clear()
        try:
            try:
                chunk = await self._source.__anext__()
            finally:
                # Signal before awaiting our own cleanup: close waits on this.
                self._reading = False
                self._read_done.set()
        except StopAsyncIteration:
            if self._on_exhausted is not None:
                self._on_exhausted()
            await self.aclose()
            raise
        except BaseException:
            await self.aclose()
            raise
        if self._closing:
            await self.aclose()
            raise StopAsyncIteration
        return chunk

    async def _finish_close(self) -> None:
        await self._read_done.wait()
        try:
            close = getattr(self._source, "aclose", None)
            if close is not None:
                await close()
        finally:
            try:
                released = self._release()
                if inspect.isawaitable(released):
                    await released
            finally:
                self._closed = True

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._finish_close())
            # A cancelled waiter must neither cancel cleanup nor leave its error
            # unobserved. Active waiters still receive the task's original error.
            self._close_task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )
        await asyncio.shield(self._close_task)


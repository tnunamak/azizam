"""Deadlines that pass when nothing can progress, not when a wall clock says so.

A slow-first-byte case needs the code under test to give up on a response the
fake holds back. A real budget races the fake: if it expires before the fake
has even logged the request, the test fails on a loaded host. These helpers
replace the timeout primitive the code under test uses with one that fires
exactly when the fake is blocked on the test clock, which implies it logged the
request and will send nothing until the test advances the clock. That is the
virtual-clock approach of TEST_STRATEGY 4.6 for the process boundary: the
production error handling still runs; only when the deadline passes is decided
by the test.

Install in the test, before the call, with the same clock the fakes use.
Each deadline is one-shot, so later reads in the same test behave normally.
"""

from __future__ import annotations

import asyncio
import types

import httpcore
import pytest
from httpcore._backends.anyio import AnyIOStream

from .clock import VirtualClock


class Deadlines:
    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.read_fired = 0
        self.timeout_fired = 0
        self._read_armed = False
        self._timeout_armed = False


def read_deadline(monkeypatch: pytest.MonkeyPatch, clock: VirtualClock) -> Deadlines:
    """The next socket read with a timeout fails with ReadTimeout once the fake holds the response."""
    state = Deadlines(clock)
    state._read_armed = True
    original = AnyIOStream.read

    async def read(stream, max_bytes: int, timeout: float | None = None) -> bytes:
        if timeout is None or not state._read_armed:
            return await original(stream, max_bytes, timeout)
        reader = asyncio.ensure_future(original(stream, max_bytes, None))
        blocked = asyncio.ensure_future(clock.until_waiting())
        try:
            await asyncio.wait({reader, blocked}, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            reader.cancel()
            blocked.cancel()
            await asyncio.gather(reader, blocked, return_exceptions=True)
            raise
        if reader.done():
            blocked.cancel()
            await asyncio.gather(blocked, return_exceptions=True)
            return reader.result()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        state._read_armed = False
        state.read_fired += 1
        raise httpcore.ReadTimeout("the fake holds the response; the read deadline passed")

    monkeypatch.setattr(AnyIOStream, "read", read)
    return state


class _BlockedTimeout:
    """``asyncio.timeout`` whose deadline is 'the fake is blocked on the clock'."""

    def __init__(self, state: Deadlines) -> None:
        self._state = state
        self._inner = asyncio.timeout(None)
        self._armer: asyncio.Future | None = None

    async def __aenter__(self):
        timeout = await self._inner.__aenter__()
        if self._state._timeout_armed:
            self._armer = asyncio.ensure_future(self._fire(timeout))
        return timeout

    async def _fire(self, timeout: asyncio.Timeout) -> None:
        await self._state.clock.until_waiting()
        self._state._timeout_armed = False
        self._state.timeout_fired += 1
        timeout.reschedule(asyncio.get_running_loop().time())

    async def __aexit__(self, *exc_info):
        if self._armer is not None:
            self._armer.cancel()
            await asyncio.gather(self._armer, return_exceptions=True)
        return await self._inner.__aexit__(*exc_info)


def module_deadline(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType, clock: VirtualClock) -> Deadlines:
    """``module`` calls ``asyncio.timeout(...)``: the first such deadline passes once the fake holds the response."""
    state = Deadlines(clock)
    state._timeout_armed = True

    class _Asyncio:
        def __getattr__(self, name: str):
            return getattr(asyncio, name)

        @staticmethod
        def timeout(_delay):
            return _BlockedTimeout(state)

    monkeypatch.setattr(module, "asyncio", _Asyncio())
    return state

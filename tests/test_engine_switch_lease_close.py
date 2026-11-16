"""Explicit response closure owns the serving lease, even before iteration."""

import asyncio
import gc
import weakref
from contextlib import aclosing

import pytest

from gateway.engine_switch import SwitchController, SwitchLease


class CountingLease:
    def __init__(self):
        self.releases = 0

    def release(self):
        self.releases += 1


class Source:
    def __init__(self, error=None, close_error=None):
        self.reads = 0
        self.closes = 0
        self.error = error
        self.close_error = close_error

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.reads += 1
        if self.reads == 1:
            return b"first"
        if self.error is not None:
            raise self.error
        raise StopAsyncIteration

    async def aclose(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


def test_close_before_first_chunk_releases_and_closes_once():
    async def check():
        serving, source = CountingLease(), Source()
        lease = SwitchLease(serving)
        wrapped = lease.hand_off(source)
        assert lease.handed_off
        assert wrapped.__aiter__() is wrapped
        await wrapped.aclose()
        await wrapped.aclose()
        assert source.reads == 0
        assert source.closes == 1
        assert serving.releases == 1
        with pytest.raises(StopAsyncIteration):
            await wrapped.__anext__()
        assert source.reads == 0
        del wrapped
        gc.collect()
        assert serving.releases == 1

    asyncio.run(check())


def test_full_iteration_and_close_release_once():
    async def check():
        serving, source = CountingLease(), Source()
        wrapped = SwitchLease(serving).hand_off(source)
        assert [chunk async for chunk in wrapped] == [b"first"]
        assert source.closes == 1
        assert serving.releases == 1
        await wrapped.aclose()
        del wrapped
        gc.collect()
        assert source.closes == 1
        assert serving.releases == 1

    asyncio.run(check())


@pytest.mark.parametrize(
    "error",
    [ValueError("source failed"), asyncio.CancelledError()],
    ids=["error", "cancelled"],
)
def test_source_error_closes_releases_and_propagates(error):
    async def check():
        serving, source = CountingLease(), Source(error=error)
        wrapped = SwitchLease(serving).hand_off(source)
        assert await wrapped.__anext__() == b"first"
        with pytest.raises(type(error)) as raised:
            await wrapped.__anext__()
        assert raised.value is error
        assert source.closes == 1
        assert serving.releases == 1
        await wrapped.aclose()
        assert serving.releases == 1

    asyncio.run(check())


def test_aclosing_before_iteration_releases():
    async def check():
        serving, source = CountingLease(), Source()
        wrapped = SwitchLease(serving).hand_off(source)
        async with aclosing(wrapped):
            pass
        assert source.reads == 0
        assert source.closes == 1
        assert serving.releases == 1

    asyncio.run(check())


def test_close_error_still_releases_once():
    async def check():
        error = RuntimeError("close failed")
        serving, source = CountingLease(), Source(close_error=error)
        wrapped = SwitchLease(serving).hand_off(source)
        with pytest.raises(RuntimeError) as raised:
            await wrapped.aclose()
        assert raised.value is error
        await wrapped.aclose()
        assert source.closes == 1
        assert serving.releases == 1

    asyncio.run(check())


def test_drop_without_close_releases():
    serving, source = CountingLease(), Source()
    lease = SwitchLease(serving)
    wrapped = lease.hand_off(source)
    reference = weakref.ref(wrapped)
    del wrapped
    gc.collect()
    assert reference() is None
    assert serving.releases == 1
    lease.release()
    assert serving.releases == 1


def test_release_is_idempotent():
    serving = CountingLease()
    lease = SwitchLease(serving)
    lease.release()
    lease.release()
    assert serving.releases == 1


@pytest.mark.parametrize("already_selected", [True, False], ids=["already-selected", "switch"])
def test_select_lease_construction_failure_balances_activity(already_selected):
    async def check():
        error = RuntimeError("lease failed")
        events = []
        lock = asyncio.Lock()

        class Drain:
            async def clear_for_swap(self, scope, settings):
                events.append("drain")

            def lease(self, scope, *, on_release):
                events.append("lease")
                raise error

        async def observe(name):
            return "chosen" if already_selected or "invoke" in events else "previous"

        async def invoke(name, args):
            events.append("invoke")

        controller = SwitchController(
            drain=Drain(), invoke=invoke, observe=observe, lock_for=lambda scope: lock
        )
        with pytest.raises(RuntimeError) as raised:
            await controller.select(
                {"id": "scope", "offerings": [{"id": "chosen"}]},
                "chosen",
                {"mode": "switch", "switch": {
                    "call": "select", "observe_selected": "selected", "args": {}
                }},
                on_acquired=lambda: events.append("acquired"),
                on_release=lambda: events.append("released"),
            )
        assert raised.value is error
        assert events == (
            ["acquired", "lease", "released"] if already_selected
            else ["acquired", "drain", "invoke", "lease", "released"]
        )
        assert not lock.locked()

    asyncio.run(check())



@pytest.mark.parametrize("cancel_read", [False, True], ids=["yield", "cancel"])
def test_close_waits_for_inflight_read_and_discards_late_chunk(cancel_read):
    async def check():
        entered, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        events = []
        serving = CountingLease()

        async def source():
            try:
                entered.set()
                await release.wait()
                yield b"late chunk"
            finally:
                events.append("source cleaned")
                assert serving.releases == 0

        wrapped = SwitchLease(serving).hand_off(source())
        reading = asyncio.create_task(wrapped.__anext__())
        await entered.wait()

        async def close():
            closing.set()
            await wrapped.aclose()

        closer = asyncio.create_task(close())
        await closing.wait()
        try:
            assert serving.releases == 0
            assert not closer.done()
            if cancel_read:
                reading.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await reading
            else:
                release.set()
                with pytest.raises(StopAsyncIteration):
                    await reading
            await closer
            assert events == ["source cleaned"]
            assert serving.releases == 1
            await wrapped.aclose()
            assert serving.releases == 1
            with pytest.raises(StopAsyncIteration):
                await wrapped.__anext__()
        finally:
            release.set()
            if not reading.done():
                reading.cancel()
            await asyncio.gather(reading, closer, return_exceptions=True)

    asyncio.run(asyncio.wait_for(check(), timeout=3))


def test_concurrent_closes_wait_for_one_source_cleanup_and_one_release():
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()
        first_started, second_started = asyncio.Event(), asyncio.Event()
        events = []
        serving = CountingLease()

        async def source():
            try:
                entered.set()
                await release.wait()
                yield b"late"
            finally:
                events.append("cleaning")
                cleaning.set()
                await finish_cleanup.wait()
                assert serving.releases == 0
                events.append("cleaned")

        wrapped = SwitchLease(serving).hand_off(source())
        reading = asyncio.create_task(wrapped.__anext__())
        await entered.wait()

        async def close(started):
            started.set()
            await wrapped.aclose()

        first = asyncio.create_task(close(first_started))
        await first_started.wait()
        second = asyncio.create_task(close(second_started))
        await second_started.wait()
        try:
            assert not first.done() and not second.done()
            assert serving.releases == 0
            release.set()
            await cleaning.wait()
            assert not first.done() and not second.done()
            assert serving.releases == 0
            finish_cleanup.set()
            with pytest.raises(StopAsyncIteration):
                await reading
            await asyncio.gather(first, second)
            assert events == ["cleaning", "cleaned"]
            assert serving.releases == 1
        finally:
            release.set()
            finish_cleanup.set()
            if not reading.done():
                reading.cancel()
            await asyncio.gather(reading, first, second, return_exceptions=True)

    asyncio.run(asyncio.wait_for(check(), timeout=3))

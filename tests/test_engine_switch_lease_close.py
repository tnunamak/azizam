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


@pytest.mark.parametrize(
    "close_error",
    [None, RuntimeError("held cleanup failed")],
    ids=["success", "error"],
)
def test_cancelled_close_waiter_preserves_shared_cleanup_and_result(close_error):
    async def check():
        cleaning, finish_cleanup = asyncio.Event(), asyncio.Event()
        first_started, second_started = asyncio.Event(), asyncio.Event()
        serving = CountingLease()

        class HeldSource(Source):
            async def aclose(self):
                cleaning.set()
                await finish_cleanup.wait()
                assert serving.releases == 0
                await super().aclose()

        source = HeldSource(close_error=close_error)
        wrapped = SwitchLease(serving).hand_off(source)

        async def close(started):
            started.set()
            await wrapped.aclose()

        first = asyncio.create_task(close(first_started))
        second = asyncio.create_task(close(second_started))
        try:
            await first_started.wait()
            await second_started.wait()
            await cleaning.wait()
            assert not first.done() and not second.done()
            assert serving.releases == 0
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not second.done(), "cancelling one waiter must not cancel cleanup"
            assert serving.releases == 0, "the lease must outlive blocked cleanup"
            finish_cleanup.set()
            if close_error is None:
                await second
            else:
                with pytest.raises(RuntimeError) as raised:
                    await second
                assert raised.value is close_error
            assert source.reads == 0
            assert source.closes == 1
            assert serving.releases == 1
            await wrapped.aclose()
            assert source.closes == 1
            assert serving.releases == 1
        finally:
            finish_cleanup.set()
            await asyncio.gather(first, second, return_exceptions=True)

    asyncio.run(asyncio.wait_for(check(), timeout=3))


@pytest.mark.parametrize("finish_stream", [False, True])
def test_gpu_and_serving_holdings_follow_stream_evidence(tmp_path, finish_stream):
    import time
    from gateway.arbitration.store import ArbitrationStore
    from gateway.arbitration.types import ResourceRef
    from gateway.gpu_admission import AdmissionScheduler, AdmissionOwner
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        async def ready():
            return True
        plan = scheduler.pin(request_id="stream", caller="caller", policy_revision="revision", service_id="svc",
                                 resources=("card/a",), claim_envelope={}, deadline=time.time() + 1, ready=ready)
        owner = await AdmissionOwner.open(plan)
        assert owner.lease.send_intent()
        serving = CountingLease()
        residency = SwitchLease(serving)
        source = Source()
        stream = owner.hand_off(residency.hand_off(source), eof_evidence="declared stream terminal observed")
        try:
            assert store.charge(ResourceRef("physical/card/a", "1")) == 1
            assert await stream.__anext__() == b"first"
            assert store.charge(ResourceRef("physical/card/a", "1")) == 1
            assert serving.releases == 0
            if finish_stream:
                with pytest.raises(StopAsyncIteration):
                    await stream.__anext__()
            await stream.aclose()
            await stream.aclose()
            assert serving.releases == 1 and source.closes == 1
            assert store.ticket("stream").phase == ("completed" if finish_stream else "unknown")
            assert store.charge(ResourceRef("physical/card/a", "1")) == (0 if finish_stream else 1)
            assert store.charge(ResourceRef("@gateway/worker", "1")) == 0
        finally:
            await owner.aclose()
            store.close()
    asyncio.run(scenario())


def test_dropped_admission_stream_closes_source_and_moves_ledger_io_off_loop(tmp_path, monkeypatch):
    import threading
    import time
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler, AdmissionOwner
    async def scenario():
        loop = asyncio.get_running_loop()
        main_thread = threading.get_ident()
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        cleaned = asyncio.Event()
        writers = []
        original = store.finish_ticket
        def tracked(*args, **kwargs):
            writers.append(threading.get_ident())
            result = original(*args, **kwargs)
            loop.call_soon_threadsafe(cleaned.set)
            return result
        monkeypatch.setattr(store, "finish_ticket", tracked)
        async def ready():
            return True
        plan = scheduler.pin(request_id="stream", caller="caller", policy_revision="rev", service_id="svc",
                             resources=("card/a",), claim_envelope={}, deadline=time.time() + 10, ready=ready)
        owner = await AdmissionOwner.open(plan)
        assert owner.lease.send_intent()
        serving, source = CountingLease(), Source()
        stream = owner.hand_off(SwitchLease(serving).hand_off(source))
        try:
            del stream
            gc.collect()
            await asyncio.wait_for(cleaned.wait(), 2)
            await owner.aclose()
            await scheduler.drain_cleanup()
            assert writers and main_thread not in writers
            assert source.closes == 1 and serving.releases == 1
            assert store.ticket("stream").phase == "unknown"
        finally:
            await owner.aclose()
            store.close()
    asyncio.run(scenario())

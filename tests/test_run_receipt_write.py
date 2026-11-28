"""Cancellation remains terminal even when the drained receipt writer fails."""
import asyncio
import threading

import pytest

from gateway.receipts import run_receipt_write


@pytest.mark.parametrize("cancel_count", [0, 1, 2])
def test_worker_failure_preserves_cancellation_and_cause(cancel_count):
    entered = threading.Event()
    release = threading.Event()
    drained = threading.Event()
    failure = RuntimeError("write failed")

    def write():
        entered.set()
        try:
            assert release.wait(30)
            raise failure
        finally:
            drained.set()

    async def scenario():
        task = asyncio.create_task(run_receipt_write(write))
        try:
            assert await asyncio.to_thread(entered.wait, 30)
            for _ in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done(), "cancellation must wait for the worker"
            release.set()
            expected = asyncio.CancelledError if cancel_count else RuntimeError
            with pytest.raises(expected) as raised:
                await task
            assert drained.is_set()
            assert task.cancelled() is bool(cancel_count)
            if cancel_count:
                assert raised.value.__cause__ is failure
            else:
                assert raised.value is failure
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

"""An interrupted reconciliation pass cannot advertise an already-started executor as ready."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.transition_executor import TransitionUnavailable
from tests.test_transition_failure_recovery import run, wire


async def twice_failed_finish(executor, gates, monkeypatch) -> str:
    """The existing real-SQLite fixture pattern: two lost finishes leave a running row."""
    await executor.reconcile_startup()
    assert executor.reconciled()
    real_finish = executor.store.finish
    calls = 0

    def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise OSError("database is locked")
        return real_finish(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(executor.store, "finish", flaky)
        began = await executor.submit(domain="dom", kind="select", idempotency_key="wedge", target="beta")
        op_id = began.operation.op_id
        await asyncio.wait_for(executor.wait(op_id), 5)
        await asyncio.sleep(0)  # let the task's removal callback run
        assert calls == 2
        assert op_id not in executor._tasks
        assert not executor._lock_for("dom").locked()
        row = await asyncio.to_thread(executor.store.get, op_id)
        assert row.state == "running"
        assert gates.state("dom") == "reconciling"
        assert not executor.reconciled()
        assert executor._recovery_pending
    return op_id


async def assert_next_submit_recovers(executor, gates, op_id, key) -> None:
    before = await asyncio.to_thread(executor.store.get, op_id)
    assert before.state == "running"
    result = await executor.submit(domain="dom", kind="select", idempotency_key=key, target="beta")
    assert type(result).__name__ == "Conflict"
    assert (result.operation.op_id, result.operation.state) == (op_id, "reconciling"), (
        "the returned conflict already contains the durably held operation"
    )
    after = await asyncio.to_thread(executor.store.get, op_id)
    assert after.state == "reconciling"
    assert gates.state("dom") == "reconciling"
    assert executor.reconciled()
    assert not executor._recovery_pending
    await executor.resolve(op_id, step="release", outcome="unknown", evidence_ref="owner")


async def join_cancelled(task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cancelled_adapter_query_leaves_recovery_pending_for_the_next_submit(tmp_path, monkeypatch):
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch)
    engine.release_outcome = engine.query_outcome = "unknown"
    executor = runtime.executor()

    async def go():
        op_id = await twice_failed_finish(executor, gates, monkeypatch)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_query(*_args):
            entered.set()
            await release.wait()
            raise AssertionError("the cancelled query must not resume")

        policy_of = executor._policy_of
        with monkeypatch.context() as patch:
            patch.setattr(executor, "_policy_of", lambda domain: {**policy_of(domain), "query_deadline_s": 10})
            patch.setattr(executor, "_adapter_for", lambda _domain: SimpleNamespace(query=blocked_query))
            submit = asyncio.create_task(executor.submit(
                domain="dom", kind="select", idempotency_key="interrupted", target="beta",
            ))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert not executor.reconciled(), "reconciliation remains unready while the adapter is blocked"
            finally:
                await join_cancelled(submit)
        assert not executor.reconciled(), "cancelling the submit must not complete reconciliation"
        assert executor._recovery_pending, "the interrupted pass must still be retried"
        # The real adapter is restored; no restart or owner action precedes retry.
        await assert_next_submit_recovers(executor, gates, op_id, "after-cancellation")

    run(go())


def test_ordinary_error_exiting_recovery_leaves_the_next_submit_able_to_retry(tmp_path, monkeypatch):
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch)
    engine.release_outcome = engine.query_outcome = "unknown"
    executor = runtime.executor()

    async def go():
        op_id = await twice_failed_finish(executor, gates, monkeypatch)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def broken_recovery(_operation):
            entered.set()
            await release.wait()
            raise RuntimeError("recovery failed")

        async def broken_hold(_operation):
            # Ordinary adapter errors alone are already converted to unknown
            # and durably held. This second fault makes the pass actually exit.
            raise OSError("recovery fallback could not complete")

        with monkeypatch.context() as patch:
            patch.setattr(executor, "_recover", broken_recovery)
            patch.setattr(executor, "_hold", broken_hold)
            submit = asyncio.create_task(executor.submit(
                domain="dom", kind="select", idempotency_key="errored", target="beta",
            ))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert not executor.reconciled(), "an in-flight recovery is not ready"
                release.set()
                with pytest.raises(TransitionUnavailable):
                    await asyncio.wait_for(asyncio.shield(submit), 2)
            finally:
                if not submit.done():
                    await join_cancelled(submit)
                elif not submit.cancelled():
                    submit.exception()
        assert not executor.reconciled(), "an ordinary error exit must not complete reconciliation"
        assert executor._recovery_pending
        await assert_next_submit_recovers(executor, gates, op_id, "after-error")

    run(go())

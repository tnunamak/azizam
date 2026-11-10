"""Cancellation cannot strand a closed gate without an executor or a recovery path."""
from __future__ import annotations

import asyncio
import threading

import pytest

from gateway.transition_ops import EffectResult
from gateway.transition_profiles import ProfileError, parse_profile
from gateway.transition_snapshot_steps import _effect
from tests.test_transition_cancel_recovery import (
    assert_next_submit_recovers, join_cancelled, run, wire,
)


class AdapterOverride:
    def __init__(self, adapter, **overrides):
        self.adapter = adapter
        self.overrides = overrides

    def __getattr__(self, name):
        return self.overrides.get(name, getattr(self.adapter, name))


async def held_release(executor, engine):
    engine.release_outcome = engine.query_outcome = "unknown"
    began = await executor.submit(
        domain="dom", kind="select", idempotency_key="held", target="beta",
    )
    held = await executor.wait(began.operation.op_id)
    await asyncio.sleep(0)  # remove the completed executor task
    assert (held.state, held.step, held.step_outcome) == ("reconciling", "release", "unknown")
    return held


def test_cancel_after_resume_commit_still_hands_off_the_operation(tmp_path, monkeypatch):
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch)
    executor = runtime.executor()

    async def go():
        held = await held_release(executor, engine)
        loop = asyncio.get_running_loop()
        committed = asyncio.Event()
        launched = asyncio.Event()
        release_worker = threading.Event()
        real_resume = executor.store.resume
        real_launch = executor._launch_resume

        def paused_resume(op_id):
            resumed = real_resume(op_id)
            loop.call_soon_threadsafe(committed.set)
            if not release_worker.wait(5):
                raise AssertionError("test did not release the committed resume")
            return resumed

        def launch(operation, after):
            real_launch(operation, after)
            launched.set()

        with monkeypatch.context() as patch:
            patch.setattr(executor.store, "resume", paused_resume)
            patch.setattr(executor, "_launch_resume", launch)
            resolver = asyncio.create_task(executor.resolve(
                held.op_id, step="release", outcome="success", evidence_ref="owner",
            ))
            try:
                await asyncio.wait_for(committed.wait(), 2)
                assert (await asyncio.to_thread(executor.store.get, held.op_id)).state == "running"
                await join_cancelled(resolver)
            finally:
                release_worker.set()
                if not resolver.done():
                    await join_cancelled(resolver)
            try:
                await asyncio.wait_for(launched.wait(), 2)
            except asyncio.TimeoutError:
                pass
            assert launched.is_set(), "a committed resume must hand off even when its caller was cancelled"
            done = await asyncio.wait_for(executor.wait(held.op_id), 5)
        assert done.state == "succeeded"
        assert gates.state("dom") == "open"

    run(go())


def test_failed_handoff_after_commit_marks_recovery_pending(tmp_path, monkeypatch):
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch)
    executor = runtime.executor()

    async def go():
        held = await held_release(executor, engine)

        def failed_launch(*_args):
            raise RuntimeError("handoff failed")

        with monkeypatch.context() as patch:
            patch.setattr(executor, "_launch_resume", failed_launch)
            with pytest.raises(RuntimeError, match="handoff failed"):
                await executor.resolve(held.op_id, step="release", outcome="success", evidence_ref="owner")
        assert executor._recovery_pending, "a committed row without a task must be recovered"
        assert not executor.reconciled()
        assert gates.state("dom") == "reconciling"
        await executor.ensure_reconciled()
        done = await asyncio.wait_for(executor.wait(held.op_id), 5)
        assert done.state == "succeeded"
        assert gates.state("dom") == "open"

    run(go())


@pytest.mark.parametrize("step", ["release", "load"])
def test_cancelled_adapter_child_is_queried_and_durably_held(tmp_path, monkeypatch, step):
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch)
    executor = runtime.executor()

    async def go():
        queries = []
        adapter = executor._adapter_for("dom")

        async def cancelled(*_args):
            raise asyncio.CancelledError

        async def query(_domain, intent):
            queries.append(intent.step)
            return EffectResult(outcome="unknown")

        monkeypatch.setattr(executor, "_adapter_for", lambda _domain: AdapterOverride(
            adapter, **{step: cancelled, "query": query},
        ))
        began = await executor.submit(
            domain="dom", kind="select", idempotency_key=f"cancel-{step}", target="beta",
        )
        try:
            held = await asyncio.wait_for(executor.wait(began.operation.op_id), 5)
        except asyncio.CancelledError:
            pytest.fail("an independently cancelled adapter child must not cancel the executor")
        assert queries == [step], "a cancelled effect is unknown and must be queried"
        assert (held.state, held.step, held.step_outcome) == ("reconciling", step, "unknown")
        assert held.evidence_ref == "adapter_error:CancelledError"
        assert gates.state("dom") == "reconciling"
        await executor.resolve(held.op_id, step=step, outcome="unknown", evidence_ref="owner")

    run(go())


def test_cancelled_snapshot_adapter_child_returns_unknown():
    async def go():
        async def cancelled():
            raise asyncio.CancelledError

        try:
            result = await _effect(cancelled(), 1, "checkpoint")
        except asyncio.CancelledError:
            pytest.fail("a cancelled snapshot adapter child must settle as unknown")
        assert (result.outcome, result.evidence_ref) == ("unknown", "adapter_error:CancelledError")

    run(go())


def test_cancelled_executor_leaves_recovery_pending_for_next_submit(tmp_path, monkeypatch):
    gates, engine, runtime, _ = wire(tmp_path, monkeypatch)
    engine.query_outcome = "unknown"
    executor = runtime.executor()

    async def go():
        entered = asyncio.Event()
        release_child = asyncio.Event()
        child_done = asyncio.Event()
        adapter = executor._adapter_for("dom")

        async def blocked_release(*_args):
            entered.set()
            try:
                await release_child.wait()
                return EffectResult(outcome="unknown")
            finally:
                child_done.set()

        with monkeypatch.context() as patch:
            patch.setattr(executor, "_adapter_for", lambda _domain: AdapterOverride(
                adapter, release=blocked_release,
            ))
            began = await executor.submit(
                domain="dom", kind="select", idempotency_key="shutdown", target="beta",
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await join_cancelled(executor._tasks[began.operation.op_id])
            finally:
                release_child.set()
                await asyncio.wait_for(child_done.wait(), 2)
            await asyncio.sleep(0)
        assert executor._recovery_pending, "shutdown must not leave this process advertising readiness"
        assert not executor.reconciled()
        assert gates.state("dom") == "reconciling"
        await assert_next_submit_recovers(executor, gates, began.operation.op_id, "after-shutdown")

    run(go())


@pytest.mark.parametrize("field", ["servable_check", "restore.reset.verify"])
def test_padded_observation_references_are_refused_at_load(field):
    raw = {
        "profile_version": 1,
        "id": "padding",
        "operations": {
            name: f"transition.{name}"
            for name in ("resident", "release", "load", "query", "checkpoint", "restore", "reset", "compatibility")
        },
        "observations": {"healthy": "transition.healthy"},
    }
    if field == "servable_check":
        raw[field] = " healthy "
    else:
        raw["restore"] = {"reset": {"verify": " healthy "}}
    with pytest.raises(ProfileError, match="surrounding whitespace"):
        parse_profile(raw, source="padded.yml")

"""Declared operation boundaries and ownership of versioned runner completion."""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway import declared_operation_job, media_job_intents, media_jobs
from gateway.engine_protocol import jobs as protocol_jobs
from gateway.engine_protocol.errors import BudgetExceeded, ProtocolError

JOB_ID = "declared-job-test"
INTENT = "specimen.revise"
OUTER_WAIT_S = 1.0
SHIPPED_ROWS = media_job_intents.load_rows()


def _target(intent: str):
    return SimpleNamespace(
        service={"enabled": True},
        description=SimpleNamespace(operations={intent: SimpleNamespace(job=object())}),
    )


def _runtime(adapter, timing=(0.001, 0.01)):
    return SimpleNamespace(
        adapter=adapter,
        connection_for=Mock(return_value=object()),
        poll_timing=Mock(return_value=timing),
        request=Mock(side_effect=lambda _op, body: dict(body)),
        result_context=lambda: None,
    )


def _file():
    return SimpleNamespace(id="result-asset", content_type="application/octet-stream", size=3)


@pytest.fixture
def job_store(monkeypatch):
    snapshot = {"status": media_jobs.STATUS_RUNNING, "metadata": {}}
    status = Mock()
    event = Mock()
    monkeypatch.setattr(media_jobs, "get", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(media_jobs, "update_status", status)
    monkeypatch.setattr(media_jobs, "append_event", event)
    return SimpleNamespace(snapshot=snapshot, status=status, event=event)


@pytest.mark.parametrize("row", SHIPPED_ROWS, ids=lambda row: row.intent)
def test_every_shipped_intent_stays_out_of_declared_resolution(monkeypatch, row):
    resolve = Mock(return_value=_target(row.intent))
    monkeypatch.setattr(declared_operation_job.dispatch, "resolve", resolve)

    assert declared_operation_job.resolve({}, row.intent, "owner-offering") is None
    resolve.assert_not_called()


def test_an_unfamiliar_intent_still_resolves(monkeypatch):
    target = _target(INTENT)
    resolve = Mock(return_value=target)
    monkeypatch.setattr(declared_operation_job.dispatch, "resolve", resolve)

    assert declared_operation_job.resolve({}, INTENT, "owner-offering") is target
    resolve.assert_called_once_with({}, "owner-offering", INTENT)


def test_resolution_reads_the_loader_instead_of_a_copied_or_cached_intent_list(monkeypatch):
    rows = Mock(return_value=(SimpleNamespace(intent=INTENT),))
    resolve = Mock(return_value=_target(INTENT))
    monkeypatch.setattr(media_job_intents, "load_rows", rows)
    monkeypatch.setattr(declared_operation_job.dispatch, "resolve", resolve)

    assert declared_operation_job.resolve({}, INTENT, "owner-offering") is None
    rows.assert_called_once_with()
    resolve.assert_not_called()


def test_terminal_v2_replay_adds_no_status_asset_or_event(job_store):
    adapter = SimpleNamespace(
        interface_version=protocol_jobs.ADAPTER_INTERFACE_VERSION_V2,
        start=AsyncMock(),
    )
    runtime = _runtime(adapter)
    job_store.snapshot["status"] = media_jobs.STATUS_SUCCEEDED

    asyncio.run(declared_operation_job.run(runtime, INTENT, {}, job_id=JOB_ID))

    adapter.start.assert_not_awaited()
    runtime.connection_for.assert_not_called()
    job_store.status.assert_not_called()
    job_store.event.assert_not_called()


@pytest.mark.parametrize("immediate", [True, False], ids=["start-bundle", "polled-bundle"])
def test_v2_bundle_keeps_the_runner_as_its_only_completion_owner(monkeypatch, job_store, immediate):
    # These doubles preserve isinstance checks without assuming unseen constructor signatures.
    bundle = Mock(spec=protocol_jobs.ResultBundle)
    ref = SimpleNamespace(id="upstream-job")
    outcome = (
        Mock(spec=protocol_jobs.Completed, bundle=bundle)
        if immediate
        else Mock(spec=protocol_jobs.Pending, ref=ref)
    )
    adapter = SimpleNamespace(
        interface_version=protocol_jobs.ADAPTER_INTERFACE_VERSION_V2,
        start=AsyncMock(return_value=outcome),
        poll=AsyncMock(return_value=SimpleNamespace(state=protocol_jobs.JobState.succeeded)),
        results=AsyncMock(return_value=bundle),
    )
    runtime = _runtime(adapter)
    commit = Mock(return_value=bundle)
    monkeypatch.setattr(protocol_jobs, "_commit_bundle", commit)

    asyncio.run(declared_operation_job.run(runtime, INTENT, {}, job_id=JOB_ID))

    commit.assert_called_once()
    assert commit.call_args.args == (runtime, JOB_ID, bundle)
    assert commit.call_args.kwargs == ({} if immediate else {"clear_marker": False})
    if not immediate:
        adapter.results.assert_awaited_once()
    # The real runner can record progress, but the wrapper may not publish a second completion/asset/event.
    assert all(call.args[1] != media_jobs.STATUS_SUCCEEDED for call in job_store.status.call_args_list)
    assert all("add_assets" not in call.kwargs for call in job_store.status.call_args_list)
    job_store.event.assert_not_called()


def test_v1_empty_result_refuses_without_completion(monkeypatch, job_store):
    monkeypatch.setattr(protocol_jobs, "run", AsyncMock(return_value=[]))

    with pytest.raises(ProtocolError, match="The engine returned no result file") as caught:
        asyncio.run(declared_operation_job.run(object(), INTENT, {}, job_id=JOB_ID))

    assert caught.value.code == "malformed_response"
    job_store.status.assert_not_called()
    job_store.event.assert_not_called()


def test_v1_single_file_is_completed_once(monkeypatch, job_store):
    ref = _file()
    monkeypatch.setattr(protocol_jobs, "run", AsyncMock(return_value=[ref]))

    asyncio.run(declared_operation_job.run(object(), INTENT, {}, job_id=JOB_ID))

    job_store.status.assert_called_once_with(
        JOB_ID,
        media_jobs.STATUS_SUCCEEDED,
        progress=100,
        path=media_jobs.DB_PATH,
        add_assets=[{"asset_id": ref.id, "kind": "file", "media_type": ref.content_type, "size_bytes": ref.size}],
    )
    job_store.event.assert_called_once_with(
        JOB_ID, "succeeded", {"status": "succeeded"}, path=media_jobs.DB_PATH,
    )


@pytest.mark.parametrize("caller_cancel", [False, True], ids=["wait-budget", "caller-cancel"])
@pytest.mark.parametrize("resist_cancel", [False, True], ids=["cooperative-cleanup", "resistant-cleanup"])
def test_unresponsive_cancel_does_not_hold_terminal_accounting(job_store, caller_cancel, resist_cancel):
    async def scenario():
        polled = asyncio.Event()
        cancel_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def poll(_conn, _ref):
            polled.set()
            return SimpleNamespace(state=protocol_jobs.JobState.running)

        async def cancel(_conn, _ref):
            cancel_started.set()
            try:
                await release_cleanup.wait()
            except asyncio.CancelledError:
                if not resist_cancel:
                    raise
                await release_cleanup.wait()

        adapter = SimpleNamespace(
            submit=AsyncMock(return_value=SimpleNamespace(id="upstream-job")),
            poll=poll,
            cancel=cancel,
        )
        task = asyncio.create_task(protocol_jobs.run(_runtime(adapter), INTENT, {}, job_id=JOB_ID))
        try:
            if caller_cancel:
                await asyncio.wait_for(polled.wait(), timeout=OUTER_WAIT_S)
                task.cancel()
            # Unlike wait_for(task), this outer guard cannot hang waiting for cancellation acknowledgment.
            done, _pending = await asyncio.wait({task}, timeout=OUTER_WAIT_S)
            assert task in done, "The outer test deadline arrived before terminal accounting"
            error = asyncio.CancelledError if caller_cancel else BudgetExceeded
            with pytest.raises(error, match="Cleanup is unknown; the upstream may still run"):
                await task
            assert cancel_started.is_set()
        finally:
            # Even the old implementation's failing case must not strand a resistant fake during test teardown.
            release_cleanup.set()
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BudgetExceeded):
                await task
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_v1_fetches_and_completes_all_assets_in_engine_order(job_store):
    # Use the real protocol runner, not a monkeypatch of the function under test.
    refs = [
        SimpleNamespace(id="result-z", content_type="image/png", size=31),
        SimpleNamespace(id="result-a", content_type="audio/wav", size=47),
        SimpleNamespace(id="result-m", content_type="application/octet-stream", size=5),
    ]
    upstream = SimpleNamespace(id="upstream-job")
    observed = SimpleNamespace(state=protocol_jobs.JobState.succeeded)
    adapter = SimpleNamespace(
        submit=AsyncMock(return_value=upstream),
        poll=AsyncMock(return_value=observed),
        fetch_assets=AsyncMock(return_value=refs),
    )
    runtime = _runtime(adapter, timing=(0.001, 1.0))
    body = {"input": "synthetic"}
    asyncio.run(declared_operation_job.run(runtime, INTENT, body, job_id=JOB_ID))

    conn = runtime.connection_for.return_value
    adapter.submit.assert_awaited_once_with(conn, INTENT, body)
    adapter.poll.assert_awaited_once_with(conn, upstream)
    adapter.fetch_assets.assert_awaited_once_with(conn, upstream, observed)
    completions = [
        call for call in job_store.status.call_args_list
        if call.args[1] == media_jobs.STATUS_SUCCEEDED
    ]
    assert len(completions) == 1
    assert completions[0].args == (JOB_ID, media_jobs.STATUS_SUCCEEDED)
    assert completions[0].kwargs == {
        "progress": 100, "path": media_jobs.DB_PATH,
        "add_assets": [
            {"asset_id": "result-z", "kind": "image", "media_type": "image/png", "size_bytes": 31},
            {"asset_id": "result-a", "kind": "audio", "media_type": "audio/wav", "size_bytes": 47},
            {"asset_id": "result-m", "kind": "file", "media_type": "application/octet-stream", "size_bytes": 5},
        ],
    }
    job_store.event.assert_called_once_with(
        JOB_ID, "succeeded", {"status": "succeeded"}, path=media_jobs.DB_PATH,
    )


def test_persisted_cancel_resistant_cleanup_settles_without_success(job_store):
    async def scenario():
        cancel_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()
        cancel_tasks = set()

        async def cancel(_conn, _ref):
            cancel_tasks.add(asyncio.current_task())
            cancel_started.set()
            try:
                while not release_cleanup.is_set():
                    try:
                        await release_cleanup.wait()
                    except asyncio.CancelledError:
                        # Model an adapter that does not acknowledge task cancellation.
                        continue
            finally:
                cleanup_finished.set()

        adapter = SimpleNamespace(
            submit=AsyncMock(return_value=SimpleNamespace(id="upstream-job")),
            poll=AsyncMock(return_value=SimpleNamespace(state=protocol_jobs.JobState.succeeded)),
            fetch_assets=AsyncMock(return_value=[_file()]),
            cancel=AsyncMock(side_effect=cancel),
        )
        job_store.snapshot["metadata"]["cancel_requested_at"] = "synthetic-cancel-request"
        task = asyncio.create_task(protocol_jobs.run(_runtime(adapter), INTENT, {}, job_id=JOB_ID))
        try:
            done, _pending = await asyncio.wait({task}, timeout=OUTER_WAIT_S)
            assert task in done, "Persisted cancellation held the runner beyond its outer deadline"
            with pytest.raises(
                media_jobs.CancellationPending, match="Cleanup is unknown; the upstream may still run",
            ):
                await task
            assert cancel_started.is_set()
            assert not cleanup_finished.is_set(), "The resistant upstream stop is still unconfirmed"
            adapter.cancel.assert_awaited_once()
            adapter.submit.assert_awaited_once()
            adapter.poll.assert_not_awaited()
            adapter.fetch_assets.assert_not_awaited()
            assert all(call.args[1] != media_jobs.STATUS_SUCCEEDED
                       for call in job_store.status.call_args_list)
            assert all("add_assets" not in call.kwargs for call in job_store.status.call_args_list)
            job_store.event.assert_not_called()
        finally:
            release_cleanup.set()
            # Release before cancelling/joining, including the old direct-await failing path.
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if cancel_tasks:
                await asyncio.gather(*cancel_tasks, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("exit_path", ["success", "wait-budget", "caller-cancel"])
def test_resistant_event_follower_cannot_hold_runner_exit(monkeypatch, job_store, exit_path):
    async def scenario():
        started = asyncio.Event()
        polled = asyncio.Event()
        release = asyncio.Event()
        follower_tasks = set()
        bundle = Mock(spec=protocol_jobs.ResultBundle)
        commit = Mock(return_value=bundle)
        monkeypatch.setattr(protocol_jobs, "_commit_bundle", commit)

        async def events(_conn, _ref):
            follower_tasks.add(asyncio.current_task())
            started.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            # A late event must not publish progress after the runner has left.
            yield SimpleNamespace(state=protocol_jobs.JobState.running)

        async def poll(_conn, _ref):
            await started.wait()
            polled.set()
            state = (protocol_jobs.JobState.succeeded if exit_path == "success"
                     else protocol_jobs.JobState.pending)
            return SimpleNamespace(state=state)

        adapter = SimpleNamespace(
            interface_version=protocol_jobs.ADAPTER_INTERFACE_VERSION_V2,
            start=AsyncMock(return_value=Mock(
                spec=protocol_jobs.Pending, ref=SimpleNamespace(id="upstream-job"))),
            poll=poll,
            events=events,
            results=AsyncMock(return_value=bundle),
            cancel=AsyncMock(),
        )
        runtime = _runtime(adapter, timing=(0.001, 0.02))
        task = asyncio.create_task(protocol_jobs.run(
            runtime, INTENT, {}, job_id=JOB_ID, follow_events=True))
        try:
            if exit_path == "caller-cancel":
                await asyncio.wait_for(polled.wait(), timeout=OUTER_WAIT_S)
                task.cancel()
            # Never wait_for the runner: the old cancellation-resistant join
            # would otherwise hang the red test while acknowledging cancellation.
            done, _pending = await asyncio.wait({task}, timeout=OUTER_WAIT_S)
            assert task in done, "Follower cleanup exceeded the outer test deadline"
            assert started.is_set()
            assert not release.is_set()
            if exit_path == "success":
                assert await task is bundle
                commit.assert_called_once_with(runtime, JOB_ID, bundle, clear_marker=False)
                adapter.results.assert_awaited_once()
                adapter.cancel.assert_not_awaited()
            else:
                expected = BudgetExceeded if exit_path == "wait-budget" else asyncio.CancelledError
                with pytest.raises(expected):
                    await task
                commit.assert_not_called()
                adapter.results.assert_not_awaited()
                adapter.cancel.assert_awaited_once()
            before = job_store.status.call_count
            release.set()
            await asyncio.gather(*follower_tasks, return_exceptions=True)
            await asyncio.sleep(0)  # run late task-result retrieval callbacks
            assert job_store.status.call_count == before
            assert commit.call_count == (1 if exit_path == "success" else 0)
            job_store.event.assert_not_called()
        finally:
            # Release even the original failing join before awaiting teardown.
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if follower_tasks:
                await asyncio.gather(*follower_tasks, return_exceptions=True)

    asyncio.run(scenario())

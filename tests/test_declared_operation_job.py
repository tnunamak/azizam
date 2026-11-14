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

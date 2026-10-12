import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Response

import proxy
from gateway import media_jobs, receipt_store
from gateway.receipts import RouteReceipt


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    media_jobs.init_db(path)
    monkeypatch.setattr(media_jobs, "DB_PATH", path)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOADS", {})
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOAD_SIZES", {})
    for kind in ("image", "audio", "video"):
        monkeypatch.setattr(proxy, f"_{kind}_media_job_tasks", set())
        monkeypatch.setattr(proxy, f"_{kind}_media_job_scheduled", set())
    monkeypatch.setattr(proxy, "_identity_job_tasks", set())
    return path


def make_job(path, intent="image.generate", **kwargs):
    return media_jobs.create(media_jobs.MediaJob(intent=intent, **kwargs), path=path)


def write_spool(job_id):
    path = proxy._image_media_job_payload_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"body": {}}', encoding="utf-8")
    return path


@pytest.mark.parametrize("kind", ["image", "audio", "video"])
def test_scheduler_deduplicates_and_settles_unexpected_worker_failure(jobs, monkeypatch, kind):
    job = make_job(jobs, intent=f"{kind}.generate")
    spool = write_spool(job.job_id)

    async def scenario():
        entered, release, callback_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        executions = []

        async def fail(job_id):
            executions.append(job_id)
            entered.set()
            await release.wait()
            raise RuntimeError("fixture failure")

        monkeypatch.setattr(proxy, f"_run_{kind}_media_job", fail)
        schedule = getattr(proxy, f"_schedule_{kind}_media_job")
        assert schedule(job.job_id) is True
        assert schedule(job.job_id) is False
        task, = getattr(proxy, f"_{kind}_media_job_tasks")
        # Registered after the production callback: this event observes its effects.
        task.add_done_callback(lambda _: callback_finished.set())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            release.set()
            await asyncio.wait_for(callback_finished.wait(), 5)
            assert executions == [job.job_id]
            assert job.job_id not in getattr(proxy, f"_{kind}_media_job_scheduled")
            assert task not in getattr(proxy, f"_{kind}_media_job_tasks")
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    row = media_jobs.get(job.job_id, audience="internal", path=jobs)
    assert row["status"] == "failed"
    assert "stopped unexpectedly" in row["error"]
    assert not spool.exists()


@pytest.mark.parametrize(
    "mode,status_response,content_response,expected_http",
    [
        ("poll-error", Response(status_code=503, content='{"error":{"message":"offline"}}',
                                media_type="application/json"), None, 503),
        ("upstream-failed", Response(status_code=200, content='{"status":"failed"}',
                                     media_type="application/json"), None, 200),
        ("content-error", Response(status_code=200, content='{"status":"succeeded"}',
                                   media_type="application/json"),
                          Response(status_code=502, content='{"error":{"message":"no output"}}',
                                   media_type="application/json"), 502),
        ("timeout", Response(status_code=200, content='{"status":"running","progress":45}',
                            media_type="application/json"), None, 504),
    ],
)
def test_video_existing_submission_settles_failures_and_deadline(
        jobs, monkeypatch, mode, status_response, content_response, expected_http):
    job = make_job(jobs, intent="video.generate", status="running",
                   metadata={"upstream_video_id": "video_fixture"})
    spool = write_spool(job.job_id)
    monkeypatch.setattr(proxy, "_load_video_media_job_payload", lambda _: {})
    monkeypatch.setattr(proxy, "_request_shim_from_video_job_payload", lambda _: object())
    monkeypatch.setattr(proxy, "_auth_context_from_image_job_payload",
                        lambda _: proxy.NO_AUTH_CONTEXT)
    submit = AsyncMock(side_effect=AssertionError("resubmitted existing video"))
    status = AsyncMock(side_effect=[status_response,
                                    AssertionError("polled again after the outcome or deadline")])
    content = AsyncMock(return_value=content_response)
    store_asset = Mock(side_effect=AssertionError("failed video published an asset"))
    monkeypatch.setattr(proxy, "proxy_comfy_video_create", submit)
    monkeypatch.setattr(proxy, "proxy_comfy_video_status", status)
    monkeypatch.setattr(proxy, "proxy_comfy_video_content", content)
    monkeypatch.setattr(proxy, "store_asset_bytes", store_asset)
    monkeypatch.setattr(proxy, "_media_job_admission_response",
                        Mock(side_effect=AssertionError("existing video was re-admitted")))
    # Only proxy's clock is replaced; the event loop and store clocks stay real.
    monkeypatch.setattr(proxy, "time",
                        SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 2]),
                                        time=lambda: 100))
    monkeypatch.setattr(proxy, "COMFY_VIDEO_TIMEOUT", 1)
    monkeypatch.setattr(proxy.asyncio, "sleep", AsyncMock())

    async def scenario():
        semaphore = asyncio.Semaphore(1)
        monkeypatch.setattr(proxy, "_video_media_job_semaphore", semaphore)
        await proxy._run_video_media_job(job.job_id)
        assert not semaphore.locked()

    asyncio.run(scenario())
    submit.assert_not_awaited()
    status.assert_awaited_once_with("video_fixture", proxy.NO_AUTH_CONTEXT)
    if mode == "content-error":
        content.assert_awaited_once()
    else:
        content.assert_not_awaited()
    store_asset.assert_not_called()
    row = media_jobs.get(job.job_id, audience="internal", path=jobs)
    assert row["status"] == "failed"
    assert row["assets"] == []
    assert row["metadata"]["last_http_status"] == expected_http
    assert not media_jobs.holds_resources(row["status"])
    if mode == "timeout":
        assert "timed out" in row["error"].lower()
    events = media_jobs.events(job.job_id, audience="internal", path=jobs)
    assert events[-1]["event_type"] == "failed"
    assert not spool.exists()


def test_receipt_maintenance_retries_after_failure_then_stops(monkeypatch, tmp_path):
    calls = []
    maintain = Mock(side_effect=[RuntimeError("temporary store failure"), 3])
    monkeypatch.setattr(proxy.receipt_store, "maintain", maintain)
    monkeypatch.setattr(proxy, "ADMIN_STORE", SimpleNamespace(path=tmp_path / "admin.db"))

    async def scenario():
        stop = asyncio.Event()
        waits = 0

        async def controlled_wait(awaitable, timeout):
            nonlocal waits
            awaitable.close()
            assert timeout == 3600
            waits += 1
            if waits <= 2:
                raise asyncio.TimeoutError
            stop.set()
            return True

        async def fake_thread(fn, *args, **kwargs):
            calls.append((fn, args, kwargs))
            return fn(*args, **kwargs)

        monkeypatch.setattr(proxy.asyncio, "wait_for", controlled_wait)
        monkeypatch.setattr(proxy.asyncio, "to_thread", fake_thread)
        await proxy._receipts_maintenance_loop(stop)
        assert waits == 3

    asyncio.run(scenario())
    assert maintain.call_count == 2
    assert calls == [(maintain, (proxy.ADMIN_STORE.path,), {})] * 2


def test_receipt_maintenance_does_not_swallow_task_cancellation(monkeypatch, tmp_path):
    maintain = Mock()
    monkeypatch.setattr(proxy.receipt_store, "maintain", maintain)
    monkeypatch.setattr(proxy, "ADMIN_STORE", SimpleNamespace(path=tmp_path / "admin.db"))

    async def due_wait(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    async def cancelled_thread(fn, *args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(proxy.asyncio, "wait_for", due_wait)
    monkeypatch.setattr(proxy.asyncio, "to_thread", cancelled_thread)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await proxy._receipts_maintenance_loop(asyncio.Event())

    asyncio.run(scenario())
    maintain.assert_not_called()

"""Restart and cancellation contracts against isolated durable stores."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import Response

import proxy
from tests.support.access import access_context  # noqa: E402
from gateway import media_jobs, me_make


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "jobs.db"
    media_jobs.init_db(path)
    monkeypatch.setattr(media_jobs, "DB_PATH", path)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    monkeypatch.setattr(proxy, "_delete_image_media_job_payload", Mock())
    return path


@pytest.mark.parametrize("kind,intent", [("image", "image.generate"), ("audio", "audio.generate"), ("video", "video.generate")])
@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("enabled,payload", [(True, True), (True, False), (False, True)])
def test_restart_recovers_or_explains_every_job(store, monkeypatch, kind, intent, status, enabled, payload):
    job = media_jobs.MediaJob(intent=intent, status=status, metadata={"mode": f"async_{kind}_generation"})
    media_jobs.create(job, path=store)
    monkeypatch.setattr(proxy, "COMFY_ENABLED", True)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_RESUME_ON_START", enabled)
    monkeypatch.setattr(proxy, "_has_image_media_job_payload", lambda _: payload)
    scheduled = Mock(return_value=True)
    monkeypatch.setattr(proxy, f"_schedule_{kind}_media_job", scheduled)
    getattr(proxy, f"_resume_pending_{kind}_media_jobs")()
    current = media_jobs.get(job.job_id, path=store)
    if enabled and payload and status == "queued":
        scheduled.assert_called_once_with(job.job_id)
    else:
        assert current["status"] == "failed"
        assert "restart" in current["error"]
        assert not media_jobs.holds_resources(current["status"])


def test_restart_has_no_history_page_limit(store, monkeypatch):
    for i in range(205):
        media_jobs.create(media_jobs.MediaJob(job_id=f"j{i}", intent="image.generate", metadata={"mode": "async_image_generation"}), path=store)
    monkeypatch.setattr(proxy, "_has_image_media_job_payload", lambda _: False)
    proxy._resume_pending_image_media_jobs()
    assert media_jobs.unfinished(path=store) == []


def test_startup_with_resume_disabled_settles_make_and_unknown_jobs(store, monkeypatch):
    for mode in [me_make.JOB_MODE, "legacy"]:
        media_jobs.create(media_jobs.MediaJob(intent="image.generate", metadata={"mode": mode}), path=store)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_RESUME_ON_START", False)
    proxy._recover_media_jobs()
    assert media_jobs.unfinished(path=store) == []
    assert all(job["error"] for job in media_jobs.recent(path=store))


def test_pending_cancel_survives_until_worker_releases(store, monkeypatch):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        semaphore = asyncio.Semaphore(1)
        job = media_jobs.MediaJob(intent="audio.generate", status="running")
        media_jobs.create(job, path=store)

        @proxy._settle_native_job
        async def worker(job_id):
            async with semaphore:
                entered.set()
                await release.wait()
                media_jobs.update_status(job_id, "succeeded", add_assets=[{"asset_id": "aud_fake"}], path=store)

        task = asyncio.create_task(worker(job.job_id))
        await entered.wait()
        result = proxy._cancel_media_job(job.job_id, audience="client")
        assert result["status"] == "running"
        assert result["cancel_requested"] is True
        assert media_jobs.holds_resources(result["status"])
        assert semaphore.locked()
        release.set()
        await task
        final = media_jobs.get(job.job_id, path=store)
        assert final["status"] == "canceled"
        assert final["assets"] == []
        assert not semaphore.locked()
        assert not final["cancel_requested"]
    asyncio.run(scenario())


def test_make_cancel_does_not_close_backend_call_before_it_stops():
    async def scenario():
        gate = proxy._MakePublishGate()
        started, release = asyncio.Event(), asyncio.Event()
        async def work():
            started.set()
            await release.wait()
            return Response(status_code=200)
        task = asyncio.create_task(proxy._cancellable_make_call(gate, work()))
        await started.wait()
        gate.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(me_make.MakeCanceled):
            await task
    asyncio.run(scenario())


def test_video_restart_polls_existing_submission(store, monkeypatch):
    job = media_jobs.MediaJob(intent="video.generate", status="running", metadata={"upstream_video_id": "vid_existing"})
    media_jobs.create(job, path=store)
    monkeypatch.setattr(proxy, "_video_media_job_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(proxy, "_load_video_media_job_payload", lambda _: {})
    monkeypatch.setattr(proxy, "_request_shim_from_video_job_payload", lambda _: object())
    # The worker checks its caller's plan before each poll: a caller whose plan allows video.
    monkeypatch.setattr(proxy, "_auth_context_from_image_job_payload", lambda _: access_context())
    submit = AsyncMock(side_effect=AssertionError("must not submit twice"))
    status = AsyncMock(return_value=Response(content='{"status":"failed"}', media_type="application/json"))
    monkeypatch.setattr(proxy, "proxy_graph_video_create", submit)
    monkeypatch.setattr(proxy, "proxy_graph_video_status", status)
    monkeypatch.setattr(proxy, "_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS", 0)
    asyncio.run(proxy._run_video_media_job(job.job_id))
    submit.assert_not_called()
    status.assert_awaited_once()
    assert media_jobs.get(job.job_id, path=store)["status"] == "failed"


@pytest.mark.parametrize("status,enabled", [("queued", True), ("running", True), ("queued", False), ("running", False)])
def test_make_restart_does_not_duplicate_unconfirmed_work(store, status, enabled):
    job = media_jobs.MediaJob(intent="image.generate", status=status, metadata={"mode": me_make.JOB_MODE})
    media_jobs.create(job, path=store)
    runtime = Mock()
    runtime.owns_job.return_value = False
    runtime.load_payload.return_value = {"body": {"prompt": "a picture"}}
    me_make.resume_jobs(runtime=runtime, jobs_path=store, enabled=enabled)
    if enabled and status == "queued":
        runtime.schedule.assert_called_once()
    else:
        assert media_jobs.get(job.job_id, path=store)["status"] == "failed"
        runtime.schedule.assert_not_called()
        runtime.drop_payload.assert_called_once_with(job.job_id)

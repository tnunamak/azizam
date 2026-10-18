"""Video-specific gap regressions; use the hermetic runner.

The ComfyUI calls (status poll, queue and history reads) are served by the FK comfyui fake.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from fastapi import Response
import proxy
from tests.support.access import access_context  # noqa: E402
from gateway import media_jobs
from tests.support.fake_upstream import fake_upstreams, load_scenario, scenario_from_data  # noqa: F401


def response(body):
    return Response(content=json.dumps(body), media_type="application/json")


@pytest.fixture
def video_state(monkeypatch):
    for name in ("_video_job_records", "_video_progress", "_video_workflows", "_video_chain_units"):
        monkeypatch.setattr(proxy, name, {})
    monkeypatch.setattr(proxy, "_video_records_restored", set())
    monkeypatch.setattr(proxy, "_video_progress_lock", asyncio.Lock())
    monkeypatch.setattr(proxy, "_persist_video_record", Mock())
    monkeypatch.setattr(proxy, "COMFY_ENABLED", True)
    monkeypatch.setattr(proxy, "configured_downstream_headers", lambda *a: {})
    monkeypatch.setattr(proxy.prompt_memory, "delete_asset", Mock())


@pytest.fixture
def worker(tmp_path, monkeypatch, video_state):
    db = tmp_path / "jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    monkeypatch.setattr(proxy, "_video_media_job_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(proxy, "_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(proxy, "COMFY_VIDEO_TIMEOUT", 10)
    monkeypatch.setattr(proxy, "_load_video_media_job_payload", lambda _: {})
    monkeypatch.setattr(proxy, "_request_shim_from_video_job_payload", lambda _: object())
    # The worker checks its caller's plan before each poll: a caller whose plan allows video.
    monkeypatch.setattr(proxy, "_auth_context_from_image_job_payload", lambda _: access_context())
    dropped = Mock()
    for name in ("_delete_video_media_job_payload", "_delete_image_media_job_payload"):
        monkeypatch.setattr(proxy, name, dropped)
    submit = AsyncMock(side_effect=AssertionError("duplicate upstream submit"))
    monkeypatch.setattr(proxy, "proxy_comfy_video_create", submit)
    monkeypatch.setattr(proxy, "_media_job_admission_response",
                        Mock(side_effect=AssertionError("re-admitted existing work")))
    stored = Mock(return_value="vid_" + "a" * 64)
    monkeypatch.setattr(proxy, "store_asset_bytes", stored)
    monkeypatch.setattr(proxy, "build_signed_asset_url",
                        lambda *a: "https://gateway.test/v1/assets/vid_fixture/content")
    monkeypatch.setattr(proxy, "asset_media_type", lambda _: "video/webm")
    job = media_jobs.MediaJob(intent="video.generate", status="running",
        metadata={"mode": "async_video_generation", "upstream_video_id": "video_p"})
    media_jobs.create(job, path=db)
    return SimpleNamespace(db=db, job=job, dropped=dropped, submit=submit, stored=stored)


def row(worker):
    return media_jobs.get(worker.job.job_id, audience="internal", path=worker.db)


def test_poll_aliases_are_monotonic_capped_and_complete_once(worker, monkeypatch):
    replies = iter([
        {"status": "in_progress", "progress": 70},
        {"status": "queued", "progress": 10},
        {"status": "running", "progress": 999},
        {"status": "completed", "progress": 100},
    ])
    observed = []
    async def poll(video_id, auth):
        assert video_id == "video_p"
        observed.append(row(worker)["progress"])
        return response(next(replies))
    content = AsyncMock(return_value=Response(
        b"fixture video", media_type="video/webm",
        headers={"Content-Disposition": 'inline; filename="render.webm"'}))
    monkeypatch.setattr(proxy, "proxy_comfy_video_status", poll)
    monkeypatch.setattr(proxy, "proxy_comfy_video_content", content)
    asyncio.run(proxy._run_video_media_job(worker.job.job_id))
    assert observed == [0, 70, 70, 95]
    final = row(worker)
    assert final["status"] == "succeeded"
    assert final["progress"] == 100
    assert len(final["assets"]) == 1
    assert final["assets"][0]["filename"] == "render.webm"
    assert final["assets"][0]["media_type"] == "video/webm"
    worker.submit.assert_not_awaited()
    content.assert_awaited_once_with("video_p", access_context(),
        index_library=False, metadata_mode=proxy.provenance.METADATA_NONE)
    worker.stored.assert_called_once_with(
        b"fixture video", kind="vid", suffix=".webm", owner_key_hash="app:app_test")
    events = media_jobs.events(worker.job.job_id, audience="internal", path=worker.db)
    assert [e["event_type"] for e in events].count("completed") == 1
    worker.dropped.assert_called_once_with(worker.job.job_id)


@pytest.mark.parametrize("mode,expected", [
    ("unavailable", "running"), ("active", "running"), ("succeeded", "succeeded"),
    ("failed", "failed"), ("orphan", "failed"),
])
def test_reconcile_distinguishes_unavailable_active_terminal_and_orphan(
        video_state, monkeypatch, mode, expected):
    active, history = set(), {}
    if mode == "unavailable":
        active, history = None, None
    elif mode == "active":
        active = {"p"}
    elif mode == "succeeded":
        history = {"p": {"status": {"completed": True}}}
    elif mode == "failed":
        history = {"p": {"status": {"completed": True, "status_str": "error"}}}
    monkeypatch.setattr(proxy, "_fetch_comfy_state", AsyncMock(return_value=(active, history)))
    monkeypatch.setattr(proxy, "time", SimpleNamespace(time=lambda: 100))
    monkeypatch.setattr(proxy, "_VIDEO_RECONCILE_RETAIN", 1000)
    proxy._video_progress["p"] = {"status": "running", "percent": 37, "updated_at": 99}
    asyncio.run(proxy._reconcile_video_progress())
    entry = proxy._video_progress["p"]
    assert entry["status"] == expected
    if mode in {"unavailable", "active"}:
        assert entry == {"status": "running", "percent": 37, "updated_at": 99}
    else:
        assert entry["ended_at"] == 100
        if mode == "succeeded":
            assert entry["percent"] == 100
        else:
            assert entry["error"]
            assert ("orphaned" in entry["error"]) == (mode == "orphan")


def test_status_fallback_never_regresses_and_terminal_error_wins(video_state, monkeypatch, fake_upstreams):
    fake = fake_upstreams.asgi(load_scenario("comfyui/video-status-progression"))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
    proxy._video_job_records["p"] = {
        "owner_key_hash": None, "deleted_at": None, "created_at": 1,
        "completed_at": None, "model": "sora-2", "size": "960x512", "seconds": "5"}
    proxy._video_progress["p"] = {"status": "running", "percent": 80, "updated_at": 2}
    async def poll():
        result = await proxy.proxy_comfy_video_status("video_p", proxy.NO_AUTH_CONTEXT)
        assert result.status_code == 200
        return json.loads(result.body)
    async def scenario():
        first = await poll()
        assert (first["status"], first["progress"]) == ("in_progress", 80)
        proxy._video_progress["p"]["status"] = "unknown"
        second = await poll()
        assert (second["status"], second["progress"]) == ("in_progress", 80)
        final = await poll()
        assert (final["status"], final["progress"]) == ("failed", 100)
        assert final["error"]
        assert "private upstream sentinel" not in json.dumps(final)
        assert proxy._video_job_records["p"]["completed_at"] is not None
    asyncio.run(asyncio.wait_for(scenario(), timeout=10))
    assert [(r.method, r.path) for r in fake.requests] == [
        ("GET", "/history/p"), ("GET", "/history/p"), ("GET", "/queue"), ("GET", "/history/p")]
    proxy._persist_video_record.assert_called_with("p")


def test_cancel_during_content_fetch_does_not_store_unwanted_asset(worker, monkeypatch):
    monkeypatch.setattr(proxy, "proxy_comfy_video_status",
                        AsyncMock(return_value=response({"status": "completed", "progress": 100})))
    async def content(*args, **kwargs):
        pending = media_jobs.request_cancel(worker.job.job_id, path=worker.db)
        assert pending["status"] == "running"
        assert proxy._video_media_job_semaphore.locked()
        return Response(b"fixture video", media_type="video/webm")
    monkeypatch.setattr(proxy, "proxy_comfy_video_content", content)
    asyncio.run(proxy._run_video_media_job(worker.job.job_id))
    assert row(worker)["status"] == "canceled"
    assert row(worker)["assets"] == []
    assert not proxy._video_media_job_semaphore.locked()
    worker.dropped.assert_called_with(worker.job.job_id)
    worker.stored.assert_not_called()


def _comfy_state_fake(fake_upstreams, monkeypatch, *, queue=None, history=None):
    """Serve ComfyUI's /queue and /history; each is a response dict (default: a healthy empty one)."""
    healthy_queue = {"json": {"queue_running": [], "queue_pending": []}}
    healthy_history = {"json": {}}
    fake = fake_upstreams.asgi(scenario_from_data({
        "protocol": "comfyui",
        "name": "comfy-state",
        "provenance": {
            "source": "hand-written",
            "reference": "ComfyUI server routes GET /queue ({queue_running, queue_pending}) and "
                         "GET /history (a JSON object keyed by prompt id) (ComfyUI server.py)",
        },
        "exchanges": [
            {"id": "queue", "repeat": True, "match": {"method": "GET", "path": "/queue"}, "response": queue or healthy_queue},
            {"id": "history", "repeat": True, "match": {"method": "GET", "path": "/history"}, "response": history or healthy_history},
        ],
    }))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
    return fake


@pytest.mark.parametrize("bad_endpoint", ["queue", "history"])
def test_malformed_provider_json_cannot_orphan_live_video(video_state, monkeypatch, fake_upstreams, bad_endpoint):
    malformed = {"headers": {"content-type": "application/json"}, "body": [{"text": "{not json"}]}
    _comfy_state_fake(fake_upstreams, monkeypatch, **{bad_endpoint: malformed})
    monkeypatch.setattr(proxy, "time", SimpleNamespace(time=lambda: 100))
    proxy._video_progress["p"] = {"status": "running", "percent": 37, "updated_at": 99}
    async def scenario():
        assert await proxy._fetch_comfy_state() == (None, None)
        await proxy._reconcile_video_progress()
    asyncio.run(scenario())
    assert proxy._video_progress["p"] == {"status": "running", "percent": 37, "updated_at": 99}


@pytest.mark.parametrize("prompt", [123, True, ["unexpected"], {"text": "unexpected"}])
def test_direct_create_rejects_non_string_prompt_before_provider(video_state, monkeypatch, fake_upstreams, prompt):
    # An optional exchange: the fake records any call and latches it as a violation.
    fake = fake_upstreams.asgi(scenario_from_data({
        "protocol": "comfyui",
        "name": "comfy-untouched",
        "provenance": {
            "source": "hand-written",
            "reference": "ComfyUI server route POST /prompt (ComfyUI server.py); the test expects no call",
        },
        "exchanges": [{
            "id": "submit", "required": False,
            "match": {"method": "POST", "path": "/prompt"},
            "response": {"json": {"prompt_id": "p"}},
        }],
    }))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
    monkeypatch.setattr(proxy, "_dispatch_admission_response", lambda *a, **kw: None)
    monkeypatch.setattr(proxy, "_service_availability_response", lambda *a, **kw: None)
    monkeypatch.setattr(proxy, "_begin_media_delivery", lambda *a: None)
    monkeypatch.setattr(proxy, "_note_media_metadata_body", lambda *a: None)
    result = asyncio.run(proxy.proxy_comfy_video_create(
        SimpleNamespace(headers={}), proxy.NO_AUTH_CONTEXT,
        body_override={"prompt": prompt, "model": "sora-2"}))
    assert result.status_code == 400
    assert json.loads(result.body)["error"]["type"] == "invalid_request_error"
    assert proxy._video_job_records == {}
    assert fake.requests == [], "invalid input contacted the provider"


@pytest.mark.parametrize("bad_endpoint", ["queue", "history"])
@pytest.mark.parametrize("shape", [[], None, "text"])
def test_wrong_shape_provider_json_is_unavailable(video_state, monkeypatch, fake_upstreams, bad_endpoint, shape):
    _comfy_state_fake(fake_upstreams, monkeypatch, **{bad_endpoint: {"json": shape}})
    assert asyncio.run(proxy._fetch_comfy_state()) == (None, None)

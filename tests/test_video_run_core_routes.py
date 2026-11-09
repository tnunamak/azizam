"""The video list and status routes over jobs of the run core, through the real routes (V5b recheck, 2026-11-09).

The gateway, the job store and the access rules are real; jobs are stored the way the run core stores them (a row filed under
the wire's own video intent, the request's facts and the result metadata in its metadata). Only the engine's HTTP boundary is
fake, and these routes never call it for a job of the run core."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

import proxy
from gateway import media_jobs
from gateway.workflow_run import REQUEST_FACTS_KEY, RESULT_METADATA_KEY, request_facts_record
from tests.support.access import authenticated, identity
from tests.support.adapter_differential_rig import GatewayRig, history_done
from tests.support.fake_upstream import HandlerTransport
from tests.support.s7_parity_images_rig import mount_rows
from tests.test_s7_parity_video_lifecycle import ALICE as PLAIN_ALICE, HOST, PLAN, Engine

ALICE = identity(PLAN, name=PLAIN_ALICE.name, app_id=PLAIN_ALICE.app_id, key_hash=PLAIN_ALICE.key_hash, person_id="usr_alice_video")

LIST_KEYS = {"id", "object", "model", "size", "seconds", "created_at", "completed_at", "prompt", "status", "progress"}


def seed(rig: GatewayRig, status: str, *, created_at: int, facts: dict[str, Any] | None, result: dict[str, Any] | None = None,
         progress: int = 0) -> str:
    """One stored job of the run core, as the run core would leave it in ``status``."""
    metadata: dict[str, Any] = {"mode": "workflow.run"}
    if facts is not None:
        metadata[REQUEST_FACTS_KEY] = request_facts_record(facts)
    if result is not None:
        metadata[RESULT_METADATA_KEY] = result
    path = Path(rig.media_jobs_db)
    job = media_jobs.create(media_jobs.MediaJob(
        intent=proxy._video_intents()[0], workflow="video.demo", created_at=created_at, updated_at=created_at, metadata=metadata,
        app_id=ALICE.app_id, owner_key_hash=ALICE.key_hash, maker_person_id=ALICE.person_id), path=path, maker_from_request=False)
    if status == "running":
        media_jobs.update_status(job.job_id, "running", progress=progress, now=created_at + 5, path=path)
    elif status == "succeeded":
        media_jobs.update_status(job.job_id, "running", now=created_at + 5, path=path)
        media_jobs.update_status(job.job_id, "succeeded", now=created_at + 60, path=path)
    elif status == "failed":
        media_jobs.update_status(job.job_id, "running", now=created_at + 5, path=path)
        media_jobs.update_status(job.job_id, "failed", error="engine fell over", now=created_at + 30, path=path, error_info={
            "error_code": "upstream_failed", "error_category": "upstream", "retryable": False, "upstream_http_status": 502})
    return job.job_id


def serve(tmp_path: Path, exercise) -> None:
    with GatewayRig(exercise.__name__, tmp_path) as rig:
        engine = Engine()
        rig.fakes.router.add_transport(HOST, HandlerTransport(engine.handle))
        rig.set(proxy, "COMFY_HOST", f"http://{HOST}")
        rig.monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), "comfy": {
            "enabled": True, "host": HOST, "port": 80, "protocol": "node-graph"}})

        async def call(method: str, path: str, **kwargs):
            with authenticated(ALICE):
                return await rig.call(method, path, **kwargs)

        asyncio.run(exercise(rig, call))
        assert engine.unexpected == [], engine.unexpected


def test_the_list_shows_a_run_core_video_as_the_list_has_always_shown_a_video(tmp_path):
    async def exercise(rig, call):
        facts = {"model": "demo-model", "size": "640x360", "seconds": "4", "resolved_model": "demo-model-v2"}
        done = seed(rig, "succeeded", created_at=1_800_000_100, facts=facts)
        running = seed(rig, "running", created_at=1_800_000_200, facts=facts, progress=40)
        failed = seed(rig, "failed", created_at=1_800_000_300, facts=facts)
        response = await call("GET", "/v1/videos", params={"order": "asc"})
        assert response.status_code == 200, response.text
        items = {item["id"]: item for item in response.json()["data"]}
        assert set(items) == {f"video_{job}" for job in (done, running, failed)}
        assert items[f"video_{done}"]["status"] == "succeeded" and items[f"video_{done}"]["progress"] == 100
        assert items[f"video_{done}"]["completed_at"] == 1_800_000_160
        assert items[f"video_{running}"]["status"] == "running" and items[f"video_{running}"]["progress"] == 40
        assert items[f"video_{running}"]["completed_at"] is None
        assert items[f"video_{failed}"]["status"] == "failed" and items[f"video_{failed}"]["error"] == "engine fell over"
        for item in items.values():
            assert item["model"] == "demo-model" and item["size"] == "640x360" and item["seconds"] == "4"
            assert set(item) - {"error"} == LIST_KEYS, sorted(set(item) ^ LIST_KEYS)
    serve(tmp_path, exercise)


def test_a_run_job_from_before_facts_were_recorded_keeps_its_model_size_and_seconds_on_status(tmp_path):
    async def exercise(rig, call):
        old = {"model": "demo-model", "size": "640x360", "seconds": "4"}
        job = seed(rig, "succeeded", created_at=1_800_000_100, facts=None, result=old)
        response = await call("GET", f"/v1/videos/video_{job}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert {key: body.get(key) for key in old} == old
        assert body["status"] == "completed" and body["azizam_status"] == "succeeded"
    serve(tmp_path, exercise)


def test_an_empty_record_of_facts_is_a_record_and_takes_nothing_from_the_result_metadata_on_status(tmp_path):
    async def exercise(rig, call):
        job = seed(rig, "succeeded", created_at=1_800_000_100, facts={}, result={"model": "engine-model", "size": "640x360"})
        body = (await call("GET", f"/v1/videos/video_{job}")).json()
        assert "model" not in body and "size" not in body and body["azizam_status"] == "succeeded"
    serve(tmp_path, exercise)


def test_recorded_facts_stand_alone_and_are_not_mixed_with_the_result_metadata_on_status(tmp_path):
    async def exercise(rig, call):
        job = seed(rig, "succeeded", created_at=1_800_000_100, facts={"model": "asked-model"},
                   result={"model": "engine-model", "size": "640x360"})
        body = (await call("GET", f"/v1/videos/video_{job}")).json()
        assert body["model"] == "asked-model" and "size" not in body
    serve(tmp_path, exercise)


class FinishingEngine(Engine):
    """The engine of the lifecycle tests, but a prompt it accepts is finished at once, with a file of the name it is given."""

    output_name = "clip.mp4"

    async def handle(self, request):
        if request.method == "GET" and request.url.path.startswith("/history/") and self.states.get(request.url.path.rsplit("/", 1)[-1]) == "completed":
            await request.aread()
            pid = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=history_done("images", self.output_name, prompt_id=pid, node="60"))
        response = await super().handle(request)
        if request.method == "POST" and request.url.path == "/prompt":
            self.states[response.json()["prompt_id"]] = "completed"
        return response


def download(tmp_path, output_name: str):
    """Make a video through the real runner over an engine that names its file ``output_name``, and download it."""
    with GatewayRig("filename-from-runner", tmp_path) as rig:
        engine = FinishingEngine()
        engine.output_name = output_name
        rig.fakes.router.add_transport(HOST, HandlerTransport(engine.handle))
        rig.set(proxy, "COMFY_HOST", f"http://{HOST}")
        rig.monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), "comfy": {
            "enabled": True, "host": HOST, "port": 80, "protocol": "node-graph"}})
        mount_rows(rig)

        async def exercise():
            with authenticated(PLAIN_ALICE):
                made = await rig.call("POST", "/__row/v1/videos", json_body={"prompt": "waves at dusk", "model": "ltx-2.5-t2v"})
                assert made.status_code == 200, made.text
                video_id = made.json()["id"]
                for _ in range(200):
                    status = await rig.call("GET", f"/v1/videos/{video_id}")
                    if status.json().get("azizam_status") in ("succeeded", "failed"):
                        break
                    await asyncio.sleep(0.05)
                assert status.json()["azizam_status"] == "succeeded", status.text
                return await rig.call("GET", f"/v1/videos/{video_id}/content", params={"metadata": "none"})

        return asyncio.run(exercise())


def test_a_video_the_runner_made_is_delivered_under_the_name_the_engine_gave_its_file(tmp_path):
    content = download(tmp_path, "clip.mp4")
    assert content.status_code == 200, content.text
    assert content.headers["content-disposition"] == 'inline; filename="clip.mp4"'
    assert content.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize(("output_name", "fallback", "encoded"), [
    ("動画.mp4", "__.mp4", "%E5%8B%95%E7%94%BB.mp4"),
    ('clip"; filename="second.mp4', "clip_; filename=_second.mp4", "clip%22%3B%20filename%3D%22second.mp4"),
    ("a\r\nX-Evil: 1.mp4", "aX-Evil: 1.mp4", None),
], ids=["non-ascii", "quote", "crlf"])
def test_a_file_name_the_engine_gave_never_breaks_the_download_header(tmp_path, output_name, fallback, encoded):
    content = download(tmp_path, output_name)
    assert content.status_code == 200, content.text
    star = f"; filename*=UTF-8''{encoded}" if encoded else ""
    assert content.headers.get_list("content-disposition") == [f'inline; filename="{fallback}"{star}']
    assert "x-evil" not in content.headers and content.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize("output_name", [".\t.", ".\x7f.", "\t\x01", "\x00.. "], ids=["tab-between-dots", "del-between-dots", "controls-only", "dots-after-cleaning"])
def test_a_file_name_that_cleans_to_nothing_usable_is_given_a_safe_name_for_its_media_type(tmp_path, output_name):
    content = download(tmp_path, output_name)
    assert content.status_code == 200, content.text
    assert content.headers.get_list("content-disposition") == ['inline; filename="video.mp4"']
    assert content.headers["content-type"] == "video/mp4"

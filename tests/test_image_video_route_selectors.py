"""The image and video routes read their result through the workflow's selectors (code-to-data S2.5b, D5 and D6).

The rig is the adapter-differential one: the real app and a fake engine. The engine's answer holds a temp preview and an
echo of an input ahead of the result, which the old collection could take first. The workflows' selectors name the node
and key of the file that is the result.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import proxy
from gateway import media_jobs, output_selectors, result_bundle
from tests.media_test_bytes import mp4_bytes
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, scenario
from tests.support.config_snapshot import publish_section
from tests.test_adapter_differential import _png

COMFY_HOST = "comfy.fk.test"
IMAGE_BODY = {"prompt": "a red fox", "response_format": "b64_json", "background": "opaque"}
VIDEO_BODY = {"prompt": "waves at dusk", "model": "ltx-2.5-t2v"}


def entry(name: str, kind: str) -> dict[str, str]:
    return {"filename": name, "subfolder": "", "type": kind}


def history(outputs: dict[str, Any]) -> dict[str, Any]:
    return {"p1": {"status": {"status_str": "success", "completed": True, "messages": []}, "outputs": outputs}}


# The save node of the image workflow is 9 and of the video workflow 60. The files before them are what an engine's
# answer holds besides the result: a preview in the temp directory, an echo of an input and a saved file of a side branch.
NOISY_IMAGE = history({
    "0": {"images": [entry("peek.png", "temp")]},
    "1": {"images": [entry("echo_input.png", "input")]},
    "3": {"images": [entry("side_00001_.png", "output")]},
    "9": {"images": [entry("fox_00001_.png", "output")]},
})
NOISY_VIDEO = history({
    "0": {"images": [entry("peek.png", "temp")]},
    "1": {"images": [entry("echo_input.mp4", "input")]},
    "3": {"images": [entry("side_00001_.mp4", "output")]},
    "60": {"images": [entry("clip_00001_.mp4", "output")]},
})


def build(tmp_path: Path, answer: dict[str, Any], view: tuple[bytes, str], **settings: Any):
    rig = GatewayRig("image-video-route-selectors", tmp_path)
    made = rig.__enter__()
    fake = made.engine("comfy", scenario("comfyui", "image-video-route-selectors", comfy_exchanges(
        history=answer, views=[view], strict=False)), host=COMFY_HOST)
    made.set(proxy, "COMFY_HOST", fake.base_url)
    made.set(proxy, "_schedule_image_media_job", lambda job_id: made.scheduled.append(job_id) or True)
    made.set(proxy, "_schedule_video_media_job", lambda job_id: made.scheduled.append(job_id) or True)
    made.set(proxy, "_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS", 0)
    if settings:
        publish_section(made.monkeypatch, "workflow_execution", settings)
    return rig, made


def viewed(made: GatewayRig) -> list[list[list[str]]]:
    return [item["query"] for item in made.recorder.engine if item["path"] == "/view"]


def run(tmp_path: Path, answer: dict[str, Any], view: tuple[bytes, str], drive, **settings: Any):
    rig, made = build(tmp_path, answer, view, **settings)
    try:
        return asyncio.run(drive(made)), viewed(made)
    finally:
        rig.__exit__(None, None, None)


def generate_image(made: GatewayRig):
    return made.call("POST", "/v1/images/generations", label="generate", json_body=IMAGE_BODY)


async def fetch_video(made: GatewayRig):
    created = await made.call("POST", "/v1/videos", label="create", json_body=VIDEO_BODY)
    return await made.call("GET", f"/v1/videos/{created.json()['id']}/content", label="content")


PNG = (_png(), "image/png")


def test_the_image_route_takes_the_file_the_selector_names_not_an_earlier_preview_or_echo(tmp_path: Path):
    response, views = run(tmp_path, NOISY_IMAGE, PNG, generate_image)
    assert response.status_code == 200, response.text
    assert views == [[["filename", "fox_00001_.png"], ["type", "output"]]]


def test_the_video_content_route_takes_the_file_the_selector_names(tmp_path: Path):
    response, views = run(tmp_path, NOISY_VIDEO, (mp4_bytes(), "video/mp4"), fetch_video)
    assert response.status_code == 200, response.text
    assert views == [[["filename", "clip_00001_.mp4"], ["type", "output"]]]


def test_an_image_answer_over_the_history_limit_is_refused_and_no_file_is_fetched(tmp_path: Path):
    response, views = run(tmp_path, NOISY_IMAGE, PNG, generate_image, result_limits={"max_history_bytes": 50})
    assert response.status_code == 502
    assert json.loads(response.text)["error"]["code"] == "history_too_large"
    assert views == []


def test_a_video_answer_over_the_history_limit_is_refused_and_no_file_is_fetched(tmp_path: Path):
    response, views = run(tmp_path, NOISY_VIDEO, (mp4_bytes(), "video/mp4"), fetch_video,
                          result_limits={"max_history_bytes": 50})
    assert response.status_code == 502
    assert json.loads(response.text)["error"]["code"] == "history_too_large"
    assert views == []


def test_an_image_file_over_the_asset_limit_is_refused_while_it_is_read(tmp_path: Path):
    response, _ = run(tmp_path, NOISY_IMAGE, PNG, generate_image, result_limits={"max_asset_bytes": 10})
    assert response.status_code == 502
    assert json.loads(response.text)["error"]["code"] == "asset_too_large"


def holding_the_only_fetch_slot(call):
    async def drive(made: GatewayRig):
        limits = result_bundle.ResultLimits(max_concurrent_fetches=1, fetch_timeout_s=30)

        # The rig shortens sleeps, so the slot is held on an event nothing sets, not on a sleep.
        async def hold():
            async with output_selectors.FETCH_GATE.slot(limits):
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await asyncio.sleep(0)
        try:
            return await call(made)
        finally:
            task.cancel()
    return drive


def test_the_image_fetch_waits_in_the_fetch_gate_and_is_refused_past_the_time_limit(tmp_path: Path):
    limits = {"result_limits": {"max_concurrent_fetches": 1, "fetch_timeout_s": 1}}
    response, views = run(tmp_path, NOISY_IMAGE, PNG, holding_the_only_fetch_slot(generate_image), **limits)
    assert response.status_code == 504
    assert json.loads(response.text)["error"]["code"] == "fetch_timeout"
    assert views == []


def test_the_video_fetch_waits_in_the_fetch_gate_and_is_refused_past_the_time_limit(tmp_path: Path):
    limits = {"result_limits": {"max_concurrent_fetches": 1, "fetch_timeout_s": 1}}
    response, views = run(tmp_path, NOISY_VIDEO, (mp4_bytes(), "video/mp4"), holding_the_only_fetch_slot(fetch_video),
                          **limits)
    assert response.status_code == 504
    assert views == []


def job_result_names(made: GatewayRig, job_id: str) -> list[str]:
    done = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
    assert done["status"] == media_jobs.STATUS_SUCCEEDED, done.get("error")
    return [result["name"] for result in result_bundle.bundle_summary(job_id, path=media_jobs.DB_PATH)["results"]]


def test_an_image_job_names_its_bundle_result_for_the_declared_output_and_lists_it_in_the_view(tmp_path: Path):
    async def drive(made: GatewayRig):
        submit = await made.call("POST", "/gateway/v1/media/jobs/image-generations", label="submit", unordered_json=True,
                                 json_body={"prompt": "a red fox", "response_format": "url", "background": "opaque"})
        job_id = submit.json()["job_id"]
        await proxy._run_image_media_job(made.scheduled[0])
        view = await made.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="view")
        return job_result_names(made, job_id), view.json()

    (names, view), views = run(tmp_path, NOISY_IMAGE, PNG, drive)
    assert names == ["image"]
    assert view.get("results") == {"image": {"kind": "assets", "count": 1}}, sorted(view)
    assert views == [[["filename", "fox_00001_.png"], ["type", "output"]]]


def test_a_video_job_names_its_bundle_result_for_the_declared_output_and_lists_it_in_the_view(tmp_path: Path):
    async def drive(made: GatewayRig):
        submit = await made.call("POST", "/gateway/v1/media/jobs", label="submit", unordered_json=True,
                                 json_body={"intent": "video.generate", "request": VIDEO_BODY})
        job_id = submit.json()["job_id"]
        await proxy._run_video_media_job(made.scheduled[0])
        view = await made.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="view")
        return job_result_names(made, job_id), view.json()

    (names, view), views = run(tmp_path, NOISY_VIDEO, (mp4_bytes(), "video/mp4"), drive)
    assert names == ["video"]
    assert view["results"] == {"video": {"kind": "assets", "count": 1}}
    assert views == [[["filename", "clip_00001_.mp4"], ["type", "output"]]]

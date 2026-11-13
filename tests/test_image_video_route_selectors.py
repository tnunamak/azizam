"""The image and video routes read their result through the workflow's selectors (code-to-data S2.5b, D5 and D6).

The rig is the adapter-differential one: the real app and a fake engine. The engine's answer holds a temp preview and an
echo of an input ahead of the result, which a reading by key alone would take first. The workflows' selectors name the
node and key of the file that is the result.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

import proxy
from gateway import media_jobs, output_selectors, result_bundle
from tests.media_test_bytes import mp4_bytes
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, scenario
from tests.support.config_snapshot import publish_section
from tests.support.video_rig import Engine, World, drive, install_runner_clock, run as run_video, settled
from tests.support.video_row_engine import edited_workflows
from tests.test_adapter_differential import _png

GRAPH_HOST = "comfy.fk.test"
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
        history=answer, views=[view], strict=False)), host=GRAPH_HOST)
    made.set(proxy, "GRAPH_HOST", fake.base_url)
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


PNG = (_png(), "image/png")


def test_the_image_route_takes_the_file_the_selector_names_not_an_earlier_preview_or_echo(tmp_path: Path):
    response, views = run(tmp_path, NOISY_IMAGE, PNG, generate_image)
    assert response.status_code == 200, response.text
    assert views == [[["filename", "fox_00001_.png"], ["type", "output"]]]


def test_an_image_answer_over_the_history_limit_is_refused_and_no_file_is_fetched(tmp_path: Path):
    response, views = run(tmp_path, NOISY_IMAGE, PNG, generate_image, result_limits={"max_history_bytes": 50})
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


# ------------------------------------------------------------------------------------------- the identity scene

# The identity graph saves its result at node 10. Before it, the answer holds what a key alone cannot tell
# from a result: a preview, an echo of a reference photo, and a file that a side branch saved.
NOISY_IDENTITY = history({
    "0": {"images": [entry("peek.png", "temp")]},
    "L1": {"images": [entry("reference.png", "input")]},
    "3": {"images": [entry("side_00001_.png", "output")]},
    "10": {"images": [entry("scene_00001_.png", "output")]},
})
PLAIN_IDENTITY = history({"10": {"images": [entry("scene_00001_.png", "output")]}})


def run_identity(made: GatewayRig):
    graph, _ = proxy._identity_template()
    return proxy._identity_run_workflow(proxy.NO_AUTH_CONTEXT, json.loads(json.dumps(graph)), {})


def test_the_identity_scene_takes_the_file_its_selector_names_not_an_earlier_preview_echo_or_side_file(tmp_path: Path):
    image, views = run(tmp_path, NOISY_IDENTITY, PNG, run_identity)
    assert image == _png()
    assert views == [[["filename", "scene_00001_.png"], ["type", "output"]]]


def test_the_identity_scene_takes_the_file_its_selector_names_from_a_plain_answer(tmp_path: Path):
    image, views = run(tmp_path, PLAIN_IDENTITY, PNG, run_identity)
    assert image == _png()
    assert views == [[["filename", "scene_00001_.png"], ["type", "output"]]]


# ----------------------------------------------------------------------------------- a workflow with no selectors

def without_selectors(call):
    """A workflow with no manifest has no selectors, so there is nothing to read its answer by."""
    async def drive(made: GatewayRig):
        made.set(proxy, "_video_manifest_of_job", lambda _prompt_id: None)
        made.set(proxy, "_graph_result_source", lambda *_args, **_kwargs: {})
        return await call(made)
    return drive


MP4 = (mp4_bytes(), "video/mp4")


def test_an_image_workflow_with_no_selectors_is_refused_with_its_reason_and_nothing_is_fetched(tmp_path: Path):
    response, views = run(tmp_path, NOISY_IMAGE, PNG, without_selectors(generate_image))
    assert response.status_code == 502
    assert json.loads(response.text)["error"]["code"] == "selectors_missing"
    assert views == []


# ----------------------------------------------------------------------------------------- the video create route

# A video create is a run-core job: the runner reads the engine's answer by the workflow's selectors and keeps the file, and
# the content route serves what the job kept. The engine is the video parity rig's, driven to its state through ``drive``.
NOISY_VIDEO_OUTPUTS = NOISY_VIDEO["p1"]["outputs"]


def video_views(engine: Engine) -> list[list[list[str]]]:
    return [[list(pair) for pair in sorted(request.url.params.multi_items())] for request in engine.calls("GET", "/view")]


async def refused_video(world: World, **settings: Any):
    """Create a video over the noisy answer and let the engine finish it. The runner reads the answer by the selectors; a
    refusal is the job's failure, which the status route shows. Returns that status body and the views the engine served."""
    if settings:
        publish_section(world.rig.monkeypatch, "workflow_execution", settings)
    world.engine.outputs = NOISY_VIDEO_OUTPUTS
    video_id = await world.create(prompt=VIDEO_BODY["prompt"])
    await drive(world, video_id, "completed", wait=False)
    await settled(world, video_id)
    status = await world.status(video_id)
    assert status.status_code == 200, status.text
    content = await world.content(video_id)
    return {**status.json(), "content": content}, video_views(world.engine)


def refusal_of(status, code: str, http_status: int) -> None:
    """The status names the wire's own failed word; the content download answers the refusal with its status and code, as the old route."""
    assert status["status"] == "failed" and status["error"]["code"] == "video_generation_failed"
    content = status["content"]
    assert content.status_code == http_status, content.text
    assert json.loads(content.text)["error"]["code"] == code


def test_the_video_content_route_takes_the_file_the_selector_names(tmp_path: Path):
    async def exercise(world: World):
        world.engine.outputs = NOISY_VIDEO_OUTPUTS
        video_id = await world.create(prompt=VIDEO_BODY["prompt"])
        await drive(world, video_id, "completed")
        response = await world.content(video_id)
        assert response.status_code == 200, response.text
        assert video_views(world.engine) == [[["filename", "clip_00001_.mp4"], ["type", "output"]]]
    run_video(tmp_path, exercise)


def test_a_video_answer_over_the_history_limit_is_refused_and_no_file_is_fetched(tmp_path: Path):
    async def exercise(world: World):
        status, views = await refused_video(world, result_limits={"max_history_bytes": 50})
        refusal_of(status, "history_too_large", 502)
        assert views == []
    run_video(tmp_path, exercise)


def test_the_video_fetch_waits_in_the_fetch_gate_and_is_refused_past_the_time_limit(tmp_path: Path):
    async def exercise(world: World):
        limits = result_bundle.ResultLimits(max_concurrent_fetches=1, fetch_timeout_s=30)

        async def hold():       # the rig shortens sleeps, so the slot is held on an event nothing sets, not on a sleep
            async with output_selectors.FETCH_GATE.slot(limits):
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await asyncio.sleep(0)
        try:
            status, views = await refused_video(world, result_limits={"max_concurrent_fetches": 1, "fetch_timeout_s": 1})
        finally:
            task.cancel()
        refusal_of(status, "fetch_timeout", 504)
        assert views == []
    run_video(tmp_path, exercise)


def test_a_video_workflow_with_no_selectors_is_refused_with_its_reason_and_nothing_is_fetched(tmp_path: Path):
    def without_selectors(entry):
        return dataclasses.replace(entry, bindings={key: dataclasses.replace(
            binding, binding={name: value for name, value in binding.binding.items() if name != "selectors"})
            for key, binding in entry.bindings.items()})

    async def exercise(world: World):
        with edited_workflows(without_selectors):
            install_runner_clock(world)
            status, views = await refused_video(world)
        refusal_of(status, "selectors_missing", 502)
        assert views == []
    run_video(tmp_path, exercise)


def test_a_video_file_over_the_asset_limit_is_refused_while_it_is_read(tmp_path: Path):
    async def exercise(world: World):
        status, _ = await refused_video(world, result_limits={"max_asset_bytes": 10})
        refusal_of(status, "asset_too_large", 502)
    run_video(tmp_path, exercise)

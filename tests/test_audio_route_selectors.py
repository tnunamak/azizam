"""The audio route reads its result through the workflow's selectors (code-to-data S2.4, D5 and D6).

The rig is the adapter-differential one: the real app and a fake engine. The engine's answer holds a temp preview and an
echo of an input ahead of the result, which the old collection took first.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import proxy
from gateway import media_jobs, output_selectors, result_bundle
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, scenario
from tests.support.config_snapshot import publish_section
from tests.test_adapter_differential import _wav

COMFY_HOST = "comfy.fk.test"
BODY = {"prompt": "a door creaks", "model": "tangoflux", "duration": 4}


def entry(name: str, kind: str) -> dict[str, str]:
    return {"filename": name, "subfolder": "", "type": kind}


def history(outputs: dict[str, Any]) -> dict[str, Any]:
    return {"p1": {"status": {"status_str": "success", "completed": True, "messages": []}, "outputs": outputs}}


NOISY = history({
    "0": {"audio": [entry("peek.flac", "temp")]},
    "1": {"audios": [entry("echo_input.wav", "input")]},
    "3": {"audios": [entry("tone_00001_.wav", "output")]},
})


def build(tmp_path: Path, answer: dict[str, Any], **settings: Any):
    rig = GatewayRig("audio-route-selectors", tmp_path)
    made = rig.__enter__()
    fake = made.engine("comfy", scenario("comfyui", "audio-route-selectors", comfy_exchanges(
        history=answer, views=[(_wav(), "audio/wav")], strict=False)), host=COMFY_HOST)
    made.set(proxy, "COMFY_HOST", fake.base_url)
    if settings:
        publish_section(made.monkeypatch, "workflow_execution", settings)
    return rig, made


def viewed(made: GatewayRig) -> list[list[list[str]]]:
    return [item["query"] for item in made.recorder.engine if item["path"] == "/view"]


def generate(tmp_path: Path, answer: dict[str, Any], **settings: Any) -> tuple[Any, list[list[list[str]]]]:
    rig, made = build(tmp_path, answer, **settings)
    try:
        response = asyncio.run(made.call("POST", "/v1/audio/generate", label="generate", json_body=BODY))
        return response, viewed(made)
    finally:
        rig.__exit__(None, None, None)


def test_the_route_takes_the_file_the_selector_names_not_an_earlier_preview_or_echo(tmp_path: Path):
    response, views = generate(tmp_path, NOISY)
    assert response.status_code == 200, response.text
    assert views == [[["filename", "tone_00001_.wav"], ["type", "output"]]]


def test_an_answer_with_no_file_for_the_selector_is_the_old_no_output_refusal(tmp_path: Path):
    only_noise = history({"0": {"audio": [entry("peek.flac", "temp")]}})
    response, views = generate(tmp_path, only_noise)
    assert response.status_code == 502 and "No audio output found" in response.text
    assert views == [], "a preview is never fetched in place of the result"


def test_an_engine_answer_over_the_history_limit_is_refused_and_no_file_is_fetched(tmp_path: Path):
    response, views = generate(tmp_path, NOISY, result_limits={"max_history_bytes": 50})
    assert response.status_code == 502
    assert json.loads(response.text)["error"]["code"] == "history_too_large"
    assert views == []


def test_a_fetch_that_waits_past_the_time_limit_is_refused(tmp_path: Path):
    rig, made = build(tmp_path, NOISY, result_limits={"max_concurrent_fetches": 1, "fetch_timeout_s": 1})

    async def go():
        limits = result_bundle.ResultLimits(max_concurrent_fetches=1, fetch_timeout_s=30)

        # The rig shortens sleeps, so the slot is held on an event nothing sets, not on a sleep.
        async def hold():
            async with output_selectors.FETCH_GATE.slot(limits):
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await asyncio.sleep(0)
        try:
            return await made.call("POST", "/v1/audio/generate", label="generate", json_body=BODY)
        finally:
            task.cancel()

    try:
        response = asyncio.run(go())
    finally:
        rig.__exit__(None, None, None)
    assert response.status_code == 504
    assert json.loads(response.text)["error"]["code"] == "fetch_timeout"
    assert viewed(made) == []


def test_the_job_names_its_bundle_result_for_the_declared_output(tmp_path: Path):
    rig, made = build(tmp_path, NOISY)
    made.set(proxy, "_schedule_audio_media_job", lambda job_id: made.scheduled.append(job_id) or True)
    try:
        async def go() -> str:
            submit = await made.call("POST", "/gateway/v1/media/jobs", label="submit", unordered_json=True,
                                     json_body={"intent": "audio.generate", "request": BODY})
            await proxy._run_audio_media_job(submit.json()["job_id"])
            return submit.json()["job_id"]

        job_id = asyncio.run(go())
        done = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
        assert done["status"] == media_jobs.STATUS_SUCCEEDED, done.get("error")
        summary = result_bundle.bundle_summary(job_id, path=media_jobs.DB_PATH)
        assert [result["name"] for result in summary["results"]] == ["audio"]
    finally:
        rig.__exit__(None, None, None)

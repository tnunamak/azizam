"""The Pixal3D ComfyUI service against scenario fakes of ComfyUI (tests/support/fake_upstream), served in process."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from engines import pixal3d_comfy_service as svc
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture

GLB = b"glTF\x02\x00\x00\x00fake-model-bytes"
PROVENANCE = {"source": "hand-written", "reference": "ComfyUI server routes GET /queue, POST /upload/image, POST /prompt, GET /history/{id}, GET /view"}
IDLE = {"queue_running": [], "queue_pending": []}
DONE = {"p1": {"status": {"status_str": "success", "completed": True}, "outputs": {
    svc.OUTPUT_NODE: {"3d": [{"filename": "model.glb", "subfolder": "pixal3d", "type": "output"}]},
    "323": {"3d": [{"filename": "other.glb", "subfolder": "", "type": "output"}]}}}}


def exchange(id_: str, method: str, path: str, response: dict, **extra) -> dict:
    return {"id": id_, "match": {"method": method, "path": path}, "response": response, **extra}


def full_run(history: list[dict], *, view: bool = True) -> list[dict]:
    steps = [exchange("queue", "GET", "/queue", {"json": IDLE}),
             exchange("upload", "POST", "/upload/image", {"json": {"name": "up.png", "subfolder": "", "type": "input"}}),
             exchange("prompt", "POST", "/prompt", {"json": {"prompt_id": "p1"}})]
    steps += [exchange(f"history{i}", "GET", "/history/p1", {"json": body}) for i, body in enumerate(history)]
    if view:
        steps.append({"id": "view", "match": {"method": "GET", "path": "/view", "query": {"filename": "model.glb", "subfolder": "pixal3d"}},
                      "response": {"body": [{"hex": GLB.hex()}]}})
    return steps


def serve(fakes, exchanges: list[dict], *, timeout: float = 2.0):
    fake = fakes.asgi(scenario_from_data({"protocol": "comfyui", "name": "pixal3d-run", "provenance": PROVENANCE,
                                          "exchanges": exchanges}), host="comfy.fk.test")
    app = svc.create_app(fake.base_url, poll_interval=0.001, timeout=timeout)
    return fake, app


def post(app, data: dict | None = None, image: bytes = b"\x89PNG-data") -> httpx.Response:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc.test") as client:
            return await client.post("/generate", files={"image": ("in.png", image, "image/png")}, data=data or {})
    return asyncio.run(run())


def seeds(prompt: dict) -> list[int]:
    return [node["inputs"]["seed"] for node in prompt.values() if node["class_type"] == "KSampler"]


def test_one_request_returns_the_textured_glb_and_patches_the_prompt(fake_upstreams) -> None:
    fake, app = serve(fake_upstreams, full_run([{}, DONE]))
    response = post(app, {"seed": "7", "resolution": "512", "bg_removal": "birefnet"})
    assert (response.status_code, response.headers["content-type"], response.content) == (200, "model/gltf-binary", GLB)
    assert b"\x89PNG-data" in fake.calls("upload")[0].body
    prompt = fake.calls("prompt")[0].json["prompt"]
    assert prompt[svc.LOAD_IMAGE_NODE]["inputs"]["image"] == "up.png"
    assert prompt[svc.RESOLUTION_NODE]["inputs"]["target_resolution"] == 512
    assert set(seeds(prompt)) == {7}


def test_omitted_options_leave_the_shipped_prompt_values(fake_upstreams) -> None:
    fake, app = serve(fake_upstreams, full_run([DONE]))
    post(app)
    shipped, prompt = svc.load_prompt(), fake.calls("prompt")[0].json["prompt"]
    assert prompt[svc.RESOLUTION_NODE] == shipped[svc.RESOLUTION_NODE]
    assert seeds(prompt) == seeds(shipped)


def test_a_busy_comfyui_is_a_503_and_nothing_is_uploaded(fake_upstreams) -> None:
    busy = {"queue_running": [[0, "other"]], "queue_pending": []}
    fake, app = serve(fake_upstreams, [exchange("queue", "GET", "/queue", {"json": busy})])
    response = post(app)
    assert response.status_code == 503 and response.headers["retry-after"] == "30"


@pytest.mark.parametrize("data,image", [({"resolution": "256"}, b"x"), ({"seed": "-1"}, b"x"), ({}, b"")])
def test_bad_input_is_refused_before_comfyui_is_called(fake_upstreams, data: dict, image: bytes) -> None:
    fake, app = serve(fake_upstreams, [])  # no exchange: any call to ComfyUI fails the test
    assert post(app, data, image).status_code in (400, 422)


def test_a_failed_prompt_is_a_502(fake_upstreams) -> None:
    failed = {"p1": {"status": {"status_str": "error", "completed": False}, "outputs": {}}}
    _, app = serve(fake_upstreams, full_run([failed], view=False))
    assert post(app).status_code == 502


def test_a_prompt_that_never_finishes_is_a_504(fake_upstreams) -> None:
    steps = full_run([], view=False) + [exchange("history", "GET", "/history/p1", {"json": {}}, repeat=True)]
    _, app = serve(fake_upstreams, steps, timeout=0.05)
    assert post(app).status_code == 504


def test_a_finished_prompt_without_a_glb_is_a_502(fake_upstreams) -> None:
    empty = {"p1": {"status": {"status_str": "success", "completed": True}, "outputs": {svc.OUTPUT_NODE: {"3d": []}}}}
    _, app = serve(fake_upstreams, full_run([empty], view=False))
    assert post(app).status_code == 502


def test_the_shipped_prompt_has_the_nodes_the_service_patches_and_no_local_paths() -> None:
    prompt = svc.load_prompt()
    assert prompt[svc.LOAD_IMAGE_NODE]["class_type"] == "LoadImage"
    assert prompt[svc.RESOLUTION_NODE]["class_type"] == "Trellis2UpsampleStage"
    assert prompt[svc.OUTPUT_NODE]["class_type"] == "Save3DAdvanced"
    text = svc.PROMPT_PATH.read_text(encoding="utf-8")
    assert "/home/" not in text and "192.168." not in text

"""The Pixal3D ComfyUI service against scenario fakes of ComfyUI (tests/support/fake_upstream), served in process."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from engines import pixal3d_comfy_service as svc
from tests.support.fake_upstream import HandlerTransport, fake_upstreams, scenario_from_data  # noqa: F401 - fixture

GLB = b"glTF\x02\x00\x00\x00fake-model-bytes"
PROVENANCE = {"source": "hand-written", "reference": "ComfyUI server routes GET /queue, POST /upload/image, POST /prompt, GET /history/{id}, GET /view"}
IDLE = {"queue_running": [], "queue_pending": []}
DONE = {"p1": {"status": {"status_str": "success", "completed": True}, "outputs": {
    svc.OUTPUT_NODE: {"3d": [{"filename": "model.glb", "subfolder": "pixal3d", "type": "output"}]},
    "323": {"3d": [{"filename": "other.glb", "subfolder": "", "type": "output"}]}}}}


def exchange(id_: str, method: str, path: str, response: dict, **extra) -> dict:
    return {"id": id_, "match": {"method": method, "path": path}, "response": response, **extra}


# The calls a failed wait makes to take its accepted prompt off the queue.
CLEANUP = [exchange("delete", "POST", "/queue", {"json": {}}),
           exchange("cleanup-queue", "GET", "/queue", {"json": {"queue_running": [], "queue_pending": []}})]


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
    assert not [r for r in fake.requests if (r.method, r.path) in {("POST", "/queue"), ("POST", "/interrupt")}]


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
    steps += [exchange("delete", "POST", "/queue", {"json": {}}),
              exchange("cleanup-queue", "GET", "/queue", {"json": IDLE})]
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


@pytest.mark.parametrize("step,path", [("queue", "/queue"), ("upload", "/upload/image"),
                                      ("prompt", "/prompt"), ("history0", "/history/p1")])
@pytest.mark.parametrize("reply", [{"status": 500, "body": [{"hex": "3c68746d6c3e6572726f723c2f68746d6c3e"}]},
                                   {"body": [{"hex": "6e6f74204a534f4e"}]}, {"json": []}])
def test_bad_upstream_reply_is_a_json_502(fake_upstreams, step, path, reply) -> None:
    steps = full_run([DONE])
    index = next(i for i, item in enumerate(steps) if item["id"] == step)
    steps[index]["response"] = reply
    steps = steps[:index + 1]
    if step.startswith("history"):  # the prompt was accepted, so the failed wait cancels it
        steps += CLEANUP
    _, app = serve(fake_upstreams, steps)
    response = post(app)
    assert response.status_code == 502
    assert isinstance(response.json()["error"], str)


def test_prompt_without_id_is_a_json_502(fake_upstreams) -> None:
    steps = full_run([], view=False)
    steps[-1]["response"] = {"json": {}}
    _, app = serve(fake_upstreams, steps)
    response = post(app)
    assert response.status_code == 502
    assert "prompt id" in response.json()["error"]


@pytest.mark.parametrize("running", [False, True])
def test_timeout_cancels_only_its_prompt(fake_upstreams, running) -> None:
    queue = {"queue_running": [[0, "p1"]] if running else [[0, "other"]],
             "queue_pending": [] if running else [[1, "p1"]]}
    steps = full_run([], view=False) + [
        exchange("history", "GET", "/history/p1", {"json": {}}, repeat=True),
        exchange("delete", "POST", "/queue", {"json": {}}),
        exchange("cleanup-queue", "GET", "/queue", {"json": queue})]
    fake, app = serve(fake_upstreams, steps, timeout=0.005)
    response = post(app)
    assert response.status_code == 504
    assert response.json()["error"] == "ComfyUI did not finish in time"
    assert fake.calls("delete")[0].json == {"delete": ["p1"]}
    assert not [r for r in fake.requests if r.path == "/interrupt"]


def test_json_bytes_are_not_a_glb(fake_upstreams) -> None:
    steps = full_run([DONE])
    steps[-1]["response"] = {"json": {"error": "not a model"}}
    _, app = serve(fake_upstreams, steps)
    assert post(app).status_code == 502


def test_history_becomes_non_json_mid_wait(fake_upstreams) -> None:
    steps = full_run([{}], view=False) + [
        exchange("bad-history", "GET", "/history/p1",
                 {"body": [{"hex": "6e6f74204a534f4e"}]})] + CLEANUP
    _, app = serve(fake_upstreams, steps)
    response = post(app)
    assert response.status_code == 502
    assert "history" in response.json()["error"]


@pytest.mark.parametrize("reply", [{"status": 500, "body": [{"hex": "3c68746d6c3e6572726f723c2f68746d6c3e"}]},
                                   {"body": [{"hex": "6e6f74204a534f4e"}]}, {"json": []}])
def test_a_failed_history_poll_cancels_the_submitted_prompt(fake_upstreams, reply) -> None:
    # A prompt ComfyUI accepted is not left queued when the wait for it fails (train 53 review P2).
    steps = full_run([{}], view=False) + [
        exchange("bad-history", "GET", "/history/p1", reply),
        exchange("delete", "POST", "/queue", {"json": {}}),
        exchange("cleanup-queue", "GET", "/queue", {"json": {"queue_running": [], "queue_pending": [[1, "p1"]]}})]
    fake, app = serve(fake_upstreams, steps)
    response = post(app)
    assert response.status_code == 502
    assert fake.calls("delete")[0].json == {"delete": ["p1"]}
    assert not [r for r in fake.requests if r.path == "/interrupt"]


def test_a_history_poll_that_cannot_connect_cancels_the_submitted_prompt(fake_upstreams) -> None:
    steps = full_run([{}], view=False) + [
        exchange("history-drop", "GET", "/history/p1", {"disconnect_before_headers": True}),
        exchange("delete", "POST", "/queue", {"json": {}}),
        exchange("cleanup-queue", "GET", "/queue", {"json": {"queue_running": [], "queue_pending": [[1, "p1"]]}})]
    fake, app = serve(fake_upstreams, steps)
    response = post(app)
    assert response.status_code == 502
    assert fake.calls("delete")[0].json == {"delete": ["p1"]}


def test_timeout_does_not_interrupt_the_next_clients_prompt(caplog) -> None:
    async def scenario():
        running = "p1"
        interrupted = []

        async def upstream(request):
            nonlocal running
            if request.method == "POST" and request.url.path == "/queue":
                assert json.loads(request.content) == {"delete": ["p1"]}
                return httpx.Response(200, json={})
            if request.url.path == "/queue":
                snapshot = {"queue_running": [[0, running]], "queue_pending": []}
                running = "other"
                return httpx.Response(200, json=snapshot)
            if request.url.path == "/interrupt":
                interrupted.append(running)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=HandlerTransport(upstream)) as client:
            service = svc.Pixal3DService("http://comfy.test", {}, lambda: client)
            await service._cancel_prompt(client, "p1")
        assert running == "other" and interrupted == []

    asyncio.run(scenario())
    assert "could not be cancelled safely" in caplog.text


@pytest.mark.parametrize("stall_request", [1, 2])
def test_cleanup_has_one_total_deadline_and_releases_busy(stall_request, caplog) -> None:
    async def scenario():
        cleanup_requests = 0
        submitted = 0
        started = asyncio.get_running_loop().time()

        async def upstream(request):
            nonlocal cleanup_requests, submitted
            path = request.url.path
            if path == "/queue" and (request.method == "POST" or cleanup_requests):
                cleanup_requests += 1
                if cleanup_requests == stall_request:
                    await asyncio.Event().wait()
                # The second request must share the first request's elapsed time.
                await asyncio.sleep(0.15)
                return httpx.Response(200, json=IDLE)
            if path == "/queue":
                return httpx.Response(200, json=IDLE)
            if path == "/upload/image":
                return httpx.Response(200, json={"name": "up.png"})
            if path == "/prompt":
                submitted += 1
                return httpx.Response(200, json={"prompt_id": "p1"})
            if path == "/history/p1":
                return httpx.Response(200, json={} if submitted == 1 else DONE)
            if path == "/view":
                return httpx.Response(200, content=GLB)
            raise AssertionError(path)

        transport = HandlerTransport(upstream)
        service = svc.Pixal3DService("http://comfy.test", svc.load_prompt(),
                                    lambda: httpx.AsyncClient(transport=transport), timeout=-1, cleanup_timeout=0.2)
        with pytest.raises(svc.Refusal) as failure:
            await service.generate(b"image", None, None)
        elapsed = asyncio.get_running_loop().time() - started
        assert failure.value.status == 504
        assert 0.18 <= elapsed < 0.3
        assert not service._busy
        cleanup_requests = 0
        assert await service.generate(b"image", None, None) == GLB

    asyncio.run(scenario())
    assert "cleanup deadline exceeded" in caplog.text


def test_cleanup_does_not_swallow_caller_cancellation() -> None:
    async def scenario():
        entered = asyncio.Event()

        async def upstream(request):
            entered.set()
            await asyncio.Event().wait()

        async with httpx.AsyncClient(transport=HandlerTransport(upstream)) as client:
            service = svc.Pixal3DService("http://comfy.test", {}, lambda: client)
            task = asyncio.create_task(service._cancel_prompt(client, "p1"))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_timeout_is_the_history_wait_limit_not_a_total_deadline() -> None:
    """Documented in the module docstring and the example README: the upload and the download do not count against it."""
    async def scenario():
        async def upstream(request):
            path = request.url.path
            if path == "/upload/image":
                await asyncio.sleep(0.15)        # longer than ``timeout``
                return httpx.Response(200, json={"name": "up.png"})
            if path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "p1"})
            if path == "/history/p1":
                return httpx.Response(200, json=DONE)
            if path == "/view":
                return httpx.Response(200, content=GLB)
            return httpx.Response(200, json=IDLE)

        transport = HandlerTransport(upstream)
        service = svc.Pixal3DService("http://comfy.test", svc.load_prompt(), lambda: httpx.AsyncClient(transport=transport), timeout=0.05)
        return await service.generate(b"image", None, None)

    assert asyncio.run(scenario()) == GLB
    assert "not a total deadline" in svc.__doc__


def test_a_prompt_comfyui_reported_as_errored_is_not_cancelled() -> None:
    deletes = []

    async def scenario():
        async def upstream(request):
            path = request.url.path
            if request.method == "POST" and path == "/queue":
                deletes.append(1)
            if path == "/upload/image":
                return httpx.Response(200, json={"name": "up.png"})
            if path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "p1"})
            if path == "/history/p1":
                return httpx.Response(200, json={"p1": {"status": {"status_str": "error"}}})
            return httpx.Response(200, json=IDLE)

        transport = HandlerTransport(upstream)
        service = svc.Pixal3DService("http://comfy.test", svc.load_prompt(), lambda: httpx.AsyncClient(transport=transport))
        with pytest.raises(svc.Refusal):
            await service.generate(b"image", None, None)

    asyncio.run(scenario())
    assert deletes == []

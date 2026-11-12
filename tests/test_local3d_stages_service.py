"""The 3D stages service: Blender and UniRig command lines (a recorded fake runner), and the UltraTex graph against a
scenario fake of ComfyUI (tests/support/fake_upstream)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from engines import local3d_stages_service as svc
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401 - fixture

GLB = b"glTF\x02\x00\x00\x00in-model"
OUT = b"glTF\x02\x00\x00\x00out-model"
ENV = {"LOCAL3D_BLENDER_BIN": "blender", "LOCAL3D_BLENDER_SCRIPT": "stages.py", "LOCAL3D_UNIRIG_DIR": "/opt/unirig",
       "LOCAL3D_UNIRIG_BIN": "/opt/unirig-venv/bin"}
IDLE = {"queue_running": [], "queue_pending": []}
DONE = {"p1": {"status": {"status_str": "success", "completed": True}, "outputs": {
    svc.BAKE_NODE: {"3d": [{"filename": "refined.glb", "subfolder": "ultratex", "type": "output"}]}}}}
PROVENANCE = {"source": "hand-written", "reference": "ComfyUI server routes GET /queue, POST /upload/image, POST /prompt, GET /history/{id}, GET /view"}


class Recorder:
    """A runner that records each command and writes the output file the last argument names, like the real tools."""

    def __init__(self, code: int = 0, write: bool = True) -> None:
        self.calls: list[tuple[list[str], dict, Path]] = []
        self.code, self.write = code, write

    async def __call__(self, argv, env, cwd):
        self.calls.append((list(argv), dict(env), cwd))
        if self.write and self.code == 0:
            target = argv[argv.index("--out") + 1] if "--out" in argv else argv[argv.index("--output") + 1]
            if target.endswith(".glb"):
                Path(target).write_bytes(OUT)
        return self.code, ""


def post(app, path: str, files: dict | None = None, data: dict | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc.test") as client:
            return await client.post(path, files=files or {"mesh": ("in.glb", GLB, "model/gltf-binary")}, data=data or {})
    return asyncio.run(run())


def app_with(runner, tmp_path: Path, **extra):
    return svc.create_app(env=ENV, runner=runner, work_dir=tmp_path, **extra)


@pytest.mark.parametrize("path,command,options", [
    ("/retopo", "retopo", ["--target-faces", "4000"]), ("/uv", "uv", []), ("/rigify", "rigify", [])])
def test_a_blender_stage_runs_the_script_command_and_returns_the_model(tmp_path, path, command, options) -> None:
    runner = Recorder()
    response = post(app_with(runner, tmp_path), path, data={"target_faces": "4000"} if options else None)
    assert (response.status_code, response.headers["content-type"], response.content) == (200, "model/gltf-binary", OUT)
    argv = runner.calls[0][0]
    assert argv[:6] == ["blender", "-b", "--factory-startup", "--python", "stages.py", "--"] and argv[6] == command
    assert argv[7] == "--in" and argv[9] == "--out" and argv[11:] == options
    assert list(tmp_path.iterdir()) == []      # the scratch of the request is gone


def test_unirig_runs_skeleton_skin_and_merge_in_order_with_its_environment(tmp_path) -> None:
    runner = Recorder()
    response = post(app_with(runner, tmp_path), "/rig")
    assert (response.status_code, response.content) == (200, OUT)
    scripts = [call[0][1] for call in runner.calls]
    assert scripts == ["launch/inference/generate_skeleton.sh", "launch/inference/generate_skin.sh", "launch/inference/merge.sh"]
    skeleton, skin, merge = (call[0] for call in runner.calls)
    assert skin[skin.index("--input") + 1] == skeleton[skeleton.index("--output") + 1]
    assert merge[merge.index("--source") + 1] == skin[skin.index("--output") + 1]
    assert merge[merge.index("--target") + 1] == skeleton[skeleton.index("--input") + 1]
    env, cwd = runner.calls[0][1], runner.calls[0][2]
    assert env["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] == "1" and env["CUDA_VISIBLE_DEVICES"] == "0"
    assert env["PATH"].startswith("/opt/unirig-venv/bin") and cwd == Path("/opt/unirig")


def test_a_failing_tool_is_a_502_and_a_missing_output_is_a_502(tmp_path) -> None:
    assert post(app_with(Recorder(code=1), tmp_path), "/uv").status_code == 502
    assert post(app_with(Recorder(write=False), tmp_path), "/uv").status_code == 502


@pytest.mark.parametrize("path,files,data", [
    ("/uv", {"mesh": ("in.glb", b"not a model", "application/octet-stream")}, None),
    ("/retopo", None, {"target_faces": "10"}),
    ("/refine", {"mesh": ("in.glb", GLB, "model/gltf-binary"), "reference": ("r.png", b"", "image/png")}, None)])
def test_bad_input_is_refused_before_a_tool_runs(tmp_path, path, files, data) -> None:
    runner = Recorder()
    assert post(app_with(runner, tmp_path), path, files, data).status_code in (400, 422)
    assert runner.calls == []


def test_a_second_request_while_one_runs_is_a_503(tmp_path) -> None:
    gate = asyncio.Event

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(argv, env, cwd):
            started.set()
            await release.wait()
            Path(argv[argv.index("--out") + 1]).write_bytes(OUT)
            return 0, ""

        app = app_with(slow, tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://svc.test") as client:
            files = {"mesh": ("in.glb", GLB, "model/gltf-binary")}
            first = asyncio.ensure_future(client.post("/uv", files=files))
            await started.wait()
            second = await client.post("/uv", files=files)
            release.set()
            return second, await first

    second, first = asyncio.run(scenario())
    assert (second.status_code, second.headers["retry-after"], first.status_code) == (503, "30", 200)


def exchange(id_: str, method: str, path: str, response: dict, **extra) -> dict:
    return {"id": id_, "match": {"method": method, "path": path}, "response": response, **extra}


def comfy_app(fakes, history: list[dict], tmp_path: Path, *, queue: dict = IDLE):
    steps = [exchange("queue", "GET", "/queue", {"json": queue})]
    if queue is IDLE:
        steps += [exchange("upload", "POST", "/upload/image", {"json": {"name": "ref-up.png", "subfolder": "", "type": "input"}}),
                  exchange("prompt", "POST", "/prompt", {"json": {"prompt_id": "p1"}})]
        steps += [exchange(f"history{i}", "GET", "/history/p1", {"json": body}) for i, body in enumerate(history)]
        steps.append({"id": "view", "match": {"method": "GET", "path": "/view", "query": {"filename": "refined.glb", "subfolder": "ultratex"}},
                      "response": {"body": [{"hex": OUT.hex()}]}})
    fake = fakes.asgi(scenario_from_data({"protocol": "comfyui", "name": "ultratex-run", "provenance": PROVENANCE, "exchanges": steps}),
                      host="comfy.fk.test")
    return fake, app_with(Recorder(), tmp_path, comfy_url=fake.base_url, poll_interval=0.001)


def refine(app, data: dict | None = None) -> httpx.Response:
    return post(app, "/refine", {"mesh": ("in.glb", GLB, "model/gltf-binary"), "reference": ("r.png", b"\x89PNG-ref", "image/png")}, data)


def test_texture_refine_patches_the_graph_and_returns_the_refined_model(fake_upstreams, tmp_path) -> None:
    fake, app = comfy_app(fake_upstreams, [{}, DONE], tmp_path)
    response = refine(app, {"seed": "7", "resolution": "1536", "steps": "30"})
    assert (response.status_code, response.content) == (200, OUT)
    assert b"\x89PNG-ref" in fake.calls("upload")[0].body
    prompt = fake.calls("prompt")[0].json["prompt"]
    assert prompt[svc.REFERENCE_NODE]["inputs"]["image"] == "ref-up.png"
    assert prompt[svc.PREP_NODE]["inputs"]["mesh_path"].endswith("in.glb")
    assert {k: prompt[svc.SAMPLER_NODE]["inputs"][k] for k in ("seed", "resolution", "steps")} == {"seed": 7, "resolution": 1536, "steps": 30}


def test_omitted_options_leave_the_shipped_graph_values(fake_upstreams, tmp_path) -> None:
    fake, app = comfy_app(fake_upstreams, [DONE], tmp_path)
    refine(app)
    shipped, prompt = svc.json.loads(svc.PROMPT_PATH.read_text()), fake.calls("prompt")[0].json["prompt"]
    assert prompt[svc.SAMPLER_NODE]["inputs"] == shipped[svc.SAMPLER_NODE]["inputs"]


def test_a_busy_comfyui_is_a_503_for_texture_refine(fake_upstreams, tmp_path) -> None:
    _, app = comfy_app(fake_upstreams, [], tmp_path, queue={"queue_running": [[0, "other"]], "queue_pending": []})
    response = refine(app)
    assert response.status_code == 503 and response.headers["retry-after"] == "30"


def test_the_shipped_graph_has_the_nodes_the_service_patches_and_no_local_paths() -> None:
    graph = svc.json.loads(svc.PROMPT_PATH.read_text(encoding="utf-8"))
    kinds = {svc.REFERENCE_NODE: "LoadImage", svc.PREP_NODE: "UltraTexPrep", svc.SAMPLER_NODE: "UltraTexSampler", svc.BAKE_NODE: "UltraTexBake"}
    assert {node: graph[node]["class_type"] for node in kinds} == kinds
    text = svc.PROMPT_PATH.read_text(encoding="utf-8") + Path(svc.__file__).read_text(encoding="utf-8")
    assert "/home/" not in text and "192.168." not in text


def test_cancelled_tool_is_reaped_before_its_runner_returns(monkeypatch, tmp_path) -> None:
    async def scenario():
        entered = asyncio.Event()
        signals = []
        reaped = False

        class Process:
            pid = 123

            async def communicate(self):
                entered.set()
                await asyncio.Event().wait()

            async def wait(self):
                nonlocal reaped
                reaped = True
                return 0

        async def spawn(*args, **kwargs):
            assert kwargs["start_new_session"] is True
            return Process()

        monkeypatch.setattr(svc.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(svc.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
        task = asyncio.create_task(svc.run_process(["tool"], {}, tmp_path))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert signals == [(123, svc.signal.SIGTERM)]
        assert reaped

    asyncio.run(scenario())


def test_configured_stage_timeout_cancels_the_runner_and_admits_the_next_request(tmp_path) -> None:
    cleaned = False
    calls = 0

    async def runner(argv, env, cwd):
        nonlocal cleaned, calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cleaned = True
        assert cleaned
        Path(argv[argv.index("--out") + 1]).write_bytes(OUT)
        return 0, ""

    app = svc.create_app(env={**ENV, "LOCAL3D_TIMEOUT_SECONDS": "0.01"}, runner=runner, work_dir=tmp_path)
    assert post(app, "/uv").status_code == 504
    assert cleaned
    assert post(app, "/uv").content == OUT

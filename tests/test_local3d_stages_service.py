"""The 3D stages service: Blender and UniRig command lines (a recorded fake runner), and the UltraTex graph against a
scenario fake of ComfyUI (tests/support/fake_upstream)."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from engines import local3d_stages_service as svc
from tests.support.fake_upstream import HandlerTransport, fake_upstreams, scenario_from_data  # noqa: F401 - fixture

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

    def __init__(self, code: int = 0, write: bool = True, output: bytes = OUT) -> None:
        self.calls: list[tuple[list[str], dict, Path]] = []
        self.code, self.write, self.output = code, write, output

    async def __call__(self, argv, env, cwd):
        self.calls.append((list(argv), dict(env), cwd))
        if self.write and self.code == 0:
            target = argv[argv.index("--out") + 1] if "--out" in argv else argv[argv.index("--output") + 1]
            if target.endswith(".glb"):
                Path(target).write_bytes(self.output)
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


# --- the lifecycle of a prompt ComfyUI accepted, scratch setup, output checks, malformed replies, input caps -----------------

class Comfy:
    """A hand-written ComfyUI for the stage service: the prompt is accepted, runs ``run_for`` seconds, then ends.
    ``run_for=None`` means it never ends on its own. A pending-queue delete is recorded; it removes the prompt only
    while it is pending, never while it runs (the real server cannot cancel a running prompt that way)."""

    def __init__(self, *, run_for: float | None = 0.0, starts_running: bool = True, replies: dict | None = None) -> None:
        self.run_for, self.starts_running, self.replies = run_for, starts_running, replies or {}
        self.deletes: list[dict] = []
        self.paths: list[str] = []
        self.mesh_existed: list[bool] = []
        self.mesh_path: Path | None = None
        self.submitted_at: float | None = None
        self.accepted = asyncio.Event()

    def ended(self) -> bool:
        if self.submitted_at is None or self.run_for is None:
            return False
        return asyncio.get_running_loop().time() - self.submitted_at >= self.run_for

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(f"{request.method} {path}")
        if path in self.replies:
            return self.replies[path]
        if request.method == "POST" and path == "/queue":
            self.deletes.append(json.loads(request.content))
            return httpx.Response(200, json={})
        if path == "/queue":
            if self.mesh_path is not None:
                self.mesh_existed.append(self.mesh_path.exists())
            if self.submitted_at is None or self.ended():
                return httpx.Response(200, json=IDLE)
            return httpx.Response(200, json={"queue_running": [[0, "p1"]] if self.starts_running else [], "queue_pending": [] if self.starts_running else [[0, "p1"]]})
        if path == "/upload/image":
            return httpx.Response(200, json={"name": "up.png"})
        if path == "/prompt":
            graph = json.loads(request.content)["prompt"]
            self.mesh_path = Path(graph[svc.PREP_NODE]["inputs"]["mesh_path"])
            self.submitted_at = asyncio.get_running_loop().time()
            self.accepted.set()
            return httpx.Response(200, json={"prompt_id": "p1"})
        if path == "/history/p1":
            return httpx.Response(200, json=DONE if self.ended() else {})
        if path == "/view":
            return httpx.Response(200, content=OUT)
        raise AssertionError(path)


def comfy_service(comfy: Comfy, tmp_path: Path, **extra) -> svc.Stages:
    transport = HandlerTransport(comfy)
    return svc.Stages(env=ENV, runner=Recorder(), comfy_url="http://comfy.test", prompt=svc.json.loads(svc.PROMPT_PATH.read_text()),
                      client_factory=lambda: httpx.AsyncClient(transport=transport), work_dir=tmp_path, poll_interval=0.005,
                      timeout=extra.pop("timeout", 0.05), **extra)


def run_refine(stages: svc.Stages) -> bytes:
    return asyncio.run(stages.run("refine", GLB, {}, b"\x89PNG-ref"))


def test_a_timed_out_refine_deletes_only_its_prompt_and_keeps_its_input_until_the_prompt_ended(tmp_path) -> None:
    comfy = Comfy(run_for=0.3)
    stages = comfy_service(comfy, tmp_path, cleanup_timeout=2.0)
    with pytest.raises(svc.Refusal) as refusal:
        run_refine(stages)
    assert refusal.value.status == 504
    assert comfy.deletes == [{"delete": ["p1"]}]
    assert not [p for p in comfy.paths if "interrupt" in p]
    assert comfy.mesh_existed and all(comfy.mesh_existed)      # the model stayed while the prompt could still run
    assert stages._busy is False and not comfy.mesh_path.exists() and list(tmp_path.iterdir()) == []


def test_a_prompt_that_never_ends_is_given_one_bounded_cleanup_deadline_then_logged(tmp_path, caplog) -> None:
    comfy = Comfy(run_for=None)
    stages = comfy_service(comfy, tmp_path, cleanup_timeout=0.15)

    async def scenario():
        started = asyncio.get_running_loop().time()
        with pytest.raises(svc.Refusal) as refusal:
            await stages.run("refine", GLB, {}, b"\x89PNG-ref")
        return refusal.value.status, asyncio.get_running_loop().time() - started

    with caplog.at_level(logging.WARNING):
        status, elapsed = asyncio.run(scenario())
    assert status == 504 and 0.15 <= elapsed < 0.6
    assert "may still run" in caplog.text
    assert stages._busy is False and list(tmp_path.iterdir()) == []


def test_a_cancelled_caller_cleans_up_the_accepted_prompt_before_capacity_is_released(tmp_path) -> None:
    comfy = Comfy(run_for=0.2)
    stages = comfy_service(comfy, tmp_path, timeout=30, cleanup_timeout=2.0)

    async def scenario():
        task = asyncio.create_task(stages.run("refine", GLB, {}, b"\x89PNG-ref"))
        await comfy.accepted.wait()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert comfy.deletes == [{"delete": ["p1"]}]
    assert comfy.mesh_existed and all(comfy.mesh_existed)
    assert stages._busy is False and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["mkdir", "mkdtemp", "write"])
def test_a_scratch_setup_error_releases_the_stage_and_the_next_request_runs(tmp_path, monkeypatch, failure) -> None:
    work_dir = tmp_path / "scratch"
    if failure == "mkdir":
        work_dir.write_text("a file, not a directory")
    else:
        broken = {"mkdtemp": (svc.tempfile, "mkdtemp"), "write": (Path, "write_bytes")}[failure]
        real = getattr(*broken)
        calls = []

        def once(*args, **kwargs):
            if not calls:
                calls.append(1)
                raise OSError(28, "No space left on device")
            return real(*args, **kwargs)
        monkeypatch.setattr(*broken, once)
    app = svc.create_app(env=ENV, runner=Recorder(), work_dir=work_dir)
    first = post(app, "/uv")
    assert first.status_code == 500 and "error" in first.json()
    if failure == "mkdir":
        work_dir.unlink()
    second = post(app, "/uv")
    assert (second.status_code, second.content) == (200, OUT)


@pytest.mark.parametrize("not_a_model", [b"", b'{"error": "not a model"}', b"<html>oops</html>", b"glT"])
def test_a_tool_output_without_the_glb_magic_is_a_json_502(tmp_path, not_a_model) -> None:
    runner = Recorder(output=not_a_model or b"x")
    if not not_a_model:
        runner = Recorder(write=False)
    response = post(app_with(runner, tmp_path), "/uv")
    assert response.status_code == 502 and response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]


def test_a_view_reply_that_is_not_a_glb_is_a_json_502(tmp_path) -> None:
    comfy = Comfy(replies={"/view": httpx.Response(200, json={"error": "not a model"})})
    stages = comfy_service(comfy, tmp_path, timeout=5)
    with pytest.raises(svc.Refusal) as refusal:
        run_refine(stages)
    assert refusal.value.status == 502


MALFORMED = {
    "queue is a list": ("/queue", httpx.Response(200, json=[]), False),
    "queue is html": ("/queue", httpx.Response(200, content=b"<html>"), False),
    "upload is a list": ("/upload/image", httpx.Response(200, json=[]), False),
    "upload is html": ("/upload/image", httpx.Response(200, content=b"<html>"), False),
    "submit is a list": ("/prompt", httpx.Response(200, json=[]), False),
    "submit has no id": ("/prompt", httpx.Response(200, json={}), False),
    "history is a list": ("/history/p1", httpx.Response(200, json=[]), True),
    "history is html": ("/history/p1", httpx.Response(200, content=b"<html>"), True),
    "history entry is a list": ("/history/p1", httpx.Response(200, json={"p1": []}), True),
    "history outputs is a list": ("/history/p1", httpx.Response(200, json={"p1": {"outputs": [1], "status": {"completed": True}}}), True),
    "history status is text": ("/history/p1", httpx.Response(200, json={"p1": {"status": "ok"}}), True),
}


@pytest.mark.parametrize("name", MALFORMED)
def test_a_malformed_comfyui_reply_is_a_json_502_and_an_accepted_prompt_is_reconciled(tmp_path, name) -> None:
    path, reply, accepted = MALFORMED[name]
    comfy = Comfy(run_for=0.0, replies={path: reply})
    app = svc.create_app(env=ENV, runner=Recorder(), work_dir=tmp_path, comfy_url="http://comfy.test", poll_interval=0.005,
                         client_factory=lambda: httpx.AsyncClient(transport=HandlerTransport(comfy)), timeout=5, cleanup_timeout=1.0)
    response = refine(app)
    assert response.status_code == 502 and response.headers["content-type"].startswith("application/json"), response.text
    assert (comfy.deletes == [{"delete": ["p1"]}]) is accepted
    assert list(tmp_path.iterdir()) == []


def test_the_stage_service_refuses_a_body_over_its_cap_before_it_reads_it(tmp_path) -> None:
    app = svc.create_app(env=ENV, runner=Recorder(), work_dir=tmp_path, max_mesh_bytes=64, max_reference_bytes=16)
    received = []

    async def scenario():
        sent = []

        async def receive():
            received.append(1)
            raise AssertionError("the body was read")

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "path": "/uv", "raw_path": b"/uv",
                 "query_string": b"", "root_path": "", "scheme": "http", "server": ("svc.test", 80), "client": ("c", 1),
                 "headers": [(b"host", b"svc.test"), (b"content-type", b"multipart/form-data; boundary=x"), (b"content-length", b"%d" % (10 * 1024 * 1024))]}
        await app(scope, receive, send)
        return sent

    sent = asyncio.run(scenario())
    assert sent[0]["status"] == 413 and received == []


def test_a_mesh_or_reference_over_its_cap_is_a_413_before_any_tool_or_comfyui_work(tmp_path) -> None:
    runner = Recorder()
    app = svc.create_app(env=ENV, runner=runner, work_dir=tmp_path, max_mesh_bytes=len(GLB), max_reference_bytes=8, comfy_url="http://comfy.invalid")
    assert post(app, "/uv", {"mesh": ("in.glb", GLB + b"x", "model/gltf-binary")}).status_code == 413
    assert post(app, "/uv").status_code == 200      # exactly the cap is fine
    big_reference = post(app, "/refine", {"mesh": ("in.glb", GLB, "model/gltf-binary"), "reference": ("r.png", b"123456789", "image/png")})
    assert big_reference.status_code == 413 and len(runner.calls) == 1


def test_the_manifests_cap_the_mesh_and_reference_inputs_at_what_the_service_accepts() -> None:
    import yaml

    for stage in ("retopo", "rig-auto", "rig-humanoid", "texture-refine", "uv-unwrap"):
        document = yaml.safe_load((Path(__file__).resolve().parent.parent / "manifests" / "workflows" / f"mesh.edit.{stage}.yml").read_text())
        assert document["inputs"]["mesh"]["max_bytes"] == svc.DEFAULT_MAX_MESH_BYTES, stage
    refine_doc = yaml.safe_load((Path(__file__).resolve().parent.parent / "manifests" / "workflows" / "mesh.edit.texture-refine.yml").read_text())
    assert refine_doc["inputs"]["reference"]["max_bytes"] == svc.DEFAULT_MAX_REFERENCE_BYTES

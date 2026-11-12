#!/usr/bin/env python3
"""Serve the 3D stage multipart contract: a model in, a model out.

The gateway's ``mesh.edit`` records send one multipart ``POST`` to a stage path and expect raw GLB bytes back:

- ``/retopo``   (``mesh``, ``target_faces``)             Blender: fewer faces
- ``/uv``       (``mesh``)                               Blender: unwrap and pack the UVs
- ``/rigify``   (``mesh``)                               Blender: a humanoid skeleton with automatic weights
- ``/rig``      (``mesh``)                               UniRig: predicted skeleton, skin weights, merged onto the model
- ``/refine``   (``mesh``, ``reference``, ``seed``, ``resolution``, ``steps``)   the UltraTex graph in ComfyUI

One request runs at a time; another gets 503. Blender and UniRig run as child processes with the command lines of
``tools/local-3d/STAGES.md``; the paths come from the environment:

``LOCAL3D_BLENDER_BIN`` (default ``blender``), ``LOCAL3D_BLENDER_SCRIPT`` (the headless Blender script),
``LOCAL3D_UNIRIG_DIR`` (the UniRig clone), ``LOCAL3D_UNIRIG_BIN`` (its venv ``bin`` directory),
``LOCAL3D_WORK_DIR`` (scratch the ComfyUI process can also read), ``COMFYUI_URL``, ``LOCAL3D_TIMEOUT_SECONDS`` (one total
deadline for a stage), ``LOCAL3D_CLEANUP_TIMEOUT_SECONDS`` (the one bounded wait that settles a ComfyUI prompt after a
timeout or a cancel, default 5), ``LOCAL3D_MAX_MESH_BYTES`` and ``LOCAL3D_MAX_REFERENCE_BYTES`` (input caps).

A texture-refine prompt that ComfyUI accepted is settled before the stage releases anything: only that prompt id is deleted
from the pending queue (never a global interrupt), and the input model and the busy flag stay until the history says the
prompt ended or the cleanup deadline passes (then the stage logs that the prompt may still run).
Run: ``uvicorn engines.local3d_stages_service:app --port 8893``
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shutil
import signal
import tempfile
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

PROMPT_PATH = Path(__file__).with_name("ultratex_prompt.json")
REFERENCE_NODE, PREP_NODE, SAMPLER_NODE, BAKE_NODE = "7", "8", "9", "10"   # LoadImage, UltraTexPrep, UltraTexSampler, UltraTexBake
SEED_MAX = 4294967295
GLB_MAGIC = b"glTF"
# The caps the manifests declare as ``max_bytes`` on the ``mesh`` and ``reference`` inputs (a test keeps them equal). A
# textured model from the picture-to-3D records is tens of MiB; 256 MiB leaves room for a large one. A reference picture is
# a photo; 32 MiB.
DEFAULT_MAX_MESH_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_REFERENCE_BYTES = 32 * 1024 * 1024
FORM_SLACK_BYTES = 1024 * 1024   # the multipart framing and the small form fields, on top of the file caps

# (argv, extra environment) -> exit code and combined output
Runner = Callable[[Sequence[str], Mapping[str, str], Path], Awaitable[tuple[int, str]]]


class Refusal(Exception):
    def __init__(self, status: int, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


def upstream_object(response: httpx.Response, step: str) -> dict[str, Any]:
    """Only a successful JSON object is a usable ComfyUI answer."""
    try:
        if response.status_code != 200:
            raise ValueError("status")
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("object")
        return body
    except ValueError as exc:
        raise Refusal(502, f"ComfyUI returned an invalid {step} reply") from exc


async def run_process(argv: Sequence[str], env: Mapping[str, str], cwd: Path) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), env={**os.environ, **env}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True)
    try:
        output, _ = await process.communicate()
    except asyncio.CancelledError:
        # Shell scripts can launch children: stop the whole request's process group
        # before the stage releases its busy flag and removes the input files.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        raise
    return process.returncode or 0, output.decode("utf-8", "replace")


def blender_argv(binary: str, script: str, command: str, source: Path, target: Path, options: Mapping[str, Any]) -> list[str]:
    """``blender -b --factory-startup --python SCRIPT -- <cmd> --in IN --out OUT [options]``."""
    argv = [binary, "-b", "--factory-startup", "--python", script, "--", command, "--in", str(source), "--out", str(target)]
    for name, value in options.items():
        argv += [f"--{name.replace('_', '-')}", str(value)]
    return argv


def unirig_steps(directory: Path, source: Path, work: Path, target: Path) -> list[list[str]]:
    """The three UniRig steps: skeleton, skin, then merge onto the original model."""
    skeleton, skin = work / "skeleton.fbx", work / "skin.fbx"
    return [
        ["bash", "launch/inference/generate_skeleton.sh", "--input", str(source), "--output", str(skeleton)],
        ["bash", "launch/inference/generate_skin.sh", "--input", str(skeleton), "--output", str(skin)],
        ["bash", "launch/inference/merge.sh", "--source", str(skin), "--target", str(source), "--output", str(target)],
    ]


def patch_prompt(prompt: dict[str, Any], *, reference: str, mesh_path: str, seed: int | None, resolution: int | None,
                 steps: int | None, prefix: str) -> dict[str, Any]:
    """A copy of the UltraTex graph for one request."""
    patched = copy.deepcopy(prompt)
    patched[REFERENCE_NODE]["inputs"]["image"] = reference
    patched[PREP_NODE]["inputs"]["mesh_path"] = mesh_path
    patched[BAKE_NODE]["inputs"]["filename_prefix"] = prefix
    for name, value in (("seed", seed), ("resolution", resolution), ("steps", steps)):
        if value is not None:
            patched[SAMPLER_NODE]["inputs"][name] = value
    return patched


def find_glb(outputs: Mapping[str, Any]) -> dict[str, str] | None:
    """The file entry of the first .glb the bake node lists, whatever key lists it."""
    node = outputs.get(BAKE_NODE)
    for value in node.values() if isinstance(node, dict) else []:
        for item in value if isinstance(value, list) else []:
            if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(".glb"):
                return {"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")}
    return None


class Stages:
    def __init__(self, *, env: Mapping[str, str], runner: Runner, comfy_url: str, prompt: dict[str, Any],
                 client_factory: Callable[[], httpx.AsyncClient], work_dir: Path, poll_interval: float, timeout: float,
                 cleanup_timeout: float = 5.0) -> None:
        self.env, self.runner, self.comfy_url = env, runner, comfy_url.rstrip("/")
        self.prompt, self.client_factory, self.work_dir = prompt, client_factory, work_dir
        self.poll_interval, self.timeout, self.cleanup_timeout = poll_interval, timeout, cleanup_timeout
        self._busy = False

    async def run(self, stage: str, mesh: bytes, options: Mapping[str, Any], reference: bytes | None = None) -> bytes:
        if self._busy:
            raise Refusal(503, "a 3D stage is already running", retry_after=30)
        self._busy = True
        work: Path | None = None
        try:
            try:
                self.work_dir.mkdir(parents=True, exist_ok=True)
                work = Path(tempfile.mkdtemp(prefix="stage-", dir=self.work_dir))
                source, target = work / "in.glb", work / "out.glb"
                source.write_bytes(mesh)
            except OSError as exc:
                logging.getLogger(__name__).warning("3D stage scratch setup failed: %s", type(exc).__name__)
                raise Refusal(500, "the stage could not prepare its scratch space") from exc
            if stage == "refine":
                glb = await asyncio.wait_for(self._refine(source, reference or b"", options), self.timeout)
            else:
                if stage == "rig":
                    await asyncio.wait_for(self._rig(source, work, target), self.timeout)
                else:
                    await asyncio.wait_for(self._blender(stage, source, target, options), self.timeout)
                if not target.exists() or target.stat().st_size == 0:
                    raise Refusal(502, "the stage finished without a model")
                glb = target.read_bytes()
            if not glb.startswith(GLB_MAGIC):
                raise Refusal(502, "the stage finished with a file that is not a binary glTF model")
            return glb
        except asyncio.TimeoutError as exc:
            raise Refusal(504, "the stage did not finish in time") from exc
        finally:
            self._busy = False
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)

    async def _blender(self, command: str, source: Path, target: Path, options: Mapping[str, Any]) -> None:
        argv = blender_argv(self.env.get("LOCAL3D_BLENDER_BIN", "blender"), self.env.get("LOCAL3D_BLENDER_SCRIPT", "local3d_blender.py"),
                            command, source, target, options)
        code, output = await self.runner(argv, {}, self.work_dir)
        if code != 0:
            raise Refusal(502, f"Blender stage {command} failed ({code})")

    async def _rig(self, source: Path, work: Path, target: Path) -> None:
        directory = Path(self.env.get("LOCAL3D_UNIRIG_DIR", "UniRig"))
        bin_dir = self.env.get("LOCAL3D_UNIRIG_BIN", "")
        env = {"TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD": "1", "CUDA_VISIBLE_DEVICES": "0",
               "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}" if bin_dir else os.environ.get("PATH", "")}
        for argv in unirig_steps(directory, source, work, target):
            code, _ = await self.runner(argv, env, directory)
            if code != 0:
                raise Refusal(502, f"UniRig step {Path(argv[1]).stem} failed ({code})")

    async def _refine(self, source: Path, reference: bytes, options: Mapping[str, Any]) -> bytes:
        prompt_id: str | None = None
        try:
            async with self.client_factory() as client:
                try:
                    queue = upstream_object(await client.get(f"{self.comfy_url}/queue"), "queue")
                    if queue.get("queue_running") or queue.get("queue_pending"):
                        raise Refusal(503, "ComfyUI is busy", retry_after=30)
                    name = f"local3d-{uuid.uuid4().hex}.png"
                    up = await client.post(f"{self.comfy_url}/upload/image", files={"image": (name, reference, "image/png")},
                                           data={"overwrite": "true"})
                    stored = upstream_object(up, "reference upload").get("name")
                    prompt = patch_prompt(self.prompt, reference=stored if isinstance(stored, str) and stored else name,
                                          mesh_path=str(source.resolve()), seed=options.get("seed"), resolution=options.get("resolution"),
                                          steps=options.get("steps"), prefix=f"ultratex/{uuid.uuid4().hex}")
                    sub = await client.post(f"{self.comfy_url}/prompt", json={"prompt": prompt})
                    accepted = upstream_object(sub, "graph submit").get("prompt_id")
                    if not isinstance(accepted, str) or not accepted:
                        raise Refusal(502, "ComfyUI graph submit reply has no prompt id")
                    prompt_id = accepted
                    entry = await self._wait(client, prompt_id)
                    ref = find_glb(entry.get("outputs") or {})
                    if ref is None:
                        raise Refusal(502, "ComfyUI finished without a model")
                    got = await client.get(f"{self.comfy_url}/view", params=ref)
                    if got.status_code != 200 or not got.content.startswith(GLB_MAGIC):
                        raise Refusal(502, f"ComfyUI would not return the model ({got.status_code})")
                    return got.content
                except BaseException:
                    # The graph reads the input model from the scratch directory: while the prompt may still run, neither the
                    # file nor the busy flag may go. The caller releases both after this returns.
                    if prompt_id is not None:
                        await self._settle_prompt(client, prompt_id)
                    raise
        except httpx.HTTPError as exc:
            raise Refusal(502, f"ComfyUI did not answer: {type(exc).__name__}") from exc

    async def _wait(self, client: httpx.AsyncClient, prompt_id: str) -> dict[str, Any]:
        while True:
            entry = self._history_entry(await client.get(f"{self.comfy_url}/history/{prompt_id}"), prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    raise Refusal(502, "ComfyUI reported an error for the graph")
                if status.get("completed") or entry.get("outputs"):
                    return entry
            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _history_entry(response: httpx.Response, prompt_id: str) -> dict[str, Any] | None:
        """The history entry of a prompt, ``None`` while it has none; a reply of another shape is a 502."""
        entry = upstream_object(response, "history").get(prompt_id)
        if entry is None or entry == {}:
            return None
        if not isinstance(entry, dict) or not isinstance(entry.get("status") or {}, dict) \
                or not isinstance(entry.get("outputs") or {}, dict):
            raise Refusal(502, "ComfyUI returned an invalid history reply")
        return entry

    async def _settle_prompt(self, client: httpx.AsyncClient, prompt_id: str) -> None:
        """Take one accepted prompt out of ComfyUI's way, inside one bounded wait.

        Only this prompt id is deleted from the pending queue; a global interrupt could stop another client's prompt. A
        prompt that already runs cannot be cancelled that way, so wait, until the deadline, for the history to say it ended
        (or for it to leave both queues). At the deadline log that it may still run and let the caller release."""
        logger = logging.getLogger(__name__)
        errors: list[str] = []
        try:
            async with asyncio.timeout(self.cleanup_timeout):
                try:
                    (await client.post(f"{self.comfy_url}/queue", json={"delete": [prompt_id]})).raise_for_status()
                except Exception as exc:  # noqa: BLE001 - cleanup reports, it does not raise
                    errors.append(type(exc).__name__)
                while True:
                    try:
                        if self._history_entry(await client.get(f"{self.comfy_url}/history/{prompt_id}"), prompt_id) is not None:
                            return
                        queue = upstream_object(await client.get(f"{self.comfy_url}/queue"), "cleanup queue")
                        if not any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id
                                   for key in ("queue_running", "queue_pending") for row in queue.get(key) or []):
                            return
                    except Exception as exc:  # noqa: BLE001
                        errors.append(type(exc).__name__)
                    await asyncio.sleep(self.poll_interval)
        except TimeoutError:
            pass
        logger.warning("ComfyUI prompt %s may still run after the 3D stage cleanup deadline%s", prompt_id,
                       f" ({', '.join(sorted(set(errors)))})" if errors else "")


def create_app(*, env: Mapping[str, str] | None = None, runner: Runner = run_process, comfy_url: str | None = None,
               prompt: dict[str, Any] | None = None, client_factory: Callable[[], httpx.AsyncClient] | None = None,
               work_dir: Path | None = None, poll_interval: float = 2.0, timeout: float | None = None,
               cleanup_timeout: float | None = None, max_mesh_bytes: int | None = None,
               max_reference_bytes: int | None = None) -> FastAPI:
    env = os.environ if env is None else env
    mesh_cap = max_mesh_bytes if max_mesh_bytes is not None else int(env.get("LOCAL3D_MAX_MESH_BYTES", DEFAULT_MAX_MESH_BYTES))
    reference_cap = max_reference_bytes if max_reference_bytes is not None else int(
        env.get("LOCAL3D_MAX_REFERENCE_BYTES", DEFAULT_MAX_REFERENCE_BYTES))
    stages = Stages(
        env=env, runner=runner, comfy_url=comfy_url or env.get("COMFYUI_URL", "http://127.0.0.1:8188"),
        prompt=prompt if prompt is not None else json.loads(PROMPT_PATH.read_text(encoding="utf-8")),
        client_factory=client_factory or (lambda: httpx.AsyncClient(timeout=60.0)),
        work_dir=work_dir or Path(env.get("LOCAL3D_WORK_DIR") or tempfile.gettempdir()) / "local3d-stages",
        poll_interval=poll_interval, timeout=timeout if timeout is not None else float(env.get("LOCAL3D_TIMEOUT_SECONDS", "900")),
        cleanup_timeout=cleanup_timeout if cleanup_timeout is not None else float(env.get("LOCAL3D_CLEANUP_TIMEOUT_SECONDS", "5")))
    app = FastAPI(title="3D stages service")

    def too_large(what: str, cap: int) -> JSONResponse:
        return JSONResponse({"error": f"the {what} is larger than {cap} bytes"}, status_code=413)

    @app.middleware("http")
    async def refuse_oversized_body(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # Refuse on the declared length before the multipart body is parsed (and spooled) at all.
        declared = request.headers.get("content-length", "")
        limit = mesh_cap + (reference_cap if request.url.path == "/refine" else 0) + FORM_SLACK_BYTES
        if declared.isdigit() and int(declared) > limit:
            return too_large("request", limit)
        return await call_next(request)

    async def read_capped(upload: UploadFile, cap: int) -> bytes | None:
        """The file's bytes, or ``None`` when it holds more than ``cap`` (it reads at most one byte past the cap)."""
        data = await upload.read(cap + 1)
        return None if len(data) > cap else data

    async def respond(stage: str, mesh: UploadFile, options: Mapping[str, Any], reference: UploadFile | None = None) -> Response:
        data = await read_capped(mesh, mesh_cap)
        if data is None:
            return too_large("model", mesh_cap)
        if not data.startswith(GLB_MAGIC):
            return JSONResponse({"error": "the model is not a binary glTF file"}, status_code=422)
        picture = await read_capped(reference, reference_cap) if reference is not None else None
        if reference is not None and picture is None:
            return too_large("reference picture", reference_cap)
        if reference is not None and not picture:
            return JSONResponse({"error": "the reference picture is empty"}, status_code=400)
        try:
            glb = await stages.run(stage, data, options, picture)
        except Refusal as refusal:
            headers = {"Retry-After": str(refusal.retry_after)} if refusal.retry_after else None
            return JSONResponse({"error": refusal.message}, status_code=refusal.status, headers=headers)
        return Response(glb, media_type="model/gltf-binary")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/retopo")
    async def retopo(mesh: UploadFile = File(...), target_faces: int | None = Form(None)) -> Response:
        if target_faces is not None and not 500 <= target_faces <= 200000:
            return JSONResponse({"error": "target_faces must be 500 to 200000"}, status_code=422)
        return await respond("retopo", mesh, {} if target_faces is None else {"target_faces": target_faces})

    @app.post("/uv")
    async def uv(mesh: UploadFile = File(...)) -> Response:
        return await respond("uv", mesh, {})

    @app.post("/rigify")
    async def rigify(mesh: UploadFile = File(...)) -> Response:
        return await respond("rigify", mesh, {})

    @app.post("/rig")
    async def rig(mesh: UploadFile = File(...)) -> Response:
        return await respond("rig", mesh, {})

    @app.post("/refine")
    async def refine(mesh: UploadFile = File(...), reference: UploadFile = File(...), seed: int | None = Form(None),
                     resolution: int | None = Form(None), steps: int | None = Form(None)) -> Response:
        if seed is not None and not 0 <= seed <= SEED_MAX:
            return JSONResponse({"error": f"seed must be 0 to {SEED_MAX}"}, status_code=422)
        if resolution is not None and not 512 <= resolution <= 2048 or steps is not None and not 10 <= steps <= 50:
            return JSONResponse({"error": "resolution must be 512 to 2048 and steps 10 to 50"}, status_code=422)
        options = {name: value for name, value in (("seed", seed), ("resolution", resolution), ("steps", steps)) if value is not None}
        return await respond("refine", mesh, options, reference)

    return app


app = create_app()

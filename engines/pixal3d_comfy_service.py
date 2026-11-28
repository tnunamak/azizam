#!/usr/bin/env python3
"""Serve the image-to-3D multipart contract over a ComfyUI that runs Pixal3D.

The gateway's mesh route sends one multipart ``POST /generate`` (fields ``image``, ``seed``, ``resolution``) and expects
raw GLB bytes back. This service keeps that contract and turns each request into one ComfyUI API prompt:

1. refuse with 503 while ComfyUI has a queued or running prompt (or this service has a request in flight);
2. upload the picture with ``/upload/image``;
3. patch the prompt (picture, seed, resolution, output name) and submit it with ``/prompt``;
4. poll ``/history/<id>`` until it finishes, then download the textured GLB with ``/view``.

``timeout`` (``PIXAL3D_TIMEOUT_SECONDS``) is the history-wait limit, not a total deadline: it starts when the prompt is
submitted and bounds only the polling. Each other HTTP call has its own 60 second client limit.

The prompt is the data file ``pixal3d_prompt.json`` next to this script. The node ids below name nodes of that file.
Run: ``COMFYUI_URL=http://127.0.0.1:8188 uvicorn engines.pixal3d_comfy_service:app --port 8892``
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

PROMPT_PATH = Path(__file__).with_name("pixal3d_prompt.json")
def load_bindings(path: Path | None = None) -> dict[str, Any]:
    data = json.loads((path or Path(__file__).with_name("pixal3d_bindings.json")).read_text())
    for role in ("image", "resolution", "output"):
        if any(not isinstance(data.get(role, {}).get(k), str) or not data[role][k] for k in ("node", "input")):
            raise ValueError(f"invalid binding: {role}")
    if not data.get("resolutions") or any(type(v) is not int or v < 1 for v in data["resolutions"]):
        raise ValueError("invalid resolutions")
    if any(not isinstance(data.get("seed", {}).get(k), str) or not data["seed"][k] for k in ("class_type", "input")):
        raise ValueError("invalid seed binding")
    return data


BINDINGS = load_bindings()
# Compatibility exports for sidecar clients; all values come from the manifest.
LOAD_IMAGE_NODE = BINDINGS["image"]["node"]
RESOLUTION_NODE = BINDINGS["resolution"]["node"]
OUTPUT_NODE = BINDINGS["output"]["node"]
RESOLUTIONS = tuple(BINDINGS["resolutions"])
SEED_MAX = 4294967295
DEFAULT_MAX_IMAGE_BYTES = 32 * 1024 * 1024


class Refusal(Exception):
    def __init__(self, status: int, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


class PromptEnded(Refusal):
    """ComfyUI itself reported that the submitted prompt ended in error: nothing is left to cancel."""


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


def load_prompt(path: Path = PROMPT_PATH) -> dict[str, Any]:
    prompt = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(prompt, dict):
        raise ValueError("workflow template must be a JSON object")
    return prompt


def patch_prompt(prompt: dict[str, Any], *, image_name: str, seed: int | None, resolution: int | None, prefix: str, bindings: dict | None = None) -> dict[str, Any]:
    """A copy of the prompt for one request. A seed sets every sampler; a resolution sets the upsample stage."""
    bindings = bindings or BINDINGS
    patched = copy.deepcopy(prompt)
    patched[bindings["image"]["node"]]["inputs"][bindings["image"]["input"]] = image_name
    patched[bindings["output"]["node"]]["inputs"][bindings["output"]["input"]] = prefix
    if resolution is not None:
        patched[bindings["resolution"]["node"]]["inputs"][bindings["resolution"]["input"]] = resolution
    if seed is not None:
        for node in patched.values():
            if node.get("class_type") == bindings["seed"]["class_type"]:
                node["inputs"][bindings["seed"]["input"]] = seed
    return patched


def find_glb(outputs: dict[str, Any], *, bindings: dict | None = None) -> dict[str, str] | None:
    """The file entry (filename, subfolder, type) of the first .glb the output node lists, whatever key lists it."""
    for value in (outputs.get((bindings or BINDINGS)["output"]["node"]) or {}).values():
        for item in value if isinstance(value, list) else []:
            if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(".glb"):
                return {"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")}
    return None


class Pixal3DService:
    def __init__(self, base_url: str, prompt: dict[str, Any], client_factory: Callable[[], httpx.AsyncClient],
                 poll_interval: float = 2.0, timeout: float = 900.0, cleanup_timeout: float = 5.0, bindings: dict | None = None) -> None:
        self.bindings = bindings or BINDINGS
        self.base_url = base_url.rstrip("/")
        self.prompt = prompt
        self.client_factory = client_factory
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.cleanup_timeout = cleanup_timeout
        self._busy = False

    async def generate(self, data: bytes, seed: int | None, resolution: int | None) -> bytes:
        if self._busy:
            raise Refusal(503, "a model is already being made", retry_after=30)
        self._busy = True
        try:
            async with self.client_factory() as client:
                return await self._run(client, data, seed, resolution)
        except httpx.HTTPError as exc:
            raise Refusal(502, f"ComfyUI did not answer: {type(exc).__name__}") from exc
        finally:
            self._busy = False

    async def _run(self, client: httpx.AsyncClient, data: bytes, seed: int | None, resolution: int | None) -> bytes:
        base = self.base_url
        queue = upstream_object(await client.get(f"{base}/queue"), "queue")
        if queue.get("queue_running") or queue.get("queue_pending"):
            raise Refusal(503, "ComfyUI is busy", retry_after=30)
        name = f"pixal3d-{uuid.uuid4().hex}.png"
        up = await client.post(f"{base}/upload/image", files={"image": (name, data, "image/png")}, data={"overwrite": "true"})
        stored = upstream_object(up, "image upload").get("name") or name
        prefix = f"pixal3d/{uuid.uuid4().hex}"
        body = {"prompt": patch_prompt(self.prompt, image_name=stored, seed=seed, resolution=resolution, prefix=prefix, bindings=self.bindings)}
        sub = await client.post(f"{base}/prompt", json=body)
        prompt_id = upstream_object(sub, "prompt submit").get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise Refusal(502, "ComfyUI prompt submit reply has no prompt id")
        try:
            entry = await self._wait(client, prompt_id)
        except PromptEnded:
            raise
        except asyncio.CancelledError:
            await self._finish_cleanup(client, prompt_id)
            raise
        except Exception:
            # Every other failed wait leaves the submitted prompt possibly running.
            await self._finish_cleanup(client, prompt_id)
            raise
        ref = find_glb(entry.get("outputs") or {}, bindings=self.bindings)
        if ref is None:
            raise Refusal(502, "ComfyUI finished without a GLB")
        got = await client.get(f"{base}/view", params=ref)
        if got.status_code != 200:
            raise Refusal(502, f"ComfyUI would not return the GLB ({got.status_code})")
        if not got.content.startswith(b"glTF"):
            raise Refusal(502, "ComfyUI returned a result that was not a GLB model")
        return got.content

    async def _finish_cleanup(self, client: httpx.AsyncClient, prompt_id: str) -> None:
        # Keep the client open through cleanup, including repeated caller cancellation.
        cleanup = asyncio.create_task(self._cancel_prompt(client, prompt_id))
        cancellation = None
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError as exc:
                cancellation = exc
            except Exception:
                break
        try:
            cleanup.result()
        except (Exception, asyncio.CancelledError):
            logging.getLogger(__name__).warning("Prompt cleanup failed")
        if cancellation is not None:
            raise cancellation

    async def _wait(self, client: httpx.AsyncClient, prompt_id: str) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.timeout
        while True:
            history = upstream_object(await client.get(f"{self.base_url}/history/{prompt_id}"), "history")
            if prompt_id in history:
                entry = history[prompt_id]
                if not isinstance(entry, dict):
                    raise Refusal(502, "History prompt entry must be an object")
                status = entry.get("status", {})
                outputs = entry.get("outputs", {})
                if not isinstance(status, dict) or not isinstance(outputs, dict):
                    raise Refusal(502, "History status and outputs must be objects")
                if self.bindings["output"]["node"] in outputs and not isinstance(outputs[self.bindings["output"]["node"]], dict):
                    raise Refusal(502, "History output node must be an object")
                if status.get("status_str") == "error":
                    raise PromptEnded(502, "ComfyUI reported an error for the prompt")
                if status.get("completed") or entry.get("outputs"):
                    return entry
            if asyncio.get_running_loop().time() > deadline:
                raise Refusal(504, "ComfyUI did not finish in time")
            await asyncio.sleep(self.poll_interval)

    async def _cancel_prompt(self, client: httpx.AsyncClient, prompt_id: str) -> None:
        # Pending deletion is scoped. Even a prompt_id on /interrupt is unsafe:
        # the installed handler checks a snapshot before a global interrupt.
        logger = logging.getLogger(__name__)
        errors = []
        try:
            async with asyncio.timeout(self.cleanup_timeout):
                try:
                    response = await client.post(f"{self.base_url}/queue", json={"delete": [prompt_id]})
                    response.raise_for_status()
                except Exception as exc:
                    errors.append(type(exc).__name__)
                try:
                    queue = upstream_object(await client.get(f"{self.base_url}/queue"), "cleanup queue")
                    if any(isinstance(row, list) and len(row) > 1 and row[1] == prompt_id
                           for row in queue.get("queue_running", [])):
                        logger.warning("ComfyUI running prompt could not be cancelled safely")
                except Exception as exc:
                    errors.append(type(exc).__name__)
        except TimeoutError:
            errors.append("cleanup deadline exceeded")
        if errors:
            logger.warning("ComfyUI prompt cleanup failed: %s", ", ".join(errors))


def create_app(base_url: str, *, prompt: dict[str, Any] | None = None,
               client_factory: Callable[[], httpx.AsyncClient] | None = None,
               poll_interval: float = 2.0, timeout: float = 900.0, cleanup_timeout: float = 5.0,
               max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES, bindings: dict | None = None) -> FastAPI:
    if type(max_image_bytes) is not int or max_image_bytes < 1:
        raise ValueError("max_image_bytes must be a positive integer")
    service = Pixal3DService(base_url, prompt if prompt is not None else load_prompt(),
                             client_factory or (lambda: httpx.AsyncClient(timeout=60.0)), poll_interval, timeout, cleanup_timeout, bindings)
    app = FastAPI(title="Pixal3D ComfyUI service")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/generate")
    async def generate(image: UploadFile = File(...), seed: int | None = Form(None),
                       resolution: int | None = Form(None), bg_removal: str | None = Form(None)) -> Response:
        # bg_removal is accepted for contract compatibility; the Pixal3D prompt removes the background itself.
        with BytesIO() as buffered:
            total = 0
            while True:
                chunk = await image.read(min(64 * 1024, max_image_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_image_bytes:
                    return JSONResponse({"error": f"image exceeds the {max_image_bytes} byte limit"}, status_code=413)
                buffered.write(chunk)
            data = buffered.getvalue()
        if not data:
            return JSONResponse({"error": "the image is empty"}, status_code=400)
        if resolution is not None and resolution not in service.bindings["resolutions"]:
            return JSONResponse({"error": f"resolution must be one of {service.bindings["resolutions"]}"}, status_code=422)
        if seed is not None and not 0 <= seed <= SEED_MAX:
            return JSONResponse({"error": f"seed must be 0 to {SEED_MAX}"}, status_code=422)
        try:
            glb = await service.generate(data, seed, resolution)
        except Refusal as refusal:
            headers = {"Retry-After": str(refusal.retry_after)} if refusal.retry_after else None
            return JSONResponse({"error": refusal.message}, status_code=refusal.status, headers=headers)
        return Response(glb, media_type="model/gltf-binary")

    return app


def _app_from_env() -> FastAPI:
    return create_app(os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188"),
                      poll_interval=float(os.environ.get("PIXAL3D_POLL_SECONDS", "2")),
                      timeout=float(os.environ.get("PIXAL3D_TIMEOUT_SECONDS", "900")),
                      cleanup_timeout=float(os.environ.get("PIXAL3D_CLEANUP_TIMEOUT_SECONDS", "5")),
                      max_image_bytes=int(os.environ.get("PIXAL3D_MAX_IMAGE_BYTES", str(DEFAULT_MAX_IMAGE_BYTES))))


app = _app_from_env()

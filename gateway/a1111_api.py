"""A1111 / SD WebUI ``/sdapi/v1`` subset, mounted at host root.

SillyTavern's "SD WebUI (AUTOMATIC1111)" and "SD.Next" image sources call a fixed set
of routes (read from ``SillyTavern/src/endpoints/stable-diffusion.js``) and replace the
URL path, so the routes must sit at host root:

* ``GET /sdapi/v1/options``, ``POST /sdapi/v1/options`` (ping, current model, set model)
* ``GET /sdapi/v1/sd-models``, ``samplers``, ``schedulers``, ``upscalers``,
  ``latent-upscale-modes``, ``sd-vae``, ``progress``
* ``POST /sdapi/v1/txt2img``, ``POST /sdapi/v1/interrupt``

This module is a translator and nothing else. ``txt2img`` becomes a call to the image
route of a catalog workflow (the ``fields`` map in ``x-vividfish-workflows`` says where
each input goes) through ``A1111Deps.call``, so authentication, profile policy and
receipts stay in the core. A1111's "checkpoints" are the gateway's stable image aliases
and its synchronous ``image.generate`` workflows.

Honesty rules (see docs/VENDOR_FACADES.md): a field that changes what the image contains
and that the gateway cannot honour (hires fix, face restoration, ADetailer and other
scripts, LoRA prompt tags, styles, variation seeds) is rejected with a 422 that names it.
A tuning knob the chosen model has no use for (negative prompt on a CFG-distilled model,
sampler, ``clip_skip``) is dropped, and the response lists it in ``x-gateway-ignored-params``.

Auth: SillyTavern sends ``Authorization: Basic base64(auth)`` from its "Authentication"
box. The password (or the whole string, when it has no colon) is the gateway API key.
``Authorization: Bearer`` and ``x-api-key`` work too.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gateway.workflow_call import BuiltRequest, CallError, build_request, effective_schema, error_from, validate_inputs

DEFAULT_MODEL = "image"
MAX_IMAGES = 10
CORE_TIMEOUT_SECONDS = 600.0

# ComfyUI-style sampler names to the names A1111 users know. Names not listed pass through.
SAMPLER_DISPLAY = {
    "euler": "Euler", "euler_ancestral": "Euler a", "heun": "Heun", "lms": "LMS", "ddim": "DDIM",
    "dpmpp_2m": "DPM++ 2M", "dpmpp_2m_sde": "DPM++ 2M SDE", "dpmpp_sde": "DPM++ SDE", "uni_pc": "UniPC",
}
DEFAULT_SAMPLER, DEFAULT_SCHEDULER = "Default", "Automatic"

# txt2img fields that change the picture and that the gateway cannot honour: (is it set?, why not)
_UNSUPPORTED: dict[str, tuple[Callable[[Any], bool], str]] = {
    "enable_hr": (bool, "hires fix (enable_hr) is not supported"),
    "restore_faces": (bool, "face restoration (restore_faces) is not supported"),
    "alwayson_scripts": (lambda v: bool(v), "scripts such as ADetailer (alwayson_scripts) are not supported"),
    "script_name": (lambda v: bool(v), "scripts (script_name) are not supported"),
    "script_args": (lambda v: bool(v), "scripts (script_args) are not supported"),
    "styles": (lambda v: bool(v), "styles are not supported"),
    "refiner_checkpoint": (lambda v: bool(v), "a refiner checkpoint is not supported"),
    "tiling": (bool, "tiling is not supported on this route; use a seamless-tile workflow"),
    "subseed_strength": (lambda v: bool(v) and float(v) > 0, "variation seeds (subseed_strength) are not supported"),
    "seed_resize_from_w": (lambda v: bool(v) and int(v) > 0, "seed resizing is not supported"),
    "seed_resize_from_h": (lambda v: bool(v) and int(v) > 0, "seed resizing is not supported"),
}
# Fields with no effect on the result here. Dropped without a note.
_BENIGN = {
    "url", "auth", "send_images", "save_images", "do_not_save_grid", "do_not_save_samples",
    "override_settings_restore_afterwards", "hr_upscaler", "hr_scale", "hr_additional_modules",
    "hr_second_pass_steps", "denoising_strength", "subseed", "sampler_index", "eta", "n_iter",
    "batch_size", "model", "prompt", "negative_prompt", "steps", "cfg_scale", "width", "height", "seed",
    "sampler_name", "scheduler", "override_settings", "clip_skip", "enable_hr", "restore_faces",
}
_OVERRIDE_NOOPS = {"CLIP_stop_at_last_layers", "sd_vae", "forge_additional_modules"}
_LORA_TAGS = ("<lora:", "<lyco:", "<hypernet:")
_UNSUPPORTED_ROUTES = {
    "img2img": "img2img is not supported by this adapter; use POST /v1/images/edits",
    "extra-single-image": "upscaling is not supported by this adapter; use the seedvr2-upscale workflow",
    "extra-batch-images": "upscaling is not supported by this adapter; use the seedvr2-upscale workflow",
    "interrogate": "interrogate is not supported by this adapter",
    "png-info": "png-info is not supported by this adapter",
}


class A1111Error(Exception):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status, self.detail = status, detail


def validation_error(field_name: str, message: str) -> A1111Error:
    """FastAPI's 422 body, which is what A1111 answers for a bad field."""
    return A1111Error(422, [{"type": "value_error", "loc": ["body", field_name], "msg": message, "input": None}])


@dataclass
class A1111Deps:
    authenticate: Callable[[Request, str], Any]
    call: Callable[..., Awaitable[Any]]
    catalog: Callable[[], list[dict[str, Any]]]
    aliases: Callable[[], Mapping[str, Any]]
    owner_of: Callable[[Any], str | None]


@dataclass
class Session:
    """What A1111 keeps as global state, kept per API key."""

    model: str = DEFAULT_MODEL
    tasks: set[asyncio.Future] = field(default_factory=set)
    interrupted: bool = False


# ------------------------------------------------------------------- catalog


def _image_entries(deps: A1111Deps) -> list[dict[str, Any]]:
    return [e for e in deps.catalog() if e["intent"] == "image.generate" and (e.get("execution") or {}).get("sync")]


def _image_aliases(deps: A1111Deps) -> list[str]:
    stable = ((deps.aliases().get("operations") or deps.aliases()).get("images") or {}).get("stable") or []
    return [str(a["alias"]) for a in stable if a.get("alias")]


def model_titles(deps: A1111Deps) -> list[str]:
    entries = _image_entries(deps)
    titles = [a for a in _image_aliases(deps) if any(a in (e.get("aliases") or []) or a == e["short_id"] for e in entries)]
    for entry in entries:
        for name in [*(entry.get("aliases") or []), entry["short_id"]]:
            if name not in titles:
                titles.append(name)
    return titles


def resolve_model(deps: A1111Deps, title: str) -> dict[str, Any]:
    for entry in _image_entries(deps):
        if title in (entry["short_id"], entry["id"]) or title in (entry.get("aliases") or []):
            return entry
    raise validation_error("sd_model_checkpoint", f"unknown model {title!r}; see GET /sdapi/v1/sd-models")


def _enum_values(deps: A1111Deps, name: str) -> list[str]:
    seen: list[str] = []
    for entry in _image_entries(deps):
        for value in (effective_schema(entry)["properties"].get(name) or {}).get("enum") or []:
            if value not in seen:
                seen.append(value)
    return seen


def _sampler_to_gateway(name: str, allowed: list[str]) -> str | None:
    wanted = name.strip().lower()
    for value in allowed:
        if wanted in (value.lower(), SAMPLER_DISPLAY.get(value, "").lower(), value.lower().replace("_", " ")):
            return value
    return None


# ------------------------------------------------------------------- txt2img


def _check_unsupported(payload: Mapping[str, Any]) -> None:
    for name, (is_set, why) in _UNSUPPORTED.items():
        if name in payload and is_set(payload[name]):
            raise validation_error(name, why)
    for name in ("prompt", "negative_prompt"):
        text = str(payload.get(name) or "").lower()
        if any(tag in text for tag in _LORA_TAGS):
            raise validation_error(name, "LoRA, LyCORIS and hypernetwork prompt tags are not supported")
    overrides = payload.get("override_settings") or {}
    if not isinstance(overrides, Mapping):
        raise validation_error("override_settings", "must be an object")
    for key, value in overrides.items():
        if key == "sd_model_checkpoint":
            continue
        if key not in _OVERRIDE_NOOPS:
            raise validation_error("override_settings", f"setting {key!r} is not supported")
        if key == "sd_vae" and value not in (None, "", "Automatic", "None"):
            raise validation_error("override_settings", "VAE selection is not supported")


def _clamp(value: float, spec: Mapping[str, Any]) -> tuple[float, bool]:
    low, high = spec.get("minimum"), spec.get("maximum")
    clamped = min(max(value, low if low is not None else value), high if high is not None else value)
    return clamped, clamped != value


def map_payload(deps: A1111Deps, payload: Mapping[str, Any], title: str) -> tuple[dict[str, Any], dict[str, Any], list[str], int, int]:
    """(catalog entry, workflow inputs, ignored field names, image count, seed)."""
    _check_unsupported(payload)
    entry = resolve_model(deps, title)
    props = effective_schema(entry)["properties"]
    inputs: dict[str, Any] = {}
    ignored: list[str] = []

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise validation_error("prompt", "prompt is required")
    inputs["prompt"] = prompt

    negative = payload.get("negative_prompt")
    if negative:
        if "negative_prompt" in props:
            inputs["negative_prompt"] = negative
        else:
            ignored.append("negative_prompt")

    for name in ("width", "height"):
        value = payload.get(name)
        if value is not None:
            if name not in props:
                raise validation_error(name, f"model {title!r} does not take {name}")
            spec = props[name]
            if isinstance(value, bool) or not isinstance(value, int) or not spec.get("minimum", 0) <= value <= spec.get("maximum", value):
                raise validation_error(name, f"{name} must be an integer from {spec.get('minimum')} to {spec.get('maximum')}")
            inputs[name] = value

    steps = payload.get("steps")
    if steps is not None and "steps" in props:
        value, changed = _clamp(int(steps), props["steps"])
        inputs["steps"] = int(value)
        if changed:
            ignored.append(f"steps(clamped to {int(value)})")
    elif steps is not None:
        ignored.append("steps")

    cfg = payload.get("cfg_scale")
    if cfg is not None:
        name = next((n for n in ("guidance", "cfg_scale", "cfg") if n in props), None)
        if name:
            value, changed = _clamp(float(cfg), props[name])
            inputs[name] = value
            if changed:
                ignored.append(f"cfg_scale(clamped to {value})")
        else:
            ignored.append("cfg_scale")

    for key, input_name, allowed in (
        ("sampler_name", "sampler", _enum_values(deps, "sampler")),
        ("scheduler", "scheduler", _enum_values(deps, "scheduler")),
    ):
        raw = payload.get(key)
        if not raw or raw in (DEFAULT_SAMPLER, DEFAULT_SCHEDULER):
            continue
        mapped = _sampler_to_gateway(str(raw), allowed) if input_name in props else None
        if mapped:
            inputs[input_name] = mapped
        else:
            ignored.append(key)

    if payload.get("clip_skip") not in (None, 0, 1) or (payload.get("override_settings") or {}).get("CLIP_stop_at_last_layers") not in (None, 0, 1):
        ignored.append("clip_skip")

    batch, iterations = payload.get("batch_size") or 1, payload.get("n_iter") or 1
    count = int(batch) * int(iterations)
    if count < 1 or count > MAX_IMAGES:
        raise validation_error("batch_size", f"batch_size * n_iter must be from 1 to {MAX_IMAGES}")
    if count > 1:
        inputs["n"] = count

    seed = payload.get("seed")
    seed = -1 if seed is None else int(seed)
    if "seed" in props:
        if seed < 0:
            seed = random.SystemRandom().randrange(0, 2**32)
        inputs["seed"] = seed
    ignored += [k for k in payload if k not in _BENIGN and k not in _UNSUPPORTED]
    return entry, inputs, sorted(set(ignored)), count, seed


def info_json(payload: Mapping[str, Any], entry: Mapping[str, Any], inputs: Mapping[str, Any], seed: int, count: int) -> str:
    """The ``info`` string A1111 returns: generation parameters as JSON text."""
    width, height = inputs.get("width"), inputs.get("height")
    return json.dumps({
        "prompt": inputs["prompt"], "all_prompts": [inputs["prompt"]] * count,
        "negative_prompt": payload.get("negative_prompt") or "", "all_negative_prompts": [payload.get("negative_prompt") or ""] * count,
        "seed": seed, "all_seeds": [seed] if count == 1 and seed >= 0 else [],
        "width": width, "height": height, "sampler_name": payload.get("sampler_name") or DEFAULT_SAMPLER,
        "cfg_scale": payload.get("cfg_scale"), "steps": inputs.get("steps"), "batch_size": count,
        "restore_faces": False, "sd_model_name": entry["short_id"], "sd_model_hash": None, "sd_vae_name": None,
        "clip_skip": 1, "job_timestamp": time.strftime("%Y%m%d%H%M%S"), "index_of_first_image": 0,
        "infotexts": [], "styles": [], "extra_generation_params": {"gateway_workflow": entry["id"]},
        "is_using_inpainting_conditioning": False, "version": "vivid-fish-gateway",
    })


# ------------------------------------------------------------------- routing


def _basic_to_bearer(request: Request) -> Request:
    """A request whose ``Authorization: Basic`` carries the key as a bearer token."""
    value = request.headers.get("authorization", "")
    if not value.lower().startswith("basic "):
        return request
    try:
        decoded = base64.b64decode(value.split(None, 1)[1], validate=True).decode("utf-8")
    except (IndexError, binascii.Error, UnicodeDecodeError):
        return request
    key = decoded.split(":", 1)[1] if ":" in decoded else decoded
    scope = dict(request.scope)
    headers = [(k, v) for k, v in request.scope["headers"] if k.lower() != b"authorization"]
    headers.append((b"authorization", f"Bearer {key.strip()}".encode()))
    scope["headers"] = headers
    return Request(scope, request.receive)


def register_a1111_routes(app: FastAPI, deps: A1111Deps, *, prefix: str = "/sdapi/v1") -> None:
    sessions: dict[str, Session] = {}

    def error_response(exc: A1111Error) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    def guarded(handler: Callable[[Request, Any, Session], Awaitable[Response]]):
        async def route(request: Request) -> Response:
            request = _basic_to_bearer(request)
            auth = deps.authenticate(request, request.url.path)
            if isinstance(auth, Response):
                return auth
            session = sessions.setdefault(str(deps.owner_of(auth) or "anonymous"), Session())
            try:
                return await handler(request, auth, session)
            except A1111Error as exc:
                return error_response(exc)
            except (ValueError, TypeError) as exc:  # a field of the wrong type, e.g. steps="many"
                return error_response(A1111Error(422, f"invalid field value: {exc}"))
            except CallError as exc:
                return JSONResponse({"detail": str(exc)}, status_code=int(exc.details.get("http_status") or 400))
        return route

    def get(path: str, handler):
        app.add_api_route(f"{prefix}/{path}", guarded(handler), methods=["GET"], include_in_schema=False)

    async def options(request: Request, auth: Any, session: Session) -> Response:
        return JSONResponse({
            "sd_model_checkpoint": session.model, "sd_vae": "Automatic", "CLIP_stop_at_last_layers": 1,
            "samples_format": "png", "sd_checkpoint_hash": "", "send_seed": True,
        })

    async def set_options(request: Request, auth: Any, session: Session) -> Response:
        try:
            body = await request.json()
        except ValueError:
            raise A1111Error(422, "request body must be a JSON object") from None
        if not isinstance(body, Mapping):
            raise A1111Error(422, "request body must be a JSON object")
        unsupported = [k for k in body if k != "sd_model_checkpoint"]
        if unsupported:
            raise validation_error(unsupported[0], f"setting {unsupported[0]!r} cannot be changed through this adapter")
        if "sd_model_checkpoint" in body:
            title = str(body["sd_model_checkpoint"])
            resolve_model(deps, title)
            session.model = title
        return JSONResponse(None)

    async def sd_models(request: Request, auth: Any, session: Session) -> Response:
        return JSONResponse([{"title": t, "model_name": t, "hash": None, "sha256": None, "filename": t, "config": None}
                             for t in model_titles(deps)])

    async def samplers(request: Request, auth: Any, session: Session) -> Response:
        names = [DEFAULT_SAMPLER] + [SAMPLER_DISPLAY.get(v, v) for v in _enum_values(deps, "sampler")]
        return JSONResponse([{"name": n, "aliases": [], "options": {}} for n in names])

    async def schedulers(request: Request, auth: Any, session: Session) -> Response:
        names = [DEFAULT_SCHEDULER.lower()] + _enum_values(deps, "scheduler")
        return JSONResponse([{"name": n, "label": n.replace("_", " ").title(), "aliases": [], "default_rho": -1, "need_inner_model": False}
                             for n in names])

    async def upscalers(request: Request, auth: Any, session: Session) -> Response:
        return JSONResponse([{"name": "None", "model_name": None, "model_path": None, "model_url": None, "scale": 4}])

    async def empty_list(request: Request, auth: Any, session: Session) -> Response:
        return JSONResponse([])

    async def progress(request: Request, auth: Any, session: Session) -> Response:
        running = len(session.tasks)
        return JSONResponse({
            "progress": 0.0, "eta_relative": 0.0,
            "state": {"skipped": False, "interrupted": session.interrupted, "stopping_generation": False, "job": "",
                      "job_count": running, "job_timestamp": time.strftime("%Y%m%d%H%M%S"), "job_no": 0,
                      "sampling_step": 0, "sampling_steps": 0},
            "current_image": None, "textinfo": None,
        })

    async def interrupt(request: Request, auth: Any, session: Session) -> Response:
        session.interrupted = bool(session.tasks)
        for task in list(session.tasks):
            task.cancel()
        return JSONResponse(None)

    async def txt2img(request: Request, auth: Any, session: Session) -> Response:
        try:
            payload = await request.json()
        except ValueError:
            raise A1111Error(422, "request body must be a JSON object") from None
        if not isinstance(payload, Mapping):
            raise A1111Error(422, "request body must be a JSON object")
        title = str(payload.get("model") or (payload.get("override_settings") or {}).get("sd_model_checkpoint") or session.model)
        entry, inputs, ignored, count, seed = map_payload(deps, payload, title)
        route = entry["execution"]["sync"]
        built = await build_request(lambda *_: None, request, auth, entry, route, inputs, wrap_intent=None, response_format="b64_json")
        task = asyncio.ensure_future(_core_call(deps, request, built))
        session.tasks.add(task)
        session.interrupted = False
        try:
            resp = await asyncio.wait_for(task, CORE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            if session.interrupted and task.cancelled():  # /interrupt, not the client leaving
                return JSONResponse({"images": [], "parameters": _echo(payload), "info": json.dumps({"interrupted": True})})
            task.cancel()
            raise
        except asyncio.TimeoutError:
            raise A1111Error(504, "the gateway did not finish the image in time") from None
        finally:
            session.tasks.discard(task)
        if resp.status_code >= 400:
            raise error_from(resp)
        images = [item["b64_json"] for item in (resp.json().get("data") or []) if isinstance(item, Mapping) and item.get("b64_json")]
        if not images:
            raise A1111Error(502, "the gateway returned no inline image data")
        headers = {"x-gateway-ignored-params": ",".join(ignored)} if ignored else {}
        body = {"images": images if payload.get("send_images", True) else [], "parameters": _echo(payload),
                "info": info_json(payload, entry, inputs, seed, count)}
        return JSONResponse(body, headers=headers)

    def _unsupported(message: str):
        async def handler(request: Request, auth: Any, session: Session) -> Response:
            raise A1111Error(501, message)
        return handler

    get("options", options)
    app.add_api_route(f"{prefix}/options", guarded(set_options), methods=["POST"], include_in_schema=False)
    get("sd-models", sd_models)
    get("samplers", samplers)
    get("schedulers", schedulers)
    get("upscalers", upscalers)
    get("latent-upscale-modes", empty_list)
    get("sd-vae", empty_list)
    get("progress", progress)
    app.add_api_route(f"{prefix}/txt2img", guarded(txt2img), methods=["POST"], include_in_schema=False)
    app.add_api_route(f"{prefix}/interrupt", guarded(interrupt), methods=["POST"], include_in_schema=False)
    for name, message in _UNSUPPORTED_ROUTES.items():
        app.add_api_route(f"{prefix}/{name}", guarded(_unsupported(message)), methods=["POST"], include_in_schema=False)


async def _core_call(deps: A1111Deps, request: Request, built: BuiltRequest) -> Any:
    kwargs: dict[str, Any] = {}
    if built.json is not None:
        kwargs["json"] = built.json
    return await deps.call(request, built.method, built.path, **kwargs)


def _echo(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``parameters`` in the reply: the request, minus the SillyTavern-only url and auth."""
    return {k: v for k, v in payload.items() if k not in ("url", "auth")}

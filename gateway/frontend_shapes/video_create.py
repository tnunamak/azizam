"""Read a ``POST /v1/videos`` request against the ``video`` block of a manifest (CODE_TO_DATA_S7_VIDEO_MOVE.md 4.2 to 4.5).

The checks run in the order the old route made them, and each refusal has the old status and words. A request this module
does not serve yet (a storyboard, a chain, workflow extensions, a reference it cannot resolve without the main module) is
returned as a ``LegacyRoute`` with the body already read. Nothing here names a workflow, a model or an engine."""

from __future__ import annotations

import base64
import binascii
import json
import random
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import Response

from gateway import provenance, video_controls, video_grid, video_form, media_delivery
from gateway.frontend_shapes import _wire
from gateway.frontend_shapes._base import LegacyRoute
from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE, detect_unknown_top_level_fields
from gateway.video_extensions import STANDARD_VIDEO_CREATE_FIELDS
from gateway.workflow_runtime_types import FrontendRow, RegistrySnapshot, RequestSpool, RunRequest, WorkflowEntry

if TYPE_CHECKING:
    from gateway.frontend_shapes.shapes import OpenAIVideoCreate

LEGACY_CREATE = "videos.create"
SUBMIT_WAIT_S = 180.0              # how long a create holds for the engine to take the job; its own transport limits end it sooner
DEFAULT_SECONDS = 5.0
FORM_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")
MIN_BASE64_BYTES = 64               # a reference that is plain base64 must decode to more than this, as it always had to


class _BytesUpload:
    """A reference sent as text, shown to the spool as the file part it stands for."""

    def __init__(self, data: bytes, filename: str, content_type: str | None) -> None:
        self.filename, self.content_type, self._data = filename, content_type, data

    async def read(self, size: int = -1) -> bytes:
        data, self._data = self._data[:size] if size > 0 else self._data, self._data[size:] if size > 0 else b""
        return data


def refuse(status: int, message: str, *, param: str | None = None, code: str | None = None, kind: str = "invalid_request_error") -> Response:
    """The old route's error object: ``param`` and ``code`` are present only when set."""
    extra = {"param": param} if param else {}
    return _wire.error_response(status, message, kind=kind, code=code, **extra)


def short_name(entry: WorkflowEntry) -> str:
    """The workflow's id without the intent that starts it: the name the old messages used."""
    return entry.id.removeprefix(f"{entry.contract.get('intent')}.")


def _reference_bytes(value: Any) -> tuple[bytes, str, str | None] | None:
    """The bytes of a reference sent as a ``data:`` URI or plain base64; None for any other kind of reference."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("data:"):
        header, _, payload = text.partition(",")
        mime = header[5:].split(";", 1)[0].strip().lower() or None
        try:
            return base64.b64decode(payload, validate=False), f"input_reference.{(mime or 'image/png').rpartition('/')[2] or 'png'}", mime
        except (binascii.Error, ValueError):
            return None
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None
    return (data, "input_reference.png", None) if len(data) >= MIN_BASE64_BYTES else None


async def read_body(shape: OpenAIVideoCreate, request: Request) -> dict[str, Any] | Response:
    content_type = request.headers.get("content-type", "")
    if any(kind in content_type for kind in FORM_TYPES):
        try:
            return await video_form.read_video_form_body(request)
        except video_form.VideoFormParseError as exc:
            return refuse(400, str(exc), param=exc.param, code="unknown_parameter" if exc.param else None)
    try:
        parsed = json.loads(await request.body() or b"")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _wire.error_response(400, "Invalid JSON body")
    return parsed if isinstance(parsed, dict) else _wire.error_response(400, "Request body must be a JSON object")


def _workflow_block(body: Mapping[str, Any]) -> Mapping[str, Any]:
    extensions = body.get("extensions")
    block = extensions.get(WORKFLOW_EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else extensions
    if not isinstance(block, Mapping):
        return {}
    return {k: v for k, v in block.items() if k not in provenance.RESERVED_GATEWAY_CONTROLS}


def _beyond(seconds: Any, limit: float) -> bool:
    try:
        return seconds is not None and float(seconds) > limit
    except (TypeError, ValueError):
        return False


async def to_run_request(shape: OpenAIVideoCreate, request: Request, row: FrontendRow, snapshot: RegistrySnapshot, *,
                         spool: RequestSpool) -> RunRequest | LegacyRoute | Response:
    from gateway.frontend_shapes import shapes
    hooks = shapes.VIDEO_HOOKS or shapes.VideoHooks()
    caller = getattr(request.state, "caller", None)
    request.state.input_notes = _wire.begin_notes()
    request.state.receipt_facts = _wire.begin_facts()
    body = await read_body(shape, request)
    if isinstance(body, Response):
        return body
    unknown = detect_unknown_top_level_fields(body, STANDARD_VIDEO_CREATE_FIELDS)
    if unknown:
        name, reason = next(iter(unknown.items()))
        return refuse(400, reason, param=name, code="unknown_parameter")
    try:
        controls = video_controls.parse_video_controls(body)
    except video_controls.VideoControlError as exc:
        return refuse(400, str(exc), param=exc.param, code="invalid_video_control")
    refused = hooks.begin_delivery(caller, body)
    if refused is not None:
        return refused
    raw_prompt = body.get("prompt")
    if raw_prompt is not None and not isinstance(raw_prompt, str):
        return refuse(400, "prompt must be a string", param="prompt")
    prompt = (raw_prompt or "").strip()
    named = body.get("model") or shape.default_target()
    public_model = str(named) if named is not None else ""
    if not prompt and body.get("shots") is None:
        return refuse(400, "prompt is required")

    resolved = hooks.resolve_model(public_model)
    if isinstance(resolved, Response):
        return resolved
    chosen = _wire.choose_workflow(snapshot, row, target=resolved or None, supplied=["prompt"], not_found=lambda target: refuse(
        404, f"Model '{public_model}' is not available for video generation.", param="model", code="model_not_found"))
    if isinstance(chosen, Response):
        return chosen
    manifest = video_grid.video_facts(chosen, shapes.REFERENCE_KIND)
    reference = body.get("input_reference")
    if manifest is None or manifest.chain:
        return LegacyRoute(LEGACY_CREATE, body)
    variant = snapshot.get(manifest.reference_variant) if reference and manifest.reference_variant else None
    if variant is not None and video_grid.video_facts(variant, shapes.REFERENCE_KIND) is not None:
        chosen, manifest = variant, video_grid.video_facts(variant, shapes.REFERENCE_KIND)
    name = short_name(chosen)
    if manifest.needs_reference and not reference:
        return refuse(400, f"Model {name} is image-to-video and requires input_reference", param="input_reference")
    seconds = body.get("seconds")
    if body.get("shots") is not None or _beyond(seconds, manifest.max_seconds):
        if manifest.variants.get("long"):
            return LegacyRoute(LEGACY_CREATE, body)
        return refuse(400, f"Model {name} cannot render shots or clips longer than {manifest.max_seconds:g} s; use a long-video model",
                      param="shots" if body.get("shots") is not None else "seconds", code="long_video_not_supported")
    if (manifest.other_requires or manifest.select_subject or body.get("inputs") or _workflow_block(body)
            or (reference is not None and _reference_bytes(reference) is None)):
        return LegacyRoute(LEGACY_CREATE, body)

    try:
        seconds_value = float(seconds) if seconds is not None else min(DEFAULT_SECONDS, manifest.max_seconds)
    except (TypeError, ValueError):
        return refuse(400, "seconds must be a number")
    if seconds_value < manifest.min_seconds or seconds_value > manifest.max_seconds:
        return refuse(400, f"seconds must be between {manifest.min_seconds} and {manifest.max_seconds} for model {name}")
    if body.get("size") is not None and not isinstance(body["size"], str):
        return refuse(400, "size must be a string like 1280x720", param="size")      # the old route answered 500 with no body
    width, height = video_grid.clamp_size(body.get("size"), manifest)
    length = video_grid.seconds_to_length(seconds_value, manifest.fps, step=manifest.step, offset=manifest.offset,
                                          min_frames=manifest.min_frames, max_frames=manifest.max_frames)
    actual_seconds = round(length / manifest.fps, 4)
    try:
        seed = int(body["seed"]) if body.get("seed") is not None else random.randint(0, 2**32 - 1)
    except (TypeError, ValueError):
        seed = random.randint(0, 2**32 - 1)
    size = f"{width}x{height}"
    seconds_text = str(int(seconds_value)) if seconds_value.is_integer() else str(seconds_value)

    media: Mapping[str, Any] = {}
    inputs: dict[str, Any] = {"prompt": prompt, "seed": seed}
    roles = manifest.roles

    def put(role: str, value: Any) -> None:
        target = roles.get(role, role)
        if _wire.declared(chosen, target):
            inputs[target] = value

    put("width", width)
    put("height", height)
    put("length", length)
    put("frame_rate", int(manifest.fps) if float(manifest.fps).is_integer() else manifest.fps)
    if body.get("negative_prompt") and _wire.declared(chosen, "negative_prompt"):
        inputs["negative_prompt"] = body["negative_prompt"]
    if reference is not None:
        decoded = _reference_bytes(reference)
        target = roles.get(manifest.reference, "input_reference")
        staged = await _wire.stage_uploads(spool, [(target, _BytesUpload(*decoded))], {target: target})
        if isinstance(staged, Response):
            return staged
        media = staged
        if body.get("image_strength") is not None:
            try:
                put("image_strength", max(0.0, min(1.0, float(body["image_strength"]))))
            except (TypeError, ValueError):
                put("image_strength", 1.0)

    audio_off = controls.get(video_controls.AUDIO_CONTROL) is False
    context = media_delivery.current()
    now = int(time.time())
    parameters = {**(context.parameters if context else {}), "size": size, "seconds": seconds_text,
                  "actual_seconds": actual_seconds, "length_frames": length}
    facts: dict[str, Any] = {
        "model": public_model, "resolved_model": name, "size": size, "seconds": seconds_text, "actual_seconds": actual_seconds,
        "length_frames": length, "frame_rate": manifest.fps, "seed": seed,
        "provenance": provenance.build_provenance_record(
            kind=shape.wire_name, requested_model=public_model, resolved_model=name, workflow_id=chosen.id,
            backend=next((r.runtime_policy.get("backend") for r in row.routes if r.runtime_policy.get("backend")), None),
            seed=seed, parameters=parameters, request_id="{wire_id}", created_at=now)}
    if audio_off:
        facts[video_controls.AUDIO_CONTROL] = False
    if context is not None and context.requested_mode:
        facts["metadata_mode"] = context.requested_mode
    options: dict[str, Any] = {"size": size, "seconds_frames": length, "frame_rate": manifest.fps}
    if audio_off:
        options[video_controls.AUDIO_CONTROL] = False
    request.state.video_facts = facts
    request.state.video_prompt = prompt
    extensions = body.get("extensions")
    from gateway.frontend_shapes._base import IDEMPOTENCY_HEADER
    return RunRequest(
        workflow_id=chosen.id, inputs=inputs, media=media, options=options, facts=facts,
        idempotency_key=request.headers.get(IDEMPOTENCY_HEADER) or None, frontend_id=row.id,
        failure_text=row.job_failure_text, retries=row.job_retries,
        extensions=dict(extensions) if isinstance(extensions, Mapping) else {},
        prompt_retention=hooks.prompt_retention(caller, body), submit_wait_s=SUBMIT_WAIT_S)

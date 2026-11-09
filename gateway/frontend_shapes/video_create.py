"""Read a ``POST /v1/videos`` request against the ``video`` block of a manifest (CODE_TO_DATA_S7_VIDEO_MOVE.md 4.2 to 4.5).

The checks run in the order the old route made them, and each refusal has the old status and words. A request this module
does not serve yet (a storyboard, a chain, workflow extensions, a reference it cannot resolve without the main module) is
returned as a ``LegacyRoute`` with the body already read. Nothing here names a workflow, a model or an engine."""

from __future__ import annotations

import base64
import binascii
import json
import math
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import Response

from gateway import provenance, video_controls, video_grid, video_form, media_delivery
from gateway.frontend_shapes import _wire
from gateway.frontend_shapes._base import LegacyRoute, coerce
from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE, detect_unknown_top_level_fields
from gateway.workflow_run import WIRE_ID_PLACEHOLDER
from gateway.video_extensions import STANDARD_VIDEO_CREATE_FIELDS
from gateway.workflow_runtime_types import FactRef, FrontendRow, RegistrySnapshot, RequestSpool, RunRequest, WorkflowEntry

if TYPE_CHECKING:
    from gateway.frontend_shapes.shapes import OpenAIVideoCreate

LEGACY_CREATE = "videos.create"
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
    """The name the old route showed for a workflow in its answers and messages: the manifest's own name (its id without the
    intent) under its capability, kept as a spelling of its own (not read back from the id) so a caller's text does not move."""
    own = entry.id.removeprefix(f"{entry.contract.get('intent')}.")
    return f"{entry.contract.get('capability')}-{own}"


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
    """The parsed body, read once: the hand-off check and the run request both ask, and an upload stream reads only once."""
    cached = getattr(request.state, "video_body", None)
    if cached is None:
        cached = request.state.video_body = await _parse_body(request)
    return cached


async def _parse_body(request: Request) -> dict[str, Any] | Response:
    content_type = request.headers.get("content-type", "")
    if any(kind in content_type for kind in FORM_TYPES):
        try:
            return await video_form.read_video_form_body(request)
        except video_form.VideoFormParseError as exc:
            return refuse(400, str(exc), param=exc.param, code="unknown_parameter" if exc.param else None)
    try:
        parsed = json.loads(await request.body() or b"")
    except (ValueError, UnicodeDecodeError, RecursionError):
        return Response(content=json.dumps({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}),
                        status_code=400, media_type="application/json")
    return parsed if isinstance(parsed, dict) else _wire.error_response(400, "Request body must be a JSON object")


def _workflow_block(body: Mapping[str, Any]) -> Mapping[str, Any]:
    extensions = body.get("extensions")
    block = extensions.get(WORKFLOW_EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else extensions
    if not isinstance(block, Mapping):
        return {}
    return {k: v for k, v in block.items() if k not in provenance.RESERVED_GATEWAY_CONTROLS}


def _is_asset(entry: WorkflowEntry, name: str) -> bool:
    return ((entry.contract.get("inputs") or {}).get(name) or {}).get("type") == "asset"


def _beyond(seconds: Any, limit: float) -> bool:
    try:
        return seconds is not None and float(seconds) > limit
    except (TypeError, ValueError):
        return False


Picked = tuple[WorkflowEntry, "video_grid.VideoManifest", str, str]


def _old_spelling(snapshot: RegistrySnapshot, row: FrontendRow, name: str) -> str:
    """A caller may still send a workflow under the key the old route knew, its short name with the capability in front
    (``<capability>-<short id>``). It names the same workflow as the short id, so it resolves there when it names nothing itself."""
    if not name or snapshot.names.owner("workflow", name) is not None or snapshot.names.owner("workflow_alias", name) is not None:
        return name
    for entry in _wire.candidates(snapshot, row, None):
        if name == short_name(entry):
            return entry.id
    return name


def select(shape: OpenAIVideoCreate, body: Mapping[str, Any], row: FrontendRow, snapshot: RegistrySnapshot,
           hooks: Any) -> Picked | LegacyRoute | Response:
    """The workflow a create runs, or the reason it does not run here: a refusal, or a hand-off to the old pipeline for what the
    data path does not serve yet (a chain, workflow extensions, a reference it cannot read). Reads nothing but the body and the
    registry, so the row can ask it before it opens a receipt."""
    named = body.get("model") or shape.default_target()
    public_model = str(named) if named is not None else ""
    resolved = hooks.resolve_model(public_model)
    if isinstance(resolved, Response):
        return resolved
    chosen = _wire.choose_workflow(snapshot, row, target=_old_spelling(snapshot, row, resolved) or None, supplied=["prompt"],
                                   not_found=lambda target: refuse(404, f"Model '{public_model}' is not available for video generation.",
                                                                   param="model", code="model_not_found"))
    if isinstance(chosen, Response):
        return chosen
    from gateway.frontend_shapes import shapes
    kind = shapes.REFERENCE_KIND
    manifest = video_grid.video_facts(chosen)
    reference = body.get("input_reference")
    if manifest is None or manifest.chain or manifest.length is None:
        return LegacyRoute(LEGACY_CREATE, body)
    variant = snapshot.get(manifest.variants[kind]) if reference and manifest.variants.get(kind) else None
    if variant is not None and video_grid.video_facts(variant) is not None:
        chosen, manifest = variant, video_grid.video_facts(variant)
    if manifest.length is None or manifest.grid is None or manifest.size is None:
        return LegacyRoute(LEGACY_CREATE, body)
    name = short_name(chosen)
    if kind in manifest.requires and not reference:
        return refuse(400, f"Model {name} is image-to-video and requires input_reference", param="input_reference")
    seconds = body.get("seconds")
    limit = manifest.length.max_seconds
    shots = body.get("shots") is not None
    if shots or (_beyond(seconds, limit) and manifest.variants.get("long")):
        if manifest.variants.get("long"):
            return LegacyRoute(LEGACY_CREATE, body)
        return refuse(400, f"Model {name} cannot render shots or clips longer than {limit:g} s; use a long-video model",
                      param="shots", code="long_video_not_supported")
    block = _workflow_block(body)
    if ((manifest.requires - {kind, "extensions"}) or manifest.select_subject or body.get("inputs")
            or ("extensions" in manifest.requires and not block)
            or (reference is not None and _reference_bytes(reference) is None)
            or any(not _wire.declared(chosen, key) or _is_asset(chosen, key) for key in block)):
        return LegacyRoute(LEGACY_CREATE, body)
    for missing in _wire.asset_input_names(chosen):      # a required picture the body leaves out, before any other problem
        if missing != kind and _wire.entry_required(chosen, missing) is True and missing not in block:
            return refuse(400, f"{missing} is required for this model", param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{missing}")
    return chosen, manifest, name, public_model


async def legacy_route(shape: OpenAIVideoCreate, request: Request, row: FrontendRow, snapshot: RegistrySnapshot) -> LegacyRoute | None:
    """Whether the old pipeline serves this create. The row asks before it enforces the route policy or opens a receipt, so the
    old handler alone owns its policy check, its one receipt and its request id. Any refusal is left to ``to_run_request``."""
    from gateway.frontend_shapes import shapes
    body = await read_body(shape, request)
    if isinstance(body, Response):
        return None
    picked = select(shape, body, row, snapshot, shapes.VIDEO_HOOKS or shapes.VideoHooks())
    return picked if isinstance(picked, LegacyRoute) else None


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
    if not prompt and body.get("shots") is None:
        return refuse(400, "prompt is required")
    picked = select(shape, body, row, snapshot, hooks)
    if isinstance(picked, (Response, LegacyRoute)):
        return picked
    chosen, manifest, name, public_model = picked
    reference, seconds = body.get("input_reference"), body.get("seconds")

    grid, limits = manifest.grid, manifest.length
    try:
        seconds_value = float(seconds) if seconds is not None else min(limits.default_seconds or DEFAULT_SECONDS, limits.max_seconds)
    except (TypeError, ValueError):
        return refuse(400, "seconds must be a number")
    if not math.isfinite(seconds_value):
        return refuse(400, "seconds must be a number")
    if seconds_value < limits.min_seconds or seconds_value > limits.max_seconds:
        return refuse(400, f"seconds must be between {float(limits.min_seconds)} and {float(limits.max_seconds)} for model {name}")
    if body.get("size") is not None and not isinstance(body["size"], str):
        return refuse(400, "size must be a string like 1280x720", param="size")      # the old route answered 500 with no body
    width, height = video_grid.resolve_size(body.get("size"), manifest.size)
    roles = manifest.roles
    block = _workflow_block(body)
    # The frame rate the run uses is resolved once: the workflow's own, or the typed one the caller sent as an extension. The
    # frame count, the recorded duration, the engine input, the facts and the options all take this one value.
    rate_key = roles.get("frame_rate", "frame_rate")
    frame_rate = float(grid.fps)
    if block.get(rate_key) is not None:
        try:
            frame_rate = float(coerce(chosen, rate_key, block[rate_key]))
        except (TypeError, ValueError):
            frame_rate = math.nan
        if not math.isfinite(frame_rate) or frame_rate <= 0:
            return refuse(400, f"{rate_key} must be a positive number", param=rate_key)
    length = video_grid.frame_count(seconds_value, frame_rate, grid)
    chosen_length = block.get(roles.get("length", "length"))
    if chosen_length is not None:                    # the manifest's own length input (frames) wins over seconds, as it always did
        try:
            length = int(chosen_length)
        except (TypeError, ValueError):
            return refuse(400, f"{roles['length']} must be an integer", param=roles["length"])
    actual_seconds = round(length / frame_rate, 4)
    # An omitted seed stays out of the request: the core draws it once, inside the claimed transaction, so two requests that
    # differ only by a missing seed are one request for the idempotency key. The drawn value reaches the facts by a ref.
    seed: int | None = None
    if body.get("seed") is not None:
        try:
            seed = int(body["seed"])
        except (TypeError, ValueError):
            seed = None
    size = f"{width}x{height}"
    seconds_text = str(int(seconds_value)) if seconds_value.is_integer() else str(seconds_value)

    media: Mapping[str, Any] = {}
    inputs: dict[str, Any] = {"prompt": prompt}
    if seed is not None:
        inputs["seed"] = seed

    def put(role: str, value: Any) -> None:
        target = roles.get(role, role)
        if _wire.declared(chosen, target):
            inputs[target] = value

    put("width", width)
    put("height", height)
    put("length", length)
    put("frame_rate", int(frame_rate) if frame_rate.is_integer() else frame_rate)
    for key, value in block.items():
        if _wire.declared(chosen, key) and key not in (roles.get("length"), roles.get("width"), roles.get("height"), rate_key):
            inputs[key] = coerce(chosen, key, value)
    if body.get("negative_prompt") and _wire.declared(chosen, "negative_prompt"):
        inputs["negative_prompt"] = body["negative_prompt"]
    if reference is not None:
        decoded = _reference_bytes(reference)
        target = roles.get(shapes.REFERENCE_KIND, "input_reference")
        refused_reference = hooks.check_reference(caller, decoded[0], chosen.contract, target)
        if refused_reference is not None:
            return refused_reference
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
        "length_frames": length, "frame_rate": frame_rate,
        "provenance": provenance.build_provenance_record(
            kind=shape.wire_name, requested_model=public_model, resolved_model=name, workflow_id=f"{chosen.contract.get('intent')}.{name}",
            backend=next((r.runtime_policy.get("backend") for r in row.routes if r.runtime_policy.get("backend")), None),
            seed=seed, parameters=parameters, request_id=WIRE_ID_PLACEHOLDER, created_at=now)}
    if seed is not None:
        facts["seed"] = seed
    refs: list[FactRef] = []
    if seed is None:
        refs += [FactRef(("seed",), f"generated:{roles.get('seed', 'seed')}"), FactRef(("provenance", "seed"), f"generated:{roles.get('seed', 'seed')}")]
    if audio_off:
        facts[video_controls.AUDIO_CONTROL] = False
    if context is not None and context.requested_mode:
        facts["metadata_mode"] = context.requested_mode
    options: dict[str, Any] = {"size": size, "seconds_frames": length, "frame_rate": frame_rate}
    if audio_off:
        options[video_controls.AUDIO_CONTROL] = False
    request.state.video_facts = facts
    request.state.video_prompt = prompt
    extensions = body.get("extensions")
    from gateway.frontend_shapes._base import IDEMPOTENCY_HEADER
    return RunRequest(
        workflow_id=chosen.id, inputs=inputs, media=media,
        sources={name: () for name in media},      # the reference is temporary: the Library keeps no note of it, as the old route did
        options=options, facts=facts,
        idempotency_key=request.headers.get(IDEMPOTENCY_HEADER) or None, frontend_id=row.id,
        failure_text=row.job_failure_text, retries=row.job_retries,
        extensions=dict(extensions) if isinstance(extensions, Mapping) else {},
        prompt_retention=hooks.prompt_retention(caller, body), fact_refs=tuple(refs))

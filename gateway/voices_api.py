"""HTTP routes for custom voices and consent recordings (OpenAI shape, plus a few additions).

OpenAI's public spec defines ``POST /v1/audio/voices`` and the ``/v1/audio/voice_consents`` CRUD.
It defines no list, get or delete for voices, so those, ``POST /v1/audio/voices/{id}`` (rename) and
``GET /v1/audio/voices/{id}/sample`` are additions. See ``custom-voices-DESIGN.md`` in the
speech-tiers notes and ``docs/LOCAL_CLIENT_RECIPES.md``.

The module is independent of ``proxy.py``: authentication, policy, the quality probe and the voice
design call arrive through :class:`VoiceApiDeps`, so the routes are tested with fakes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from gateway.custom_voices import (
    VOICE_ID_RE,
    VoiceError,
    VoiceStore,
    validate_labels,
    validate_name,
)
from gateway.voice_design import DesignPromptError, design_instruct
from gateway.voice_audio import ProcessedSample, SampleLimits, VoiceSampleError, process_sample

AUDIO_EXTENSIONS = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/ogg": "ogg", "audio/aac": "aac", "audio/flac": "flac", "audio/webm": "webm", "audio/mp4": "m4a",
    "audio/x-m4a": "m4a", "video/mp4": "m4a", "video/webm": "webm",
}
DEFAULT_DESIGN_SCRIPT = (
    "Hello, and welcome. I'm glad you could join us today. Let me tell you a little about myself: "
    "I enjoy clear conversations, good questions, and taking the time to explain things well. "
    "If there is anything you would like to know, just ask, and we will work through it together."
)
_EXTENSION_RE = re.compile(r"^[a-z0-9]{1,5}$")
MIN_PAGE, MAX_PAGE = 1, 100
FORM_OVERHEAD_BYTES = 256 * 1024
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8}){0,3}$")


@dataclass
class VoiceApiDeps:
    store: Callable[[], VoiceStore]
    sample_limits: Callable[[], SampleLimits]
    authenticate: Callable[[Request, str], Any]                 # AuthContext or an error Response
    enforce_policy: Callable[[Any, str], Response | None]
    owner_of: Callable[[Any], str | None]
    probe_quality: Callable[[Path], Awaitable[dict[str, Any] | None]]
    design_voice: Callable[[str, str, int], Awaitable[bytes]]   # (engine instruction, script, seed) -> WAV bytes
    design_settings: Callable[[], Mapping[str, Any] | None]     # the design engine's ``voice_design`` data
    builtin_voice_names: Callable[[], frozenset[str]]           # lower-case names a custom voice may not take
    compatible_engines: Callable[[], list[str]]
    quality_warn_below: Callable[[], float]


def error_response(status: int, code: str, message: str, *, param: str | None = None) -> JSONResponse:
    error: dict[str, Any] = {"message": message, "type": "invalid_request_error", "code": code}
    if param is not None:
        error["param"] = param
    return JSONResponse({"error": error}, status_code=status)


def voice_view(meta: Mapping[str, Any], engines: list[str]) -> dict[str, Any]:
    """The public voice object: OpenAI's fields first, our additions after."""
    view = {k: meta.get(k) for k in ("object", "id", "type", "name", "created_at")}
    for key in ("description", "labels", "duration_seconds", "consent", "seed", "prompt", "design_instruct", "quality", "warnings"):
        view[key] = meta.get(key)
    view["compatible_engines"] = list(engines)
    return view


def list_page(items: list[dict[str, Any]], has_more: bool, view: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    data = [view(item) for item in items]
    return {
        "object": "list",
        "data": data,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "has_more": has_more,
    }


def parse_page_args(request: Request) -> tuple[str | None, int]:
    raw = request.query_params.get("limit")
    try:
        limit = 20 if raw in (None, "") else int(raw)
    except ValueError:
        raise VoiceError(400, "invalid_limit", "limit must be an integer between 1 and 100.", param="limit") from None
    if not MIN_PAGE <= limit <= MAX_PAGE:
        raise VoiceError(400, "invalid_limit", "limit must be an integer between 1 and 100.", param="limit")
    return request.query_params.get("after") or None, limit


async def _read_upload(form: Any, field: str, max_bytes: int, code: str = "sample_too_large") -> tuple[bytes, str]:
    upload = form.get(field)
    if upload is None or isinstance(upload, str) or not hasattr(upload, "read"):
        raise VoiceError(400, "missing_file", f"{field} (a file) is required.", param=field)
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise VoiceError(400, code, f"{field} is larger than {max_bytes // (1024 * 1024)} MiB.", param=field)
    mime = (getattr(upload, "content_type", "") or "").split(";")[0].strip().lower()
    extension = AUDIO_EXTENSIONS.get(mime)
    if extension is None:
        name = getattr(upload, "filename", "") or ""
        suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        extension = suffix if _EXTENSION_RE.match(suffix) else "bin"
    return data, extension


def _text_field(form: Any, field: str) -> str | None:
    value = form.get(field)
    return value if isinstance(value, str) else None


def register_voice_routes(app: FastAPI, deps: VoiceApiDeps) -> None:
    """Add the voice and consent routes straight to ``app``.

    Not an ``APIRouter``: current FastAPI wraps an included router in an opaque object, which hides
    these routes from route enumeration (``app.routes``) that the alias and registry tests rely on.
    """

    def guard_sync(request: Request, policy_path: str) -> tuple[Any, str | None] | Response:
        auth = deps.authenticate(request, policy_path)
        if isinstance(auth, Response):
            return auth
        denied = deps.enforce_policy(auth, policy_path)
        if denied is not None:
            return denied
        return auth, deps.owner_of(auth)

    async def guard(request: Request, policy_path: str) -> tuple[Any, str | None] | Response:
        # Authentication reads SQLite; keep it off the event loop.
        return await asyncio.to_thread(guard_sync, request, policy_path)

    def view(meta: Mapping[str, Any]) -> dict[str, Any]:
        return voice_view(meta, deps.compatible_engines())

    async def form_of(request: Request, max_bytes: int):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes + FORM_OVERHEAD_BYTES:
            raise VoiceError(413, "sample_too_large", f"The upload is larger than {max_bytes // (1024 * 1024)} MiB.")
        if "multipart/form-data" not in request.headers.get("content-type", ""):
            raise VoiceError(400, "multipart_required", "This request must be multipart/form-data.")
        return await request.form(max_part_size=1024 * 1024)

    # ---- consents ----------------------------------------------------------
    async def create_consent(request: Request):
        gate = await guard(request, "/v1/audio/voice_consents")
        if isinstance(gate, Response):
            return gate
        _, owner = gate
        form = None
        try:
            limits = deps.sample_limits()
            form = await form_of(request, limits.max_bytes)
            name = _text_field(form, "name")
            language = _text_field(form, "language")
            if not name or not name.strip():
                raise VoiceError(400, "invalid_name", "name is required.", param="name")
            if not language or not LANGUAGE_RE.match(language):
                raise VoiceError(400, "invalid_language", "language is required, as a BCP 47 tag such as en-US.", param="language")
            recording, extension = await _read_upload(form, "recording", limits.max_bytes, "recording_too_large")
            if not recording:
                raise VoiceError(400, "invalid_audio_sample", "recording is empty.", param="recording")
            meta = deps.store().create_consent(owner, name=name.strip()[:256], language=language, recording=recording, extension=extension)
            return JSONResponse(meta)
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)
        finally:
            if form is not None:
                await form.close()

    async def list_consents(request: Request):
        gate = await guard(request, "/v1/audio/voice_consents")
        if isinstance(gate, Response):
            return gate
        try:
            after, limit = parse_page_args(request)
            items, more = await asyncio.to_thread(deps.store().list_consents, gate[1], after=after, limit=limit)
            return JSONResponse(list_page(items, more, dict))
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)

    async def get_consent(request: Request, consent_id: str):
        gate = await guard(request, "/v1/audio/voice_consents/{consent_id}")
        if isinstance(gate, Response):
            return gate
        try:
            return JSONResponse(await asyncio.to_thread(deps.store().get_consent, gate[1], consent_id))
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)

    async def rename_consent(request: Request, consent_id: str):
        gate = await guard(request, "/v1/audio/voice_consents/{consent_id}")
        if isinstance(gate, Response):
            return gate
        try:
            body = await _json_body(request)
            name = body.get("name")
            if not isinstance(name, str) or not name.strip():
                raise VoiceError(400, "invalid_name", "name is required.", param="name")
            return JSONResponse(deps.store().rename_consent(gate[1], consent_id, name.strip()[:256]))
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)

    async def delete_consent(request: Request, consent_id: str):
        gate = await guard(request, "/v1/audio/voice_consents/{consent_id}")
        if isinstance(gate, Response):
            return gate
        try:
            deps.store().delete_consent(gate[1], consent_id)
            return JSONResponse({"id": consent_id, "object": "audio.voice_consent", "deleted": True})
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)

    # ---- voices ------------------------------------------------------------
    async def _json_body(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            raise VoiceError(400, "invalid_json", "The request body is not valid JSON.") from None
        if not isinstance(body, dict):
            raise VoiceError(400, "invalid_json", "The request body must be a JSON object.")
        return body

    async def _finish(owner: str | None, meta: dict[str, Any]) -> dict[str, Any]:
        """Quality probe after creation. A voice that cannot be probed is still created."""
        store = deps.store()
        quality: dict[str, Any] | None
        try:
            probe = await deps.probe_quality(store.reference_path(owner, meta["id"]))
        except Exception:  # noqa: BLE001 - the probe is advisory
            probe = None
        warnings = list(meta.get("warnings") or [])
        if probe is None or "error" in probe:
            quality = {"speaker_similarity": None, "encoder": None, "engine": None, "status": "unchecked",
                       "reason": (probe or {}).get("error", "probe_failed")}
            warnings.append("quality_check_unavailable")
        else:
            similarity = float(probe["speaker_similarity"])
            quality = {
                "speaker_similarity": similarity,
                "encoder": probe.get("encoder"),
                "engine": probe.get("engine"),
                "status": "ok" if similarity >= deps.quality_warn_below() else "low",
            }
            if quality["status"] == "low":
                warnings.append("low_speaker_similarity")
        return store.update_voice(owner, meta["id"], {"quality": quality, "warnings": warnings})

    async def create_voice(request: Request):
        gate = await guard(request, "/v1/audio/voices")
        if isinstance(gate, Response):
            return gate
        _, owner = gate
        form = None
        try:
            limits = deps.sample_limits()
            is_json = "application/json" in request.headers.get("content-type", "")
            if is_json:
                fields: Mapping[str, Any] = await _json_body(request)
            else:
                form = await form_of(request, limits.max_bytes)
                fields = {key: form.get(key) for key in ("type", "name", "consent", "prompt", "script_hint", "model", "seed", "description", "labels", "remove_background_noise")}
            voice_type = fields.get("type") or "audio_sample"
            if voice_type not in ("audio_sample", "prompt"):
                raise VoiceError(400, "invalid_type", "type must be audio_sample or prompt.", param="type")
            name = validate_name(fields.get("name"), reserved=deps.builtin_voice_names())
            store = deps.store()
            labels_raw = fields.get("labels")
            if isinstance(labels_raw, str):
                try:
                    labels_raw = json.loads(labels_raw)
                except ValueError:
                    raise VoiceError(400, "invalid_labels", "labels must be a JSON object.", param="labels") from None
            labels = validate_labels(labels_raw, store.limits)
            description = fields.get("description")
            if description is not None and (not isinstance(description, str) or len(description) > 1000):
                raise VoiceError(400, "invalid_description", "description must be a string of at most 1000 characters.", param="description")
            denoise = str(fields.get("remove_background_noise") or "").lower() in ("1", "true", "yes")

            if voice_type == "audio_sample":
                if form is None:
                    raise VoiceError(400, "multipart_required", "Creating a voice from an audio sample requires multipart/form-data.")
                consent = fields.get("consent") or None
                if consent is not None and not isinstance(consent, str):
                    raise VoiceError(400, "invalid_consent", "consent must be a consent recording id.", param="consent")
                original, extension = await _read_upload(form, "audio_sample", limits.max_bytes)
                processed = await asyncio.to_thread(process_sample, original, limits, denoise=denoise)
                meta = store.create_voice(
                    owner, name=name, voice_type="audio_sample", reference_wav=processed.wav,
                    duration_seconds=processed.duration_seconds, original=original, original_extension=extension,
                    consent_id=consent, description=description, labels=labels,
                    measurements=processed.measurements(), warnings=processed.warnings,
                )
            else:
                meta = await _create_prompt_voice(owner, name, fields, labels, description, limits, denoise)
            return JSONResponse(view(await _finish(owner, meta)))
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)
        except VoiceSampleError as exc:
            return error_response(400, exc.code, exc.message, param=exc.param)
        finally:
            if form is not None:
                await form.close()

    async def _create_prompt_voice(owner, name, fields, labels, description, limits, denoise) -> dict[str, Any]:
        prompt = fields.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 2000:
            raise VoiceError(400, "invalid_prompt", "prompt must be 1-2000 characters of text.", param="prompt")
        script = fields.get("script_hint")
        if script is not None and (not isinstance(script, str) or not script.strip() or len(script) > 2000):
            raise VoiceError(400, "invalid_script_hint", "script_hint must be 1-2000 characters of text.", param="script_hint")
        script = script.strip() if script else DEFAULT_DESIGN_SCRIPT
        model = fields.get("model") or "auto"
        if model != "auto":
            raise VoiceError(400, "unsupported_model", "model must be auto; this gateway has one voice design model.", param="model")
        seed_raw = fields.get("seed")
        try:
            seed = int(seed_raw) if seed_raw not in (None, "") else int.from_bytes(os.urandom(3), "big")
        except (TypeError, ValueError):
            raise VoiceError(400, "invalid_seed", "seed must be an integer.", param="seed") from None
        try:
            instruct, ignored = design_instruct(prompt, deps.design_settings())
        except DesignPromptError as exc:
            raise VoiceError(400, "invalid_prompt", str(exc), param="prompt") from exc
        wav = await deps.design_voice(instruct, script, seed)
        try:
            processed: ProcessedSample = await asyncio.to_thread(process_sample, wav, limits, denoise=False)
        except VoiceSampleError as exc:
            raise VoiceError(400, "script_too_short", f"The designed voice was too short to clone ({exc.message}); use a longer script_hint.", param="script_hint") from exc
        return deps.store().create_voice(
            owner, name=name, voice_type="prompt", reference_wav=processed.wav, duration_seconds=processed.duration_seconds,
            original=None, original_extension="wav", consent_id=None, description=description, labels=labels,
            measurements=processed.measurements(),
            warnings=[*processed.warnings, *([f"design_terms_ignored: {', '.join(ignored)}"] if ignored else [])],
            seed=seed, prompt=prompt.strip(), transcript=script, design_instruct=instruct,
        )

    async def get_voice(request: Request, voice_id: str):
        gate = await guard(request, "/v1/audio/voices/{voice_id}")
        if isinstance(gate, Response):
            return gate
        try:
            return JSONResponse(view(await asyncio.to_thread(deps.store().get_voice, gate[1], voice_id)))
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)

    async def update_voice(request: Request, voice_id: str):
        gate = await guard(request, "/v1/audio/voices/{voice_id}")
        if isinstance(gate, Response):
            return gate
        try:
            body = await _json_body(request)
            changes: dict[str, Any] = {}
            if "name" in body:
                changes["name"] = validate_name(body["name"], reserved=deps.builtin_voice_names())
            if "description" in body:
                if body["description"] is not None and (not isinstance(body["description"], str) or len(body["description"]) > 1000):
                    raise VoiceError(400, "invalid_description", "description must be a string of at most 1000 characters.", param="description")
                changes["description"] = body["description"]
            if "labels" in body:
                changes["labels"] = validate_labels(body["labels"], deps.store().limits)
            if not changes:
                raise VoiceError(400, "nothing_to_update", "Send name, description or labels.")
            return JSONResponse(view(deps.store().update_voice(gate[1], voice_id, changes)))
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)

    async def delete_voice(request: Request, voice_id: str):
        gate = await guard(request, "/v1/audio/voices/{voice_id}")
        if isinstance(gate, Response):
            return gate
        try:
            deps.store().delete_voice(gate[1], voice_id)
            return JSONResponse({"id": voice_id, "object": "audio.voice", "deleted": True})
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)

    async def voice_sample(request: Request, voice_id: str):
        gate = await guard(request, "/v1/audio/voices/{voice_id}")
        if isinstance(gate, Response):
            return gate
        try:
            store = deps.store()
            store.get_voice(gate[1], voice_id)
            return FileResponse(store.reference_path(gate[1], voice_id), media_type="audio/wav")
        except VoiceError as exc:
            return error_response(exc.status, exc.code, exc.message, param=exc.param)

    for prefix in ("/v1", "/openai/v1"):
        app.add_api_route(f"{prefix}/audio/voice_consents", create_consent, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{prefix}/audio/voice_consents", list_consents, methods=["GET"], include_in_schema=False)
        app.add_api_route(f"{prefix}/audio/voice_consents/{{consent_id}}", get_consent, methods=["GET"], include_in_schema=False)
        app.add_api_route(f"{prefix}/audio/voice_consents/{{consent_id}}", rename_consent, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{prefix}/audio/voice_consents/{{consent_id}}", delete_consent, methods=["DELETE"], include_in_schema=False)
        app.add_api_route(f"{prefix}/audio/voices", create_voice, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{prefix}/audio/voices/{{voice_id}}", get_voice, methods=["GET"], include_in_schema=False)
        app.add_api_route(f"{prefix}/audio/voices/{{voice_id}}", update_voice, methods=["POST"], include_in_schema=False)
        app.add_api_route(f"{prefix}/audio/voices/{{voice_id}}", delete_voice, methods=["DELETE"], include_in_schema=False)
        app.add_api_route(f"{prefix}/audio/voices/{{voice_id}}/sample", voice_sample, methods=["GET"], include_in_schema=False)


def custom_voice_id(value: Any) -> str | None:
    """The custom voice id in a speech request's ``voice`` value, or None for a built-in name.

    An object ``{"id": "voice_..."}`` is always custom (a malformed id is an error for the caller to
    report). A string is custom only when it is exactly a ``voice_<24 hex>`` id, so every other
    string keeps its built-in meaning.
    """
    if isinstance(value, Mapping):
        ident = value.get("id")
        return ident if isinstance(ident, str) else ""
    if isinstance(value, str) and VOICE_ID_RE.match(value.strip()):
        return value.strip()
    return None

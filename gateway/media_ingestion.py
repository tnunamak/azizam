"""Index generated media that completed inside a synchronous route.

The route already stores bytes in the asset cache. This module only records a
durable media-job projection so Library can list the generated item with its
receipt and provenance.
"""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from fastapi import Request
from starlette.responses import Response

from gateway import media_delivery, media_jobs, provenance, receipt_store
from gateway.receipts import RouteReceipt

_KIND_BY_PREFIX = {"img": "image", "aud": "audio", "vid": "video"}
_MEDIA_TYPE_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}


def index_sync_assets(
    *,
    request_id: str,
    intent: str,
    assets: Iterable[Mapping[str, Any]],
    workflow: str | None = None,
    backend: str | None = None,
    app_profile: str | None = None,
    app_id: str | None = None,
    owner_key_hash: str | None = None,
    api_key_name: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    path=None,
) -> media_jobs.MediaJob | None:
    """Create the succeeded media-job row for a synchronous media response.

    Returns ``None`` when no valid asset handles are present. The metadata bag
    must stay safe for ``media_jobs``: no prompts, request bodies, or media
    bytes.
    """
    safe_assets = [_normalize_asset(asset) for asset in assets]
    safe_assets = [asset for asset in safe_assets if asset is not None]
    if not request_id or not intent or not safe_assets:
        return None
    return media_jobs.create(
        media_jobs.MediaJob(
            request_id=request_id,
            intent=intent,
            workflow=workflow,
            backend=backend,
            status=media_jobs.STATUS_SUCCEEDED,
            progress=100,
            assets=safe_assets,
            app_profile=app_profile,
            app_id=app_id,
            owner_key_hash=owner_key_hash,
            api_key_name=api_key_name,
            metadata=dict(metadata or {}),
        ),
        path=media_jobs.DB_PATH if path is None else path,
    )


async def start_receipt(request: Request, auth_context: Any, *, intent: str, profile: Any = None) -> RouteReceipt:
    """Begin a route receipt for synchronous audio/media routes."""
    identity = getattr(auth_context, "identity", None)
    profile_id = _profile_id(profile) or _profile_id(getattr(identity, "profile", None))
    body = await _safe_request_json(request)
    receipt = RouteReceipt(
        protocol="openai.audio",
        intent=intent,
        path=str(request.url.path),
        app_profile=profile_id,
        app_id=getattr(identity, "app_id", None) if identity is not None else None,
        owner_key_hash=getattr(identity, "key_hash", None) if identity is not None else None,
        api_key_name=getattr(identity, "name", None) if identity is not None else None,
        requested_model=_body_string(body, "model"),
        stream=_body_stream_requested(body),
    )
    receipt.set_policy_decision("allowed", reason="route_authorized", source="runtime_route_policy")
    visibility = getattr(getattr(getattr(identity, "profile", None), "status_visibility", None), "level", None)
    if visibility:
        receipt.visibility = str(visibility)
    receipt_store.begin(receipt, path=receipt_store.DB_PATH)
    media_delivery.begin(profile_default=None, receipt=receipt)
    return receipt


def finish_receipt(receipt: RouteReceipt, *, status_code: int) -> RouteReceipt:
    """Finalize a begun receipt using the HTTP result."""
    if receipt.status in {"succeeded", "failed", "cancelled"}:
        return receipt
    status = "succeeded" if 200 <= int(status_code) < 400 else "failed"
    receipt.metadata["status_code"] = int(status_code)
    receipt.metadata["terminal_reason"] = f"http_{int(status_code)}"
    receipt.finish(status)
    receipt_store.finalize(receipt, path=receipt_store.DB_PATH)
    return receipt


def finish_response(receipt: RouteReceipt, response: Response) -> Response:
    """Finish receipt when a regular response is ready, or when a stream drains."""
    response.headers.setdefault("x-request-id", receipt.request_id)
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        finish_receipt(receipt, status_code=response.status_code)
        return response

    receipt.stream = True
    response.body_iterator = _finish_streaming_iterator(iterator, receipt, response.status_code)
    return response


async def _finish_streaming_iterator(source: Any, receipt: RouteReceipt, status_code: int):
    try:
        async for chunk in source:
            yield chunk
    except asyncio.CancelledError:
        receipt.metadata["terminal_reason"] = "cancelled"
        if receipt.status not in {"succeeded", "failed", "cancelled"}:
            receipt.finish("cancelled")
            receipt_store.finalize(receipt, path=receipt_store.DB_PATH)
        raise
    except BaseException:
        if receipt.status not in {"succeeded", "failed", "cancelled"}:
            finish_receipt(receipt, status_code=500)
        raise
    else:
        finish_receipt(receipt, status_code=status_code)


def index_receipt_assets(receipt: RouteReceipt, assets: Iterable[Mapping[str, Any]]) -> media_jobs.MediaJob | None:
    """Index media assets against a route receipt and attach the job id."""
    job = index_sync_assets(
        request_id=receipt.request_id,
        intent=receipt.intent,
        assets=assets,
        workflow=receipt.resolved_workflow or receipt.requested_workflow,
        backend=receipt.backend,
        app_profile=receipt.app_profile,
        app_id=receipt.app_id,
        owner_key_hash=receipt.owner_key_hash,
        api_key_name=receipt.api_key_name,
        metadata={"mode": "sync_media_route"},
        path=media_jobs.DB_PATH,
    )
    if job is not None:
        receipt.job_id = job.job_id
    return job


def index_video_delivery(
    *,
    video_id: str,
    asset_id: str,
    filename: str,
    media_type: str,
    record: Mapping[str, Any] | None,
) -> media_jobs.MediaJob | None:
    """Index a synchronous public video content delivery as a Library asset."""
    safe_record = record if isinstance(record, Mapping) else {}
    workflow = _safe_video_workflow(safe_record)
    job = index_sync_assets(
        request_id=video_id,
        intent="video.generate",
        assets=[{
            "asset_id": asset_id,
            "kind": "video",
            "media_type": media_type,
            "filename": Path(filename).name,
        }],
        workflow=workflow,
        backend=_safe_video_backend(safe_record),
        app_profile=_safe_string(safe_record.get("app_profile")),
        app_id=_safe_string(safe_record.get("app_id")),
        owner_key_hash=_safe_string(safe_record.get("owner_key_hash")),
        api_key_name=_safe_string(safe_record.get("api_key_name")),
        metadata={"mode": "sync_video_delivery"},
        path=media_jobs.DB_PATH,
    )
    return job


def _safe_video_backend(record: Mapping[str, Any]) -> str | None:
    provenance_record = record.get("provenance")
    if isinstance(provenance_record, Mapping):
        backend = provenance_record.get("backend")
        if isinstance(backend, str) and backend:
            return backend
    backend = record.get("backend")
    return backend if isinstance(backend, str) and backend else None


def _safe_video_workflow(record: Mapping[str, Any]) -> str | None:
    provenance_record = record.get("provenance")
    if isinstance(provenance_record, Mapping):
        workflow = provenance_record.get("workflow_id")
        if isinstance(workflow, str) and workflow:
            return workflow
    resolved = record.get("resolved_model")
    if isinstance(resolved, str) and resolved:
        return f"video.generate.{resolved}"
    return None


def _safe_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def store_audio_response(
    content: bytes,
    media_type: str | None,
    receipt: RouteReceipt,
    *,
    store_asset: Callable[..., str],
) -> list[dict[str, Any]]:
    """Store and index a buffered audio response for a synchronous route."""
    if not content:
        return []
    if not _supported_audio_media_type(media_type):
        return []
    normalized_media_type = _normalize_audio_media_type(media_type)
    suffix = _audio_suffix(normalized_media_type)
    stored = bytes(content)
    if _is_opus_media_type(media_type) and not stored.startswith(b"OggS"):
        return []
    if suffix == ".wav" and _is_raw_pcm_media_type(media_type):
        stored = _pcm16_to_wav(stored)
        normalized_media_type = "audio/wav"
    provenance_record = _audio_provenance_record(receipt, normalized_media_type)
    stored = media_delivery.attach_provenance(stored, suffix, provenance_record)
    asset_id = store_asset(
        stored,
        kind="aud",
        suffix=suffix,
        owner_key_hash=receipt.owner_key_hash,
    )
    asset = {
        "asset_id": asset_id,
        "kind": "audio",
        "media_type": normalized_media_type,
        "filename": f"audio{suffix}",
        "size_bytes": len(stored),
    }
    index_receipt_assets(receipt, [asset])
    return [asset]



async def _safe_request_json(request: Request) -> Mapping[str, Any] | None:
    try:
        value = await request.json()
    except Exception:
        return None
    return value if isinstance(value, Mapping) else None


def _body_string(body: Mapping[str, Any] | None, key: str) -> str | None:
    if not isinstance(body, Mapping):
        return None
    value = body.get(key)
    return value if isinstance(value, str) and value else None


def _body_stream_requested(body: Mapping[str, Any] | None) -> bool:
    if not isinstance(body, Mapping):
        return False
    if body.get("stream") is True:
        return True
    value = body.get("stream_format")
    return isinstance(value, str) and value.lower() == "sse"

def _profile_id(profile: Any) -> str | None:
    if profile is None:
        return None
    if isinstance(profile, str):
        return profile
    raw = getattr(profile, "id", None)
    return str(raw) if raw else None


def _normalize_asset(asset: Mapping[str, Any]) -> dict[str, Any] | None:
    asset_id = str(asset.get("asset_id") or asset.get("id") or "").strip()
    if not _valid_asset_id(asset_id):
        return None
    out: dict[str, Any] = {
        "asset_id": asset_id,
        "kind": _kind_for_asset(asset_id, asset),
        "media_type": _media_type_for_asset(asset_id, asset),
    }
    for key in ("width", "height", "duration_s", "duration", "seed", "index", "poster_asset_id", "filename", "size_bytes"):
        value = asset.get(key)
        if value is not None:
            out[key] = value
    return out


def _valid_asset_id(asset_id: str) -> bool:
    if "_" not in asset_id:
        return False
    prefix, digest = asset_id.split("_", 1)
    return (
        prefix in _KIND_BY_PREFIX
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    )


def _kind_for_asset(asset_id: str, asset: Mapping[str, Any]) -> str:
    raw = str(asset.get("kind") or "")
    if raw in {"image", "video", "audio"}:
        return raw
    return _KIND_BY_PREFIX.get(asset_id.split("_", 1)[0], "asset")


def _media_type_for_asset(asset_id: str, asset: Mapping[str, Any]) -> str:
    raw = str(asset.get("media_type") or asset.get("content_type") or "")
    if "/" in raw:
        return raw
    suffix = PurePosixPath(str(asset.get("filename") or "")).suffix.lower()
    if not suffix:
        suffix = {"img": ".png", "aud": ".wav", "vid": ".mp4"}.get(asset_id.split("_", 1)[0], "")
    return _MEDIA_TYPE_BY_SUFFIX.get(suffix, "application/octet-stream")


_AUDIO_SUFFIX_BY_MEDIA_TYPE = {
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/wave": ".wav",
    "audio/pcm": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/x-m4a": ".m4a",
}

_BROWSER_MEDIA_TYPE_BY_SUFFIX = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


def _normalize_audio_media_type(media_type: str | None) -> str:
    value = str(media_type or "").split(";", 1)[0].strip().lower()
    if value in _AUDIO_SUFFIX_BY_MEDIA_TYPE:
        suffix = _AUDIO_SUFFIX_BY_MEDIA_TYPE[value]
        return _BROWSER_MEDIA_TYPE_BY_SUFFIX.get(suffix, value)
    if value.startswith("audio/"):
        return value
    return "application/octet-stream"


def _audio_suffix(media_type: str | None) -> str:
    value = str(media_type or "").split(";", 1)[0].strip().lower()
    return _AUDIO_SUFFIX_BY_MEDIA_TYPE.get(value, ".bin")


def _supported_audio_media_type(media_type: str | None) -> bool:
    value = str(media_type or "").split(";", 1)[0].strip().lower()
    return value in _AUDIO_SUFFIX_BY_MEDIA_TYPE or _is_raw_pcm_media_type(media_type)


def _is_opus_media_type(media_type: str | None) -> bool:
    value = str(media_type or "").split(";", 1)[0].strip().lower()
    return value == "audio/opus"


def _is_raw_pcm_media_type(media_type: str | None) -> bool:
    value = str(media_type or "").lower()
    return value.split(";", 1)[0].strip() == "audio/pcm"


def _pcm16_to_wav(content: bytes, *, sample_rate: int = 24000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(content)
    return out.getvalue()


def _audio_provenance_record(receipt: RouteReceipt, media_type: str) -> dict[str, Any]:
    return provenance.build_provenance_record(
        receipt=receipt,
        kind="audio",
        workflow_id=receipt.resolved_workflow or receipt.requested_workflow,
        backend=receipt.backend,
        parameters={"media_type": media_type},
    )

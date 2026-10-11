"""Helpers for durable ``video.generate`` media jobs.

This module owns only shape decisions that are useful before route wiring:
safe metadata, normalized video assets, and OpenAI/Sora-shaped status payloads.
It deliberately avoids scheduling, persistence, and backend calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from gateway import media_jobs

INTENT_VIDEO_GENERATE = "video.generate"
VIDEO_OBJECT = "video"
LEGACY_VIDEO_OBJECT = "video.generation"

OPENAI_STATUS_BY_INTERNAL = {
    media_jobs.STATUS_QUEUED: "queued",
    media_jobs.STATUS_RUNNING: "in_progress",
    media_jobs.STATUS_SUCCEEDED: "completed",
    media_jobs.STATUS_FAILED: "failed",
    media_jobs.STATUS_CANCELED: "failed",
}

_VIDEO_EXTENSIONS = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}
_OUTPUT_KEYS = ("videos", "gifs", "images")


@dataclass(frozen=True)
class VideoJobSpec:
    """Safe, prompt-free parameters for a durable video job record."""

    model: str
    workflow: str
    size: str
    seconds: float
    actual_seconds: float | None = None
    seed: int | None = None
    request_hash: str | None = None
    idempotency: str | None = None


def video_job_metadata(spec: VideoJobSpec) -> dict[str, Any]:
    """Return metadata safe to store on ``media_jobs.MediaJob``.

    Do not include raw prompts, request bodies, input images, or backend
    responses here; ``media_jobs`` will enforce the same safety vocabulary.
    """

    metadata: dict[str, Any] = {
        "mode": "async_video_generation",
        "model": spec.model,
        "size": spec.size,
        "seconds": float(spec.seconds),
    }
    if spec.actual_seconds is not None:
        metadata["actual_seconds"] = round(float(spec.actual_seconds), 4)
    if spec.seed is not None:
        metadata["seed"] = int(spec.seed)
    if spec.request_hash:
        metadata["request_hash"] = spec.request_hash
    if spec.idempotency:
        metadata["idempotency"] = spec.idempotency
    media_jobs.MediaJob(
        intent=INTENT_VIDEO_GENERATE,
        workflow=spec.workflow,
        metadata=metadata,
    )
    return metadata


def new_video_generate_job(
    spec: VideoJobSpec,
    *,
    backend: str | None = None,
    request_id: str = "",
    app_profile: str | None = None,
    api_key_name: str | None = None,
    parent_request_id: str | None = None,
) -> media_jobs.MediaJob:
    """Build a queued durable job for a ``video.generate`` request."""

    return media_jobs.MediaJob(
        intent=INTENT_VIDEO_GENERATE,
        workflow=spec.workflow,
        backend=backend,
        request_id=request_id,
        app_profile=app_profile,
        api_key_name=api_key_name,
        parent_request_id=parent_request_id,
        metadata=video_job_metadata(spec),
    )


def media_type_for_video_filename(filename: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    return _VIDEO_EXTENSIONS.get(suffix, "application/octet-stream")


def normalize_video_asset(
    *,
    filename: str,
    asset_id: str | None = None,
    url: str | None = None,
    content_url: str | None = None,
    media_type: str | None = None,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the safe asset shape stored on a durable video job."""

    name = str(filename or "").strip()
    if not name:
        raise ValueError("filename is required")
    normalized: dict[str, Any] = {
        "kind": "video",
        "filename": PurePosixPath(name).name,
        "media_type": media_type or media_type_for_video_filename(name),
    }
    if asset_id:
        normalized["asset_id"] = str(asset_id)
    if url:
        normalized["url"] = str(url)
    if content_url:
        normalized["content_url"] = str(content_url)
    if source:
        source_payload = {
            key: str(value)
            for key, value in source.items()
            if value is not None and key in {"node_id", "output_key", "type", "subfolder"}
        }
        if source_payload:
            normalized["source"] = source_payload
    media_jobs.MediaJob(intent=INTENT_VIDEO_GENERATE, assets=[normalized])
    return normalized


def extract_comfyui_video_assets(history_job: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Extract normalized video assets from a ComfyUI history job.

    The ComfyUI-specific name is intentional: this is a binding adapter around
    the backend-agnostic ``normalize_video_asset`` shape.
    """

    outputs = history_job.get("outputs") if isinstance(history_job, Mapping) else None
    if not isinstance(outputs, Mapping):
        return []

    assets: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for node_id, node_output in outputs.items():
        if not isinstance(node_output, Mapping):
            continue
        for output_key in _OUTPUT_KEYS:
            entries = node_output.get(output_key) or []
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                filename = str(entry.get("filename") or "").strip()
                if not filename:
                    continue
                media_type = media_type_for_video_filename(filename)
                if not media_type.startswith("video/"):
                    continue
                subfolder = str(entry.get("subfolder") or "")
                output_type = str(entry.get("type") or "output")
                dedupe_key = (filename, subfolder, output_type)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                assets.append(
                    normalize_video_asset(
                        filename=filename,
                        media_type=media_type,
                        source={
                            "node_id": node_id,
                            "output_key": output_key,
                            "subfolder": subfolder,
                            "type": output_type,
                        },
                    )
                )
    return assets


def video_status_event(
    *,
    status: str,
    progress: int,
    step: int | None = None,
    total: int | None = None,
    phase: str | None = None,
    asset_count: int | None = None,
) -> dict[str, Any]:
    """Build the safe event payload appended for visible job progress."""

    if status not in media_jobs.ALL_STATUSES:
        raise ValueError(f"status must be one of {sorted(media_jobs.ALL_STATUSES)}")
    payload: dict[str, Any] = {
        "status": status,
        "progress": _progress(progress),
    }
    if step is not None:
        payload["step"] = max(0, int(step))
    if total is not None:
        payload["total"] = max(0, int(total))
    if phase:
        payload["phase"] = str(phase)
    if asset_count is not None:
        payload["asset_count"] = max(0, int(asset_count))
    return payload


def video_generation_payload(
    job: Mapping[str, Any],
    *,
    public_id: str | None = None,
    include_assets: bool = False,
) -> dict[str, Any]:
    """Project a durable job into the route-level OpenAI ``video`` shape."""

    metadata = job.get("metadata") if isinstance(job.get("metadata"), Mapping) else {}
    assets = job.get("assets") if isinstance(job.get("assets"), list) else []
    internal_status = str(job.get("status") or media_jobs.STATUS_QUEUED)
    payload: dict[str, Any] = {
        "id": public_id or job.get("job_id"),
        "object": VIDEO_OBJECT,
        "azizam_object": LEGACY_VIDEO_OBJECT,
        "model": metadata.get("model") or job.get("workflow"),
        "status": openai_video_status(internal_status),
        "azizam_status": internal_status,
        "progress": _progress(job.get("progress", 0)),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        **{name: job.get(name) for name in (*media_jobs.PHASE_FIELDS, "attempt_number", "previous_attempts")},
        "completed_at": job.get("updated_at") if internal_status == media_jobs.STATUS_SUCCEEDED else None,
        "expires_at": metadata.get("expires_at"),
        "prompt": metadata.get("prompt"),
        "size": metadata.get("size") or "1280x720",
        "seconds": metadata.get("seconds") or "5",
        "remixed_from_video_id": metadata.get("remixed_from_video_id"),
        "error": None,
    }
    if job.get("workflow") is not None:
        payload["azizam_resolved_model"] = job["workflow"]
    for key in ("size", "seconds", "actual_seconds"):
        if key in metadata:
            payload[key] = metadata[key]
    if job.get("error"):
        error = job["error"]
        payload["error"] = (
            dict(error)
            if isinstance(error, Mapping)
            else {"code": "video_generation_failed", "message": str(error)}
        )
    if include_assets and assets:
        payload["assets"] = [dict(asset) for asset in assets if isinstance(asset, Mapping)]
    elif job.get("status") == media_jobs.STATUS_SUCCEEDED and assets:
        first = next((asset for asset in assets if isinstance(asset, Mapping)), None)
        if first:
            content_url = first.get("content_url") or first.get("url")
            if content_url:
                payload["content_url"] = content_url
    return {key: value for key, value in payload.items() if value is not None or key in {
        "completed_at", "expires_at", "prompt", "remixed_from_video_id", "error",
        *media_jobs.PHASE_FIELDS, "attempt_number", "previous_attempts",
    }}


def openai_video_status(status: str) -> str:
    """Map internal durable-job status to the OpenAI video public status."""

    return OPENAI_STATUS_BY_INTERNAL.get(status, status)


def _progress(value: Any) -> int:
    try:
        progress = int(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"progress must be an int 0..100, got {value!r}") from e
    if progress < 0 or progress > 100:
        raise ValueError(f"progress must be an int 0..100, got {value!r}")
    return progress

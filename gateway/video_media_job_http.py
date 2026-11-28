"""HTTP-facing adapters for durable OpenAI video media jobs.

This module builds job and result shapes, with random request tokens. Route
wiring can call them before/after backend execution without teaching
``proxy.py`` about durable media-job internals.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any, Mapping

from gateway import media_jobs, video_media_jobs

DEFAULT_VIDEO_SIZE = "1280x720"
DEFAULT_VIDEO_SECONDS = 5.0


@dataclass(frozen=True)
class VideoRequestContext:
    """Route facts that are safe to attach to a durable job."""

    workflow: str
    backend: str | None = None
    request_id: str = ""
    idempotency_key: str | None = None
    app_profile: str | None = None
    api_key_name: str | None = None
    parent_request_id: str | None = None


@dataclass(frozen=True)
class VideoExecutionResult:
    """Prompt-free facts learned while executing a durable video job."""

    status: str
    progress: int | None = None
    assets: list[Mapping[str, Any]] = field(default_factory=list)
    error: str | None = None
    actual_seconds: float | None = None
    phase: str | None = None


def job_from_openai_video_request(
    body: Mapping[str, Any],
    context: VideoRequestContext,
) -> media_jobs.MediaJob:
    """Build a queued durable job from a ``POST /v1/videos`` JSON body."""

    model = _string(body.get("model")) or context.workflow
    spec = video_media_jobs.VideoJobSpec(
        model=model,
        workflow=context.workflow,
        size=_string(body.get("size")) or DEFAULT_VIDEO_SIZE,
        seconds=_float(body.get("seconds"), default=DEFAULT_VIDEO_SECONDS),
        seed=_optional_int(body.get("seed")),
        request_hash=secrets.token_hex(32),
        idempotency="header" if context.idempotency_key else "request",
    )
    return video_media_jobs.new_video_generate_job(
        spec,
        backend=context.backend,
        request_id=context.request_id,
        app_profile=context.app_profile,
        api_key_name=context.api_key_name,
        parent_request_id=context.parent_request_id,
    )


def update_from_execution_result(result: VideoExecutionResult) -> dict[str, Any]:
    """Return kwargs suitable for ``media_jobs.update_status``."""

    update: dict[str, Any] = {
        "status": result.status,
        "progress": result.progress,
        "error": result.error,
        "add_assets": [dict(asset) for asset in result.assets],
    }
    metadata_patch: dict[str, Any] = {}
    if result.actual_seconds is not None:
        metadata_patch["actual_seconds"] = round(float(result.actual_seconds), 4)
    if metadata_patch:
        update["metadata_patch"] = metadata_patch
    return {key: value for key, value in update.items() if value not in (None, [])}


def event_from_execution_result(result: VideoExecutionResult) -> dict[str, Any]:
    """Build the safe progress event payload for a visible execution update."""

    return video_media_jobs.video_status_event(
        status=result.status,
        progress=result.progress if result.progress is not None else _default_progress(result.status),
        phase=result.phase,
        asset_count=len(result.assets) if result.assets else None,
    )


def completed_asset_from_stored_video(
    *,
    filename: str,
    asset_id: str,
    content_url: str,
    media_type: str | None = None,
) -> dict[str, Any]:
    """Build the persisted asset shape for a completed stored ``vid_*`` asset."""

    handle = _string(asset_id)
    if not handle.startswith("vid_"):
        raise ValueError("video asset_id must start with 'vid_'")
    signed_url = _string(content_url)
    if not signed_url:
        raise ValueError("content_url is required")
    return video_media_jobs.normalize_video_asset(
        filename=filename,
        asset_id=handle,
        content_url=signed_url,
        media_type=media_type,
    )


def content_response_from_job(job: Mapping[str, Any]) -> dict[str, Any]:
    """Project a completed durable job to the ``GET /v1/videos/{id}/content`` shape."""

    if job.get("status") != media_jobs.STATUS_SUCCEEDED:
        raise ValueError("video content is only available for succeeded jobs")
    assets = job.get("assets") if isinstance(job.get("assets"), list) else []
    first = next((asset for asset in assets if isinstance(asset, Mapping)), None)
    if first is None:
        raise ValueError("succeeded video job has no content asset")
    content_url = first.get("content_url") or first.get("url")
    if not content_url:
        raise ValueError("succeeded video job has no signed content URL")
    return {
        "id": job.get("job_id"),
        "object": "video.content",
        "media_type": first.get("media_type") or "application/octet-stream",
        "asset_id": first.get("asset_id"),
        "url": content_url,
        "filename": first.get("filename"),
    }


def generation_response_from_job(
    job: Mapping[str, Any],
    *,
    public_id: str | None = None,
) -> dict[str, Any]:
    """Project a job to the OpenAI video generation polling response."""

    return video_media_jobs.video_generation_payload(job, public_id=public_id)


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _float(value: Any, *, default: float) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _default_progress(status: str) -> int:
    if status == media_jobs.STATUS_SUCCEEDED:
        return 100
    if status == media_jobs.STATUS_QUEUED:
        return 0
    return 0

"""The stored clip a remix or an edit starts from (CODE_TO_DATA S7, section 4.6).

``resolve_source`` turns the wire id of a finished video into the clip's asset and the facts a later run needs. What the
request asked for (``request_facts``) names the model and the resolved model. What the clip *is* (frame rate, size, length,
sound) always comes from probing the stored file: a clip that was asked for at one rate and came out at another has the
rate it came out at. The probe reads the stored file, never the engine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from gateway import video_inputs


@dataclass(frozen=True)
class VideoSource:
    wire_id: str
    asset_id: str                    # the stored clip
    model: str | None                # what the request named
    resolved_model: str | None
    fps: float | None                # from the probe; None when the file could not be probed
    size: str | None
    seconds: float | None
    has_audio: bool | None           # None: unknown


@dataclass(frozen=True)
class SourceRefused:
    http_status: int
    message: str
    kind: str = "invalid_request_error"
    param: str | None = None


def probe_asset(body: bytes) -> Mapping[str, Any]:
    """What the stored file shows: ``fps``, ``width``, ``height``, ``seconds``, ``has_audio``; empty when it cannot be read."""
    found = video_inputs.probe_video(body)
    if found is None:
        return {}
    return {"fps": found.fps, "width": found.width, "height": found.height, "seconds": found.duration,
            "has_audio": found.has_audio}


async def resolve_source(job_id: str, wire_id: str, caller: Any, jobs: Any, *,
                         probe: Callable[[bytes], Mapping[str, Any]] = probe_asset) -> VideoSource | SourceRefused:
    """The source a finished job holds. Unknown, or not the caller's: 404 (the job store answers both alike). Not finished: 404
    with the word of its status, as the content route says it. A finished job with no stored clip is 404 too."""
    view = await asyncio.to_thread(jobs.get, job_id, caller)
    if view is None:
        return SourceRefused(404, "video_id not found", kind="not_found")
    if view.status != "succeeded":
        return SourceRefused(404, f"Video content is not ready yet (status={view.status})")
    ref = next((r for r in (view.results or ()) if r.asset_id and r.media_type != "application/json"), None)
    if ref is None:
        return SourceRefused(404, "video_id not found", kind="not_found")
    facts_of = getattr(jobs, "request_facts", None)
    facts = await asyncio.to_thread(facts_of, job_id, caller) if facts_of is not None else None
    asked = getattr(facts, "facts", None) or {}
    try:
        content = await asyncio.to_thread(jobs.open_result, job_id, ref, caller)
        shown = await asyncio.to_thread(probe, content.body or b"")
    except Exception:  # noqa: BLE001 - an unreadable clip is a source of unknown properties, never a refusal here
        shown = {}
    size = f"{shown['width']}x{shown['height']}" if shown.get("width") and shown.get("height") else None
    return VideoSource(
        wire_id=wire_id, asset_id=ref.asset_id, model=asked.get("model"), resolved_model=asked.get("resolved_model"),
        fps=shown.get("fps"), size=size, seconds=shown.get("seconds"), has_audio=shown.get("has_audio"))

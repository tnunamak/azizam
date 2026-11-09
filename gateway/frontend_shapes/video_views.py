"""The lifecycle half of the video wire: the id a caller sees, and the status, list, delete and content answers.

``OpenAIVideoCreate`` takes these as a base. Every method renders a ``RunJobView`` (and, for the facts the request kept, the
job's ``RequestFacts``); none reads a store or names an engine. The words of the wire (the object name, the id prefix, the
media-type prefix) come from the shape that mixes this in, so no literal here names the capability."""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote

from starlette.responses import JSONResponse, Response

from gateway import media_delivery, media_metadata, provenance
from gateway.frontend_shapes import _wire
from gateway.frontend_shapes._base import TERMINAL
from gateway.workflow_runtime_types import CallerContext, JobService, RequestFacts, RunJobView

WIRE_STATUS = {"queued": "queued", "running": "in_progress", "succeeded": "completed", "failed": "failed", "canceled": "failed"}
EXPIRES_AFTER_S = 86400                 # the old record's expiry: a day after it was made
MEDIA_TYPES = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska"}
FALLBACK_MEDIA_TYPE = "application/octet-stream"
# Facts of the request a status body shows by the name the wire gives them (fact key -> body key).
BODY_FACTS = (("model", "model"), ("size", "size"), ("seconds", "seconds"), ("actual_seconds", "actual_seconds"),
              ("resolved_model", "azizam_resolved_model"), ("remixed_from_video_id", "remixed_from_video_id"),
              ("extended_from_video_id", "extended_from_video_id"), ("total_seconds", "total_seconds"), ("warnings", "warnings"))
# Where an older job (no recorded facts) kept the same things: the names its result metadata used.
LEGACY_VIEW_FACTS = ("model", "size", "seconds", "azizam_resolved_model", "remixed_from_video_id", "extended_from_video_id",
                     "total_seconds")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
NOT_ACCESSIBLE = "Full metadata is available only to the maker while their prompt is kept."

# Called with (wire id, asset id, file name, media type, the request's facts) after a maker's first fetch of a finished job.
DeliveryNote = Callable[[str, str, str, str, RequestFacts], None]


def download_name(filename: Any) -> str:
    """The engine's file name as a download name: its last path part, with control characters dropped. "" when the
    engine gave none, or when what is left names nothing (empty, or only dots and spaces: ``.``, ``..``)."""
    if not isinstance(filename, str):
        return ""
    name = _CONTROL.sub("", posixpath.basename(filename))
    return name if name.strip(". ") else ""


def content_disposition(name: str) -> str:
    """An inline ``Content-Disposition`` for ``name`` (RFC 6266): a quoted ASCII fallback that cannot end the quoted string,
    and, when the name differs from it, the real name as ``filename*`` (RFC 5987, UTF-8, percent-encoded)."""
    fallback = "".join(c if " " <= c < "\x7f" and c not in '"\\' else "_" for c in name)
    value = f'inline; filename="{fallback}"'
    return value if fallback == name else f"{value}; filename*=UTF-8''{quote(name, safe='')}"


class VideoViews:
    """Mixed into the shape of the create route. Needs ``wire_name``, ``output_prefix``, ``media_refs`` and ``read``."""

    wire_name: str
    output_prefix: str

    def wire_id(self, job_id: str) -> str:
        """The id a caller sees for a job: the wire's name, an underscore and the job id."""
        return f"{self.wire_name}_{job_id}"

    def job_id_of(self, wire_id: str) -> str | None:
        """The job id a wire id names; None when the id does not have the wire's form (the route answers 400)."""
        prefix = f"{self.wire_name}_"
        if not isinstance(wire_id, str) or not wire_id.startswith(prefix) or len(wire_id) == len(prefix):
            return None
        return wire_id[len(prefix):]

    def invalid_id(self) -> Response:
        return _wire.error_response(400, f"invalid {self.wire_name}_id")

    def unknown_id(self) -> Response:
        return _wire.error_response(404, f"{self.wire_name}_id not found", kind="not_found")

    def status_body(self, view: RunJobView, facts: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The video object of one job: the wire's status word beside the job store's own, and the facts its request kept."""
        status = WIRE_STATUS.get(view.status, "queued")
        own = "failed" if view.status == "canceled" else view.status
        body: dict[str, Any] = {
            "id": self.wire_id(view.job_id), "object": self.wire_name, "azizam_object": f"{self.wire_name}.generation",
            "status": status, "azizam_status": own, "progress": 100 if view.status == "succeeded" else view.progress,
            "created_at": view.created_at_s, "completed_at": view.finished_at_s if view.status in TERMINAL else None,
            "expires_at": view.created_at_s + EXPIRES_AFTER_S if view.created_at_s is not None else None,
            "prompt": getattr(facts, "prompt", None), "remixed_from_video_id": None, "error": None, "updated_at": view.updated_at_s}
        if facts is not None and getattr(facts, "recorded", True):     # a job with no record at all (an older one) falls back to the result metadata
            for key, name in BODY_FACTS:
                if facts.get(key) is not None:
                    body[name] = facts[key]
        else:
            for name in LEGACY_VIEW_FACTS:
                if view.result_metadata.get(name) is not None:
                    body[name] = view.result_metadata[name]
        if view.failure is not None:
            body["error"] = {"code": view.failure.code, "message": view.failure.message}
        return body

    def list_item(self, view: RunJobView, facts: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One job as the list route has always shown an item: the job store's own status word (a canceled job is failed),
        not the status body's wire word, the error as its text, and only the fields a list shows."""
        body = self.status_body(view, facts)
        item: dict[str, Any] = {"id": body["id"], "object": body["object"], "model": body.get("model"), "size": body.get("size"),
                                "seconds": body.get("seconds"), "created_at": body["created_at"], "completed_at": body["completed_at"],
                                "prompt": body["prompt"]}
        if view.failure is not None:
            item["error"] = view.failure.message
        item["status"] = body["azizam_status"]
        item["progress"] = body["progress"]
        return item

    def render_job(self, view: RunJobView) -> Response:
        """The create answer: the video object of the job, with the id every lifecycle route accepts. No ``Location``."""
        return JSONResponse(self.status_body(view))

    def render_status(self, view: RunJobView, facts: Mapping[str, Any] | None = None) -> Response:
        return JSONResponse(self.status_body(view, facts))

    def render_listing(self, items: Sequence[RunJobView | Mapping[str, Any]], has_more: bool,
                       facts: Mapping[str, Mapping[str, Any] | None] | None = None) -> Response:
        """The list object. An item is a job view (rendered here, with its facts by job id) or a body that is ready."""
        data = [dict(item) if isinstance(item, Mapping) else self.list_item(item, (facts or {}).get(item.job_id))
                for item in items]
        # The list route has always answered with json.dumps' own spacing; its clients and goldens see those bytes.
        return Response(content=json.dumps({"object": "list", "data": data, "first_id": data[0]["id"] if data else None,
                                            "last_id": data[-1]["id"] if data else None, "has_more": has_more}),
                        media_type="application/json")

    def render_deleted(self, job_id: str) -> Response:
        return JSONResponse({"id": self.wire_id(job_id), "object": self.wire_name, "deleted": True})

    def render_delete_conflict(self) -> Response:
        return _wire.error_response(409, "cancel request failed: the job did not stop", kind="api_error",
                                    code=f"{self.wire_name}_cancel_failed")

    def render_bytes(self, view: RunJobView, jobs: JobService, caller: CallerContext, *, metadata_mode: str | None = None,
                     caller_person_id: str | None = None, never_keep: bool = False,
                     on_delivered: DeliveryNote | None = None) -> Response:
        """The finished job's one file. A job not finished is 404 ``<wire>_not_ready`` with the wire's status word.

        ``metadata_mode`` is the one the request asked for; without it the job's own submit-time choice stands, and then
        ``full``. ``full`` is for the maker only, while the prompt is kept. ``none`` strips the metadata from the bytes
        sent (never from the stored copy)."""
        if view.status != "succeeded":
            word = WIRE_STATUS.get(view.status, "queued")
            return _wire.error_response(404, f"{self.wire_name.capitalize()} content is not ready yet (status={word})",
                                        code=f"{self.wire_name}_not_ready")
        refs = [ref for ref in self.media_refs(view) if ref.media_type.startswith(self.output_prefix)]
        if len(refs) != 1:
            return _wire.error_response(500, f"No {self.wire_name} output found in job", kind="api_error")
        ref = refs[0]
        facts = jobs.request_facts(view.job_id, caller)
        mode = metadata_mode or (facts.get("metadata_mode") if facts is not None else None) or provenance.METADATA_FULL
        if mode == provenance.METADATA_FULL and (
                facts is None or facts.maker_person_id is None or facts.maker_person_id != caller_person_id
                or facts.prompt is None or never_keep):
            return JSONResponse({"error": {"code": "prompt_not_accessible", "message": NOT_ACCESSIBLE}}, status_code=403)
        content = self.read(view, ref, jobs, caller)
        filename = view.result_metadata.get("filename")
        name = download_name(filename)
        if isinstance(filename, str) and filename and not name:          # a name was given but cleans to nothing usable
            name = self.wire_name + next((ext for ext, kind in MEDIA_TYPES.items() if kind == ref.media_type), ".bin")
        suffix = posixpath.splitext(name)[1].lower()
        media_type = MEDIA_TYPES.get(suffix, FALLBACK_MEDIA_TYPE) if suffix else ref.media_type
        try:
            if mode == provenance.METADATA_NONE:
                content = media_delivery.deliver_bytes(content, suffix or ".bin", mode)
        except media_metadata.MediaMetadataError:
            return _wire.error_response(502, "Could not prepare video for delivery", kind="api_error")
        current = jobs.get(view.job_id, caller)           # the cancel fence: a job canceled or deleted while its file was
        if current is None or current.status != "succeeded":      # read publishes nothing and delivers nothing
            return self.unknown_id()
        headers = {"Content-Disposition": content_disposition(name)} if name else {}
        answer = Response(content=content, media_type=media_type, headers=headers)      # built first: a failure here tells no one
        if on_delivered is not None and facts is not None and facts.maker_person_id is not None and ref.asset_id:
            on_delivered(self.wire_id(view.job_id), ref.asset_id, name, media_type, facts)
        return answer

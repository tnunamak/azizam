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

from starlette.responses import JSONResponse, Response

from gateway import media_delivery, media_metadata, provenance
from gateway.frontend_shapes import _wire
from gateway.frontend_shapes._base import TERMINAL
from gateway.workflow_runtime_types import RESULT_REFUSED_MESSAGE, RUN_STARTED_PROGRESS, CallerContext, JobService, RequestFacts, RunJobView

WIRE_STATUS = {"queued": "queued", "running": "in_progress", "succeeded": "completed", "failed": "failed", "canceled": "failed"}


def effective_status(view: RunJobView) -> str:
    """The job store's status word, except that a run the engine has not reported on yet is still queued: the store marks a
    run ``running`` when it hands the prompt over, and the wire says ``in_progress`` only once the engine reports it running."""
    return "queued" if view.status == "running" and (view.progress or 0) <= RUN_STARTED_PROGRESS else view.status


_ADDRESS = re.compile(r"\s+at\s+\S+://\S+")      # " at http://host:port": where the engine runs, which no caller of a job needs


def public_failure_message(failure: Any) -> str:
    """The failure text a caller reads. A job that failed because the engine could not be reached names the engine and not its
    address: the text reaches every caller who can read the job (the sync routes keep the address for the owner's own call)."""
    message = failure.message
    return _ADDRESS.sub("", message) if failure.code == "upstream_unavailable" else message


NO_RESULT = "no_result"              # the run core's failure code for a run that ended without a result file
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
NOT_ACCESSIBLE = "Full metadata is available only to the maker while their prompt is kept."

# Called with (wire id, asset id, file name, media type, the request's facts) after a maker's first fetch of a finished job.
DeliveryNote = Callable[[str, str, str, str, RequestFacts], None]


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
        state = effective_status(view)
        status = WIRE_STATUS.get(state, "queued")
        own = "failed" if state == "canceled" else state
        body: dict[str, Any] = {
            "id": self.wire_id(view.job_id), "object": self.wire_name, "azizam_object": f"{self.wire_name}.generation",
            "status": status, "azizam_status": own, "progress": 100 if view.status == "succeeded" else view.progress,
            "created_at": view.created_at_s, "completed_at": view.finished_at_s if view.status in TERMINAL else None,
            "expires_at": view.created_at_s + EXPIRES_AFTER_S if view.created_at_s is not None else None,
            "prompt": getattr(facts, "prompt", None), "remixed_from_video_id": None, "error": None, "updated_at": view.updated_at_s}
        if facts is not None:
            for key, name in BODY_FACTS:
                if facts.get(key) is not None:
                    body[name] = facts[key]
        else:
            for name in LEGACY_VIEW_FACTS:
                if view.result_metadata.get(name) is not None:
                    body[name] = view.result_metadata[name]
        if view.failure is not None:
            # The code is the wire's own word for any failed job, as it always was; the message keeps the cause.
            body["error"] = {"code": f"{self.wire_name}_generation_failed", "message": public_failure_message(view.failure)}
        return body

    def list_item(self, view: RunJobView, facts: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """One job as the list route has always shown an item: the job store's own status word (a canceled job is failed),
        not the status body's wire word, the error as its text, and only the fields a list shows."""
        body = self.status_body(view, facts)
        item: dict[str, Any] = {"id": body["id"], "object": body["object"], "model": body.get("model"), "size": body.get("size"),
                                "seconds": body.get("seconds"), "created_at": body["created_at"], "completed_at": body["completed_at"],
                                "prompt": body["prompt"]}
        if view.failure is not None:
            item["error"] = public_failure_message(view.failure)
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
        if view.status == "failed" and view.failure is not None and view.failure.code == NO_RESULT:
            # The run ended with no file to keep: the content is missing on the engine's side, as the old route answered.
            return _wire.error_response(502, view.failure.message, kind="api_error")
        if view.status == "failed" and view.failure is not None and view.failure.message == RESULT_REFUSED_MESSAGE:
            # A limit refused the result while it was read: the content answers with the refusal and its code, as the old route did.
            return JSONResponse({"error": {"message": view.failure.message, "type": "api_error", "code": view.failure.code}},
                                status_code=view.failure.upstream_status or 502)
        if view.status != "succeeded":
            word = WIRE_STATUS.get(effective_status(view), "queued")
            return _wire.error_response(404, f"{self.wire_name.capitalize()} content is not ready yet (status={word})",
                                        code=f"{self.wire_name}_not_ready")
        refs = [ref for ref in self.media_refs(view) if ref.media_type.startswith(self.output_prefix)]
        if len(refs) != 1:
            return _wire.error_response(500, f"No {self.wire_name} output found in job", kind="api_error")
        ref = refs[0]
        facts = jobs.request_facts(view.job_id, caller)
        # The mode asked for, else the one the job recorded, else the plan's own default (as the old route resolved it).
        chosen = metadata_mode or (facts.get("metadata_mode") if facts is not None else None)
        mode = chosen or media_delivery.current_mode() or provenance.METADATA_FULL
        if mode == provenance.METADATA_FULL and (
                facts is None or facts.maker_person_id is None or facts.maker_person_id != caller_person_id
                or facts.prompt is None or never_keep):
            if chosen == provenance.METADATA_FULL:
                return JSONResponse({"error": {"code": "prompt_not_accessible", "message": NOT_ACCESSIBLE}}, status_code=403)
            # Full was only the plan's default, not the caller's choice (this request or the submit): send the file
            # without metadata rather than refuse a finished render because no prompt was kept, as the old route did.
            mode = provenance.METADATA_NONE
        content = self.read(view, ref, jobs, caller)
        filename = view.result_metadata.get("filename")
        suffix = posixpath.splitext(filename)[1].lower() if isinstance(filename, str) else ""
        media_type = MEDIA_TYPES.get(suffix, FALLBACK_MEDIA_TYPE) if suffix else ref.media_type
        try:
            if mode == provenance.METADATA_NONE:
                content = media_delivery.deliver_bytes(content, suffix or ".bin", mode)
            elif facts is not None and isinstance(facts.get("provenance"), Mapping) and facts["provenance"]:
                # The stored copy is clean; the maker's full copy carries the record the request kept (as the old route stamped it).
                content = media_delivery.attach_provenance(content, suffix or ".bin", facts["provenance"])
        except media_metadata.MediaMetadataError:
            return _wire.error_response(502, "Could not prepare video for delivery", kind="api_error")
        current = jobs.get(view.job_id, caller)           # the cancel fence: a job canceled or deleted while its file was
        if current is None or current.status != "succeeded":      # read publishes nothing and delivers nothing
            return self.unknown_id()
        if on_delivered is not None and facts is not None and facts.maker_person_id is not None and ref.asset_id:
            on_delivered(self.wire_id(view.job_id), ref.asset_id, filename if isinstance(filename, str) else "", media_type, facts)
        headers = {"Content-Disposition": f'inline; filename="{posixpath.basename(filename)}"'} if isinstance(filename, str) and filename else {}
        return Response(content=content, media_type=media_type, headers=headers)

"""The reviewed wire shapes of the OpenAI-compatible image, audio and video routes and the image jobs route.

A row in ``registry/protocol_frontends.yml`` names one of these by ``shape`` and says which workflows it selects and
whether it answers in one call (``sync``) or as a job (``job``). The shape reads its wire into a ``RunRequest`` and
renders the core's answer in its own vocabulary. It never decides which workflow runs by an intent or a model name."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping
from typing import Any

from starlette.responses import JSONResponse, Response

from gateway import image_output
from gateway.frontend_shapes import _wire
from gateway.frontend_shapes._base import Shape, job_object
from gateway.workflow_runtime_types import (
    CallerContext,
    JobService,
    MediaRef,
    ResultRef,
    RunJobView,
    WorkflowEntry,
)

ANSWER_FORMS = ("b64_json", "url")
AssetUrl = Callable[[ResultRef, CallerContext], str]
# The deployment's default workflow for a wire, by shape id: installed at start by the main module from its
# configuration (a shape holds no deployment setting). A shape with no entry has no default.
DEFAULT_WORKFLOWS: dict[str, Callable[[], str | None]] = {}
MAX_IMAGES = 10                 # the public range of ``n``: one request returns at most this many pictures
_SIZE = re.compile(r"^(\d{1,5})x(\d{1,5})$")


class OpenAIImagesJson(Shape):
    """``POST`` a JSON body with a prompt; the answer is ``{"created", "data": [...]}``, one item per image."""

    shape_id = "openai.images.json"
    files = "none"
    output_prefix = "image/"
    timeout_code = "image_generation_timeout"
    wire_codes = frozenset({timeout_code, "image_client_disconnected"})
    answer_forms = frozenset(ANSWER_FORMS)
    answer_field = "response_format"
    # Wire fields that shape the picture or its encoding, not the workflow's inputs: the answer's encoding and size are
    # read from the wire request when it is rendered.
    transport_fields = Shape.transport_fields | frozenset({
        "size", "n", "quality", "background", "output_format", "output_compression", "style", "moderation",
        "partial_images", "stream", "extensions"})

    def __init__(self, *, wait_s: float = 300.0, asset_url: AssetUrl | None = None) -> None:
        super().__init__(wait_s=wait_s)
        self.asset_url = asset_url

    def default_target(self) -> str | None:
        resolver = DEFAULT_WORKFLOWS.get(self.shape_id)
        return resolver() if resolver is not None else None

    def fill_inputs(self, entry: WorkflowEntry, inputs: dict[str, Any], wire: Mapping[str, Any]) -> dict[str, Any]:
        match = _SIZE.match(str(wire.get("size") or ""))
        if match and _wire.declared(entry, "width") and _wire.declared(entry, "height"):
            inputs["width"], inputs["height"] = int(match.group(1)), int(match.group(2))
        return inputs

    def read_wire(self, fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | Response:
        """The options that change the picture or its encoding (they reach the adapter and the run's identity), and the
        answer form (how this request wants the pictures shown). Each is checked as the wire always checked it."""
        def refuse(message: str, param: str, code: str | None = None) -> Response:
            return _wire.error_response(400, message, param=param, code=code)

        def sent(name: str) -> bool:
            return fields.get(name) not in (None, "")

        background, problem = image_output.parse_image_background(fields.get("background"))
        if problem:
            return refuse(problem, "background", "invalid_value")
        output_format, problem = image_output.parse_output_format(fields.get("output_format"))
        if problem:
            return refuse(problem, "output_format", "invalid_value")
        conflict = image_output.background_format_conflict(background or "auto", output_format)
        if conflict:
            return refuse(conflict, "background", "invalid_value")
        compression, problem = image_output.parse_output_compression(fields.get("output_compression"), output_format)
        if problem:
            return refuse(problem, "output_compression", "invalid_value")
        quality, problem = image_output.parse_quality(fields.get("quality"))
        if problem:
            return refuse(problem, "quality", "invalid_value")
        count = fields.get("n", 1)
        if isinstance(count, str) and count.strip().lstrip("+-").isdigit():     # a form field is text
            count = int(count)
        if isinstance(count, bool) or not isinstance(count, int):
            return refuse("n must be an integer", "n")
        if not 1 <= count <= MAX_IMAGES:
            return refuse(f"n must be between 1 and {MAX_IMAGES}", "n")
        form = fields.get("response_format") or None
        if form is not None and form not in ANSWER_FORMS:
            return _wire.error_response(400, "response_format must be 'b64_json' or 'url'")
        options: dict[str, Any] = {}
        for name, value in (("n", count), ("quality", quality), ("output_format", output_format), ("output_compression", compression),
                            ("background", background)):
            if sent(name):
                options[name] = value
        return options, {"response_format": form}

    async def render_success(self, view: RunJobView, jobs: JobService, caller: CallerContext,
                             answer: Mapping[str, Any] | None = None) -> Response:
        # An explicit form is what this request asked for; one that was not asked for is the URL when the deployment gives
        # this shape a URL renderer, else the bytes.
        form = (answer or {}).get("response_format") or ("url" if self.asset_url is not None else "b64_json")
        if form == "url" and self.asset_url is None:
            return _wire.error_response(400, "response_format 'url' is not available on this route", param="response_format")
        items = []
        for ref in self.media_refs(view):
            item: dict[str, Any] = {"asset_id": ref.asset_id, "media_type": ref.media_type}
            if form == "url":
                item["url"] = self.asset_url(ref, caller)
            else:
                item["b64_json"] = base64.b64encode(self.read(view, ref, jobs, caller)).decode("ascii")
            items.append(item)
        return JSONResponse({"created": view.created_at_s, "data": items})


class OpenAIImagesMultipart(OpenAIImagesJson):
    """The same answer for a multipart request: a picture, and optionally a mask or more pictures, as file parts."""

    shape_id = "openai.images.multipart"
    body = "multipart"
    files = "one"


class ImageJobs(OpenAIImagesJson):
    """The image jobs route: the request of the JSON images shape, answered as a job object (202, or 200 when the job
    is already over) with a ``Location`` header. Poll and content URLs are the media-jobs routes."""

    shape_id = "openai.images.jobs"


class AudioGenerate(Shape):
    """``POST`` a JSON body with a prompt; a sync row answers with the audio bytes, a job row with a job object."""

    shape_id = "openai.audio.json"
    files = "none"
    output_prefix = "audio/"
    timeout_code = "audio_generation_timeout"

    async def render_success(self, view: RunJobView, jobs: JobService, caller: CallerContext,
                             answer: Mapping[str, Any] | None = None) -> Response:
        refs = self.media_refs(view)
        if len(refs) != 1:
            return _wire.error_response(500, "The result is not one audio file", kind="api_error", code="result_shape")
        return Response(content=self.read(view, refs[0], jobs, caller), media_type=refs[0].media_type)


_VIDEO_STATUS = {"queued": "queued", "running": "in_progress", "succeeded": "completed", "failed": "failed",
                 "canceled": "failed"}


class OpenAIVideoCreate(Shape):
    """``POST /v1/videos``: a JSON body with a prompt; the answer is a video object that is queued at once."""

    shape_id = "openai.video.create"
    files = "none"
    output_prefix = "video/"
    transport_fields = frozenset({"model", "user"})

    @property
    def wire_name(self) -> str:
        """The OpenAI wire's name for what this shape moves (the object type, and the field that names a source): the
        second part of the shape id. Read from the id so the word is data of the shape, not a literal in the code."""
        return self.shape_id.split(".")[1]

    def render_job(self, view: RunJobView) -> Response:
        status = _VIDEO_STATUS.get(view.status, "queued")
        body = {"id": view.job_id, "object": self.wire_name, "status": status, "progress": 100 if status == "completed" else 0,
                "created_at": view.created_at_s, "completed_at": view.updated_at_s if status == "completed" else None,
                "error": None if view.failure is None else {"code": view.failure.code, "message": view.failure.message}}
        return JSONResponse(body, headers={"Location": f"/gateway/v1/media/jobs/{view.job_id}"})


class OpenAIVideoEdit(OpenAIVideoCreate):
    """``POST /v1/videos/edits``: a prompt and the video to change, named as ``{"video": {"id": ...}}``. The video fills
    the workflow's required asset input as a reference to a stored asset; the core resolves what the reference holds."""

    shape_id = "openai.video.json_source"
    files = "one"

    def file_inputs(self, entry: WorkflowEntry, part_names: list[str]) -> Mapping[str, str]:
        return {}

    async def to_run_request(self, request, row, snapshot, *, spool):  # type: ignore[override]
        run = await super().to_run_request(request, row, snapshot, spool=spool)
        if isinstance(run, Response):
            return run
        video = run.inputs.get(self.wire_name)
        source = video.get("id") if isinstance(video, Mapping) else None
        if not isinstance(source, str) or not source:
            return _wire.error_response(400, f"The source is missing: send {{\"{self.wire_name}\": {{\"id\": ...}}}}", param=self.wire_name)
        entry = snapshot.get(run.workflow_id)
        targets = _wire.asset_input_names(entry) if entry is not None else []
        if not targets:
            return _wire.error_response(400, "This workflow edits no video", param=self.wire_name)
        inputs = {name: value for name, value in run.inputs.items() if name != self.wire_name}
        ref = MediaRef(digest="", media_type="application/octet-stream", size=0, asset_id=source)
        from dataclasses import replace
        return replace(run, inputs=inputs, media={targets[0]: (ref,)})

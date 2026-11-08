"""The reviewed wire shapes of the OpenAI-compatible image, audio and video routes and the image jobs route.

A row in ``registry/protocol_frontends.yml`` names one of these by ``shape`` and says which workflows it selects and
whether it answers in one call (``sync``) or as a job (``job``). The shape reads its wire into a ``RunRequest`` and
renders the core's answer in its own vocabulary. It never decides which workflow runs by an intent or a model name."""

from __future__ import annotations

import base64
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, ClassVar

from starlette.responses import JSONResponse, Response

import json

from gateway import client_errors, image_extensions, image_output
from gateway.frontend_shapes import _wire
from gateway.frontend_shapes._base import Shape, coerce, job_object
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
# Inputs a wire sets itself, by shape id: installed by the main module from configuration. Only a declared input is set.
FIXED_INPUTS: dict[str, Callable[[], Mapping[str, Any]]] = {}
MAX_IMAGES = 10                 # the public range of ``n``: one request returns at most this many pictures
_SIZE = re.compile(r"^(\d{1,5})x(\d{1,5})$")


class OpenAIImagesJson(Shape):
    """``POST`` a JSON body with a prompt; the answer is ``{"created", "data": [...]}``, one item per image."""

    shape_id = "openai.images.json"
    files = "none"
    not_object_text = "Request body must be a JSON object"
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

    def precheck(self, fields: Mapping[str, Any], uploads: Sequence[tuple[str, Any]]) -> Response | None:
        if not fields.get("prompt"):
            return _wire.error_response(400, "prompt is required")
        return None

    def no_such_model(self, target: str | None, refusal: Response) -> Response:
        if json.loads(bytes(refusal.body)).get("error", {}).get("code") != "workflow_not_found":
            return refusal
        return _wire.error_response(404, f"Model '{target}' is not available for image generation.", code="model_not_found",
                                    param="model")

    def fill_inputs(self, entry: WorkflowEntry, inputs: dict[str, Any], wire: Mapping[str, Any]) -> dict[str, Any]:
        extensions = wire.get("extensions")
        block = extensions.get(image_extensions.WORKFLOW_EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else None
        if isinstance(block, Mapping):
            inputs.update({str(name): coerce(entry, str(name), value) for name, value in block.items()})
        dimensions = read_size(wire.get("size"))
        if isinstance(dimensions, tuple) and _wire.declared(entry, "width") and _wire.declared(entry, "height"):
            inputs["width"], inputs["height"] = dimensions
        fixed = FIXED_INPUTS.get(self.shape_id)
        for name, value in (fixed().items() if fixed is not None else ()):
            if _wire.declared(entry, name):
                inputs[name] = value
        return inputs

    # The top-level wire fields this route accepts; any other is refused (a workflow's own inputs go under ``extensions``).
    standard_fields: ClassVar[frozenset[str]] = image_extensions.STANDARD_IMAGE_GENERATE_FIELDS

    def read_wire(self, fields: dict[str, Any], upload_names: Sequence[str] = ()
                  ) -> tuple[dict[str, Any], dict[str, Any]] | Response:
        """The options that change the picture or its encoding (they reach the adapter and the run's identity), and the
        answer form (how this request wants the pictures shown). Each is checked as the wire always checked it."""
        def refuse(message: str, param: str, code: str | None = None) -> Response:
            return _wire.error_response(400, message, param=param, code=code, null_code=True)

        def sent(name: str) -> bool:
            return fields.get(name) not in (None, "")

        unknown = image_extensions.detect_unknown_top_level_fields({**fields, **dict.fromkeys(upload_names)}, self.standard_fields)
        if unknown:
            name, message = next(iter(unknown.items()))
            return _wire.error_response(400, message, param=name, code="unknown_parameter")
        extensions = fields.get("extensions")
        if isinstance(extensions, str):                       # a form field is text
            try:
                extensions = json.loads(extensions)
            except ValueError:
                extensions = None
            if not isinstance(extensions, Mapping):
                return refuse("extensions must be a JSON object string", "extensions", "invalid_json")
            fields["extensions"] = extensions

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


IMAGE_PARTS = ("image[]", "image", "images")      # the file parts that carry the pictures to change
MASK_PART = "mask"
SECOND_CHOICE = ".multi_image"                   # DEFAULT_WORKFLOWS key suffix: the default when several pictures arrive
_DIMENSIONS = re.compile(r"[0-9]+x[0-9]+")


def read_size(text: Any) -> tuple[int, int] | None | bool:
    """``WIDTHxHEIGHT`` in the spellings that worked before strict validation (``512X768``, ``512 x 768``). None: no size
    or ``auto``; False: not a size."""
    if text is None or text == "auto":
        return None
    normalized = re.sub(r"\s*[xX]\s*", "x", text.strip()) if isinstance(text, str) else ""
    if not _DIMENSIONS.fullmatch(normalized):
        return False
    width, height = (int(part) for part in normalized.split("x"))
    return (width, height) if width > 0 and height > 0 else False


class OpenAIImagesMultipart(OpenAIImagesJson):
    """The same answer for a multipart request: a picture, and optionally a mask or more pictures, as file parts."""

    shape_id = "openai.images.multipart"
    body = "multipart"
    files = "one"
    standard_fields = image_extensions.STANDARD_IMAGE_EDIT_FIELDS
    not_object_text = "The request body must be a JSON object"
    intent = "image.edit"

    def unreadable_form(self) -> Response:
        return client_errors.client_error(400, "invalid_form", error_type="invalid_request_error")

    def precheck(self, fields: Mapping[str, Any], uploads: Sequence[tuple[str, Any]]) -> Response | None:
        if read_size(fields.get("size")) is False:
            return _wire.error_response(400, "size must be 'auto' or WIDTHxHEIGHT with positive integers", param="size",
                                        code=None, null_code=True)
        count = len([name for name, _ in uploads if name in IMAGE_PARTS])
        if count == 0:
            return _wire.error_response(400, "image is required")
        return self.check_count(count)

    def check_count(self, count: int) -> Response | None:
        return None

    def refine_target(self, target: str | None, *, named: bool, uploads: Sequence[tuple[str, Any]],
                      exists: Callable[[str], bool]) -> str | None:
        """The edit name a request runs: a name that is no edit workflow reads as its ``-edit`` sibling, a mask asks for
        the ``-inpaint`` sibling, and several pictures with no name ask for the multi-picture default."""
        if not target:
            return target
        count = len([name for name, _ in uploads if name in IMAGE_PARTS])
        masked = any(name == MASK_PART for name, _ in uploads)
        default = DEFAULT_WORKFLOWS.get(self.shape_id)
        multi = DEFAULT_WORKFLOWS.get(self.shape_id + SECOND_CHOICE)
        if multi is not None and target == multi():
            return self.inpaint_of(default() if default else target, exists) if masked else target
        if not target.endswith(("-edit", "-inpaint")) and not exists(target):
            target = f"{target}-edit"
        if masked:
            return self.inpaint_of(target, exists)
        if count > 1 and default is not None and target == default() and multi is not None:
            return multi()
        return target

    @staticmethod
    def inpaint_of(name: str, exists: Callable[[str], bool]) -> str:
        if name.endswith("-inpaint"):
            return name
        sibling = f"{name.removesuffix('-edit')}-inpaint"
        return sibling if exists(sibling) else name

    def no_such_model(self, target: str | None, refusal: Response) -> Response:
        if json.loads(bytes(refusal.body)).get("error", {}).get("code") != "workflow_not_found":
            return refusal
        return _wire.error_response(404, f"Model '{target}' is not available for image editing.", code="model_not_found",
                                    param="model", null_code=True)

    def postcheck(self, entry: WorkflowEntry, fields: Mapping[str, Any], uploads: Sequence[tuple[str, Any]]) -> Response | None:
        name = entry.id.removeprefix(f"{self.intent}.")
        if _wire.entry_required(entry, "prompt") is True and not fields.get("prompt"):
            return _wire.error_response(400, "prompt is required")
        pictures = len([part for part, _ in uploads if part in IMAGE_PARTS])
        slots = [asset for asset in _wire.asset_input_names(entry) if asset != MASK_PART]
        needed = len([asset for asset in slots if _wire.entry_required(entry, asset) is True])
        if pictures < needed:
            return _wire.error_response(400, f"workflow {name} requires at least {needed} input image(s); {pictures} were provided",
                                        param="image[]", code="missing_required_image")
        if pictures > len(slots):
            return _wire.error_response(400, f"workflow {name} accepts at most {len(slots)} input image(s); {pictures} were provided",
                                        param="image[]", code="too_many_images")
        if _wire.entry_required(entry, MASK_PART) is True and not any(part == MASK_PART for part, _ in uploads):
            return _wire.error_response(400, f"mask is required for workflow input {MASK_PART}", param=MASK_PART,
                                        code="missing_required_parameter")
        return None


class OpenAIImagesVariations(OpenAIImagesMultipart):
    """``/v1/images/variations``: one picture and no prompt, the narrowest of the image wires."""

    shape_id = "openai.images.variations"
    standard_fields = image_extensions.STANDARD_IMAGE_VARIATIONS_FIELDS

    def check_count(self, count: int) -> Response | None:
        if count > 1:
            return _wire.error_response(400, f"/v1/images/variations accepts exactly one input image; {count} were provided",
                                        param="image", code="too_many_images")
        return None

    def refine_target(self, target: str | None, *, named: bool, uploads: Sequence[tuple[str, Any]],
                      exists: Callable[[str], bool]) -> str | None:
        if target and not target.endswith(("-edit", "-inpaint")):
            return f"{target}-edit"
        return target

    def postcheck(self, entry: WorkflowEntry, fields: Mapping[str, Any], uploads: Sequence[tuple[str, Any]]) -> Response | None:
        return None


class ImageJobs(OpenAIImagesJson):
    """The image jobs route: the request of the JSON images shape, answered as a job object (202, or 200 when the job
    is already over) with a ``Location`` header. Poll and content URLs are the media-jobs routes."""

    shape_id = "openai.images.jobs"
    not_object_text = "JSON body must be an object"


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

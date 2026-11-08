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
# The deployment's signed links to a stored picture, by use: "link" is the item's ``url``, "asset" its ``asset_url``. Installed
# at start by the main module (they sign for the request's host).
ASSET_URLS: dict[str, Callable[[ResultRef, Any], str]] = {}
REQUEST_KEY = "request"         # where ``to_response`` hands the request to ``render_success`` in the answer mapping
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

    def front(self, fields: Mapping[str, Any]) -> Response | None:
        """Hook: the first checks of the wire, in the order its clients met them, and the inputs they accept."""
        _wire.note("model")
        _wire.note("size")
        return None

    def precheck(self, fields: Mapping[str, Any], uploads: Sequence[tuple[str, Any]]) -> Response | None:
        if not fields.get("prompt"):
            _wire.note("prompt", rejected="missing")
            return _wire.error_response(400, "prompt is required")
        _wire.note("prompt")
        return None

    def no_such_model(self, target: str) -> Response | None:
        return _wire.error_response(404, f"Model '{target}' is not available for image generation.", code="model_not_found",
                                    param="model")

    def dimensions(self, text: Any) -> tuple[int, int] | None:
        """The picture size this request runs at: the text read the way this wire always read it, 1024 square when the text is
        not a size. None: this wire leaves the size to the workflow."""
        return old_dimensions(text)

    def fill_inputs(self, entry: WorkflowEntry, inputs: dict[str, Any], wire: Mapping[str, Any]) -> dict[str, Any]:
        extensions = wire.get("extensions")
        block = extensions.get(image_extensions.WORKFLOW_EXTENSION_NAMESPACE) if isinstance(extensions, Mapping) else None
        if isinstance(block, Mapping):
            inputs.update({str(name): coerce(entry, str(name), value) for name, value in block.items()})
        dimensions = self.dimensions(wire.get("size"))
        if dimensions is not None and _wire.declared(entry, "width") and _wire.declared(entry, "height"):
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
            for name, reason in unknown.items():
                _wire.note(name, rejected=reason)
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

        refused = self.front(fields)
        if refused is not None:
            return refused
        background, problem = image_output.parse_image_background(fields.get("background"))
        if problem:
            _wire.note("background", rejected=problem)
            return refuse(problem, "background", "invalid_value")
        if fields.get("background") is not None:
            _wire.note("background")
        output_format, problem = image_output.parse_output_format(fields.get("output_format"))
        if problem:
            _wire.note("output_format", rejected=problem)
            return refuse(problem, "output_format", "invalid_value")
        if sent("output_format"):
            _wire.note("output_format")
        conflict = image_output.background_format_conflict(background or "auto", output_format)
        if conflict:
            _wire.note("background", rejected="conflicts_with_output_format")
            return refuse(conflict, "background", "invalid_value")
        compression, problem = image_output.parse_output_compression(fields.get("output_compression"), output_format)
        if problem:
            _wire.note("output_compression", rejected=problem)
            return refuse(problem, "output_compression", "invalid_value")
        if sent("output_compression"):
            _wire.note("output_compression")
        quality, problem = image_output.parse_quality(fields.get("quality"))
        if problem:
            _wire.note("quality", rejected=problem)
            return refuse(problem, "quality", "invalid_value")
        if sent("quality"):
            _wire.note("quality")
        count = fields.get("n", 1)
        if isinstance(count, str) and count.strip().lstrip("+-").isdigit():     # a form field is text
            count = int(count)
        if isinstance(count, bool) or not isinstance(count, int):
            _wire.note("n", rejected="not_integer")
            return refuse("n must be an integer", "n")
        if not 1 <= count <= MAX_IMAGES:
            _wire.note("n", rejected="out_of_range")
            return refuse(f"n must be between 1 and {MAX_IMAGES}", "n")
        _wire.note("n")
        form = fields.get("response_format") or None
        if form is not None and form not in ANSWER_FORMS:
            _wire.note("response_format", rejected="unsupported_value")
            return _wire.error_response(400, "response_format must be 'b64_json' or 'url'")
        _wire.note("response_format")
        options: dict[str, Any] = {}
        for name, value in (("n", count), ("quality", quality), ("output_format", output_format), ("output_compression", compression),
                            ("background", background)):
            if sent(name):
                options[name] = value
        return options, {"response_format": form, **self.echo(fields, output_format, quality, background)}

    def echo(self, fields: Mapping[str, Any], output_format: str, quality: str, background: str | None) -> dict[str, Any]:
        """What the answer repeats of the request: the output format, the quality unless it is ``auto``, the background when the
        request settles it (a format with no alpha channel settles it as opaque), and the size the run used, with the note
        when that is not the size the caller wrote."""
        echo: dict[str, Any] = {"output_format": output_format}
        if quality != "auto":
            echo["quality"] = quality
        if background in ("transparent", "opaque"):
            echo["background"] = background
        elif output_format == "jpeg":
            echo["background"] = "opaque"
        size = self.dimensions(fields.get("size"))
        if size is not None:
            effective = f"{size[0]}x{size[1]}"
            echo["size"] = effective
            requested = str(fields.get("size") or "").strip()
            if requested and requested != effective:
                echo["size_adjusted"] = f"{requested}->{effective}"
                _wire.fact("image_size_adjustment", {"requested": requested, "effective": effective}, f"size_adjusted:{requested}->{effective}")
        return echo

    async def render_success(self, view: RunJobView, jobs: JobService, caller: CallerContext,
                             answer: Mapping[str, Any] | None = None) -> Response:
        # An explicit form is what this request asked for; one that was not asked for is the URL when the deployment gives
        # this shape a URL renderer, else the bytes.
        request = (answer or {}).get(REQUEST_KEY)
        link = self.asset_url or ((lambda ref, _caller: ASSET_URLS["link"](ref, request)) if request is not None and "link" in ASSET_URLS else None)
        form = (answer or {}).get("response_format") or ("url" if link is not None else "b64_json")
        if form == "url" and link is None:
            return _wire.error_response(400, "response_format 'url' is not available on this route", param="response_format")
        items = []
        for ref in self.media_refs(view):
            item: dict[str, Any] = {"asset_id": ref.asset_id, "media_type": ref.media_type}
            if request is not None and "asset" in ASSET_URLS:
                item["asset_url"] = ASSET_URLS["asset"](ref, request)
            if form == "url":
                item["url"] = link(ref, caller)
            else:
                item["b64_json"] = base64.b64encode(self.read(view, ref, jobs, caller)).decode("ascii")
            items.append(item)
        body: dict[str, Any] = {"created": view.created_at_s, "data": items}
        body.update({name: value for name, value in (answer or {}).items() if name in ECHOED})
        return JSONResponse(body, headers=result_headers(view.result_metadata, answer or {}))


# The answer fields that repeat the request, and the headers the result's facts become (name: metadata key).
ECHOED = ("output_format", "background", "quality", "size")


def result_headers(metadata: Mapping[str, Any], answer: Mapping[str, Any]) -> dict[str, str]:
    """The response headers for the facts the run returned (rounds, files, warnings) and the size note of the request."""
    headers: dict[str, str] = {}
    warnings = metadata.get("warnings")
    if warnings:
        headers["X-Proxy-Warning"] = "; ".join(str(w) for w in warnings)
    if "rounds" in metadata:
        headers["X-Image-Rounds"] = str(metadata["rounds"])
    if "returned" in metadata:
        headers["X-Image-Returned"] = str(metadata["returned"])
    if answer.get("size_adjusted"):
        headers["X-Azizam-Size-Adjusted"] = str(answer["size_adjusted"])
    return headers


IMAGE_PARTS = ("image[]", "image", "images")      # the file parts that carry the pictures to change
MASK_PART = "mask"
SECOND_CHOICE = ".multi_image"                   # DEFAULT_WORKFLOWS key suffix: the default when several pictures arrive
_DIMENSIONS = re.compile(r"[0-9]+x[0-9]+")


def old_dimensions(text: Any) -> tuple[int, int]:
    """``WIDTHxHEIGHT`` with a lower-case ``x`` and two whole numbers; anything else is 1024 square (what the generation
    route always did with the text)."""
    if not isinstance(text, str) or "x" not in text:
        return 1024, 1024
    try:
        width, height = text.lower().split("x")
        return int(width), int(height)
    except ValueError:
        return 1024, 1024


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
    files = "any"                  # an edit may take several pictures (a workflow with more than one required asset)
    standard_fields = image_extensions.STANDARD_IMAGE_EDIT_FIELDS
    not_object_text = "The request body must be a JSON object"

    def dimensions(self, text: Any) -> tuple[int, int] | None:
        size = read_size(text)
        return size if isinstance(size, tuple) else None

    def unreadable_form(self) -> Response:
        return client_errors.client_error(400, "invalid_form", error_type="invalid_request_error")

    def front(self, fields: Mapping[str, Any]) -> Response | None:
        if read_size(fields.get("size")) is False:
            _wire.note("size", rejected="invalid_dimensions")
            return _wire.error_response(400, "size must be 'auto' or WIDTHxHEIGHT with positive integers", param="size",
                                        code=None, null_code=True)
        _wire.note("model")
        if fields.get("size"):
            _wire.note("size")
        return None

    def precheck(self, fields: Mapping[str, Any], uploads: Sequence[tuple[str, Any]]) -> Response | None:
        names = [name for name, _ in uploads if name in IMAGE_PARTS]
        if not names:
            _wire.note(IMAGE_PARTS[1], rejected="missing")
            return _wire.error_response(400, "image is required")
        _wire.note(IMAGE_PARTS[0] if IMAGE_PARTS[0] in names else IMAGE_PARTS[1])
        return self.check_count(len(names))

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

    def no_such_model(self, target: str) -> Response | None:
        return _wire.error_response(404, f"Model '{target}' is not available for image editing.", code="model_not_found",
                                    param="model")

    def postcheck(self, entry: WorkflowEntry, fields: Mapping[str, Any], uploads: Sequence[tuple[str, Any]]) -> Response | None:
        name = entry.id.removeprefix(f"{entry.contract.get('intent')}.")
        if _wire.entry_required(entry, "prompt") is True and not fields.get("prompt"):
            _wire.note("prompt", rejected="missing")
            return _wire.error_response(400, "prompt is required")
        if fields.get("prompt"):
            _wire.note("prompt")
        elif not _wire.declared(entry, "prompt"):
            _wire.note("prompt", ignored="workflow does not use prompt")
        pictures = len([part for part, _ in uploads if part in IMAGE_PARTS])
        slots = [asset for asset in _wire.asset_input_names(entry) if asset != MASK_PART]
        needed = len([asset for asset in slots if _wire.entry_required(entry, asset) is True])
        if pictures < needed:
            _wire.note(IMAGE_PARTS[0], rejected="missing_required_image")
            return _wire.error_response(400, f"workflow {name} requires at least {needed} input image(s); {pictures} were provided",
                                        param="image[]", code="missing_required_image")
        if pictures > len(slots):
            _wire.note(IMAGE_PARTS[0], rejected="too_many_images")
            return _wire.error_response(400, f"workflow {name} accepts at most {len(slots)} input image(s); {pictures} were provided",
                                        param="image[]", code="too_many_images")
        masked = any(part == MASK_PART for part, _ in uploads)
        if _wire.entry_required(entry, MASK_PART) is True and not masked:
            _wire.note(MASK_PART, rejected="missing")
            return _wire.error_response(400, f"mask is required for workflow input {MASK_PART}", param=MASK_PART,
                                        code="missing_required_parameter")
        if masked:
            _wire.note(MASK_PART)
        return None


class OpenAIImagesVariations(OpenAIImagesMultipart):
    """``/v1/images/variations``: one picture and no prompt, the narrowest of the image wires."""

    shape_id = "openai.images.variations"
    files = "one"
    standard_fields = image_extensions.STANDARD_IMAGE_VARIATIONS_FIELDS

    def check_count(self, count: int) -> Response | None:
        if count > 1:
            return _wire.error_response(400, f"/v1/images/variations accepts exactly one input image; {count} were provided",
                                        param=IMAGE_PARTS[1], code="too_many_images")
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

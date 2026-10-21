"""HTTP routes for the likeness check and the identity scene workflow.

* ``POST /gateway/v1/faces/likeness``  score the faces in ``candidate`` against
  the people in the ``reference`` photos. CPU only, about 2 s.
* ``POST /gateway/v1/images/identity`` put the people from the reference photos
  into a new scene and return the best of N seeds with its likeness scores.
  GPU, about two minutes per attempt.

Authentication, policy, the face sidecar and the ComfyUI call arrive through
:class:`IdentityApiDeps`, so the routes are tested with fakes (the pattern of
``gateway/voices_api.py``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from gateway import identity_scene as ids
from gateway import likeness, request_forms
from gateway.identity_scene import IdentityDeps, IdentityError, PersonSpec, SceneRequest
from gateway.likeness import LikenessError
from gateway.extensions import ExtensionError, multipart_extensions

LIKENESS_PATH = "/gateway/v1/faces/likeness"
IDENTITY_PATH = "/gateway/v1/images/identity"
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_PHOTOS = 12


class FaceServiceClient:
    """Async client for ``services/face-service`` (POST /v1/faces/analyze)."""

    def __init__(self, base_url: str, *, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def analyze(self, image: bytes, model: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10)) as client:
            response = await client.post(
                f"{self.base_url}/v1/faces/analyze",
                files={"image": ("image", image, "application/octet-stream")},
                data={"model": model},
            )
        if response.status_code != 200:
            raise RuntimeError(f"face service returned {response.status_code}")
        return list(response.json()["faces"])


class IdentityUnavailable(Exception):
    """The face sidecar could not be started; carries the 503 response to return."""

    def __init__(self, response: Response) -> None:
        super().__init__("face service unavailable")
        self.response = response


@dataclass
class IdentityApiDeps:
    authenticate: Callable[[Request, str], Any]                 # AuthContext or an error Response
    enforce_policy: Callable[[Any, str], Response | None]
    ensure_face_service: Callable[[], Awaitable[Response | None]]
    face_client: Callable[[], FaceServiceClient]
    run_workflow: Callable[[Any, dict[str, Any], dict[str, bytes]], Awaitable[bytes]]  # (auth, graph, uploads) -> png
    template: Callable[[], tuple[Mapping[str, Any], Mapping[str, Any]]]               # (graph, gateway binding)
    store_image: Callable[[Request, Any, bytes, str], dict[str, Any]]                  # -> data[] item
    models: tuple[str, ...] = likeness.DEFAULT_MODELS
    # Called as (auth, form) after the form is read: open the media delivery
    # context and read the metadata control. Returns an error Response or None.
    note_delivery: Callable[[Any, Mapping[str, Any]], Response | None] | None = None
    # (request, auth, SceneRequest, response_format) -> 202 job response; None disables async mode.
    # An async job runs in a task created after note_delivery, so it inherits the delivery context.
    submit_job: Callable[[Request, Any, SceneRequest, str], Awaitable[Response]] | None = None


def error(
    status: int, code: str, message: str, *, param: str | None = None, faces: list[dict[str, Any]] | None = None
) -> JSONResponse:
    body: dict[str, Any] = {"message": message, "type": "invalid_request_error" if status < 500 else "api_error", "code": code}
    if param:
        body["param"] = param
    if faces:
        body["faces"] = faces  # the detected faces (photo, bbox, age, gender) to pick boxes from
    return JSONResponse({"error": body}, status_code=status)


# ------------------------------------------------------------------ form parsing


def _is_upload(value: Any) -> bool:
    return hasattr(value, "read") and hasattr(value, "filename")


async def _uploads(form: Any, *names: str) -> list[bytes]:
    out: list[bytes] = []
    for name in names:
        for item in form.getlist(name):
            if _is_upload(item):
                data = await item.read()
                if not data:
                    raise IdentityError(f"{name} file is empty", param=name)
                out.append(data)
    return out


def _json_field(form: Any, name: str) -> Any:
    raw = form.get(name)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, (str, bytes)):
        return raw  # already parsed (extensions JSON)
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise IdentityError(f"{name} must be valid JSON", param=name) from exc


def _enum(form: Any, name: str, choices: tuple[str, ...], default: str) -> str:
    raw = form.get(name)
    if raw is None or raw == "":
        return default
    value = str(raw).strip().lower()
    if value not in choices:
        raise IdentityError(f"{name} must be one of {', '.join(choices)}", param=name)
    return value


def _number(form: Any, name: str, cast: Callable[[Any], Any], default: Any, lo: float, hi: float) -> Any:
    raw = form.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError) as exc:
        raise IdentityError(f"{name} must be a number", param=name) from exc
    if not lo <= value <= hi:
        raise IdentityError(f"{name} must be between {lo:g} and {hi:g}", param=name)
    return value


def _bool(form: Any, name: str, default: bool) -> bool:
    raw = form.get(name)
    if raw is None or raw == "":
        return default
    lowered = str(raw).strip().lower()
    if lowered not in {"true", "false", "1", "0"}:
        raise IdentityError(f"{name} must be true or false", param=name)
    return lowered in {"true", "1"}


def _source(item: Any, photo_count: int) -> ids.RefSource:
    """One reference: a photo index, or {image, box}."""
    if isinstance(item, int) and not isinstance(item, bool):
        item = {"image": item}
    if not isinstance(item, Mapping):
        raise IdentityError("each reference must be a photo index or {image, box}", param="people")
    image = item.get("image", 0)
    if not isinstance(image, int) or isinstance(image, bool) or not 0 <= image < photo_count:
        raise IdentityError("person image must be the index of an uploaded photo", param="people")
    box = item.get("box")
    if box is not None:
        if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
            raise IdentityError("person box must be [x1, y1, x2, y2] in photo pixels", param="people")
        box = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    generate = item.get("generate", True)
    if not isinstance(generate, bool):
        raise IdentityError("reference generate must be true or false", param="people")
    return ids.RefSource(image=image, box=box, generate=generate)


def parse_people(raw: Any, photo_count: int) -> list[PersonSpec]:
    """``people`` JSON, one object per person.

    ``{"label": "grandma", "image": 0, "box": [x1,y1,x2,y2]}`` names one reference photo. A person
    with several photos uses ``"images": [0, {"image": 2, "box": [..]}]`` (more photos give the
    generator more to copy and give the score more references to compare). Entries that repeat a
    label are merged into one person. ``description`` and ``infant`` are optional.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise IdentityError("people must be a JSON array", param="people")
    people: list[PersonSpec] = []
    by_label: dict[str, PersonSpec] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise IdentityError("each person must be an object", param="people")
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            raise IdentityError("person label must be a string", param="people")
        if "images" in item:
            if not isinstance(item["images"], list) or not item["images"]:
                raise IdentityError("person images must be a non-empty array", param="people")
            sources = [_source(entry, photo_count) for entry in item["images"]]
        else:
            sources = [_source(item, photo_count)]
        description = item.get("description") if isinstance(item.get("description"), str) else None
        infant = item.get("infant") if isinstance(item.get("infant"), bool) else None
        if label and label in by_label:  # a repeated label adds photos to the same person
            existing = by_label[label]
            existing.more.extend(sources)
            existing.description = existing.description or description
            existing.infant = existing.infant if existing.infant is not None else infant
            continue
        spec = PersonSpec(label=label, image=sources[0].image, box=sources[0].box, description=description,
                          infant=infant, more=sources[1:], generate_first=sources[0].generate)
        people.append(spec)
        if label:
            by_label[label] = spec
    return people


def build_scene_request(source: Any, photos: list[bytes], prompt: str, *, default_people: Any = None) -> SceneRequest:
    """Build a SceneRequest from form fields or extension options (same names in both)."""
    return SceneRequest(
        photos=photos,
        prompt=prompt,
        people=parse_people(_json_field(source, "people"), len(photos)),
        attempts=_number(source, "attempts", int, 3, 1, ids.MAX_ATTEMPTS),
        seed=_number(source, "seed", int, 0, 0, 2**31 - 1),
        resolution=_number(source, "resolution", int, None, 512, 2048),
        steps=_number(source, "steps", int, 25, 1, 60),
        cfg=_number(source, "cfg", float, 1.0, 0, 10),
        only_listed_people=_bool(source, "only_listed_people", True),
        photo_as_canvas=_bool(source, "photo_as_canvas", True),
        stop_when_pass=_bool(source, "stop_when_pass", True),
        facing_each_other=_bool(source, "facing_each_other", False),
        pooling=_enum(source, "pooling", likeness.POOLINGS, "best"),
        max_refs_per_person=_number(source, "max_refs_per_person", int, ids.DEFAULT_REFS_PER_PERSON, 1, ids.MAX_REFS_PER_PERSON),
        refine=_enum(source, "refine", ("off", "auto", "all"), "auto"),
        refine_attempts=_number(source, "refine_attempts", int, 2, 1, 4),
        pose_policy=_enum(source, "pose_policy", ("likeness", "as_written"), "likeness"),
        thresholds=parse_thresholds(_json_field(source, "thresholds")),
    )


SCENE_OPTION_NAMES = frozenset({
    "people", "attempts", "seed", "resolution", "steps", "cfg", "only_listed_people", "photo_as_canvas",
    "stop_when_pass", "facing_each_other", "pooling", "max_refs_per_person", "refine", "refine_attempts",
    "pose_policy", "thresholds", "mode",
})


def wants_async(source: Any, req: SceneRequest, default: str = "auto") -> bool:
    """``mode``: sync, async, or auto (async unless one attempt and no refinement)."""
    mode = _enum(source, "mode", ("auto", "sync", "async"), default)
    if mode == "auto":
        return not (req.attempts == 1 and req.refine == "off")
    return mode == "async"


def parse_thresholds(raw: Any) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping) or not all(
        k in likeness.DEFAULT_THRESHOLDS and isinstance(v, (int, float)) for k, v in raw.items()
    ):
        raise IdentityError(
            f"thresholds must be an object with keys from {sorted(likeness.DEFAULT_THRESHOLDS)}", param="thresholds"
        )
    return {k: float(v) for k, v in raw.items()}


# ------------------------------------------------------------------- OpenAPI


def _people_schema() -> dict[str, Any]:
    return {
        "type": "string",
        "description": (
            "JSON array of people. Each: {label?, image? (index of an uploaded photo, default 0), "
            "box? [x1,y1,x2,y2] in that photo's pixels, description?, infant? (overrides the age guess)}. "
            "A person with several photos: {label, images: [0, {image: 2, box: [..]}]}; entries that repeat a label merge. "
            "Each reference may also say generate:false to be used for scoring only (sunglasses, profile, tiny face). The first photo is the anchor (give a box if it holds several faces); in later photos without a box the face that looks like the anchor is chosen, and a photo with nobody who matches is skipped with a warning. "
            "Without a box, people are matched to faces one to one by infant:true (youngest face) and age/gender words in label or description; "
            "ambiguous cases fail with 400 ambiguous_faces and the list of detected faces, never a guess. "
            "Omit to auto-detect: every face of a single photo, or the largest face of each of several photos."
        ),
    }


LIKENESS_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["reference", "candidate"],
            "properties": {
                "reference": {"type": "array", "items": {"type": "string", "format": "binary"},
                              "description": "Reference photo(s) of the real people. Repeat the field for several photos."},
                "candidate": {"type": "string", "format": "binary", "description": "The image to check."},
                "people": _people_schema(),
                "pooling": {"type": "string", "enum": list(likeness.POOLINGS), "default": "best",
                            "description": "How several reference photos of one person are scored: closest photo, or the mean over all."},
                "thresholds": {"type": "string", "description": "JSON object overriding match, uncertain and min_margin."},
            },
        }}},
    },
    "responses": {"200": {"description": "Per-person likeness scores", "content": {"application/json": {"schema": {
        "type": "object",
        "properties": {
            "object": {"type": "string", "enum": ["likeness.check"]},
            "overall": {"type": "object", "description": "status pass|review|fail, min_score, mean_score, needs_human_check"},
            "people": {"type": "array", "items": {"type": "object", "description":
                "label, verdict match|uncertain|mismatch|missing, reliability normal|low, match{candidate_face,bbox,scores,score,margin}, notes"}},
            "thresholds": {"type": "object"},
            "models": {"type": "array", "items": {"type": "string"}},
        },
    }}}}},
}

IDENTITY_OPENAPI: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["image", "prompt"],
            "properties": {
                "image": {"type": "array", "items": {"type": "string", "format": "binary"},
                          "description": "Reference photo(s) at full resolution. Never send tiny face crops: the gateway crops roomy heads itself. Several photos of one person are welcome: list them in people[].images."},
                "prompt": {"type": "string", "description": "The new scene, poses and gaze. People are tagged for you."},
                "people": _people_schema(),
                "attempts": {"type": "integer", "minimum": 1, "maximum": ids.MAX_ATTEMPTS, "default": 3,
                             "description": "Maximum seeds to try; stops early when every person meets the threshold. ~2 min each."},
                "seed": {"type": "integer", "minimum": 0, "default": 0},
                "resolution": {"type": "integer", "minimum": 512, "maximum": 2048,
                               "description": "Per-reference pixel budget. Default: 1536 for up to 3 images, 1280 up to 5, 1024 beyond."},
                "steps": {"type": "integer", "minimum": 1, "maximum": 60, "default": 25},
                "cfg": {"type": "number", "minimum": 0, "maximum": 10, "default": 1.0},
                "photo_as_canvas": {"type": "boolean", "default": True,
                                    "description": "Also pass the first photo whole as the style, clothing and setting reference."},
                "only_listed_people": {"type": "boolean", "default": True},
                "stop_when_pass": {"type": "boolean", "default": True},
                "pooling": {"type": "string", "enum": list(likeness.POOLINGS), "default": "best",
                            "description": "How several reference photos of one person are scored: closest photo, or the mean over all."},
                "max_refs_per_person": {"type": "integer", "minimum": 1, "maximum": ids.MAX_REFS_PER_PERSON, "default": ids.DEFAULT_REFS_PER_PERSON,
                                        "description": "Reference heads sent to the generator per person: the best by size, sharpness and frontal pose. All photos are used for scoring."},
                "refine": {"type": "string", "enum": ["off", "auto", "all"], "default": "auto",
                           "description": "Per-person head refinement after composing: auto refines people below the match threshold, all refines everybody. A pass is kept only if it helps that person and hurts nobody."},
                "refine_attempts": {"type": "integer", "minimum": 1, "maximum": 4, "default": 2},
                "pose_policy": {"type": "string", "enum": ["likeness", "as_written"], "default": "likeness",
                                "description": "likeness rewrites profile / forehead-touching requests to a three-quarter view and says so in warnings[]; as_written keeps your wording."},
                "mode": {"type": "string", "enum": ["auto", "sync", "async"], "default": "auto",
                         "description": "async returns 202 with a media job (poll_url, content_url); auto is async unless attempts=1 and refine=off. sync holds the connection for minutes."},
                "facing_each_other": {"type": "boolean", "default": False,
                                      "description": "Two people only: prefer, and stop early on, results where both heads are turned toward each other (landmark yaw; head turn, not gaze)."},
                "response_format": {"type": "string", "enum": ["b64_json", "url"], "default": "b64_json"},
                "thresholds": {"type": "string", "description": "JSON object overriding match, uncertain and min_margin."},
            },
        }}},
    },
    "responses": {"200": {"description": "Best image plus likeness scores (200), or a media job to poll when mode resolves to async (202: id, status, progress, poll_url, content_url)", "content": {"application/json": {"schema": {
        "type": "object",
        "properties": {
            "created": {"type": "integer"},
            "data": {"type": "array", "items": {"type": "object", "description": "asset_id, asset_url, media_type, b64_json|url"}},
            "likeness": {"type": "object", "description": "Same shape as POST /gateway/v1/faces/likeness for the selected image"},
            "refinements": {"type": "array", "items": {"type": "object", "description": "person, pass, score_before, score_after, others_before, others_after, accepted"}},
            "warnings": {"type": "array", "items": {"type": "object", "description": "code, param, message (for example pose_rewritten)"}},
            "attempts": {"type": "array", "items": {"type": "object", "description": "attempt, seed, status, min_score, scores{label: score}, selected"}},
            "people": {"type": "array", "items": {"type": "object"}},
            "seconds": {"type": "number"},
        },
    }}}}},
}


# --------------------------------------------------------------------- routes


def register_identity_routes(app: FastAPI, deps: IdentityApiDeps) -> None:
    def guard(request: Request, path: str) -> Any:
        auth = deps.authenticate(request, path)
        if isinstance(auth, Response):
            return auth
        denied = deps.enforce_policy(auth, path)
        return denied if denied is not None else auth

    async def form_of(request: Request) -> Any:
        if "multipart/form-data" not in request.headers.get("content-type", ""):
            raise IdentityError("this request must be multipart/form-data")
        return await request_forms.read_form(request, max_part_size=MAX_PART_BYTES)

    def note_form(auth: Any, form: Any) -> Response | None:
        body = dict(form.multi_items())
        try:
            if "extensions" in body:
                body["extensions"] = multipart_extensions(body["extensions"])
        except ExtensionError as exc:
            return error(400, "invalid_extensions", str(exc), param="extensions")
        return deps.note_delivery(auth, body) if deps.note_delivery is not None else None

    def face_deps() -> IdentityDeps:
        client = deps.face_client()
        return IdentityDeps(analyze=client.analyze, run_workflow=_unused_workflow, models=deps.models)

    async def likeness_route(request: Request):
        auth = guard(request, LIKENESS_PATH)
        if isinstance(auth, Response):
            return auth
        try:
            form = await form_of(request)
            bad_metadata = note_form(auth, form)
            if bad_metadata is not None:
                return bad_metadata
            photos = await _uploads(form, "reference", "reference[]")
            candidates = await _uploads(form, "candidate")
            if not photos or len(photos) > MAX_PHOTOS:
                raise IdentityError(f"send 1 to {MAX_PHOTOS} reference photos", param="reference")
            if len(candidates) != 1:
                raise IdentityError("send exactly one candidate image", param="candidate")
            req = SceneRequest(
                photos=photos, prompt="-", people=parse_people(_json_field(form, "people"), len(photos)),
                thresholds=parse_thresholds(_json_field(form, "thresholds")),
                pooling=_enum(form, "pooling", likeness.POOLINGS, "best"),
            )
            not_ready = await deps.ensure_face_service()
            if not_ready is not None:
                return not_ready
            fdeps = face_deps()
            people = await ids.resolve_people(req, fdeps, with_heads=False)
            candidate_faces = await ids._faces(fdeps, candidates[0])
            report = likeness.check_likeness(
                [p.person for p in people], candidate_faces, models=deps.models, thresholds=req.thresholds,
                pooling=req.pooling,
            )
            report["references"] = [
                {"label": p.label, "photo": p.primary_photo, "reason": p.reason, "reference_photos": len(p.person.faces), "face_bbox": [round(v, 1) for v in p.face_bbox], "age_estimate": p.age}
                for p in people
            ]
            return JSONResponse(report)
        except (IdentityError, LikenessError) as exc:
            return error(400, getattr(exc, "code", "invalid_request"), str(exc), param=getattr(exc, "param", None), faces=getattr(exc, "faces", None))
        except (httpx.HTTPError, RuntimeError):
            return error(502, "face_service_error", "face service failed.")

    async def identity_route(request: Request):
        auth = guard(request, IDENTITY_PATH)
        if isinstance(auth, Response):
            return auth
        try:
            form = await form_of(request)
            photos = await _uploads(form, "image", "image[]")
            if not photos or len(photos) > MAX_PHOTOS:
                raise IdentityError(f"send 1 to {MAX_PHOTOS} reference photos", param="image")
            response_format = str(form.get("response_format") or "b64_json")
            if response_format not in ("b64_json", "url"):
                raise IdentityError("response_format must be 'b64_json' or 'url'", param="response_format")
            bad_metadata = note_form(auth, form)
            if bad_metadata is not None:
                return bad_metadata
            req = build_scene_request(form, photos, str(form.get("prompt") or ""))
            if wants_async(form, req) and deps.submit_job is not None:
                return await submit(request, auth, req, response_format)
            return await run_scene(request, auth, req, response_format)
        except (IdentityError, LikenessError) as exc:
            return error(400, getattr(exc, "code", "invalid_request"), str(exc), param=getattr(exc, "param", None), faces=getattr(exc, "faces", None))
        except (httpx.HTTPError, RuntimeError):
            return error(502, "identity_backend_error", "identity workflow failed.")

    async def execute(auth: Any, req: SceneRequest) -> ids.SceneResult:
        """Run the pipeline. Raises IdentityUnavailable when the face sidecar cannot start."""
        not_ready = await deps.ensure_face_service()
        if not_ready is not None:
            raise IdentityUnavailable(not_ready)
        graph, binding = deps.template()

        async def run_workflow(workflow: dict[str, Any], uploads: dict[str, bytes]) -> bytes:
            return await deps.run_workflow(auth, workflow, uploads)

        fdeps = face_deps()
        fdeps.run_workflow = run_workflow
        return await ids.run_identity_scene(
            req, fdeps, template=graph,
            image_nodes=list(binding["image_nodes"]), prompt_node=str(binding["prompt_node"]), seed_node=str(binding["seed_node"]),
        )

    async def run_scene(request: Request, auth: Any, req: SceneRequest, response_format: str) -> Response:
        """Synchronous run; shared by the native route and the /v1/images/edits alias."""
        try:
            result = await execute(auth, req)
        except IdentityUnavailable as unavailable:
            return unavailable.response
        item = deps.store_image(request, auth, result.image, response_format)
        return JSONResponse(scene_payload(result, item))

    async def submit(request: Request, auth: Any, req: SceneRequest, response_format: str) -> Response:
        assert deps.submit_job is not None
        # Fail a bad people list now (400 with the detected faces), not minutes later inside the job.
        not_ready = await deps.ensure_face_service()
        if not_ready is not None:
            return not_ready
        await ids.resolve_people(req, face_deps(), with_heads=False)
        return await deps.submit_job(request, auth, req, response_format)

    app.add_api_route(LIKENESS_PATH, likeness_route, methods=["POST"], openapi_extra=LIKENESS_OPENAPI,
                      summary="Score how well the faces in an image match reference faces", tags=["identity"])
    app.add_api_route(IDENTITY_PATH, identity_route, methods=["POST"], openapi_extra=IDENTITY_OPENAPI,
                      summary="Put the people from reference photos into a new scene, with likeness scores", tags=["identity"])
    app.state.identity_run_scene = run_scene
    app.state.identity_execute = execute
    app.state.identity_submit = submit


async def _unused_workflow(graph: dict[str, Any], uploads: dict[str, bytes]) -> bytes:  # pragma: no cover
    raise RuntimeError("workflow runner was not provided")


EXTRA_KEYS = ("likeness", "attempts", "refinements", "people", "warnings", "seconds", "prompt_used")


def scene_extras(result: ids.SceneResult) -> dict[str, Any]:
    """Everything a synchronous identity response gets besides the image bytes."""
    return {
        "likeness": result.report,
        "attempts": result.attempts,
        "refinements": result.refinements,
        "people": result.people,
        "warnings": result.warnings,
        "seconds": result.seconds,
        "prompt_used": result.prompt_used,
    }


def stored_scene_extras(result: ids.SceneResult) -> dict[str, Any]:
    """Keep async diagnostics without prompt text or free-form person descriptions."""
    extras = scene_extras(result)
    extras.pop("prompt_used")
    extras["people"] = [{k: v for k, v in person.items() if k not in {"label", "reason"}}
                        for person in result.people]
    extras["warnings"] = [
        {k: v for k, v in warning.items()
         if k == "code" or (k == "param" and v in {"prompt", "people", "image"})}
        for warning in result.warnings
    ]
    return extras


def scene_payload(result: ids.SceneResult, item: Mapping[str, Any]) -> dict[str, Any]:
    import time

    return {"created": int(time.time()), "data": [dict(item)], **scene_extras(result)}

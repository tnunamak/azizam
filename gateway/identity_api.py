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
from gateway import likeness
from gateway.identity_scene import IdentityDeps, IdentityError, PersonSpec, SceneRequest
from gateway.likeness import LikenessError

LIKENESS_PATH = "/gateway/v1/faces/likeness"
IDENTITY_PATH = "/gateway/v1/images/identity"
MAX_PART_BYTES = 64 * 1024 * 1024
MAX_PHOTOS = 6


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
            raise RuntimeError(f"face service returned {response.status_code}: {response.text[:200]}")
        return list(response.json()["faces"])


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


def error(status: int, code: str, message: str, *, param: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"message": message, "type": "invalid_request_error" if status < 500 else "api_error", "code": code}
    if param:
        body["param"] = param
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
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise IdentityError(f"{name} must be valid JSON", param=name) from exc


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


def parse_people(raw: Any, photo_count: int) -> list[PersonSpec]:
    """``people`` JSON: ``[{"label": "grandma", "image": 0, "box": [x1,y1,x2,y2], "infant": false}]``."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise IdentityError("people must be a JSON array", param="people")
    people: list[PersonSpec] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise IdentityError("each person must be an object", param="people")
        box = item.get("box")
        if box is not None:
            if not (isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box)):
                raise IdentityError("person box must be [x1, y1, x2, y2] in photo pixels", param="people")
            box = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        image = item.get("image", 0)
        if not isinstance(image, int) or not 0 <= image < photo_count:
            raise IdentityError("person image must be the index of an uploaded photo", param="people")
        label = item.get("label")
        if label is not None and not isinstance(label, str):
            raise IdentityError("person label must be a string", param="people")
        people.append(PersonSpec(
            label=label, image=image, box=box,
            description=item.get("description") if isinstance(item.get("description"), str) else None,
            infant=item.get("infant") if isinstance(item.get("infant"), bool) else None,
        ))
    return people


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
                          "description": "Reference photo(s) at full resolution. Never send tiny face crops: the gateway crops roomy heads itself."},
                "prompt": {"type": "string", "description": "The new scene, poses and gaze. People are tagged for you."},
                "people": _people_schema(),
                "attempts": {"type": "integer", "minimum": 1, "maximum": ids.MAX_ATTEMPTS, "default": 3,
                             "description": "Maximum seeds to try; stops early when every person meets the threshold. ~2 min each."},
                "seed": {"type": "integer", "minimum": 0, "default": 0},
                "resolution": {"type": "integer", "minimum": 512, "maximum": 2048, "default": 1536},
                "steps": {"type": "integer", "minimum": 1, "maximum": 60, "default": 25},
                "cfg": {"type": "number", "minimum": 0, "maximum": 10, "default": 1.0},
                "photo_as_canvas": {"type": "boolean", "default": True,
                                    "description": "Also pass the first photo whole as the style, clothing and setting reference."},
                "only_listed_people": {"type": "boolean", "default": True},
                "stop_when_pass": {"type": "boolean", "default": True},
                "response_format": {"type": "string", "enum": ["b64_json", "url"], "default": "b64_json"},
                "thresholds": {"type": "string", "description": "JSON object overriding match, uncertain and min_margin."},
            },
        }}},
    },
    "responses": {"200": {"description": "Best image plus likeness scores", "content": {"application/json": {"schema": {
        "type": "object",
        "properties": {
            "created": {"type": "integer"},
            "data": {"type": "array", "items": {"type": "object", "description": "asset_id, asset_url, media_type, b64_json|url"}},
            "likeness": {"type": "object", "description": "Same shape as POST /gateway/v1/faces/likeness for the selected image"},
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
        return await request.form(max_part_size=MAX_PART_BYTES)

    def face_deps() -> IdentityDeps:
        client = deps.face_client()
        return IdentityDeps(analyze=client.analyze, run_workflow=_unused_workflow, models=deps.models)

    async def likeness_route(request: Request):
        auth = guard(request, LIKENESS_PATH)
        if isinstance(auth, Response):
            return auth
        try:
            form = await form_of(request)
            photos = await _uploads(form, "reference", "reference[]")
            candidates = await _uploads(form, "candidate")
            if not photos or len(photos) > MAX_PHOTOS:
                raise IdentityError(f"send 1 to {MAX_PHOTOS} reference photos", param="reference")
            if len(candidates) != 1:
                raise IdentityError("send exactly one candidate image", param="candidate")
            req = SceneRequest(
                photos=photos, prompt="-", people=parse_people(_json_field(form, "people"), len(photos)),
                thresholds=parse_thresholds(_json_field(form, "thresholds")),
            )
            not_ready = await deps.ensure_face_service()
            if not_ready is not None:
                return not_ready
            fdeps = face_deps()
            people = await ids.resolve_people(req, fdeps, with_heads=False)
            candidate_faces = await ids._faces(fdeps, candidates[0])
            report = likeness.check_likeness(
                [p.person for p in people], candidate_faces, models=deps.models, thresholds=req.thresholds
            )
            report["references"] = [
                {"label": p.label, "photo": p.spec.image, "face_bbox": [round(v, 1) for v in p.face_bbox], "age_estimate": p.age}
                for p in people
            ]
            return JSONResponse(report)
        except (IdentityError, LikenessError) as exc:
            return error(400, getattr(exc, "code", "invalid_request"), str(exc), param=getattr(exc, "param", None))
        except (httpx.HTTPError, RuntimeError) as exc:
            return error(502, "face_service_error", f"face service failed: {exc or type(exc).__name__}")

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
            req = SceneRequest(
                photos=photos,
                prompt=str(form.get("prompt") or ""),
                people=parse_people(_json_field(form, "people"), len(photos)),
                attempts=_number(form, "attempts", int, 3, 1, ids.MAX_ATTEMPTS),
                seed=_number(form, "seed", int, 0, 0, 2**31 - 1),
                resolution=_number(form, "resolution", int, 1536, 512, 2048),
                steps=_number(form, "steps", int, 25, 1, 60),
                cfg=_number(form, "cfg", float, 1.0, 0, 10),
                only_listed_people=_bool(form, "only_listed_people", True),
                photo_as_canvas=_bool(form, "photo_as_canvas", True),
                stop_when_pass=_bool(form, "stop_when_pass", True),
                thresholds=parse_thresholds(_json_field(form, "thresholds")),
            )
            return await run_scene(request, auth, req, response_format)
        except (IdentityError, LikenessError) as exc:
            return error(400, getattr(exc, "code", "invalid_request"), str(exc), param=getattr(exc, "param", None))
        except (httpx.HTTPError, RuntimeError) as exc:
            return error(502, "identity_backend_error", f"identity workflow failed: {exc or type(exc).__name__}")

    async def run_scene(request: Request, auth: Any, req: SceneRequest, response_format: str) -> Response:
        """Shared by the native route and the /v1/images/edits alias."""
        not_ready = await deps.ensure_face_service()
        if not_ready is not None:
            return not_ready
        graph, binding = deps.template()

        async def run_workflow(workflow: dict[str, Any], uploads: dict[str, bytes]) -> bytes:
            return await deps.run_workflow(auth, workflow, uploads)

        fdeps = face_deps()
        fdeps.run_workflow = run_workflow
        result = await ids.run_identity_scene(
            req, fdeps, template=graph,
            image_nodes=list(binding["image_nodes"]), prompt_node=str(binding["prompt_node"]), seed_node=str(binding["seed_node"]),
        )
        item = deps.store_image(request, auth, result.image, response_format)
        payload = scene_payload(result, item)
        return JSONResponse(payload)

    app.add_api_route(LIKENESS_PATH, likeness_route, methods=["POST"], openapi_extra=LIKENESS_OPENAPI,
                      summary="Score how well the faces in an image match reference faces", tags=["identity"])
    app.add_api_route(IDENTITY_PATH, identity_route, methods=["POST"], openapi_extra=IDENTITY_OPENAPI,
                      summary="Put the people from reference photos into a new scene, with likeness scores", tags=["identity"])
    app.state.identity_run_scene = run_scene


async def _unused_workflow(graph: dict[str, Any], uploads: dict[str, bytes]) -> bytes:  # pragma: no cover
    raise RuntimeError("workflow runner was not provided")


def scene_payload(result: ids.SceneResult, item: Mapping[str, Any]) -> dict[str, Any]:
    import time

    return {
        "created": int(time.time()),
        "data": [dict(item)],
        "likeness": result.report,
        "attempts": result.attempts,
        "people": result.people,
        "seconds": result.seconds,
    }

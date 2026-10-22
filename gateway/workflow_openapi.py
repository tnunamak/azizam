"""Typed native OpenAPI for gateway workflows, generated from the manifests.

FastAPI cannot infer the native media routes (they take ``request: Request``),
so ``/openapi.json`` used to show ``requestBody: null`` for
``POST /gateway/v1/workflows/{workflow_id}/run`` and no typed inputs for the
manifest-backed knobs on the image, audio and video routes. This module is a
pure post-processor: it takes the FastAPI schema plus the loaded workflow
registry and adds

* one input schema per workflow, ``components.schemas["WorkflowInputs_<id>"]``;
* one concrete ``POST /gateway/v1/workflows/<short-id>/run`` path per workflow
  that the run route serves, with a multipart body typed from the manifest;
* a typed request body, 2xx response and error envelope for the other native
  routes that had none (media jobs, remix);
* ``extensions["com.azizam.workflow"]`` typed as ``anyOf`` the input schemas
  of the workflows each route can serve;
* the root extension ``x-azizam-workflows``: for every workflow its aliases,
  typical time, the sync route, the async route, which one to prefer, and where
  each input goes in the request (body field, extension, form file).

Why per-workflow schemas and not one discriminated union: the workflow is chosen
by the path (run route) or by ``model`` (other routes), and neither is a
property inside the typed object, so there is no discriminator for ``oneOf`` to
key on. A union without a discriminator validates "any workflow matches", which
tells a client nothing. Concrete per-workflow paths and per-workflow component
schemas are plain OpenAPI that validators, code generators and ``search()``-style
agent tools handle well; the union remains only as ``anyOf`` on the extension
block, where several workflows really do share one field.

The module is pure. It does not import FastAPI or ``proxy.py``.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable, Mapping

from gateway.audio_extensions import ROUTE_MANAGED_AUDIO_INPUTS
from gateway.discovery import WorkflowRecord
from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE
from gateway.video_extensions import ROUTE_MANAGED_VIDEO_INPUTS
from gateway.video_limits import limits_for_workflow

# Component names use only letters, digits and underscores: a dot in a name
# makes code generators (datamodel-code-generator) treat it as a module path.
SCHEMA_PREFIX = "WorkflowInputs_"
EXTENSION_SCHEMA_PREFIX = "WorkflowExtensionInputs_"
FORM_PREFIX = "WorkflowForm_"
ROOT_EXTENSION = "x-azizam-workflows"
SLOW_SECONDS = 30  # above this, prefer the async route when there is one

JOBS_SUBMIT = "/gateway/v1/media/jobs"
JOB_POLL = "/gateway/v1/media/jobs/{job_id}"
JOB_CONTENT = "/gateway/v1/media/jobs/{job_id}/content"
JOB_CANCEL = "/gateway/v1/media/jobs/{job_id}/cancel"
RUN_ROUTE = "/gateway/v1/workflows/{workflow_id}/run"

_JSON = "application/json"
_MULTIPART = "multipart/form-data"

_MEDIA_JOB_STATUSES = ["queued", "running", "succeeded", "failed", "canceled"]
_VIDEO_STATUSES = ["queued", "in_progress", "completed", "failed"]

# Inputs the image routes own at the top level (mirrors
# gateway.extension_catalog._ROUTE_MANAGED_WORKFLOW_INPUTS).
_IMAGE_GENERATE_MANAGED = frozenset({"prompt", "source_image", "width", "height"})
_IMAGE_EDIT_MANAGED = frozenset(
    {"prompt", "source_image", "width", "height", "mask", "source_mask", "mask_asset"}
)
# The multipart part the OpenAI-compatible edit route reads pictures from. One
# part per picture, in submission order; the field repeats.
_IMAGE_EDIT_IMAGE_PART = "image[]"

_JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


def short_id(record: WorkflowRecord) -> str:
    """``audio.generate.sa3-small-sfx`` -> ``sa3-small-sfx``."""
    prefix = f"{record.intent}."
    return record.id[len(prefix):] if record.id.startswith(prefix) else record.id


def schema_name(workflow_id: str, prefix: str = SCHEMA_PREFIX) -> str:
    """Component name for a workflow id: ``audio.generate.x-y`` -> ``..._audio_generate_x_y``."""
    return prefix + re.sub(r"[^A-Za-z0-9]+", "_", workflow_id).strip("_")


def extension_schema_name(workflow_id: str) -> str:
    """Component name for a workflow's extension-only input schema."""
    return schema_name(workflow_id, EXTENSION_SCHEMA_PREFIX)


def _schema_name(record: WorkflowRecord, prefix: str = SCHEMA_PREFIX) -> str:
    return schema_name(record.id, prefix)


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


# --------------------------------------------------------------------- schemas


def _input_schema(spec: Mapping[str, Any], *, form: bool) -> dict[str, Any]:
    kind = str(spec.get("type", "string"))
    if kind == "asset":
        if form:
            out: dict[str, Any] = {"type": "string", "format": "binary"}
        else:
            out = {
                "type": "string",
                "description": (
                    "Media input: a data URI, an http(s) URL, or a gateway "
                    "asset or video reference."
                ),
            }
        out["x-azizam-asset"] = True
    else:
        out = {"type": _JSON_TYPES.get(kind, "string")}
    for key in ("minimum", "maximum", "enum", "default"):
        if key in spec:
            out[key] = copy.deepcopy(spec[key])
    # A grid of accepted values (value = step * k + offset) is not expressible
    # as JSON Schema `multipleOf`, so publish it as an extension keyword.
    for key in ("step", "offset"):
        if key in spec:
            out[f"x-azizam-{key}"] = copy.deepcopy(spec[key])
    # The plain `help` line describes the input for people and API clients
    # alike; research provenance lives in docs/workflows/<id>.md, never here.
    note = spec.get("help") or spec.get("description")
    if note:
        base = out.get("description")
        out["description"] = f"{base} {note}" if base else note
    return out


def workflow_inputs_schema(record: WorkflowRecord, *, form: bool = False) -> dict[str, Any]:
    """JSON Schema for one workflow's manifest inputs."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, spec in (record.manifest.get("inputs") or {}).items():
        if not isinstance(spec, Mapping):
            continue
        properties[name] = _input_schema(spec, form=form)
        if spec.get("required"):
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "title": record.manifest.get("display_name") or record.id,
        "properties": properties,
        "additionalProperties": False,
        "x-azizam-workflow-id": record.id,
    }
    if required:
        schema["required"] = required
    description = record.manifest.get("summary") or record.manifest.get("description")
    if description:
        schema["description"] = " ".join(str(description).split())
    return schema


def advertised_inputs_schema(record: WorkflowRecord) -> dict[str, Any]:
    """The input schema callers are shown: the manifest inputs a request can set.

    It reads the same field map (``request_mapping``) the request is built and
    validated from, so an input a route sets itself (``{"in": "none"}``) in a
    mode the workflow offers is never advertised.
    """
    schema = workflow_inputs_schema(record)
    owned = gateway_owned_inputs(record)
    schema["properties"] = {name: spec for name, spec in schema["properties"].items() if name not in owned}
    required = [name for name in schema.get("required") or [] if name not in owned]
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    return schema


def extension_inputs_schema(record: WorkflowRecord) -> dict[str, Any]:
    """Extension-only view of a workflow's inputs.

    The video route reads these fields from the top-level body and refuses them
    inside ``extensions["com.azizam.workflow"]``, so the union advertised there
    must not name them. A route that shares one schema for both places keeps
    ``advertised_inputs_schema``.
    """
    schema = advertised_inputs_schema(record)
    mapping = request_mapping(record, "async")
    keep = {
        name for name, where in mapping.items()
        if where.get("in") in {"extension", "file+extension"}
    }
    schema["properties"] = {
        name: spec for name, spec in schema["properties"].items() if name in keep
    }
    required = [name for name in schema.get("required") or [] if name in keep]
    if required:
        schema["required"] = required
    else:
        schema.pop("required", None)
    return schema


def gateway_owned_inputs(record: WorkflowRecord) -> frozenset[str]:
    """Manifest inputs that a route the workflow offers sets itself."""
    execution = _execution(record, _typical_seconds(record))
    return frozenset(
        name
        for route in (execution["sync"], execution["async"]) if route
        for name, where in (route.get("fields") or {}).items() if where.get("in") == "none"
    )


# ------------------------------------------------------------- request mapping


def request_mapping(record: WorkflowRecord, mode: str) -> dict[str, dict[str, Any]]:
    """Where each manifest input goes in a request for ``mode``.

    ``mode`` is ``sync`` or ``async``. Each entry is one of

    * ``{"in": "body", "field": f}``: a top-level JSON (or form) field;
    * ``{"in": "body", "field": "size", "combine": ["width", "height"]}``:
      ``WIDTHxHEIGHT`` built from two inputs;
    * ``{"in": "extension", "field": name}``: a key under
      ``extensions["com.azizam.workflow"]``;
    * ``{"in": "file", "field": f}``: a multipart file part (repeat ``f``
      once per input that names it);
    * ``{"in": "file+extension", ...}``: a multipart file part plus an
      extension value ``{"multipart_field": f}``;
    * ``{"in": "form", "field": name}``: a plain multipart form field (run route);
    * ``{"in": "none"}``: accepted by the manifest, not settable on this route.
    """
    inputs = record.manifest.get("inputs") or {}
    intent = record.intent
    out: dict[str, dict[str, Any]] = {}
    is_asset = {n: (s or {}).get("type") == "asset" for n, s in inputs.items()}

    if mode == "sync" and gateway_route(record):  # a native route: plain multipart form
        for name in inputs:
            field = "image" if name == "source_image" else name
            out[name] = {"in": "file" if is_asset[name] else "form", "field": field}
        return out

    if intent == "video.edit" and mode == "sync":  # run route: plain form
        for name in inputs:
            out[name] = {"in": "file" if is_asset[name] else "form", "field": name}
        return out

    # The OpenAI-compatible edit route takes every reference picture as a
    # repeated ``image[]`` part, in submission order: the source picture first,
    # then each input a backend binding marks ``route_bound_inputs``. Naming a
    # second picture as its own part would describe a file that route never
    # reads, so the catalog would teach a request that cannot work.
    route_images = _native_route_image_inputs(record, is_asset) if intent == "image.edit" else []
    for name in route_images:
        out[name] = {"in": "file", "field": _IMAGE_EDIT_IMAGE_PART}

    for name in inputs:
        if name in out:
            continue
        if intent == "image.generate":
            managed = _IMAGE_GENERATE_MANAGED
            if name == "prompt":
                out[name] = {"in": "body", "field": "prompt"}
            elif name in ("width", "height"):
                out[name] = {"in": "body", "field": "size", "combine": ["width", "height"]}
            elif name in managed:
                out[name] = {"in": "none"}
            else:
                out[name] = {"in": "extension", "field": name}
        elif intent == "image.edit":
            if name == "prompt":
                out[name] = {"in": "form", "field": "prompt"}
            elif name == "source_image":
                out[name] = {"in": "file", "field": _IMAGE_EDIT_IMAGE_PART}
            elif name == "mask":
                out[name] = {"in": "file", "field": "mask"}
            elif name in ("width", "height"):
                out[name] = {"in": "form", "field": "size", "combine": ["width", "height"]}
            elif name in _IMAGE_EDIT_MANAGED:
                out[name] = {"in": "none"}
            elif is_asset[name]:
                out[name] = {"in": "file+extension", "field": name}
            else:
                out[name] = {"in": "extension", "field": name}
        elif intent == "audio.generate":
            if name in ROUTE_MANAGED_AUDIO_INPUTS:
                out[name] = {"in": "body", "field": name}
            else:
                out[name] = {"in": "extension", "field": name}
        elif intent.startswith("video."):
            if name in ("prompt", "negative_prompt", "seed", "image_strength"):
                out[name] = {"in": "body", "field": name}
            elif name in ("width", "height"):
                out[name] = {"in": "body", "field": "size", "combine": ["width", "height"]}
            elif name == "length":
                out[name] = {"in": "body", "field": "seconds"}
            elif name == "image":
                out[name] = {"in": "body", "field": "input_reference"}
            elif name in ROUTE_MANAGED_VIDEO_INPUTS:
                out[name] = {"in": "none"}
            else:
                out[name] = {"in": "extension", "field": name}
        else:
            out[name] = {"in": "extension", "field": name}
    return out


def gateway_route(record: WorkflowRecord) -> str | None:
    """The native route a gateway-composed workflow is served at, when it has one."""
    binding = (record.manifest.get("bindings") or {}).get("gateway")
    route = binding.get("route") if isinstance(binding, Mapping) else None
    return route if isinstance(route, str) and route.startswith("/") else None


def _native_route_image_inputs(
    record: WorkflowRecord, is_asset: Mapping[str, bool]
) -> list[str]:
    """Asset inputs the native edit route carries as repeated ``image[]`` parts.

    The source picture is the first part. Any backend binding's
    ``route_bound_inputs`` adds the pictures the route reads itself, in the
    manifest's order. Driven by manifest data only: no workflow id or engine
    name appears here.
    """
    bound: set[str] = set()
    bindings = record.manifest.get("bindings")
    if isinstance(bindings, Mapping):
        for binding in bindings.values():
            if not isinstance(binding, Mapping):
                continue
            declared = binding.get("route_bound_inputs")
            if isinstance(declared, list | tuple | set | frozenset):
                bound.update(
                    str(name) for name in declared if isinstance(name, str) and name
                )
    ordered = [name for name, asset in is_asset.items() if asset and name in bound and name != "source_image"]
    if is_asset.get("source_image"):
        ordered.insert(0, "source_image")
    return ordered


def route_inputs(record: WorkflowRecord) -> list[dict[str, Any]]:
    """Request fields the route owns that the manifest does not list.

    ``size`` and ``seconds`` are not manifest inputs for the LTX and MiniMax
    video workflows, but the video route takes them; ``n`` is the OpenAI image
    count.
    """
    inputs = record.manifest.get("inputs") or {}
    has_size = "width" in inputs and "height" in inputs
    out: list[dict[str, Any]] = []
    if gateway_route(record):
        return out
    if record.intent in ("image.generate", "image.edit"):
        out.append({"name": "n", "type": "integer", "minimum": 1, "maximum": 10,
                    "label": "number of results",
                    "description": "Number of images.", "in": "body", "field": "n"})
    if record.intent.startswith("video."):
        if "length" not in inputs:
            out.append({"name": "seconds", "type": "number", "minimum": 1,
                        "description": "Clip length in seconds.",
                        "in": "body", "field": "seconds"})
    if not has_size and record.intent != "audio.generate":
        out.append({"name": "size", "type": "string",
                    "description": "WIDTHxHEIGHT, e.g. 960x512.",
                    "in": "body" if record.intent != "image.edit" else "form",
                    "field": "size"})
    return out


def _execution(record: WorkflowRecord, typical_seconds: float | None) -> dict[str, Any]:
    """Sync and async routes for one workflow, and which to prefer."""
    intent = record.intent
    # Video workflows are configured as ``video-<short id>``; the rest use the short id.
    model = f"video-{short_id(record)}" if intent.startswith("video.") else short_id(record)
    sync: dict[str, Any] | None = None
    async_: dict[str, Any] | None = None

    def job_route(wrapped_intent: str) -> dict[str, Any]:
        return {
            "kind": "media_job",
            "submit": {
                "method": "POST",
                "path": JOBS_SUBMIT,
                "content_type": _JSON,
                "body": {"intent": wrapped_intent, "request": "<the sync request body>"},
                "model": model,
            },
            "poll": {"method": "GET", "path": JOB_POLL},
            "content": {"method": "GET", "path": JOB_CONTENT},
            "cancel": {"method": "POST", "path": JOB_CANCEL},
            "statuses": _MEDIA_JOB_STATUSES,
            "terminal": ["succeeded", "failed", "canceled"],
            "fields": request_mapping(record, "async"),
        }

    native = gateway_route(record)
    if native:
        sync = {"method": "POST", "path": native, "content_type": _MULTIPART,
                "fields": request_mapping(record, "sync"),
                "output": "JSON (see the route's response schema)"}
        if intent == "image.edit":
            sync["alternate"] = {"method": "POST", "path": "/v1/images/edits", "model": model,
                                 "note": "options go in extensions['com.azizam.workflow']"}
    elif intent == "image.generate":
        sync = {"method": "POST", "path": "/v1/images/generations", "content_type": _JSON,
                "model": model, "fields": request_mapping(record, "sync"),
                "output": "data[].b64_json or data[].url"}
        async_ = job_route("image.generate")
    elif intent == "image.edit":
        sync = {"method": "POST", "path": "/v1/images/edits", "content_type": _MULTIPART,
                "model": model, "fields": request_mapping(record, "sync"),
                "extensions_field": "extensions (JSON string)",
                "output": "data[].b64_json or data[].url"}
    elif intent == "audio.generate":
        sync = {"method": "POST", "path": "/v1/audio/generate", "content_type": _JSON,
                "model": model, "fields": request_mapping(record, "sync"),
                "output": "audio bytes (see Content-Type)"}
        async_ = job_route("audio.generate")
    elif intent.startswith("video."):
        async_ = {
            "kind": "video",
            "submit": {"method": "POST", "path": "/v1/videos", "content_type": _JSON,
                       "model": model},
            "poll": {"method": "GET", "path": "/v1/videos/{video_id}"},
            "content": {"method": "GET", "path": "/v1/videos/{video_id}/content"},
            "cancel": {"method": "DELETE", "path": "/v1/videos/{video_id}"},
            "statuses": _VIDEO_STATUSES,
            "terminal": ["completed", "failed"],
            "fields": request_mapping(record, "async"),
        }
        if intent == "video.edit":
            sync = {"method": "POST", "path": f"/gateway/v1/workflows/{short_id(record)}/run",
                    "content_type": _MULTIPART, "fields": request_mapping(record, "sync"),
                    "output": "video bytes (blocks until rendered)"}
    if typical_seconds is not None and typical_seconds <= SLOW_SECONDS and sync:
        recommended = "sync"
    elif async_:
        recommended = "async"
    else:
        recommended = "sync"
    return {"recommended": recommended, "sync": sync, "async": async_}


# --------------------------------------------------------------------- aliases

_ALIAS_OPERATION_INTENTS = {
    "images": ("image.generate",),
    "image_edits": ("image.edit",),
    "videos": ("video.generate", "video.edit"),
    "audio_generate": ("audio.generate",),
}


def match_alias_target(
    resolves_to: str, intent: str, shorts: Iterable[str]
) -> str | None:
    """Map an alias target like ``video-ltx-2.5-t2v`` to a workflow short id."""
    shorts = set(shorts)
    candidates = [resolves_to]
    kind = intent.split(".")[0]
    if resolves_to.startswith(f"{kind}-"):
        candidates.append(resolves_to[len(kind) + 1:])
    for cand in list(candidates):
        if cand.endswith("-edit"):
            candidates.append(cand[: -len("-edit")])
    for cand in candidates:
        if cand in shorts:
            return cand
    return None


def aliases_by_workflow(
    records: Iterable[WorkflowRecord], aliases: Mapping[str, Any] | None
) -> dict[str, list[str]]:
    """``{workflow id: [stable alias, ...]}`` from ``/gateway/v1/aliases`` data."""
    records = list(records)
    out: dict[str, list[str]] = {}
    for operation, intents in _ALIAS_OPERATION_INTENTS.items():
        section = (aliases or {}).get(operation) or {}
        for entry in section.get("stable") or []:
            target, alias = entry.get("resolves_to"), entry.get("alias")
            if not target or not alias:
                continue
            for intent in intents:
                by_short = {short_id(r): r for r in records if r.intent == intent}
                hit = match_alias_target(str(target), intent, by_short)
                if hit:
                    names = out.setdefault(by_short[hit].id, [])
                    if alias not in names:
                        names.append(str(alias))
    return out


# ------------------------------------------------------------------ the spec


def _typical_seconds(record: WorkflowRecord) -> float | None:
    value = record.manifest.get("typical_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def workflow_catalog(
    records: Iterable[WorkflowRecord],
    aliases: Mapping[str, Any] | None = None,
    limits: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The ``x-azizam-workflows`` list.

    ``limits`` maps a workflow id to its published limits (see ``video_limits``); a workflow
    with limits gets them as ``limits``.
    """
    records = sorted(records, key=lambda r: r.id)
    alias_map = aliases_by_workflow(records, aliases)
    catalog = []
    for record in records:
        typical = _typical_seconds(record)
        outputs = [
            {k: v for k, v in (("name", n), ("media_type", (s or {}).get("media_type"))) if v}
            for n, s in (record.manifest.get("outputs") or {}).items()
        ]
        catalog.append({
            "id": record.id,
            "short_id": short_id(record),
            "intent": record.intent,
            "display_name": record.manifest.get("display_name"),
            "description": " ".join(str(record.manifest.get("summary") or record.manifest.get("description") or "").split()),
            "aliases": alias_map.get(record.id, []),
            "typical_seconds": typical,
            "inputs_schema": _ref(_schema_name(record)),
            "outputs": outputs,
            "route_inputs": route_inputs(record),
            "execution": _execution(record, typical),
        })
        published = limits_for_workflow(limits or {}, record.id)
        if published:
            catalog[-1]["limits"] = dict(published)
    return catalog


_ERROR_REF = {"$ref": "#/components/schemas/GatewayErrorEnvelope"}

_ERROR_SCHEMA = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["message"],
            "properties": {
                "message": {"type": "string"},
                "type": {"type": "string"},
                "code": {"type": ["string", "null"]},
                "param": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        }
    },
}


def _errors() -> dict[str, Any]:
    def one(desc: str) -> dict[str, Any]:
        return {"description": desc, "content": {_JSON: {"schema": _ERROR_REF}}}
    return {
        "400": one("Invalid input. `error.message` names the field and the rule."),
        "401": one("Missing or invalid bearer token."),
        "403": one("Not permitted by the app profile."),
        "404": one("Unknown workflow, model, or job."),
        "502": one("Backend failed while rendering."),
        "503": one("Backend unavailable or disabled."),
    }


_MEDIA_JOB_SCHEMA = {
    "type": "object",
    "required": ["job_id", "status"],
    "properties": {
        "object": {"type": "string", "enum": ["media.job"]},
        "id": {"type": "string"},
        "job_id": {"type": "string"},
        "intent": {"type": "string"},
        "status": {"type": "string", "enum": _MEDIA_JOB_STATUSES},
        "progress": {"type": "integer", "minimum": 0, "maximum": 100},
        "error": {"type": ["string", "object", "null"]},
        "error_code": {"type": ["string", "null"], "description": "A short token naming the failure."},
        "error_category": {
            "type": ["string", "null"],
            "enum": ["authorization", "invalid_input", "upstream", "capacity", "timeout", "safety", "internal", None],
            "description": ("Who refused: the gateway (authorization, invalid_input, capacity), the work "
                            "(upstream, timeout, safety), or a gateway bug (internal)."),
        },
        "retryable": {"type": ["boolean", "null"], "description": "Whether the same job may succeed later."},
        "upstream_http_status": {
            "type": ["integer", "null"],
            "description": "The HTTP status of an upstream failure; null when the gateway refused.",
        },
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string"},
                    "kind": {"type": "string"},
                    "media_type": {"type": "string"},
                    "size_bytes": {"type": "integer"},
                },
                "additionalProperties": True,
            },
        },
        "poll_url": {"type": "string"},
        "content_url": {"type": "string"},
    },
    "additionalProperties": True,
}

_MEDIA_JOB_CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "object": {"type": "string"},
        "job_id": {"type": "string"},
        "data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asset_id": {"type": "string"},
                    "media_type": {"type": "string"},
                    "url": {"type": "string", "description": "Signed URL; fetch without a bearer token."},
                    "b64_json": {"type": "string"},
                },
                "additionalProperties": True,
            },
        },
    },
    "additionalProperties": True,
}


def _json_body(schema: Mapping[str, Any], required: bool = True) -> dict[str, Any]:
    return {"required": required, "content": {_JSON: {"schema": dict(schema)}}}


def _by_intent(records: Iterable[WorkflowRecord], *prefixes: str) -> list[WorkflowRecord]:
    return [r for r in records if any(r.intent.startswith(p) for p in prefixes)]


def _extension_property(records: list[WorkflowRecord]) -> dict[str, Any]:
    def ref(record: WorkflowRecord) -> dict[str, str]:
        # The video route takes its own fields at the top level and refuses
        # them inside the extension namespace, so advertise the extension-only
        # input schema there. Other routes share one schema for both places.
        prefix = EXTENSION_SCHEMA_PREFIX if record.intent == "video.generate" else SCHEMA_PREFIX
        return _ref(_schema_name(record, prefix))

    return {
        "type": "object",
        "description": (
            "Gateway-owned controls. Send only inputs the selected workflow "
            "lists; see `x-azizam-workflows` for which inputs belong here."
        ),
        "properties": {
            WORKFLOW_EXTENSION_NAMESPACE: {
                "anyOf": [ref(r) for r in records],
                "description": "Inputs of the workflow named by `model`.",
            }
        },
        "additionalProperties": True,
    }


def _patch_extensions(op: dict[str, Any], records: list[WorkflowRecord]) -> None:
    if not records:
        return
    content = ((op.get("requestBody") or {}).get("content") or {}).get(_JSON)
    schema = (content or {}).get("schema")
    if isinstance(schema, dict):
        schema.setdefault("properties", {})["extensions"] = _extension_property(records)


def _async_extension(op: dict[str, Any], mode: str, **fields: Any) -> None:
    op[f"x-azizam-{mode}"] = fields


def augment_openapi(
    spec: dict[str, Any],
    records: Iterable[WorkflowRecord],
    aliases: Mapping[str, Any] | None = None,
    video_limits: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Add the typed native surface to a FastAPI OpenAPI document, in place."""
    records = sorted(records, key=lambda r: r.id)
    info = spec.setdefault("info", {})
    if info.get("title") in (None, "FastAPI"):
        info["title"] = "Azizam"
    info["description"] = (
        "OpenAI-compatible gateway plus native workflow routes. Discover with "
        "`GET /gateway/v1/workflows` and the `x-azizam-workflows` list in this "
        "document: each workflow names its sync route, its async route, and where "
        "every input goes. Slow work (video, long music) goes through an async "
        "route: submit, poll, then fetch the content."
    )
    components = spec.setdefault("components", {}).setdefault("schemas", {})
    components["GatewayErrorEnvelope"] = copy.deepcopy(_ERROR_SCHEMA)
    components["MediaJob"] = copy.deepcopy(_MEDIA_JOB_SCHEMA)
    components["MediaJobContent"] = copy.deepcopy(_MEDIA_JOB_CONTENT_SCHEMA)
    for record in records:
        components[_schema_name(record)] = advertised_inputs_schema(record)
        if record.intent == "video.generate":
            components[_schema_name(record, EXTENSION_SCHEMA_PREFIX)] = extension_inputs_schema(record)

    paths = spec.setdefault("paths", {})
    errors = _errors()

    # Typed workflow knobs on the routes that take them.
    route_records = {
        ("/v1/images/generations", "/openai/v1/images/generations"): _by_intent(records, "image.generate"),
        ("/v1/images/edits", "/openai/v1/images/edits"): [],  # multipart; documented in the catalog
        ("/v1/videos", "/openai/v1/videos"): _by_intent(records, "video."),
        ("/v1/audio/generate", "/openai/v1/audio/generate"): _by_intent(records, "audio."),
    }
    for route_paths, recs in route_records.items():
        for path in route_paths:
            op = (paths.get(path) or {}).get("post")
            if op:
                _patch_extensions(op, recs)
    for path in ("/v1/videos", "/openai/v1/videos"):
        op = (paths.get(path) or {}).get("post")
        if op:
            _async_extension(
                op, "async", poll="GET /v1/videos/{video_id}",
                content="GET /v1/videos/{video_id}/content",
                statuses=_VIDEO_STATUSES,
            )

    # Media jobs: the async route for image, audio and video generation. The
    # job's `request` is the body of the matching sync route, so type it with
    # the (already extension-typed) route bodies.
    request_refs = []
    for component, path in (
        ("ImageGenerationRequest", "/v1/images/generations"),
        ("AudioGenerationRequest", "/v1/audio/generate"),
        ("VideoGenerationRequest", "/v1/videos"),
    ):
        body = _route_json_schema(paths, path)
        if body is not None:
            components[component] = copy.deepcopy(body)
            request_refs.append(_ref(component))
    submit_body = {
        "type": "object",
        "required": ["intent", "request"],
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["image.generate", "audio.generate", "music.generate",
                         "sfx.generate", "video.generate"],
            },
            "request": {
                "description": (
                    "The body you would send to the matching sync route: "
                    "`/v1/images/generations` (image.generate), `/v1/audio/generate` "
                    "(audio.generate, music.generate, sfx.generate) or `/v1/videos` "
                    "(video.generate)."
                ),
                **({"anyOf": request_refs} if request_refs
                   else {"type": "object", "additionalProperties": True}),
            },
        },
    }
    job_ok = {"description": "Job accepted. Poll `poll_url`.",
              "content": {_JSON: {"schema": _ref("MediaJob")}}}
    if JOBS_SUBMIT in paths and "post" in paths[JOBS_SUBMIT]:
        op = paths[JOBS_SUBMIT]["post"]
        op["requestBody"] = _json_body(submit_body)
        op["responses"] = {"202": job_ok, "201": job_ok, **errors}
        op["summary"] = "Submit durable async media work"
        op["description"] = (
            "Async route for image, audio and video generation. Poll "
            "`GET /gateway/v1/media/jobs/{job_id}` until `status` is terminal "
            "(`succeeded`, `failed`, `canceled`), then fetch "
            "`/gateway/v1/media/jobs/{job_id}/content` for signed asset URLs."
        )
    image_job = paths.get("/gateway/v1/media/jobs/image-generations", {}).get("post")
    if image_job is not None and "ImageGenerationRequest" in components:
        image_job["requestBody"] = _json_body(_ref("ImageGenerationRequest"))
        image_job["responses"] = {"202": job_ok, "200": job_ok, **errors}
    poll = paths.get(JOB_POLL, {}).get("get")
    if poll is not None:
        poll["responses"] = {
            "200": {"description": "Job state.", "content": {_JSON: {"schema": _ref("MediaJob")}}},
            "404": errors["404"],
        }
    content = paths.get(JOB_CONTENT, {}).get("get")
    if content is not None:
        content["responses"] = {
            "200": {"description": "Signed URLs of the finished assets.",
                    "content": {_JSON: {"schema": _ref("MediaJobContent")}}},
            "409": {"description": "Job not finished yet (`media_job_not_ready`).",
                    "content": {_JSON: {"schema": _ERROR_REF}}},
            "410": {"description": "Output files are missing (`media_job_outputs_missing`).",
                    "content": {_JSON: {"schema": _ERROR_REF}}},
            "503": {"description": "Output storage is temporarily unavailable (`media_job_outputs_unavailable`); retry later.",
                    "headers": {"Retry-After": {"description": "Seconds before retrying.", "schema": {"type": "integer"}}},
                    "content": {_JSON: {"schema": _ERROR_REF}}},
            "404": errors["404"],
        }

    # Remix: JSON body typed.
    remix_body = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string"},
            "seconds": {"type": ["integer", "number", "string"]},
            "size": {"type": "string"},
            "seed": {"type": "integer"},
            "negative_prompt": {"type": "string"},
        },
        "additionalProperties": True,
    }
    for path in ("/v1/videos/{video_id}/remix", "/openai/v1/videos/{video_id}/remix"):
        op = (paths.get(path) or {}).get("post")
        if op is not None:
            op["requestBody"] = _json_body(remix_body)
            op.setdefault("responses", {}).update({k: v for k, v in errors.items()
                                                  if k not in op["responses"]})

    # Workflow run route: generic template typed, plus a concrete path per workflow.
    run_records = [r for r in records if r.intent == "video.edit"]
    for record in run_records:
        form_name = _schema_name(record, FORM_PREFIX)
        components[form_name] = workflow_inputs_schema(record, form=True)
    generic = paths.get(RUN_ROUTE, {}).get("post")
    if generic is not None:
        generic["summary"] = "Run a video-edit workflow synchronously"
        generic["description"] = (
            "Serves `video.edit` workflows only (`GET /gateway/v1/workflows` lists "
            "them). The request blocks until the render ends and returns the video "
            "bytes. For anything slower than a few seconds prefer the async route "
            "`POST /v1/videos` (see `x-azizam-workflows`). Every workflow also "
            "has a concrete path below with its exact form fields."
        )
        generic["requestBody"] = {
            "required": True,
            "content": {_MULTIPART: {"schema": {
                "oneOf": [_ref(_schema_name(r, FORM_PREFIX)) for r in run_records]
            } if run_records else {"type": "object"}}},
        }
        generic["responses"] = {
            "200": {"description": "Rendered video.",
                    "content": {"video/mp4": {"schema": {"type": "string", "format": "binary"}}}},
            **errors,
        }
    for record in run_records:
        path = f"/gateway/v1/workflows/{short_id(record)}/run"
        outputs = (record.manifest.get("outputs") or {})
        media = next((o.get("media_type") for o in outputs.values() if o.get("media_type")), "video/mp4")
        paths[path] = {"post": {
            "tags": ["gateway-workflows"],
            "operationId": f"run_{short_id(record).replace('.', '_').replace('-', '_')}",
            "summary": f"Run {record.manifest.get('display_name') or record.id}",
            "description": " ".join(str(record.manifest.get("summary") or record.manifest.get("description") or "").split()),
            "x-azizam-workflow-id": record.id,
            "requestBody": {"required": True, "content": {
                _MULTIPART: {"schema": _ref(_schema_name(record, FORM_PREFIX))}}},
            "responses": {
                "200": {"description": "Rendered output.",
                        "content": {media: {"schema": {"type": "string", "format": "binary"}}}},
                **errors,
            },
        }}

    spec[ROOT_EXTENSION] = workflow_catalog(records, aliases, video_limits)
    return spec


def _route_json_schema(paths: Mapping[str, Any], path: str) -> dict[str, Any] | None:
    """The inline JSON request schema of a POST route, if it has one."""
    op = (paths.get(path) or {}).get("post") or {}
    content = ((op.get("requestBody") or {}).get("content") or {}).get(_JSON) or {}
    schema = content.get("schema")
    return schema if isinstance(schema, dict) and "$ref" not in schema else None

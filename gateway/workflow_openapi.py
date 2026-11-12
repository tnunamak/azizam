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
from typing import Any, Iterable, Mapping, Sequence

from gateway.workflow_view import settable_inputs
from gateway.discovery import WorkflowRecord
from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE
from gateway import compatibility, media_job_intents, workflow_request_maps
from gateway.manifests import media_bearing
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
# The contract input a video request's ``input_reference`` fills. The projection and the route read it from here.
VIDEO_REFERENCE_INPUT = "image"
_MESH_PATH = "/v1/3d/generations/glb"

_JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "object": "object",
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
    """JSON Schema for one input spec, following the type grammar (CODE_TO_DATA_S3, D4): an object lists its closed
    ``properties``, an array its ``items``, and an asset is a media reference wherever it sits. A form request marks the
    asset itself as a binary part, never the array or object that holds it."""
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
    elif kind == "array":
        out = {"type": "array"}
        for key, word in (("min_items", "minItems"), ("max_items", "maxItems")):
            if key in spec:
                out[word] = spec[key]
        if isinstance(spec.get("items"), Mapping):
            out["items"] = _input_schema(spec["items"], form=form)
    else:
        out = {"type": _JSON_TYPES.get(kind, "string")}
        properties = spec.get("properties")
        if kind == "object" and isinstance(properties, Mapping):
            children = {str(name): child for name, child in properties.items() if isinstance(child, Mapping)}
            out["properties"] = {name: _input_schema(child, form=form) for name, child in children.items()}
            out["additionalProperties"] = False
            required = [name for name, child in children.items() if child.get("required")]
            if required:
                out["required"] = required
        if kind == "string":
            for key, word in (("min_length", "minLength"), ("max_length", "maxLength")):
                if key in spec:
                    out[word] = spec[key]
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
    for name, spec in settable_inputs(record.contract).items():
        if not isinstance(spec, Mapping):
            continue
        properties[name] = _input_schema(spec, form=form)
        if spec.get("required"):
            required.append(name)
    schema: dict[str, Any] = {
        "type": "object",
        "title": record.contract.get("display_name") or record.id,
        "properties": properties,
        "additionalProperties": False,
        "x-azizam-workflow-id": record.id,
    }
    if required:
        schema["required"] = required
    description = record.contract.get("summary") or record.contract.get("description")
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

    An input that holds media inside a list or object is ``{"in": "none"}`` on every route: no route resolves the
    references inside a container yet, so none can honestly advertise one.

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
    if mode not in ("sync", "async"):
        raise ValueError(f"{record.id} ({mode}): unsupported request mode")
    # Where each input goes is data: the rows in registry/workflow_request_maps.yml (gateway/workflow_request_maps.py).
    row = workflow_request_maps.select(record, mode)
    if row is None:
        raise ValueError(f"{record.id} ({mode}): no row in registry/workflow_request_maps.yml places its inputs")
    out = workflow_request_maps.place(row, record, mode)
    for name, spec in settable_inputs(record.contract).items():
        if media_bearing(spec) and spec.get("type") != "asset":
            out[name] = {"in": "none"}
    _reference_input_from_mapping(record, mode, out)
    return out


def reference_input(record: WorkflowRecord, mode: str) -> str | None:
    """The scalar-asset contract key placed at top-level input_reference."""
    fields = request_mapping(record, mode)
    return _reference_input_from_mapping(record, mode, fields)


def _reference_input_from_mapping(
    record: WorkflowRecord,
    mode: str,
    fields: Mapping[str, Mapping[str, Any]],
) -> str | None:
    """Invert the completed placement map without selecting or resolving a row."""
    if mode not in ("sync", "async"):
        raise ValueError(f"{record.id} ({mode}): unsupported request mode")
    candidates = sorted(
        name for name, where in fields.items()
        if where.get("in") == "body" and where.get("field") == "input_reference"
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        raise ValueError(
            f"{record.id} ({mode}): multiple inputs map to body.input_reference: {candidates!r}"
        )
    name = candidates[0]
    if "combine" in fields[name]:
        raise ValueError(f"{record.id} ({mode}): input {name!r} at body.input_reference cannot combine inputs")
    inputs = record.contract.get("inputs")
    spec = inputs.get(name) if isinstance(inputs, Mapping) else None
    if (not isinstance(spec, Mapping) or spec.get("type") != "asset"
            or name not in settable_inputs(record.contract)):
        raise ValueError(
            f"{record.id} ({mode}): input {name!r} at body.input_reference must be a settable scalar asset"
        )
    return name


def gateway_route(record: WorkflowRecord) -> str | None:
    """The native route a gateway-composed workflow is served at, when it has one."""
    binding = (record.contract.get("bindings") or {}).get("gateway")
    route = binding.get("route") if isinstance(binding, Mapping) else None
    return route if isinstance(route, str) and route.startswith("/") else None


def _bound_input_names(record: WorkflowRecord) -> set[str]:
    """Manifest inputs some service binding maps to a service field."""
    bindings = record.contract.get("bindings")
    if not isinstance(bindings, Mapping):
        return set()
    names: set[str] = set()
    for binding in bindings.values():
        mapping = binding.get("map") if isinstance(binding, Mapping) else None
        if isinstance(mapping, Mapping):
            names.update(str(name) for name in mapping)
    return names


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
    bindings = record.contract.get("bindings")
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


def _carried_by_run_route(record: WorkflowRecord) -> bool:
    """True when the route a run uses is the generic run route, which takes only its own field mapping."""
    execution = _execution(record, _typical_seconds(record))
    route = execution[execution["recommended"]]
    return bool(route) and route.get("path") == f"/gateway/v1/workflows/{record.id}/run"


def route_inputs(record: WorkflowRecord) -> list[dict[str, Any]]:
    """Request fields the route owns that the manifest does not list.

    ``size`` and ``seconds`` are not manifest inputs for the LTX and MiniMax
    video workflows, but the video route takes them; ``n`` is the OpenAI image
    count.
    """
    inputs = settable_inputs(record.contract)
    out: list[dict[str, Any]] = []
    if gateway_route(record) or _carried_by_run_route(record):
        return out
    execution = workflow_request_maps._MAPS.execution
    row = workflow_request_maps.execution_row(record.intent)
    if row is None:
        return out
    for ref in row.route_inputs:
        field_row = execution.field_rows[ref]
        guard = field_row["unless_all_inputs"]
        if guard and all(name in inputs for name in guard):
            continue
        # The strict reader restricts field values to scalars.
        out.append(dict(field_row["value"]))
    return out


def _execution(record: WorkflowRecord, typical_seconds: float | None) -> dict[str, Any]:
    """Sync and async routes for one workflow, and which to prefer."""
    intent = record.intent
    native = gateway_route(record)
    row = workflow_request_maps.execution_row(intent)
    model = (row.model_prefix if row is not None else "") + short_id(record)
    sync: dict[str, Any] | None = None
    async_: dict[str, Any] | None = None

    if native:
        sync = {"method": "POST", "path": native, "content_type": _MULTIPART,
                "fields": request_mapping(record, "sync"),
                "output": "JSON (see the route's response schema)",
                "response": {"kind": "json"}}
        if row is not None and row.native_alternate is not None:
            sync["alternate"] = workflow_request_maps.render_alternate(row, model=model)
    else:
        sync = workflow_request_maps.render_sync(record)
        async_ = workflow_request_maps.render_async(record)
    if typical_seconds is not None and typical_seconds <= SLOW_SECONDS and sync:
        recommended = "sync"
    elif async_:
        recommended = "async"
    else:
        recommended = "sync"
    return {"recommended": recommended, "sync": sync, "async": async_}


# --------------------------------------------------------------------- aliases

def _target_prefixes(intent: str) -> set[str]:
    relations = compatibility.COMPATIBILITY.alias_operation_intents
    return {row.target_prefix for rows in relations.values() for row in rows if row.intent == intent}


def match_alias_target(
    resolves_to: str, intent: str, shorts: Iterable[str]
) -> str | None:
    """Map an alias target like ``video-ltx-2.5-t2v`` to a workflow short id."""
    shorts = set(shorts)
    candidates = [resolves_to]
    for prefix in sorted(_target_prefixes(intent)):
        if resolves_to.startswith(prefix):
            candidates.append(resolves_to[len(prefix):])
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
    """``{workflow id: [stable alias, ...]}`` from ``/gateway/v1/aliases`` data.

    Which intents each alias section may name comes from registry/compatibility.yml. A target
    that is a canonical workflow id names that workflow only; other targets are short names."""
    records = list(records)
    by_id = {record.id: record for record in records}
    out: dict[str, list[str]] = {}
    for operation, rows in compatibility.COMPATIBILITY.alias_operation_intents.items():
        intents = [row.intent for row in rows]
        section = (aliases or {}).get(operation) or {}
        for entry in section.get("stable") or []:
            target, alias = entry.get("resolves_to"), entry.get("alias")
            if not target or not alias:
                continue
            target = str(target)
            if target in by_id:
                hits = [by_id[target]] if by_id[target].intent in intents else []
            else:
                hits = []
                for intent in intents:
                    by_short = {short_id(r): r for r in records if r.intent == intent}
                    hit = match_alias_target(target, intent, by_short)
                    if hit:
                        hits.append(by_short[hit])
            for record in hits:
                names = out.setdefault(record.id, [])
                if alias not in names:
                    names.append(str(alias))
    return out


# ------------------------------------------------------------------ the spec


def _typical_seconds(record: WorkflowRecord) -> float | None:
    value = record.contract.get("typical_seconds")
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
    workflow_request_maps.validate_execution_records(records)
    alias_map = aliases_by_workflow(records, aliases)
    catalog = []
    for record in records:
        typical = _typical_seconds(record)
        outputs = [
            {k: v for k, v in (("name", n), ("media_type", (s or {}).get("media_type"))) if v}
            for n, s in (record.contract.get("outputs") or {}).items()
        ]
        catalog.append({
            "id": record.id,
            "short_id": short_id(record),
            "intent": record.intent,
            "display_name": record.contract.get("display_name"),
            "description": " ".join(str(record.contract.get("summary") or record.contract.get("description") or "").split()),
            "aliases": alias_map.get(record.id, []),
            "typical_seconds": typical,
            "status": record.contract.get("status") or "current",
            "superseded_by": record.contract.get("superseded_by"),
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


def _by_route(
    records: Iterable[WorkflowRecord], path: str | Mapping[str, str],
    *, content_type: str | None = None,
) -> list[WorkflowRecord]:
    """Records whose declared sync or async submission uses this path or path slot."""
    selected = []
    for record in records:
        row = workflow_request_maps.execution_row(record.intent)
        if row is None:
            continue
        routes = (row.sync, row.async_["submit"] if row.async_ is not None else None)
        for route in routes:
            if route is not None and route["path"] == path \
                    and (content_type is None or route["content_type"] == content_type):
                selected.append(record)
                break
    return selected


def _uses_extension_schema(record: WorkflowRecord) -> bool:
    """The resource transport separates top-level fields from extension inputs."""
    row = workflow_request_maps.execution_row(record.intent)
    # This is the published async transport kind, not the workflow's modality.
    return row is not None and row.async_ is not None and row.async_["kind"] == "video"


def _extension_property(records: list[WorkflowRecord]) -> dict[str, Any]:
    def ref(record: WorkflowRecord) -> dict[str, str]:
        # The video route takes its own fields at the top level and refuses
        # them inside the extension namespace, so advertise the extension-only
        # input schema there. Other routes share one schema for both places.
        prefix = EXTENSION_SCHEMA_PREFIX if _uses_extension_schema(record) else SCHEMA_PREFIX
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
        properties = schema.setdefault("properties", {})
        patched = _extension_property(records)
        # Keep the namespaces the route schema already declares (descriptor-owned controls); add the workflow one.
        declared = (properties.get("extensions") or {}).get("properties") or {}
        patched["properties"] = {**declared, **patched["properties"]}
        properties["extensions"] = patched


def _async_extension(op: dict[str, Any], mode: str, **fields: Any) -> None:
    op[f"x-azizam-{mode}"] = fields


def augment_openapi(
    spec: dict[str, Any],
    records: Iterable[WorkflowRecord],
    aliases: Mapping[str, Any] | None = None,
    video_limits: Mapping[str, Mapping[str, Any]] | None = None,
    declared_intents: Sequence[str] = (),
) -> dict[str, Any]:
    """Add the typed native surface to a FastAPI OpenAPI document, in place.

    ``declared_intents`` are the operations enabled engines declare (declared_operation_job.intents); with none, the
    document is the one the static intent table alone gives."""
    records = sorted(records, key=lambda r: r.id)
    workflow_request_maps.validate_execution_records(records)
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
        if _uses_extension_schema(record):
            components[_schema_name(record, EXTENSION_SCHEMA_PREFIX)] = extension_inputs_schema(record)

    paths = spec.setdefault("paths", {})
    errors = _errors()

    # Typed workflow knobs on the routes that take them.
    route_records = {
        ("/v1/images/generations", "/openai/v1/images/generations"): _by_route(records, "/v1/images/generations"),
        ("/v1/images/edits", "/openai/v1/images/edits"): [],  # multipart; documented in the catalog
        ("/v1/videos", "/openai/v1/videos"): _by_route(records, "/v1/videos"),
        ("/v1/audio/generate", "/openai/v1/audio/generate"): _by_route(records, "/v1/audio/generate"),
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
    for component, path, _intents in media_job_intents.submit_routes():
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
                "enum": list(media_job_intents.SUBMITTABLE_INTENTS),
            },
            "request": {
                "description": media_job_intents.request_description(),
                **({"anyOf": request_refs} if request_refs
                   else {"type": "object", "additionalProperties": True}),
            },
        },
    }
    if declared_intents:
        # A declared operation takes its input object and a model, not the body of a sync route. Each shape names its
        # own intents, so a shipped intent cannot be sent with the declared shape or the other way round.
        declared_body = {
            "type": "object",
            "required": ["intent", "model", "input"],
            "properties": {
                "intent": {"type": "string", "enum": list(declared_intents)},
                "model": {"type": "string", "description": "The offering that serves a declared intent."},
                "input": {
                    "type": "object", "additionalProperties": True,
                    "description": "The input object of a declared intent; the engine's description decides which fields it uses."},
            },
        }
        submit_body = {"oneOf": [submit_body, declared_body]}
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
    run_records = _by_route(records, {"from": "workflow_run_path"}, content_type=_MULTIPART)
    for record in run_records:
        form_name = _schema_name(record, FORM_PREFIX)
        components[form_name] = workflow_inputs_schema(record, form=True)
    generic = paths.get(RUN_ROUTE, {}).get("post")
    if generic is not None:
        generic["summary"] = "Run a workflow by its full id"
        generic["description"] = (
            "Starts a media job for the workflow with this full id (`GET /gateway/v1/workflows` "
            "lists them) and answers the job: 202 while it is queued or running, with its "
            "address in `Location`. Read it at `GET /gateway/v1/media/jobs/{job_id}` and its "
            "output at `GET /gateway/v1/media/jobs/{job_id}/content`. `Prefer: wait=N` waits "
            "up to N seconds for the job to end before it answers. Each video-edit workflow "
            "also has a concrete path below with its exact form fields."
        )
        generic["requestBody"] = {
            "required": True,
            "content": {_MULTIPART: {"schema": {
                "oneOf": [_ref(_schema_name(r, FORM_PREFIX)) for r in run_records]
            } if run_records else {"type": "object"}}},
        }
        generic["responses"] = {
            "200": {"description": "The job, ended within `Prefer: wait`.",
                    "content": {_JSON: {"schema": {"type": "object"}}}},
            "202": {"description": "The job, queued or running; `Location` is its address.",
                    "content": {_JSON: {"schema": {"type": "object"}}}},
            **errors,
        }
    for record in run_records:
        path = f"/gateway/v1/workflows/{record.id}/run"        # the route takes full ids only (design S4-S8 3.0)
        paths[path] = {"post": {
            "tags": ["gateway-workflows"],
            "operationId": f"run_{short_id(record).replace('.', '_').replace('-', '_')}",
            "summary": f"Run {record.contract.get('display_name') or record.id}",
            "description": " ".join(str(record.contract.get("summary") or record.contract.get("description") or "").split()),
            "x-azizam-workflow-id": record.id,
            "requestBody": {"required": True, "content": {
                _MULTIPART: {"schema": _ref(_schema_name(record, FORM_PREFIX))}}},
            "responses": {
                "200": {"description": "The job, ended within `Prefer: wait`.",
                        "content": {_JSON: {"schema": {"type": "object"}}}},
                "202": {"description": "The job, queued or running; `Location` is its address.",
                        "content": {_JSON: {"schema": {"type": "object"}}}},
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

"""Capture functions for the code-to-data parity goldens (tests/fixtures/c2d_parity/).

The parity tests compare these functions' output with the stored goldens; capture.py writes the goldens
from the same functions. Nothing here reads a golden.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from gateway import capability_ids, extension_catalog, models_catalog
from gateway.capabilities import Capability, capability_from_workflow_record
from gateway.discovery import VALID_AUDIENCES
from gateway.local_capabilities import build_local_capabilities
from tests.support import c2d_parity
from tests.test_local_capabilities import REPRESENTATIVE_CONFIG

UNFAMILIAR_INTENT = "frobnicate.widget-9"


# ------------------------------------------------------------------ capability_ids


def shipped_intents() -> list[str]:
    import yaml

    return sorted({
        str(yaml.safe_load(path.read_text(encoding="utf-8"))["intent"])
        for path in sorted(c2d_parity.MANIFESTS.glob("*.yml"))
    })


def capability_words() -> list[str]:
    """Every word the tables key, every shipped workflow intent, and words no table knows (sorted, deduplicated)."""
    words = {
        *capability_ids.PROTOCOL_CAPABILITIES, *capability_ids.INTENT_CAPABILITIES, *shipped_intents(),
        UNFAMILIAR_INTENT, "openai", "  chat  ", "",
    }
    return sorted(words)


def capture_capability_ids() -> dict[str, Any]:
    words = capability_words()
    endpoints = ["/chat/completions", "/completions", "/embeddings", "/images", "/rerank", "/unknown"]
    return c2d_parity.as_golden({
        "words": words,
        "capability_of_word": {word: capability_ids.capability_of_word(word) for word in words},
        "offered_model_kinds": {word: capability_ids.offered_model_kinds([word]) for word in words},
        "inner_hop_endpoints_for": {word: sorted(capability_ids.inner_hop_endpoints_for([word])) for word in words},
        "capabilities_without_inner_endpoint": {
            word: capability_ids.capabilities_without_inner_endpoint([word]) for word in words
        },
        "inner_hop_endpoints_for_combined": {
            "chat+embeddings": sorted(capability_ids.inner_hop_endpoints_for(["chat", "embeddings"])),
            "rerank+image": sorted(capability_ids.inner_hop_endpoints_for(["rerank", "image"])),
            "none": sorted(capability_ids.inner_hop_endpoints_for([])),
        },
        "inner_hop_endpoint_served": {ep: capability_ids.inner_hop_endpoint_served(ep) for ep in endpoints},
        # Table order is the contract for INNER_HOP_ENDPOINTS (the first path of a capability is its primary one).
        "inner_hop_endpoints_table": {key: list(paths) for key, paths in capability_ids.INNER_HOP_ENDPOINTS.items()},
        # proxy.py reads admission as ``INNER_HOP_ADMISSION.get(endpoint, ("chat.generate", "chat"))``.
        "inner_hop_admission": {
            ep: list(capability_ids.INNER_HOP_ADMISSION.get(ep, ("chat.generate", "chat"))) for ep in endpoints
        },
        "labels": {
            "CAPABILITY_LABELS": dict(capability_ids.CAPABILITY_LABELS),
            "MODEL_KIND_LABELS": dict(capability_ids.MODEL_KIND_LABELS),
            "MODEL_KINDS": list(capability_ids.MODEL_KINDS),
        },
    })


# ------------------------------------------------------------------ models_catalog / extension_catalog fixtures


def _cap(intent: str, backend: str = "svc", **fields: Any) -> Capability:
    return Capability(intent=intent, backend=backend, **fields)


def fixture_capabilities() -> list[Capability]:
    """One capability per shipped workflow (first sorted backend), the local inventory of
    REPRESENTATIVE_CONFIG (model files stubbed absent), and rerank/decision rows no local builder makes."""
    registry = c2d_parity.shipped_registry()
    caps = [capability_from_workflow_record(record) for record in registry.records()]
    with c2d_parity.model_files_absent():
        caps.extend(build_local_capabilities(REPRESENTATIVE_CONFIG))
    caps.append(_cap("text.rerank", "rr", model="rerank-model", input_modalities=("text",), output_modalities=("text",)))
    caps.append(_cap("decision.systemone", "dec", model="decider", input_modalities=("text",), output_modalities=("text",)))
    return sorted(caps, key=lambda c: c.sort_key)


def identity(cap: Capability) -> list[Any]:
    return [cap.intent, cap.backend, cap.model, cap.workflow]


OPERATIONS = [
    "chat", "chat.completions", "chat_completions", "CHAT", "completions", "completion", "text", "text.completions",
    "embeddings", "embedding", "text.embeddings", "images", "image", "image.generate", "image.generations",
    "image.edit", "image.edits", "images.edit", "edits", "audio", "audio.generate", "speech", "speech.synthesis", "tts",
    "transcription", "transcriptions", "speech.transcription", "stt", "translation", "translations", "speech.translation",
    "video", "videos", "video.generate", "video.edit", "videos.edit", "rerank", "reranking", "text.rerank",
    "decision", "decisions", "image_edits", "audio_generate", "mesh", "mesh.generate", "unknown-operation", "",
]

INTENTS_FOR_EXAMPLES = sorted({
    "chat.completions", "text.completions", "text.embeddings", "image.generate", "image.edit", "speech.synthesis",
    "speech.transcription", "speech.translation", "audio.generate", "video.generate", "video.edit", "text.rerank",
    "image.analyze", "mesh.generate", "decision.systemone", UNFAMILIAR_INTENT, *shipped_intents(),
})


def capture_models_catalog() -> dict[str, Any]:
    caps = fixture_capabilities()
    by_operation = {
        op: [identity(cap) for cap in models_catalog._capabilities_for_operation(caps, op)] for op in OPERATIONS
    }
    prefixes = {op: list(models_catalog._intent_prefixes_for_operation(op)) for op in OPERATIONS}
    examples = {intent: models_catalog._examples("row-id", [_cap(intent)]) for intent in INTENTS_FOR_EXAMPLES}
    paths = {intent: models_catalog._path_for_capability(_cap(intent)) for intent in INTENTS_FOR_EXAMPLES}
    empty = {"examples_empty": models_catalog._examples("row-id", []), "operation_of_none": models_catalog._capabilities_for_operation(None, "chat")}

    alias_views = {
        "images": {"stable": [{"alias": "image-fast", "resolves_to": "image.generate.flux2-klein-4b"}]},
        "image_edits": {"stable": [{"alias": "image-edit-fast", "resolves_to": "image.edit.flux2-klein-4b"}]},
        "videos": {"stable": [{"alias": "video-default", "resolves_to": "video.generate.video-ltx-2.5-t2v"}]},
        "chat": {"stable": [{"alias": "chat-default", "resolves_to": "default"}]},
    }
    catalog = models_catalog.build_models_catalog(capabilities=caps, alias_views=alias_views)

    def row_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "alias": row["alias"],
            "workflow": row["workflow"],
            "capability": row.get("capability"),
            "icon_key": row.get("icon_key"),
            "operations": row["operations"],
            "capabilities": row["capabilities"],
            "io": row["io"],
            "path": row["use"]["path"],
            "examples": row["examples"],
        }

    return c2d_parity.as_golden({
        "operations": OPERATIONS,
        "intents": INTENTS_FOR_EXAMPLES,
        "capabilities_for_operation": by_operation,
        "intent_prefixes_for_operation": prefixes,
        "examples_by_intent": examples,
        "path_by_intent": paths,
        "empty_inputs": empty,
        "fixture_capability_identities": [identity(cap) for cap in caps],
        "catalog_counts": catalog["counts"],
        # Row order is the catalog's own (available first, then id); that order is the contract.
        "catalog_aliases": [row_view(row) for row in catalog["aliases"]],
        "catalog_models": [row_view(row) for row in catalog["models"]],
    })


def capture_extensions() -> dict[str, Any]:
    registry = c2d_parity.shipped_registry()
    return c2d_parity.as_golden({
        "audiences": list(VALID_AUDIENCES),
        "summaries": {audience: extension_catalog.summarize_extensions(registry, audience) for audience in VALID_AUDIENCES},
    })




# ------------------------------------------------------------------ model_metadata


def _load_manifests() -> dict[str, dict[str, Any]]:
    import yaml

    out: dict[str, dict[str, Any]] = {}
    for path in sorted(c2d_parity.MANIFESTS.glob("*.yml")):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        out[str(manifest["id"])] = manifest
    return out


def _metadata_view(metadata: Any) -> Any:
    if metadata is None:
        return None
    return {"fields": metadata.to_fields(), "record": dataclasses.asdict(metadata)}


METADATA_OPERATIONS = [
    "chat", "embeddings", "images", "image_edits", "videos", "speech", "transcriptions", "audio_generate", "rerank",
    "decisions", "mesh", "unknown-operation", "",
]
METADATA_SERVICE_CAPABILITY_CASES: dict[str, Any] = {
    "chat": ["chat"], "completions": ["completions"], "embeddings": ["embeddings"], "rerank": ["rerank"],
    "decisions": ["decisions"], "vision+chat": ["vision", "chat"], "vision+completions": ["completions", "vision"],
    "vision alone": ["vision"], "vision+embeddings": ["vision", "embeddings"], "unknown": ["frobnicate"],
    "openai marker": ["openai"], "empty": [], "none": None, "uppercase": ["CHAT", " Vision "],
    "first match wins (rerank before chat in the list)": ["rerank", "chat"],
    "table order wins over list order": ["decisions", "chat"], "non-string members": [1, None],
}
METADATA_OPERATIONS_OF_CASES: dict[str, Any] = {
    "no inputs": {"input_modalities": None, "among": None},
    "text": {"input_modalities": ["text"], "among": None},
    "text+image": {"input_modalities": ["text", "image"], "among": None},
    "image+text (order)": {"input_modalities": ["image", "text"], "among": None},
    "audio": {"input_modalities": ["audio"], "among": None},
    "video (matches none)": {"input_modalities": ["video"], "among": None},
    "empty list": {"input_modalities": [], "among": None},
    "string not list": {"input_modalities": "text", "among": None},
    "among chat+images": {"input_modalities": None, "among": ["chat", "images"]},
    "among with unknown": {"input_modalities": ["text"], "among": ["images", "nope", "image_edits"]},
}


def capture_model_metadata() -> dict[str, Any]:
    from gateway import model_metadata as mm
    from gateway import stable_aliases

    operations = sorted({*METADATA_OPERATIONS, *stable_aliases.OPERATIONS})
    types = [*mm.MODEL_TYPES, "unknown-type"]
    manifests = _load_manifests()
    return c2d_parity.as_golden({
        "operations": operations,
        "from_operation": {op: _metadata_view(mm.from_operation(op)) for op in operations},
        "types": types,
        # operations_of keeps the table's order (the first operation of a type is its primary), so lists are not sorted.
        "operations_of": {
            f"{type_}|{case}": list(mm.operations_of(type_, spec["input_modalities"], spec["among"]))
            for type_ in types for case, spec in METADATA_OPERATIONS_OF_CASES.items()
        },
        "operations_of_non_string_type": list(mm.operations_of(None)),
        "service_capability_cases": sorted(METADATA_SERVICE_CAPABILITY_CASES),
        "from_service_capabilities": {
            case: _metadata_view(mm.from_service_capabilities(words))
            for case, words in METADATA_SERVICE_CAPABILITY_CASES.items()
        },
        "from_origin": {
            str(origin): _metadata_view(mm.from_origin(origin))
            for origin in (mm.ORIGIN_LLM_CATALOG, mm.ORIGIN_DECISION, None, "other")
        },
        "workflow_ids": sorted(manifests),
        "from_contract": {wid: _metadata_view(mm.from_contract(manifest)) for wid, manifest in sorted(manifests.items())},
        "from_contract_non_mapping": _metadata_view(mm.from_contract(None)),
    })


# ------------------------------------------------------------------ workflow_openapi / workflows_catalog

FULL_JSON_LIMIT_BYTES = 20_000
BASE_URL = "https://gateway.example.test"
# Stable-alias discovery data in the shape ``stable_aliases.describe_aliases`` returns.
ALIASES: dict[str, Any] = {
    "images": {"stable": [
        {"alias": "image-fast", "resolves_to": "flux2-klein-4b"},
        {"alias": "image-quality", "resolves_to": "krea2-turbo"},
    ]},
    "image_edits": {"stable": [{"alias": "image-edit-fast", "resolves_to": "flux2-klein-4b-edit"}]},
    "videos": {"stable": [
        {"alias": "video-default", "resolves_to": "video-ltx-2.5-t2v"},
        {"alias": "video-long", "resolves_to": "video-ltx-2.5-long"},
    ]},
    "audio_generate": {"stable": [{"alias": "sfx-default", "resolves_to": "tangoflux"}]},
}
VIDEO_LIMITS: dict[str, dict[str, Any]] = {"video-ltx-2.5-long": {"max_total_seconds": 60, "segment_seconds": 10}}


def _sized(value: Any) -> dict[str, Any]:
    """The golden form of one item: its canonical SHA-256 and size, and the full JSON when under 20 KB."""
    compact = c2d_parity.canonical_hash(value)
    size = len(c2d_parity.canonical(value).encode("utf-8"))
    item: dict[str, Any] = {"sha256": compact, "bytes": size}
    if size < FULL_JSON_LIMIT_BYTES:
        item["full"] = c2d_parity.jsonable(value)
    return item


def _base_openapi() -> dict[str, Any]:
    from gateway import workflow_openapi as wo

    def post(schema: dict[str, Any] | None = None) -> dict[str, Any]:
        op: dict[str, Any] = {"responses": {"200": {"description": "ok"}}}
        if schema is not None:
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
        return {"post": op}

    body = {"type": "object", "properties": {"model": {"type": "string"}, "prompt": {"type": "string"}}}
    paths: dict[str, Any] = {}
    for path in ("/v1/images/generations", "/openai/v1/images/generations", "/v1/videos", "/openai/v1/videos",
                 "/v1/audio/generate", "/openai/v1/audio/generate"):
        paths[path] = post(dict(body))
    paths["/v1/images/edits"] = post()
    paths["/openai/v1/images/edits"] = post()
    paths[wo.JOBS_SUBMIT] = post()
    paths["/gateway/v1/media/jobs/image-generations"] = post()
    paths[wo.JOB_POLL] = {"get": {"responses": {}}}
    paths[wo.JOB_CONTENT] = {"get": {"responses": {}}}
    paths[wo.RUN_ROUTE] = post()
    paths["/v1/videos/{video_id}/remix"] = post()
    paths["/openai/v1/videos/{video_id}/remix"] = post()
    return {"openapi": "3.1.0", "info": {"title": "FastAPI", "version": "0"}, "paths": paths}


def capture_openapi() -> dict[str, Any]:
    import copy

    from gateway import workflow_openapi as wo

    registry = c2d_parity.shipped_registry()
    records = registry.records()
    spec = wo.augment_openapi(_base_openapi(), records, copy.deepcopy(ALIASES), copy.deepcopy(VIDEO_LIMITS))
    schemas = spec["components"]["schemas"]
    extension_routes = ["/v1/images/generations", "/openai/v1/images/generations", "/v1/videos", "/openai/v1/videos",
                        "/v1/audio/generate", "/openai/v1/audio/generate"]
    catalog = {entry["id"]: entry for entry in spec[wo.ROOT_EXTENSION]}
    per_workflow: dict[str, Any] = {}
    for record in sorted(records, key=lambda r: r.id):
        names = [wo._schema_name(record)]
        if record.intent.startswith("video."):
            names.append(wo._schema_name(record, wo.EXTENSION_SCHEMA_PREFIX))
        if record.intent == "video.edit":
            names.append(wo._schema_name(record, wo.FORM_PREFIX))
        ref = f"#/components/schemas/{wo._schema_name(record, wo.EXTENSION_SCHEMA_PREFIX if record.intent.startswith('video.') else wo.SCHEMA_PREFIX)}"
        carried_by = []
        for route in extension_routes:
            any_of = spec["paths"][route]["post"].get("requestBody", {}).get("content", {}).get("application/json", {}) \
                .get("schema", {}).get("properties", {}).get("extensions", {}).get("properties", {}) \
                .get(wo.WORKFLOW_EXTENSION_NAMESPACE, {}).get("anyOf", [])
            if {"$ref": ref} in any_of:
                carried_by.append(route)
        run_path = f"/gateway/v1/workflows/{record.id}/run"
        fragment = {
            "components": {name: schemas.get(name) for name in names},
            "catalog_entry": catalog.get(record.id),
            "extension_ref": ref,
            "extension_carried_by_routes": carried_by,
            "run_path": spec["paths"].get(run_path),
        }
        per_workflow[record.id] = _sized(fragment)
    shared = {
        "info": spec["info"],
        "shared_components": {k: v for k, v in sorted(schemas.items()) if not k.startswith(("WorkflowInputs_", "WorkflowExtensionInputs_", "WorkflowForm_"))},
        "workflow_ids_in_catalog_order": [entry["id"] for entry in spec[wo.ROOT_EXTENSION]],
    }
    return c2d_parity.as_golden({
        "inputs": {"aliases": ALIASES, "video_limits": VIDEO_LIMITS, "policy": "fixed: default/stable/batch/media/no tags"},
        "workflows": per_workflow,
        "shared": _sized(shared),
    })


def capture_workflows_catalog() -> dict[str, Any]:
    import copy

    from gateway.workflows_catalog import build_workflows_catalog

    registry = c2d_parity.shipped_registry()
    records = registry.records()
    capabilities = [capability_from_workflow_record(record) for record in records]
    catalog = build_workflows_catalog(
        records=records, capabilities=capabilities, aliases=copy.deepcopy(ALIASES), base_url=BASE_URL,
    )
    rows = {row["id"]: row for row in catalog["workflows"]}
    per_workflow = {
        workflow_id: {
            "examples": _sized({"examples": row["examples"], "example_request": row["example_request"]}),
            "row": _sized(row),
        }
        for workflow_id, row in sorted(rows.items())
    }
    envelope = {key: value for key, value in catalog.items() if key != "workflows"}
    return c2d_parity.as_golden({
        "inputs": {"base_url": BASE_URL, "aliases": ALIASES, "policy": "fixed: default/stable/batch/media/no tags",
                   "capabilities": "capability_from_workflow_record(record) per shipped record, first backend"},
        "workflows": per_workflow,
        "workflow_ids_in_catalog_order": [row["id"] for row in catalog["workflows"]],
        "envelope": envelope,
    })

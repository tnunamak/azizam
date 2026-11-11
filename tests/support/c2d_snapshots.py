"""Capture functions for the code-to-data parity goldens (tests/fixtures/c2d_parity/).

The parity tests compare these functions' output with the stored goldens; capture.py writes the goldens
from the same functions. Nothing here reads a golden.
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any

from gateway import capability_ids, extension_catalog, model_metadata, models_catalog, stable_aliases
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
        UNFAMILIAR_INTENT, "openai", "  chat  ", "CHAT", "Chat", "",
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


# Operation names the code under test accepts. The first three groups come from production data; the last two are
# fixed lists. PROXY_OPERATIONS are the names proxy.py passes at its call sites (found by reading proxy.py:
# resolve_model_for_operation / resolve_stable_alias / operation= arguments). WIRE_OPERATION_ALIASES are the spellings
# models_catalog._intent_prefixes_for_operation matches by literal, which no production table exposes.
PROXY_OPERATIONS = [
    "chat", "completions", "embeddings", "images", "image_edits", "videos", "speech", "transcriptions", "translations",
    "rerank",
]
WIRE_OPERATION_ALIASES = [
    "chat.completions", "chat_completions", "CHAT", "completion", "text", "text.completions", "embedding",
    "text.embeddings", "image", "image.generate", "image.generations", "image.edit", "image.edits", "images.edit",
    "edits", "audio", "audio.generate", "speech.synthesis", "tts", "transcription", "speech.transcription", "stt",
    "translation", "speech.translation", "video", "video.generate", "video.edit", "videos.edit", "reranking",
    "text.rerank", "decision", "decisions", "audio_generate", "mesh", "mesh.generate", "unknown-operation", "",
]
OPERATIONS = sorted({
    *stable_aliases.OPERATIONS, *stable_aliases._SECTION_FOR_OPERATION, *PROXY_OPERATIONS, *WIRE_OPERATION_ALIASES,
})

# Intents: every key of the production intent tables, one name under each metadata family prefix, the fixed list the
# first golden used, an unfamiliar intent, and every shipped workflow intent.
INTENTS_FOR_EXAMPLES = sorted({
    *model_metadata._CAPABILITY_INTENTS, *(f"{family}.x" for family in model_metadata._CAPABILITY_INTENT_FAMILIES),
    *capability_ids.INTENT_CAPABILITIES,
    "chat.completions", "text.completions", "text.embeddings", "image.generate", "image.edit", "speech.synthesis",
    "speech.transcription", "speech.translation", "audio.generate", "video.generate", "video.edit", "text.rerank",
    "image.analyze", "mesh.generate", "decision.systemone", UNFAMILIAR_INTENT, *shipped_intents(),
})


# Inputs of the second build_models_catalog call: a local service with declared models, a remote-provider service
# with prices, a disabled service, listed /v1/models entries, receipts, measured health and listed metadata.
CATALOG_SERVICE_RECORDS: dict[str, Any] = {"services": {
    "svc-local": {
        "id": "svc-local", "name": "Local models", "enabled": True, "locality": "local",
        "config": {"description": "Models served on this machine", "capabilities": ["chat", "completions"], "models": [
            {"id": "chat-small", "kind": "chat", "description": "Small local chat model", "typical_seconds": 3},
            {"id": "mystery", "kind": "frobnicate"},
        ]},
    },
    "svc-embed": {
        "id": "svc-embed", "enabled": True, "locality": "local",
        "config": {"capabilities": ["embeddings"], "models": [{"id": "embed-mini", "kind": "embeddings", "free": True}]},
    },
    "svc-vision": {
        "id": "svc-vision", "enabled": True,
        "config": {"capabilities": ["chat", "vision"], "models": [{"id": "chat-vision", "kind": "chat"}]},
    },
    "svc-remote": {
        "id": "svc-remote", "name": "Remote provider", "enabled": True,
        "config": {
            "provider_type": "remote", "protocol": "openai-compatible", "provider_id": "examplecloud",
            "price": {"models": {"remote-chat": {"input_per_million": 1.5, "output_per_million": 6.0}}},
            "models": [{"id": "remote-chat", "description": "A hosted chat model"}, {"id": "remote-off", "enabled": False}],
        },
    },
    "svc-off": {"id": "svc-off", "enabled": False, "config": {"models": [{"id": "never-listed", "kind": "chat"}]}},
}}
CATALOG_MODEL_ENTRIES: list[dict[str, Any]] = [
    {"id": "chat-small", "description": "Listed description", "provider": "local-provider"},
    {"id": "remote-chat", "owned_by": "examplecloud"},
    {"id": "chat-small-fast"},
]
CATALOG_RECEIPTS: list[dict[str, Any]] = [
    {"request_id": "req_a", "requested_model": "chat-small-fast", "resolved_model": "chat-small", "status": "succeeded", "latency_ms": 1200},
    {"request_id": "req_b", "requested_model": "chat-small-fast", "resolved_model": "chat-small", "status": "succeeded", "latency_ms": 2800},
    {"request_id": "req_c", "requested_model": "remote-chat", "resolved_model": "remote-chat", "status": "failed", "latency_ms": 500},
]
CATALOG_HEALTH: dict[str, Any] = {
    "svc-local": {"available": True, "reason": "Health endpoint responded", "source": "health"},
    "svc-remote": {"available": False, "reason": "Health endpoint did not respond", "source": "health"},
}
CATALOG_LISTED_METADATA: dict[str, Any] = {
    "chat-small": {"type": "language", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                   "max_tokens": 8192, "tags": ["tool-use", "vision"]},
}


def _row_item(row: dict[str, Any]) -> dict[str, Any]:
    item = _sized(row)
    item["id"] = row["id"]
    return item


def assert_sized_rows(actual: list[dict[str, Any]], golden: list[dict[str, Any]], label: str) -> None:
    """Compare catalog rows: ids and order first, then each row's hash, then (full copy stored) field by field."""
    assert [row["id"] for row in actual] == [row["id"] for row in golden], f"{label}: row ids or their order changed"
    for got, want in zip(actual, golden):
        if got["sha256"] == want["sha256"]:
            continue
        if "full" in got and "full" in want:
            for field in sorted(set(got["full"]) | set(want["full"])):
                assert got["full"].get(field) == want["full"].get(field), f"{label} row {want['id']} field {field}"
        raise AssertionError(f"{label} row {want['id']}: canonical JSON differs (no full copy stored or key order only)")


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
        "videos": {"stable": [{"alias": "video-default", "resolves_to": "video.generate.ltx-2.5-t2v"}]},
        "chat": {"stable": [{"alias": "chat-default", "resolves_to": "default"}]},
    }
    catalog = models_catalog.build_models_catalog(capabilities=caps, alias_views=alias_views)

    with c2d_parity.model_files_absent():
        registered = models_catalog.registered_model_capabilities(CATALOG_SERVICE_RECORDS)
        second_caps = [*registered,
                       _cap("chat.completions", "svc-local", model="chat-small-fast", input_modalities=("text",),
                            output_modalities=("text",), tags=("alias", "resolves_to:chat-small"))]
        second = models_catalog.build_models_catalog(
            capabilities=second_caps,
            alias_views={"chat": {"stable": [{"alias": "chat-small-fast", "resolves_to": "chat-small"}]}},
            model_entries=CATALOG_MODEL_ENTRIES,
            service_records=CATALOG_SERVICE_RECORDS,
            recent_receipts=CATALOG_RECEIPTS,
            health_evidence=CATALOG_HEALTH,
            model_metadata=CATALOG_LISTED_METADATA,
        )

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
        # Full rows, not a projection: every field a caller reads is compared. A row over 20 KB keeps its SHA-256 only.
        "catalog_aliases": [_row_item(row) for row in catalog["aliases"]],
        "catalog_models": [_row_item(row) for row in catalog["models"]],
        "registered_model_capabilities": [c2d_parity.jsonable(cap) for cap in registered],
        "second_catalog": {
            "inputs": "CATALOG_SERVICE_RECORDS, CATALOG_MODEL_ENTRIES, CATALOG_RECEIPTS, CATALOG_HEALTH, CATALOG_LISTED_METADATA",
            "envelope": {key: value for key, value in second.items() if key not in ("aliases", "models")},
            "aliases": [_row_item(row) for row in second["aliases"]],
            "models": [_row_item(row) for row in second["models"]],
        },
        # probe_service_health does network I/O (an injected get_json is not enough: its default and its TLS and
        # connect-failure paths open sockets), so it is not captured here.
    })


def capture_extensions() -> dict[str, Any]:
    registry = c2d_parity.shipped_registry()
    caps = [*fixture_capabilities(), _cap("chat.completions", "svc-alias", model="aliased-model", caveats=("aliases: nick-a, nick-b",)),
            _cap("chat.completions", "svc-alias", model="other-model", caveats=("note: unrelated", "aliases: nick-c"))]
    caps = sorted(caps, key=lambda c: c.sort_key)
    names = model_capability_names(caps)
    return c2d_parity.as_golden({
        "audiences": list(VALID_AUDIENCES),
        "summaries": {audience: extension_catalog.summarize_extensions(registry, audience) for audience in VALID_AUDIENCES},
        "model_capability_names": names,
        # One workflow name, one model, aliases (declared in a caveat) and an unknown name, per audience.
        "model_capabilities": {
            f"{audience}|{label}": _sized(extension_catalog.summarize_model_capabilities(caps, model=name, audience=audience))
            for audience in VALID_AUDIENCES for label, name in names.items()
        },
    })


def model_capability_names(caps: list[Capability]) -> dict[str, str]:
    workflow = next(cap.workflow for cap in caps if cap.workflow)
    model = next(cap.model for cap in caps if cap.model and not cap.workflow)
    return {"workflow": workflow, "model": model, "alias": "nick-a", "second alias": "nick-c", "unknown": "no-such-model-name"}




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


CAPABILITY_MODALITY_CASES: dict[str, tuple[Any, Any]] = {
    "defaults": ((), ()),
    "declared text+image in, text out": (["text", "image"], ["text"]),
    "declared audio in, image out": (["audio"], ["image"]),
    "unknown and non-string values": (["bogus", 3, None], ["bogus"]),
}
FROM_FIELDS_CASES: dict[str, Any] = {
    "none": None, "empty": {}, "string": "language", "list": ["type"], "unknown type": {"type": "bogus"},
    "type only": {"type": "language"},
    "full language": {"type": "language", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                      "max_tokens": 4096, "tags": ["reasoning", "tool-use", "vision"]},
    "modalities out of order, upper case, unknown": {"type": "image", "architecture": {
        "input_modalities": ["IMAGE", "text", "bogus"], "output_modalities": ["image", "image"]}},
    "architecture not a mapping": {"type": "audio", "architecture": ["text"]},
    "max_tokens bool": {"type": "language", "max_tokens": True}, "max_tokens string": {"type": "language", "max_tokens": "8"},
    "max_tokens zero": {"type": "language", "max_tokens": 0},
    "tags unknown, out of order": {"type": "language", "tags": ["vision", "bogus", "file-input", "tool-use"]},
    "tags not a list": {"type": "language", "tags": "vision"},
    "null architecture lists": {"type": "embedding", "architecture": {"input_modalities": None, "output_modalities": None}},
}
# Sources for annotate_entries. Every lookup is a literal table here, so the capture reads nothing else.
ANNOTATE_DECLARED = {
    "declared-lang": {"type": "language", "tags": ["reasoning", "vision"], "max_tokens": 32000},
    "declared-media": {"type": "image", "tags": ["reasoning"], "input_modalities": ["text", "image"], "max_tokens": 5},
    "declared-bad": {"type": "bogus", "max_tokens": True, "tags": "x", "input_modalities": ["bogus"]},
    "declared-image-input": {"input_modalities": ["image", "text"]},
    "alias-declared": {"type": "language", "max_tokens": 1000},
    "loaded-a": {"tags": ["tool-use"], "max_tokens": 2048}, "loaded-b": {"tags": ["tool-use", "reasoning"], "max_tokens": 2048},
}
ANNOTATE_SERVICE_CAPS = {
    "svc-chat": ["chat"], "svc-vision": ["chat", "vision"], "svc-embed": ["embeddings"], "svc-rerank": ["rerank"],
    "svc-decisions": ["decisions"], "svc-none": ["frobnicate"], "declared-lang": ["chat"], "loaded-a": ["chat"],
}
ANNOTATE_ORIGINS = {"origin-llm": "llm-catalog", "origin-decision": "decision", "svc-chat": "llm-catalog", "origin-other": "other"}


def _annotate_entries(manifests: dict[str, dict[str, Any]]) -> list[Any]:
    first_per_intent: dict[str, str] = {}
    for workflow_id, manifest in sorted(manifests.items()):
        first_per_intent.setdefault(str(manifest["intent"]), workflow_id)
    entries: list[Any] = [
        {"id": f"wf-{intent}", "azizam_intent": intent, "azizam_workflow_id": workflow_id}
        for intent, workflow_id in sorted(first_per_intent.items())
    ]
    entries += [{"id": f"cap-{intent}", "azizam_intent": intent} for intent in sorted(model_metadata._CAPABILITY_INTENTS)]
    entries += [
        {"id": "cap-unfamiliar", "azizam_intent": UNFAMILIAR_INTENT},
        {"id": "cap-family", "azizam_intent": "chat.responses-x"},
        {"id": "wf-unknown-manifest", "azizam_intent": "image.generate", "azizam_workflow_id": "no-such-workflow"},
        {"id": "origin-llm"}, {"id": "origin-decision"}, {"id": "origin-other"}, {"id": "current", "azizam_current_loaded": True},
        {"id": "svc-chat"}, {"id": "svc-vision"}, {"id": "svc-embed"}, {"id": "svc-rerank"}, {"id": "svc-decisions"},
        {"id": "svc-none"}, {"id": "untyped"}, {"id": ""}, "not-a-dict",
        {"id": "declared-lang"}, {"id": "declared-media", "azizam_intent": "image.generate"},
        {"id": "declared-bad", "azizam_intent": "text.embeddings"}, {"id": "declared-image-input", "azizam_intent": "chat.completions"},
        {"id": "inherited-alias"}, {"id": "dyn-current"},
        # Aliases: resolved to a listed entry, to a workflow target, to an operation default, to mixed types,
        # to itself, to nothing, and with an operator declaration.
        {"id": "alias-listed", "azizam_resolves_to": {"chat": "cap-chat.completions"}},
        {"id": "alias-two-listed", "azizam_resolves_to": {"images": "cap-image.generate", "image_edits": "cap-image.edit"}},
        {"id": "alias-workflow", "azizam_resolves_to": {"videos": "wf-model-video"}},
        {"id": "alias-operation", "azizam_resolves_to": {"speech": "not-listed"}},
        {"id": "alias-mixed", "azizam_resolves_to": {"chat": "cap-chat.completions", "images": "cap-image.generate"}},
        {"id": "alias-self", "azizam_resolves_to": {"chat": "alias-self"}},
        {"id": "alias-empty", "azizam_resolves_to": {"chat": ""}},
        {"id": "alias-not-mapping", "azizam_resolves_to": "chat"},
        {"id": "alias-declared", "azizam_resolves_to": {"chat": "cap-chat.completions"}},
        {"id": "alias-unresolvable", "azizam_resolves_to": {"mesh": "nowhere"}},
    ]
    return entries


def capture_annotate_entries() -> dict[str, Any]:
    from gateway import model_metadata as mm

    manifests = _load_manifests()
    video_workflow = next((wid for wid, m in sorted(manifests.items()) if str(m["intent"]).startswith("video.")), "")
    sources = mm.MetadataSources(
        declared=ANNOTATE_DECLARED.get,
        service_capabilities=ANNOTATE_SERVICE_CAPS.get,
        manifest=manifests.get,
        workflow_for_model=lambda model_id: manifests.get(video_workflow) if model_id == "wf-model-video" else None,
        origins=ANNOTATE_ORIGINS,
        inherited={"inherited-alias": mm.ModelMetadata("language", ("text",), ("text",), 99, ("reasoning",))},
    )
    entries = _annotate_entries(manifests)

    def run(items: list[Any], **kwargs: Any) -> list[Any]:
        copies = copy.deepcopy(items)
        mm.annotate_entries(copies, sources, **kwargs)
        return copies

    narrowed = [e for e in entries if isinstance(e, dict) and e["id"].startswith(("alias-listed", "alias-two", "svc-chat"))]
    index_input = [*entries, {"id": "cap-chat.completions", "extra": 1}, {"id": "svc-chat", "azizam_current_loaded": True}]
    return {
        "annotated": run(entries),
        # ``catalog`` is the unfiltered pool alias targets resolve against: a narrowed list still resolves them.
        "annotated_narrowed_with_catalog": run(narrowed, catalog=entries),
        "annotated_narrowed_without_catalog": run(narrowed),
        "metadata_index": mm.metadata_index(copy.deepcopy(index_input), sources),
        "base_metadata": {
            str(e["id"]): _metadata_view(mm.base_metadata(e, sources)) for e in entries if isinstance(e, dict)
        },
    }


def capture_metadata_combinators() -> dict[str, Any]:
    from gateway import model_metadata as mm

    chat = mm.from_operation("chat")
    chat_vision = mm.ModelMetadata("language", ("text", "image"), ("text",), 8000, ("vision",))
    chat_reason = mm.ModelMetadata("language", ("text",), ("text",), 8000, ("reasoning", "tool-use"))
    image = mm.from_operation("images")
    parts = {
        "none": [], "only none": [None], "one": [chat], "same type": [chat_vision, chat_reason],
        "different types": [chat, image], "untyped member": [chat, mm.ModelMetadata(), None],
        "differing max_tokens": [chat_vision, mm.ModelMetadata("language", ("text",), ("text",), 1, ())],
    }
    declared_cases = {
        "none": None, "not a mapping": ["type"], "empty": {}, "type": {"type": "image"}, "unknown type": {"type": "x"},
        "tags on language": {"tags": ["reasoning", "bogus"]}, "tags on media": {"type": "audio", "tags": ["reasoning"]},
        "vision tag adds image input": {"tags": ["vision"]}, "image input adds vision tag": {"input_modalities": ["text", "image"]},
        "max_tokens ok": {"max_tokens": 10}, "max_tokens negative": {"max_tokens": -1}, "max_tokens bool": {"max_tokens": True},
        "modalities not a list": {"input_modalities": "text"},
    }
    return {
        "merge": {name: _metadata_view(mm.merge(items)) for name, items in parts.items()},
        "common": {name: _metadata_view(mm.common(items)) for name, items in parts.items()},
        "apply_declared": {
            f"{base}|{case}": _metadata_view(mm.apply_declared(derived, declared))
            for base, derived in (("none", None), ("chat", chat), ("image", image))
            for case, declared in declared_cases.items()
        },
        "without_reasoning": _metadata_view(mm.without_reasoning(chat_reason)),
        "type_of": {
            f"{i}>{o}": mm.type_of(i, o)
            for i, o in ((["text"], ["text"]), (["audio"], ["text"]), (["image"], ["text"]), (["text"], ["image"]),
                         (["text"], ["embedding"]), ([], []), (["text"], ["text", "audio"]), (["video"], ["text"]))
        },
        "asset_media_type": {
            case: mm.asset_media_type(spec) for case, spec in {
                "none": {}, "one": {"accepts": ["image/png"]}, "same kind": {"accepts": ["image/png", "image/jpeg"]},
                "mixed kinds": {"accepts": ["image/png", "audio/wav"]}, "unknown kind": {"accepts": ["model/gltf"]},
                "blank": {"accepts": [" "]},
            }.items()
        },
        "loaded_model_metadata": {
            case: _metadata_view(mm.loaded_model_metadata(ids, mm.MetadataSources(
                declared=ANNOTATE_DECLARED.get, service_capabilities=ANNOTATE_SERVICE_CAPS.get)))
            for case, ids in {"none": [], "undeclared": ["svc-chat"], "one": ["loaded-a"], "two": ["loaded-a", "loaded-b"],
                              "declared lang": ["declared-lang"], "blank": ["", None]}.items()
        },
    }


def capability_intents() -> list[str]:
    """Every key of the intent table, a name under each family prefix, the bare prefixes, and odd intents."""
    from gateway import model_metadata as mm

    return sorted({
        *mm._CAPABILITY_INTENTS, *(f"{family}.other" for family in mm._CAPABILITY_INTENT_FAMILIES),
        *mm._CAPABILITY_INTENT_FAMILIES,
        UNFAMILIAR_INTENT, "", " image.generate ", "Chat.completions", "decision.systemone", "responses.create",
        "chat.completions.stream",
    })


def capture_model_metadata() -> dict[str, Any]:
    from gateway import model_metadata as mm
    from gateway import stable_aliases

    operations = sorted({*METADATA_OPERATIONS, *stable_aliases.OPERATIONS})
    types = [*mm.MODEL_TYPES, "unknown-type"]
    manifests = _load_manifests()
    return c2d_parity.as_golden({
        "operations": operations,
        "capability_intents": capability_intents(),
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
        "from_contract_non_mapping": {
            case: _metadata_view(mm.from_contract(value))
            for case, value in {"none": None, "string": "chat", "list": [{"type": "string"}], "int": 3, "empty mapping": {}}.items()
        },
        # The intent table and the family table themselves (a family answer can equal an exact one, so dropping a row
        # is only visible in the table), then every answer from_capability gives over them.
        "capability_intent_table": {intent: _metadata_view(md) for intent, md in mm._CAPABILITY_INTENTS.items()},
        # The goldens name each family by its old dotted prefix; the table now keys it by the bare family.
        "capability_intent_families": [[f"{family}.", _metadata_view(md)] for family, md in mm._CAPABILITY_INTENT_FAMILIES.items()],
        "from_capability": {
            f"{intent}|{case}": _metadata_view(mm.from_capability(intent, ins, outs))
            for intent in capability_intents()
            for case, (ins, outs) in CAPABILITY_MODALITY_CASES.items()
        },
        "from_fields": {case: _metadata_view(mm.from_fields(fields)) for case, fields in FROM_FIELDS_CASES.items()},
        "from_fields_cases": sorted(FROM_FIELDS_CASES),
        "combinators": capture_metadata_combinators(),
        "annotate": capture_annotate_entries(),
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
    # Every path operation of the augmented spec except each workflow's own run path (held per workflow above):
    # the typed generation routes with their extensions and the video ``x-azizam-async`` extension, media-jobs
    # submit, poll and content, remix bodies, and the generic run-route template.
    own_run_paths = {f"/gateway/v1/workflows/{record.id}/run" for record in records}
    path_operations = {path: _sized(spec["paths"][path]) for path in sorted(spec["paths"]) if path not in own_run_paths}
    return c2d_parity.as_golden({
        "inputs": {"aliases": ALIASES, "video_limits": VIDEO_LIMITS, "policy": "fixed: default/stable/batch/media/no tags"},
        "workflows": per_workflow,
        "shared": _sized(shared),
        "path_operations": path_operations,
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

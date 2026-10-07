"""Policy-aware model catalog for the member Models surface.

The catalog is a view over existing gateway facts: stable aliases,
capabilities, optional OpenAI-shaped model entries, and recent receipts.  It
does not probe backends, read config files, or encode engine-specific rules.
Callers supply already-loaded data and an already-authenticated policy view.
"""

from __future__ import annotations

import asyncio
import copy
import json
import statistics
import inspect
from pathlib import Path
import time
from dataclasses import replace
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, Response

from gateway.capabilities import Capability
from gateway.availability import project_service
from gateway.local_capabilities import build_local_capabilities
from gateway.model_presentation import (CAPABILITY_LABELS, LISTED_METADATA_KEY, RECOMMENDED_ALIASES, capability_presentation, product_capability,
    declared_health_success, engine_label, model_locality, service_health_endpoint, CAPABILITY_ICON_KEYS)
from gateway.model_files import model_file_availability
from gateway.provider_model_catalog import (
    remote_provider_model_capabilities,
    remote_provider_model_metadata,
)
from gateway.service_registry import runtime_backend_from_service

CatalogProvider = Callable[[Request], Mapping[str, Any] | Response | Awaitable[Mapping[str, Any] | Response]]
MODEL_DISPLAY_NAMES: Mapping[str, str] = json.loads(
    Path(__file__).with_name("model_display_names.json").read_text(encoding="utf-8")
)


def create_models_catalog_router(
    catalog_provider: CatalogProvider,
    *,
    prefix: str = "/me/api",
) -> APIRouter:
    """Create the member-facing models catalog route.

    ``catalog_provider`` is called with the request and must return a catalog
    dict.  Keeping the router thin lets proxy.py own auth/session lookup while
    this module stays pure and hermetic in tests.
    """

    router = APIRouter(prefix=prefix, tags=["user-models"], include_in_schema=False)

    @router.get("/models/catalog", response_model=None)
    async def models_catalog(request: Request):
        try:
            payload = catalog_provider(request)
            if inspect.isawaitable(payload):
                payload = await payload
            if isinstance(payload, Response):
                return payload
        except Exception as exc:  # noqa: BLE001
            status_code = int(getattr(exc, "status_code", status.HTTP_500_INTERNAL_SERVER_ERROR))
            code = str(getattr(exc, "code", "models_catalog_unavailable"))
            message = str(
                getattr(
                    exc,
                    "message",
                    "Models catalog is unavailable" if status_code >= 500 else "Models catalog request failed",
                )
            )
            return JSONResponse(
                status_code=status_code,
                content={
                    "error": {
                        "code": code,
                        "message": message,
                    }
                },
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=payload,
            headers={"cache-control": "no-store"},
        )

    return router


def registered_model_capabilities(service_records: Mapping[str, Any]) -> list[Capability]:
    """Project declared service models onto their existing protocol capabilities.

    Generic endpoint capabilities have no model ID. Bind explicit registry model
    rows before policy evaluation so restricted profiles can see their models.
    No upstream discovery, secrets, or engine-specific assumptions are needed.
    """
    out: list[Capability] = []
    for service_id, service in _services_mapping(service_records).items():
        if not service.get("enabled", True):
            continue
        remote_capabilities = remote_provider_model_capabilities(service_id, service)
        if remote_capabilities:
            out.extend(remote_capabilities)
            continue
        config = service.get("config") if isinstance(service.get("config"), Mapping) else service
        models = config.get("models")
        if isinstance(models, Mapping):
            models = [{"id": key, **(dict(value) if isinstance(value, Mapping) else {})}
                      for key, value in models.items()]
        if not isinstance(models, list):
            continue
        capabilities = build_local_capabilities(
            {}, backends={service_id: runtime_backend_from_service(service)},
            enabled_flags={"llm": True, "embeddings": True, "tts": True, "stt": True,
                           "images": False, "videos": False, "audio": False},
        )
        model_records = [model for model in models if isinstance(model, Mapping)]
        for model in model_records:
            if not isinstance(model, Mapping):
                continue
            model_id = model.get("model_group") or model.get("public") or model.get("id")
            if not model_id:
                continue
            availability = model_file_availability(model, model_records, service=service)
            out.extend(replace(capability, model=str(model_id), availability=availability)
                       for capability in capabilities if capability.model is None)
    return out


def build_models_catalog(
    *,
    alias_views: Mapping[str, Any] | None = None,
    capabilities: Iterable[Capability] = (),
    capability_sets: Iterable[Iterable[Capability]] | None = None,
    model_entries: Iterable[Mapping[str, Any]] = (),
    service_records: Mapping[str, Any] | None = None,
    recent_receipts: Iterable[Mapping[str, Any]] = (),
    health_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    workflow_records: Iterable[Mapping[str, Any]] = (),
    model_metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic catalog for the Models UI.

    Capabilities must already reflect the caller's policy.  Rows denied by
    policy are omitted.  Timing is computed only from supplied receipts or
    explicit manifest/config facts; otherwise it is reported as unknown.
    ``model_metadata`` maps a row id to the `/v1/models` contract fields
    (``type``, ``architecture``, ``max_tokens``, ``tags``) so both lists agree.
    Those fields also decide the row's ``capability``, ``icon_key`` and ``io``.
    """

    capability_groups = [list(group) for group in capability_sets] if capability_sets is not None else [list(capabilities)]

    model_entry_by_id = {
        str(entry.get("id")): dict(entry)
        for entry in model_entries
        if isinstance(entry, Mapping) and entry.get("id")
    }
    service_records = service_records or {}
    health_evidence = health_evidence or {}
    workflow_metadata = _workflow_metadata(workflow_records)
    service_model_metadata = _service_model_metadata(service_records)
    receipt_stats = _receipt_stats(recent_receipts)
    listed_metadata = model_metadata or {}

    alias_rows: list[dict[str, Any]] = []
    model_rows: list[dict[str, Any]] = []
    allowed_count = 0
    for group in capability_groups:
        aliases, models, count = _rows_for_capabilities(
            group,
            alias_views=alias_views or {},
            model_entries=model_entry_by_id,
            service_records=service_records,
            receipt_stats=receipt_stats,
            health_evidence=health_evidence,
            workflow_metadata=workflow_metadata,
            service_model_metadata=service_model_metadata,
            listed_metadata=listed_metadata,
        )
        alias_rows.extend(aliases)
        model_rows.extend(models)
        allowed_count += count

    aliases = _dedupe_alias_targets(_merge_rows(alias_rows))
    model_rows = _merge_rows(model_rows)
    for row in (*aliases, *model_rows):
        listed = listed_metadata.get(row["id"])
        if listed:
            row.update(copy.deepcopy(dict(listed)))
            architecture = listed.get("architecture") or {}
            row["io"] = {"input": list(architecture.get("input_modalities") or ()),
                         "output": list(architecture.get("output_modalities") or ())}

    return {
        "schema_version": 1,
        "object": "models.catalog",
        "aliases": aliases,
        "models": model_rows,
        "counts": {
            "aliases": len(aliases),
            "models": len(model_rows),
            "capabilities": allowed_count,
        },
    }


def _rows_for_capabilities(
    capabilities: Iterable[Capability],
    *,
    alias_views: Mapping[str, Any],
    model_entries: Mapping[str, Mapping[str, Any]],
    service_records: Mapping[str, Any],
    receipt_stats: Mapping[str, dict[str, Any]],
    health_evidence: Mapping[str, Mapping[str, Any]],
    workflow_metadata: Mapping[str, Mapping[str, Any]],
    service_model_metadata: Mapping[str, Mapping[str, Any]],
    listed_metadata: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    allowed_caps = [cap for cap in capabilities if cap.policy_available is not False]
    denied_targets: set[str] = set()
    for cap in capabilities:
        if cap.policy_available is not False:
            continue
        denied_targets.update(str(target) for target in (cap.model, cap.workflow) if target)
        if cap.workflow:
            denied_targets.update(_public_model_ids_for_workflow(model_entries, str(cap.workflow)))
    caps_by_model: dict[str, list[Capability]] = defaultdict(list)
    for cap in allowed_caps:
        if cap.model:
            caps_by_model[str(cap.model)].append(cap)
        if cap.workflow:
            workflow_key = str(cap.workflow)
            public_ids = _public_model_ids_for_workflow(model_entries, workflow_key)
            for public_id in public_ids or [workflow_key]:
                caps_by_model[str(public_id)].append(cap)

    aliases = _alias_rows(
        alias_views,
        caps_by_model=caps_by_model,
        denied_targets=denied_targets,
        model_entries=model_entries,
        service_records=service_records,
        receipt_stats=receipt_stats,
        health_evidence=health_evidence,
        workflow_metadata=workflow_metadata,
        service_model_metadata=service_model_metadata,
        listed_metadata=listed_metadata,
    )
    concrete_by_model = {
        model: [cap for cap in caps if "alias" not in cap.tags]
        for model, caps in caps_by_model.items()
    }
    # A caller alias covers only its serving backend and declared operation.
    # Retain a concrete row when it exposes any additional capability.
    covered = set()
    visible_alias_ids = {row["id"] for row in aliases}
    for operation, view in alias_views.items():
        if not isinstance(view, Mapping):
            continue
        for raw in view.get("stable") or ():
            if not isinstance(raw, Mapping) or raw.get("alias") not in visible_alias_ids:
                continue
            target = str(raw.get("resolves_to") or "")
            caps = (_capabilities_for_operation([*caps_by_model.get(target, ()), *caps_by_model.get(f"role:{target}", ())], str(operation))
                    or _capabilities_for_operation(caps_by_model.get(str(raw["alias"]), ()), str(operation)))
            backend = str(raw.get("service") or _first_backend(caps) or "")
            for cap in caps:
                if cap.backend == backend:
                    covered.add((cap.intent, backend, target))
                    if cap.model == f"role:{target}":
                        covered.add((cap.intent, backend, cap.model))
    # A registered public role name can cover its internal spelling without
    # an alias. Scope the proof to this policy group, intent, and backend.
    for model, caps in concrete_by_model.items():
        if model.startswith("role:"):
            public_caps = concrete_by_model.get(model.removeprefix("role:"), ())
            for cap in caps:
                if any(public.intent == cap.intent and public.backend == cap.backend
                       for public in public_caps):
                    covered.add((cap.intent, cap.backend, model))
    visible_models = {}
    for model, caps in concrete_by_model.items():
        if caps and not all(any((cap.intent, cap.backend, target) in covered
                                for target in (model, cap.model, cap.workflow) if target)
                            for cap in caps):
            visible_models[model] = caps
    models = _model_rows(
        model_entries,
        caps_by_model=visible_models,
        service_records=service_records,
        receipt_stats=receipt_stats,
        health_evidence=health_evidence,
        workflow_metadata=workflow_metadata,
        service_model_metadata=service_model_metadata,
        listed_metadata=listed_metadata,
    )
    return aliases, models, len(allowed_caps)


def _dedupe_alias_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Synonymous media names share a card; chat profiles remain choices."""
    merged = {}
    for row in rows:
        key = (row["id"],) if row.get("capability_title") == "Chat" else (
            row.get("_serving_backend"), tuple(sorted(row["resolves_to"].items())),
            tuple(cap["intent"] for cap in row["capabilities"]))
        if key not in merged:
            merged[key] = row
        else:
            current = merged[key]
            names = list(dict.fromkeys([*current["use_names"], *row["use_names"]]))
            if row["recommended"] and not current["recommended"]:
                merged[key] = row
                current = row
            current["use_names"] = names
    for row in merged.values():
        row.pop("_serving_backend", None)
    return list(merged.values())


def _merge_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("object") or ""), str(row.get("id") or ""))
        if key not in merged:
            merged[key] = dict(row)
            merged[key]["operations"] = list(row.get("operations") or [])
            merged[key]["resolves_to"] = dict(row.get("resolves_to") or {})
            continue
        current = merged[key]
        current["operations"] = sorted(set(current.get("operations") or []) | set(row.get("operations") or []))
        current.setdefault("resolves_to", {}).update(row.get("resolves_to") or {})
        current["capabilities"] = _merge_capability_summaries(current.get("capabilities") or [], row.get("capabilities") or [])
        current["io"] = _merge_io_summary(current.get("io") or {}, row.get("io") or {})
        if current.get("availability", {}).get("available") is not True and row.get("availability", {}).get("available") is True:
            current["availability"] = row["availability"]
    out = list(merged.values())
    for row in out:
        row["operations"] = sorted(set(row.get("operations") or []))
    return sorted(out, key=lambda r: (not r["availability"]["available"], r["id"]))


def _merge_io_summary(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        "input": sorted(set(left.get("input") or []) | set(right.get("input") or [])),
        "output": sorted(set(left.get("output") or []) | set(right.get("output") or [])),
    }


def _merge_capability_summaries(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_intent = {str(item.get("intent")): dict(item) for item in left if isinstance(item, Mapping) and item.get("intent")}
    for item in right:
        if isinstance(item, Mapping) and item.get("intent") and str(item["intent"]) not in by_intent:
            by_intent[str(item["intent"])] = dict(item)
    return [by_intent[key] for key in sorted(by_intent)]


def _public_model_ids_for_workflow(
    model_entries: Mapping[str, Mapping[str, Any]],
    workflow_id: str,
) -> list[str]:
    return [
        model_id
        for model_id, entry in model_entries.items()
        if str(entry.get("azizam_workflow_id") or "") == workflow_id
    ]


def _alias_rows(
    alias_views: Mapping[str, Any],
    *,
    caps_by_model: Mapping[str, Sequence[Capability]],
    denied_targets: set[str],
    model_entries: Mapping[str, Mapping[str, Any]],
    service_records: Mapping[str, Any],
    receipt_stats: Mapping[str, dict[str, Any]],
    health_evidence: Mapping[str, Mapping[str, Any]],
    workflow_metadata: Mapping[str, Mapping[str, Any]],
    service_model_metadata: Mapping[str, Mapping[str, Any]],
    listed_metadata: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_alias: dict[str, dict[str, Any]] = {}
    for operation, view in alias_views.items():
        if not isinstance(view, Mapping):
            continue
        for raw in view.get("stable") or ():
            if not isinstance(raw, Mapping) or not raw.get("alias"):
                continue
            alias = str(raw["alias"])
            target = str(raw.get("resolves_to") or "")
            target_caps = _capabilities_for_operation([*caps_by_model.get(target, ()), *caps_by_model.get(f"role:{target}", ())], str(operation)) if target else []
            alias_caps = _capabilities_for_operation(caps_by_model.get(alias, ()), str(operation))
            if target and not target_caps and target in denied_targets:
                continue
            if raw.get("service"):
                target_caps = [cap for cap in target_caps if cap.backend == str(raw["service"])]
                alias_caps = [cap for cap in alias_caps if cap.backend == str(raw["service"])]
            concrete_caps = [cap for cap in target_caps if "alias" not in cap.tags]
            caps = concrete_caps or target_caps or alias_caps
            if not caps:
                continue
            operation_row = _base_row(
                row_id=alias,
                display_name=alias,
                is_alias=True,
                capabilities=caps,
                service_id=str(raw.get("service") or _first_backend(caps) or ""),
                target=target or None,
                model_entry=model_entries.get(target) or model_entries.get(alias),
                service_records=service_records,
                receipt_stats=receipt_stats,
                health_evidence=health_evidence,
                workflow_metadata=workflow_metadata,
                service_model_metadata=service_model_metadata,
                listed=listed_metadata.get(alias),
            )
            operation_row["_serving_backend"] = str(raw.get("service") or _first_backend(caps) or "")
            presentation = capability_presentation(caps[0], {
                "capability": operation_row["capabilities"][0]["capability"], **raw,
                LISTED_METADATA_KEY: listed_metadata.get(alias)})
            operation_row.update({key: presentation[key] for key in
                                 ("capability", "title", "capability_title", "icon_key", "secondary_profile")})
            operation_row["recommended"] = alias == RECOMMENDED_ALIASES.get(presentation["capability"])
            operation_row["display_name"] = presentation["title"]
            operation_row["name"] = presentation["title"]
            if raw.get("description"):
                operation_row["description"] = presentation["summary"]
                if str(raw["description"]) != presentation["summary"]:
                    operation_row["technical_description"] = str(raw["description"])
            operation_row["use_names"] = list(dict.fromkeys([alias, *operation_row["use_names"]]))
            row = by_alias.setdefault(alias, operation_row)
            if row is not operation_row:
                row["capabilities"] = _merge_capability_summaries(row.get("capabilities") or [], operation_row.get("capabilities") or [])
                row["io"] = _merge_io_summary(row.get("io") or {}, operation_row.get("io") or {})
                if row.get("availability", {}).get("available") is not True and operation_row.get("availability", {}).get("available") is True:
                    row["availability"] = operation_row["availability"]
            row["operations"].append(str(operation))
            if target:
                row["resolves_to"][str(operation)] = target
            if raw.get("error"):
                row["availability"] = {
                    "available": False,
                    "status": "unavailable",
                    "reason": str(raw["error"]),
                    "measured": False,
                }
    rows = list(by_alias.values())
    for row in rows:
        row["operations"] = sorted(set(row["operations"]))
    return sorted(rows, key=lambda r: (not r["availability"]["available"], r["id"]))


def _capabilities_for_operation(capabilities: Sequence[Capability] | None, operation: str) -> list[Capability]:
    caps = list(capabilities or ())
    if not caps:
        return []
    prefixes = _intent_prefixes_for_operation(operation)
    if not prefixes:
        return caps
    return [cap for cap in caps if any(cap.intent.startswith(prefix) for prefix in prefixes)]


def _intent_prefixes_for_operation(operation: str) -> tuple[str, ...]:
    op = operation.strip().lower().replace("_", ".")
    if op in {"chat", "chat.completions"}:
        return ("chat.",)
    if op in {"completions", "completion", "text", "text.completions"}:
        return ("text.completions",)
    if op in {"embeddings", "embedding", "text.embeddings"}:
        return ("text.embeddings",)
    if op in {"images", "image", "image.generate", "image.generations"}:
        return ("image.generate",)
    if op in {"image.edit", "image.edits", "images.edit", "edits"}:
        return ("image.edit",)
    if op in {"audio", "audio.generate"}:
        return ("audio.generate",)
    if op in {"speech", "speech.synthesis", "tts"}:
        return ("speech.synthesis",)
    if op in {"transcription", "transcriptions", "speech.transcription", "stt"}:
        return ("speech.transcription",)
    if op in {"translation", "translations", "speech.translation"}:
        return ("speech.translation",)
    if op in {"video", "videos"}:
        # The /v1/videos operation serves both generation and editing (remix, extend).
        return ("video.generate", "video.edit")
    if op == "video.generate":
        return ("video.generate",)
    if op in {"video.edit", "videos.edit"}:
        return ("video.edit",)
    if op in {"rerank", "reranking", "text.rerank"}:
        return ("text.rerank",)
    if op in {"decision", "decisions"}:
        return ("decision.",)
    return ()


def _model_rows(
    model_entries: Mapping[str, Mapping[str, Any]],
    *,
    caps_by_model: Mapping[str, Sequence[Capability]],
    service_records: Mapping[str, Any],
    receipt_stats: Mapping[str, dict[str, Any]],
    health_evidence: Mapping[str, Mapping[str, Any]],
    workflow_metadata: Mapping[str, Mapping[str, Any]],
    service_model_metadata: Mapping[str, Mapping[str, Any]],
    listed_metadata: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    ids = set(caps_by_model)
    rows = [
        _base_row(
            row_id=model_id,
            display_name=model_id,
            is_alias=False,
            capabilities=list(caps_by_model.get(model_id, ())),
            service_id=_first_backend(caps_by_model.get(model_id, ())),
            target=None,
            model_entry=model_entries.get(model_id),
            service_records=service_records,
            receipt_stats=receipt_stats,
            health_evidence=health_evidence,
            workflow_metadata=workflow_metadata,
            service_model_metadata=service_model_metadata,
            listed=listed_metadata.get(model_id),
        )
        for model_id in ids
    ]
    return sorted(rows, key=lambda r: (not r["availability"]["available"], r["id"]))


def _base_row(
    *,
    row_id: str,
    display_name: str,
    is_alias: bool,
    capabilities: Sequence[Capability],
    service_id: str | None,
    target: str | None,
    model_entry: Mapping[str, Any] | None,
    service_records: Mapping[str, Any],
    receipt_stats: Mapping[str, dict[str, Any]],
    health_evidence: Mapping[str, Mapping[str, Any]],
    workflow_metadata: Mapping[str, Mapping[str, Any]],
    service_model_metadata: Mapping[str, Mapping[str, Any]],
    listed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    service = {"id": service_id, **_service_record(service_records, service_id)}
    workflow_id = str((model_entry or {}).get("azizam_workflow_id") or next(
        (cap.workflow for cap in capabilities if cap.workflow), ""
    ))
    manifest = (workflow_metadata.get(row_id) or (workflow_metadata.get(target) if target else None)
                or workflow_metadata.get(workflow_id) or {})
    model_mapping = (
        service_model_metadata.get(row_id)
        or (service_model_metadata.get(target) if target else None)
        or {}
    )
    timing = (
        _timing_from_receipt_stats(receipt_stats.get(row_id))
        or (_timing_from_receipt_stats(receipt_stats.get(target)) if target else None)
        or _timing_from_receipt_stats(receipt_stats.get(workflow_id))
        or _configured_timing(manifest, service)
        or _configured_timing(model_mapping, service)
        or _configured_timing(model_entry, service)
    )
    recent_receipts = (
        _recent_from_receipt_stats(receipt_stats.get(row_id))
        or (_recent_from_receipt_stats(receipt_stats.get(target)) if target else [])
        or _recent_from_receipt_stats(receipt_stats.get(workflow_id))
    )
    locality = _locality(service, model_mapping or model_entry)
    presentation_metadata = {**(model_entry or {}), **model_mapping, **manifest, "locality": locality,
                             LISTED_METADATA_KEY: listed}
    has_declared_title = any(
        isinstance(presentation_metadata.get(field), str)
        and presentation_metadata[field].strip()
        for field in ("title", "display_name", "name")
    )
    catalog_title = MODEL_DISPLAY_NAMES.get(row_id) or (
        MODEL_DISPLAY_NAMES.get(str(target)) if target else None
    )
    if not has_declared_title and catalog_title:
        presentation_metadata["title"] = catalog_title
    raw_description = _description(presentation_metadata)
    if is_alias:
        presentation_metadata["alias"] = row_id
        if capabilities and product_capability(capabilities[0], presentation_metadata) == "chat":
            for field in ("title", "display_name", "name"):
                presentation_metadata.pop(field, None)
    presentation = capability_presentation(capabilities[0], presentation_metadata) if capabilities else {}
    cost = _cost(service, model_mapping or model_entry)
    summary = {}
    if timing is not None:
        summary["typical_seconds"] = timing["typical_seconds"]
    if locality == "local":
        summary["cost_label"] = "Local · no provider charge"
    elif cost["kind"] == "free":
        summary["cost_label"] = "No provider charge"
    elif cost["price"] is not None:
        summary["price"] = cost["price"]
    return {
        "id": row_id,
        "object": "model_catalog.alias" if is_alias else "model_catalog.model",
        "display_name": presentation.get("title", display_name),
        "recommended": row_id == RECOMMENDED_ALIASES.get(presentation.get("capability")),
        **{key: presentation[key] for key in ("capability", "title", "capability_title", "icon_key", "recommended_model", "secondary_profile") if key in presentation},
        "use_names": list(dict.fromkeys([row_id, *([target] if target and not target.startswith("role:") else [])])),
        "alias": is_alias,
        "operations": [],
        "resolves_to": {},
        "description": presentation.get("summary"),
        **({"technical_description": presentation_metadata.get("technical_description") or raw_description}
           if presentation_metadata.get("technical_description")
           or raw_description and raw_description != presentation.get("summary") else {}),
        "name": presentation.get("title", display_name),
        "capabilities": _capability_summaries(capabilities, manifest),
        "provider": _provider(service, model_mapping or model_entry, service_id),
        "engine_label": engine_label(service, service_id),
        "locality": locality,
        "cost": cost,
        "summary": summary,
        "timing": timing or {"typical_seconds": None, "source": None},
        "availability": _availability(
            capabilities,
            target=target,
            service_records=service_records,
            service_id=service_id,
            health=_health_for_row(health_evidence, service_id=service_id, capabilities=capabilities),
        ),
        "io": _io_summary(capabilities),
        "examples": _examples(row_id, capabilities),
        "recent_receipts": recent_receipts,
        "use": _use_snippets(row_id, capabilities),
    }


def _capability_summaries(capabilities: Sequence[Capability], metadata: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    out = []
    for cap in sorted(capabilities, key=lambda c: c.sort_key):
        out.append(
            {
                "intent": cap.intent,
                "label": CAPABILITY_LABELS.get(product_capability(cap, metadata), "AI"),
                "capability": product_capability(cap, metadata),
                "icon_key": CAPABILITY_ICON_KEYS.get(product_capability(cap, metadata), "chat"),
                "input_modalities": list(cap.input_modalities),
                "output_modalities": list(cap.output_modalities),
                "streaming": cap.streaming,
                "available": cap.available,
            }
        )
    return out


def _io_summary(capabilities: Sequence[Capability]) -> dict[str, list[str]]:
    inputs: list[str] = []
    outputs: list[str] = []
    for cap in capabilities:
        inputs.extend(cap.input_modalities)
        outputs.extend(cap.output_modalities)
    return {
        "input": sorted(set(inputs)),
        "output": sorted(set(outputs)),
    }


def _availability(
    capabilities: Sequence[Capability],
    *,
    target: str | None,
    service_records: Mapping[str, Any] | None = None,
    service_id: str | None = None,
    health: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if capabilities and any(cap.policy_available is not False for cap in capabilities):
        declared_unavailable = next(
            (
                cap.availability
                for cap in capabilities
                if isinstance(cap.availability, Mapping)
                and cap.availability.get("available") is False
            ),
            None,
        )
        if isinstance(declared_unavailable, Mapping):
            return dict(declared_unavailable)
        projected_service_id = service_id or _first_backend(capabilities) or ""
        projection = project_service(
            projected_service_id,
            service_records=service_records,
            evidence={projected_service_id: health} if health else None,
            fallback_capability=capabilities[0],
        )
        return projection.to_public_dict()
    if capabilities:
        reasons = [
            reason
            for cap in capabilities
            for reason in cap.policy_reasons
            if reason
        ]
        return {
            "available": False,
            "status": "unavailable",
            "reason": reasons[0] if reasons else "Not available to your access plan",
            "measured": False,
        }
    if target:
        return {
            "available": False,
            "status": "unavailable",
            "reason": "Target is not currently visible to your access plan",
            "measured": False,
        }
    return {
        "available": False,
        "status": "unavailable",
        "reason": "No serving capability is configured",
        "measured": False,
    }


def _examples(row_id: str, capabilities: Sequence[Capability]) -> list[dict[str, Any]]:
    if not capabilities:
        return []
    intent = capabilities[0].intent
    if intent == "chat.completions":
        return [{"kind": "chat", "body": {"model": row_id, "messages": [{"role": "user", "content": "Hello"}]}}]
    if intent == "text.completions":
        return [{"kind": "completion", "body": {"model": row_id, "prompt": "Hello"}}]
    if intent == "text.embeddings":
        return [{"kind": "embeddings", "body": {"model": row_id, "input": "Hello"}}]
    if intent == "image.generate":
        return [{"kind": "image", "body": {"model": row_id, "prompt": "A small red cabin"}}]
    if intent == "image.edit":
        return [{"kind": "image_edit", "body": {"model": row_id, "prompt": "Replace the sky"}, "files": ["image"]}]
    if intent == "speech.synthesis":
        return [{"kind": "speech", "body": {"model": row_id, "input": "Hello", "voice": "alloy"}}]
    if intent == "speech.transcription":
        return [{"kind": "transcription", "body": {"model": row_id}, "files": ["file"]}]
    if intent == "speech.translation":
        return [{"kind": "translation", "body": {"model": row_id}, "files": ["file"]}]
    if intent == "audio.generate":
        return [{"kind": "audio", "body": {"model": row_id, "prompt": "Gentle piano loop"}}]
    if intent == "video.generate":
        return [{"kind": "video", "body": {"model": row_id, "prompt": "A kite above a beach"}}]
    if intent == "text.rerank":
        return [{"kind": "rerank", "body": {"model": row_id, "query": "capital of France", "documents": ["Paris is the capital of France.", "Berlin is in Germany."]}}]
    return [{"kind": intent, "body": None}]


def _use_snippets(row_id: str, capabilities: Sequence[Capability]) -> dict[str, Any]:
    path = _path_for_capability(capabilities[0]) if capabilities else None
    example = _examples(row_id, capabilities)[0] if capabilities else {"body": None}
    body = example.get("body") if isinstance(example, Mapping) else None
    is_json_runnable = path is not None and isinstance(body, Mapping) and not example.get("files")
    return {
        "model": row_id,
        "path": path,
        "curl": (
            f"curl $AZIZAM_BASE{path} "
            '-H "Authorization: Bearer $AZIZAM_API_KEY" '
            '-H "Content-Type: application/json" '
            f"-d '{json.dumps(body, separators=(',', ':'))}'"
        ) if is_json_runnable else None,
        "python": {"path": path, "model": row_id} if path is not None else None,
        "javascript": {"path": path, "model": row_id} if path is not None else None,
    }


def _path_for_capability(capability: Capability) -> str | None:
    if capability.intent == "chat.completions":
        return "/v1/chat/completions"
    if capability.intent == "text.completions":
        return "/v1/completions"
    if capability.intent == "text.embeddings":
        return "/v1/embeddings"
    if capability.intent == "image.generate":
        return "/v1/images/generations"
    if capability.intent == "image.edit":
        return "/v1/images/edits"
    if capability.intent == "speech.synthesis":
        return "/v1/audio/speech"
    if capability.intent == "speech.transcription":
        return "/v1/audio/transcriptions"
    if capability.intent == "speech.translation":
        return "/v1/audio/translations"
    if capability.intent == "audio.generate":
        return "/v1/audio/generate"
    if capability.intent == "video.generate":
        return "/v1/videos"
    if capability.intent == "text.rerank":
        return "/v1/rerank"
    return None


def _service_record(records: Mapping[str, Any], service_id: str | None) -> Mapping[str, Any]:
    if not service_id:
        return {}
    direct = records.get(service_id)
    if isinstance(direct, Mapping):
        return direct
    services = records.get("services")
    if isinstance(services, Mapping) and isinstance(services.get(service_id), Mapping):
        return services[service_id]
    return {}


def _first_backend(capabilities: Sequence[Capability] | None) -> str | None:
    for cap in capabilities or ():
        if cap.backend:
            return str(cap.backend)
    return None


def _description(*sources: Mapping[str, Any] | None) -> str | None:
    expanded: list[Mapping[str, Any]] = []
    for source in sources:
        if isinstance(source, Mapping):
            expanded.append(source)
            config = source.get("config")
            if isinstance(config, Mapping):
                expanded.append(config)
    for source in expanded:
        if isinstance(source, Mapping) and source.get("description"):
            return str(source["description"])
    return None


def _provider(service: Mapping[str, Any], model_entry: Mapping[str, Any] | None, service_id: str | None) -> str | None:
    for source in (model_entry or {}, service, service.get("config") if isinstance(service.get("config"), Mapping) else {}):
        if isinstance(source, Mapping):
            for key in ("provider", "provider_id", "owned_by"):
                if source.get(key):
                    return str(source[key])
    return service_id


def _locality(service: Mapping[str, Any], model_entry: Mapping[str, Any] | None) -> str:
    return model_locality(service, model_entry)


def _cost(service: Mapping[str, Any], model_entry: Mapping[str, Any] | None) -> dict[str, Any]:
    for source in (model_entry or {}, service, service.get("config") if isinstance(service.get("config"), Mapping) else {}):
        if not isinstance(source, Mapping):
            continue
        price = source.get("price") or source.get("cost")
        if isinstance(price, Mapping) and isinstance(price.get("models"), Mapping):
            price = None
        if price is not None:
            return {"kind": "paid", "price": price}
        if source.get("free") is True:
            return {"kind": "free", "price": None}
    locality = _locality(service, model_entry)
    if locality == "local":
        return {"kind": "free", "price": None}
    if locality == "remote":
        return {"kind": "paid", "price": None}
    return {"kind": "unknown", "price": None}


def _configured_timing(model_entry: Mapping[str, Any] | None, service: Mapping[str, Any]) -> dict[str, Any] | None:
    for source in (model_entry or {}, service, service.get("config") if isinstance(service.get("config"), Mapping) else {}):
        if isinstance(source, Mapping) and source.get("typical_seconds") is not None:
            return {"typical_seconds": source.get("typical_seconds"), "source": "configured"}
    return None


def _workflow_metadata(records: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        manifest = record.get("manifest") if isinstance(record.get("manifest"), Mapping) else record
        if not isinstance(manifest, Mapping):
            continue
        keys = [
            record.get("id"),
            manifest.get("id"),
            manifest.get("model"),
            manifest.get("model_id"),
        ]
        bindings = manifest.get("bindings")
        if isinstance(bindings, Mapping):
            gateway = bindings.get("gateway")
            if isinstance(gateway, Mapping):
                keys.append(gateway.get("model_id"))
        for key in keys:
            if key:
                out[str(key)] = manifest
    return out


def _service_model_metadata(service_records: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    services = _services_mapping(service_records)
    out: dict[str, Mapping[str, Any]] = {}
    for service_id, service in services.items():
        remote_metadata = remote_provider_model_metadata(service_id, service)
        if remote_metadata:
            out.update(remote_metadata)
            continue
        config = service.get("config") if isinstance(service.get("config"), Mapping) else service
        models = config.get("models") if isinstance(config, Mapping) else None
        if isinstance(models, Mapping):
            models = [{"id": key, **(dict(value) if isinstance(value, Mapping) else {})}
                      for key, value in models.items()]
        if not isinstance(models, list):
            continue
        for record in models:
            if not isinstance(record, Mapping):
                continue
            keys = [
                record.get("model_group"),
                record.get("id"),
                record.get("public"),
                record.get("model"),
                record.get("upstream_model"),
            ]
            enriched = {**record, "service_id": service_id}
            for key in keys:
                if key:
                    out[str(key)] = enriched
    return out


def _health_for_row(
    health_evidence: Mapping[str, Mapping[str, Any]],
    *,
    service_id: str | None,
    capabilities: Sequence[Capability],
) -> Mapping[str, Any] | None:
    candidates = [service_id, *(cap.backend for cap in capabilities)]
    for candidate in candidates:
        if candidate and candidate in health_evidence:
            return health_evidence[candidate]
    return None


def _services_mapping(service_records: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = service_records.get("services") if isinstance(service_records.get("services"), Mapping) else service_records
    out: dict[str, Mapping[str, Any]] = {}
    for service_id, service in (raw or {}).items():
        if isinstance(service, Mapping):
            out[str(service_id)] = service
    return out


class HealthProbeNoResponse(Exception):
    def __init__(self, *, status_code: int | None = None) -> None:
        super().__init__(f"health probe returned HTTP {status_code}" if status_code is not None else "health probe did not respond")
        self.status_code = status_code


async def probe_service_health(
    service_records: Mapping[str, Any],
    *,
    get_json: Callable[[str], Awaitable[Mapping[str, Any] | None]],
    service_ids: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Probe generic service health endpoints declared as data.

    Services without ``health_endpoint`` are reported as unknown. This function
    never infers readiness from enablement alone.
    """

    services = _services_mapping(service_records)
    requested = set(str(service_id) for service_id in service_ids) if service_ids is not None else set(services)

    async def probe_one(service_id: str) -> tuple[str, dict[str, Any]]:
        service = services.get(service_id)
        if not isinstance(service, Mapping):
            return service_id, {
                "available": None,
                "status": "unknown",
                "reason": "Live availability is not measured for this service",
                "source": None,
                "observed_at_s": int(time.time()),
            }
        endpoint = _health_endpoint(service)
        if not endpoint:
            return service_id, {
                "available": None,
                "status": "unknown",
                "reason": "Live availability is not measured for this service",
                "source": None,
                "observed_at_s": int(time.time()),
            }
        missing_route = False
        try:
            payload = await get_json(endpoint)
        except HealthProbeNoResponse as exc:
            missing_route = exc.status_code in {404, 405}
            payload = None
        except Exception:  # noqa: BLE001
            payload = None
        if not isinstance(payload, Mapping):
            if missing_route:
                return service_id, {
                    "available": None,
                    "status": "unknown",
                    "reason": "Health endpoint did not respond",
                    "source": "health",
                    "kind": "no_response",
                    "observed_at_s": int(time.time()),
                }
            return service_id, {
                "available": False,
                "status": "unavailable",
                "reason": "Health endpoint did not respond",
                "source": "health",
                "kind": "no_response",
                "observed_at_s": int(time.time()),
            }
        available = _availability_from_health_payload(payload)
        status = _status_from_health_payload(payload)
        if status == "unknown" and declared_health_success(service, payload):
            available, status = True, "ready"
        if available is False:
            reason = "Health endpoint reported unavailable"
        elif available is True:
            reason = "Health endpoint responded"
        else:
            reason = "Health endpoint responded; readiness is unknown"
        return service_id, {
            "available": available,
            "status": status,
            "reason": reason,
            "source": "health",
            "observed_at_s": int(time.time()),
        }

    semaphore = asyncio.Semaphore(8)

    async def limited_probe(service_id: str) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            return await probe_one(service_id)

    pairs = await asyncio.gather(*(limited_probe(service_id) for service_id in requested))
    return dict(pairs)


def _health_endpoint(service: Mapping[str, Any]) -> str | None:
    return service_health_endpoint(service)


def _availability_from_health_payload(payload: Mapping[str, Any]) -> bool | None:
    for key in ("available", "ready", "ok", "healthy"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    status_value = str(payload.get("status") or payload.get("state") or "").strip().lower()
    if status_value in {"up", "ok", "ready", "healthy", "available"}:
        return True
    if status_value in {"down", "failed", "unhealthy", "unavailable", "busy", "warming", "starting"}:
        return False
    return None


def _status_from_health_payload(payload: Mapping[str, Any]) -> str:
    status_value = str(payload.get("status") or payload.get("state") or "").strip().lower()
    if status_value in {"starting", "warming"}:
        return "starting"
    if status_value in {"busy"}:
        return "busy"
    if _availability_from_health_payload(payload) is True:
        return "ready"
    if _availability_from_health_payload(payload) is False:
        return "unavailable"
    return "unknown"


def auth_contexts_from_user_snapshot(
    snapshot: Mapping[str, Any],
    *,
    default_profile_id: str | None,
    profile_loader: Callable[[str], Any | None],
    context_for_profile: Callable[[Any, str], Any],
) -> list[Any]:
    """Build fail-closed catalog auth contexts from a user portal snapshot."""

    profile_ids: list[str] = []
    saw_enabled_app = False
    saw_default_app = False
    for app_record in snapshot.get("apps") or ():
        if not isinstance(app_record, Mapping):
            continue
        if app_record.get("disabled_at") or app_record.get("self_service_enabled") is False:
            continue
        saw_enabled_app = True
        profile_id = str(app_record.get("effective_profile_id") or app_record.get("profile_id") or "").strip()
        if profile_id == "default-local":
            saw_default_app = True
            profile_ids.append("default-local")
        elif profile_id:
            profile_ids.append(profile_id)
    if not profile_ids and default_profile_id and not saw_enabled_app:
        profile_ids.append(default_profile_id)
    contexts: list[Any] = []
    seen: set[str] = set()
    for profile_id in profile_ids:
        if profile_id in seen:
            continue
        seen.add(profile_id)
        profile = profile_loader(profile_id)
        if profile is None:
            continue
        contexts.append(context_for_profile(profile, profile_id))
    return contexts


def _receipt_stats(receipts: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    values: dict[str, list[float]] = defaultdict(list)
    recent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for receipt in receipts:
        latency_ms = receipt.get("latency_ms")
        has_latency = isinstance(latency_ms, (int, float)) and not isinstance(latency_ms, bool)
        timing_eligible = has_latency and _receipt_succeeded(receipt.get("status"))
        keys = {
            str(key)
            for key in (
                receipt.get("requested_model"),
                receipt.get("resolved_model"),
                receipt.get("requested_workflow"),
                receipt.get("resolved_workflow"),
            )
            if key
        }
        for key in keys:
            if timing_eligible:
                values[key].append(float(latency_ms) / 1000.0)
            request_id = receipt.get("request_id")
            if request_id and len(recent[key]) < 3:
                recent[key].append(
                    {
                        "request_id": str(request_id),
                        "href": f"/r/{request_id}",
                        "status": receipt.get("status"),
                        "latency_ms": latency_ms if has_latency else None,
                    }
                )
    return {
        key: {"latencies": values.get(key, []), "recent": recent.get(key, [])}
        for key in set(values) | set(recent)
    }


def _receipt_succeeded(status_value: Any) -> bool:
    if isinstance(status_value, int):
        return 200 <= status_value < 300
    status_text = str(status_value or "").strip().lower()
    return status_text in {"success", "succeeded", "completed", "complete", "ok", "200", "201", "202", "204"}


def _timing_from_receipt_stats(stats: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not stats:
        return None
    samples = stats.get("latencies")
    if not samples:
        return None
    return {
        "typical_seconds": round(statistics.median(samples), 3),
        "source": "receipts",
        "sample_count": len(samples),
    }


def _recent_from_receipt_stats(stats: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not stats:
        return []
    rows = stats.get("recent")
    if not isinstance(rows, list):
        return []
    return [
        row
        for row in rows
        if isinstance(row, dict)
    ]

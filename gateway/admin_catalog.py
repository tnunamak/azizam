"""Admin dashboard catalog loaders.

This module projects registry data into renderer-friendly catalog records.
Live service state, workflows, models, and user data are layered in by
``proxy.py`` before rendering the dashboard.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterable
from typing import Any, Mapping

import yaml

from gateway import service_registry, model_presentation
from gateway.model_routes import model_records, route_step
from gateway.registry_snapshot import Snapshot
from gateway.access_plans import ALL, CAPABILITIES, MEDIA_METADATA_LEVELS, RECEIPT_DETAIL_LEVELS
from gateway.capability_ids import CAPABILITY_LABELS, INTENT_CAPABILITIES, PROTOCOL_CAPABILITIES

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
_ADMIN_SURFACES_CATALOG_NAME = "admin_surfaces.yml"


def admin_api_surfaces(
    *,
    registry: Mapping[str, Any] | None = None,
    proxy_config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if registry is not None and isinstance(registry.get("protocol_frontends"), Mapping):
        protocol_frontends = registry["protocol_frontends"]
    else:
        protocol_frontends = service_registry.load_seed_catalogs(
            proxy_config or {}
        ).get("protocol_frontends", {})

    surfaces = [
        _api_surface_from_protocol_frontend(frontend)
        for frontend in protocol_frontends.values()
        if isinstance(frontend, Mapping)
    ]
    surfaces.extend(load_admin_surfaces_catalog(_catalog_path(proxy_config, _ADMIN_SURFACES_CATALOG_NAME)))
    return surfaces


def _catalog_path(proxy_config: Mapping[str, Any] | None, name: str) -> Path:
    if proxy_config is None:
        return _REGISTRY_DIR / name
    return service_registry.catalog_dir_from_config(proxy_config) / name


def load_admin_surfaces_catalog(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records = raw.get("records") if isinstance(raw, Mapping) else None
    if not isinstance(records, list):
        return []
    return [
        _normalize_admin_surface(record, row_index=index)
        for index, record in enumerate(records)
        if isinstance(record, Mapping)
    ]


def _normalize_admin_surface(record: Mapping[str, Any], *, row_index: int) -> dict[str, Any]:
    for field in ("name", "status"):
        value = record.get(field)
        if not isinstance(value, str) or not value.strip():
            row = record.get("id") or f"records[{row_index}]"
            raise ValueError(f"Admin surface {row!r}: {field} must be a non-empty string")
    return {
        "name": record["name"],
        "route_base": str(record.get("route_base") or ""),
        "status": record["status"],
        "profile_gates": _string_list(record.get("profile_gates")),
        "clients": _string_list(record.get("clients")),
        "description": str(record.get("description") or ""),
    }


def _api_surface_from_protocol_frontend(frontend: Mapping[str, Any]) -> dict[str, Any]:
    protocol = str(frontend.get("protocol") or frontend.get("id") or "")
    return {
        "name": str(frontend.get("name") or protocol or model_presentation.PRESENTATION_WORDS["admin_surface"]["fallback_name"]),
        "route_base": _surface_route_base(frontend.get("routes")),
        "status": "live" if frontend.get("enabled", True) else "disabled",
        "profile_gates": [protocol] if protocol else [],
        "clients": [],
        "description": _surface_description(frontend),
    }


def _surface_route_base(routes: Any) -> str:
    if not isinstance(routes, (list, tuple)):
        return ""
    for route in reversed(routes):
        if isinstance(route, Mapping):
            path = str(route.get("path") or "").strip()
            if path:
                return path
        else:
            path = str(route or "").strip()
            if path:
                return path
    return ""


def _surface_description(frontend: Mapping[str, Any]) -> str:
    intents = [
        str(intent).strip().replace(".", " ").replace("_", " ")
        for intent in frontend.get("intents") or []
        if str(intent).strip()
    ]
    words = model_presentation.PRESENTATION_WORDS["admin_surface"]
    if intents:
        return words["intents_description"].format(intents=", ".join(intents))
    protocol = str(frontend.get("protocol") or words["default_protocol"]).strip()
    return (words["protocol_description"].format(protocol=protocol.title())
            if protocol else words["empty_description"])


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if str(item).strip()]


def _choice(option_id: str, *, available: bool | None, prose: Mapping[str, Any] | None = None, fallback_label: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": option_id,
        "label": str((prose or {}).get("label") or fallback_label or option_id),
        "description": (str((prose or {})["description"]) if (prose or {}).get("description") else None),
    }
    if available is not None:
        out["available"] = available
    return out


def _service_capabilities(service: Mapping[str, Any]) -> list[str]:
    config = service.get("config")
    config = config if isinstance(config, Mapping) else {}
    raw = service.get("capabilities") or config.get("capabilities") or ()
    if isinstance(raw, str):
        raw = (raw,)
    return list(dict.fromkeys(PROTOCOL_CAPABILITIES[str(cap)] for cap in raw if str(cap) in PROTOCOL_CAPABILITIES))


_RECEIPT_DETAIL_LABELS = {
    "none": "None", "basic": "Basic", "standard": "Standard", "full": "Full",
}
_MEDIA_METADATA_LABELS = {"none": "Strip workflow data from files", "full": "Keep workflow data in files"}


def access_plan_editor_contract(registry: Mapping[str, Any],
                                extra_providers: Iterable[Mapping[str, Any]] = (), *,
                                snapshot: Snapshot | None = None) -> dict[str, Any]:
    """Choices for the access plan editor, read from the service registry.

    Models and workflows are listed per capability; engines are the local
    services an app could call through their own API; paid providers are the
    remote services. All of it is data the operator configured.
    """
    normalized = service_registry.normalize_service_registry(registry)
    items: dict[str, dict[tuple[str, str], bool]] = {cap: {} for cap in CAPABILITIES}
    item_connections: dict[tuple[str, str, str], set[str]] = {}
    engines: dict[str, dict[str, Any]] = {}
    providers: dict[str, dict[str, Any]] = {}
    connections: dict[str, dict[str, Any]] = {}
    for service_id, service in normalized["services"].items():
        if not isinstance(service, Mapping):
            continue
        enabled = bool(service.get("enabled", True))
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        label = str(service.get("name") or service_id)
        source = config.get("credential_source") or {"kind": "static-key"}
        if source.get("kind") in {"connection", "cli-session"}:
            connection_id = str(source["connection_id"])
            connections[connection_id] = {"id": connection_id, "label": label, "available": enabled,
                                          "service_id": service_id, "request_protocol": config.get("request_protocol"),
                                          "credential_source": dict(source)}
        elif service_registry.is_remote_provider_config(config):
            providers[str(service_id)] = {"id": str(service_id), "label": label, "available": enabled}
        else:
            engines[str(service_id)] = {"id": str(service_id), "label": label, "available": enabled}
    # Discovery rows exist only in the held runtime snapshot, the same rows
    # used by model publication and dispatch. Never persist a second catalog.
    model_services = snapshot.services if snapshot is not None else normalized["services"]
    for service_id, service in model_services.items():
        config = service.get("config") or {}
        enabled = bool(service.get("enabled", True))
        capabilities = _service_capabilities(service)
        source = config.get("credential_source") or {}
        if not capabilities and source.get("kind") in {"connection", "cli-session"}:
            capabilities = ["chat"]
        for model in model_records(config):
            if model.get("execution_only") is True:
                continue
            model_id = str(model.get("model_group") or model.get("id") or "").strip()
            owner = (service_id, model.get("base_model_id") or model.get("id"))
            edges = snapshot.model_routes.by_model.get(owner, ()) if snapshot is not None else ()
            steps = (route_step(model_services, edge) for edge in edges)
            connection_ids = {step.connection_id for step in steps if step.connection_id}
            for capability in capabilities:
                if model_id:
                    key = ("model", model_id)
                    items[capability][key] = items[capability].get(key, False) or enabled
                    if connection_ids:
                        item_connections.setdefault((capability, *key), set()).update(connection_ids)
    for extra in extra_providers:  # exact connection grants, independent of paid API access
        connections[str(extra["id"])] = {**connections.get(str(extra["id"]), {}), "id": str(extra["id"]), "label": str(extra["label"]),
                                          "available": bool(extra.get("available", True))}
        if extra.get("grant_note"):  # the kind's terms reminder, shown beside the grant
            connections[str(extra["id"])]["grant_note"] = str(extra["grant_note"])
    for workflow_id, workflow in normalized["workflows"].items():
        if not isinstance(workflow, Mapping):
            continue
        manifest = workflow.get("manifest") if isinstance(workflow.get("manifest"), Mapping) else {}
        capability = INTENT_CAPABILITIES.get(str(manifest.get("intent") or ""))
        if capability:
            key = ("workflow", str(workflow.get("id") or workflow_id))
            items[capability][key] = items[capability].get(key, False) or bool(workflow.get("enabled", True))
    return {
        "schema_version": 2,
        "capabilities": [
            {
                "id": capability,
                "label": CAPABILITY_LABELS[capability],
                "items": [
                    {"kind": kind, "id": item_id, "available": available,
                     **({"connection_ids": sorted(item_connections[(capability, kind, item_id)])}
                        if (capability, kind, item_id) in item_connections else {})}
                    for (kind, item_id), available in sorted(items[capability].items())
                ],
            }
            for capability in CAPABILITIES
        ],
        "engines": [engines[key] for key in sorted(engines)],
        "paid_providers": [providers[key] for key in sorted(providers)],
        "connections": [connections[key] for key in sorted(connections)],
        "receipt_detail": [_choice(level, available=None, fallback_label=_RECEIPT_DETAIL_LABELS[level])
                           for level in RECEIPT_DETAIL_LEVELS],
        "media_metadata": [_choice(level, available=None, fallback_label=_MEDIA_METADATA_LABELS[level])
                           for level in MEDIA_METADATA_LEVELS],
        # The editor starts from this document: every capability, local only.
        "new_plan": {
            "name": "",
            "can_use": {capability: {"items": ALL} for capability in CAPABILITIES},
            "direct_engine_access": {"engines": [], "operations": []},
            "paid_providers": [],
            "connections": [],
            "provider_fallback": False,
            "caps": {"usd_per_request": None, "usd_per_day": None, "usd_per_month": None},
            "can_load_models": True,
            "receipt_detail": "basic",
            "media_metadata": "none",
            "categories": [],
        },
    }

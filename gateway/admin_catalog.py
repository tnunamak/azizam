"""Admin dashboard catalog loaders.

This module projects registry data into renderer-friendly catalog records.
Live service state, workflows, models, and user data are layered in by
``proxy.py`` before rendering the dashboard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from gateway import service_registry
from gateway.access_plans import ALL, CAPABILITIES, MEDIA_METADATA_LEVELS, RECEIPT_DETAIL_LEVELS
from gateway.capability_ids import CAPABILITY_LABELS, PROTOCOL_CAPABILITIES

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
        _normalize_admin_surface(record)
        for record in records
        if isinstance(record, Mapping)
    ]


def _normalize_admin_surface(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": str(record.get("name") or "Admin API"),
        "route_base": str(record.get("route_base") or ""),
        "status": str(record.get("status") or "live"),
        "profile_gates": _string_list(record.get("profile_gates")),
        "clients": _string_list(record.get("clients")),
        "description": str(record.get("description") or ""),
    }


def _api_surface_from_protocol_frontend(frontend: Mapping[str, Any]) -> dict[str, Any]:
    protocol = str(frontend.get("protocol") or frontend.get("id") or "")
    return {
        "name": str(frontend.get("name") or protocol or "Protocol frontend"),
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
    if intents:
        return "Supports " + ", ".join(intents) + "."
    protocol = str(frontend.get("protocol") or "protocol").strip()
    return f"{protocol.title()} protocol frontend." if protocol else "Protocol frontend."


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


def access_plan_editor_contract(registry: Mapping[str, Any]) -> dict[str, Any]:
    """Choices for the access plan editor, read from the service registry.

    Models and workflows are listed per capability; engines are the local
    services an app could call through their own API; paid providers are the
    remote services. All of it is data the operator configured.
    """
    normalized = service_registry.normalize_service_registry(registry)
    items: dict[str, dict[tuple[str, str], bool]] = {cap: {} for cap in CAPABILITIES}
    engines: dict[str, dict[str, Any]] = {}
    providers: dict[str, dict[str, Any]] = {}
    for service_id, service in normalized["services"].items():
        if not isinstance(service, Mapping):
            continue
        enabled = bool(service.get("enabled", True))
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        label = str(service.get("name") or service_id)
        if service_registry.is_remote_provider_config(config):
            providers[str(service_id)] = {"id": str(service_id), "label": label, "available": enabled}
        else:
            engines[str(service_id)] = {"id": str(service_id), "label": label, "available": enabled}
        capabilities = _service_capabilities(service)
        for model in config.get("models") or []:
            if not isinstance(model, Mapping):
                continue
            model_id = str(model.get("model_group") or model.get("id") or "").strip()
            for capability in capabilities:
                if model_id:
                    key = ("model", model_id)
                    items[capability][key] = items[capability].get(key, False) or enabled
    for workflow_id, workflow in normalized["workflows"].items():
        if not isinstance(workflow, Mapping):
            continue
        manifest = workflow.get("manifest") if isinstance(workflow.get("manifest"), Mapping) else {}
        capability = PROTOCOL_CAPABILITIES.get(str(manifest.get("intent") or ""))
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
                    {"kind": kind, "id": item_id, "available": available}
                    for (kind, item_id), available in sorted(items[capability].items())
                ],
            }
            for capability in CAPABILITIES
        ],
        "engines": [engines[key] for key in sorted(engines)],
        "paid_providers": [providers[key] for key in sorted(providers)],
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
            "provider_fallback": False,
            "caps": {"usd_per_request": None, "usd_per_day": None, "usd_per_month": None},
            "can_load_models": True,
            "receipt_detail": "basic",
            "media_metadata": "none",
        },
    }

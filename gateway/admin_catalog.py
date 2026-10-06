"""Admin dashboard catalog loaders.

This module projects registry data into renderer-friendly catalog records.
Live service state, workflows, models, and user data are layered in by
``proxy.py`` before rendering the dashboard.
"""

from __future__ import annotations

from gateway.compat import read_field

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

from gateway import service_registry
from gateway.policy import (
    default_profile_id,
    TARGET_ACTIVATION_MODES,
    TARGET_VISIBILITY_MODES,
    VISIBILITY_LEVELS,
)
from gateway.profiles import serialize_default_profile
from gateway.admin_store import RUNTIME_PROFILE_MAPPING_SOURCE_SPECS

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
_PROFILE_POLICY_CATALOG_PATH = _REGISTRY_DIR / "profile_policy_catalog.yml"
_ADMIN_SURFACES_CATALOG_NAME = "admin_surfaces.yml"
_PROFILE_POLICY_SECTION_VALIDATORS: dict[str, frozenset[str]] = {
    "target_visibility": frozenset(TARGET_VISIBILITY_MODES),
    "target_activation": frozenset(TARGET_ACTIVATION_MODES),
    "status_visibility": frozenset(VISIBILITY_LEVELS),
}


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


@lru_cache(maxsize=1)
def _bundled_profile_policy_catalog() -> dict[str, Any]:
    return load_profile_policy_catalog(_PROFILE_POLICY_CATALOG_PATH)


def load_profile_policy_catalog(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _normalize_profile_policy_catalog(raw, source=path)


def _normalize_profile_policy_catalog(
    raw: Any,
    *,
    source: object = "profile policy catalog",
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{source} must contain a mapping of catalog sections")

    catalog: dict[str, Any] = {}
    for section, options in raw.items():
        if not isinstance(section, str):
            continue
        if section == "fallback_profile":
            if not isinstance(options, Mapping):
                raise ValueError(f"{source}:fallback_profile must contain a mapping")
            fallback_id = str(options.get("id") or "").strip()
            if fallback_id != default_profile_id():
                raise ValueError(f"{source}:fallback_profile must use {default_profile_id()!r}")
            catalog[section] = dict(options, id=fallback_id)
            continue
        if not isinstance(options, list):
            raise ValueError(f"{source}:{section} must contain a list of options")
        catalog[section] = [
            _normalize_profile_policy_option(section, option, source=source)
            for option in options
            if isinstance(option, Mapping) and option.get("id")
        ]
    return catalog


def _normalize_profile_policy_option(
    section: str,
    option: Mapping[str, Any],
    *,
    source: object,
) -> dict[str, Any]:
    option_id = str(option.get("id") or "").strip()
    valid_ids = _PROFILE_POLICY_SECTION_VALIDATORS.get(section)
    if valid_ids is not None and option_id not in valid_ids:
        raise ValueError(f"{source}:{section} contains unsupported id {option_id!r}")

    normalized = dict(option)
    normalized["id"] = option_id
    return normalized


def profile_policy_catalog(
    *,
    catalog_dir: str | Path | None = None,
) -> dict[str, Any]:
    if catalog_dir is None:
        catalog = _bundled_profile_policy_catalog()
    else:
        path = Path(catalog_dir) / "profile_policy_catalog.yml"
        catalog = (
            load_profile_policy_catalog(path)
            if path.exists()
            else _bundled_profile_policy_catalog()
        )
    return copy.deepcopy(catalog)


def profile_policy_label_map(section: str) -> dict[str, str]:
    return {
        str(option["id"]): str(option.get("summary_label") or option.get("label"))
        for option in _bundled_profile_policy_catalog().get(section, [])
        if option.get("id")
    }


def coerce_policy_catalog(value: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, Mapping):
        return profile_policy_catalog()
    out = profile_policy_catalog()
    for section, options in value.items():
        if isinstance(section, str) and isinstance(options, list):
            out[section] = [
                dict(option)
                for option in options
                if isinstance(option, Mapping) and option.get("id")
            ]
    return out


def _catalog_option_map(catalog: Mapping[str, Any], section: str) -> dict[str, Mapping[str, Any]]:
    options = catalog.get(section)
    if not isinstance(options, list):
        return {}
    return {str(option["id"]): option for option in options if isinstance(option, Mapping) and option.get("id")}


def _choice(option_id: str, *, available: bool | None, prose: Mapping[str, Any] | None = None, fallback_label: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": option_id,
        "label": str((prose or {}).get("label") or fallback_label or option_id),
        "description": (str((prose or {})["description"]) if (prose or {}).get("description") else None),
    }
    if available is not None:
        out["available"] = available
    return out


def _policy_values(record: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    for route in record.get("routes") or []:
        if not isinstance(route, Mapping):
            continue
        for key in ("runtime_policy", "azizam_policy"):
            policy = read_field(route, key)
            if isinstance(policy, Mapping) and isinstance(policy.get("intent"), str):
                values.append(policy["intent"])
    return values


def policy_editor_contract(registry: Mapping[str, Any]) -> dict[str, Any]:
    """Safely join normalized registry facts with presentation-only catalog data."""
    normalized = service_registry.normalize_service_registry(registry)
    catalog = profile_policy_catalog()
    dynamic: dict[str, dict[str, bool]] = {key: {} for key in ("protocols", "intents", "workflows", "models")}

    def add(section: str, value: object, available: bool) -> None:
        option_id = str(value or "").strip()
        if option_id:
            dynamic[section][option_id] = dynamic[section].get(option_id, False) or available

    for frontend in normalized["protocol_frontends"].values():
        if not isinstance(frontend, Mapping):
            continue
        enabled = bool(frontend.get("enabled", True))
        add("protocols", frontend.get("protocol"), enabled)
        for intent in frontend.get("intents") or []:
            add("intents", intent, enabled)
        for intent in _policy_values(frontend):
            add("intents", intent, enabled)
    for workflow in normalized["workflows"].values():
        if not isinstance(workflow, Mapping):
            continue
        enabled = bool(workflow.get("enabled", True))
        add("workflows", workflow.get("id"), enabled)
        manifest = workflow.get("manifest")
        if isinstance(manifest, Mapping):
            add("intents", manifest.get("intent"), enabled)
    capability_values: dict[str, set[str]] = {key: set() for key in ("exposure", "trust_tiers", "resource_tiers", "tags")}
    for capability in normalized["capabilities"].values():
        if not isinstance(capability, Mapping):
            continue
        enabled = bool(capability.get("enabled", True))
        add("intents", capability.get("intent"), enabled)
        for key, source_key in (("exposure", "exposure"), ("trust_tiers", "trust_tier"), ("resource_tiers", "resource_tier")):
            value = str(capability.get(source_key) or "").strip()
            if value:
                capability_values[key].add(value)
        for tag in capability.get("tags") or []:
            if str(tag).strip():
                capability_values["tags"].add(str(tag).strip())
    for service in normalized["services"].values():
        if not isinstance(service, Mapping):
            continue
        enabled = bool(service.get("enabled", True))
        config = service.get("config")
        if isinstance(config, Mapping):
            for model in config.get("models") or []:
                if isinstance(model, Mapping):
                    add("models", model.get("model_group") or model.get("id"), enabled)

    choices: dict[str, list[dict[str, Any]]] = {}
    for section in ("protocols", "intents", "workflows", "models"):
        prose = _catalog_option_map(catalog, section)
        choices[section] = [_choice(option_id, available=available, prose=prose.get(option_id)) for option_id, available in sorted(dynamic[section].items())]
    for section in ("exposure", "trust_tiers", "resource_tiers", "tags"):
        prose = _catalog_option_map(catalog, section)
        ids = capability_values[section] | set(prose)
        choices[section] = [_choice(option_id, available=None, prose=prose.get(option_id)) for option_id in sorted(ids)]
    for section in ("status_visibility", "target_visibility", "target_activation"):
        choices[section] = [_choice(str(option["id"]), available=None, prose=option) for option in catalog.get(section, [])]
    fallback = catalog.get("fallback_profile")
    if not isinstance(fallback, Mapping):
        raise ValueError("profile policy catalog is missing fallback_profile")
    return {
        "schema_version": 1,
        "profile": {"create": {"profile": serialize_default_profile()}, "set_semantics": {"omitted": "no_opinion", "empty": "deny_all"}, "choices": choices},
        "runtime_mappings": {
            "source_kinds": [{key: value for key, value in spec.items() if key != "priority"} for spec in RUNTIME_PROFILE_MAPPING_SOURCE_SPECS],
            "fallback_profile": {"id": default_profile_id(), "label": str(fallback.get("label") or default_profile_id()), "description": str(fallback.get("description") or "") or None, "assignable": True},
        },
    }

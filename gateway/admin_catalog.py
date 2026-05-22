"""Admin dashboard catalog loaders.

This module projects registry data into renderer-friendly catalog records.
Live service state, workflows, models, and user data are layered in by
``proxy.py`` before rendering the dashboard.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

from gateway import service_registry
from gateway.policy import (
    TARGET_ACTIVATION_MODES,
    TARGET_VISIBILITY_MODES,
    VISIBILITY_LEVELS,
)

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
def _bundled_profile_policy_catalog() -> dict[str, list[dict[str, Any]]]:
    return load_profile_policy_catalog(_PROFILE_POLICY_CATALOG_PATH)


def load_profile_policy_catalog(path: Path) -> dict[str, list[dict[str, Any]]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _normalize_profile_policy_catalog(raw, source=path)


def _normalize_profile_policy_catalog(
    raw: Any,
    *,
    source: object = "profile policy catalog",
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{source} must contain a mapping of catalog sections")

    catalog: dict[str, list[dict[str, Any]]] = {}
    for section, options in raw.items():
        if not isinstance(section, str):
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
) -> dict[str, list[dict[str, Any]]]:
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

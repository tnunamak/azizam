"""Protocol route metadata projected from registry records."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from gateway import service_registry


def protocol_frontend_records(
    *,
    registry: Mapping[str, Any] | None = None,
    proxy_config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    seed_frontends = service_registry.load_seed_catalogs(proxy_config or {}).get(
        "protocol_frontends",
        {},
    )
    if isinstance(registry, Mapping) and isinstance(
        registry.get("protocol_frontends"),
        Mapping,
    ):
        values = registry["protocol_frontends"].values()
    else:
        values = seed_frontends.values()
    records: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        record = copy.deepcopy(dict(item))
        seed = seed_frontends.get(str(record.get("id") or ""))
        if isinstance(seed, Mapping):
            for key in (
                "registered_routes",
                "asset_content_policies",
                "public_discovery",
            ):
                if key not in record and key in seed:
                    record[key] = copy.deepcopy(seed[key])
        records.append(record)
    return records


def mount_frontend_records(
    *,
    registry: Mapping[str, Any] | None = None,
    proxy_config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The frontend records the route mount reads.

    A stored builtin frontend keeps its own fields (enabled, name), but its routes come from the
    seed: builtin frontends ship with the code and are not editable, so a database seeded before a
    row existed still mounts it. A builtin seed the stored registry lacks is added. Projections
    (aliases, policies) keep reading the stored records, as they did before.
    """
    seed_frontends = service_registry.load_seed_catalogs(proxy_config or {}).get("protocol_frontends", {})
    records: list[dict[str, Any]] = []
    stored_ids: set[str] = set()
    for record in protocol_frontend_records(registry=registry, proxy_config=proxy_config):
        frontend_id = str(record.get("id") or "")
        stored_ids.add(frontend_id)
        seed = seed_frontends.get(frontend_id)
        if record.get("source") == "builtin" and isinstance(seed, Mapping) and "registered_routes" in seed:
            record = {**record, "registered_routes": copy.deepcopy(seed["registered_routes"])}
        records.append(record)
    for frontend_id, seed in seed_frontends.items():
        if frontend_id not in stored_ids and isinstance(seed, Mapping) and seed.get("source") == "builtin":
            records.append(copy.deepcopy(dict(seed)))
    return records


def registered_route_records(
    *,
    registry: Mapping[str, Any] | None = None,
    proxy_config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    for frontend in protocol_frontend_records(
        registry=registry,
        proxy_config=proxy_config,
    ):
        if frontend.get("enabled") is False:
            continue
        raw_routes = frontend.get("registered_routes")
        if not isinstance(raw_routes, list):
            continue
        for route in raw_routes:
            if isinstance(route, Mapping):
                routes.append(copy.deepcopy(dict(route)))
    return routes


def canonical_protocol_route_aliases(
    *,
    registry: Mapping[str, Any] | None = None,
    proxy_config: Mapping[str, Any] | None = None,
) -> tuple[dict[str, object], ...]:
    aliases: list[dict[str, object]] = []
    for route in registered_route_records(
        registry=registry,
        proxy_config=proxy_config,
    ):
        canonical = _path(route.get("canonical"))
        alias = _path(route.get("alias"))
        methods = _methods(route.get("methods"))
        if not canonical or not alias or not methods:
            continue
        aliases.append(
            {
                "methods": tuple(methods),
                "canonical": canonical,
                "alias": alias,
            }
        )
    return tuple(aliases)


def runtime_route_policy(
    *,
    registry: Mapping[str, Any] | None = None,
    proxy_config: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    policies: dict[str, dict[str, Any]] = {}
    for route in registered_route_records(
        registry=registry,
        proxy_config=proxy_config,
    ):
        path = _path(route.get("canonical") or route.get("path"))
        policy = route.get("runtime_policy")
        if path and isinstance(policy, Mapping):
            policies[path] = copy.deepcopy(dict(policy))
    return policies


def azizam_policy(
    *,
    registry: Mapping[str, Any] | None = None,
    proxy_config: Mapping[str, Any] | None = None,
) -> dict[str, tuple[str, str]]:
    policies: dict[str, tuple[str, str]] = {}
    for route in registered_route_records(
        registry=registry,
        proxy_config=proxy_config,
    ):
        path = _path(route.get("canonical") or route.get("path"))
        policy = route.get("azizam_policy")
        if not path or not isinstance(policy, Mapping):
            continue
        protocol = str(policy.get("protocol") or "").strip()
        intent = str(policy.get("intent") or "").strip()
        if protocol and intent:
            policies[path] = (protocol, intent)
    return policies


def asset_content_route_policy_by_kind(
    *,
    registry: Mapping[str, Any] | None = None,
    proxy_config: Mapping[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    policies: dict[str, dict[str, Any]] = {}
    for frontend in protocol_frontend_records(
        registry=registry,
        proxy_config=proxy_config,
    ):
        raw_policies = frontend.get("asset_content_policies")
        if not isinstance(raw_policies, Mapping):
            continue
        for kind, policy in raw_policies.items():
            if isinstance(policy, Mapping):
                policies[str(kind)] = copy.deepcopy(dict(policy))
    return policies


def _path(value: Any) -> str:
    return str(value or "").strip()


def _methods(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raw = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = value
    else:
        return []
    methods: list[str] = []
    for item in raw:
        method = str(item).strip().upper()
        if method and method not in methods:
            methods.append(method)
    return methods

"""Pure gateway extension discovery helpers."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

import yaml
from starlette.convertors import CONVERTOR_TYPES
from starlette.routing import PARAM_REGEX

from gateway import toolcall_detection as _message_data
from gateway.workflow_view import settable_inputs
from gateway.capabilities import Capability, summarize_capabilities
from gateway.chat_extensions import (
    CHAT_EXTENSION_NAMESPACE,
    CHAT_EXTENSION_SPECS,
    ROUTING_EXTENSION_NAMESPACE,
    ROUTING_EXTENSION_SPECS,
)
from gateway.discovery import VALID_AUDIENCES, WorkflowRegistry
from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE
from gateway.prompt_memory import NAMESPACE as PROMPT_MEMORY_NAMESPACE


_EXTENSION_ROWS_PATH = Path(__file__).resolve().parents[1] / "registry" / "protocol_frontends.yml"
_EXTENSION_NAMESPACES = (
    PROMPT_MEMORY_NAMESPACE, WORKFLOW_EXTENSION_NAMESPACE,
    CHAT_EXTENSION_NAMESPACE, ROUTING_EXTENSION_NAMESPACE,
)


@dataclass(frozen=True)
class ExtensionRoute:
    route: str
    order: int
    route_inputs: frozenset[str] = frozenset()
    intent_prefixes: tuple[str, ...] = ()
    route_intent: str | None = None


# No registered rows: the workflow-id route is dynamic; media jobs lives only
# under frontends. Keep their existing prompt-policy positions without fake rows.
_RESIDUAL_EXTENSION_ROUTES = (
    (PROMPT_MEMORY_NAMESPACE, ExtensionRoute("/v1/videos/workflows/{workflow_id}", 50)),
    (PROMPT_MEMORY_NAMESPACE, ExtensionRoute("/gateway/v1/media/jobs/image-generations", 90)),
)


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return tuple(value)


def _route_identity(route: str) -> str:
    """One route however it is spelled: no trailing slash, path parameters unnamed. A parameter keeps its converter
    (``{n:int}`` and ``{u:uuid}`` are two routes; ``{a}`` and ``{b:str}`` are one). Letter case stays (paths are
    case-sensitive)."""
    trimmed = route[:-1] if len(route) > 1 and route.endswith("/") else route
    return PARAM_REGEX.sub(lambda match: "{" + (match.group(2) or ":str") + "}", trimmed)


def load_extension_routes(
    path: str | Path = _EXTENSION_ROWS_PATH,
) -> Mapping[str, tuple[ExtensionRoute, ...]]:
    """Read release registered_routes once; expose immutable, explicitly ordered facts."""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping) or not isinstance(document.get("records"), list):
        raise ValueError("extension routes require a records list")
    found: dict[str, list[ExtensionRoute]] = {namespace: [] for namespace in _EXTENSION_NAMESPACES}
    orders: set[int] = set()
    identities: set[tuple[str, str]] = set()
    for namespace, residual in _RESIDUAL_EXTENSION_ROUTES:
        found[namespace].append(residual)
        orders.add(residual.order)
        identities.add((namespace, _route_identity(residual.route)))
    for record_number, record in enumerate(document["records"], start=1):
        if not isinstance(record, Mapping):
            raise ValueError("extension route record must be a mapping")
        rows = record.get("registered_routes", [])
        if not isinstance(rows, list):
            raise ValueError("registered_routes must be a list")
        for row_number, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                raise ValueError("registered route must be a mapping")
            if not any(key in row for key in (
                "extensions", "extension_order", "route_inputs", "workflow_targets",
            )):
                continue
            namespaces = _string_list(row.get("extensions"), "extensions")
            if not namespaces:
                raise ValueError("extensions must not be empty")
            for namespace in namespaces:
                if namespace not in found:
                    raise ValueError(f"unknown extension namespace: {namespace!r}")
            if len(set(namespaces)) != len(namespaces):
                raise ValueError("duplicate extension namespace")
            order = row.get("extension_order")
            if type(order) is not int:
                raise ValueError("extension_order must be an integer")
            if order in orders:
                raise ValueError(f"duplicate extension_order: {order}")
            orders.add(order)
            route = row.get("canonical")
            if not isinstance(route, str) or not route.startswith("/"):
                raise ValueError("extension route needs a canonical path")
            location = f"{path}: record {record_number}, row {row_number}"
            if "//" in route:
                raise ValueError(f"{location}: extension route has an empty path segment: {route!r}")
            parameter_names: set[str] = set()
            for parameter in PARAM_REGEX.finditer(route):
                converter = (parameter.group(2) or ":str")[1:]
                if converter not in CONVERTOR_TYPES:
                    raise ValueError(f"{location}: unknown path converter {converter!r}: {route!r}")
                name = parameter.group(1)
                if name in parameter_names:
                    raise ValueError(f"{location}: duplicate path parameter {name!r}: {route!r}")
                parameter_names.add(name)
            route_inputs = frozenset(_string_list(row.get("route_inputs", []), "route_inputs"))
            prefixes: tuple[str, ...] = ()
            route_intent: str | None = None
            if WORKFLOW_EXTENSION_NAMESPACE in namespaces:
                targets = row.get("workflow_targets")
                if not isinstance(targets, Mapping):
                    raise ValueError("workflow_targets must be a mapping")
                route_intent = targets.get("route_intent")
                if not isinstance(route_intent, str) or not route_intent:
                    raise ValueError("workflow_targets needs route_intent")
                prefixes = _string_list(targets.get("intent_prefixes"), "intent_prefixes")
                if not prefixes:
                    raise ValueError("intent_prefixes must not be empty")
                if "route_inputs" not in row:
                    raise ValueError("workflow route needs route_inputs")
            elif "workflow_targets" in row or "route_inputs" in row:
                raise ValueError("workflow_targets and route_inputs need the workflow extension")
            entry = ExtensionRoute(route, order, route_inputs, prefixes, route_intent)
            for namespace in namespaces:
                identity = (namespace, _route_identity(route))
                if identity in identities:
                    raise ValueError(
                        f"{location}: duplicate extension route: "
                        f"namespace {namespace!r}, route {route!r}"
                    )
                identities.add(identity)
                found[namespace].append(entry)
    return MappingProxyType({
        namespace: tuple(sorted(rows, key=lambda row: row.order))
        for namespace, rows in found.items()
    })


EXTENSION_ROUTES = load_extension_routes()


def summarize_extensions(registry: WorkflowRegistry, audience: str) -> dict[str, Any]:
    """Return a client-safe catalog of supported gateway extensions."""
    _check_audience(audience)

    workflow_targets = _workflow_targets(registry)
    return {
        "schema_version": 1,
        "audience": audience,
        "extensions": [
            {
                "namespace": PROMPT_MEMORY_NAMESPACE,
                "description": "Per-request prompt retention choice; this control is removed before backend dispatch.",
                "gateway_controls": [{"name": "mode", "values": ["forget"], "default": "member and household settings"}],
                "routes": [
                    {"route": row.route, "target": "gateway_prompt_policy", "controls": ["mode"]}
                    for row in EXTENSION_ROUTES[PROMPT_MEMORY_NAMESPACE]
                ],
            },
            {
                "namespace": WORKFLOW_EXTENSION_NAMESPACE,
                "description": "Gateway-owned workflow controls for image, video, and audio routes.",
                "gateway_controls": [
                    {
                        "name": "metadata",
                        "values": ["full", "none"],
                        "default": "the app profile's media_metadata.default",
                        "description": _message_data.GATEWAY_MESSAGES["metadata_description"],
                    }
                ],
                "routes": [
                    {
                        "route": row.route,
                        "target": "workflow_manifest_inputs",
                        "targets": _targets_for_route(
                            workflow_targets,
                            row.intent_prefixes,
                            route_inputs=row.route_inputs,
                        ),
                    }
                    for row in EXTENSION_ROUTES[WORKFLOW_EXTENSION_NAMESPACE]
                ],
            },
            {
                "namespace": CHAT_EXTENSION_NAMESPACE,
                "description": "Gateway-owned chat diagnostics controls.",
                "routes": [
                    {
                        "route": row.route,
                        "target": "chat_extension_inputs",
                        "controls": _spec_names(CHAT_EXTENSION_SPECS),
                    }
                    for row in EXTENSION_ROUTES[CHAT_EXTENSION_NAMESPACE]
                ],
            },
            {
                "namespace": ROUTING_EXTENSION_NAMESPACE,
                "description": "Gateway-owned routing hints that cannot override app profile policy.",
                "routes": [
                    {
                        "route": row.route,
                        "target": "routing_extension_inputs",
                        "controls": _spec_names(ROUTING_EXTENSION_SPECS),
                    }
                    for row in EXTENSION_ROUTES[ROUTING_EXTENSION_NAMESPACE]
                ],
            },
        ],
    }


def summarize_model_capabilities(
    capabilities: Iterable[Capability], *, model: str, audience: str
) -> dict[str, Any]:
    """Return the standard capability envelope filtered to a model/workflow name."""
    _check_audience(audience)
    return summarize_capabilities(
        (cap for cap in capabilities if _capability_matches_model(cap, model)),
        audience=audience,
    )


def _workflow_targets(registry: WorkflowRegistry) -> list[dict[str, Any]]:
    by_intent: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"backends": set(), "workflows": []}
    )
    for record in registry.records():
        for backend in record.supported_backends:
            by_intent[record.intent]["backends"].add(backend)
        by_intent[record.intent]["workflows"].append(
            {
                "id": record.id,
                "controls": _workflow_extension_controls(record.contract),
            }
        )

    return [
        {
            "intent": intent,
            "backends": sorted(data["backends"]),
            "workflows": sorted(data["workflows"], key=lambda item: item["id"]),
        }
        for intent, data in sorted(by_intent.items())
    ]


def _targets_for_route(
    targets: Iterable[dict[str, Any]],
    prefixes: tuple[str, ...],
    *,
    route_inputs: frozenset[str],
) -> list[dict[str, Any]]:
    return [
        _with_route_owned_controls_removed(target, route_inputs)
        for target in targets
        if any(str(target["intent"]).startswith(prefix) for prefix in prefixes)
    ]


def _workflow_extension_controls(manifest: dict[str, Any]) -> list[str]:
    inputs = settable_inputs(manifest)
    bindings = manifest.get("bindings")
    if not inputs or not isinstance(bindings, dict):
        return []
    mapped: set[str] = set()
    for binding in bindings.values():
        if not isinstance(binding, dict):
            continue
        mapping = binding.get("map")
        if isinstance(mapping, dict):
            mapped.update(str(name) for name in mapping if name in inputs)
    return sorted(mapped)


def _with_route_owned_controls_removed(
    target: dict[str, Any],
    route_inputs: frozenset[str],
) -> dict[str, Any]:
    workflows = []
    for workflow in target.get("workflows", []):
        controls = [
            name
            for name in workflow.get("controls", [])
            if name not in route_inputs
        ]
        workflows.append({"id": workflow["id"], "controls": controls})
    return {**target, "workflows": workflows}


def _spec_names(specs: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(specs)


def _capability_matches_model(capability: Capability, model: str) -> bool:
    if capability.model == model or capability.workflow == model:
        return True
    return model in _aliases_from_caveats(capability.caveats)


def _aliases_from_caveats(caveats: Iterable[str]) -> set[str]:
    aliases: set[str] = set()
    for caveat in caveats:
        if not caveat.startswith("aliases: "):
            continue
        aliases.update(
            alias.strip() for alias in caveat.removeprefix("aliases: ").split(",")
        )
    aliases.discard("")
    return aliases


def _check_audience(audience: str) -> None:
    if audience not in VALID_AUDIENCES:
        raise ValueError(f"audience must be one of {VALID_AUDIENCES!r}, got {audience!r}")

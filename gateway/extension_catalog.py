"""Pure gateway extension discovery helpers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from gateway.capabilities import Capability, summarize_capabilities
from gateway.chat_extensions import (
    CHAT_EXTENSION_NAMESPACE,
    CHAT_EXTENSION_SPECS,
    ROUTING_EXTENSION_NAMESPACE,
    ROUTING_EXTENSION_SPECS,
)
from gateway.discovery import VALID_AUDIENCES, WorkflowRegistry
from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE
from gateway.audio_extensions import ROUTE_MANAGED_AUDIO_INPUTS
from gateway.video_extensions import ROUTE_MANAGED_VIDEO_INPUTS


_WORKFLOW_ROUTES = (
    ("/v1/images/generations", ("image.generate",), "image.generate"),
    ("/v1/images/edits", ("image.edit",), "image.edit"),
    ("/v1/videos", ("video.",), "video.generate"),
    ("/v1/audio/generate", ("audio.",), "audio.generate"),
)

_ROUTE_MANAGED_WORKFLOW_INPUTS: dict[str, frozenset[str]] = {
    "image.generate": frozenset({"prompt", "source_image", "width", "height"}),
    "image.edit": frozenset(
        {
            "prompt",
            "source_image",
            "width",
            "height",
            "mask",
            "source_mask",
            "mask_asset",
        }
    ),
    "audio.generate": ROUTE_MANAGED_AUDIO_INPUTS,
    "video.generate": ROUTE_MANAGED_VIDEO_INPUTS,
}


def summarize_extensions(registry: WorkflowRegistry, audience: str) -> dict[str, Any]:
    """Return a client-safe catalog of supported gateway extensions."""
    _check_audience(audience)

    workflow_targets = _workflow_targets(registry)
    return {
        "schema_version": 1,
        "audience": audience,
        "extensions": [
            {
                "namespace": WORKFLOW_EXTENSION_NAMESPACE,
                "description": "Gateway-owned workflow controls for image, video, and audio routes.",
                "gateway_controls": [
                    {
                        "name": "metadata",
                        "values": ["full", "none"],
                        "default": "the app profile's media_metadata.default",
                        "description": (
                            "Whether delivered media keeps its embedded ComfyUI workflow "
                            "and gateway provenance (full) or has all metadata removed "
                            "losslessly (none). Valid on every route that returns generated media."
                        ),
                    }
                ],
                "routes": [
                    {
                        "route": route,
                        "target": "workflow_manifest_inputs",
                        "targets": _targets_for_route(
                            workflow_targets,
                            prefixes,
                            route_intent=route_intent,
                        ),
                    }
                    for route, prefixes, route_intent in _WORKFLOW_ROUTES
                ],
            },
            {
                "namespace": CHAT_EXTENSION_NAMESPACE,
                "description": "Gateway-owned chat diagnostics controls.",
                "routes": [
                    {
                        "route": "/v1/chat/completions",
                        "target": "chat_extension_inputs",
                        "controls": _spec_names(CHAT_EXTENSION_SPECS),
                    },
                    {
                        "route": "/v1/responses",
                        "target": "chat_extension_inputs",
                        "controls": _spec_names(CHAT_EXTENSION_SPECS),
                    },
                ],
            },
            {
                "namespace": ROUTING_EXTENSION_NAMESPACE,
                "description": "Gateway-owned routing hints that cannot override app profile policy.",
                "routes": [
                    {
                        "route": "/v1/chat/completions",
                        "target": "routing_extension_inputs",
                        "controls": _spec_names(ROUTING_EXTENSION_SPECS),
                    },
                    {
                        "route": "/v1/responses",
                        "target": "routing_extension_inputs",
                        "controls": _spec_names(ROUTING_EXTENSION_SPECS),
                    },
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
                "controls": _workflow_extension_controls(record.manifest),
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
    route_intent: str,
) -> list[dict[str, Any]]:
    return [
        _with_route_owned_controls_removed(target, route_intent)
        for target in targets
        if any(str(target["intent"]).startswith(prefix) for prefix in prefixes)
    ]


def _workflow_extension_controls(manifest: dict[str, Any]) -> list[str]:
    inputs = manifest.get("inputs")
    bindings = manifest.get("bindings")
    if not isinstance(inputs, dict) or not isinstance(bindings, dict):
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
    route_intent: str,
) -> dict[str, Any]:
    route_managed = _ROUTE_MANAGED_WORKFLOW_INPUTS.get(route_intent, frozenset())
    workflows = []
    for workflow in target.get("workflows", []):
        controls = [
            name
            for name in workflow.get("controls", [])
            if name not in route_managed
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

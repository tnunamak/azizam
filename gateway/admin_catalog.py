"""Static admin dashboard catalogs.

This module holds stable product vocabulary: protocol families, capability
selectors, and front-door API surfaces. Live service state, workflows, models,
and user data are layered in by ``proxy.py`` before rendering the dashboard.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping

from gateway.policy import (
    TARGET_ACTIVATION_MODES,
    TARGET_VISIBILITY_MODES,
    VISIBILITY_LEVELS,
)

_API_SURFACES: tuple[dict[str, Any], ...] = (
    {
        "name": "OpenAI-compatible",
        "route_base": "/v1",
        "status": "live",
        "profile_gates": ["openai"],
        "clients": ["OpenAI SDKs", "Open WebUI", "LibreChat", "Continue"],
        "description": (
            "Chat, completions, responses, embeddings, models, images, audio, "
            "and video routes."
        ),
    },
    {
        "name": "Anthropic Messages",
        "route_base": "/v1/messages",
        "status": "partial",
        "profile_gates": ["anthropic"],
        "clients": ["Claude-shaped coding agents"],
        "description": "Messages-compatible chat surface backed by local LLM routing.",
    },
    {
        "name": "Ollama-compatible",
        "route_base": "/api",
        "status": "partial",
        "profile_gates": ["ollama"],
        "clients": ["Ollama SDKs", "local model tools"],
        "description": "Ollama-shaped chat/generate/tags compatibility where it maps cleanly.",
    },
    {
        "name": "Gateway-native",
        "route_base": "/gateway/v1",
        "status": "live",
        "profile_gates": ["gateway"],
        "clients": ["agents", "media tooling"],
        "description": "Capabilities, workflows, receipts, and durable media jobs.",
    },
    {
        "name": "Admin API",
        "route_base": "/admin/api",
        "status": "live",
        "profile_gates": ["admin session", "oak_"],
        "clients": ["operators", "automation"],
        "description": "Control-plane API for Apps, Profiles, keys, audit, and policy simulation.",
    },
)

_PROFILE_POLICY_CATALOG: dict[str, list[dict[str, Any]]] = {
    "protocols": [
        {
            "id": "openai",
            "label": "OpenAI-compatible",
            "summary_label": "OpenAI-compatible",
            "description": "/openai/v1 and /v1 SDKs",
            "default": True,
        },
        {
            "id": "anthropic",
            "label": "Anthropic Messages",
            "summary_label": "Anthropic Messages",
            "description": "/anthropic/v1/messages clients",
        },
        {
            "id": "ollama",
            "label": "Ollama-compatible",
            "summary_label": "Ollama-compatible",
            "description": "/ollama/api and /api clients",
        },
        {
            "id": "gateway",
            "label": "Gateway jobs",
            "summary_label": "Gateway jobs",
            "description": "capabilities, assets, receipts, media jobs",
        },
    ],
    "intents": [
        {
            "id": "chat",
            "label": "Chat",
            "summary_label": "chat",
            "description": "chat completions and messages",
            "default": True,
        },
        {
            "id": "text.completions",
            "label": "Text completions",
            "summary_label": "text completions",
            "description": "legacy completion clients",
        },
        {
            "id": "text.embeddings",
            "label": "Embeddings",
            "summary_label": "embeddings",
            "description": "RAG, search, memory, indexes",
        },
        {
            "id": "image.generate",
            "label": "Image generation",
            "summary_label": "image generation",
            "description": "text to image workflows",
        },
        {
            "id": "image.edit",
            "label": "Image editing",
            "summary_label": "image editing",
            "description": "inpaint, mask, ControlNet-like workflows",
        },
        {
            "id": "speech.synthesis",
            "label": "Text to speech",
            "summary_label": "text to speech",
            "description": "voice output",
        },
        {
            "id": "speech.transcription",
            "label": "Speech to text",
            "summary_label": "speech transcription",
            "description": "transcription input",
        },
        {
            "id": "audio.generate",
            "label": "Music / sound",
            "summary_label": "music/audio generation",
            "description": "audio generation workflows",
        },
        {
            "id": "video.generate",
            "label": "Video",
            "summary_label": "video generation",
            "description": "expensive queued video jobs",
        },
        {
            "id": "models.list",
            "label": "Model list",
            "summary_label": "model list",
            "description": "show compatible models to clients",
        },
    ],
    "resource_tiers": [
        {
            "id": "interactive",
            "label": "Interactive",
            "summary_label": "interactive",
            "description": "fast requests, normal chat",
            "default": True,
        },
        {
            "id": "batch",
            "label": "Batch",
            "summary_label": "batch",
            "description": "queued media and longer jobs",
        },
        {
            "id": "expensive",
            "label": "Expensive",
            "summary_label": "expensive",
            "description": "video and heavy GPU workflows",
        },
    ],
    "exposure": [
        {
            "id": "default",
            "label": "Default capabilities",
            "summary_label": "default capabilities",
            "description": "normal model and workflow catalog",
            "default": True,
        },
        {
            "id": "opt_in",
            "label": "Opt-in capabilities",
            "summary_label": "opt-in capabilities",
            "description": "operator-curated extras",
        },
    ],
    "trust_tiers": [
        {
            "id": "stable",
            "label": "Stable",
            "summary_label": "stable",
            "description": "expected to work for users",
            "default": True,
        },
        {
            "id": "experimental",
            "label": "Experimental",
            "summary_label": "experimental",
            "description": "new, flaky, or changing",
        },
        {
            "id": "private",
            "label": "Private",
            "summary_label": "private",
            "description": "personal/local-only workflows",
        },
    ],
    "target_visibility": [
        {
            "id": TARGET_VISIBILITY_MODES[0],
            "label": "Allowed catalog",
            "summary_label": "allowed catalog",
            "description": (
                "Clients can see targets this Profile is allowed to use, even "
                "when a target may be cold."
            ),
            "default": True,
        },
        {
            "id": TARGET_VISIBILITY_MODES[1],
            "label": "Available now",
            "summary_label": "available now",
            "description": (
                "Clients only see targets the gateway believes are currently "
                "usable under this Profile and runtime state."
            ),
        },
    ],
    "target_activation": [
        {
            "id": TARGET_ACTIVATION_MODES[0],
            "label": "May activate",
            "summary_label": "may activate",
            "description": (
                "Requests may load, switch, start, or queue a target when the "
                "Profile and runtime policy allow it."
            ),
            "default": True,
        },
        {
            "id": TARGET_ACTIVATION_MODES[1],
            "label": "Existing only",
            "summary_label": "existing only",
            "description": (
                "Requests must use already loaded/running/selected targets; "
                "cold or disruptive switches are denied."
            ),
        },
    ],
    "status_visibility": [
        {
            "id": level,
            "label": level.replace("_", " ").title(),
            "default": level == "basic",
        }
        for level in VISIBILITY_LEVELS
    ],
}


def admin_api_surfaces() -> list[dict[str, Any]]:
    return copy.deepcopy(list(_API_SURFACES))


def profile_policy_catalog() -> dict[str, list[dict[str, Any]]]:
    return copy.deepcopy(_PROFILE_POLICY_CATALOG)


def profile_policy_label_map(section: str) -> dict[str, str]:
    return {
        str(option["id"]): str(option.get("summary_label") or option.get("label"))
        for option in _PROFILE_POLICY_CATALOG.get(section, [])
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

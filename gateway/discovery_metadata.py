"""Public discovery metadata for agent clients.

Protocol/front-door facts are projected from the public protocol frontend
catalog. The live FastAPI app passes a request-derived base URL when it wants
absolute links; tests and docs can use the same builder with a fixed base URL.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

PROTOCOL_FRONTENDS_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "registry" / "protocol_frontends.yml"
)


def _load_protocol_frontend_records(
    path: Path = PROTOCOL_FRONTENDS_CATALOG_PATH,
) -> tuple[dict[str, Any], ...]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records = raw.get("records") if isinstance(raw, Mapping) else None
    if not isinstance(records, list):
        return ()
    return tuple(dict(record) for record in records if isinstance(record, Mapping))


def _route_path(route: Any) -> str:
    if isinstance(route, Mapping):
        return str(route.get("path") or "").strip()
    return str(route or "").strip()


def _route_paths(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for route in value:
        path = _route_path(route)
        if path and path not in out:
            out.append(path)
    return out


def _canonical_base_path(record: Mapping[str, Any], discovery: Mapping[str, Any]) -> str:
    if discovery.get("canonical_base_path"):
        return str(discovery["canonical_base_path"])
    routes = _route_paths(record.get("routes"))
    return next((route for route in routes if route), "")


def _convenience_base_path(record: Mapping[str, Any], canonical_base_path: str) -> str | None:
    routes = _route_paths(record.get("routes"))
    for route in routes:
        if route and route != canonical_base_path:
            return route
    return None


def _protocol_frontend_records(
    protocol_frontends: Mapping[str, Any] | tuple[Mapping[str, Any], ...] | None,
) -> Any:
    if protocol_frontends is None:
        return _load_protocol_frontend_records()
    if isinstance(protocol_frontends, Mapping):
        return protocol_frontends.values()
    return protocol_frontends


def build_public_protocols(
    protocol_frontends: Mapping[str, Any] | tuple[Mapping[str, Any], ...] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Project registry protocol frontend records into public discovery JSON."""

    protocols: list[dict[str, Any]] = []
    for record in _protocol_frontend_records(protocol_frontends):
        if not isinstance(record, Mapping):
            continue
        if record.get("enabled") is False:
            continue
        discovery = record.get("public_discovery")
        if not isinstance(discovery, Mapping):
            continue
        protocol_id = str(record.get("id") or record.get("protocol") or "").strip()
        if not protocol_id:
            continue
        canonical_base_path = _canonical_base_path(record, discovery)
        protocol: dict[str, Any] = {
            "id": protocol_id,
            "base_path": str(discovery.get("base_path") or canonical_base_path),
        }
        if canonical_base_path:
            protocol["canonical_base_path"] = canonical_base_path
        convenience_base_path = discovery.get("convenience_base_path")
        if convenience_base_path is None:
            convenience_base_path = _convenience_base_path(record, canonical_base_path)
        if convenience_base_path:
            protocol["convenience_base_path"] = str(convenience_base_path)
        for key in (
            "compatibility",
            "default",
            "base_url_recipe",
            "streaming",
            "policy",
            "endpoints",
            "limits",
        ):
            if key in discovery:
                protocol[key] = copy.deepcopy(discovery[key])
        protocols.append(protocol)
    return tuple(protocols)


PUBLIC_PROTOCOLS: tuple[dict[str, Any], ...] = build_public_protocols()


def _recipe_value(root: str, key: str, value: Any) -> Any:
    url_field = key.endswith(("_url", "_base_url", "_url_template"))
    if url_field and isinstance(value, str) and value.startswith("/"):
        if value == "/":
            return root
        return f"{root}{value}"
    return copy.deepcopy(value)


def build_public_recipes(
    *,
    root: str,
    protocol_frontends: Mapping[str, Any] | tuple[Mapping[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Project public recipe fragments from protocol frontend records."""

    recipes: dict[str, Any] = {}
    for record in _protocol_frontend_records(protocol_frontends):
        if not isinstance(record, Mapping) or record.get("enabled") is False:
            continue
        discovery = record.get("public_discovery")
        if not isinstance(discovery, Mapping):
            continue
        recipe = discovery.get("recipe")
        if not isinstance(recipe, Mapping):
            continue
        protocol_id = str(record.get("id") or record.get("protocol") or "").strip()
        if not protocol_id:
            continue
        recipes[protocol_id.replace("-", "_")] = {
            key: _recipe_value(root, key, value) for key, value in recipe.items()
        }
    return recipes


def _join(base_url: str | None, path: str) -> str:
    if not base_url:
        return path
    return f"{base_url.rstrip('/')}{path}"


def _base_url(base_url: str | None) -> str:
    return (base_url or "{base_url}").rstrip("/")


def build_gateway_discovery_metadata(
    *,
    base_url: str | None = None,
    service_name: str = "azizam",
    registry: Mapping[str, Any] | None = None,
    mcp_tools: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return unauthenticated public metadata for agent/client discovery.

    ``mcp_tools`` is the live tool list of the MCP server, so the Connect page
    shows what the server exposes instead of a copy that can drift.
    """
    root = _base_url(base_url)
    protocol_frontends = None
    if isinstance(registry, Mapping) and isinstance(
        registry.get("protocol_frontends"),
        Mapping,
    ):
        protocol_frontends = registry["protocol_frontends"]
    links = {
        "health": "/health",
        "login": "/login",
        "user_portal": "/",
        "user_session": "/me/api/session",
        "user_apps": "/me/api/apps",
        "user_app": "/me/api/apps/{app_id}",
        "user_app_keys": "/me/api/apps/{app_id}/keys",
        "user_api_key": "/me/api/credentials/{credential_id}",
        "user_api_key_access": "/me/api/credentials/{credential_id}/access",
        "user_access_request": "/me/api/access-request",
        "openapi": "/openapi.json",
        "llms_txt": "/llms.txt",
        "mcp": "/mcp",
        "skill_catalog": "/.well-known/skills",
        "skill": "/.well-known/skills/azizam-gateway/SKILL.md",
        "workflow_catalog": "/openapi.json#x-azizam-workflows",
        "capabilities": "/gateway/v1/capabilities",
        "extensions": "/gateway/v1/extensions",
        "model_capabilities": "/gateway/v1/models/{model}/capabilities",
        "workflows": "/gateway/v1/workflows",
        "receipts": "/gateway/v1/receipts",
        "media_jobs": "/gateway/v1/media/jobs",
        "asset": "/gateway/v1/assets/{asset_id}",
        "gateway_asset_content": "/gateway/v1/assets/{asset_id}/content",
        "providers": "/gateway/v1/providers",
        "provider": "/gateway/v1/providers/{provider}",
        "provider_native": "/gateway/v1/providers/{provider}/{path...}",
        "asset_content": "/v1/assets/{asset_id}/content",
    }

    return {
        "schema_version": 1,
        "object": "azizam_discovery",
        "service": {
            "name": service_name,
            "product": "Azizam",
            "base_url": base_url.rstrip("/") if base_url else None,
            "canonical_host": base_url.rstrip("/") if base_url else None,
            "local_first": True,
            "privacy": "Requests are routed to configured local backends unless an app profile explicitly allows fallback.",
        },
        "auth": {
            "type": "bearer",
            "header": "Authorization",
            "token_prefix": "opk_",
            "applies_to": [
                "/openai/v1/*",
                "/anthropic/v1/*",
                "/ollama/api/*",
                "/gateway/v1/*",
                "/v1/*",
                "/api/*",
            ],
            "public_without_auth": [
                "/health",
                "/.well-known/azizam",
                "/.well-known/skills",
                "/llms.txt",
                "/openapi.json",
            ],
            "signed_capability_urls": [
                "/v1/images/{id}/content",
                "/v1/assets/{asset_id}/content",
                "/gateway/v1/assets/{asset_id}/content",
            ],
            "signed_capability_url_contract": "Signed content URLs are bearerless capability URLs until expiry; metadata lookup and asset input reuse remain scoped to the minting API key.",
            "secret_handling": [
                "Do not ask users to paste raw opk tokens into chat.",
                "Use environment variables or local secret stores.",
                "Do not read or print local key stores.",
            ],
        },
        "protocols": [
            copy.deepcopy(protocol)
            for protocol in build_public_protocols(protocol_frontends)
        ],
        "recipes": build_public_recipes(
            root=root,
            protocol_frontends=protocol_frontends,
        ),
        "mcp": {
            "url": _join(base_url, "/mcp"),
            "transport": "streamable-http",
            "auth": "Authorization: Bearer <app key>",
            "tools": [
                {
                    "name": str(tool["name"]),
                    "title": str(tool.get("title") or tool["name"]),
                    "description": str(tool.get("description") or ""),
                }
                for tool in mcp_tools or ()
            ],
        },
        "links": links,
        "absolute_links": {
            key: _join(base_url, path) for key, path in links.items()
        },
        "repository_docs": [
            "docs/LOCAL_CLIENT_RECIPES.md",
            "docs/CORE_CONTRACTS.md",
            "ARCHITECTURE_RECOMMENDATION.md",
        ],
        "agent_guidance": {
            "start_here": [
                "GET /health without auth.",
                "Fetch this metadata document.",
                "Fetch the azizam-gateway skill if your agent runtime supports skills.",
                "Use /gateway/v1/capabilities with a bearer token before assuming a modality.",
                "Read x-azizam-workflows in /openapi.json for typed workflow inputs, sync/async routes and aliases, then call the route with curl or an OpenAI SDK.",
            ],
            "default_surface": "Use canonical /openai/v1 endpoints for new OpenAI-compatible setup; /v1 remains a convenience route for existing clients.",
            "protocol_aliases": "Use /anthropic/v1 for Anthropic Messages and /ollama/api for Ollama-compatible clients; /api remains an Ollama convenience route.",
            "native_passthrough": "Use /gateway/v1/providers and /gateway/v1/providers/{provider} for policy-projected provider discovery facts. Use /gateway/v1/providers/{provider}/{path...} only when the App profile explicitly allows provider-native native_passthrough. Do not guess root provider prefixes.",
            "ollama_lifecycle": "Ollama create, pull, push, copy, and delete are intentionally unsupported through the protocol frontend.",
            "debugging": "Use /gateway/v1/receipts to inspect accepted, rejected, and ignored workflow controls.",
        },
    }


def build_skill_catalog(*, base_url: str | None = None) -> dict[str, Any]:
    """Return a tiny unauthenticated skill catalog."""
    skill_path = "/.well-known/skills/azizam-gateway/SKILL.md"
    return {
        "schema_version": 1,
        "object": "skill_catalog",
        "skills": [
            {
                "id": "azizam-gateway",
                "name": "azizam-gateway",
                "media_type": "text/markdown",
                "href": skill_path,
                "url": _join(base_url, skill_path),
                "description": "Discover-first instructions for using Azizam safely from agent clients.",
            }
        ],
    }


def build_llms_txt(*, base_url: str | None = None) -> str:
    """Return a compact llms.txt-style discovery document."""
    root = (base_url or "").rstrip("/")
    prefix = f"{root}" if root else ""
    lines = [
        "# Azizam",
        "",
        "Local-first AI gateway with canonical OpenAI, Anthropic, Ollama, and gateway-native discovery surfaces.",
        f"Canonical host: {root}" if root else "Discovery links use the request origin.",
        "",
        "## Discovery",
        f"- Gateway metadata: {prefix}/.well-known/azizam",
        f"- OpenAPI: {prefix}/openapi.json",
        f"- Agent skill: {prefix}/.well-known/skills/azizam-gateway/SKILL.md",
        "",
        "## Auth",
        "- Use Authorization: Bearer $AZIZAM_API_KEY (x-api-key is accepted too, as Anthropic SDKs send it) for all /openai/v1, /anthropic/v1, /ollama/api, /v1, /api, and /gateway/v1 calls except /health.",
        "- Do not ask users to paste raw opk tokens into chat.",
        "",
        "## Preferred Flow",
        "1. GET /health.",
        "2. GET /.well-known/azizam.",
        "3. GET /gateway/v1/capabilities with bearer auth.",
        "4. Use /openai/v1 by default for new setup; /v1 remains available for existing OpenAI-compatible clients.",
        "5. Use /anthropic/v1 or /ollama/api only for clients that require those protocols; /api remains an Ollama convenience route.",
        "6. Use /gateway/v1/providers to discover provider-native lanes; use /gateway/v1/providers/{provider}/{path...} only for policy-gated native_passthrough.",
        "7. Ollama lifecycle routes create/pull/push/copy/delete return unsupported_operation and do not mutate model files.",
        "8. Fetch /gateway/v1/workflows before using extensions.com.azizam.workflow controls.",
        "9. Use /gateway/v1/receipts when a request is rejected or a workflow extension is ignored.",
        "10. Reuse generated images via data[].asset_id or signed /v1/assets/{asset_id}/content URLs when present; asset IDs are scoped to the minting App API key.",
        "",
        "## Native Workflows (image, audio, video generation and editing)",
        f"- {prefix}/openapi.json lists every workflow in the root list `x-azizam-workflows`: typed inputs (`WorkflowInputs_<id>` schemas), aliases, typical seconds, the sync route, the async route, which to prefer, and where each input goes in the request.",
        "- Slow work (video, long music) uses an async route: submit, poll until terminal, then fetch the content. Image, audio and video generation can use POST /gateway/v1/media/jobs; video uses POST /v1/videos.",
        "- Any OpenAPI-aware tool can read this document; for a plain call, build a curl request from the entry's `execution` block (route, content type, and the `fields` map that says where each input goes).",
        "",
        "## Decisions",
        f"- POST {prefix}/v1/systemone or {prefix}/typesafe/v1/systemone: TypeSafe Choice, Score and Noul with calibrated probabilities. Use decide (local fast), decide-quality (opt-in local quality), or jev-* (local alias).",
        f"- TypeSafe SDK: TYPESAFE_BASE_URL={prefix}/typesafe. GET {prefix}/typesafe/v1/models lists permitted decision models.",
        f"- POST {prefix}/v1/decisions is reserved for OpenAI: 501 until a remote forwarding hook is configured; no local schema is guessed.",
        "",
        "## MCP and A1111 adapters",
        f"- MCP (Streamable HTTP, same bearer key): {prefix}/mcp. Three tools: `capabilities` (workflows, aliases, typed inputs), `run`, `job`. Media comes back as signed asset URLs.",
        f"- A1111 /sdapi/v1 subset at host root for SillyTavern's SD WebUI source: {prefix}/sdapi/v1/txt2img, options, sd-models, samplers, schedulers, upscalers, progress, interrupt. Put `user:API_KEY` in its Authentication box.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "PUBLIC_PROTOCOLS",
    "build_public_protocols",
    "build_public_recipes",
    "build_gateway_discovery_metadata",
    "build_llms_txt",
    "build_skill_catalog",
]

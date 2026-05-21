"""Public discovery metadata for agent clients.

This module is deliberately pure: no filesystem, no environment, no secrets.
The live FastAPI app passes a request-derived base URL when it wants absolute
links; tests and docs can use the same builder with a fixed base URL.
"""

from __future__ import annotations

from typing import Any


PUBLIC_PROTOCOLS: tuple[dict[str, Any], ...] = (
    {
        "id": "openai",
        "base_path": "/openai/v1",
        "canonical_base_path": "/openai/v1",
        "convenience_base_path": "/v1",
        "compatibility": "openai-compatible",
        "default": True,
        "base_url_recipe": "{base_url}/openai/v1",
        "streaming": {
            "chat_completions": True,
            "responses": True,
            "images": False,
            "audio": False,
        },
        "endpoints": [
            "/openai/v1/models",
            "/openai/v1/chat/completions",
            "/openai/v1/completions",
            "/openai/v1/embeddings",
            "/openai/v1/responses",
            "/openai/v1/images/generations",
            "/openai/v1/images/edits",
            "/openai/v1/audio/speech",
            "/openai/v1/audio/transcriptions",
            "/openai/v1/audio/translations",
            "/openai/v1/audio/generate",
            "/openai/v1/videos",
            "/v1/models",
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/embeddings",
            "/v1/responses",
            "/v1/images/generations",
            "/v1/images/edits",
            "/v1/audio/speech",
            "/v1/audio/transcriptions",
            "/v1/audio/translations",
            "/v1/audio/generate",
            "/v1/videos",
        ],
        "limits": [
            "audio translations are registered but return an OpenAI-shaped unsupported_audio_translation error unless the local STT backend declares translation support",
        ],
    },
    {
        "id": "anthropic",
        "base_path": "/anthropic/v1",
        "canonical_base_path": "/anthropic/v1",
        "compatibility": "anthropic-messages-subset",
        "default": False,
        "base_url_recipe": "{base_url}/anthropic",
        "streaming": {"messages": True},
        "endpoints": [
            "/anthropic/v1/models",
            "/anthropic/v1/messages",
            "/anthropic/v1/messages/count_tokens",
            "/v1/messages",
            "/v1/messages/count_tokens",
        ],
        "limits": [
            "image blocks are translated to gateway multimodal chat; document/thinking blocks are rejected",
            "/v1/models remains OpenAI-shaped; use /anthropic/v1/models for Anthropic ModelInfoList",
            "count_tokens is a best-effort gateway estimator",
        ],
    },
    {
        "id": "ollama",
        "base_path": "/ollama/api",
        "canonical_base_path": "/ollama/api",
        "convenience_base_path": "/api",
        "compatibility": "ollama-subset",
        "default": False,
        "base_url_recipe": "{base_url}/ollama",
        "streaming": {"chat": True, "generate": True, "embed": False},
        "endpoints": [
            "/ollama/api/tags",
            "/ollama/api/chat",
            "/ollama/api/generate",
            "/ollama/api/embed",
            "/ollama/api/embeddings",
            "/ollama/api/show",
            "/ollama/api/ps",
            "/ollama/api/version",
            "/ollama/api/create",
            "/ollama/api/copy",
            "/ollama/api/pull",
            "/ollama/api/push",
            "/ollama/api/delete",
            "/api/tags",
            "/api/chat",
            "/api/generate",
            "/api/embed",
            "/api/embeddings",
            "/api/show",
            "/api/ps",
            "/api/version",
            "/api/create",
            "/api/copy",
            "/api/pull",
            "/api/push",
            "/api/delete",
        ],
        "limits": [
            "model catalog/status endpoints are projected from the gateway; model mutation endpoints return unsupported_operation",
            "model lifecycle endpoints create/pull/push/copy/delete do not mutate local model files through the gateway",
            "backend-native options without portable meaning are rejected",
        ],
    },
    {
        "id": "gateway",
        "base_path": "/gateway/v1",
        "compatibility": "ai-gateway-native",
        "default": False,
        "base_url_recipe": "{base_url}/gateway/v1",
        "streaming": {},
        "endpoints": [
            "/gateway/v1/capabilities",
            "/gateway/v1/workflows",
            "/gateway/v1/receipts",
            "/gateway/v1/media/jobs",
            "/gateway/v1/assets/{asset_id}",
            "/gateway/v1/providers",
            "/gateway/v1/providers/{provider}",
        ],
    },
    {
        "id": "provider-native",
        "base_path": "/gateway/v1/providers",
        "compatibility": "provider-native-passthrough",
        "default": False,
        "base_url_recipe": "{base_url}/gateway/v1/providers/{provider}",
        "streaming": {},
        "policy": [
            "requires provider-native protocol access",
            "requires native_passthrough enabled for the App profile",
            "provider and operation must be allowed by policy",
            "no root catch-all or legacy root Tabby shim",
            "protocol frontend path collisions stay owned by the frontend; use /gateway/v1/providers/{provider}/{path...} for raw provider-native paths",
        ],
        "endpoints": [
            "/gateway/v1/providers",
            "/gateway/v1/providers/{provider}",
            "/gateway/v1/providers/{provider}/{path...}",
        ],
    },
)


def _join(base_url: str | None, path: str) -> str:
    if not base_url:
        return path
    return f"{base_url.rstrip('/')}{path}"


def _base_url(base_url: str | None) -> str:
    return (base_url or "{base_url}").rstrip("/")


def build_gateway_discovery_metadata(
    *,
    base_url: str | None = None,
    service_name: str = "vivid-fish-ai-gateway",
) -> dict[str, Any]:
    """Return unauthenticated public metadata for agent/client discovery."""
    root = _base_url(base_url)
    links = {
        "health": "/health",
        "login": "/login",
        "user_portal": "/",
        "user_portal_alias": "/me",
        "user_session": "/me/api/session",
        "user_apps": "/me/api/apps",
        "user_app": "/me/api/apps/{app_id}",
        "user_app_keys": "/me/api/apps/{app_id}/keys",
        "user_api_key": "/me/api/credentials/{credential_id}",
        "user_api_key_access": "/me/api/credentials/{credential_id}/access",
        "user_access_request": "/me/api/access-request",
        "openapi": "/openapi.json",
        "llms_txt": "/llms.txt",
        "skill_catalog": "/.well-known/skills",
        "skill": "/.well-known/skills/openai-proxy-gateway/SKILL.md",
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
        "object": "ai_gateway_discovery",
        "service": {
            "name": service_name,
            "product": "Vivid Fish AI Gateway",
            "base_url": base_url.rstrip("/") if base_url else None,
            "canonical_host": "https://ai.vivid.fish",
            "compatibility_aliases": ["https://openai.vivid.fish"],
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
                "/.well-known/ai-gateway",
                "/.well-known/openai-proxy",
                "/.well-known/skills",
                "/llms.txt",
                "/openapi.json",
            ],
            "secret_handling": [
                "Do not ask users to paste raw opk tokens into chat.",
                "Use environment variables or local secret stores.",
                "Do not read or print proxy_api_keys.yml.",
            ],
        },
        "protocols": list(PUBLIC_PROTOCOLS),
        "recipes": {
            "openai": {
                "canonical_base_url": f"{root}/openai/v1",
                "compatibility_base_url": f"{root}/v1",
                "auth": "Authorization: Bearer $OPENAI_PROXY_API_KEY",
                "models_url": f"{root}/openai/v1/models",
                "chat_url": f"{root}/openai/v1/chat/completions",
            },
            "anthropic": {
                "canonical_base_url": f"{root}/anthropic",
                "messages_url": f"{root}/anthropic/v1/messages",
                "auth": "x-api-key or Authorization bearer accepted by SDK/client; store the opk token outside prompts and config checked into git",
                "notes": [
                    "OpenAI-style /v1/messages remains a legacy convenience alias; prefer /anthropic/v1/messages.",
                    "Image, document, thinking, prompt-cache, and beta-header semantics are not represented.",
                ],
            },
            "ollama": {
                "canonical_base_url": f"{root}/ollama",
                "compatibility_base_url": root,
                "tags_url": f"{root}/ollama/api/tags",
                "chat_url": f"{root}/ollama/api/chat",
                "auth": "Authorization: Bearer $OPENAI_PROXY_API_KEY; this is not an unauthenticated local Ollama daemon",
                "unsupported_lifecycle": [
                    "create",
                    "pull",
                    "push",
                    "copy",
                    "delete",
                ],
                "unsupported_lifecycle_note": "Lifecycle routes return unsupported_operation and never mutate local model files.",
            },
            "gateway": {
                "canonical_base_url": f"{root}/gateway/v1",
                "capabilities_url": f"{root}/gateway/v1/capabilities",
                "providers_url": f"{root}/gateway/v1/providers",
                "auth": "Authorization: Bearer $OPENAI_PROXY_API_KEY",
            },
            "provider_native": {
                "discovery_url": f"{root}/gateway/v1/providers",
                "provider_url": f"{root}/gateway/v1/providers/{{provider}}",
                "passthrough_url_template": f"{root}/gateway/v1/providers/{{provider}}/{{path...}}",
                "auth": "Authorization: Bearer $OPENAI_PROXY_API_KEY plus App profile native_passthrough permission",
                "discovery_facts": "Provider rows include caller-projected native_passthrough policy, allowed operations/backends, required gateway auth, non-secret downstream auth classes when known, and live enabled state.",
                "collision_strategy": "Canonical protocol frontends own /openai/v1, /anthropic/v1, /ollama/api, /v1, and /api. Provider-native passthrough is only under /gateway/v1/providers/{provider}/{path...}.",
            },
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
                "Fetch the openai-proxy-gateway skill if your agent runtime supports skills.",
                "Use /gateway/v1/capabilities with a bearer token before assuming a modality.",
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
    skill_path = "/.well-known/skills/openai-proxy-gateway/SKILL.md"
    return {
        "schema_version": 1,
        "object": "skill_catalog",
        "skills": [
            {
                "id": "openai-proxy-gateway",
                "name": "openai-proxy-gateway",
                "media_type": "text/markdown",
                "href": skill_path,
                "url": _join(base_url, skill_path),
                "description": "Discover-first instructions for using Vivid Fish AI Gateway safely from agent clients.",
            }
        ],
    }


def build_llms_txt(*, base_url: str | None = None) -> str:
    """Return a compact llms.txt-style discovery document."""
    root = (base_url or "").rstrip("/")
    prefix = f"{root}" if root else ""
    lines = [
        "# Vivid Fish AI Gateway",
        "",
        "Local-first AI gateway with canonical OpenAI, Anthropic, Ollama, and gateway-native discovery surfaces.",
        "Canonical host: https://ai.vivid.fish (https://openai.vivid.fish remains a compatibility alias).",
        "",
        "## Discovery",
        f"- Gateway metadata: {prefix}/.well-known/ai-gateway",
        f"- Alias metadata: {prefix}/.well-known/openai-proxy",
        f"- OpenAPI: {prefix}/openapi.json",
        f"- Agent skill: {prefix}/.well-known/skills/openai-proxy-gateway/SKILL.md",
        "",
        "## Auth",
        "- Use Authorization: Bearer $AI_GATEWAY_API_KEY (OPENAI_PROXY_API_KEY remains a working alias) for all /openai/v1, /anthropic/v1, /ollama/api, /v1, /api, and /gateway/v1 calls except /health.",
        "- Do not ask users to paste raw opk tokens into chat.",
        "",
        "## Preferred Flow",
        "1. GET /health.",
        "2. GET /.well-known/ai-gateway.",
        "3. GET /gateway/v1/capabilities with bearer auth.",
        "4. Use /openai/v1 by default for new setup; /v1 remains available for existing OpenAI-compatible clients.",
        "5. Use /anthropic/v1 or /ollama/api only for clients that require those protocols; /api remains an Ollama convenience route.",
        "6. Use /gateway/v1/providers to discover provider-native lanes; use /gateway/v1/providers/{provider}/{path...} only for policy-gated native_passthrough; there is no root Tabby shim.",
        "7. Ollama lifecycle routes create/pull/push/copy/delete return unsupported_operation and do not mutate model files.",
        "8. Fetch /gateway/v1/workflows before using extensions.com.vividfish.workflow controls.",
        "9. Use /gateway/v1/receipts when a request is rejected or a workflow extension is ignored.",
        "10. Reuse generated images via data[].asset_id or signed /v1/assets/{asset_id}/content URLs when present; asset IDs are scoped to the minting App API key.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "PUBLIC_PROTOCOLS",
    "build_gateway_discovery_metadata",
    "build_llms_txt",
    "build_skill_catalog",
]

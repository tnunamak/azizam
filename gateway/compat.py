"""Azizam migration contracts. Remove temporary aliases after the live cutover.

Media provenance aliases are permanent: existing assets must remain readable.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

ENV_ALIASES = {'AZIZAM_ADMIN_DB': ['AI_GATEWAY_ADMIN_DB', 'OPENAI_PROXY_ADMIN_DB'],
 'AZIZAM_ADMIN_DB_PATH': ['AI_GATEWAY_ADMIN_DB_PATH'],
 'AZIZAM_ADMIN_KEY': ['AI_GATEWAY_ADMIN_KEY', 'OPENAI_PROXY_ADMIN_KEY', 'VIVID_ADMIN_API_KEY'],
 'AZIZAM_ADMIN_KEY_FILE': ['AI_GATEWAY_ADMIN_KEY_FILE'],
 'AZIZAM_ADMIN_OIDC_ADMIN_GROUPS': ['AI_GATEWAY_ADMIN_OIDC_ADMIN_GROUPS'],
 'AZIZAM_ADMIN_SESSION_KEY': ['AI_GATEWAY_ADMIN_SESSION_KEY', 'OPENAI_PROXY_ADMIN_SESSION_KEY'],
 'AZIZAM_API_KEY': ['AI_GATEWAY_API_KEY',
                    'OPENAI_PROXY_API_KEY',
                    'VIVID_API_KEY',
                    'VIVID_OPENAI_API_KEY'],
 'AZIZAM_AUTO_CREATE_PERSONAL_APP': ['AI_GATEWAY_AUTO_CREATE_PERSONAL_APP'],
 'AZIZAM_BASE': ['AI_GATEWAY_BASE', 'OPENAI_PROXY_BASE'],
 'AZIZAM_BASE_URL': ['AI_GATEWAY_BASE_URL', 'OPENAI_PROXY_BASE_URL', 'VIVID_OPENAI_BASE_URL'],
 'AZIZAM_BRAND': ['AI_GATEWAY_BRAND'],
 'AZIZAM_CONFIG_PATH': ['AI_GATEWAY_CONFIG_PATH'],
 'AZIZAM_DOCKER_GATEWAY': ['AI_GATEWAY_DOCKER_GATEWAY'],
 'AZIZAM_DOCKER_SUBNET': ['AI_GATEWAY_DOCKER_SUBNET'],
 'AZIZAM_DOWNSTREAM_API_KEY': ['AI_GATEWAY_DOWNSTREAM_API_KEY'],
 'AZIZAM_DRAIN_RETRY_AFTER_S': ['AI_GATEWAY_DRAIN_RETRY_AFTER_S'],
 'AZIZAM_GRACEFUL_SHUTDOWN_S': ['AI_GATEWAY_GRACEFUL_SHUTDOWN_S'],
 'AZIZAM_INTERNAL_LITELLM_KEY': ['AI_GATEWAY_INTERNAL_LITELLM_KEY'],
 'AZIZAM_LITELLM': ['AI_GATEWAY_LITELLM'],
 'AZIZAM_LITELLM_API_KEY': ['AI_GATEWAY_LITELLM_API_KEY'],
 'AZIZAM_LITELLM_BASE_URL': ['AI_GATEWAY_LITELLM_BASE_URL'],
 'AZIZAM_LITELLM_CONFIG_PATH': ['AI_GATEWAY_LITELLM_CONFIG_PATH'],
 'AZIZAM_LITELLM_PID_FILE': ['AI_GATEWAY_LITELLM_PID_FILE'],
 'AZIZAM_LITELLM_RELOAD_INTERVAL': ['AI_GATEWAY_LITELLM_RELOAD_INTERVAL'],
 'AZIZAM_MANAGE_LITELLM': ['AI_GATEWAY_MANAGE_LITELLM'],
 'AZIZAM_MEDIA_JOBS_DB': ['AI_GATEWAY_MEDIA_JOBS_DB'],
 'AZIZAM_METRICS_DB': ['AI_GATEWAY_METRICS_DB'],
 'AZIZAM_OIDC_CLIENT_SECRET': ['AI_GATEWAY_OIDC_CLIENT_SECRET'],
 'AZIZAM_PRISMA_PREFLIGHT_TIMEOUT': ['AI_GATEWAY_PRISMA_PREFLIGHT_TIMEOUT'],
 'AZIZAM_PROVIDER_LEDGER_DB': ['AI_GATEWAY_PROVIDER_LEDGER_DB'],
 'AZIZAM_RECEIPTS_DB': ['AI_GATEWAY_RECEIPTS_DB'],
 'AZIZAM_REQUIREMENTS_FILE': ['AI_GATEWAY_REQUIREMENTS_FILE'],
 'AZIZAM_REQUIREMENTS_HASHES': ['AI_GATEWAY_REQUIREMENTS_HASHES'],
 'AZIZAM_RESPONSES_DB': ['AI_GATEWAY_RESPONSES_DB'],
 'AZIZAM_SMOKE_KEY': ['AI_GATEWAY_SMOKE_KEY'],
 'AZIZAM_TEST_SUBSTRATE_BASE_URL': ['AI_GATEWAY_TEST_SUBSTRATE_BASE_URL'],
 'AZIZAM_URL': ['AI_GATEWAY_URL'],
 'AZIZAM_USER_SELF_SERVICE': ['AI_GATEWAY_USER_SELF_SERVICE'],
 'AZIZAM_WORDMARK': ['AI_GATEWAY_WORDMARK'],
 'AZIZAM_ADMIN_TOKEN': ['OPENAI_PROXY_ADMIN_TOKEN'],
 'AZIZAM_AUTH_ENABLED': ['OPENAI_PROXY_AUTH_ENABLED'],
 'AZIZAM_CF_MODE': ['OPENAI_PROXY_CF_MODE'],
 'AZIZAM_KEY': ['OPENAI_PROXY_KEY'],
 'AZIZAM_KEY_ACTIVITY_LOG': ['OPENAI_PROXY_KEY_ACTIVITY_LOG'],
 'AZIZAM_LAN_BYPASS': ['OPENAI_PROXY_LAN_BYPASS'],
 'AZIZAM_MODEL_COMPAT_ENABLED': ['OPENAI_PROXY_MODEL_COMPAT_ENABLED'],
 'AZIZAM_PORT': ['OPENAI_PROXY_PORT'],
 'AZIZAM_PUBLIC_URL': ['OPENAI_PROXY_PUBLIC_URL'],
 'AZIZAM_QUERY_PARAM_KEY': ['OPENAI_PROXY_QUERY_PARAM_KEY'],
 'AZIZAM_SETUP_ALLOWED_CIDRS': ['OPENAI_PROXY_SETUP_ALLOWED_CIDRS'],
 'AZIZAM_SHOWCASE_EXAMPLES_DIR': ['OPENAI_PROXY_SHOWCASE_EXAMPLES_DIR'],
 'AZIZAM_SMOKE_ANTHROPIC_MODEL': ['OPENAI_PROXY_SMOKE_ANTHROPIC_MODEL'],
 'AZIZAM_SMOKE_AUDIO_SPEECH': ['OPENAI_PROXY_SMOKE_AUDIO_SPEECH'],
 'AZIZAM_SMOKE_AUDIO_SPEECH_MODEL': ['OPENAI_PROXY_SMOKE_AUDIO_SPEECH_MODEL'],
 'AZIZAM_SMOKE_AUDIO_TRANSCRIPTION': ['OPENAI_PROXY_SMOKE_AUDIO_TRANSCRIPTION'],
 'AZIZAM_SMOKE_AUDIO_TRANSCRIPTION_MODEL': ['OPENAI_PROXY_SMOKE_AUDIO_TRANSCRIPTION_MODEL'],
 'AZIZAM_SMOKE_AUDIO_TRANSLATION': ['OPENAI_PROXY_SMOKE_AUDIO_TRANSLATION'],
 'AZIZAM_SMOKE_AUDIO_TRANSLATION_MODEL': ['OPENAI_PROXY_SMOKE_AUDIO_TRANSLATION_MODEL'],
 'AZIZAM_SMOKE_AUDIO_VOICE': ['OPENAI_PROXY_SMOKE_AUDIO_VOICE'],
 'AZIZAM_SMOKE_EMBEDDINGS_MODEL': ['OPENAI_PROXY_SMOKE_EMBEDDINGS_MODEL'],
 'AZIZAM_SMOKE_IMAGES': ['OPENAI_PROXY_SMOKE_IMAGES'],
 'AZIZAM_SMOKE_IMAGE_MODEL': ['OPENAI_PROXY_SMOKE_IMAGE_MODEL'],
 'AZIZAM_SMOKE_IMAGE_SIZE': ['OPENAI_PROXY_SMOKE_IMAGE_SIZE'],
 'AZIZAM_SMOKE_LIVE_EMBEDDINGS': ['OPENAI_PROXY_SMOKE_LIVE_EMBEDDINGS'],
 'AZIZAM_SMOKE_LIVE_LLM': ['OPENAI_PROXY_SMOKE_LIVE_LLM'],
 'AZIZAM_SMOKE_LIVE_TOOLS': ['OPENAI_PROXY_SMOKE_LIVE_TOOLS'],
 'AZIZAM_SMOKE_LIVE_VISION': ['OPENAI_PROXY_SMOKE_LIVE_VISION'],
 'AZIZAM_SMOKE_MODEL': ['OPENAI_PROXY_SMOKE_MODEL'],
 'AZIZAM_SMOKE_OLLAMA_EMBED_MODEL': ['OPENAI_PROXY_SMOKE_OLLAMA_EMBED_MODEL'],
 'AZIZAM_SMOKE_OLLAMA_MODEL': ['OPENAI_PROXY_SMOKE_OLLAMA_MODEL'],
 'AZIZAM_SMOKE_OPENAI_MODEL': ['OPENAI_PROXY_SMOKE_OPENAI_MODEL'],
 'AZIZAM_SMOKE_RESPONSES': ['OPENAI_PROXY_SMOKE_RESPONSES'],
 'AZIZAM_SMOKE_RESPONSES_MODEL': ['OPENAI_PROXY_SMOKE_RESPONSES_MODEL'],
 'AZIZAM_SMOKE_TIMEOUT': ['OPENAI_PROXY_SMOKE_TIMEOUT'],
 'AZIZAM_SMOKE_VIDEO': ['OPENAI_PROXY_SMOKE_VIDEO'],
 'AZIZAM_SMOKE_VIDEO_MODEL': ['OPENAI_PROXY_SMOKE_VIDEO_MODEL'],
 'AZIZAM_SMOKE_VISION_MODEL': ['OPENAI_PROXY_SMOKE_VISION_MODEL'],
 'AZIZAM_TIMEOUT_S': ['OPENAI_PROXY_TIMEOUT_S']}


def get_env(name: str, default: Any = None, *, environ: Mapping[str, str] | None = None) -> Any:
    """Read the first nonempty new/legacy value; empty strings are unset."""
    env = os.environ if environ is None else environ
    canonical = next((new for new, old in ENV_ALIASES.items() if name in old), name)
    for candidate in (canonical, *ENV_ALIASES.get(canonical, ())):
        if env.get(candidate):
            return env[candidate]
    return default


# Only gateway-owned field names. Call these helpers at their producers/consumers,
# never on an arbitrary request, provider payload, schema or metadata tree.
JSON_ALIASES = {
    'vivid_fish_alias': 'azizam_alias',
    'vivid_fish_alias_errors': 'azizam_alias_errors',
    'vivid_fish_backend': 'azizam_backend',
    'vivid_fish_catalog_warning': 'azizam_catalog_warning',
    'vivid_fish_current_loaded': 'azizam_current_loaded',
    'vivid_fish_current_loaded_fast': 'azizam_current_loaded_fast',
    'vivid_fish_current_model_ids': 'azizam_current_model_ids',
    'vivid_fish_engine_candidates': 'azizam_engine_candidates',
    'vivid_fish_intent': 'azizam_intent',
    'vivid_fish_loaded': 'azizam_loaded',
    'vivid_fish_manifest_version': 'azizam_manifest_version',
    'vivid_fish_model': 'azizam_model',
    'vivid_fish_model_access': 'azizam_model_access',
    'vivid_fish_model_path': 'azizam_model_path',
    'vivid_fish_profile_model_allowlist': 'azizam_profile_model_allowlist',
    'vivid_fish_resolved_model': 'azizam_resolved_model',
    'vivid_fish_resolves_to': 'azizam_resolves_to',
    'vivid_fish_role': 'azizam_role',
    'vivid_fish_target_activation': 'azizam_target_activation',
    'vivid_fish_target_visibility': 'azizam_target_visibility',
    'vivid_fish_warnings': 'azizam_warnings',
    'vivid_fish_workflow': 'azizam_workflow',
    'vivid_fish_workflow_id': 'azizam_workflow_id',
    'x-vividfish-asset': 'x-azizam-asset',
    'x-vividfish-workflow-id': 'x-azizam-workflow-id',
    'x-vividfish-workflows': 'x-azizam-workflows',
    'x_ai_gateway_llm_execution': 'x_azizam_llm_execution',
    'x-vividfish-sync': 'x-azizam-sync',
    'x-vividfish-async': 'x-azizam-async',
    'vivid_object': 'azizam_object',
    'vivid_status': 'azizam_status',
    'openai_proxy_policy': 'azizam_policy',
    'com.vividfish.workflow': 'com.azizam.workflow',
}
JSON_OLD_NAMES = {new: old for old, new in JSON_ALIASES.items()}


def canonical_key(key: Any) -> Any:
    return JSON_ALIASES.get(key, key)


def read_field(value: Mapping[str, Any], name: str, default: Any = None) -> Any:
    """Read one gateway-owned field; an explicitly present new key wins."""
    new = canonical_key(name)
    if new in value:
        return value[new]
    return value.get(JSON_OLD_NAMES.get(new, new), default)


def accept_aliases(value: Any) -> Any:
    """Add canonical names on ONE gateway-owned object, preserving both values."""
    if not isinstance(value, Mapping):
        return value
    result = dict(value)
    for key, item in value.items():
        new = canonical_key(key)
        if new not in result:
            result[new] = item
    return result


def emit_aliases(value: Mapping[str, Any]) -> dict[str, Any]:
    """Add temporary aliases on ONE gateway-owned object in O(n); never recurse.

    Existing keys, including collisions, keep their original values. The
    workflow namespace is accepted on input but generated with the new name.
    """
    result = dict(value)
    for key, item in value.items():
        if key == "com.azizam.workflow" or key == "com.vividfish.workflow":
            continue
        alias = JSON_OLD_NAMES.get(key) or JSON_ALIASES.get(key)
        if alias is not None and alias not in result:
            result[alias] = item
    return result


def emit_model_list_aliases(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The /v1/models contract owns its envelope and each data entry only."""
    result = emit_aliases(envelope)
    if isinstance(envelope.get("data"), list):
        result["data"] = [emit_aliases(entry) if isinstance(entry, Mapping) else entry
                          for entry in envelope["data"]]
    return result


HEADER_ALIASES = {'x-azizam-admin-action': ('x-ai-gateway-admin-action',),
 'x-azizam-backend': ('x-ai-gateway-backend', 'x-gateway-backend'),
 'x-azizam-cost-usd': ('x-gateway-cost-usd',),
 'x-azizam-csrf': ('x-ai-gateway-csrf',),
 'x-azizam-draining': ('x-gateway-draining',),
 'x-azizam-engine': ('x-gateway-engine', 'x-vivid-engine'),
 'x-azizam-execution': ('x-ai-gateway-execution',),
 'x-azizam-fallback': ('x-gateway-fallback', 'x-vivid-fallback'),
 'x-azizam-fallback-reasons': ('x-gateway-fallback-reasons',),
 'x-azizam-ignored-params': ('x-gateway-ignored-params',),
 'x-azizam-internal-protocol-forward': ('x-vivid-fish-internal-protocol-forward',),
 'x-azizam-key-prefix': ('x-ai-gateway-key-prefix',),
 'x-azizam-llm-execution': ('x-ai-gateway-llm-execution',),
 'x-azizam-profile': ('x-ai-gateway-profile',),
 'x-azizam-provider': ('x-gateway-provider',),
 'x-azizam-receipt-id': ('x-ai-gateway-receipt-id',),
 'x-azizam-remote': ('x-gateway-remote',),
 'x-azizam-request-id': ('x-ai-gateway-request-id', 'x-gateway-request-id'),
 'x-azizam-resolved-model': ('x-ai-gateway-resolved-model',),
 'x-azizam-service-id': ('x-ai-gateway-service-id',),
 'x-azizam-size-adjusted': ('x-vivid-size-adjusted',),
 'x-azizam-token-counting': ('x-ai-gateway-token-counting',),
 'x-azizam-toolcall-advisory': ('x-gateway-toolcall-advisory',),
 'x-azizam-user-action': ('x-ai-gateway-user-action',)}

def header_names(name: str) -> tuple[str, ...]:
    if name in HEADER_ALIASES:
        return (name, *HEADER_ALIASES[name])
    if name.startswith("x-azizam-"):
        return (name, "x-ai-gateway-" + name[len("x-azizam-"):])
    return (name,)


def canonical_header_name(name: str) -> str:
    name = name.lower()
    for new, old in HEADER_ALIASES.items():
        if name in old:
            return new
    for prefix in ("x-ai-gateway-", "x-gateway-"):
        if name.startswith(prefix):
            return "x-azizam-" + name[len(prefix):]
    return name


def normalize_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    result = list(headers)
    present = {key.lower() for key, _ in headers}
    for key, value in headers:
        encoded = canonical_header_name(key.decode("latin1")).encode("latin1")
        if encoded not in present:
            result.append((encoded, value))
            present.add(encoded)
    return result


def emit_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    """Emit equal old/new headers, replacing any stale upstream old value."""
    normalized = normalize_headers(headers)
    result = []
    for key, value in normalized:
        name = key.decode("latin1").lower()
        if canonical_header_name(name) != name:
            continue
        result.append((key, value))
        for alias in header_names(name)[1:]:
            result.append((alias.encode("latin1"), value))
    return result


def emit_header_mapping(headers: Mapping[str, str]) -> dict[str, str]:
    return {key.decode("latin1"): value.decode("latin1") for key, value in
            emit_headers([(k.encode("latin1"), v.encode("latin1")) for k, v in headers.items()])}


class ProductCompatibilityMiddleware:
    """Header compatibility only. Request/response body bytes pass through."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        scope = dict(scope, headers=normalize_headers(scope.get("headers", [])))

        async def compatible_send(message):
            if message["type"] == "http.response.start":
                message = dict(message, headers=emit_headers(list(message.get("headers", []))))
            await send(message)

        return await self.app(scope, receive, compatible_send)

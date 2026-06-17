#!/usr/bin/env python3
"""
Vivid Fish AI Gateway - Unified OpenAI-compatible API for multiple backends.
Served externally at ai.vivid.fish (Traefik on 192.168.1.4 → 192.168.1.180:5000).

Routes requests to:
- TabbyAPI (Qwen3-VL) for chat/completions/vision
- TTS backend (Voxtral-4B via vLLM) for /v1/audio/speech
- Future: llama.cpp for tool calling

Features:
- OpenAI-compatible API on single port
- Idle unload/reload for VRAM management
- Health monitoring and auto-restart
- TTS passthrough to external TTS service
"""

import asyncio
import base64
import copy
import json
import math
import os
import random
import signal
import sys
import subprocess
import tempfile
import threading
import time
import atexit
import hashlib
import hmac
import ipaddress
import secrets
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
import contextlib
from contextlib import asynccontextmanager
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import parse_qs, parse_qsl, quote, urlencode

import httpx
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Local: status dashboard backend.
import status_endpoint
import firehose_metrics
import metrics_store
import metrics_sampler
import responses_store
import responses_translator
from gateway import (
    admin_store,
    audio_media_jobs,
    default_profiles,
    media_jobs,
    receipt_store,
    resource_admission_http,
    route_registry,
    service_registry,
    video_media_job_http,
    video_media_jobs,
)
from gateway.admin_api import (
    AdminAuthError,
    AdminContext,
    create_admin_router,
    derive_admin_attention,
)
from gateway.admin_catalog import admin_api_surfaces, profile_policy_catalog
from gateway.config_sources import KNOWN_ENV_SOURCE_NAMES, inventory_config_sources
from gateway.admin_oidc import (
    IDTokenClaimsError,
    OIDCConfigError,
    PKCEPair,
    SessionError,
    StateRecord,
    access_names_from_oidc_claims,
    admin_role_from_oidc_claims,
    authorization_url,
    bootstrap_admin_role,
    consume_state,
    csrf_token_for_session,
    decode_session_cookie,
    encode_session_cookie,
    generate_nonce,
    generate_pkce_pair,
    load_oidc_config,
    record_state,
    session_from_claims,
    validate_id_token_claims,
)
from gateway.admin_render import render_admin_dashboard
from gateway.local_owner import (
    hash_password as local_hash_password,
    load_or_create_signing_key as local_load_signing_key,
    verify_password as local_verify_password,
)
from gateway.setup_render import render_setup_page
from gateway.admin_runtime import (
    AdminAuthenticator,
    AdminSQLiteStore,
    USER_ACCESS_DEFAULTS_SETTING_KEY,
    create_admin_service_account_key,
    runtime_identity_for_app_credential_hash,
)
from gateway.api import create_gateway_router
from gateway.user_render import render_user_portal
from gateway.user_runtime import (
    UserPortalError,
    build_user_activity_snapshot,
    build_user_portal_snapshot,
    create_user_app,
    delete_user_app,
    get_user_app_credential,
    get_user_receipt,
    mint_user_app_key,
    request_user_access,
    revoke_user_app_key,
)
from gateway.audio_extensions import (
    bind_audio_workflow_extensions,
    detect_unknown_audio_fields,
)
from gateway.backends.chat import build_chat_loopback_request
from gateway.backends.embeddings import build_embeddings_loopback_request
from gateway.backends.llm_execution import (
    LITELLM_BODY_EXECUTION_PATHS,
    LlmExecutionRequest,
    litellm_headers,
    litellm_runtime_from_registry,
    litellm_url,
    send_llm_execution_request,
)
from gateway.capabilities import (
    Capability,
    ResourceEstimate,
    capability_from_workflow_record,
    capability_with_policy_view,
    summarize_capabilities,
)
from gateway.chat_extensions import (
    validate_chat_completions_extensions,
    validate_responses_extensions,
)
from gateway.comfyui_workflow_validation import validate_comfyui_workflow_graph
from gateway.transforms import (
    REGISTRY as _TRANSFORM_REGISTRY,
    TransformContext as _TransformContext,
    run_request_pipeline as _run_request_pipeline,
    run_response_pipeline as _run_response_pipeline,
    ReasoningEffortTransform as _ReasoningEffortTransform,
    QWEN3_ENABLE_THINKING_KEY as _QWEN3_ENABLE_THINKING_KEY,
    normalize_chat_completion_payload as _normalize_chat_completion_payload,
    ValueMapTransform as _ValueMapTransform,
)
from gateway.toolcall_detection import (
    detect_unlifted_toolcalls as _detect_unlifted_toolcalls,
    advisory_for_backend as _toolcall_advisory_for_backend,
    request_had_tools as _request_had_tools,
    normalize_toolcall_args as _normalize_toolcall_args,
    normalize_toolcall_finish_reason as _normalize_toolcall_finish_reason,
    ToolcallStreamDetector as _ToolcallStreamDetector,
)
from gateway.discovery import WorkflowRegistry
from gateway.discovery_metadata import (
    build_gateway_discovery_metadata,
    build_llms_txt,
    build_skill_catalog,
)
from gateway.local_capabilities import (
    INTENT_AUDIO_GENERATE,
    INTENT_SPEECH,
    INTENT_TRANSCRIPTIONS,
    INTENT_TRANSLATIONS,
    INTENT_VIDEO_GENERATE,
    build_local_capabilities,
)
from gateway.extensions import ExtensionError
from gateway.image_extensions import (
    STANDARD_IMAGE_EDIT_FIELDS,
    STANDARD_IMAGE_GENERATE_FIELDS,
    WORKFLOW_EXTENSION_NAMESPACE,
    bind_workflow_extensions,
    detect_unknown_top_level_fields,
    record_image_extension_binding,
)
from gateway.litellm_substrate import write_litellm_config_artifact
from gateway.manifests import (
    ManifestError,
    bind_backend_request,
    load_workflow_manifest,
    manifest_identity,
)
from gateway.video_extensions import (
    STANDARD_VIDEO_CREATE_FIELDS,
    bind_video_workflow_extensions,
)
from gateway.openai_media_schemas import (
    AUDIO_GENERATE_OPENAPI,
    AUDIO_MODELS_OPENAPI,
    AUDIO_SPEECH_OPENAPI,
    AUDIO_TRANSCRIPTIONS_OPENAPI,
    AUDIO_TRANSLATIONS_OPENAPI,
    IMAGES_EDITS_OPENAPI,
    IMAGES_GENERATIONS_OPENAPI,
    IMAGES_VARIATIONS_OPENAPI,
    SIGNED_IMAGE_CONTENT_OPENAPI,
    VIDEOS_CONTENT_OPENAPI,
    VIDEOS_CREATE_OPENAPI,
    VIDEOS_STATUS_OPENAPI,
    VOICES_LIST_OPENAPI,
)
from gateway.policy import DEFAULT_PROFILE, DENIED, PolicyDecision
from gateway.receipts import RouteReceipt
from gateway.runtime_auth import (
    RuntimeIdentity,
    discovery_audience_for_profile,
    evaluate_and_apply,
    hash_api_key as runtime_hash_api_key,
    make_policy_request,
    normalize_key_hash as runtime_normalize_key_hash,
)
from gateway.protocols import anthropic as anthropic_protocol
from gateway.protocols import ollama as ollama_protocol

# =============================================================================
# Configuration
# =============================================================================

_default_config = Path(__file__).parent / "proxy_config.yml"
CONFIG_PATH = Path(os.environ["AI_GATEWAY_CONFIG_PATH"]) if os.environ.get("AI_GATEWAY_CONFIG_PATH") else _default_config


def load_config() -> dict:
    """Load configuration from YAML file."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    return {}


config = load_config()
# Preserve the raw YAML config so reload_runtime_config() can re-apply the
# service-registry overlay without re-reading the file.
_bootstrap_config: dict = dict(config)


def _configured_admin_db_path(admin_config: Mapping[str, Any] | None = None) -> Path:
    admin_config = admin_config or {}
    raw = (
        os.environ.get("AI_GATEWAY_ADMIN_DB")
        or os.environ.get("OPENAI_PROXY_ADMIN_DB")
        or admin_config.get("db_path")
        or "data/admin.db"
    )
    path = Path(str(raw))
    if path.is_absolute():
        return path
    return Path(__file__).parent / path


auth_cfg = config.get("auth", {})
admin_cfg = config.get("admin", {}) or {}
user_defaults_cfg = admin_cfg.get("user_defaults") or {}
if not isinstance(user_defaults_cfg, Mapping):
    user_defaults_cfg = {}

ADMIN_DB_PATH = _configured_admin_db_path(admin_cfg)


def _load_effective_runtime_config(raw_config: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay DB-owned service registry onto bootstrap config."""

    try:
        admin_store.init_db(ADMIN_DB_PATH)
        registry = service_registry.ensure_service_registry(
            raw_config,
            path=ADMIN_DB_PATH,
            actor="startup",
        )
        _write_litellm_config_artifact(registry)
        return service_registry.apply_service_registry_to_config(raw_config, registry)
    except Exception as e:  # noqa: BLE001
        print(
            "[proxy] WARNING: service registry unavailable; "
            f"falling back to bootstrap config for this process: {e}",
            flush=True,
        )
        return copy.deepcopy(dict(raw_config))


def _write_litellm_config_artifact(registry: Mapping[str, Any]) -> None:
    try:
        path = write_litellm_config_artifact(
            registry,
            # Anchor the artifact to the gateway's state dir (next to admin.db)
            # so it honors AI_GATEWAY_ADMIN_DB isolation instead of always
            # landing in the repo data/ directory.
            base_dir=ADMIN_DB_PATH.parent,
        )
        if __name__ == "__main__":
            print(f"[proxy] LiteLLM config generated at {path}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] WARNING: LiteLLM config generation failed: {e}", flush=True)


config = _load_effective_runtime_config(config)
auth_cfg = config.get("auth", {})
admin_cfg = config.get("admin", {}) or {}
user_defaults_cfg = admin_cfg.get("user_defaults") or {}
if not isinstance(user_defaults_cfg, Mapping):
    user_defaults_cfg = {}


def bool_from_config(value, default: bool) -> bool:
    """Parse permissive bool config without making YAML null mean false."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    return bool(value)


# Proxy settings
PROXY_HOST = config.get("proxy", {}).get("host", "127.0.0.1")
PROXY_PORT = int(os.environ.get("OPENAI_PROXY_PORT", os.environ.get("LLM_PROXY_PORT", config.get("proxy", {}).get("port", 5000))))

# TabbyAPI backend
tabby_cfg = config.get("backends", {}).get("tabby", {})
TABBY_HOST = f"http://{tabby_cfg.get('host', '127.0.0.1')}:{tabby_cfg.get('port', 5050)}"
TABBY_HEALTH_ENDPOINT = tabby_cfg.get("health_endpoint", "/health")
TABBY_LOAD_TIMEOUT = tabby_cfg.get("load_timeout", 300)
TABBY_IDLE_TIMEOUT = int(os.environ.get("TABBY_IDLE_TIMEOUT", tabby_cfg.get("idle_timeout", 300)))
# The gateway never owns an engine's process existence (the binding principle:
# it manages running and routing, never existence). Tabby's lifecycle —
# start/stop/crash-restart — belongs to the operator's supervisor: a systemd
# unit, a docker container, or a `command` LifecycleDriver. There is no
# gateway-spawns-tabby mode anymore; `managed` is retained only as a
# read-back-compatible no-op so an old config with `managed: true` still loads
# (it simply has no effect — the gateway will not spawn a subprocess).
TABBY_MANAGED = False  # process ownership removed; see CommandDriver / systemd

# KoboldCpp backend
kobold_cfg = config.get("backends", {}).get("kobold", {})
KOBOLD_HOST = f"http://{kobold_cfg.get('host', '127.0.0.1')}:{kobold_cfg.get('port', 5051)}"
KOBOLD_HEALTH_ENDPOINT = kobold_cfg.get("health_endpoint", "/api/v1/model")
KOBOLD_SERVICE = kobold_cfg.get("service_name", "koboldcpp.service")

def _is_chat_capable_native_backend(backend_id: str, backend_cfg: Mapping) -> bool:
    """Return True if this backend belongs in LLM_BACKEND_HOSTS.

    Criteria (all must hold):
    - enabled (default True when absent)
    - has at least one of the capabilities: chat, completions
    - is NOT the LiteLLM execution substrate (that has no host to proxy to)

    The LiteLLM substrate is identified by execution_substrate == "litellm" or
    kind in {litellm, llm-substrate} — same test as service_registry.is_litellm_service_record.
    """
    if not bool_from_config(backend_cfg.get("enabled"), True):
        return False
    caps = backend_cfg.get("capabilities") or []
    if isinstance(caps, (str, bytes)):
        caps = [caps]
    cap_set = {str(c).strip().lower() for c in caps if str(c).strip()}
    if not (cap_set & {"chat", "completions"}):
        return False
    # Exclude the LiteLLM managed substrate — it has no direct host to proxy to.
    execution_substrate = str(backend_cfg.get("execution_substrate") or "").strip().lower()
    kind = str(backend_cfg.get("kind") or "").strip().lower()
    if (
        backend_id.strip().lower() == "litellm"
        or execution_substrate == "litellm"
        or kind in {"litellm", "llm-substrate"}
    ):
        return False
    return True


def _build_llm_backend_hosts() -> dict[str, str]:
    """Derive the {id: "http://host:port"} map of chat-capable native backends.

    Reads config["backends"] (a boot-time projection of the DB registry) so that
    any service added via the admin UI and marked chat-capable is automatically
    included after a restart. The tabby/kobold entries come out identical to the
    previous hard-coded dict for the current deployment.
    """
    result: dict[str, str] = {}
    for bid, bcfg in (config.get("backends") or {}).items():
        if not isinstance(bcfg, Mapping):
            continue
        if not _is_chat_capable_native_backend(bid, bcfg):
            continue
        host = bcfg.get("host", "127.0.0.1")
        port = bcfg.get("port")
        if not port:
            continue
        result[bid] = f"http://{host}:{port}"
    return result


LLM_BACKEND_HOSTS: dict[str, str] = _build_llm_backend_hosts()


def configured_backend_capabilities(backend: str) -> set[str]:
    raw = (config.get("backends") or {}).get(backend, {})
    if not isinstance(raw, Mapping):
        return set()
    capabilities = raw.get("capabilities") or ()
    if isinstance(capabilities, (str, bytes)):
        return {str(capabilities).strip().lower()}
    return {
        str(capability).strip().lower()
        for capability in capabilities
        if str(capability).strip()
    }


def backend_supports(backend: str, capability: str) -> bool:
    return capability.lower() in configured_backend_capabilities(backend)


def profile_allows_backend(auth_context: "AuthContext", backend: str) -> bool:
    profile = getattr(getattr(auth_context, "identity", None), "profile", None)
    allowed = getattr(profile, "backends", None)
    return allowed is None or backend in allowed


def backend_for_host(host: str) -> Optional[str]:
    for backend, backend_host in LLM_BACKEND_HOSTS.items():
        if host == backend_host:
            return backend
    # Registry-driven fallback: check if the host matches the highest-priority
    # backend for the embeddings capability (replaces the hardcoded EMBEDDINGS_HOST
    # pin so a second embeddings backend is resolved correctly).
    _embeddings_host = host_for_capability("embeddings")
    if _embeddings_host and host == _embeddings_host:
        backends = backends_for_capability("embeddings")
        return backends[0] if backends else None
    # Legacy fallback: if capability resolution yields nothing but EMBEDDINGS_HOST
    # is set and enabled, preserve the old behavior.
    if EMBEDDINGS_ENABLED and host == EMBEDDINGS_HOST:
        return "embeddings"
    # Unknown host: no match.  The caller (catch-all route) uses a separate
    # registry-aware fallback rather than depending on this returning a value.
    return None


def _highest_priority_chat_backend() -> Optional[str]:
    """Return the id of the highest-priority enabled chat-capable native backend.

    Priority follows the registry convention: lower numeric value = higher priority
    (same ordering used by list_resource_summaries). Returns None when
    LLM_BACKEND_HOSTS is empty.
    """
    backends = config.get("backends") or {}
    candidates = []
    for bid in LLM_BACKEND_HOSTS:
        bcfg = backends.get(bid)
        if not isinstance(bcfg, Mapping):
            continue
        priority = int(bcfg.get("priority") or 100)
        candidates.append((priority, bid))
    if not candidates:
        return None
    candidates.sort()  # ascending: lowest priority number wins
    return candidates[0][1]


def vision_capable_llm_backends() -> list[str]:
    """Return ids of vision-capable backends in LLM_BACKEND_HOSTS, ordered by priority.

    A backend is vision-capable if it is in LLM_BACKEND_HOSTS (enabled + chat/completions
    capable) AND its capabilities include "vision".  The kobold mmproj check is NOT done
    here — that is orchestration-level logic handled at the call site.
    """
    backends = config.get("backends") or {}
    result = []
    for bid in LLM_BACKEND_HOSTS:
        bcfg = backends.get(bid)
        if not isinstance(bcfg, Mapping):
            continue
        caps = {str(c).strip().lower() for c in (bcfg.get("capabilities") or []) if str(c).strip()}
        if "vision" not in caps:
            continue
        priority = int(bcfg.get("priority") or 100)
        result.append((priority, bid))
    result.sort()
    return [bid for _, bid in result]


def backends_for_capability(capability: str) -> list[str]:
    """Return ids of enabled backends that declare *capability*, ordered by priority (asc).

    Mirrors the §2.1 resolution algorithm: scan config["backends"], keep every
    backend that is enabled and whose capabilities list includes the requested
    capability string (case-insensitive).  Priority follows the registry
    convention: lower numeric value = higher priority (same as
    _highest_priority_chat_backend).  Backends with no explicit priority default
    to 100.

    This is the capability-dispatch analogue of vision_capable_llm_backends():
    it is NOT limited to LLM_BACKEND_HOSTS — it covers ALL backends (TTS, STT,
    embeddings, etc.) so that /v1/audio/speech, /v1/audio/transcriptions, and
    /v1/embeddings can resolve their host from the registry rather than from a
    hardcoded constant.

    Compound capability matching: a capability like "audio.speech" matches if
    EITHER the full compound string "audio.speech" is in the backend's caps OR
    all of its dot-separated parts ("audio" AND "speech") are individually
    present.  This allows the §2.1 vocabulary (audio.speech, audio.transcriptions,
    audio.translations, embeddings) to work with existing config entries that
    express the same semantics as separate strings (e.g. ["audio", "speech"]).
    Simple (non-compound) capabilities like "embeddings" or "vision" must appear
    verbatim in the caps list.
    """
    cap = capability.strip().lower()
    parts = cap.split(".")
    compound = len(parts) > 1
    backends = config.get("backends") or {}
    result: list[tuple[int, str]] = []
    for bid, bcfg in backends.items():
        if not isinstance(bcfg, Mapping):
            continue
        if not bool_from_config(bcfg.get("enabled"), True):
            continue
        caps = {str(c).strip().lower() for c in (bcfg.get("capabilities") or []) if str(c).strip()}
        # Match either the full compound string or all component parts.
        if compound:
            matched = cap in caps or all(p in caps for p in parts)
        else:
            matched = cap in caps
        if not matched:
            continue
        priority = int(bcfg.get("priority") or 100)
        result.append((priority, bid))
    result.sort()
    return [bid for _, bid in result]


def host_for_capability(capability: str) -> str | None:
    """Return the host URL of the highest-priority enabled backend for *capability*.

    Returns the first backend's ``http://host:port`` from backends_for_capability,
    or None if no enabled backend declares the capability.  Health-checking is
    out of scope for this step; the caller should fall back to the legacy constant
    when None is returned.
    """
    backends = config.get("backends") or {}
    for bid in backends_for_capability(capability):
        bcfg = backends.get(bid)
        if not isinstance(bcfg, Mapping):
            continue
        host = bcfg.get("host", "127.0.0.1")
        port = bcfg.get("port")
        if not port:
            continue
        return f"http://{host}:{port}"
    return None


# Model routing - auto-detect backend from filesystem
routing_cfg = config.get("model_routing", {})
MODEL_DIR = routing_cfg.get("model_dir", "/media/windows/AI Models/LLMs")
GGUF_DIR = routing_cfg.get("gguf_dir", "/media/windows/AI Models/LLMs/GGUF")
TABBY_SERVICE = routing_cfg.get("tabby_service", "tabbyapi.service")
KOBOLD_SERVICE_NAME = routing_cfg.get("kobold_service", "koboldcpp.service")
SWITCH_TIMEOUT = routing_cfg.get("switch_timeout", 60)
# Idle (inter-chunk) timeout when forwarding /v1/chat/completions and /v1/completions
# to the LLM backend. This is the max silence between bytes — NOT a wall-clock cap.
# It governs:
#   - streaming: max wait for the first SSE chunk (covers prompt processing) and
#     max gap between subsequent tokens (catches a wedged backend mid-generation).
#   - non-streaming: total wait for the response body, since the proxy reads it as
#     a single chunk via aread() and the backend writes the whole JSON at once.
# Reasoning models with 4K-token prompts can sit silent 1-3 minutes during prompt
# processing; vision PP for a small image adds ~30-40s; long structured outputs
# at >5 tok/s never approach this. 600s (10 min) of silence is well past any
# legitimate work and tight enough that a hung backend frees the socket promptly.
UPSTREAM_READ_TIMEOUT = float(os.environ.get("UPSTREAM_READ_TIMEOUT", routing_cfg.get("upstream_read_timeout", 600)))

# Roles: logical aliases for backends. Lets clients send `model: "default"` instead of
# hardcoding a filename. Roles map to a backend type ("kobold", "tabby", or any enabled
# registry backend) — never to a specific model file for unmanaged backends.
ROLES = config.get("roles", {})

# Local-swap backends require a GGUF file path to hot-swap the model on disk.
# kobold and tabby are the canonical local-swap backends; any backend whose role
# entry carries a "path" key is also treated as local-swap.
_LOCAL_SWAP_BACKENDS: frozenset[str] = frozenset({"kobold", "tabby"})

# Optional catalog of additional GGUF aliases (parallel to ROLES). Each entry has
# id/backend/path[/description] and is treated like a role with a pinned model:
# clients send `model: "<id>"` and the proxy resolves it via ensure_target().
# This lets us surface arbitrary GGUFs on /v1/models without inventing roles.
# All MODELS_CATALOG entries require a local file path (this is a GGUF catalog).
# Any enabled registry backend may appear here; kobold/tabby behavior is unchanged.
def _normalize_models_catalog(raw) -> dict:
    """Normalize config['models'] (list of dicts) into {normalized_id: entry}.

    Accepts any backend that is known in config["backends"] (or is a legacy
    local-swap backend).  All entries in this catalog MUST supply a local file
    path — it is a GGUF-alias catalog by design.  Entries without a path are
    skipped, matching the previous kobold/tabby-only behaviour exactly.
    """
    known_backends: frozenset[str] = frozenset(
        (config.get("backends") or {}).keys()
    ) | _LOCAL_SWAP_BACKENDS
    out: dict = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id", "")).strip().lower()
        backend = item.get("backend")
        path = item.get("path")
        # Require a known backend AND a local path (GGUF catalog — path is mandatory).
        if not mid or backend not in known_backends or not path:
            continue
        mmproj_raw = item.get("mmproj")
        mmproj_path = str(mmproj_raw).strip() if mmproj_raw else None
        out[mid] = {
            "id": mid,
            "backend": backend,
            "path": str(path).strip(),
            "mmproj": mmproj_path or None,
            "description": str(item.get("description", "") or ""),
        }
    return out


MODELS_CATALOG = _normalize_models_catalog(config.get("models"))

# TTS backend
tts_cfg = config.get("backends", {}).get("tts", {})
TTS_ENABLED = bool_from_config(tts_cfg.get("enabled"), True)
TTS_HOST = f"http://{tts_cfg.get('host', '127.0.0.1')}:{tts_cfg.get('port', 8880)}"
TTS_TIMEOUT = int(tts_cfg.get("timeout", 120))

# STT backend
stt_cfg = config.get("backends", {}).get("stt", {})
STT_ENABLED = bool_from_config(stt_cfg.get("enabled"), True)
STT_HOST = f"http://{stt_cfg.get('host', '127.0.0.1')}:{stt_cfg.get('port', 5092)}"
STT_TIMEOUT = int(stt_cfg.get("timeout", 120))

# Embeddings backend
embeddings_cfg = config.get("backends", {}).get("embeddings", {})
EMBEDDINGS_ENABLED = bool_from_config(
    embeddings_cfg.get("enabled"),
    bool(embeddings_cfg),
)
EMBEDDINGS_HOST = f"http://{embeddings_cfg.get('host', '127.0.0.1')}:{embeddings_cfg.get('port', 5061)}"
EMBEDDINGS_HEALTH_ENDPOINT = embeddings_cfg.get("health_endpoint", "/health")
EMBEDDINGS_TIMEOUT = int(os.environ.get("EMBEDDINGS_TIMEOUT", embeddings_cfg.get("timeout", 120)))
EMBEDDINGS_BACKEND_MODEL = embeddings_cfg.get("model_id", "nomic-embed-text-v1.5")

# ComfyUI backend
comfy_cfg = config.get("backends", {}).get("comfyui", {})
COMFY_ENABLED = bool_from_config(comfy_cfg.get("enabled"), True)
COMFY_HOST = f"http://{comfy_cfg.get('host', '127.0.0.1')}:{comfy_cfg.get('port', 7890)}"
COMFY_TIMEOUT = int(comfy_cfg.get("timeout", 300))
_workflows_dir_cfg = comfy_cfg.get("workflows_dir", "./workflows/images")
COMFY_WORKFLOWS_DIR = Path(_workflows_dir_cfg)
if not COMFY_WORKFLOWS_DIR.is_absolute():
    COMFY_WORKFLOWS_DIR = (Path(__file__).parent / _workflows_dir_cfg).resolve()
COMFY_DEFAULT_MODEL = comfy_cfg.get("default_model", "flux2-klein-4b")
# Video workflows live separately from image workflows so they're version-controlled
# next to proxy.py rather than inside the ComfyUI directory.
_video_workflows_dir_cfg = comfy_cfg.get("video_workflows_dir", "./workflows")
COMFY_VIDEO_WORKFLOWS_DIR = Path(_video_workflows_dir_cfg)
if not COMFY_VIDEO_WORKFLOWS_DIR.is_absolute():
    COMFY_VIDEO_WORKFLOWS_DIR = (Path(__file__).parent / _video_workflows_dir_cfg).resolve()
COMFY_VIDEO_TIMEOUT = int(comfy_cfg.get("video_timeout", 1800))

# Image cache + adaptive-batching config (Fix 1 + Fix 2: honest `n`, real URLs).
images_cfg = config.get("images", {}) or {}
IMAGE_CACHE_DIR = Path(images_cfg.get(
    "cache_dir",
    str(Path(__file__).parent / "cache" / "images"),
))
VRAM_ESTIMATES_FILE = Path(images_cfg.get(
    "vram_estimates_file",
    str(Path(__file__).parent / "cache" / "vram_estimates.json"),
))
IMAGE_SIGNED_URL_TTL = int(images_cfg.get("signed_url_ttl", 3600))
IMAGE_SIGNED_URL_SECRET_CFG = str(images_cfg.get("signed_url_secret", "") or "")
IMAGE_CACHE_TTL = int(images_cfg.get("cache_ttl", 86400))
IMAGE_CACHE_EVICT_INTERVAL = int(images_cfg.get("cache_eviction_interval", 3600))
IMAGE_VRAM_RESERVE_BYTES = int(images_cfg.get("vram_reserve_bytes", 1 * 1024 * 1024 * 1024))
IMAGE_MAX_BATCH_SIZE = int(images_cfg.get("max_batch_size", 4))
IMAGE_BACKGROUND_REMOVAL_CFG = images_cfg.get("background_removal", {}) or {}
IMAGE_BACKGROUND_REMOVAL_ENABLED = bool_from_config(IMAGE_BACKGROUND_REMOVAL_CFG.get("enabled"), True)
IMAGE_BACKGROUND_REMOVAL_TIMEOUT = float(IMAGE_BACKGROUND_REMOVAL_CFG.get("decision_timeout", 45))
IMAGE_MEDIA_JOBS_CFG = images_cfg.get("media_jobs", {}) or {}
GATEWAY_CFG = config.get("gateway", {}) or {}
RESOURCE_ADMISSION_CFG = GATEWAY_CFG.get("resource_admission", {}) or {}
MEDIA_JOB_ADMISSION_CFG = RESOURCE_ADMISSION_CFG.get("media_jobs", {}) or {}
IMAGE_MEDIA_JOB_PAYLOAD_DIR = Path(IMAGE_MEDIA_JOBS_CFG.get(
    "payload_dir",
    str(Path(__file__).parent / "data" / "image_media_jobs"),
))
IMAGE_MEDIA_JOB_MAX_ATTEMPTS = int(IMAGE_MEDIA_JOBS_CFG.get("max_attempts", 3))
IMAGE_MEDIA_JOB_RETRY_BASE_SECONDS = float(IMAGE_MEDIA_JOBS_CFG.get("retry_base_seconds", 5))
IMAGE_MEDIA_JOB_RETRY_MAX_SECONDS = float(IMAGE_MEDIA_JOBS_CFG.get("retry_max_seconds", 30))
IMAGE_MEDIA_JOB_CONCURRENCY = max(1, int(IMAGE_MEDIA_JOBS_CFG.get("concurrency", 1)))
IMAGE_MEDIA_JOB_RESUME_ON_START = bool_from_config(
    os.environ.get("IMAGE_MEDIA_JOB_RESUME_ON_START", IMAGE_MEDIA_JOBS_CFG.get("resume_on_start")),
    True,
)
_image_media_job_semaphore = asyncio.Semaphore(IMAGE_MEDIA_JOB_CONCURRENCY)
_image_media_job_tasks: set[asyncio.Task] = set()
_image_media_job_scheduled: set[str] = set()

# Video config (parallel to images). Maps `model:` aliases -> ComfyUI workflow JSON.
videos_cfg = config.get("videos", {})
VIDEO_DEFAULT_WORKFLOW = videos_cfg.get("default_workflow", "video-ltx-13b-q8")
VIDEO_WORKFLOWS = videos_cfg.get("workflows", {}) or {}
VIDEO_ALIASES = {
    str(k).strip().lower(): str(v).strip()
    for k, v in (videos_cfg.get("aliases", {}) or {}).items()
}

# Routing
routing_cfg = config.get("routing", {})
CHECK_INTERVAL = routing_cfg.get("check_interval", 30)

# LLM behavior (model pinning to avoid kobold restarts on every role change).
# When pin_loaded_model is true (default), ensure_target() will NOT restart
# llama-turbo to swap models — it serves with whatever's loaded and logs a
# one-shot warning per distinct mismatched path. Manual swaps via `llm-switch`
# remain the supported way to change the loaded GGUF.
llm_cfg = config.get("llm", {}) or {}
PIN_LOADED_MODEL = bool(llm_cfg.get("pin_loaded_model", True))

USE_LOADED_MODEL_ALIAS = "current"
USE_LOADED_FAST_MODEL_ALIAS = "current-fast"
USE_LOADED_MODEL_ALIASES = frozenset({
    USE_LOADED_MODEL_ALIAS,
    USE_LOADED_FAST_MODEL_ALIAS,
})

# Tracks model_paths we've already warned about (when PIN_LOADED_MODEL=True)
# so we don't spam the journal with one warning per request.
_pin_mismatch_warned: set = set()

# Auth
auth_cfg = config.get("auth", {})
admin_cfg = config.get("admin", {}) or {}
user_defaults_cfg = admin_cfg.get("user_defaults") or {}
if not isinstance(user_defaults_cfg, Mapping):
    user_defaults_cfg = {}

# Health
HEALTH_CHECK_TIMEOUT = 5
MAX_LOAD_FAILURES = 3
LOAD_FAILURE_BACKOFF = 30

# Endpoints that require model to be loaded
MODEL_REQUIRED_PATHS = [
    "/v1/completions",
    "/v1/chat/completions",
]

# Vision model detection patterns (case-insensitive)
VISION_MODEL_PATTERNS = [
    "vl",      # Qwen-VL, Qwen2-VL, Qwen3-VL
    "vision",  # generic
    "pixtral", # Mistral vision
    "llava",   # LLaVA models
    "cogvlm",  # CogVLM
    "internvl",# InternVL
]


# =============================================================================
# Live runtime registry — hot-reload without process restart
# =============================================================================
# Module-level names (TABBY_HOST, KOBOLD_HOST, LLM_BACKEND_HOSTS, …) are
# Python globals: every function that references them looks up the current
# value at call time, not at import time.  reload_runtime_config() re-derives
# all of them from the DB and reassigns the module names atomically (one
# reference assignment per name, safe under the GIL), so any in-flight or
# subsequent request sees either the old or the new values — never a mix.
#
# The LiteLLM subprocess still needs its own restart to pick up litellm
# config-file changes (that is a separate concern tracked in the design doc).
# Native-routed and capability-routed requests reflect the new registry
# immediately after this function returns.

def _derive_backend_constants(effective_cfg: dict) -> dict:
    """Re-derive all backend-derived constants from *effective_cfg*.

    Returns a dict whose keys match the module-level names that
    reload_runtime_config() will reassign.  Factored out so tests can call
    it directly without touching global state.
    """
    t_cfg = effective_cfg.get("backends", {}).get("tabby", {})
    k_cfg = effective_cfg.get("backends", {}).get("kobold", {})
    tts_c = effective_cfg.get("backends", {}).get("tts", {})
    stt_c = effective_cfg.get("backends", {}).get("stt", {})
    emb_c = effective_cfg.get("backends", {}).get("embeddings", {})
    cfy_c = effective_cfg.get("backends", {}).get("comfyui", {})

    new_tabby_host = f"http://{t_cfg.get('host', '127.0.0.1')}:{t_cfg.get('port', 5050)}"
    new_kobold_host = f"http://{k_cfg.get('host', '127.0.0.1')}:{k_cfg.get('port', 5051)}"
    new_tts_host = f"http://{tts_c.get('host', '127.0.0.1')}:{tts_c.get('port', 8880)}"
    new_stt_host = f"http://{stt_c.get('host', '127.0.0.1')}:{stt_c.get('port', 5092)}"
    new_emb_host = f"http://{emb_c.get('host', '127.0.0.1')}:{emb_c.get('port', 5061)}"
    new_cfy_host = f"http://{cfy_c.get('host', '127.0.0.1')}:{cfy_c.get('port', 7890)}"
    new_tts_enabled = bool_from_config(tts_c.get("enabled"), True)
    new_stt_enabled = bool_from_config(stt_c.get("enabled"), True)
    new_emb_enabled = bool_from_config(emb_c.get("enabled"), bool(emb_c))
    new_cfy_enabled = bool_from_config(cfy_c.get("enabled"), True)

    # Build LLM_BACKEND_HOSTS from the new config — reuse the same predicate
    # as _build_llm_backend_hosts() but over the new effective_cfg.
    new_llm_hosts: dict[str, str] = {}
    for bid, bcfg in (effective_cfg.get("backends") or {}).items():
        if not isinstance(bcfg, Mapping):
            continue
        if not _is_chat_capable_native_backend(bid, bcfg):
            continue
        host = bcfg.get("host", "127.0.0.1")
        port = bcfg.get("port")
        if not port:
            continue
        new_llm_hosts[bid] = f"http://{host}:{port}"

    return {
        "TABBY_HOST": new_tabby_host,
        "KOBOLD_HOST": new_kobold_host,
        "TTS_HOST": new_tts_host,
        "STT_HOST": new_stt_host,
        "EMBEDDINGS_HOST": new_emb_host,
        "COMFY_HOST": new_cfy_host,
        "TTS_ENABLED": new_tts_enabled,
        "STT_ENABLED": new_stt_enabled,
        "EMBEDDINGS_ENABLED": new_emb_enabled,
        "COMFY_ENABLED": new_cfy_enabled,
        "LLM_BACKEND_HOSTS": new_llm_hosts,
    }


def reload_runtime_config() -> dict:
    """Re-derive all backend constants from the current DB service registry.

    Atomically reassigns every module-level name that was originally computed
    from config["backends"] at import time.  Safe to call from any thread:
    each name assignment is a single reference swap under the GIL.

    Returns the dict of updated names → new values (for logging / tests).

    Note: the LiteLLM subprocess config file is NOT reloaded here — that
    requires a separate litellm restart.  Native-routed and capability-routed
    requests see the new registry immediately.
    """
    # Re-overlay the service registry onto the bootstrap YAML config.
    new_cfg = _load_effective_runtime_config(_bootstrap_config)

    # Atomically swap the module-level config dict in place so that any code
    # reading `config.get(...)` directly (e.g. _highest_priority_chat_backend)
    # also sees the new values.  We clear-and-update rather than replacing the
    # reference so existing callers that captured `config` at import time still
    # work (there are none in practice, but this is defensive).
    config.clear()
    config.update(new_cfg)

    # Re-derive and reassign every backend-derived constant.
    # Use globals() rather than sys.modules[__name__] so this works even when
    # a test has popped 'proxy' from sys.modules (e.g. test_gateway_api.py).
    _g = globals()
    updates = _derive_backend_constants(new_cfg)
    for name, value in updates.items():
        _g[name] = value

    print(
        f"[proxy] runtime config reloaded — "
        f"LLM backends: {list(updates['LLM_BACKEND_HOSTS'].keys())} "
        f"TABBY={updates['TABBY_HOST']} KOBOLD={updates['KOBOLD_HOST']}",
        flush=True,
    )
    return updates


def is_vision_model(model_name: str) -> bool:
    """Detect if a model supports vision based on naming patterns."""
    if not model_name:
        return False
    name_lower = model_name.lower()
    # Check for common vision model patterns
    for pattern in VISION_MODEL_PATTERNS:
        # Match pattern as word boundary (e.g., "vl" in "Qwen3-VL" but not in "evolve")
        # Check if pattern appears after a separator or at start/end
        if f"-{pattern}" in name_lower or f"_{pattern}" in name_lower:
            return True
        if name_lower.startswith(pattern) or name_lower.endswith(pattern):
            return True
        if f"-{pattern}-" in name_lower or f"_{pattern}_" in name_lower:
            return True
    return False

# Endpoints that should NOT reset idle timer
NO_ACTIVITY_PATHS = [
    "/health",
    "/v1/model",
    "/v1/models",
    "/v1/audio",  # TTS doesn't affect LLM idle
]

# TTS endpoints - route to TTS backend
TTS_PATHS = [
    "/v1/audio/speech",
]

# =============================================================================
# Model routing - detect backend from filesystem
# =============================================================================

async def get_active_backend() -> Optional[str]:
    """Check which LLM backend is currently running.

    Probes all backends in LLM_BACKEND_HOSTS (registry-derived) in priority
    order and returns the id of the first one that answers healthy.  For the
    current deployment this resolves to 'tabby' or 'kobold' exactly as before.
    """
    backends = config.get("backends") or {}
    # Sort by priority (lower = higher priority) so the preferred backend wins.
    candidates = sorted(
        LLM_BACKEND_HOSTS.keys(),
        key=lambda bid: int((backends.get(bid) or {}).get("priority") or 100),
    )
    for bid in candidates:
        if await check_service_health(bid):
            return bid
    return None


# Drop-in file used to override the kobold model env var for a specific GGUF.
# Numbered higher than 10-qwen27b.conf so systemd's last-wins semantics apply.
KOBOLD_MODEL_DROPIN = Path.home() / f".config/systemd/user/{KOBOLD_SERVICE_NAME}.d/20-model.conf"

# Generic per-service swap-lock registry.  Keyed by service id (e.g. "kobold",
# "tabby", or any future backend id).  A lock is created lazily on first access.
# Concurrency safety: dict reads/writes are GIL-atomic in CPython, so two
# coroutines racing to create the lock for the same key will at worst both
# evaluate asyncio.Lock() and one will overwrite the other — but both callers
# will then obtain *the surviving lock*, which is fine because the only
# invariant we need is "all callers for id X share one lock object".  The
# setdefault() call below makes this explicit and reduces the window to one
# atomic C-level operation.
_service_swap_locks: dict[str, asyncio.Lock] = {}


def swap_lock_for(service_id: str) -> asyncio.Lock:
    """Return the swap lock for *service_id*, creating it on first call."""
    lock = _service_swap_locks.get(service_id)
    if lock is None:
        lock = _service_swap_locks.setdefault(service_id, asyncio.Lock())
    return lock

# Kobold loaded-state cache. Seeded from the backend's state endpoint on first read
# after startup, then updated authoritatively after every swap. TTL guards against
# the server being restarted out-of-band (e.g. manual systemctl restart).
_kobold_state: dict = {"model": None, "mmproj": None, "ts": 0.0}
_KOBOLD_STATE_TTL = 5.0

# Per-state_type seed strategies.  Each entry describes how to ask a live backend
# what model it has loaded.  Add a new entry here when wiring up a new backend type
# (vllm, ollama, etc.) — no other code needs to change.
#
# Keys:
#   endpoint     – path to GET; response must be JSON
#   model_key    – dot-path into the JSON to the loaded model identifier
#   mmproj_key   – dot-path to the mmproj path, or None if the backend has no mmproj
#   mmproj_pgrep – if True, fall back to pgrep --mmproj parsing when mmproj_key is None
#                  (llama.cpp exposes mmproj only in the process cmdline, not in /props)
#
# Dot-path format: "a.b" means data["a"]["b"].  A bare key "x" means data["x"].
_BACKEND_STATE_STRATEGIES: dict = {
    "llama_cpp": {
        "endpoint": "/props",
        "model_key": "model_path",
        "mmproj_key": None,
        "mmproj_pgrep": True,
    },
    "tabbyapi": {
        "endpoint": "/v1/model",
        "model_key": "id",
        "mmproj_key": None,
        "mmproj_pgrep": False,
    },
    # vllm exposes /v1/models (list); take the first model id.
    # Set mmproj_pgrep: False — vllm has no mmproj concept.
    "vllm": {
        "endpoint": "/v1/models",
        "model_key": "data.0.id",
        "mmproj_key": None,
        "mmproj_pgrep": False,
    },
    # Generic OpenAI-compatible backend (Ollama, LM Studio, etc.)
    "openai_compatible": {
        "endpoint": "/v1/models",
        "model_key": "data.0.id",
        "mmproj_key": None,
        "mmproj_pgrep": False,
    },
}


def _resolve_dot_path(data: dict, path: str) -> Optional[str]:
    """Walk dot-separated keys/indices into a parsed JSON structure."""
    cur: object = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (IndexError, ValueError):
                return None
        else:
            return None
    return str(cur) if cur is not None else None


def _kobold_mmproj_from_pgrep() -> Optional[str]:
    """Read --mmproj from the llama-server process on the kobold port.

    /props does not expose the mmproj path, so pgrep is the fallback source.
    Scoped to the kobold port to avoid matching other llama-server instances.
    """
    kobold_port = str(kobold_cfg.get("port", 5051))
    try:
        result = subprocess.run(
            ["pgrep", "-af", "llama-server"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in result.stdout.splitlines():
            if f"--port {kobold_port}" not in line:
                continue
            idx = line.find("--mmproj ")
            if idx == -1:
                return None
            tail = line[idx + len("--mmproj "):]
            end = tail.find(" --")
            if end != -1:
                tail = tail[:end]
            return tail.strip() or None
    except Exception as e:
        print(f"[proxy] _kobold_mmproj_from_pgrep: pgrep failed: {e}", flush=True)
    return None


def _seed_kobold_state() -> None:
    """Populate _kobold_state by querying the live backend.

    Strategy is selected by `state_type` in the kobold backend config
    (proxy_config.yml).  Falls back to pgrep-only if the HTTP query fails or
    the state_type is unrecognised.  Called when the cache is cold (startup or
    TTL expiry) — synchronous but cheap relative to a model swap.
    """
    import urllib.request as _urlreq

    state_type = kobold_cfg.get("state_type", "llama_cpp")
    strategy = _BACKEND_STATE_STRATEGIES.get(state_type)

    model: Optional[str] = None
    mmproj: Optional[str] = None

    if strategy:
        try:
            url = f"{KOBOLD_HOST}{strategy['endpoint']}"
            with _urlreq.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read())
                model = _resolve_dot_path(data, strategy["model_key"])
        except Exception:
            pass  # server not up yet, or wrong endpoint — state stays None

        if strategy.get("mmproj_key"):
            try:
                url = f"{KOBOLD_HOST}{strategy['endpoint']}"
                with _urlreq.urlopen(url, timeout=2) as resp:
                    data = json.loads(resp.read())
                    mmproj = _resolve_dot_path(data, strategy["mmproj_key"])
            except Exception:
                pass
        elif strategy.get("mmproj_pgrep"):
            mmproj = _kobold_mmproj_from_pgrep()
    else:
        # Unknown state_type — best-effort pgrep only.
        print(
            f"[proxy] _seed_kobold_state: unknown state_type={state_type!r}; "
            "falling back to pgrep (add an entry to _BACKEND_STATE_STRATEGIES to fix this)",
            flush=True,
        )
        mmproj = _kobold_mmproj_from_pgrep()

    _kobold_state["model"] = model
    _kobold_state["mmproj"] = mmproj
    _kobold_state["ts"] = time.time()


def _kobold_state_fresh() -> bool:
    return (time.time() - _kobold_state["ts"]) < _KOBOLD_STATE_TTL


def kobold_has_mmproj() -> Optional[bool]:
    """Return True iff the running kobold llama-server has an mmproj loaded.

    Used by Fix 3 (vision routing): if mmproj is loaded, the kobold backend is
    natively vision-capable and we don't need to switch to tabby. Returns None
    if we couldn't determine (server not running, seed failed).
    """
    if not _kobold_state_fresh():
        _seed_kobold_state()
    model = _kobold_state["model"]
    if model is None:
        return None
    return _kobold_state["mmproj"] is not None


def currently_loaded_kobold_mmproj() -> Optional[str]:
    """Return the mmproj path the kobold llama-server has loaded, or None."""
    if not _kobold_state_fresh():
        _seed_kobold_state()
    return _kobold_state["mmproj"]


def currently_loaded_kobold_model() -> Optional[str]:
    """Return the model path the kobold llama-server is currently serving, or None."""
    if not _kobold_state_fresh():
        _seed_kobold_state()
    return _kobold_state["model"]


# (tabby swap lock is managed by the generic swap_lock_for("tabby") registry above.)


def is_literal_gguf_path(value: str) -> bool:
    """Return True if `value` looks like an absolute path to an existing .gguf file."""
    if not value:
        return False
    try:
        return value.endswith(".gguf") and os.path.isabs(value) and os.path.exists(value)
    except Exception:
        return False


async def currently_loaded_tabby_model() -> Optional[str]:
    """Return the model id TabbyAPI is currently serving, or None if nothing is loaded
    or the backend is unreachable. Uses the same `/v1/model` endpoint as `is_model_loaded`."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5)) as client:
            resp = await client.get(
                f"{TABBY_HOST}/v1/model",
                headers={"x-api-key": API_KEY},
            )
            if resp.status_code == 200:
                model_id = resp.json().get("id")
                return model_id or None
    except Exception:
        pass
    return None


async def _currently_loaded_model_for(backend: str) -> Optional[str]:
    """Return the model a swap-managed backend currently has loaded.

    The single contained dispatch from a backend id to its loaded-model probe,
    used by the driver-swap path's already-loaded short-circuit. (The probes
    themselves are engine-shaped state introspection; this keeps that knowledge
    in one lookup rather than scattered through the swap logic.)
    """
    if backend == "kobold":
        return currently_loaded_kobold_model()
    if backend == "tabby":
        return await currently_loaded_tabby_model()
    return None


def _currently_loaded_aux_for(backend: str) -> Optional[str]:
    """Return the aux (mmproj) file a swap-managed backend currently has loaded.

    Contained dispatch from backend id to the aux-probe; used by the
    driver-swap path to populate SwapContext.loaded_aux. Only kobold carries
    an aux/mmproj concept — all other backends return None.
    """
    if backend == "kobold":
        return currently_loaded_kobold_mmproj()
    return None


async def ensure_target(backend: str, model: Optional[str], mmproj: Optional[str] = None) -> bool:
    """Make a (backend, model, mmproj) target real before forwarding a request.

    Every backend goes through its LifecycleDriver — there is no kobold/tabby
    special-case anymore. The driver is resolved from the service's
    `lifecycle.driver` (DATA); engine-shaped knowledge (env key names, load body,
    prechecks) lives in the driver + driver_config, never here.

      - Non-swap drivers (external / docker / command): ensure_ready() —
        health-check (external), or start-then-poll (docker / command). An
        always-on vLLM, remote OpenAI-compatible endpoint, or second llama.cpp
        instance passes through instead of 503-ing.
      - Swap drivers (systemd dropin-env-restart / http-load-unload): when a
        model is requested, build + precheck the activation, then swap() under
        the per-service lock. The driver does the already-loaded short-circuit
        and pin-loaded skip from SwapContext.

    `mmproj` is the optional aux file (kobold vision). Pass None for text-only.

    Returns True on success. Caller turns False into a 503.
    """
    from gateway.lifecycle_drivers import driver_for_service, supports_swap, SwapContext

    bcfg = (config.get("backends") or {}).get(backend) or {}
    driver = driver_for_service(backend, config.get("backends"))

    # Non-swap driver, or no model requested → just make the backend ready.
    if not (supports_swap(driver) and model):
        ready = await driver.ensure_ready(bcfg, timeout_s=HEALTH_CHECK_TIMEOUT)
        if not ready:
            print(
                f"[proxy] ensure_target: backend '{backend}' unreachable via driver",
                flush=True,
            )
        return ready

    # The driver builds + prechecks its own activation from driver_config. On a
    # precheck failure (e.g. a missing GGUF/aux file) it returns None.
    activation = driver.build_activation(bcfg, model=model, aux=mmproj or None)
    if activation is None:
        return False

    # The driver does the already-loaded short-circuit and pin-loaded skip from
    # SwapContext — no engine literal appears here.
    async with swap_lock_for(backend):
        return await driver.swap(
            bcfg,
            activation,
            ctx=SwapContext(
                loaded_model=await _currently_loaded_model_for(backend),
                target_model=model,
                loaded_aux=_currently_loaded_aux_for(backend),
                target_aux=mmproj or None,
                pin_loaded=bool(bcfg.get("pin_loaded_model", PIN_LOADED_MODEL)),
            ),
            warned_models=_pin_mismatch_warned,
        )


# =============================================================================
# Load credentials
# =============================================================================

TABBY_ADMIN_KEY_ENV = auth_cfg.get("admin_key_env", "TABBY_ADMIN_KEY")
TABBY_API_KEY_ENV = auth_cfg.get("api_key_env", "TABBY_API_KEY")
ADMIN_KEY = os.environ.get(TABBY_ADMIN_KEY_ENV, "") or os.environ.get("TABBY_ADMIN_KEY", "")
API_KEY = os.environ.get(TABBY_API_KEY_ENV, "") or os.environ.get("TABBY_API_KEY", "")

# Try loading from tabbyapi's api_tokens.yml (disabled when env var not set)
_tabbyapi_dir_env = os.environ.get("AGW_TABBYAPI_DIR", "")
tokens_file = Path(_tabbyapi_dir_env) / "api_tokens.yml" if _tabbyapi_dir_env else None
if tokens_file and tokens_file.exists():
    for line in tokens_file.read_text().splitlines():
        if line.startswith("admin_key:") and not ADMIN_KEY:
            ADMIN_KEY = line.split(":", 1)[1].strip()
        elif line.startswith("api_key:") and not API_KEY:
            API_KEY = line.split(":", 1)[1].strip()

# Publish the resolved keys under their configured env names so the generic,
# data-driven downstream auth path (auth_inject's `secret_env`) can resolve
# them — including this api_tokens.yml fallback. Without this, a deployment that
# supplies the keys ONLY via api_tokens.yml (env vars unset) would inject no
# downstream auth. setdefault: never clobber an explicitly-set env var.
if ADMIN_KEY:
    os.environ.setdefault(TABBY_ADMIN_KEY_ENV, ADMIN_KEY)
if API_KEY:
    os.environ.setdefault(TABBY_API_KEY_ENV, API_KEY)


# =============================================================================
# Proxy auth
# =============================================================================

CLIENT_AUTH_HEADERS = [
    "authorization",
    "x-api-key",
    "x-admin-key",
    "api-key",
]


def config_path(value: str | None, default_name: str) -> Path:
    """Resolve config paths relative to this proxy directory."""
    path = Path(value or default_name)
    if path.is_absolute():
        return path
    return Path(__file__).parent / path


AUTH_PUBLIC_PATHS = auth_cfg.get("public_paths") or ["/health"]


def _admin_db_path() -> Path:
    raw = (
        os.environ.get("AI_GATEWAY_ADMIN_DB")
        or os.environ.get("OPENAI_PROXY_ADMIN_DB")
        or admin_cfg.get("db_path")
        or "data/admin.db"
    )
    path = Path(str(raw))
    if path.is_absolute():
        return path
    return Path(__file__).parent / path


def _admin_session_signing_key_from_config() -> bytes | None:
    """Return an explicit signing key configured by env or proxy_config.yml."""
    raw = admin_cfg.get("session_signing_key") or ""
    env_name = admin_cfg.get("session_signing_key_env") or ""
    if env_name:
        raw = os.environ.get(str(env_name), raw)
    raw = os.environ.get("AI_GATEWAY_ADMIN_SESSION_KEY", raw)
    raw = os.environ.get("OPENAI_PROXY_ADMIN_SESSION_KEY", raw)
    if not raw:
        return None
    key = str(raw).encode("utf-8")
    if len(key) < 32:
        print(
            "[proxy] WARNING: admin session signing key is shorter than 32 bytes; "
            "falling back to the persistent generated key",
            flush=True,
        )
        return None
    return key


def _admin_session_signing_key_path() -> Path:
    """Path to the persistent auto-generated browser-session signing key."""
    return _admin_db_path().with_name("admin_session_key")


def _admin_session_signing_key() -> bytes | None:
    """Resolve the browser-session signing key.

    Order of precedence:
      1. ``admin.session_signing_key`` / ``AI_GATEWAY_ADMIN_SESSION_KEY`` /
         ``OPENAI_PROXY_ADMIN_SESSION_KEY`` — explicit operator config.
      2. A persistent, restrictive-permission file next to ``admin.db`` —
         auto-generated on first call so the happy path requires no env var.
    """
    explicit = _admin_session_signing_key_from_config()
    if explicit:
        return explicit
    try:
        return local_load_signing_key(_admin_session_signing_key_path())
    except RuntimeError as exc:
        print(f"[proxy] WARNING: {exc}", flush=True)
        return None


def _bootstrap_admin_oidc_cfg() -> dict:
    oidc_cfg = admin_cfg.get("oidc") or {}
    if not isinstance(oidc_cfg, Mapping):
        oidc_cfg = {}
    oidc_cfg = dict(oidc_cfg)
    if (
        oidc_cfg.get("enabled")
        and not oidc_cfg.get("signing_key")
        and not oidc_cfg.get("signing_key_env")
    ):
        session_env = admin_cfg.get("session_signing_key_env")
        if session_env:
            oidc_cfg["signing_key_env"] = session_env
        elif admin_cfg.get("session_signing_key"):
            oidc_cfg["signing_key"] = admin_cfg.get("session_signing_key")
        else:
            generated_key = _admin_session_signing_key()
            if generated_key:
                oidc_cfg["signing_key"] = generated_key
    return oidc_cfg


def _load_admin_oidc_config():
    try:
        return load_oidc_config(_bootstrap_admin_oidc_cfg(), env=os.environ)
    except OIDCConfigError as e:
        print(f"[proxy] WARNING: admin OIDC disabled: {e}", flush=True)
        return load_oidc_config({"enabled": False})


ADMIN_DB_PATH = _admin_db_path()
ADMIN_OIDC_CONFIG = _load_admin_oidc_config()
ADMIN_OIDC_STATES: dict[str, StateRecord] = {}
ADMIN_OIDC_JWKS_CACHE: dict[str, Any] = {}
ADMIN_SESSION_SIGNING_KEY = (
    ADMIN_OIDC_CONFIG.signing_key
    if ADMIN_OIDC_CONFIG.enabled
    else _admin_session_signing_key()
)
ADMIN_STORE = AdminSQLiteStore(ADMIN_DB_PATH)
# Wire in-process hot-reload: after every service-registry mutation the store
# calls reload_runtime_config() so the new backend hosts take effect
# immediately without a process restart.
ADMIN_STORE.on_service_registry_changed = reload_runtime_config
ADMIN_AUTH = AdminAuthenticator(
    ADMIN_STORE,
    signing_key=ADMIN_SESSION_SIGNING_KEY,
    cookie=ADMIN_OIDC_CONFIG.cookie,
)


def _profile_ref_to_store_id(ref: Any) -> str | None:
    raw = str(ref or "").strip()
    if not raw:
        return None
    if raw in {"default", "default-local"}:
        return None
    if admin_store.get_profile(raw, path=ADMIN_STORE.path) is not None:
        return raw
    for profile in admin_store.list_profiles(path=ADMIN_STORE.path):
        profile_doc = profile.get("profile")
        policy_id = (
            str(profile_doc.get("id") or "")
            if isinstance(profile_doc, Mapping)
            else ""
        )
        if raw in {str(profile.get("id") or ""), str(profile.get("name") or ""), policy_id}:
            return str(profile.get("id"))
    return raw


def _user_self_service_enabled() -> bool:
    defaults = _runtime_user_access_defaults()
    return bool_from_config(defaults.get("self_service_apps"), True)


def _auto_create_personal_app_enabled() -> bool:
    defaults = _runtime_user_access_defaults()
    return bool_from_config(
        defaults.get("auto_create_personal_app"),
        _user_self_service_enabled(),
    )


def _bootstrap_user_access_defaults() -> dict[str, Any]:
    defaults = dict(user_defaults_cfg) if isinstance(user_defaults_cfg, Mapping) else {}
    env_self_service = os.environ.get("AI_GATEWAY_USER_SELF_SERVICE")
    if env_self_service is not None:
        defaults["self_service_apps"] = bool_from_config(env_self_service, True)
    env_auto_create = os.environ.get("AI_GATEWAY_AUTO_CREATE_PERSONAL_APP")
    if env_auto_create is not None:
        defaults["auto_create_personal_app"] = bool_from_config(
            env_auto_create,
            bool_from_config(defaults.get("self_service_apps"), True),
        )
    return defaults


def _has_bootstrap_user_access_defaults() -> bool:
    return bool(_bootstrap_user_access_defaults())


def _runtime_user_access_defaults() -> dict[str, Any]:
    try:
        setting = admin_store.get_operator_setting(
            USER_ACCESS_DEFAULTS_SETTING_KEY,
            path=ADMIN_STORE.path,
        )
    except Exception:  # noqa: BLE001
        return _bootstrap_user_access_defaults()
    value = setting.get("value") if isinstance(setting, Mapping) else None
    if isinstance(value, Mapping):
        self_service = bool_from_config(value.get("self_service_apps"), True)
        return {
            "self_service_apps": self_service,
            "auto_create_personal_app": bool_from_config(
                value.get("auto_create_personal_app"),
                self_service,
            ),
        }
    return _bootstrap_user_access_defaults()


def _user_defaults_seeded_in_admin_db() -> bool:
    try:
        return (
            admin_store.get_operator_setting(
                USER_ACCESS_DEFAULTS_SETTING_KEY,
                path=ADMIN_STORE.path,
            )
            is not None
        )
    except Exception:  # noqa: BLE001
        return False


def _mapping_profile_id(ref: Any) -> str | None:
    if str(ref or "").strip() == "default-local":
        return "default-local"
    return _profile_ref_to_store_id(ref)


def _seed_runtime_mapping_from_bootstrap(
    *,
    source_kind: str,
    source_value: str | None,
    profile_id: str,
) -> bool:
    # Avoid re-enabling a mapping an operator deliberately disabled.
    kind = str(source_kind or "").strip().lower()
    if kind == "all_users":
        value = "*"
    else:
        value = " ".join(str(source_value or "").strip().lower().strip("/").split())
    if not kind or (kind != "all_users" and not value):
        return False
    for row in admin_store.list_runtime_profile_mappings(
        include_disabled=True,
        path=ADMIN_STORE.path,
    ):
        if (
            str(row.get("source_kind") or "") == kind
            and str(row.get("source_value") or "") == value
            and str(row.get("profile_id") or "") == profile_id
        ):
            return False
    admin_store.create_runtime_profile_mapping(
        source_kind=source_kind,
        source_value=source_value,
        profile_id=profile_id,
        created_by="bootstrap-config",
        path=ADMIN_STORE.path,
    )
    return True


def _seed_user_defaults_from_bootstrap_config() -> dict[str, int]:
    """Import first-run mutable defaults into admin.db once.

    ``admin.user_defaults`` remains a bootstrap convenience so a fresh host can
    start, but runtime policy must become admin.db-owned after boot. This
    seeding path is intentionally idempotent and never re-enables mappings an
    operator has disabled in the dashboard.
    """
    bootstrap_defaults = _bootstrap_user_access_defaults()
    if not bootstrap_defaults:
        return {"settings": 0, "mappings": 0}

    seeded_settings = 0
    setting = admin_store.get_operator_setting(
        USER_ACCESS_DEFAULTS_SETTING_KEY,
        path=ADMIN_STORE.path,
    )
    if setting is None:
        defaults = {
            "self_service_apps": bool_from_config(
                bootstrap_defaults.get("self_service_apps"),
                True,
            ),
            "auto_create_personal_app": bool_from_config(
                bootstrap_defaults.get("auto_create_personal_app"),
                bool_from_config(bootstrap_defaults.get("self_service_apps"), True),
            ),
        }
        admin_store.put_operator_setting(
            USER_ACCESS_DEFAULTS_SETTING_KEY,
            defaults,
            updated_by="bootstrap-config",
            path=ADMIN_STORE.path,
        )
        admin_store.record_audit(
            "operator.user_access_defaults.seed",
            actor_kind="system",
            actor_id="startup",
            target_kind="operator_setting",
            target_id=USER_ACCESS_DEFAULTS_SETTING_KEY,
            metadata={"fields": sorted(defaults)},
            path=ADMIN_STORE.path,
        )
        seeded_settings = 1

    seeded_mappings = 0
    default_profile_id = _mapping_profile_id(bootstrap_defaults.get("default_profile_id"))
    if default_profile_id:
        seeded_mappings += int(
            _seed_runtime_mapping_from_bootstrap(
                source_kind="all_users",
                source_value=None,
                profile_id=default_profile_id,
            )
        )

    for mapping_name, source_kind in (
        ("role_profile_ids", "gateway_role"),
        ("domain_profile_ids", "domain"),
        ("email_profile_ids", "email"),
    ):
        mapping = bootstrap_defaults.get(mapping_name)
        if not isinstance(mapping, Mapping):
            continue
        for source_value, profile_ref in mapping.items():
            profile_id = _mapping_profile_id(profile_ref)
            if not profile_id:
                continue
            seeded_mappings += int(
                _seed_runtime_mapping_from_bootstrap(
                    source_kind=source_kind,
                    source_value=str(source_value),
                    profile_id=profile_id,
                )
            )

    if seeded_mappings:
        admin_store.record_audit(
            "runtime_profile_mapping.seed",
            actor_kind="system",
            actor_id="startup",
            target_kind="runtime_profile_mapping",
            target_id="bootstrap-user-defaults",
            metadata={"count": seeded_mappings},
            path=ADMIN_STORE.path,
        )

    return {"settings": seeded_settings, "mappings": seeded_mappings}


def _profile_from_map(mapping_name: str, key: str) -> str | None:
    mapping = user_defaults_cfg.get(mapping_name)
    if not isinstance(mapping, Mapping):
        return None
    raw = mapping.get(key) or mapping.get(key.lower())
    return _profile_ref_to_store_id(raw)


def _stored_oidc_access_names_for_user(user_id: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"oidc_group": [], "oidc_role": []}
    if not user_id:
        return out
    for row in admin_store.list_user_identity_claims(
        user_id,
        path=ADMIN_STORE.path,
    ):
        kind = str(row.get("claim_kind") or "")
        if kind in out:
            out[kind].append(str(row.get("claim_value") or ""))
    return out


def _default_profile_id_for_user(
    *,
    email: str,
    role: str,
    oidc_groups: Iterable[str] = (),
    oidc_roles: Iterable[str] = (),
) -> str | None:
    normalized_email = email.strip().lower()
    mapped_profile = admin_store.resolve_runtime_profile_mapping(
        email=normalized_email,
        gateway_role=role,
        oidc_groups=oidc_groups,
        oidc_roles=oidc_roles,
        path=ADMIN_STORE.path,
    )
    if mapped_profile:
        return _profile_ref_to_store_id(mapped_profile)

    if _user_defaults_seeded_in_admin_db():
        return None

    if normalized_email:
        profile_id = _profile_from_map("email_profile_ids", normalized_email)
        if profile_id:
            return profile_id
        _, _, domain = normalized_email.partition("@")
        if domain:
            profile_id = _profile_from_map("domain_profile_ids", domain)
            if profile_id:
                return profile_id

    normalized_role = (role or "none").strip().lower() or "none"
    profile_id = _profile_from_map("role_profile_ids", normalized_role)
    if profile_id:
        return profile_id

    return _profile_ref_to_store_id(user_defaults_cfg.get("default_profile_id"))


def _admin_role_for_user_id(user_id: str) -> str:
    roles = {
        str(role.get("role") or "")
        for role in admin_store.list_admin_roles(
            subject_kind="user",
            subject_id=user_id,
            path=ADMIN_STORE.path,
        )
    }
    for role in ("owner", "superadmin", "admin"):
        if role in roles:
            return role
    return "none"


def _default_profile_id_for_session(session) -> str | None:
    user_id = str(getattr(session, "subject", "") or "")
    oidc_access = _stored_oidc_access_names_for_user(user_id)
    return _default_profile_id_for_user(
        email=str(getattr(session, "email", "") or ""),
        role=_admin_role_for_user_id(user_id),
        oidc_groups=oidc_access["oidc_group"],
        oidc_roles=oidc_access["oidc_role"],
    )


def _inherited_profile_id_for_app(app: Mapping[str, Any]) -> str | None:
    if not _user_self_service_enabled():
        return None
    owner_user_id = str(app.get("owner_user_id") or "")
    if not owner_user_id:
        return None
    user = admin_store.get_user(owner_user_id, path=ADMIN_STORE.path)
    if user is None:
        return None
    oidc_access = _stored_oidc_access_names_for_user(owner_user_id)
    return _default_profile_id_for_user(
        email=str(user.get("email") or ""),
        role=_admin_role_for_user_id(owner_user_id),
        oidc_groups=oidc_access["oidc_group"],
        oidc_roles=oidc_access["oidc_role"],
    )


def _provision_existing_user_personal_apps() -> int:
    if not (_user_self_service_enabled() and _auto_create_personal_app_enabled()):
        return 0
    created = 0
    for user in admin_store.list_users(path=ADMIN_STORE.path):
        user_id = str(user.get("id") or "")
        if not user_id:
            continue
        before = admin_store.list_apps_for_user(user_id, path=ADMIN_STORE.path)
        if any(app.get("kind") == "personal" for app in before):
            continue
        label = user.get("email") or user.get("display_name") or user_id
        oidc_access = _stored_oidc_access_names_for_user(user_id)
        default_profile_id = _default_profile_id_for_user(
            email=str(user.get("email") or ""),
            role=_admin_role_for_user_id(user_id),
            oidc_groups=oidc_access["oidc_group"],
            oidc_roles=oidc_access["oidc_role"],
        )
        app = admin_store.ensure_personal_app_for_user(
            user_id,
            name=f"{label} personal access",
            default_profile_id=default_profile_id,
            path=ADMIN_STORE.path,
        )
        admin_store.record_audit(
            "app.self_create",
            actor_kind="system",
            actor_id="startup",
            target_kind="app",
            target_id=str(app["id"]),
            metadata={
                "email": user.get("email"),
                "via": "startup_auto_provision",
                "effective_profile_id": default_profile_id or "default-local",
            },
            path=ADMIN_STORE.path,
        )
        created += 1
    return created


def _ensure_personal_app_for_owner(
    user: Mapping[str, Any],
    *,
    role: str,
    via: str = "setup_local_owner",
) -> Mapping[str, Any] | None:
    """Ensure a just-claimed local owner has a personal App.

    Parity with the OIDC login path, so the user portal can hand the new owner
    a working key without a separate "create App" step. No-op (returns None)
    when self-service or auto-create is disabled.
    """
    if not (_user_self_service_enabled() and _auto_create_personal_app_enabled()):
        return None
    user_id = str(user.get("id") or "")
    if not user_id:
        return None
    label = user.get("email") or user.get("display_name") or user_id
    oidc_access = _stored_oidc_access_names_for_user(user_id)
    default_profile_id = _default_profile_id_for_user(
        email=str(user.get("email") or ""),
        role=role,
        oidc_groups=oidc_access["oidc_group"],
        oidc_roles=oidc_access["oidc_role"],
    )
    app = admin_store.ensure_personal_app_for_user(
        user_id,
        name=f"{label} personal access",
        default_profile_id=default_profile_id,
        path=ADMIN_STORE.path,
    )
    admin_store.record_audit(
        "app.self_create",
        actor_kind="user",
        actor_id=user_id,
        target_kind="app",
        target_id=str(app["id"]),
        metadata={
            "email": user.get("email"),
            "via": via,
            "effective_profile_id": default_profile_id or "default-local",
        },
        path=ADMIN_STORE.path,
    )
    return app


AuthIdentity = RuntimeIdentity


@dataclass(frozen=True)
class AuthContext:
    identity: Optional[AuthIdentity] = None


NO_AUTH_CONTEXT = AuthContext()


def hash_api_key(api_key: str) -> str:
    """Hash a client-facing proxy key for storage and lookup."""
    return runtime_hash_api_key(api_key)


def normalize_key_hash(key_hash: str) -> str:
    return runtime_normalize_key_hash(key_hash)


def get_client_api_key_from_headers(headers) -> str:
    """Extract the caller's proxy key from OpenAI-compatible auth headers."""
    auth_header = headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        parts = auth_header.split(None, 1)
        if len(parts) == 2:
            return parts[1].strip()
        return ""

    for header_name in ("x-api-key", "api-key"):
        api_key = headers.get(header_name, "")
        if api_key:
            return api_key.strip()

    return ""


AUTH_ENABLED = bool_from_config(
    os.environ.get("OPENAI_PROXY_AUTH_ENABLED", auth_cfg.get("enabled")),
    True,
)

# When enabled, Ollama read-only discovery routes (/api/tags, /api/version and
# their /ollama/api/* aliases) are served without a bearer token.  This matches
# how a local Ollama daemon behaves and lets Open WebUI and similar clients
# auto-discover models without needing explicit token wiring.
#
# Safe default is False because this gateway is typically internet-facing.
# Set auth.ollama_compat_discovery: true in proxy_config.yml or export
# OLLAMA_COMPAT_DISCOVERY=true to opt in.
OLLAMA_COMPAT_DISCOVERY_ENABLED = bool_from_config(
    os.environ.get("OLLAMA_COMPAT_DISCOVERY", auth_cfg.get("ollama_compat_discovery")),
    False,
)

_OLLAMA_DISCOVERY_PUBLIC_PATHS = frozenset({
    "/api/tags",
    "/ollama/api/tags",
    "/api/version",
    "/ollama/api/version",
})


# ---------------------------------------------------------------------------
# Optional frictionless-auth features (all OFF by default; see proxy_config.yml
# auth: block). With every default left off, behavior is identical to before:
# header Bearer/x-api-key required on every non-public route.
#
# 1. LAN/localhost bypass  — clients in lan_bypass_cidrs skip the key entirely.
# 2. ?api_key= query param — for clients that cannot set an auth header.
# 3. path-prefix tokens    — /<token>/v1/... where the path IS the credential.
#
# SECURITY: the real client IP is, by default, the raw TCP socket peer. We only
# consult X-Forwarded-For when the operator has named their reverse proxy in
# trusted_proxies (rightmost-first walk — the leftmost XFF entry is attacker-
# controlled). Cloudflare's CF-Connecting-IP is honored only when the peer is a
# verified Cloudflare IP. This makes LAN bypass safe behind any proxy (or none).
# ---------------------------------------------------------------------------

LAN_BYPASS_ENABLED = bool_from_config(
    os.environ.get("OPENAI_PROXY_LAN_BYPASS", auth_cfg.get("lan_bypass_enabled")),
    False,
)
ALLOW_QUERY_PARAM_KEY = bool_from_config(
    os.environ.get("OPENAI_PROXY_QUERY_PARAM_KEY", auth_cfg.get("allow_query_param_key")),
    False,
)
CLOUDFLARE_MODE = bool_from_config(
    os.environ.get("OPENAI_PROXY_CF_MODE", auth_cfg.get("cloudflare_mode")),
    False,
)


def _parse_cidrs(raw) -> list:
    """Parse a list of IP/CIDR strings into ip_network objects; skip bad ones."""
    nets = []
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",") if p.strip()]
    for entry in raw or []:
        try:
            nets.append(ipaddress.ip_network(str(entry).strip(), strict=False))
        except ValueError:
            print(f"[proxy] WARNING: ignoring invalid CIDR in auth config: {entry!r}", flush=True)
    return nets


LAN_BYPASS_CIDRS = _parse_cidrs(auth_cfg.get("lan_bypass_cidrs"))
TRUSTED_PROXIES = _parse_cidrs(auth_cfg.get("trusted_proxies"))
PATH_TOKENS = dict(auth_cfg.get("path_tokens") or {})

# Cloudflare edge IP ranges, populated at startup when CLOUDFLARE_MODE is on.
# None => fetch failed => refuse to honor CF-Connecting-IP (fail closed).
_CF_IP_NETWORKS: list | None = None

# Docker bridge / RFC-1918 ranges used only to warn operators about the
# containerized-without-trusted-proxies footgun (NOT auto-trusted for anything).
_DOCKER_BRIDGE_HINT_NETS = _parse_cidrs(["172.16.0.0/12", "10.0.0.0/8"])


def _init_cloudflare_ip_ranges() -> None:
    """Fetch Cloudflare edge IP ranges once at startup (CLOUDFLARE_MODE only).

    On any failure, leaves _CF_IP_NETWORKS as None so CF-Connecting-IP is refused
    (fail closed). Synchronous + best-effort; ranges change rarely.
    """
    global _CF_IP_NETWORKS
    if not CLOUDFLARE_MODE:
        return
    try:
        import urllib.request
        nets = []
        for url in ("https://www.cloudflare.com/ips-v4", "https://www.cloudflare.com/ips-v6"):
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read().decode("utf-8", "ignore")
            nets.extend(_parse_cidrs([ln for ln in body.splitlines() if ln.strip()]))
        if nets:
            _CF_IP_NETWORKS = nets
            print(f"[proxy] cloudflare_mode: loaded {len(nets)} Cloudflare IP ranges", flush=True)
        else:
            print("[proxy] WARNING: cloudflare_mode on but no IP ranges parsed; "
                  "CF-Connecting-IP will be refused (fail closed)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] WARNING: cloudflare_mode on but IP-range fetch failed ({e}); "
              "CF-Connecting-IP will be refused (fail closed)", flush=True)


def _warn_auth_footguns() -> None:
    """Surface the common LAN-bypass misconfiguration at startup."""
    if LAN_BYPASS_ENABLED and not LAN_BYPASS_CIDRS:
        print("[proxy] WARNING: lan_bypass_enabled but lan_bypass_cidrs is empty; "
              "LAN bypass will never trigger.", flush=True)
    if LAN_BYPASS_ENABLED and not TRUSTED_PROXIES:
        print("[proxy] NOTE: LAN bypass uses the raw TCP socket peer (no trusted_proxies "
              "set). If you run behind a reverse proxy or in Docker, the peer is the "
              "proxy/bridge IP, not the real client — set auth.trusted_proxies so "
              "X-Forwarded-For is honored.", flush=True)


_init_cloudflare_ip_ranges()
_warn_auth_footguns()


def _normalize_ip(ip_str: str):
    """Return an ip_address, unwrapping IPv4-mapped IPv6 (::ffff:1.2.3.4)."""
    addr = ipaddress.ip_address(ip_str.strip())
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _ip_in_any(addr, nets) -> bool:
    for net in nets:
        try:
            if addr in net:
                return True
        except TypeError:
            # IPv4 addr vs IPv6 net (or vice versa) — not a match.
            continue
    return False


def resolve_real_client_ip(request: Request):
    """Best-effort, spoofing-resistant real client IP.

    Default: trust ONLY the TCP socket peer. Forwarded headers are consulted only
    when the peer is a configured trusted proxy. Returns an ip_address or None.
    """
    client = request.scope.get("client")
    if not client:
        return None
    try:
        peer = _normalize_ip(client[0])
    except ValueError:
        return None

    # Cloudflare: only when the peer is verified Cloudflare infra (fail closed).
    if CLOUDFLARE_MODE and _CF_IP_NETWORKS and _ip_in_any(peer, _CF_IP_NETWORKS):
        cf = request.headers.get("cf-connecting-ip", "").strip()
        if cf:
            try:
                return _normalize_ip(cf)
            except ValueError:
                return peer

    # X-Forwarded-For, only if the immediate peer is a trusted proxy.
    if TRUSTED_PROXIES and _ip_in_any(peer, TRUSTED_PROXIES):
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            for candidate in reversed([p.strip() for p in xff.split(",") if p.strip()]):
                try:
                    cand = _normalize_ip(candidate)
                except ValueError:
                    continue
                if not _ip_in_any(cand, TRUSTED_PROXIES):
                    return cand  # first non-proxy IP from the right = real client

    return peer


def _client_is_lan(request: Request) -> bool:
    if not LAN_BYPASS_ENABLED or not LAN_BYPASS_CIDRS:
        return False
    addr = resolve_real_client_ip(request)
    return addr is not None and _ip_in_any(addr, LAN_BYPASS_CIDRS)


def _path_token_from_request(request_path: str) -> str | None:
    """Return a configured path-prefix token if request_path begins with one."""
    if not PATH_TOKENS:
        return None
    first = request_path.lstrip("/").split("/", 1)[0]
    for token in PATH_TOKENS:
        if hmac.compare_digest(first, str(token)):
            return token
    return None


def _identity_for(name: str, marker: str):
    """Build a synthetic identity for non-credential auth (LAN / path token)."""
    return RuntimeIdentity(
        name=name,
        key_hash=f"{marker}:{hashlib.sha256(name.encode()).hexdigest()[:16]}",
        profile=DEFAULT_PROFILE,
    )


def _resolve_client_api_key(request: Request) -> str:
    """Header Bearer/x-api-key, then optional ?api_key= fallback."""
    key = get_client_api_key_from_headers(request.headers)
    if key:
        return key
    if ALLOW_QUERY_PARAM_KEY:
        return (request.query_params.get("api_key") or "").strip()
    return ""


def active_app_credential_count() -> int:
    try:
        return len(
            admin_store.list_active_credentials(
                subject_kind="app",
                path=ADMIN_STORE.path,
            )
        )
    except Exception:  # noqa: BLE001
        return 0


def is_public_path(path: str) -> bool:
    """Allow health checks (and optionally Ollama discovery) without client auth."""
    candidates = list(AUTH_PUBLIC_PATHS)
    if OLLAMA_COMPAT_DISCOVERY_ENABLED:
        candidates.extend(_OLLAMA_DISCOVERY_PUBLIC_PATHS)
    for public_path in candidates:
        public_path = str(public_path)
        if public_path.endswith("*") and path.startswith(public_path[:-1]):
            return True
        if path == public_path:
            return True
    return False


def auth_error(message: str, status_code: int = 401) -> Response:
    return Response(
        content=json.dumps({
            "error": {
                "message": message,
                "type": "authentication_error",
            }
        }),
        status_code=status_code,
        media_type="application/json",
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate_request(request: Request, request_path: str) -> AuthContext | Response:
    """Authenticate a client-facing proxy key when auth is configured.

    Decision order: public path -> LAN bypass -> path-prefix token ->
    header/query API key -> 401. All bypass mechanisms are opt-in and OFF by
    default, so with stock config this is identical to plain Bearer auth.
    """
    if not AUTH_ENABLED or is_public_path(request_path):
        return NO_AUTH_CONTEXT

    # 1. Trusted LAN/localhost client (opt-in via auth.lan_bypass_*).
    if _client_is_lan(request):
        return AuthContext(identity=_identity_for("lan-bypass", "lan"))

    # 2. Per-device path-prefix token (opt-in via auth.path_tokens).
    token = _path_token_from_request(request_path)
    if token is not None:
        return AuthContext(identity=_identity_for(str(PATH_TOKENS[token]), "path-token"))

    # 3. Header Bearer / x-api-key, then optional ?api_key= query fallback.
    api_key = _resolve_client_api_key(request)
    if not api_key:
        return auth_error("Missing API key.")

    key_hash = hash_api_key(api_key)
    identity = runtime_identity_for_app_credential_hash(
        key_hash,
        inherited_profile_id_for_app=_inherited_profile_id_for_app,
        path=ADMIN_STORE.path,
    )
    if not identity:
        return auth_error("Invalid API key.")

    return AuthContext(identity=identity)


def pop_header(headers: dict, header_name: str):
    for key in list(headers.keys()):
        if key.lower() == header_name:
            headers.pop(key, None)


def resolve_secret_value(value) -> str:
    """Resolve a literal or env-backed secret value from auth config."""
    if isinstance(value, dict):
        secret = value.get("value", "")
        env_name = value.get("env", "")
        if env_name:
            secret = os.environ.get(env_name, secret)
        prefix = value.get("prefix", "")
        if prefix and secret:
            return f"{prefix} {secret}"
        return str(secret or "")

    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value.removeprefix("env:"), "")

    return str(value or "")


def service_auth_config(auth_context: AuthContext, service: str) -> dict:
    if not auth_context.identity:
        return {}
    service_cfg = auth_context.identity.downstream.get(service, {})
    return service_cfg if isinstance(service_cfg, dict) else {}


def downstream_secret(service_cfg: dict, name: str, default: str = "") -> str:
    if name in service_cfg:
        return resolve_secret_value(service_cfg[name])

    env_name = service_cfg.get(f"{name}_env", "")
    if env_name:
        return os.environ.get(env_name, default)

    return default


def apply_configured_downstream_headers(headers: dict, service_cfg: dict):
    configured_headers = service_cfg.get("headers", {})
    if not isinstance(configured_headers, dict):
        return

    for header_name, header_value in configured_headers.items():
        resolved_value = resolve_secret_value(header_value)
        if resolved_value:
            headers[str(header_name)] = resolved_value


def configured_downstream_headers(auth_context: AuthContext, service: str) -> dict:
    """Build headers for proxy-originated backend calls."""
    headers = {}
    service_cfg = service_auth_config(auth_context, service)
    apply_configured_downstream_headers(headers, service_cfg)
    return headers


def _path_matches(request_path: str, patterns) -> bool:
    """A rule with no `paths` matches everything; otherwise exact-path match.

    Kept deliberately simple (exact paths) — that is all any current backend
    needs. A pattern list is operator data, so richer matching can be added
    without touching call sites if a backend ever requires it.
    """
    if not patterns:
        return True
    return request_path in patterns


def _resolve_inject_secret(service_cfg: dict, rule: Mapping) -> str:
    """Resolve the secret value for one auth_inject rule.

    Resolution order (all DATA — no hardcoded engine secret):
      1. `secret`: a downstream_secret name — looks up `service_cfg[name]` then
         `service_cfg[name_env]` (the per-identity downstream config).
      2. `secret_env`: an environment variable name to read directly. This is
         the deployment-wide fallback (e.g. the gateway's own TABBY_ADMIN_KEY),
         used when the identity carries no per-user key — exactly what the old
         hardcoded tabby auth did, now expressed as config.
    """
    secret_name = rule.get("secret")
    if secret_name:
        value = downstream_secret(service_cfg, str(secret_name))
        if value:
            return value
    secret_env = rule.get("secret_env")
    if secret_env:
        return os.environ.get(str(secret_env), "")
    return ""


def apply_auth_inject(headers: dict, service_cfg: dict, request_path: str):
    """Inject downstream auth headers from the service's `auth_inject` DATA.

    `service_cfg["auth_inject"]` is a list of rules, first-match-wins:

        auth_inject:
          - paths: ["/v1/models", "/v1/model/list"]   # omit = matches all paths
            header: x-admin-key
            secret: admin_key            # per-identity downstream secret name…
            secret_env: TABBY_ADMIN_KEY  # …falling back to this env var
          - header: x-api-key
            secret: api_key
            secret_env: TABBY_API_KEY

    For each request, the first rule whose `paths` match (and whose resolved
    secret is non-empty) sets its header, then injection stops. This expresses
    per-backend auth quirks (e.g. an admin key required only for model-list
    endpoints) as operator-authored config — no engine name appears in code.
    """
    rules = service_cfg.get("auth_inject")
    if not isinstance(rules, (list, tuple)):
        return

    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        if not _path_matches(request_path, rule.get("paths")):
            continue
        header_name = rule.get("header")
        if not header_name:
            continue
        value = _resolve_inject_secret(service_cfg, rule)
        if value:
            headers[str(header_name)] = value
            return


def prepare_downstream_headers(
    request_headers,
    auth_context: AuthContext,
    service: str,
    request_path: str = "",
) -> dict:
    """Build backend headers without leaking client-facing proxy credentials."""
    headers = dict(request_headers)
    pop_header(headers, "host")
    pop_header(headers, "content-length")

    if AUTH_ENABLED or auth_context.identity:
        for header_name in CLIENT_AUTH_HEADERS:
            pop_header(headers, header_name)

    service_cfg = service_auth_config(auth_context, service)
    apply_configured_downstream_headers(headers, service_cfg)

    # auth_inject is a property of the BACKEND (e.g. "this engine needs an admin
    # key for model-list endpoints"), not of each identity — so fall back to the
    # backend's declared auth_inject when the identity doesn't set its own. This
    # is what makes downstream auth work for every caller (incl. anonymous), as
    # the old hardcoded tabby auth did, but now from DATA.
    inject_cfg = service_cfg
    if not service_cfg.get("auth_inject"):
        backend_inject = (config.get("backends") or {}).get(service, {}).get("auth_inject")
        if backend_inject:
            inject_cfg = {**service_cfg, "auth_inject": backend_inject}
    apply_auth_inject(headers, inject_cfg, request_path)

    return headers


def handle_cli_command() -> bool:
    if len(sys.argv) < 2:
        return False

    if sys.argv[1] == "refresh-openai-models":
        if len(sys.argv) != 2:
            print("Usage: python3 proxy.py refresh-openai-models", file=sys.stderr)
            sys.exit(2)
        sys.exit(asyncio.run(refresh_openai_model_aliases()))

    if sys.argv[1] == "generate-admin-key":
        if len(sys.argv) == 3 and sys.argv[2] in {"-h", "--help", "help"}:
            print("Usage: python3 proxy.py generate-admin-key <service-account-name> [role]")
            sys.exit(0)
        if len(sys.argv) not in (3, 4):
            print(
                "Usage: python3 proxy.py generate-admin-key <service-account-name> [role]",
                file=sys.stderr,
            )
            sys.exit(2)
        name = sys.argv[2].strip()
        role = sys.argv[3].strip() if len(sys.argv) == 4 else "owner"
        if not name:
            print("Service account name cannot be empty.", file=sys.stderr)
            sys.exit(2)
        if role not in {"owner", "admin"}:
            print("Role must be one of: owner, admin.", file=sys.stderr)
            sys.exit(2)
        admin_store.init_db(ADMIN_DB_PATH)
        api_key, credential, assignment = create_admin_service_account_key(
            name, role=role, path=ADMIN_DB_PATH
        )
        print(f"Created AI Gateway admin key for service account {name}.")
        print(f"Stored key hash in {ADMIN_DB_PATH}.")
        print(f"Credential id: {credential['id']} ({credential['prefix']}...)")
        print(f"Role assignment: {assignment['role']}")
        print("Use this key only for /admin/api control-plane calls:")
        print(api_key)
        return True

    return False


# TabbyAPI config path (optional; feature disabled when AGW_TABBYAPI_DIR is not set)
TABBY_CONFIG_FILE = Path(_tabbyapi_dir_env) / "config.yml" if _tabbyapi_dir_env else None

def get_tabby_model_config() -> tuple[str, str, dict]:
    """Read TabbyAPI config live. Returns (default_model, model_dir, model_params)."""
    default_model = os.environ.get("TABBY_DEFAULT_MODEL", "")
    model_dir = ""
    model_params = {}

    if TABBY_CONFIG_FILE and TABBY_CONFIG_FILE.exists():
        try:
            with open(TABBY_CONFIG_FILE) as f:
                tabby_config = yaml.safe_load(f)
            model_cfg = tabby_config.get("model", {})
            model_dir = model_cfg.get("model_dir", "")
            if not default_model:
                default_model = model_cfg.get("model_name", "")
            for key in ["max_seq_len", "cache_size", "cache_mode", "gpu_split_auto",
                        "autosplit_reserve", "max_batch_size", "chunk_size"]:
                if key in model_cfg:
                    model_params[key] = model_cfg[key]
        except Exception as e:
            print(f"[proxy] Warning: Failed to load tabby config: {e}", flush=True)

    return default_model, model_dir, model_params

# Initial load for startup values
DEFAULT_MODEL, MODEL_DIR, _ = get_tabby_model_config()

# Register the tabby params-provider so HttpLoadUnloadDriver can call
# get_tabby_model_config() live at swap time (option A from
# LIFECYCLE_DRIVER_PORT.md §3.2).  The key "tabby-config" lives in
# driver_config.load_body_params — purely data; no engine literal in driver code.
try:
    from gateway.lifecycle_drivers import register_params_provider as _reg_pp

    def _tabby_params_provider(svc: object) -> dict:  # type: ignore[type-arg]
        _, _, params = get_tabby_model_config()
        return params

    _reg_pp("tabby-config", _tabby_params_provider)
except Exception:
    pass


# =============================================================================
# Model compatibility
# =============================================================================

compat_cfg = config.get("model_compatibility", {})
MODEL_COMPAT_ENABLED = bool_from_config(
    os.environ.get("OPENAI_PROXY_MODEL_COMPAT_ENABLED", compat_cfg.get("enabled")),
    True,
)
OPENAI_MODEL_ALIASES_FILE = config_path(
    os.environ.get("OPENAI_MODEL_ALIASES_FILE", compat_cfg.get("generated_aliases_file")),
    "openai_model_aliases.generated.yml",
)
OPENAI_MODELS_API_URL = compat_cfg.get("openai_models_api_url", "https://api.openai.com/v1/models")
OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE = bool_from_config(
    os.environ.get("OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE", compat_cfg.get("include_private")),
    False,
)
MODEL_COMPAT_DEFAULTS = compat_cfg.get("defaults", {})
MODEL_COMPAT_ALIASES = compat_cfg.get("aliases", {})
TTS_BACKEND_MODEL = MODEL_COMPAT_DEFAULTS.get("speech") or tts_cfg.get("default_model", "mistralai/Voxtral-4B-TTS-2603")
SUPPORTED_TTS_RESPONSE_FORMATS = {"wav", "mp3"}
TTS_RESPONSE_MEDIA_TYPES = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
}
STT_BACKEND_MODEL = MODEL_COMPAT_DEFAULTS.get("transcriptions") or stt_cfg.get("default_model", "parakeet-tdt-0.6b-v3")
EMBEDDINGS_MODEL = MODEL_COMPAT_DEFAULTS.get("embeddings") or EMBEDDINGS_BACKEND_MODEL
IMAGE_EDIT_DEFAULT_MODEL = MODEL_COMPAT_DEFAULTS.get("image_edits", "flux2-klein-4b-edit")

# Voice metadata derived from the id. Keep in sync with the voxtral-voices
# transform map in gateway/transforms.py (the single source of truth).
_OPENAI_VOICE_NAMES = {
    "alloy": ("Alloy", "OpenAI alias"),
    "echo": ("Echo", "OpenAI alias"),
    "fable": ("Fable", "OpenAI alias"),
    "nova": ("Nova", "OpenAI alias"),
    "onyx": ("Onyx", "OpenAI alias"),
    "shimmer": ("Shimmer", "OpenAI alias"),
    "sophia": ("Sophia", "OpenAI alias"),
    "isabella": ("Isabella", "OpenAI alias"),
    "lily": ("Lily", "OpenAI alias"),
    "evan": ("Evan", "OpenAI alias"),
    "adam": ("Adam", "OpenAI alias"),
    "ash": ("Ash", "OpenAI alias"),
    "coral": ("Coral", "OpenAI alias"),
    "sage": ("Sage", "OpenAI alias"),
    "verse": ("Verse", "OpenAI alias"),
    "ballad": ("Ballad", "OpenAI alias"),
    "juniper": ("Juniper", "OpenAI alias"),
}

_LANG_NAMES = {
    "ar": "Arabic", "de": "German", "es": "Spanish", "fr": "French",
    "hi": "Hindi", "it": "Italian", "nl": "Dutch", "pt": "Portuguese",
}


def _voxtral_voice_map() -> dict[str, str]:
    """Return the voice-name → mapped-voice dict from the voxtral-voices transform.

    Single source of truth lives in the voxtral-voices registry entry in
    gateway/transforms.py.  This accessor lets the voice listing endpoint
    read from that same source instead of a duplicate VOICE_MAP constant.
    """
    try:
        primitives = _TRANSFORM_REGISTRY.resolve("voxtral-voices")
        if primitives and isinstance(primitives[0], _ValueMapTransform):
            return dict(primitives[0]._map)
    except (KeyError, IndexError, AttributeError):
        pass
    return {}


def _voice_metadata(voice_id: str, mapped: str) -> dict:
    """Derive {name, language, gender, description} for a given voice id."""
    if voice_id in _OPENAI_VOICE_NAMES:
        display_name, _kind = _OPENAI_VOICE_NAMES[voice_id]
        # OpenAI alias -> mapped Voxtral voice. Gender from the *mapped* name.
        gender = "female" if "female" in mapped else ("male" if "male" in mapped else "unknown")
        return {
            "name": display_name,
            "language": "en",
            "gender": gender,
            "description": f"OpenAI '{voice_id}' alias (mapped to {mapped})",
        }
    parts = voice_id.split("_", 1)
    if len(parts) == 2 and parts[0] in _LANG_NAMES:
        lang_code, suffix = parts
        gender = "female" if "female" in suffix else ("male" if "male" in suffix else "unknown")
        return {
            "name": f"{_LANG_NAMES[lang_code]} {gender.capitalize()}",
            "language": lang_code,
            "gender": gender,
            "description": "Voxtral native multilingual voice",
        }
    # Bare English natives like "casual_male", "neutral_female", etc.
    gender = "female" if "female" in voice_id else ("male" if "male" in voice_id else "unknown")
    pretty = voice_id.replace("_", " ").title()
    return {
        "name": pretty,
        "language": "en",
        "gender": gender,
        "description": "Voxtral native English voice",
    }

OPENAI_STATIC_MODEL_CATEGORIES = {
    "dall-e-2": "images",
    "dall-e-3": "images",
    "tts-1": "speech",
    "tts-1-hd": "speech",
    "whisper-1": "transcriptions",
}

OPERATION_COMPATIBLE_CATEGORIES = {
    "chat": {"chat", "unknown"},
    "completions": {"chat", "unknown"},
    "embeddings": {"embeddings", "unknown"},
    "speech": {"speech", "unknown"},
    "transcriptions": {"transcriptions", "unknown"},
    "translations": {"transcriptions", "translations", "unknown"},
    "images": {"images", "unknown"},
    "image_edits": {"images", "unknown"},
    # `video` is the OpenAI category for sora-* models (see infer_openai_model_category).
    # Local Wan/LTX names without an OpenAI category fall through as "unknown".
    "videos": {"video", "unknown"},
}
OPENAI_PUBLIC_MODEL_OWNERS = {"openai", "openai-internal", "system"}


class ModelCompatibilityError(Exception):
    pass


@dataclass(frozen=True)
class ModelResolution:
    requested_model: str
    effective_model: str
    operation: str
    category: Optional[str] = None
    is_openai_alias: bool = False


def normalize_model_id(model_name: str) -> str:
    return (model_name or "").strip().lower()


def load_generated_openai_model_aliases() -> dict[str, str]:
    """Load generated OpenAI model IDs and their inferred categories."""
    if not OPENAI_MODEL_ALIASES_FILE.exists():
        return {}

    try:
        with open(OPENAI_MODEL_ALIASES_FILE) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[proxy] Warning: Failed to load OpenAI model aliases: {e}", flush=True)
        return {}

    aliases = {}
    models = data.get("models", []) if isinstance(data, dict) else []
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = normalize_model_id(item.get("id", ""))
        if model_id:
            aliases[model_id] = item.get("category") or "unknown"
    return aliases


def configured_model_alias_category(model_name: str) -> Optional[str]:
    if not isinstance(MODEL_COMPAT_ALIASES, dict):
        return None

    alias_cfg = MODEL_COMPAT_ALIASES.get(model_name) or MODEL_COMPAT_ALIASES.get(normalize_model_id(model_name))
    if isinstance(alias_cfg, dict):
        return alias_cfg.get("category") or alias_cfg.get("operation")
    if isinstance(alias_cfg, str):
        return alias_cfg
    return None


def infer_openai_model_category(model_name: str) -> Optional[str]:
    """Infer an OpenAI model family from stable naming conventions."""
    model_id = normalize_model_id(model_name)
    if not model_id:
        return None

    configured_category = configured_model_alias_category(model_id)
    if configured_category:
        return configured_category

    if model_id in OPENAI_STATIC_MODEL_CATEGORIES:
        return OPENAI_STATIC_MODEL_CATEGORIES[model_id]

    if model_id.startswith(("dall-e-", "gpt-image-")) or model_id.startswith("chatgpt-image"):
        return "images"

    if "embedding" in model_id:
        return "embeddings"

    if "transcribe" in model_id or "diarize" in model_id or model_id.startswith("whisper"):
        return "transcriptions"

    if model_id.startswith("tts-") or model_id.endswith("-tts") or "-tts-" in model_id:
        return "speech"

    if model_id.startswith("sora"):
        return "video"

    if "moderation" in model_id:
        return "moderation"

    if "deep-research" in model_id:
        return "deep_research"

    if model_id.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4", "codex-", "computer-use-")):
        if "realtime" in model_id or model_id.startswith("gpt-audio"):
            return "audio_chat"
        return "chat"

    generated_aliases = load_generated_openai_model_aliases()
    return generated_aliases.get(model_id)


def is_openai_model_alias(model_name: str) -> bool:
    return infer_openai_model_category(model_name) is not None


def default_model_for_operation(operation: str, requested_model: str = "") -> str:
    configured_default = MODEL_COMPAT_DEFAULTS.get(operation)
    if configured_default:
        return configured_default

    if operation in ("chat", "completions"):
        return DEFAULT_MODEL or requested_model
    if operation == "speech":
        return TTS_BACKEND_MODEL
    if operation == "transcriptions":
        return STT_BACKEND_MODEL
    if operation == "translations":
        return STT_BACKEND_MODEL
    if operation == "images":
        return COMFY_DEFAULT_MODEL
    if operation == "image_edits":
        return IMAGE_EDIT_DEFAULT_MODEL
    if operation == "videos":
        return VIDEO_DEFAULT_WORKFLOW
    if operation == "embeddings":
        return EMBEDDINGS_MODEL if EMBEDDINGS_ENABLED else ""
    return requested_model


def resolve_model_for_operation(operation: str, requested_model: str) -> ModelResolution:
    """Resolve public OpenAI model names to local operation defaults."""
    if not MODEL_COMPAT_ENABLED or not requested_model:
        return ModelResolution(
            requested_model=requested_model,
            effective_model=requested_model,
            operation=operation,
        )

    category = infer_openai_model_category(requested_model)
    if not category:
        return ModelResolution(
            requested_model=requested_model,
            effective_model=requested_model,
            operation=operation,
        )

    compatible_categories = OPERATION_COMPATIBLE_CATEGORIES.get(operation, set())
    if category not in compatible_categories:
        raise ModelCompatibilityError(
            f"Model '{requested_model}' is an OpenAI {category} model and cannot be used for {operation} requests."
        )

    effective_model = default_model_for_operation(operation, requested_model)
    if not effective_model:
        raise ModelCompatibilityError(
            f"No local default model is configured for {operation} requests."
        )

    return ModelResolution(
        requested_model=requested_model,
        effective_model=effective_model,
        operation=operation,
        category=category,
        is_openai_alias=True,
    )


def model_compatibility_error(message: str) -> Response:
    return Response(
        content=json.dumps({
            "error": {
                "message": message,
                "type": "invalid_request_error",
            }
        }),
        status_code=400,
        media_type="application/json",
    )


def openai_error_response(
    message: str,
    *,
    status_code: int = 400,
    error_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> Response:
    error: dict[str, Any] = {
        "message": message,
        "type": error_type,
    }
    if param is not None:
        error["param"] = param
    if code is not None:
        error["code"] = code
    return Response(
        content=json.dumps({"error": error}),
        status_code=status_code,
        media_type="application/json",
    )


def parse_openai_json_body(body: bytes) -> tuple[dict[str, Any] | None, Response | None]:
    try:
        body_json = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, openai_error_response(
            "Malformed JSON body.",
            status_code=400,
            error_type="invalid_request_error",
            code="invalid_json",
        )
    if not isinstance(body_json, dict):
        return None, openai_error_response(
            "JSON body must be an object.",
            status_code=400,
            error_type="invalid_request_error",
            code="invalid_request",
        )
    return body_json, None


def _service_model_records(service_cfg: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw_models = service_cfg.get("models")
    if isinstance(raw_models, Mapping):
        return [
            {"id": model_id, **dict(raw)}
            if isinstance(raw, Mapping)
            else {"id": model_id, "upstream_model": raw}
            for model_id, raw in raw_models.items()
        ]
    if isinstance(raw_models, list):
        return [raw for raw in raw_models if isinstance(raw, Mapping)]
    return []


def _backend_default_model_to_load(backend: str) -> Optional[str]:
    backend_cfg = (config.get("backends") or {}).get(backend, {})
    if not isinstance(backend_cfg, Mapping):
        return DEFAULT_MODEL if backend == "tabby" else None
    configured = str(
        backend_cfg.get("activation_model")
        or backend_cfg.get("model_id")
        or backend_cfg.get("default_model")
        or ""
    ).strip()
    if configured:
        return configured
    return DEFAULT_MODEL if backend == "tabby" else None


def _configured_litellm_local_model_target(
    requested_model: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return the local backend activation target for a LiteLLM model group.

    LiteLLM owns execution, but the gateway still owns local service readiness.
    This helper recognizes model groups generated from local service data and
    returns the backend/model/mmproj that must be hot before forwarding.
    Unknown model groups are left for LiteLLM to handle.
    """
    normalized = normalize_model_id(requested_model)
    backends = config.get("backends") if isinstance(config.get("backends"), Mapping) else {}

    for backend in LLM_BACKEND_HOSTS:
        service_cfg = backends.get(backend, {})
        if not isinstance(service_cfg, Mapping):
            continue

        service_model = str(service_cfg.get("model_id") or "").strip()
        if service_model and normalize_model_id(service_model) == normalized:
            return backend, _backend_default_model_to_load(backend), None

        for item in _service_model_records(service_cfg):
            # LiteLLM registers one public route name per deployment. Match
            # that exact name so unknown aliases do not trigger local backend
            # activation before LiteLLM rejects them.
            registered_name = str(
                item.get("model_group")
                or item.get("id")
                or item.get("upstream_model")
                or ""
            ).strip()
            if normalize_model_id(registered_name) != normalized:
                continue

            activation_model = str(
                item.get("activation_model")
                or item.get("model_path")
                or item.get("path")
                or ""
            ).strip()
            activation_mmproj = str(
                item.get("activation_mmproj")
                or item.get("mmproj")
                or ""
            ).strip()
            if backend == "tabby" and not activation_model:
                activation_model = str(item.get("upstream_model") or item.get("id") or "").strip()
            if backend == "kobold" and activation_model and not os.path.isabs(activation_model):
                activation_model = ""
            return backend, activation_model or None, activation_mmproj or None

    return None, None, None


def _litellm_activation_failure_response(
    *,
    requested_model: str,
    backend: str,
    target_model: Optional[str],
) -> Response:
    return Response(
        content=json.dumps({
            "error": {
                "message": (
                    f"Failed to satisfy request for model '{requested_model}': "
                    f"could not start backend '{backend}'"
                    + (f" or load model '{target_model}'." if target_model else ".")
                ),
                "type": "service_unavailable",
                "code": "backend_unavailable",
            }
        }),
        status_code=503,
        media_type="application/json",
        headers={"Retry-After": "10"},
    )


async def _ensure_litellm_local_backend_target(
    auth_context: AuthContext,
    *,
    requested_model: str,
    backend: Optional[str],
    target_model: Optional[str],
    target_mmproj: Optional[str] = None,
) -> Response | None:
    if not backend:
        return None
    if not profile_allows_backend(auth_context, backend):
        return _policy_denied_response(PolicyDecision(
            decision=DENIED,
            reasons=(f"backend {backend!r} not permitted by profile",),
        ))
    availability = _service_availability_response(backend, service_label=backend)
    if availability is not None:
        return availability
    if await ensure_target(backend, target_model, target_mmproj):
        return None
    return _litellm_activation_failure_response(
        requested_model=requested_model,
        backend=backend,
        target_model=target_model,
    )


async def _prepare_litellm_local_llm_request(
    auth_context: AuthContext,
    *,
    request_path: str,
    body_json: Mapping[str, Any],
) -> tuple[Response | None, Mapping[str, Any]]:
    """Apply gateway-owned local readiness before LiteLLM execution.

    Unknown model names are intentionally not rejected here: they may be model
    groups supplied by a non-local LiteLLM provider. Only gateway-declared local
    targets are activated.
    """
    body = copy.deepcopy(dict(body_json))
    model_name = body.get("model", "")
    if not isinstance(model_name, str) or not model_name:
        return openai_error_response(
            "Missing required parameter: 'model'.",
            status_code=400,
            param="model",
            code="missing_required_parameter",
        ), body

    if _profile_target_activation(auth_context) == "existing_only":
        return None, body

    target_backend, target_model, target_mmproj = (
        _configured_litellm_local_model_target(model_name)
    )

    vision_needed = (
        request_path == "/v1/chat/completions"
        and has_vision_content(body.get("messages"))
    )
    if vision_needed and not target_backend:
        active = await get_active_backend()
        # vision_capable_llm_backends() answers the membership question: which
        # backends in LLM_BACKEND_HOSTS carry the "vision" capability?  For
        # today's config this resolves to ["tabby"] when tabby has vision=True,
        # [] otherwise — exactly as the previous literal check.  The kobold
        # mmproj check is orchestration-level logic that stays unchanged.
        _vision_members = vision_capable_llm_backends()
        active_can_serve_vision = (
            active in _vision_members
            and profile_allows_backend(auth_context, active)
        ) or (
            active == "kobold"
            and profile_allows_backend(auth_context, "kobold")
            and kobold_has_mmproj() is True
        )
        if active_can_serve_vision:
            return None, body

    if vision_needed and target_backend:
        _vision_members = vision_capable_llm_backends()
        target_can_serve_vision = (
            target_backend in _vision_members
        ) or (
            target_backend == "kobold"
            and bool(target_mmproj)
        )
        if not target_can_serve_vision:
            return Response(
                content=json.dumps({
                    "error": {
                        "message": (
                            "Request includes images but the selected local model "
                            "group is not configured with a vision-capable target."
                        ),
                        "type": "invalid_request_error",
                        "code": "vision_unsupported",
                    }
                }),
                status_code=400,
                media_type="application/json",
                headers={"X-Vision-Routed": "false"},
            ), body

    if not target_backend:
        return None, body

    if request_path in ("/v1/chat/completions", "/v1/completions"):
        role_cfg = ROLES.get(model_name) if isinstance(ROLES, dict) else None
        if isinstance(role_cfg, Mapping):
            sampling = role_cfg.get("sampling")
            if isinstance(sampling, Mapping):
                for field, value in sampling.items():
                    if field not in body:
                        body[field] = value


    if request_path == "/v1/chat/completions":
        # Step 2: reasoning_effort handled by the reasoning-effort built-in.
        # Body mutation goes through the transform; 400 plumbing stays here.
        reasoning_error = _ReasoningEffortTransform().apply(body)
        if reasoning_error:
            return Response(
                content=json.dumps({"error": {
                    "message": reasoning_error,
                    "type": "invalid_request_error",
                    "param": "reasoning_effort",
                    "code": "invalid_value",
                }}),
                status_code=400,
                media_type="application/json",
            ), body

    response = await _ensure_litellm_local_backend_target(
        auth_context,
        requested_model=model_name,
        backend=target_backend,
        target_model=target_model,
        target_mmproj=target_mmproj,
    )
    return response, body


async def get_tabby_loaded_model_id() -> str:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{TABBY_HOST}/v1/model",
                headers={"x-api-key": API_KEY},
                timeout=HEALTH_CHECK_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json().get("id") or ""
    except Exception:
        pass
    return ""


async def get_kobold_model_id() -> str:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{KOBOLD_HOST}/v1/models", timeout=HEALTH_CHECK_TIMEOUT)
            if resp.status_code != 200:
                return ""
            data = resp.json().get("data", [])
            if data and isinstance(data[0], dict):
                return data[0].get("id") or ""
    except Exception:
        pass
    return ""


async def effective_llm_model_for_host(target_host: str) -> str:
    if target_host == KOBOLD_HOST:
        return await get_kobold_model_id()
    if target_host == TABBY_HOST:
        return await get_tabby_loaded_model_id() or DEFAULT_MODEL
    return ""


@dataclass(frozen=True)
class CurrentLoadedLlmTarget:
    backend: str
    host: str
    model: str


def _is_use_loaded_model_alias(value: Any) -> bool:
    return (
        isinstance(value, str)
        and normalize_model_id(value) in USE_LOADED_MODEL_ALIASES
    )


def _is_use_loaded_fast_model_alias(value: Any) -> bool:
    return (
        isinstance(value, str)
        and normalize_model_id(value) == USE_LOADED_FAST_MODEL_ALIAS
    )


async def current_llm_model_for_backend(backend: str) -> str:
    """Return the model actually loaded on a backend, never a configured fallback."""
    backend = str(backend or "").strip()
    if backend == "kobold":
        loaded_path = currently_loaded_kobold_model()
        if not loaded_path:
            return ""
        loaded_id = await get_kobold_model_id()
        return loaded_id or os.path.basename(loaded_path) or loaded_path
    if backend == "tabby":
        return await currently_loaded_tabby_model() or ""

    host = LLM_BACKEND_HOSTS.get(backend)
    backend_cfg = (config.get("backends") or {}).get(backend, {})
    if not host or not isinstance(backend_cfg, Mapping):
        return ""
    state_type = str(backend_cfg.get("state_type") or "openai_compatible")
    strategy = _BACKEND_STATE_STRATEGIES.get(state_type)
    if not strategy:
        return ""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
            resp = await client.get(f"{host}{strategy['endpoint']}")
        if resp.status_code != 200:
            return ""
        data = resp.json()
        if not isinstance(data, dict):
            return ""
        return _resolve_dot_path(data, str(strategy["model_key"])) or ""
    except Exception:
        return ""


async def _current_loaded_llm_target(
    auth_context: AuthContext,
) -> tuple[CurrentLoadedLlmTarget | None, Response | None]:
    active = await get_active_backend()
    if not active:
        return None, openai_error_response(
            "model 'current' requested, but no local LLM backend is active.",
            status_code=503,
            error_type="service_unavailable",
            code="current_model_unavailable",
        )
    if active not in LLM_BACKEND_HOSTS:
        return None, openai_error_response(
            f"model 'current' requested, but active backend {active!r} is not an OpenAI-compatible LLM backend.",
            status_code=503,
            error_type="service_unavailable",
            code="current_model_unavailable",
        )
    if not profile_allows_backend(auth_context, active):
        return None, _policy_denied_response(PolicyDecision(
            decision=DENIED,
            reasons=(f"backend {active!r} not permitted by profile",),
        ))
    availability = _service_availability_response(active, service_label=active)
    if availability is not None:
        return None, availability

    loaded_model = await current_llm_model_for_backend(active)
    if not loaded_model:
        return None, openai_error_response(
            f"model 'current' requested, but backend {active!r} has no loaded model.",
            status_code=503,
            error_type="service_unavailable",
            code="current_model_unavailable",
        )
    return CurrentLoadedLlmTarget(
        backend=active,
        host=LLM_BACKEND_HOSTS[active],
        model=loaded_model,
    ), None


async def refresh_openai_model_aliases() -> int:
    """Fetch available OpenAI model IDs and cache inferred categories locally."""
    api_key = os.environ.get("OPENAI_MODEL_DISCOVERY_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_MODEL_DISCOVERY_API_KEY or OPENAI_API_KEY to refresh model aliases.", file=sys.stderr)
        return 2

    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
        resp = await client.get(
            OPENAI_MODELS_API_URL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if resp.status_code != 200:
        print(f"OpenAI model refresh failed: HTTP {resp.status_code} {resp.text[:300]}", file=sys.stderr)
        return 1

    data = resp.json()
    model_entries = []
    for item in data.get("data", []):
        model_id = item.get("id", "")
        if not model_id:
            continue
        if not OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE:
            owned_by = item.get("owned_by", "")
            is_private_fine_tune = ":" in model_id or model_id.startswith("ft:")
            if owned_by not in OPENAI_PUBLIC_MODEL_OWNERS or is_private_fine_tune:
                continue
        model_entries.append({
            "id": model_id,
            "category": infer_openai_model_category(model_id) or "unknown",
            "created": item.get("created"),
            "owned_by": item.get("owned_by"),
        })

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": OPENAI_MODELS_API_URL,
        "models": sorted(model_entries, key=lambda entry: entry["id"]),
    }
    with open(OPENAI_MODEL_ALIASES_FILE, "w") as f:
        yaml.safe_dump(output, f, sort_keys=False)
    print(f"Wrote {len(model_entries)} OpenAI model aliases to {OPENAI_MODEL_ALIASES_FILE}.")
    return 0


# =============================================================================
# State management
# =============================================================================

class ProxyState:
    def __init__(self):
        self.last_activity = time.time()
        self.activity_lock = threading.Lock()

        self.model_loading = False
        self.model_unloading = False
        self.load_lock = asyncio.Lock()
        self.load_event: Optional[asyncio.Event] = None
        self.unload_event: Optional[asyncio.Event] = None

        self.load_failure_count = 0
        self.last_load_failure_time = 0
        self.last_load_error: Optional[str] = None
        # NOTE: no tabby_process field — the gateway does not own engine
        # processes (see the TABBY_MANAGED removal). Existence is the operator's
        # supervisor's job (systemd / docker / CommandDriver).
        self.shutdown_requested = False

    def update_activity(self):
        with self.activity_lock:
            self.last_activity = time.time()

    def get_idle_seconds(self) -> float:
        with self.activity_lock:
            return time.time() - self.last_activity

    def reset_load_failures(self):
        self.load_failure_count = 0
        self.last_load_failure_time = 0

    def record_load_failure(self):
        self.load_failure_count += 1
        self.last_load_failure_time = time.time()

    def can_attempt_load(self) -> bool:
        if self.load_failure_count >= MAX_LOAD_FAILURES:
            elapsed = time.time() - self.last_load_failure_time
            if elapsed < LOAD_FAILURE_BACKOFF * self.load_failure_count:
                return False
        return True


state = ProxyState()


# =============================================================================
# TabbyAPI subprocess management
# =============================================================================

# Tabby process spawn/stop/log/wait-for-ready removed. The gateway no longer
# owns tabby's existence — an operator who wants the gateway to *start* tabby
# declares a `command` (or systemd/docker) LifecycleDriver on the tabby service;
# crash-restart belongs to that supervisor. Readiness is observed via
# check_tabby_health() / the driver's ensure_ready(), never by spawning here.


async def probe_backend(
    host: str,
    port: int | None,
    path: str = "/v1/models",
    *,
    scheme: str = "http",
) -> dict[str, Any]:
    """Probe a backend's reachability for the admin "Test connection" control.

    Issues a GET to ``{scheme}://{host}:{port}{path}`` and reports the outcome
    without persisting anything. Returns ``{ok, status, error, url}`` where
    ``ok`` is true on any HTTP response < 500 (the endpoint answered), and
    ``error`` carries the real connection/transport error otherwise so the
    operator sees why it failed.
    """
    safe_path = path if path.startswith("/") else "/" + path
    authority = f"{host}:{port}" if port else str(host)
    url = f"{scheme}://{authority}{safe_path}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=HEALTH_CHECK_TIMEOUT)
        body: Any = None
        try:
            ct = resp.headers.get("content-type", "")
            if "json" in ct:
                body = resp.json()
        except Exception:  # noqa: BLE001 — non-JSON body is fine; body stays None
            pass
        return {
            "ok": resp.status_code < 500,
            "status": resp.status_code,
            "error": "" if resp.status_code < 500 else f"HTTP {resp.status_code}",
            "url": url,
            "body": body,
        }
    except Exception as exc:  # noqa: BLE001 — surface the real transport error
        return {"ok": False, "status": None, "error": str(exc) or type(exc).__name__, "url": url, "body": None}


async def check_service_health(service_id: str) -> bool:
    """Generic health probe driven by the registry-projected config["backends"] entry.

    Reads host, port, health_endpoint, and timeout from config["backends"][service_id].
    Default health_endpoint is "/health"; default timeout is HEALTH_CHECK_TIMEOUT.
    Returns False when the service_id is not present in config["backends"].
    """
    bcfg = (config.get("backends") or {}).get(service_id)
    if not isinstance(bcfg, Mapping):
        return False
    host = bcfg.get("host", "127.0.0.1")
    port = bcfg.get("port")
    if not port:
        return False
    endpoint = bcfg.get("health_endpoint", "/health")
    timeout = int(bcfg.get("timeout") or HEALTH_CHECK_TIMEOUT)
    # Cap the health-check timeout at HEALTH_CHECK_TIMEOUT so callers don't
    # accidentally inherit very long service timeouts (e.g. TTS: 86400 s).
    timeout = min(timeout, HEALTH_CHECK_TIMEOUT)
    url = f"http://{host}:{port}{endpoint}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=timeout)
            return resp.status_code == 200
    except Exception:
        return False


async def check_tabby_health() -> bool:
    """Check if TabbyAPI is healthy."""
    return await check_service_health("tabby")


async def check_embeddings_health() -> bool:
    """Check if the dedicated embeddings backend is healthy."""
    if not EMBEDDINGS_ENABLED:
        return False
    return await check_service_health("embeddings")


async def tabby_backend_healthy() -> bool:
    """Return True iff the tabby backend is reachable and healthy.

    The gateway no longer restarts engines — existence and crash-restart belong
    to the operator's supervisor (systemd / docker / CommandDriver). This is a
    pure health observation; callers decide what to do when it's False (e.g.
    return 503 and let the supervisor bring tabby back).
    """
    return await check_tabby_health()


# =============================================================================
# TTS Backend
# =============================================================================

async def check_tts_health() -> bool:
    """Check if TTS backend is healthy."""
    if not TTS_ENABLED:
        return False
    return await check_service_health("tts")


def convert_wav_to_mp3(wav_content: bytes) -> bytes:
    """Convert WAV bytes to MP3 bytes using the local ffmpeg binary."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as wav_file, tempfile.NamedTemporaryFile(suffix=".mp3") as mp3_file:
        wav_file.write(wav_content)
        wav_file.flush()
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                wav_file.name,
                "-codec:a",
                "libmp3lame",
                "-f",
                "mp3",
                mp3_file.name,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or "ffmpeg failed"
            raise RuntimeError(detail)
        return Path(mp3_file.name).read_bytes()


async def proxy_tts_request(request: Request, auth_context: AuthContext) -> Response:
    """Forward TTS request to TTS backend."""
    if not TTS_ENABLED:
        return openai_error_response(
            "TTS not enabled",
            status_code=503,
            error_type="service_unavailable",
        )
    service_response = _service_availability_response("tts", service_label="TTS")
    if service_response is not None:
        return service_response

    # Voice mapping is module-scoped (single source of truth shared with /v1/voices).
    body = await request.body()
    headers = prepare_downstream_headers(request.headers, auth_context, "tts")

    # Map voice in request body
    requested_response_format = "wav"
    try:
        body_json = json.loads(body)
        response_format = str(body_json.get("response_format", "") or "wav").lower()
        if response_format not in SUPPORTED_TTS_RESPONSE_FORMATS:
            return openai_error_response(
                "This gateway currently supports response_format='wav' or 'mp3' for speech synthesis.",
                status_code=400,
                param="response_format",
                code="unsupported_response_format",
            )
        requested_response_format = response_format

        requested_model = body_json.get("model", "tts-1")
        try:
            model_resolution = resolve_model_for_operation("speech", requested_model)
        except ModelCompatibilityError as e:
            return model_compatibility_error(str(e))

        requested_voice = body_json.get("voice", "").lower()
        body_json["voice"] = requested_voice  # normalise to lowercase before transform
        _run_request_pipeline(["voxtral-voices"], body_json, _TransformContext(), _TRANSFORM_REGISTRY)
        body_json["model"] = model_resolution.effective_model or TTS_BACKEND_MODEL
        body_json["response_format"] = "wav"
        body = json.dumps(body_json).encode()
        pop_header(headers, "content-type")
        headers["content-type"] = "application/json"
    except (json.JSONDecodeError, AttributeError):
        pass

    _tts_host = host_for_capability("audio.speech") or (TTS_HOST if TTS_ENABLED else None) or TTS_HOST
    url = f"{_tts_host}/v1/audio/speech"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TTS_TIMEOUT, connect=10)) as client:
            resp = await client.post(url, headers=headers, content=body)

            # TTS returns audio data, stream it back
            content = resp.content
            content_type = resp.headers.get("content-type", TTS_RESPONSE_MEDIA_TYPES["wav"])
            if 200 <= resp.status_code < 300 and requested_response_format == "mp3":
                try:
                    content = await asyncio.to_thread(convert_wav_to_mp3, resp.content)
                    content_type = TTS_RESPONSE_MEDIA_TYPES["mp3"]
                except Exception as e:
                    return Response(
                        content=json.dumps({"error": {"message": f"TTS audio conversion failed: {e}", "type": "api_error"}}),
                        status_code=502,
                        media_type="application/json",
                    )
            elif 200 <= resp.status_code < 300:
                content_type = TTS_RESPONSE_MEDIA_TYPES.get(requested_response_format, content_type)
            return Response(
                content=content,
                status_code=resp.status_code,
                media_type=content_type
            )

    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": {"message": "TTS request timeout", "type": "api_error"}}),
            status_code=504,
            media_type="application/json"
        )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": {"message": f"TTS backend unavailable at {_tts_host}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"TTS proxy error: {str(e)}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )


# =============================================================================
# STT Backend
# =============================================================================

async def check_stt_health() -> bool:
    """Check if STT backend is healthy."""
    if not STT_ENABLED:
        return False
    return await check_service_health("stt")


def stt_backend_supports_translation() -> bool:
    stt_backend = config.get("backends", {}).get("stt", {})
    capabilities = stt_backend.get("capabilities", []) if isinstance(stt_backend, dict) else []
    backend_caps = {str(cap).lower() for cap in capabilities if isinstance(cap, str)}
    return bool({"translation", "translations"} & backend_caps)


def audio_translation_unsupported_response() -> Response:
    return openai_error_response(
        (
            "Audio translation is not supported by the configured local STT backend. "
            "This gateway will not return transcription output from the translations route."
        ),
        status_code=400,
        error_type="invalid_request_error",
        param="file",
        code="unsupported_audio_translation",
    )


async def proxy_stt_request(
    request: Request,
    auth_context: AuthContext,
    *,
    operation: str = "transcriptions",
) -> Response:
    """Forward STT request to STT backend."""
    if not STT_ENABLED:
        return openai_error_response(
            "STT not enabled",
            status_code=503,
            error_type="service_unavailable",
        )
    service_response = _service_availability_response("stt", service_label="STT")
    if service_response is not None:
        return service_response
    if operation == "translations" and not stt_backend_supports_translation():
        return audio_translation_unsupported_response()
    if operation not in {"transcriptions", "translations"}:
        return openai_error_response(
            f"Unsupported STT operation: {operation}",
            status_code=400,
            code="unsupported_stt_operation",
        )

    headers = prepare_downstream_headers(request.headers, auth_context, "stt")

    _stt_capability = f"audio.{operation}"  # audio.transcriptions or audio.translations
    _stt_host = host_for_capability(_stt_capability) or (STT_HOST if STT_ENABLED else None) or STT_HOST
    url = f"{_stt_host}/v1/audio/{operation}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(STT_TIMEOUT, connect=10)) as client:
            if "multipart/form-data" in request.headers.get("content-type", ""):
                form = await request.form()
                if "file" not in form:
                    return openai_error_response(
                        "No file part in the request.",
                        status_code=400,
                        param="file",
                        code="missing_file",
                    )
                requested_model = str(form.get("model", "whisper-1"))
                try:
                    model_resolution = resolve_model_for_operation(operation, requested_model)
                except ModelCompatibilityError as e:
                    return model_compatibility_error(str(e))

                data = {}
                files = []
                for name, value in form.multi_items():
                    if name == "model":
                        data[name] = model_resolution.effective_model or STT_BACKEND_MODEL
                    elif hasattr(value, "filename") and hasattr(value, "read"):
                        file_content = await value.read()
                        files.append((
                            name,
                            (
                                value.filename or "audio",
                                file_content,
                                value.content_type or "application/octet-stream",
                            ),
                        ))
                    else:
                        data[name] = str(value)

                if "model" not in data:
                    data["model"] = STT_BACKEND_MODEL
                if not files:
                    return openai_error_response(
                        "No file part in the request.",
                        status_code=400,
                        param="file",
                        code="missing_file",
                    )

                pop_header(headers, "content-type")
                pop_header(headers, "content-length")
                def send_multipart_stt_request():
                    with httpx.Client(timeout=httpx.Timeout(STT_TIMEOUT, connect=10)) as sync_client:
                        return sync_client.post(url, headers=headers, data=data, files=files)

                resp = await asyncio.to_thread(send_multipart_stt_request)
            else:
                return openai_error_response(
                    "No file part in the request.",
                    status_code=400,
                    param="file",
                    code="missing_file",
                )

            content_type = resp.headers.get("content-type", "application/json")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=content_type
            )

    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": {"message": "STT request timeout", "type": "api_error"}}),
            status_code=504,
            media_type="application/json"
        )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": {"message": f"STT backend unavailable at {_stt_host}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"STT proxy error: {str(e)}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )


# =============================================================================
# ComfyUI Backend (Image Generation)
# =============================================================================

_UI_WORKFLOW_CACHE: dict[str, Optional[dict]] = {}
_UI_WORKFLOW_MISSING_WARNED: set[str] = set()


def _load_ui_workflow(api_path: Path) -> Optional[dict]:
    """Load a UI-format workflow JSON sibling to the given API workflow.

    Looks for `<name>.ui.json` next to `<name>.json`. Returns the parsed dict
    if found, else None. Results are cached by absolute path. Logs a one-shot
    warning per alias when a UI twin is missing so outputs lack the embedded
    workflow PNGInfo/mp4 metadata that ComfyUI's frontend uses to re-open them.
    """
    api_path = Path(api_path).resolve()
    key = str(api_path)
    if key in _UI_WORKFLOW_CACHE:
        return _UI_WORKFLOW_CACHE[key]
    ui_path = api_path.with_name(api_path.stem + ".ui.json")
    if not ui_path.exists():
        if key not in _UI_WORKFLOW_MISSING_WARNED:
            _UI_WORKFLOW_MISSING_WARNED.add(key)
            print(
                f"[proxy] no UI workflow twin at {ui_path}; outputs will lack "
                f"embedded workflow metadata (cannot be re-opened in ComfyUI frontend)",
                flush=True,
            )
        _UI_WORKFLOW_CACHE[key] = None
        return None
    try:
        with open(ui_path) as f:
            ui = json.load(f)
        _UI_WORKFLOW_CACHE[key] = ui
        return ui
    except Exception as e:
        print(f"[proxy] failed to read UI workflow {ui_path}: {e}", flush=True)
        _UI_WORKFLOW_CACHE[key] = None
        return None


def _build_comfy_submit_payload(workflow: dict, **extra: Any) -> dict:
    """Build the JSON body for POST /prompt, including extra_pnginfo when available.

    Pops the proxy-private `__ui_workflow__` key off `workflow` (added by the
    workflow loaders) and threads it into `extra_data.extra_pnginfo.workflow`.
    Without this, ComfyUI saves outputs without the UI-format graph in PNGInfo,
    so users can't drag the resulting mp4/PNG into ComfyUI's frontend to
    re-open the generation.
    """
    ui = workflow.pop("__ui_workflow__", None)
    payload = {"prompt": workflow}
    if ui:
        payload["extra_data"] = {"extra_pnginfo": {"workflow": ui}}
    payload.update(extra)
    return payload


def load_comfy_workflow(model_name: str) -> Optional[dict]:
    """Load a ComfyUI workflow template by model name.

    Attaches the UI-format twin (`<model>.ui.json`) under the private
    `__ui_workflow__` key when present, so submit sites can include it in
    `extra_data.extra_pnginfo.workflow`. The key survives deepcopy and is
    popped by `_build_comfy_submit_payload`.
    """
    workflow_path = COMFY_WORKFLOWS_DIR / f"{model_name}.json"
    if not workflow_path.exists():
        workflow_path = _comfy_workflow_artifact_path_for_model(model_name)
    if not workflow_path.exists():
        return None
    workflow = _load_comfy_workflow_file(workflow_path)
    if workflow is None:
        return None
    return workflow


# Cache for object_info slot definitions and for derived UI graphs (keyed by a
# hash of the API graph, so it survives across requests but updates if a workflow
# file changes).
_OBJECT_INFO_CACHE: dict[str, Optional[dict]] = {}
_DERIVED_UI_CACHE: dict[str, Optional[dict]] = {}


def _comfy_object_info(class_type: str) -> Optional[dict]:
    """Fetch (and cache) a node type's slot definitions from ComfyUI /object_info.

    Used to derive a UI-format graph from an API graph. Returns None on any error
    (ComfyUI down, unknown node) so the caller falls back gracefully.
    """
    if class_type in _OBJECT_INFO_CACHE:
        return _OBJECT_INFO_CACHE[class_type]
    info = None
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=3)) as client:
            resp = client.get(f"{COMFY_HOST}/object_info/{class_type}")
        if resp.status_code == 200:
            info = resp.json().get(class_type)
    except Exception:
        info = None
    _OBJECT_INFO_CACHE[class_type] = info
    return info


def _derive_ui_workflow(api: Mapping[str, Any]) -> Optional[dict]:
    """Derive a UI-format ComfyUI graph from an API-format workflow.

    So generated images embed the 'workflow' PNG chunk (re-openable in the
    ComfyUI frontend) even when no hand-authored `<name>.ui.json` twin exists.
    Uses /object_info for each node's input/output slot ordering. Best-effort:
    returns None if ComfyUI is unreachable or any node type is unknown, in which
    case the caller simply omits the chunk (status quo, no regression).
    """
    api = {k: v for k, v in api.items() if not (isinstance(k, str) and k.startswith("_"))}
    try:
        ids = sorted(api.keys(), key=lambda x: int(x))
    except (TypeError, ValueError):
        return None
    if not ids:
        return None

    def input_order(ct: str) -> Optional[list]:
        oi = _comfy_object_info(ct)
        if not oi:
            return None
        req = list(oi.get("input", {}).get("required", {}).keys())
        opt = list(oi.get("input", {}).get("optional", {}).keys())
        return req + opt

    def output_types(ct: str) -> list:
        oi = _comfy_object_info(ct)
        if not oi:
            return []
        names = oi.get("output_name") or oi.get("output") or []
        types = oi.get("output") or []
        return list(zip(names, types)) if names else [(t, t) for t in types]

    nodes = []
    node_by_id: dict[int, dict] = {}
    for i, nid in enumerate(ids):
        spec = api[nid]
        ct = spec.get("class_type")
        if not ct:
            return None
        oi = input_order(ct)
        if oi is None:
            return None  # unknown node -> bail, omit chunk rather than emit a broken graph
        ins = spec.get("inputs", {}) or {}
        ui_inputs, widget_vals = [], []
        for name in oi:
            v = ins.get(name, None)
            ui_inputs.append({"name": name, "localized_name": name, "type": "*",
                              "link": None, "widget": {"name": name}})
            if not (isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)) and v is not None:
                widget_vals.append(v)
        ui_outputs = [{"name": (n or t), "localized_name": (n or t), "type": t,
                       "links": [], "slot_index": j}
                      for j, (n, t) in enumerate(output_types(ct))]
        node = {"id": int(nid), "type": ct,
                "pos": [100 + (i % 4) * 340, 80 + (i // 4) * 240],
                "size": [300, 120], "flags": {}, "order": i, "mode": 0,
                "inputs": ui_inputs, "outputs": ui_outputs,
                "properties": {"Node name for S&R": ct}, "widgets_values": widget_vals}
        nodes.append(node)
        node_by_id[int(nid)] = node

    links = []
    lid = 0
    for nid in ids:
        spec = api[nid]
        ct = spec.get("class_type")
        oi = input_order(ct) or []
        tnode = node_by_id[int(nid)]
        for name, v in (spec.get("inputs", {}) or {}).items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                from_id, from_slot = int(v[0]), int(v[1])
                tslot = oi.index(name) if name in oi else 0
                lid += 1
                fnode = node_by_id.get(from_id)
                ltype = (fnode["outputs"][from_slot]["type"]
                         if fnode and from_slot < len(fnode["outputs"]) else "*")
                links.append([lid, from_id, from_slot, int(nid), tslot, ltype])
                if tslot < len(tnode["inputs"]):
                    tnode["inputs"][tslot]["link"] = lid
                if fnode and from_slot < len(fnode["outputs"]):
                    fnode["outputs"][from_slot]["links"].append(lid)

    return {"id": "00000000-0000-0000-0000-000000000000", "revision": 0,
            "last_node_id": max(int(i) for i in ids), "last_link_id": lid,
            "nodes": nodes, "links": links, "groups": [], "config": {},
            "extra": {}, "version": 0.4}


def _load_comfy_workflow_file(workflow_path: Path) -> Optional[dict]:
    with open(workflow_path) as f:
        workflow = json.load(f)
    errors = validate_comfyui_workflow_graph(workflow)
    if errors:
        print(
            f"[proxy] invalid ComfyUI workflow {workflow_path}: {'; '.join(errors)}",
            flush=True,
        )
        return None
    # Prefer a hand-authored UI twin (nice layout); otherwise derive one on the fly
    # from the API graph so generated images STILL embed the re-openable workflow
    # chunk. Works for any workflow, current or future, with no twin file needed.
    ui = _load_ui_workflow(workflow_path)
    if ui is None:
        ui = _derive_ui_workflow(workflow)
    if ui is not None:
        workflow["__ui_workflow__"] = ui
    return workflow


def _comfy_workflow_artifact_path_for_model(model_name: str) -> Path:
    try:
        registry = GATEWAY_WORKFLOW_REGISTRY
    except NameError:
        return COMFY_WORKFLOWS_DIR / f"{model_name}.json"
    for record in registry.records():
        bindings = record.manifest.get("bindings")
        if not isinstance(bindings, Mapping):
            continue
        comfy = bindings.get("comfyui")
        if not isinstance(comfy, Mapping):
            continue
        artifact = comfy.get("workflow")
        if not isinstance(artifact, str) or not artifact:
            continue
        if Path(artifact.replace("package://", "")).stem != model_name:
            continue
        return _resolve_workflow_artifact_path(record, artifact)
    return COMFY_WORKFLOWS_DIR / f"{model_name}.json"


def _resolve_workflow_artifact_path(record: Any, artifact: str) -> Path:
    if artifact.startswith("package://"):
        artifact = artifact.removeprefix("package://").lstrip("/")
    path = Path(artifact)
    if path.is_absolute():
        return path

    project_relative = (Path(__file__).resolve().parent / path).resolve()
    if project_relative.exists():
        return project_relative

    source_path = Path(getattr(record, "source_path", "") or "")
    if str(source_path):
        base = source_path if source_path.is_dir() else source_path.parent
        return (base / path).resolve()
    return project_relative


async def check_comfy_health() -> bool:
    """Check if ComfyUI backend is healthy."""
    if not COMFY_ENABLED:
        return False
    return await check_service_health("comfyui")


def parse_size(size_str: str) -> tuple[int, int]:
    """Parse OpenAI size string like '1024x1024' to (width, height)."""
    if not size_str or "x" not in size_str:
        return 1024, 1024
    try:
        w, h = size_str.lower().split("x")
        return int(w), int(h)
    except:
        return 1024, 1024


# =============================================================================
# Image cache, signed URLs, and adaptive batching (Fix 1 + Fix 2)
# =============================================================================
# Single source of truth for everything in the image hot path that needs to
# survive restarts: VRAM-per-(workflow,size) measurements and on-disk PNG bytes.
# Implementation rules:
#   - No silent downgrades: clients that ask for `response_format=url` get URLs
#     they can dereference (signed, bearerless, and time-limited); clients that
#     ask for `n=4` get 4 images (or a 503 with Retry-After if VRAM can't fit
#     even one).
#   - VRAM probe is a *measurement*, not a guess: we read free VRAM from
#     ComfyUI's /system_stats before/after a single-image run and persist
#     the per-image cost to disk so subsequent restarts inherit the cache.

_vram_estimates_lock = threading.Lock()
# Schema v3 entry shape:
#   {
#     "peak_bytes": int,            # == marginal_bytes; kept for status-page compat
#     "marginal_bytes": int,        # comfy_used_peak - comfy_used_before (or - baseline)
#     "comfy_baseline_bytes": int,  # min ComfyUI per-process VRAM in the 2s pre-run window
#     "model_state": "warm" | "cold",
#     "samples_count": int,
#     "trusted": bool,
#     "measured_at": int (unix s),
#     "source": "probe" | "probe_unloaded" | "endpoint_diff",
#   }
#
# Schema v2 (legacy) stored `peak_bytes = vram_total - min(vram_free)` — i.e.
# whole-GPU peak occupancy during the run. That captured voxtral, desktop, and
# the resident ComfyUI baseline in addition to flux's actual contribution,
# yielding numbers ~4x too high. v2 entries are migrated to v3 on read but
# marked trusted=False so the batcher ignores them and re-measures.
#
# Schema v1 (older) was a flat dict {key: int_bytes} where the int was
# `free_before - free_after_round` — the residual, not the peak. Also migrated
# to v3 untrusted.
_VRAM_ESTIMATES_SCHEMA_VERSION = 3
_VRAM_LEGACY_TRUST_FLOOR = 64 * 1024 * 1024  # 64 MiB
# Sanity bounds for marginal measurements. Anything outside is rejected and
# logged loudly — it indicates a probe bug or extraordinary external pressure
# (concurrent allocation by another process race-conditioning the diff).
_VRAM_MARGINAL_MIN_BYTES = 100 * 1024 * 1024       # 100 MiB
_VRAM_MARGINAL_MAX_BYTES = 20 * 1024 * 1024 * 1024  # 20 GiB
# Cold-path detection threshold: if ComfyUI's per-process VRAM rose by more
# than this between the 2s pre-run baseline and the moment of submit, treat
# the run as a cold (first-load) measurement and use `peak - baseline` as the
# marginal cost instead of `peak - before`.
_VRAM_COLD_LOAD_THRESHOLD_BYTES = 3 * 1024 * 1024 * 1024  # 3 GiB
_vram_estimates_cache: dict[str, dict] = {}
_vram_estimates_loaded = False
_image_url_signing_secret: Optional[bytes] = None


def _normalize_vram_entry(value) -> Optional[dict]:
    """Coerce a raw cache value (legacy int, v2 dict, or v3 dict) into v3 shape.

    Legacy v1 ints (`free_before - free_after`) and v2 dicts (`total - min_free`,
    i.e. whole-GPU peak occupancy) record the wrong semantics for the batcher.
    Both are migrated forward with trusted=False so the cache surfaces them on
    the status page but the batcher re-measures on the next run.
    """
    if isinstance(value, (int, float)):
        peak = int(value)
        if peak < _VRAM_LEGACY_TRUST_FLOOR:
            peak = 0
        return {
            "peak_bytes": peak,
            "marginal_bytes": peak,
            "comfy_baseline_bytes": 0,
            "model_state": "unknown",
            "samples_count": 0,
            "trusted": False,
            "measured_at": 0,
            "source": "endpoint_diff_v1",
        }
    if isinstance(value, dict):
        try:
            peak = int(value.get("peak_bytes", 0))
        except (TypeError, ValueError):
            peak = 0
        # Detect schema version by the presence of v3-only fields.
        has_v3 = "marginal_bytes" in value and "model_state" in value
        if has_v3:
            try:
                marginal = int(value.get("marginal_bytes", 0))
            except (TypeError, ValueError):
                marginal = 0
            try:
                baseline = int(value.get("comfy_baseline_bytes", 0))
            except (TypeError, ValueError):
                baseline = 0
            try:
                samples = int(value.get("samples_count", 0))
            except (TypeError, ValueError):
                samples = 0
            state = str(value.get("model_state", "unknown"))
            trusted = bool(value.get("trusted", False))
            source = str(value.get("source", "unknown"))
        else:
            # v2 entry: peak_bytes was whole-GPU peak. Force untrusted so the
            # batcher won't plan against the old absolute semantics.
            marginal = peak
            baseline = 0
            samples = 0
            state = "unknown"
            trusted = False
            source = "v2_absolute"
        return {
            "peak_bytes": marginal if has_v3 else peak,
            "marginal_bytes": marginal,
            "comfy_baseline_bytes": baseline,
            "model_state": state,
            "samples_count": samples,
            "trusted": trusted,
            "measured_at": int(value.get("measured_at", 0) or 0),
            "source": source,
        }
    return None


def _load_vram_estimates() -> dict[str, dict]:
    """Lazy-load the on-disk VRAM-per-image estimates (schema v2)."""
    global _vram_estimates_loaded
    with _vram_estimates_lock:
        if _vram_estimates_loaded:
            return _vram_estimates_cache
        if VRAM_ESTIMATES_FILE.exists():
            try:
                raw = json.loads(VRAM_ESTIMATES_FILE.read_text())
                entries_blob = None
                if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                    # v2 wrapped shape.
                    entries_blob = raw.get("entries")
                elif isinstance(raw, dict):
                    # v1 flat shape — migrate.
                    entries_blob = raw
                if isinstance(entries_blob, dict):
                    for k, v in entries_blob.items():
                        if not isinstance(k, str):
                            continue
                        entry = _normalize_vram_entry(v)
                        if entry is not None:
                            _vram_estimates_cache[k] = entry
            except Exception as e:
                print(f"[proxy] Could not load VRAM estimates ({VRAM_ESTIMATES_FILE}): {e}", flush=True)
        _vram_estimates_loaded = True
        return _vram_estimates_cache


def _persist_vram_estimates() -> None:
    with _vram_estimates_lock:
        try:
            VRAM_ESTIMATES_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": _VRAM_ESTIMATES_SCHEMA_VERSION,
                "entries": _vram_estimates_cache,
            }
            VRAM_ESTIMATES_FILE.write_text(json.dumps(payload, indent=2))
        except Exception as e:
            print(f"[proxy] Could not persist VRAM estimates: {e}", flush=True)


def _vram_cache_key(workflow_name: str, width: int, height: int) -> str:
    return f"{workflow_name}:{width}x{height}"


def get_vram_estimate(workflow_name: str, width: int, height: int) -> Optional[int]:
    """Return cached VRAM-per-image bytes for this (workflow, size), or None.

    Only returns trusted measurements. Untrusted (legacy or probe-fallback)
    entries cause the caller to re-measure on next run instead of planning
    capacity against bad data.
    """
    cache = _load_vram_estimates()
    entry = cache.get(_vram_cache_key(workflow_name, width, height))
    if not isinstance(entry, dict):
        return None
    if not entry.get("trusted"):
        return None
    peak = entry.get("peak_bytes")
    return int(peak) if isinstance(peak, int) and peak > 0 else None


def record_vram_estimate(
    workflow_name: str,
    width: int,
    height: int,
    bytes_per_image: int,
    *,
    trusted: bool = True,
    source: str = "probe",
    model_state: str = "unknown",
    comfy_baseline_bytes: int = 0,
    samples_count: int = 0,
) -> None:
    """Persist a measured VRAM-per-image cost so restarts inherit the calibration.

    ``bytes_per_image`` is flux's *marginal* per-image contribution (the
    per-image slice of ``comfy_used_peak - comfy_used_before`` or
    ``comfy_used_peak - comfy_baseline``), NOT whole-GPU peak occupancy. This
    is insensitive to concurrent voxtral / desktop / TTS activity because the
    sample stream is per-process (filtered to ComfyUI PID(s) on GPU0 via
    ``nvidia-smi --query-compute-apps``).

    We keep a lower-bound (the measured marginal), since under-estimating
    leads to OOM. Adopt a 10% safety margin baked into the stored value when
    trusted.

    Trusted measurements come from the in-run per-process probe. Untrusted
    measurements (probe failed, sanity bounds violated, fell back to endpoint
    diff) are persisted with trusted=False so the status page can surface
    them but the batcher won't plan against them.
    """
    if bytes_per_image <= 0:
        return
    # Hard sanity gate for trusted writes. Loud rejection — caller should see
    # this in journal and investigate. Untrusted writes bypass the bound check
    # (they're informational only).
    if trusted and (
        bytes_per_image < _VRAM_MARGINAL_MIN_BYTES
        or bytes_per_image > _VRAM_MARGINAL_MAX_BYTES
    ):
        print(
            f"[proxy] record_vram_estimate: REJECTING out-of-bounds marginal "
            f"for {workflow_name} {width}x{height}: "
            f"{bytes_per_image / (1024**2):.1f} MiB "
            f"(allowed range "
            f"{_VRAM_MARGINAL_MIN_BYTES / (1024**2):.0f} MiB .. "
            f"{_VRAM_MARGINAL_MAX_BYTES / (1024**3):.0f} GiB). "
            f"Recording untrusted.",
            flush=True,
        )
        trusted = False
        source = source + "_oob"
    safe_value = int(bytes_per_image * 1.1) if trusted else int(bytes_per_image)
    key = _vram_cache_key(workflow_name, width, height)
    cache = _load_vram_estimates()
    prior = cache.get(key) if isinstance(cache.get(key), dict) else None
    prior_peak = int(prior.get("peak_bytes", 0)) if prior else 0
    prior_trusted = bool(prior.get("trusted")) if prior else False
    # Only let a trusted measurement overwrite a prior trusted one. An
    # untrusted measurement never displaces a trusted one. When both are
    # trusted, keep the larger value — never under-estimate.
    if trusted:
        new_peak = max(safe_value, prior_peak) if prior_trusted else safe_value
        new_trusted = True
    else:
        if prior_trusted:
            return
        new_peak = safe_value
        new_trusted = False
    cache[key] = {
        "peak_bytes": new_peak,
        "marginal_bytes": new_peak,
        "comfy_baseline_bytes": int(comfy_baseline_bytes or 0),
        "model_state": str(model_state or "unknown"),
        "samples_count": int(samples_count or 0),
        "trusted": new_trusted,
        "measured_at": int(time.time()),
        "source": source,
    }
    _persist_vram_estimates()


async def comfy_vram_snapshot(
    client: httpx.AsyncClient,
) -> tuple[Optional[int], Optional[int]]:
    """Return (free, total) VRAM bytes on GPU0 from ComfyUI /system_stats."""
    try:
        resp = await client.get(f"{COMFY_HOST}/system_stats", timeout=HEALTH_CHECK_TIMEOUT)
        if resp.status_code != 200:
            return None, None
        payload = resp.json()
        devices = payload.get("devices") or []
        if not devices:
            return None, None
        # GPU0 is the inference card per the project's NAS+RTX layout.
        free = devices[0].get("vram_free")
        total = devices[0].get("vram_total")
        free_i = int(free) if isinstance(free, (int, float)) else None
        total_i = int(total) if isinstance(total, (int, float)) else None
        return free_i, total_i
    except Exception as e:
        print(f"[proxy] comfy_vram_snapshot: {e}", flush=True)
        return None, None


async def comfy_free_vram_bytes(client: httpx.AsyncClient) -> Optional[int]:
    """Backward-compatible: just the free-VRAM read."""
    free, _ = await comfy_vram_snapshot(client)
    return free


# Sample cadence for the in-run VRAM probe. Spec floor is 250ms; default 500ms.
_VRAM_PROBE_INTERVAL_S = 0.5
_VRAM_PROBE_INTERVAL_MIN_S = 0.25
# How long the "pre-run baseline" window stays open after probe start. Used to
# compute ``comfy_baseline_bytes`` = min of ComfyUI per-process VRAM in this
# window. With 500 ms cadence we get ~4 samples in 2 s.
_VRAM_BASELINE_WINDOW_S = 2.0

# GPU0 is the inference card (per the project's NAS+RTX layout). nvidia-smi
# exposes UUIDs in its ``--query-compute-apps`` output; we filter by GPU0's
# UUID so that any GPU1 (LLM card) allocations from the same process are
# correctly excluded.
_gpu0_uuid_cache: Optional[str] = None
_gpu0_uuid_lock = threading.Lock()


def _resolve_gpu0_uuid() -> Optional[str]:
    """Return the GPU0 UUID string from ``nvidia-smi``. Cached after first call."""
    global _gpu0_uuid_cache
    with _gpu0_uuid_lock:
        if _gpu0_uuid_cache is not None:
            return _gpu0_uuid_cache or None
        try:
            p = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,uuid",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except Exception as e:  # FileNotFoundError, TimeoutExpired, etc.
            print(f"[proxy] _resolve_gpu0_uuid: nvidia-smi failed: {e}", flush=True)
            _gpu0_uuid_cache = ""
            return None
        if p.returncode != 0:
            _gpu0_uuid_cache = ""
            return None
        for line in p.stdout.strip().splitlines():
            parts = [x.strip() for x in line.split(",", 1)]
            if len(parts) == 2 and parts[0] == "0":
                _gpu0_uuid_cache = parts[1]
                return parts[1]
        _gpu0_uuid_cache = ""
        return None


def _resolve_comfy_pids() -> set[int]:
    """Identify all ComfyUI processes by scanning /proc.

    A process is considered "ComfyUI" if its cmdline contains ``main.py`` AND
    its cwd resolves to a path containing ``ComfyUI``. Then we expand the set
    to include all descendant tasks via /proc/<pid>/task/*/children, because
    ComfyUI occasionally spawns short-lived worker subprocesses that hold
    their own VRAM allocations.

    Returns an empty set on any error (callers handle gracefully).
    """
    roots: set[int] = set()
    try:
        proc_entries = os.listdir("/proc")
    except Exception:
        return roots
    for name in proc_entries:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmdline = f.read().decode("utf-8", errors="replace").split("\x00")
            if not any("main.py" in part for part in cmdline if part):
                continue
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except Exception:
            continue
        if "ComfyUI" in cwd:
            roots.add(pid)
    if not roots:
        return roots
    # Walk descendants. /proc/<pid>/task/<tid>/children lists immediate child
    # PIDs whitespace-separated. We BFS to catch grandchildren too.
    all_pids = set(roots)
    frontier = list(roots)
    while frontier:
        pid = frontier.pop()
        try:
            tasks = os.listdir(f"/proc/{pid}/task")
        except Exception:
            continue
        for tid in tasks:
            try:
                with open(f"/proc/{pid}/task/{tid}/children", "r") as f:
                    children_raw = f.read().split()
            except Exception:
                continue
            for child in children_raw:
                if not child.isdigit():
                    continue
                cpid = int(child)
                if cpid not in all_pids:
                    all_pids.add(cpid)
                    frontier.append(cpid)
    return all_pids


def _sample_comfy_vram_bytes(comfy_pids: set[int], gpu0_uuid: Optional[str]) -> Optional[int]:
    """Return ComfyUI's per-process VRAM occupancy on GPU0, in bytes.

    Sums ``used_memory`` rows from ``nvidia-smi --query-compute-apps`` where
    the PID is in ``comfy_pids`` and (if ``gpu0_uuid`` is known) the gpu_uuid
    matches GPU0. Returns None on nvidia-smi failure so the probe can flag
    the run as untrusted.
    """
    try:
        p = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,gpu_uuid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception as e:
        print(f"[proxy] _sample_comfy_vram_bytes: nvidia-smi failed: {e}", flush=True)
        return None
    if p.returncode != 0:
        return None
    total_mb = 0
    matched = False
    for line in p.stdout.strip().splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            used_mb = int(parts[2])
        except ValueError:
            continue
        if pid not in comfy_pids:
            continue
        if gpu0_uuid and parts[1] != gpu0_uuid:
            continue
        total_mb += used_mb
        matched = True
    if not matched:
        # ComfyUI is in our PID set but holds no compute context on GPU0 yet.
        # That's a legitimate 0 (model unloaded), not a failure.
        return 0
    return total_mb * 1024 * 1024


class _VramProbe:
    """Tracks ComfyUI's per-process VRAM contribution across a single round.

    Marginal cost semantics (replacing the older whole-GPU ``total - min_free``):

      - ``comfy_used_before``    = first sample, taken before the /prompt POST.
      - ``comfy_used_baseline``  = min comfy_used during the first ~2 s window.
      - ``comfy_used_peak``      = max comfy_used across all samples.

    Marginal = ``peak - before`` for a warm run. When the run is cold (the
    model loaded for the first time), ``before`` already includes the loaded
    weights, so the marginal underestimates the real round cost; we detect
    this via ``before - baseline > _VRAM_COLD_LOAD_THRESHOLD_BYTES`` and use
    ``peak - baseline`` instead.

    Per-process sampling makes the measurement insensitive to concurrent
    voxtral / desktop / TTS activity — those PIDs are filtered out of the
    ``nvidia-smi --query-compute-apps`` row stream. The legacy ``total -
    min_free`` figure is also retained (as ``system_peak_used_bytes``) so the
    status page can still report whole-GPU pressure.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        interval: float = _VRAM_PROBE_INTERVAL_S,
    ) -> None:
        self._client = client
        self._interval = max(_VRAM_PROBE_INTERVAL_MIN_S, float(interval))
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._gpu0_uuid: Optional[str] = None
        self._comfy_pids: set[int] = set()
        self._t_start: float = 0.0
        self.samples = 0
        # Whole-GPU view (legacy): retained for "system pressure" reporting.
        self.min_free: Optional[int] = None
        self.total: Optional[int] = None
        # Per-ComfyUI-process view: the new source of truth for the batcher.
        self.comfy_before: Optional[int] = None
        self.comfy_baseline_min: Optional[int] = None  # min within baseline window
        self.comfy_peak: Optional[int] = None
        self.error: Optional[str] = None
        # Set to True if at least one per-process sample succeeded.
        self.have_per_process_data = False

    async def _record_sample(self) -> None:
        # Whole-GPU side: keep populating min_free / total via ComfyUI's
        # /system_stats. Cheap, async, and feeds the legacy "system pressure"
        # metric (NOT used by the batcher).
        free, total = await comfy_vram_snapshot(self._client)
        if isinstance(total, int) and self.total is None:
            self.total = total
        if isinstance(free, int):
            if self.min_free is None or free < self.min_free:
                self.min_free = free

        # Per-process side: poll nvidia-smi in a worker thread so we don't
        # block the event loop. ~5-15 ms per call on this box.
        try:
            comfy_used = await asyncio.to_thread(
                _sample_comfy_vram_bytes, self._comfy_pids, self._gpu0_uuid
            )
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            comfy_used = None

        if isinstance(comfy_used, int):
            self.have_per_process_data = True
            if self.comfy_before is None:
                self.comfy_before = comfy_used
            if self.comfy_peak is None or comfy_used > self.comfy_peak:
                self.comfy_peak = comfy_used
            # Baseline window: only the first ~2 s after probe start. We
            # specifically want the MIN here (model may already be in the
            # process of loading on the very first sample of a cold run).
            if (time.monotonic() - self._t_start) <= _VRAM_BASELINE_WINDOW_S:
                if self.comfy_baseline_min is None or comfy_used < self.comfy_baseline_min:
                    self.comfy_baseline_min = comfy_used

        self.samples += 1

    async def _run(self) -> None:
        try:
            # Resolve identifiers once. Cheap; both calls are short subprocess
            # invocations gated by 2s timeouts.
            self._gpu0_uuid = await asyncio.to_thread(_resolve_gpu0_uuid)
            self._comfy_pids = await asyncio.to_thread(_resolve_comfy_pids)
            if not self._comfy_pids:
                self.error = "no ComfyUI PIDs resolved"
                # Continue sampling anyway — at least the whole-GPU fields
                # will be populated for the legacy fallback path.

            # Capture an immediate "before" baseline so we have at least one
            # data point even on very short rounds. This sample becomes
            # ``comfy_before``.
            await self._record_sample()
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                    break  # stop signaled — exit loop, final sample below.
                except asyncio.TimeoutError:
                    await self._record_sample()
            # One final sample after stop to catch end-of-run state.
            await self._record_sample()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"

    def start(self) -> None:
        if self._task is None:
            self._t_start = time.monotonic()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"

    # ----- whole-GPU view (legacy, for status "system pressure") -----------

    def system_peak_used_bytes(self) -> Optional[int]:
        """Legacy ``total - min_free``: whole-GPU peak occupancy during the run.

        Kept for the status page's system-pressure reporting. NOT used by the
        batcher — it conflates concurrent allocations from other processes.
        """
        if not isinstance(self.total, int) or not isinstance(self.min_free, int):
            return None
        peak = self.total - self.min_free
        return peak if peak > 0 else None

    # ----- per-process view (new, used by the batcher) ---------------------

    def marginal_and_state(self) -> tuple[Optional[int], str]:
        """Compute flux's marginal VRAM contribution and the model_state tag.

        Returns ``(marginal_bytes, model_state)``:
          - ``model_state="warm"``: ``before`` was already at steady state, so
            marginal = ``peak - before``.
          - ``model_state="cold"``: ``before`` jumped > 3 GiB above the 2s
            baseline minimum during the probe window, indicating a first-load
            run. Use ``peak - baseline_min`` so the recorded cost includes
            the model weights, otherwise we'd persist an unrealistically low
            value (steady-state activations only).
          - Returns ``(None, "unknown")`` if we lack the inputs.
        """
        if not self.have_per_process_data:
            return None, "unknown"
        if not isinstance(self.comfy_before, int) or not isinstance(self.comfy_peak, int):
            return None, "unknown"
        baseline = (
            self.comfy_baseline_min
            if isinstance(self.comfy_baseline_min, int)
            else self.comfy_before
        )
        cold = (self.comfy_before - baseline) > _VRAM_COLD_LOAD_THRESHOLD_BYTES
        if cold:
            marginal = self.comfy_peak - baseline
            state = "cold"
        else:
            marginal = self.comfy_peak - self.comfy_before
            state = "warm"
        if marginal <= 0:
            return None, state
        return marginal, state

    def trustworthy(self) -> bool:
        # Require at least two samples (otherwise we may have caught only the
        # idle baseline) AND a usable per-process marginal.
        if self.samples < 2:
            return False
        marginal, _ = self.marginal_and_state()
        return isinstance(marginal, int) and marginal > 0

    # ----- backward-compatibility shim ------------------------------------

    def peak_used_bytes(self) -> Optional[int]:
        """Deprecated: returns the per-process marginal (NOT whole-GPU peak).

        Old call sites referred to this as "peak"; the field name now means
        flux's marginal contribution to ComfyUI's allocation. Whole-GPU peak
        is available via :meth:`system_peak_used_bytes`.
        """
        marginal, _ = self.marginal_and_state()
        return marginal


def compute_max_batch(free_vram: Optional[int], per_image: Optional[int]) -> int:
    """Adaptive batch sizing under a free-VRAM budget.

    Conservative floor of 1: a request that can't even fit one image returns 503
    upstream rather than triggering an OOM. The caller decides between
    "submit one anyway because we have no estimate" and "503"; this helper just
    returns the safe ceiling given the inputs.
    """
    if not isinstance(per_image, int) or per_image <= 0:
        # No estimate yet — be cautious: probe with batch_size=1.
        return 1
    if not isinstance(free_vram, int) or free_vram <= 0:
        return 1
    budget = max(0, free_vram - IMAGE_VRAM_RESERVE_BYTES)
    if budget < per_image:
        return 0  # caller surfaces 503 with Retry-After
    cap = max(1, budget // per_image)
    return min(int(cap), max(1, IMAGE_MAX_BATCH_SIZE))


def apply_batch_size(workflow: dict, meta: dict, batch_size: int) -> bool:
    """Set batch_size on the workflow's latent node. Returns True iff applied."""
    node_id = meta.get("batch_size_node")
    if not node_id or node_id not in workflow:
        return False
    inputs = workflow[node_id].get("inputs")
    if not isinstance(inputs, dict) or "batch_size" not in inputs:
        return False
    inputs["batch_size"] = max(1, int(batch_size))
    return True


@dataclass(frozen=True)
class ImageBackgroundRemovalDecision:
    remove_background: bool
    confidence: Optional[float] = None
    reason: str = ""
    source: str = "default"
    error: str = ""


def _background_removal_fallback(source: str, error: str = "") -> ImageBackgroundRemovalDecision:
    return ImageBackgroundRemovalDecision(
        remove_background=False,
        confidence=0.0,
        reason="background removal bypassed",
        source=source,
        error=str(error or "")[:200],
    )


def parse_image_background(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Parse OpenAI's image `background` option.

    Returns (value, error). None and empty string mean OpenAI's default: auto.
    """
    if value is None or value == "":
        return "auto", None
    if not isinstance(value, str):
        return None, "background must be one of 'transparent', 'opaque', or 'auto'"
    normalized = value.strip().lower()
    if normalized in ("transparent", "opaque", "auto"):
        return normalized, None
    return None, "background must be one of 'transparent', 'opaque', or 'auto'"


def _ideogram4_is_grounded_json(prompt: str) -> bool:
    """True only if ``prompt`` is an Ideogram-4 caption with real grounding boxes.

    A prompt that merely parses as JSON is NOT enough: callers (e.g. an agent)
    routinely send ``{"high_level_description": "...prose..."}`` with no
    ``elements`` — Ideogram then gets a degenerate, ungrounded caption and
    renders worse text/layout (and trips the safety filter more often). Treat
    such payloads as plain text so they get structurized into real obj/text
    boxes. We accept elements at the top level (hand-authored shape) OR under
    ``compositional_deconstruction`` (the shape our structurizer emits).
    """
    try:
        data = json.loads(prompt)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    for container in (data, data.get("compositional_deconstruction")):
        if isinstance(container, dict):
            els = container.get("elements")
            if isinstance(els, list) and len(els) > 0:
                return True
    return False


def _ideogram4_brief_from_prompt(prompt: str) -> str:
    """Return a plain-text creative brief to feed the structurizer.

    Plain text passes through unchanged. JSON-wrapped prose (no grounding
    elements) gets its human-readable description fields pulled out so the
    structurizer converts the *intent*, not raw JSON syntax.
    """
    try:
        data = json.loads(prompt)
    except (ValueError, TypeError):
        return prompt
    if not isinstance(data, dict):
        return prompt
    parts = []
    for key in ("high_level_description", "prompt", "description", "scene"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    style = data.get("style_description")
    if isinstance(style, dict):
        aes = style.get("aesthetics") or style.get("medium")
        if isinstance(aes, str) and aes.strip():
            parts.append(aes.strip())
    # Fall back to the raw string only if we found no usable prose.
    return "  ".join(parts) if parts else prompt


_IDEOGRAM4_STRUCTURIZE_USER_TEMPLATE = (
    "Convert this image description to an Ideogram 4 JSON caption. Output ONLY the JSON "
    "object, nothing before or after it, no markdown fences, no commentary.\n\n"
    "Input: {prompt}\n\n"
    "Schema (bbox = integers 0-1000 in order [y_min, x_min, y_max, x_max]).\n"
    "Element types: \"obj\" for visual objects, \"text\" for words rendered IN the image "
    "(labels, signs, captions, speech bubbles, annotations).\n"
    "Rules that make the model render reliably (follow ALL of them):\n"
    "1. DEPTH BANDS: always begin elements with full-width horizontal bands stacked back "
    "to front — a BACKGROUND band, a MID-GROUND band, and a GROUND band — even for simple "
    "scenes. Prefix each desc with its role in caps (e.g. \"BACKGROUND — ...\", \"GROUND — "
    "...\", \"PRIMARY SUBJECT — ...\"). These bands are what stop the model emitting a blank "
    "refusal image; never omit them.\n"
    "2. SUBJECT + PROPS: then a PRIMARY SUBJECT box, plus 1-3 secondary prop/scenery boxes "
    "spread across the canvas. Give each desc material + lighting detail, not just a noun.\n"
    "3. TEXT: for EVERY piece of visible text emit a SEPARATE \"text\" element with the exact "
    "words in its \"text\" field and a roomy own bbox. Never bury text inside an obj desc.\n"
    "4. Fill style_description.lighting and .photo with a full descriptive sentence each, not "
    "one word. Always give a concrete \"background\" string.\n"
    "5. ENGLISH ONLY: every \"text\" element value must be English using only Latin "
    "letters/numerals. Never emit Chinese, Japanese, Korean, or any non-English glyphs "
    "(the model injects them unprompted otherwise). Also append ', all rendered text in "
    "English only' to high_level_description.\n"
    'Example shape:\n'
    '{{"high_level_description":"one rich sentence describing the whole scene",'
    '"style_description":{{"aesthetics":"...","lighting":"full sentence on light source, direction, quality, color temperature",'
    '"photo":"full sentence on framing, composition, depth of field, lens","medium":"photograph"}},'
    '"compositional_deconstruction":{{"background":"concrete description of the backdrop",'
    '"elements":['
    '{{"type":"obj","bbox":[0,0,800,1000],"desc":"BACKGROUND — ... material/lighting"}},'
    '{{"type":"obj","bbox":[400,0,600,1000],"desc":"MID-GROUND — ... material/lighting"}},'
    '{{"type":"obj","bbox":[600,0,1000,1000],"desc":"GROUND — ... material/lighting"}},'
    '{{"type":"obj","bbox":[300,250,900,750],"desc":"PRIMARY SUBJECT — ... material/lighting"}},'
    '{{"type":"obj","bbox":[350,40,800,240],"desc":"PROP — a nearby object"}},'
    '{{"type":"text","bbox":[120,150,260,880],"text":"EXACT WORDS HERE","desc":"handwritten label"}}'
    ']}}}}'
)


def _ideogram4_minimal_grounded_json(brief: str) -> str:
    """Last-resort JSON wrapper for a plain-text brief.

    Returning raw plain text to Ideogram 4 almost always trips its safety
    classifier (grey 'Image blocked' output). When the LLM converter is
    unavailable or keeps producing invalid JSON, wrap the brief in a minimal
    but *grounded* JSON skeleton (subject + floor + backdrop boxes) — the
    r/StableDiffusion 'enough bboxes' fix — so we still hand the model the
    structured form it expects instead of guaranteed-blocked plain text.
    """
    safe = json.dumps(brief)[1:-1]  # escape quotes/newlines, drop outer quotes
    return (
        '{"high_level_description":"' + safe + '",'
        '"style_description":{"aesthetics":"as described","lighting":"flat",'
        '"photo":"illustration","medium":"illustration"},'
        '"compositional_deconstruction":{"background":"a plain backdrop",'
        '"elements":['
        '{"type":"obj","bbox":[250,300,750,800],"desc":"' + safe + '"},'
        '{"type":"obj","bbox":[0,800,1000,1000],"desc":"the ground"},'
        '{"type":"obj","bbox":[0,0,1000,280],"desc":"the backdrop behind the scene"}'
        ']}}'
    )


async def _ideogram4_structurize_prompt(plain_prompt: str) -> str:
    """Convert a plain-text prompt to Ideogram 4 JSON caption via local LLM.

    Retries the conversion a few times on parse failure, since Qwen3
    occasionally emits empty/garbled output. NEVER falls back to raw plain
    text (that reliably trips Ideogram's safety filter); on total failure it
    returns a minimal grounded JSON skeleton instead. Qwen3 in thinking mode
    uses ~1700 reasoning tokens before emitting visible output, so max_tokens
    must be >=2000.
    """
    # Truncate to ~600 chars — the converter only needs the creative brief, not
    # the full style/constraint boilerplate that callers often append.
    brief = plain_prompt[:600]
    user_msg = _IDEOGRAM4_STRUCTURIZE_USER_TEMPLATE.format(prompt=brief)
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5)) as client:
                resp = await client.post(
                    f"{KOBOLD_HOST}/v1/chat/completions",
                    json={
                        "messages": [{"role": "user", "content": user_msg}],
                        "max_tokens": 3000,
                        # Slightly vary temperature across retries so a stuck
                        # generation has a chance to produce different output.
                        "temperature": 0.1 + 0.2 * attempt,
                    },
                )
            if resp.status_code != 200:
                continue
            content = resp.json()["choices"][0]["message"]["content"].strip()
            # Drop any <think>...</think> block the model may leak despite thinking suppression.
            if "</think>" in content:
                content = content.split("</think>", 1)[1].strip()
            # Strip accidental markdown fences
            if content.startswith("```"):
                content = content.split("```")[1].lstrip("json").strip()
            # Some models add prose before/after the JSON — extract the outermost object.
            if not content.startswith("{") and "{" in content:
                content = content[content.index("{"): content.rindex("}") + 1]
            json.loads(content)  # validate — raises if not valid JSON
            return content
        except Exception:
            continue
    # All attempts failed — return grounded JSON, never raw plain text.
    return _ideogram4_minimal_grounded_json(brief)


async def resolve_image_background_decision(
    background: str,
    prompt: str,
    *,
    auth_context: AuthContext,
    force_bypass: bool = False,
) -> ImageBackgroundRemovalDecision:
    """Resolve OpenAI's background option into a workflow output selection."""
    if force_bypass:
        return _background_removal_fallback("forced_bypass", "edit mask supplied")
    if background == "transparent":
        return ImageBackgroundRemovalDecision(
            remove_background=True,
            confidence=1.0,
            reason="client requested transparent background",
            source="request",
        )
    if background == "opaque":
        return ImageBackgroundRemovalDecision(
            remove_background=False,
            confidence=1.0,
            reason="client requested opaque background",
            source="request",
        )
    return await decide_image_background_removal(prompt, auth_context=auth_context)


def _extract_json_object(text: str) -> Optional[dict]:
    """Best-effort JSON object extraction for backends that ignore json_object."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(stripped[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_background_removal_decision(raw: Any, *, source: str = "llm") -> ImageBackgroundRemovalDecision:
    """Normalize the LLM's structured decision into the internal shape."""
    if isinstance(raw, str):
        raw = _extract_json_object(raw)
    if not isinstance(raw, dict):
        return _background_removal_fallback(source, "decision was not a JSON object")

    remove_background = raw.get("remove_background")
    if not isinstance(remove_background, bool):
        return _background_removal_fallback(source, "remove_background was not a boolean")

    confidence = raw.get("confidence")
    try:
        confidence_val = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence_val = None
    if confidence_val is not None:
        confidence_val = max(0.0, min(1.0, confidence_val))

    reason = raw.get("reason") or raw.get("rationale") or ""
    return ImageBackgroundRemovalDecision(
        remove_background=remove_background,
        confidence=confidence_val,
        reason=str(reason)[:200],
        source=source,
    )


async def decide_image_background_removal(
    prompt: str,
    *,
    auth_context: AuthContext,
    force_bypass: bool = False,
) -> ImageBackgroundRemovalDecision:
    """Ask the active LLM whether this image request should return transparency.

    Failure is intentionally non-fatal. The proxy can always produce the normal
    image, so unknown/invalid decisions default to bypass.
    """
    if force_bypass:
        return _background_removal_fallback("forced_bypass", "edit mask supplied")
    if not IMAGE_BACKGROUND_REMOVAL_ENABLED:
        return _background_removal_fallback("disabled")
    prompt = str(prompt or "").strip()
    if not prompt:
        return _background_removal_fallback("empty_prompt")

    active = await get_active_backend()
    if active == "kobold":
        target_host = KOBOLD_HOST
        service = "kobold"
    elif active == "tabby":
        target_host = TABBY_HOST
        service = "tabby"
    else:
        return _background_removal_fallback("llm_unavailable", "no active LLM backend")

    model_id = await effective_llm_model_for_host(target_host)
    if not model_id:
        model_id = default_model_for_operation("chat", "default")

    headers = configured_downstream_headers(auth_context, service)
    # enable_thinking=False suppresses thinking (engine-agnostic kwarg — the load-bearing mechanism).
    # No /no_think prompt-string injection: verified non-functional; kwarg-only is the contract.
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Decide whether an image generation request should have its "
                    "background removed and returned as a transparent PNG. "
                    "Use the user's intent, not keyword matching. Return only JSON "
                    "with keys remove_background boolean, confidence number 0..1, "
                    "and reason string. Choose true for reusable assets such as "
                    "logos, stickers, product cutouts, icons, isolated objects for "
                    "commerce, or explicitly transparent-background intent. Choose "
                    "false for portraits, landscapes, scenes, paintings, context-rich "
                    "images, and ordinary photos where the environment is part of the result."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 160,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {_QWEN3_ENABLE_THINKING_KEY: False},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(IMAGE_BACKGROUND_REMOVAL_TIMEOUT, connect=5)) as client:
            resp = await client.post(f"{target_host}/v1/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            return _background_removal_fallback("llm_error", f"HTTP {resp.status_code}: {resp.text[:120]}")
        data = resp.json()
        content = (
            ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
            if isinstance(data, dict)
            else ""
        )
        return parse_background_removal_decision(content, source="llm")
    except Exception as e:
        return _background_removal_fallback("llm_error", str(e))


def set_workflow_background_removal(workflow: dict, meta: dict, enabled: bool) -> bool:
    """Select the workflow-defined plain or transparent image path.

    Background-removal graph structure lives in the committed workflow JSON.
    The proxy only toggles the SaveImage input declared in `_meta`.
    """
    cfg = meta.get("background_removal")
    if not isinstance(cfg, dict):
        return False
    save_node_id = str(cfg.get("save_node") or "")
    selected_input = cfg.get("transparent_image" if enabled else "plain_image")

    save = workflow.get(save_node_id)
    if not isinstance(save, dict) or save.get("class_type") != "SaveImage":
        return False
    inputs = save.get("inputs")
    if not isinstance(inputs, dict):
        return False
    if not (
        isinstance(selected_input, list)
        and len(selected_input) == 2
        and isinstance(selected_input[0], str)
        and isinstance(selected_input[1], int)
        and selected_input[0] in workflow
    ):
        return False
    inputs["images"] = list(selected_input)
    return True


def _image_signing_secret() -> bytes:
    """Resolve (and lazily generate) the HMAC secret used for signed image URLs."""
    global _image_url_signing_secret
    if _image_url_signing_secret is not None:
        return _image_url_signing_secret
    if IMAGE_SIGNED_URL_SECRET_CFG:
        _image_url_signing_secret = IMAGE_SIGNED_URL_SECRET_CFG.encode("utf-8")
        return _image_url_signing_secret
    secret_path = IMAGE_CACHE_DIR / ".url_signing_secret"
    if secret_path.exists():
        try:
            data = secret_path.read_bytes().strip()
            if data:
                _image_url_signing_secret = data
                return _image_url_signing_secret
        except Exception:
            pass
    # Generate and persist.
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48).encode("utf-8")
    try:
        secret_path.write_bytes(secret)
        os.chmod(secret_path, 0o600)
    except Exception as e:
        print(f"[proxy] Could not persist image URL signing secret: {e}", flush=True)
    _image_url_signing_secret = secret
    return secret


def _sign_image_token(image_id: str, expires: int) -> str:
    return _sign_asset_token(image_id, expires)


def _sign_asset_token(asset_id: str, expires: int) -> str:
    msg = f"{asset_id}:{expires}".encode("utf-8")
    return hmac.new(_image_signing_secret(), msg, hashlib.sha256).hexdigest()


def verify_image_signature(image_id: str, expires: int, sig: str) -> bool:
    """Constant-time HMAC verification for signed image URLs."""
    return verify_asset_signature(image_id, expires, sig)


def verify_asset_signature(asset_id: str, expires: int, sig: str) -> bool:
    """Constant-time HMAC verification for signed asset URLs."""
    if expires < int(time.time()):
        return False
    expected = _sign_asset_token(asset_id, expires)
    return hmac.compare_digest(expected, str(sig or ""))


def _asset_owner_key(auth_context: AuthContext | None) -> str | None:
    identity = getattr(auth_context, "identity", None) if auth_context else None
    key_hash = getattr(identity, "key_hash", None)
    return str(key_hash) if key_hash else None


def _asset_metadata_path(asset_id: str) -> Path | None:
    parsed = _parse_asset_id(asset_id)
    if parsed is None:
        return None
    _, digest = parsed
    return IMAGE_CACHE_DIR / f"{digest}.meta.json"


def _read_asset_metadata(asset_id: str) -> dict[str, Any]:
    path = _asset_metadata_path(asset_id)
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_asset_metadata(asset_id: str, metadata: Mapping[str, Any]) -> None:
    path = _asset_metadata_path(asset_id)
    if path is None:
        return
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        tmp.write_text(json.dumps(dict(metadata), sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        print(f"[proxy] Could not persist asset metadata for {asset_id}: {e}", flush=True)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _record_asset_owner(asset_id: str, owner_key_hash: str | None) -> None:
    if not owner_key_hash:
        return
    metadata = _read_asset_metadata(asset_id)
    owners_raw = metadata.get("owner_key_hashes") or []
    owners = [str(owner) for owner in owners_raw if isinstance(owner, str)]
    if owner_key_hash not in owners:
        owners.append(owner_key_hash)
    metadata["asset_id"] = asset_id
    metadata["owner_key_hashes"] = sorted(set(owners))
    metadata["updated_at"] = int(time.time())
    _write_asset_metadata(asset_id, metadata)


def _asset_access_allowed(asset_id: str, auth_context: AuthContext | None) -> bool:
    owners_raw = _read_asset_metadata(asset_id).get("owner_key_hashes") or []
    owners = {str(owner) for owner in owners_raw if isinstance(owner, str)}
    if not owners:
        # Fail closed for authenticated callers when metadata is absent. Auth-
        # disabled local runs have no identity and keep the old cache behavior.
        return _asset_owner_key(auth_context) is None
    owner_key_hash = _asset_owner_key(auth_context)
    return bool(owner_key_hash and owner_key_hash in owners)


def store_asset_bytes(
    content: bytes,
    *,
    kind: str = "img",
    suffix: str = ".png",
    owner_key_hash: str | None = None,
) -> str:
    """Write `content` to the content-addressed cache. Returns the asset_id."""
    kind = str(kind).strip().lower()
    if kind not in {"img", "aud", "vid"}:
        raise ValueError("asset kind must be one of img, aud, vid")
    suffix = str(suffix or "").strip().lower()
    if not suffix.startswith("."):
        suffix = f".{suffix or 'bin'}"
    digest = hashlib.sha256(content).hexdigest()
    asset_id = f"{kind}_{digest}"
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = IMAGE_CACHE_DIR / f"{digest}{suffix}"
    if not target.exists():
        # Atomic write: tmp + rename.
        tmp = target.with_suffix(f"{suffix}.tmp")
        tmp.write_bytes(content)
        tmp.replace(target)
    _record_asset_owner(asset_id, owner_key_hash)
    return asset_id


def store_image_bytes(content: bytes, *, owner_key_hash: str | None = None) -> str:
    """Write `content` to the content-addressed cache. Returns the image_id."""
    return store_asset_bytes(
        content,
        kind="img",
        suffix=".png",
        owner_key_hash=owner_key_hash,
    )


_ASSET_SUFFIXES_BY_KIND = {
    "img": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
    "aud": (".wav", ".mp3", ".flac", ".ogg", ".m4a"),
    "vid": (".mp4", ".webm", ".mov", ".mkv"),
}

_ASSET_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
}

_ASSET_KIND_NAMES = {
    "img": "image",
    "aud": "audio",
    "vid": "video",
}


def _parse_asset_id(asset_id: str) -> tuple[str, str] | None:
    if not isinstance(asset_id, str) or "_" not in asset_id:
        return None
    kind, digest = asset_id.split("_", 1)
    if kind not in _ASSET_SUFFIXES_BY_KIND:
        return None
    if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        return None
    return kind, digest


def asset_cache_path(asset_id: str) -> Optional[Path]:
    parsed = _parse_asset_id(asset_id)
    if parsed is None:
        return None
    kind, digest = parsed
    candidates = [IMAGE_CACHE_DIR / f"{digest}{suffix}" for suffix in _ASSET_SUFFIXES_BY_KIND[kind]]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def asset_media_type(asset_id: str, path: Path | None = None) -> str:
    path = path or asset_cache_path(asset_id)
    suffix = path.suffix.lower() if path is not None else ""
    return _ASSET_MEDIA_TYPES.get(suffix, "application/octet-stream")


def _gateway_asset_metadata_payload(
    asset_id: str,
    request: Request,
    *,
    cache_path: Path,
) -> dict[str, Any]:
    parsed = _parse_asset_id(asset_id)
    kind = parsed[0] if parsed else ""
    stat = cache_path.stat()
    metadata = _read_asset_metadata(asset_id)
    updated_at = metadata.get("updated_at")
    try:
        updated_at_value = int(updated_at)
    except (TypeError, ValueError):
        updated_at_value = int(stat.st_mtime)
    return {
        "object": "gateway.asset",
        "id": asset_id,
        "asset_id": asset_id,
        "kind": _ASSET_KIND_NAMES.get(kind, kind or "asset"),
        "media_type": asset_media_type(asset_id, cache_path),
        "size_bytes": int(stat.st_size),
        "created_at": int(stat.st_ctime),
        "updated_at": updated_at_value,
        "content_url": build_signed_asset_url(request, asset_id),
        "content_route": f"/v1/assets/{asset_id}/content",
        "expires_in": IMAGE_SIGNED_URL_TTL,
        "owner_scoped": bool(metadata.get("owner_key_hashes")),
    }


def image_cache_path(image_id: str) -> Optional[Path]:
    if not image_id.startswith("img_"):
        return None
    return asset_cache_path(image_id)


def build_signed_asset_url(request: Request, asset_id: str) -> str:
    """Build a signed, hostname-aware URL for a generated asset."""
    expires = int(time.time()) + IMAGE_SIGNED_URL_TTL
    sig = _sign_asset_token(asset_id, expires)
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/v1/assets/{asset_id}/content?expires={expires}&sig={sig}"


def build_signed_image_url(request: Request, image_id: str) -> str:
    """Build a signed, hostname-aware URL for an image stored in the cache.

    Honors `X-Forwarded-Proto` and the request's Host header so the URL works
    regardless of which Traefik route the client came in on.
    """
    expires = int(time.time()) + IMAGE_SIGNED_URL_TTL
    sig = _sign_image_token(image_id, expires)
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/v1/images/{image_id}/content?expires={expires}&sig={sig}"


async def evict_image_cache_loop(stop_event: asyncio.Event) -> None:
    """Background task: delete cache entries older than IMAGE_CACHE_TTL seconds."""
    while not stop_event.is_set():
        try:
            cutoff = time.time() - IMAGE_CACHE_TTL
            if IMAGE_CACHE_DIR.exists():
                asset_suffixes = {
                    suffix
                    for suffixes in _ASSET_SUFFIXES_BY_KIND.values()
                    for suffix in suffixes
                }
                for entry in IMAGE_CACHE_DIR.iterdir():
                    if not entry.is_file():
                        continue
                    if entry.suffix not in asset_suffixes and not entry.name.endswith(".meta.json"):
                        continue
                    try:
                        if entry.stat().st_mtime < cutoff:
                            entry.unlink(missing_ok=True)
                    except Exception:
                        continue
        except Exception as e:
            print(f"[proxy] image cache eviction: {e}", flush=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=IMAGE_CACHE_EVICT_INTERVAL)
        except asyncio.TimeoutError:
            continue


def has_vision_content(messages) -> bool:
    """Return True iff any chat message carries an image content block.

    We check the OpenAI standard `image_url` block as well as the Anthropic-
    flavored `input_image` shape that some clients use against this proxy.
    """
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype in ("image_url", "input_image", "image"):
                return True
    return False


def _comfy_execution_error_message(job: Mapping[str, Any] | None) -> str | None:
    """Return a safe, short ComfyUI execution error summary if one is present."""
    status_block = (job or {}).get("status", {}) if isinstance(job, Mapping) else {}
    if not isinstance(status_block, Mapping):
        return None

    messages = status_block.get("messages", []) or []
    for msg in messages:
        if not (isinstance(msg, (list, tuple)) and msg and msg[0] == "execution_error"):
            continue
        payload = msg[1] if len(msg) > 1 and isinstance(msg[1], Mapping) else {}
        node_type = str(payload.get("node_type") or "").strip()
        node_id = str(payload.get("node_id") or "").strip()
        exception_message = str(payload.get("exception_message") or "").strip()
        exception_type = str(payload.get("exception_type") or "").strip()

        location = "ComfyUI execution error"
        if node_type and node_id:
            location = f"ComfyUI {node_type} node {node_id}"
        elif node_type:
            location = f"ComfyUI {node_type}"
        elif node_id:
            location = f"ComfyUI node {node_id}"

        reason = exception_message or exception_type or "execution_error"
        return f"{location}: {reason}"[:500]

    status = str(status_block.get("status_str") or status_block.get("status") or "").lower()
    if status == "error":
        return "ComfyUI execution failed"
    return None


async def _comfy_run_round(
    client: httpx.AsyncClient,
    workflow: dict,
    headers: dict,
    label: str,
    *,
    vram_probe: Optional[_VramProbe] = None,
) -> tuple[Optional[Response], list[bytes]]:
    """Submit a single ComfyUI prompt and harvest the resulting image bytes.

    Returns (error_response, image_bytes_list). On success error_response is None.
    Caller is responsible for any retry / batch-splitting strategy.

    If ``vram_probe`` is provided, it is started before the prompt is
    submitted and stopped after the round completes (including on error).
    The probe samples ComfyUI's per-process VRAM via nvidia-smi (filtered to
    the ComfyUI PID set on GPU0); callers read ``marginal_and_state()`` to
    get flux's marginal contribution and whether the run was warm or cold.
    """
    if vram_probe is not None:
        vram_probe.start()

    async def _finalize_probe() -> None:
        if vram_probe is not None:
            try:
                await vram_probe.stop()
            except Exception as e:
                print(f"[proxy] VRAM probe stop error: {e}", flush=True)

    try:
        try:
            resp = await client.post(
                f"{COMFY_HOST}/prompt",
                headers=headers,
                json=_build_comfy_submit_payload(workflow),
            )
        except httpx.TimeoutException:
            return Response(
                content=json.dumps({"error": {"message": "ComfyUI request timeout", "type": "api_error"}}),
                status_code=504,
                media_type="application/json",
            ), []
        except httpx.ConnectError:
            return Response(
                content=json.dumps({"error": {"message": f"ComfyUI unavailable at {COMFY_HOST}", "type": "api_error"}}),
                status_code=502,
                media_type="application/json",
            ), []

        if resp.status_code != 200:
            return Response(
                content=json.dumps({"error": {"message": f"ComfyUI error: {resp.text}", "type": "api_error"}}),
                status_code=502,
                media_type="application/json",
            ), []

        result = resp.json()
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            return Response(
                content=json.dumps({"error": {"message": "No prompt_id returned", "type": "api_error"}}),
                status_code=502,
                media_type="application/json",
            ), []

        print(f"[proxy] ComfyUI {label} submitted: {prompt_id}", flush=True)

        max_polls = COMFY_TIMEOUT * 2  # 0.5s cadence
        job = None
        for _ in range(max_polls):
            await asyncio.sleep(0.5)
            try:
                history_resp = await client.get(f"{COMFY_HOST}/history/{prompt_id}", headers=headers)
            except (httpx.TimeoutException, httpx.ConnectError):
                continue
            if history_resp.status_code != 200:
                continue
            history = history_resp.json()
            if prompt_id not in history:
                continue
            candidate = history[prompt_id]
            error_message = _comfy_execution_error_message(candidate)
            if error_message:
                return Response(
                    content=json.dumps({
                        "error": {
                            "message": error_message,
                            "type": "api_error",
                        }
                    }),
                    status_code=502,
                    media_type="application/json",
                ), []
            if candidate.get("status", {}).get("completed"):
                job = candidate
                break

        if job is None:
            return Response(
                content=json.dumps({"error": {"message": "Image generation timeout", "type": "api_error"}}),
                status_code=504,
                media_type="application/json",
            ), []

        images: list[bytes] = []
        outputs = job.get("outputs", {})
        for _node_id, node_output in outputs.items():
            if "images" not in node_output:
                continue
            for img_info in node_output["images"]:
                filename = img_info.get("filename")
                subfolder = img_info.get("subfolder", "")
                img_type = img_info.get("type", "output")
                params = {"filename": filename, "type": img_type}
                if subfolder:
                    params["subfolder"] = subfolder
                try:
                    img_resp = await client.get(f"{COMFY_HOST}/view", headers=headers, params=params)
                except (httpx.TimeoutException, httpx.ConnectError):
                    continue
                if img_resp.status_code == 200 and img_resp.content:
                    images.append(img_resp.content)
        return None, images
    finally:
        await _finalize_probe()


def _build_image_response_payload(
    request: Request,
    images: list[bytes],
    response_format: str,
    warning_headers: dict,
    auth_context: AuthContext | None = None,
) -> Response:
    """Shape `data[]` for either b64_json or url, honoring the client's choice."""
    data: list[dict] = []
    owner_key_hash = _asset_owner_key(auth_context)
    for raw in images:
        image_id = store_image_bytes(raw, owner_key_hash=owner_key_hash)
        item: dict[str, Any] = {
            "asset_id": image_id,
            "asset_url": build_signed_asset_url(request, image_id),
            "media_type": "image/png",
        }
        if response_format == "url":
            item["url"] = build_signed_image_url(request, image_id)
        else:
            b64 = base64.b64encode(raw).decode("utf-8")
            item["b64_json"] = b64
        data.append(item)
    headers = {"content-type": "application/json"}
    headers.update(warning_headers)
    return Response(
        content=json.dumps({
            "created": int(time.time()),
            "data": data,
        }),
        status_code=200,
        media_type="application/json",
        headers=headers,
    )


def _default_image_response_format(requested_model: Any) -> str:
    model_id = normalize_model_id(str(requested_model or ""))
    if model_id.startswith("dall-e-"):
        return "url"
    return "b64_json"


async def _generate_images_with_adaptive_batching(
    request: Request,
    client: httpx.AsyncClient,
    base_workflow: dict,
    meta: dict,
    seed_node: Optional[str],
    seed_field: str,
    workflow_name: str,
    width: int,
    height: int,
    n_requested: int,
    response_format: str,
    headers: dict,
    background_removal_decision: Optional[ImageBackgroundRemovalDecision] = None,
    fallback_workflow: Optional[dict] = None,
    fallback_workflow_name: Optional[str] = None,
    image_transform: Optional[Callable[[bytes], bytes]] = None,
    image_response_headers: Optional[dict[str, str]] = None,
    fixed_seed: Optional[int] = None,
    auth_context: AuthContext | None = None,
) -> Response:
    """Submit `ceil(n / max_batch)` rounds and aggregate `n_requested` images.

    On the first round (when no VRAM estimate exists), force batch_size=1 and
    measure the per-image VRAM cost so subsequent rounds (and subsequent
    requests) can scale up safely. Never exceeds free VRAM minus reserve.
    """
    estimate = get_vram_estimate(workflow_name, width, height)
    free_before = await comfy_free_vram_bytes(client)
    warnings: list[str] = []
    response_headers: dict[str, str] = {}

    images_collected: list[bytes] = []
    images_remaining = n_requested
    rounds_run = 0

    # Soft cap on rounds — each round produces at least 1 image, so worst case
    # we run n_requested rounds. Add a small extra buffer for retry-on-OOM.
    max_rounds = max(1, n_requested + 2)

    while images_remaining > 0 and rounds_run < max_rounds:
        rounds_run += 1
        free_now = await comfy_free_vram_bytes(client)

        # Sizing model:
        #   - Probe round (rounds_run==1, no estimate): batch_size=1 so we can
        #     measure incremental VRAM cost without risking OOM.
        #   - Subsequent rounds (or any round when we have an estimate):
        #     compute cap from free_now / estimate. If we have no estimate but
        #     are past round 1, assume the cost is small (model already
        #     resident) and trust IMAGE_MAX_BATCH_SIZE as the ceiling.
        if rounds_run == 1 and estimate is None:
            batch_target = 1
        elif estimate is not None:
            cap = compute_max_batch(free_now, estimate)
            if cap == 0:
                msg = (
                    f"Insufficient free VRAM for image generation: "
                    f"need ~{estimate / (1024**3):.2f} GiB per image plus "
                    f"{IMAGE_VRAM_RESERVE_BYTES / (1024**3):.2f} GiB reserve, "
                    f"have {(free_now or 0) / (1024**3):.2f} GiB. "
                    f"Retry after model unloads or another job completes."
                )
                return Response(
                    content=json.dumps({
                        "error": {
                            "message": msg,
                            "type": "service_unavailable",
                            "code": "vram_exhausted",
                        }
                    }),
                    status_code=503,
                    media_type="application/json",
                    headers={"Retry-After": "30"},
                )
            batch_target = min(cap, images_remaining)
        else:
            # No estimate, but we already proved on round 1 that the model fits
            # and can produce images. Trust the configured ceiling — any
            # OOM here is data we'll capture on the next request via the
            # measurement code below.
            batch_target = min(IMAGE_MAX_BATCH_SIZE, images_remaining)

        # Build a per-round workflow copy. Re-seed each round so multiple rounds
        # produce different images instead of the same image repeated.
        round_workflow = copy.deepcopy(base_workflow)
        if seed_node and seed_node in round_workflow:
            round_seed = (
                (fixed_seed + len(images_collected))
                if fixed_seed is not None
                else random.randint(0, 2**32 - 1)
            )
            inputs = round_workflow[seed_node].get("inputs", {})
            inputs[seed_field] = round_seed

        applied = apply_batch_size(round_workflow, meta, batch_target)
        if not applied and batch_target > 1:
            # Workflow lacks a batch_size_node — can't scale up. Loop instead,
            # but warn the client so this isn't a silent slow path.
            warnings.append(
                f"workflow '{workflow_name}' has no batch_size_node; "
                f"falling back to {images_remaining} sequential rounds"
            )
            batch_target = 1

        # Only run the probe on rounds where we don't yet have a trusted
        # estimate (rounds_run == 1 is the typical case). Subsequent rounds
        # skip the probe overhead.
        probe: Optional[_VramProbe] = None
        if estimate is None:
            probe = _VramProbe(client)

        err, round_images = await _comfy_run_round(
            client,
            round_workflow,
            headers,
            label=f"image round {rounds_run}/{max_rounds} (target={batch_target})",
            vram_probe=probe,
        )
        if err is not None:
            if (
                background_removal_decision is not None
                and background_removal_decision.remove_background
                and fallback_workflow is not None
            ):
                try:
                    body = json.loads(err.body.decode("utf-8")) if getattr(err, "body", None) else {}
                    msg = ((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
                except Exception:
                    msg = ""
                msg = msg[:200] or f"HTTP {getattr(err, 'status_code', 'unknown')}"
                print(f"[proxy] background removal failed; retrying without RMBG: {msg}", flush=True)
                firehose_metrics.record_event(
                    "image_background_removal_fallback",
                    "RMBG failed; retried without background removal",
                    workflow=workflow_name,
                    reason=msg,
                )
                bypass_decision = ImageBackgroundRemovalDecision(
                    remove_background=False,
                    confidence=0.0,
                    reason="RMBG failed; retried without background removal",
                    source="rmbg_fallback",
                    error=msg,
                )
                return await _generate_images_with_adaptive_batching(
                    request=request,
                    client=client,
                    base_workflow=copy.deepcopy(fallback_workflow),
                    meta=meta,
                    seed_node=seed_node,
                    seed_field=seed_field,
                    workflow_name=fallback_workflow_name or workflow_name,
                    width=width,
                    height=height,
                    n_requested=n_requested,
                    response_format=response_format,
                    headers=headers,
                    background_removal_decision=bypass_decision,
                    image_transform=image_transform,
                    image_response_headers=image_response_headers,
                    fixed_seed=fixed_seed,
                    auth_context=auth_context,
                )
            return err

        if not round_images:
            warnings.append(f"round {rounds_run} produced 0 images")
            # Avoid infinite loops if the backend silently returns nothing.
            if rounds_run >= 2 and not images_collected:
                return Response(
                    content=json.dumps({"error": {"message": "ComfyUI returned no images", "type": "api_error"}}),
                    status_code=502,
                    media_type="application/json",
                )
            continue

        images_collected.extend(round_images)
        images_remaining -= len(round_images)

        # VRAM measurement after the first successful round.
        # Preferred: in-run per-process probe measures flux's MARGINAL
        # contribution to ComfyUI's GPU0 allocation (peak_comfy_used -
        # before_comfy_used). Insensitive to concurrent voxtral / desktop /
        # TTS activity, unlike the old `total - min_free` approach which
        # captured whole-GPU pressure at peak.
        # Fallback: endpoint diff `free_before - free_after`, tagged as
        # untrusted so the batcher doesn't plan against it.
        if estimate is None and rounds_run == 1:
            per_image_cost = 0
            measurement_trusted = False
            source = "probe"
            model_state = "unknown"
            comfy_baseline = 0
            samples_count = probe.samples if probe is not None else 0
            free_after, _ = await comfy_vram_snapshot(client)

            if probe is not None and probe.trustworthy():
                marginal, model_state = probe.marginal_and_state()
                if isinstance(marginal, int) and marginal > 0:
                    # marginal is the per-batch-round delta; divide by
                    # batch_target to get per-image cost (cache key is
                    # per-image).
                    divisor = max(1, batch_target)
                    per_image_cost = marginal // divisor
                    measurement_trusted = True
                    source = "probe"
                comfy_baseline = (
                    probe.comfy_baseline_min
                    if isinstance(probe.comfy_baseline_min, int)
                    else 0
                )
            elif probe is not None and probe.error:
                print(
                    f"[proxy] VRAM probe error for {workflow_name} "
                    f"{width}x{height}: {probe.error}",
                    flush=True,
                )

            if per_image_cost <= 0:
                # Probe failed or returned nothing useful. Fall back to the
                # legacy endpoint diff so we at least persist *something*,
                # but mark it untrusted.
                if isinstance(free_after, int) and isinstance(free_before, int) and free_after < free_before:
                    per_image_cost = (free_before - free_after) // max(1, batch_target)
                if per_image_cost <= 0:
                    per_image_cost = 4 * 1024 * 1024  # synthetic floor: 4 MiB
                measurement_trusted = False
                source = "endpoint_diff"
                if model_state == "unknown":
                    model_state = "unknown"

            record_vram_estimate(
                workflow_name,
                width,
                height,
                per_image_cost,
                trusted=measurement_trusted,
                source=source,
                model_state=model_state,
                comfy_baseline_bytes=comfy_baseline,
                samples_count=samples_count,
            )
            # Re-read; will be None if measurement was untrusted (so we
            # don't make scheduling decisions off it).
            estimate = get_vram_estimate(workflow_name, width, height)
            sys_peak = probe.system_peak_used_bytes() if probe is not None else None
            sys_peak_str = (
                f"{sys_peak / (1024**3):.2f} GiB" if isinstance(sys_peak, int) else "n/a"
            )
            before_str = (
                f"{probe.comfy_before / (1024**3):.2f} GiB"
                if probe is not None and isinstance(probe.comfy_before, int)
                else "n/a"
            )
            peak_str = (
                f"{probe.comfy_peak / (1024**3):.2f} GiB"
                if probe is not None and isinstance(probe.comfy_peak, int)
                else "n/a"
            )
            print(
                f"[proxy] VRAM estimate for {workflow_name} {width}x{height}: "
                f"{per_image_cost / (1024**2):.1f} MiB/image marginal "
                f"(trusted={measurement_trusted}, source={source}, "
                f"model_state={model_state}, probe_samples={samples_count}, "
                f"comfy_before={before_str}, comfy_peak={peak_str}, "
                f"system_peak={sys_peak_str})",
                flush=True,
            )

    images_collected = images_collected[:n_requested]
    if not images_collected:
        return Response(
            content=json.dumps({"error": {"message": "No images generated", "type": "api_error"}}),
            status_code=500,
            media_type="application/json",
        )

    if len(images_collected) < n_requested:
        warnings.append(
            f"requested n={n_requested} but produced {len(images_collected)} after {rounds_run} round(s)"
        )

    if image_transform is not None:
        try:
            images_collected = [image_transform(raw) for raw in images_collected]
        except Exception as e:
            return Response(
                content=json.dumps({
                    "error": {
                        "message": f"Image post-processing failed: {e}",
                        "type": "api_error",
                    }
                }),
                status_code=502,
                media_type="application/json",
            )

    if warnings:
        # Single header, comma-joined per RFC 7234. Visible to the client.
        response_headers["X-Proxy-Warning"] = "; ".join(warnings)
    if image_response_headers:
        response_headers.update(image_response_headers)
    response_headers["X-Image-Rounds"] = str(rounds_run)
    response_headers["X-Image-Returned"] = str(len(images_collected))
    if background_removal_decision is not None:
        response_headers["X-Image-Background-Removal"] = (
            "true" if background_removal_decision.remove_background else "false"
        )
        if background_removal_decision.source:
            response_headers["X-Image-Background-Removal-Source"] = background_removal_decision.source

    print(
        f"[proxy] ComfyUI generated {len(images_collected)}/{n_requested} image(s) "
        f"across {rounds_run} round(s)",
        flush=True,
    )

    return _build_image_response_payload(
        request,
        images_collected,
        response_format,
        response_headers,
        auth_context=auth_context,
    )


async def proxy_comfy_request(
    request: Request,
    auth_context: AuthContext,
    receipt: RouteReceipt | None = None,
) -> Response:
    """Handle OpenAI-compatible image generation request via ComfyUI."""
    if not COMFY_ENABLED:
        if receipt is not None:
            receipt.add_fallback_reason("comfy_disabled")
        return Response(
            content=json.dumps({"error": {"message": "Image generation not enabled", "type": "service_unavailable"}}),
            status_code=503,
            media_type="application/json"
        )
    service_response = _service_availability_response(
        "comfyui",
        service_label="ComfyUI",
    )
    if service_response is not None:
        if receipt is not None:
            receipt.add_fallback_reason("comfy_operator_mode")
        return service_response

    try:
        body = await request.json()
    except:
        _record_image_input(receipt, "body", rejected_reason="invalid_json")
        return Response(
            content=json.dumps({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )
    unknown_response = _record_unknown_image_fields(
        receipt,
        body,
        STANDARD_IMAGE_GENERATE_FIELDS,
    )
    if unknown_response is not None:
        return unknown_response

    prompt = body.get("prompt", "")
    requested_model = body.get("model", COMFY_DEFAULT_MODEL)
    model = requested_model
    size = body.get("size", "1024x1024")
    if str(model).startswith("ideogram4") and prompt:
        # Only skip conversion when the prompt is ALREADY a grounded Ideogram-4
        # caption (has real obj/text elements). JSON that merely wraps a prose
        # description with no elements (a common agent mistake) must still be
        # structurized — otherwise Ideogram gets an ungrounded caption and the
        # text/layout quality collapses. See _ideogram4_is_grounded_json.
        if not _ideogram4_is_grounded_json(prompt):
            prompt = await _ideogram4_structurize_prompt(
                _ideogram4_brief_from_prompt(prompt)
            )
    if receipt is not None:
        receipt.requested_model = str(requested_model) if requested_model is not None else None
    _record_image_input(receipt, "model")
    _record_image_input(receipt, "size")
    background, background_err = parse_image_background(body.get("background"))
    if background_err:
        _record_image_input(receipt, "background", rejected_reason=background_err)
        return Response(
            content=json.dumps({"error": {
                "message": background_err,
                "type": "invalid_request_error",
                "param": "background",
                "code": "invalid_value",
            }}),
            status_code=400,
            media_type="application/json",
        )
    if "background" in body:
        _record_image_input(receipt, "background")
    try:
        n = int(body.get("n", 1))
    except (TypeError, ValueError):
        _record_image_input(receipt, "n", rejected_reason="not_integer")
        return Response(
            content=json.dumps({"error": {"message": "n must be an integer", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    if n < 1 or n > 10:
        _record_image_input(receipt, "n", rejected_reason="out_of_range")
        return Response(
            content=json.dumps({"error": {"message": "n must be between 1 and 10", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    _record_image_input(receipt, "n")
    response_format = body.get(
        "response_format",
        _default_image_response_format(requested_model),
    )
    if response_format not in ("b64_json", "url"):
        _record_image_input(receipt, "response_format", rejected_reason="unsupported_value")
        return Response(
            content=json.dumps({"error": {"message": "response_format must be 'b64_json' or 'url'", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    _record_image_input(receipt, "response_format")
    headers = configured_downstream_headers(auth_context, "comfyui")
    try:
        model_resolution = resolve_model_for_operation("images", model)
    except ModelCompatibilityError as e:
        _record_image_input(receipt, "model", rejected_reason="model_incompatible")
        return model_compatibility_error(str(e))
    if model_resolution.is_openai_alias:
        print(f"[proxy] Model alias {model} -> {model_resolution.effective_model} for image generation", flush=True)
        model = model_resolution.effective_model

    if receipt is not None:
        receipt.resolved_model = str(model) if model is not None else None

    if not prompt:
        _record_image_input(receipt, "prompt", rejected_reason="missing")
        return Response(
            content=json.dumps({"error": {"message": "prompt is required", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )
    _record_image_input(receipt, "prompt")

    manifest = _find_image_manifest("image.generate", str(model))
    gateway_compose = _gateway_image_generate_compose(manifest)
    if gateway_compose == "offset_inpaint_tile":
        if background == "transparent":
            _record_image_input(
                receipt,
                "background",
                rejected_reason="unsupported_for_gateway_composition",
            )
            return Response(
                content=json.dumps({
                    "error": {
                        "message": "background=transparent is not supported for offset-inpaint tile generation",
                        "type": "invalid_request_error",
                        "param": "background",
                        "code": "unsupported_value",
                    }
                }),
                status_code=400,
                media_type="application/json",
            )
        return await _proxy_offset_inpaint_tile_request(
            request=request,
            body=body,
            auth_context=auth_context,
            receipt=receipt,
            manifest=manifest,
            prompt=prompt,
            model=str(model),
            size=str(size),
            n=n,
            response_format=response_format,
            headers=headers,
        )

    workflow = load_comfy_workflow(model)
    if not workflow:
        if receipt is not None:
            receipt.add_fallback_reason(f"unknown_model:{model}")
        return Response(
            content=json.dumps({"error": {"message": f"Unknown model: {model}", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )

    workflow = copy.deepcopy(workflow)
    meta = workflow.pop("_meta", {})
    workflow_name = str(meta.get("name") or model)
    if receipt is not None:
        receipt.requested_workflow = str(model) if model is not None else None
        receipt.resolved_workflow = workflow_name
    if manifest is not None:
        identity = manifest_identity(manifest)
        if receipt is not None:
            receipt.attach_manifest(
                manifest_id=identity.id,
                manifest_version=identity.version,
                manifest_hash=identity.manifest_hash,
            )
    else:
        _attach_image_manifest_if_available(receipt, "image.generate", str(model))
    if receipt is not None:
        decision = _apply_image_route_policy(
            receipt,
            auth_context,
            model=str(model) if model is not None else None,
            workflow=_policy_workflow_for_receipt(receipt),
            manifest=manifest,
        )
        if decision.denied:
            return _policy_denied_response(decision)
    target_decision = _workflow_target_policy_decision(
        auth_context,
        requested=str(model) if model is not None else None,
        resolved=workflow_name,
        manifest=manifest,
        extra_ids=(
            [_policy_workflow_for_receipt(receipt)]
            if _policy_workflow_for_receipt(receipt)
            else ()
        ),
    )
    if target_decision is not None and target_decision.denied:
        return _policy_denied_response(target_decision)
    background_decision = await resolve_image_background_decision(
        background or "auto",
        prompt,
        auth_context=auth_context,
    )

    prompt_node = meta.get("prompt_node")
    size_node = meta.get("size_node")
    seed_node = meta.get("seed_node")

    if prompt_node and prompt_node in workflow:
        workflow[prompt_node]["inputs"]["text"] = prompt

    width, height = parse_size(size)
    for _sn in ([size_node] if isinstance(size_node, str) else (size_node or [])):
        if _sn and _sn in workflow:
            workflow[_sn]["inputs"]["width"] = width
            workflow[_sn]["inputs"]["height"] = height
    workflow = _apply_manifest_route_inputs(
        manifest,
        workflow,
        {"prompt": prompt, "width": width, "height": height},
    )

    workflow, extension_response = await _apply_workflow_extensions_to_image_request(
        request_body=body,
        manifest=manifest,
        backend_request=workflow,
        standard_fields=STANDARD_IMAGE_GENERATE_FIELDS,
        receipt=receipt,
        auth_context=auth_context,
    )
    if extension_response is not None:
        return extension_response
    fixed_seed = _workflow_extension_integer(body, "seed")

    fallback_workflow = copy.deepcopy(workflow)
    set_workflow_background_removal(fallback_workflow, meta, False)

    if background_decision.remove_background:
        if set_workflow_background_removal(workflow, meta, True):
            print(
                f"[proxy] Image background removal enabled for {workflow_name} "
                f"(confidence={background_decision.confidence})",
                flush=True,
            )
            firehose_metrics.record_event(
                "image_background_removal",
                "background removal enabled",
                workflow=workflow_name,
                confidence=background_decision.confidence,
                source=background_decision.source,
            )
            workflow_name_for_batching = f"{workflow_name}+rmbg"
        else:
            print(f"[proxy] background removal requested but workflow {workflow_name} has no background_removal wiring", flush=True)
            firehose_metrics.record_event(
                "image_background_removal_fallback",
                "workflow has no RMBG wiring",
                workflow=workflow_name,
            )
            background_decision = ImageBackgroundRemovalDecision(
                remove_background=False,
                confidence=0.0,
                reason="workflow has no RMBG wiring",
                source="workflow_unsupported",
            )
            workflow_name_for_batching = workflow_name
    else:
        set_workflow_background_removal(workflow, meta, False)
        workflow_name_for_batching = workflow_name

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(COMFY_TIMEOUT, connect=10)) as client:
            return await _generate_images_with_adaptive_batching(
                request=request,
                client=client,
                base_workflow=workflow,
                meta=meta,
                seed_node=seed_node,
                seed_field="seed",
                workflow_name=workflow_name_for_batching,
                width=width,
                height=height,
                n_requested=n,
                response_format=response_format,
                headers=headers,
                background_removal_decision=background_decision,
                fallback_workflow=fallback_workflow,
                fallback_workflow_name=workflow_name,
                fixed_seed=fixed_seed,
                auth_context=auth_context,
            )
    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": {"message": "ComfyUI request timeout", "type": "api_error"}}),
            status_code=504,
            media_type="application/json"
        )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": {"message": f"ComfyUI unavailable at {COMFY_HOST}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"ComfyUI proxy error: {str(e)}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )


async def proxy_comfy_edit_request(
    request: Request,
    auth_context: AuthContext,
    receipt: RouteReceipt | None = None,
) -> Response:
    """Handle OpenAI-compatible image edit request via ComfyUI."""
    if not COMFY_ENABLED:
        if receipt is not None:
            receipt.add_fallback_reason("comfy_disabled")
        return Response(
            content=json.dumps({"error": {"message": "Image generation not enabled", "type": "service_unavailable"}}),
            status_code=503,
            media_type="application/json"
        )
    service_response = _service_availability_response(
        "comfyui",
        service_label="ComfyUI",
    )
    if service_response is not None:
        if receipt is not None:
            receipt.add_fallback_reason("comfy_operator_mode")
        return service_response

    form_dict: dict[str, Any] = {}
    try:
        form = await request.form()
    except Exception as e:
        _record_image_input(receipt, "body", rejected_reason="invalid_form")
        return Response(
            content=json.dumps({"error": {"message": f"Invalid form data: {e}", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )
    for key, value in form.multi_items():
        if key in form_dict:
            continue
        form_dict[str(key)] = value
    extensions_raw = form.get("extensions")
    if isinstance(extensions_raw, str):
        try:
            form_dict["extensions"] = json.loads(extensions_raw)
        except json.JSONDecodeError:
            _record_image_input(receipt, "extensions", rejected_reason="invalid_json")
            return Response(
                content=json.dumps({"error": {
                    "message": "extensions must be a JSON object string",
                    "type": "invalid_request_error",
                    "param": "extensions",
                    "code": "invalid_json",
                }}),
                status_code=400,
                media_type="application/json",
            )
    unknown_response = _record_unknown_image_fields(
        receipt,
        form_dict,
        STANDARD_IMAGE_EDIT_FIELDS,
        extra_allowed_fields=set(_extension_asset_field_references(form_dict).values()),
    )
    if unknown_response is not None:
        return unknown_response

    image_file = form.get("image") or form.get("image[]")
    mask_file = form.get("mask")
    prompt = form.get("prompt", "")
    requested_model = form.get("model", "flux2-klein-4b-edit")
    model = requested_model
    size = form.get("size", "")
    if receipt is not None:
        receipt.requested_model = str(requested_model) if requested_model is not None else None
    _record_image_input(receipt, "model")
    if size:
        _record_image_input(receipt, "size")
    background, background_err = parse_image_background(form.get("background"))
    if background_err:
        _record_image_input(receipt, "background", rejected_reason=background_err)
        return Response(
            content=json.dumps({
                "error": {
                    "message": background_err,
                    "type": "invalid_request_error",
                    "param": "background",
                    "code": "invalid_value",
                }
            }),
            status_code=400,
            media_type="application/json",
        )
    if form.get("background") is not None:
        _record_image_input(receipt, "background")
    try:
        n = int(form.get("n", 1))
    except (TypeError, ValueError):
        _record_image_input(receipt, "n", rejected_reason="not_integer")
        return Response(
            content=json.dumps({"error": {"message": "n must be an integer", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    if n < 1 or n > 10:
        _record_image_input(receipt, "n", rejected_reason="out_of_range")
        return Response(
            content=json.dumps({"error": {"message": "n must be between 1 and 10", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    _record_image_input(receipt, "n")
    response_format = str(form.get("response_format", "b64_json"))
    if response_format not in ("b64_json", "url"):
        _record_image_input(receipt, "response_format", rejected_reason="unsupported_value")
        return Response(
            content=json.dumps({"error": {"message": "response_format must be 'b64_json' or 'url'", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    _record_image_input(receipt, "response_format")
    headers = configured_downstream_headers(auth_context, "comfyui")
    try:
        model_resolution = resolve_model_for_operation("image_edits", model)
    except ModelCompatibilityError as e:
        _record_image_input(receipt, "model", rejected_reason="model_incompatible")
        return model_compatibility_error(str(e))
    if model_resolution.is_openai_alias:
        print(f"[proxy] Model alias {model} -> {model_resolution.effective_model} for image edit", flush=True)
        model = model_resolution.effective_model

    if not image_file:
        _record_image_input(receipt, "image", rejected_reason="missing")
        return Response(
            content=json.dumps({"error": {"message": "image is required", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )
    _record_image_input(receipt, "image[]" if form.get("image[]") else "image")

    if not prompt:
        _record_image_input(receipt, "prompt", rejected_reason="missing")
        return Response(
            content=json.dumps({"error": {"message": "prompt is required", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )
    _record_image_input(receipt, "prompt")

    if not (str(model).endswith("-edit") or str(model).endswith("-inpaint")):
        model = f"{model}-edit"
    if mask_file:
        model = _prefer_inpaint_workflow_for_mask(str(model))

    if receipt is not None:
        receipt.resolved_model = str(model) if model is not None else None

    workflow = load_comfy_workflow(model)
    if not workflow:
        if receipt is not None:
            receipt.add_fallback_reason(f"unknown_edit_model:{model}")
        return Response(
            content=json.dumps({"error": {"message": f"Unknown edit model: {model}", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(COMFY_TIMEOUT, connect=10)) as client:
            image_content = await image_file.read()
            mask_content = await mask_file.read() if mask_file else None
            mask_context: ImageEditMaskContext | None = None
            if mask_content is not None:
                mask_context, mask_error = _prepare_image_edit_mask_context(
                    source_image_content=image_content,
                    mask_content=mask_content,
                )
                if mask_error is not None:
                    _record_image_input(receipt, "mask", rejected_reason="invalid_mask")
                    return mask_error
                _record_image_input(receipt, "mask")

            files = {"image": (image_file.filename or "input.png", image_content, "image/png")}
            upload_resp = await client.post(f"{COMFY_HOST}/upload/image", headers=headers, files=files)

            if upload_resp.status_code != 200:
                return Response(
                    content=json.dumps({"error": {"message": f"Image upload failed: {upload_resp.text}", "type": "api_error"}}),
                    status_code=502,
                    media_type="application/json"
                )

            upload_result = upload_resp.json()
            uploaded_filename = upload_result.get("name")
            if not uploaded_filename:
                return Response(
                    content=json.dumps({"error": {"message": "No filename returned from upload", "type": "api_error"}}),
                    status_code=502,
                    media_type="application/json"
                )

            print(f"[proxy] Uploaded image: {uploaded_filename}", flush=True)

            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(image_content))
                img_width, img_height = img.size
                print(f"[proxy] Detected image size: {img_width}x{img_height}", flush=True)
            except Exception as e:
                print(f"[proxy] Could not detect image size: {e}, using default 1024x1024", flush=True)
                img_width, img_height = 1024, 1024

            if size:
                try:
                    w, h = size.lower().split("x")
                    img_width, img_height = int(w), int(h)
                except:
                    pass

            workflow = copy.deepcopy(workflow)
            meta = workflow.pop("_meta", {})
            workflow_name = str(meta.get("name") or model)
            if receipt is not None:
                receipt.requested_workflow = str(model) if model is not None else None
                receipt.resolved_workflow = workflow_name
            manifest = _find_image_manifest("image.edit", str(model))
            if manifest is not None:
                identity = manifest_identity(manifest)
                if receipt is not None:
                    receipt.attach_manifest(
                        manifest_id=identity.id,
                        manifest_version=identity.version,
                        manifest_hash=identity.manifest_hash,
                    )
            else:
                _attach_image_manifest_if_available(receipt, "image.edit", str(model))
            required_mask_input = _manifest_required_route_mask_input(manifest)
            if mask_context is None and required_mask_input:
                _record_image_input(receipt, "mask", rejected_reason="missing")
                return Response(
                    content=json.dumps({
                        "error": {
                            "message": f"mask is required for workflow input {required_mask_input}",
                            "type": "invalid_request_error",
                            "param": "mask",
                            "code": "missing_required_parameter",
                        }
                    }),
                    status_code=400,
                    media_type="application/json",
                )
            if receipt is not None:
                decision = _apply_image_route_policy(
                    receipt,
                    auth_context,
                    model=str(model) if model is not None else None,
                    workflow=_policy_workflow_for_receipt(receipt),
                    manifest=manifest,
                )
                if decision.denied:
                    return _policy_denied_response(decision)
            target_decision = _workflow_target_policy_decision(
                auth_context,
                requested=str(model) if model is not None else None,
                resolved=workflow_name,
                manifest=manifest,
                extra_ids=(
                    [_policy_workflow_for_receipt(receipt)]
                    if _policy_workflow_for_receipt(receipt)
                    else ()
                ),
            )
            if target_decision is not None and target_decision.denied:
                return _policy_denied_response(target_decision)
            extension_asset_fields, asset_error_response = _allowed_image_extension_asset_fields(
                request_body=form_dict,
                manifest=manifest,
                receipt=receipt,
            )
            if asset_error_response is not None:
                return asset_error_response
            background_decision = await resolve_image_background_decision(
                background or "auto",
                prompt,
                auth_context=auth_context,
                force_bypass=bool(mask_file),
            )

            prompt_node = meta.get("prompt_node")
            image_node = meta.get("image_node")
            seed_node = meta.get("seed_node")
            size_node = meta.get("size_node")

            if prompt_node and prompt_node in workflow:
                workflow[prompt_node]["inputs"]["text"] = prompt

            if image_node and image_node in workflow:
                workflow[image_node]["inputs"]["image"] = uploaded_filename

            mask_input_name = _manifest_route_managed_mask_input(manifest)
            mask_uses_workflow = False
            if mask_context is not None and mask_input_name and mask_content is not None:
                mask_files = {"image": ("normalized-mask.png", mask_context.edit_mask, "image/png")}
                mask_upload_resp = await client.post(
                    f"{COMFY_HOST}/upload/image",
                    headers=headers,
                    files=mask_files,
                )
                if mask_upload_resp.status_code != 200:
                    return Response(
                        content=json.dumps({"error": {"message": f"Mask upload failed: {mask_upload_resp.text}", "type": "api_error"}}),
                        status_code=502,
                        media_type="application/json"
                    )
                mask_upload_result = mask_upload_resp.json()
                uploaded_mask_filename = mask_upload_result.get("name")
                if not uploaded_mask_filename:
                    return Response(
                        content=json.dumps({"error": {"message": "No mask filename returned from upload", "type": "api_error"}}),
                        status_code=502,
                        media_type="application/json"
                    )
                workflow = bind_backend_request(
                    manifest,
                    "comfyui",
                    workflow,
                    {mask_input_name: uploaded_mask_filename},
                ).request
                mask_uses_workflow = True

            # Set size on Flux2Scheduler and EmptyFlux2LatentImage nodes.
            # V2 workflows use node 48 for scheduler and 47 for empty latent.
            for node_id in ["47", "48", size_node]:
                if node_id and node_id in workflow:
                    if "width" in workflow[node_id]["inputs"]:
                        workflow[node_id]["inputs"]["width"] = img_width
                    if "height" in workflow[node_id]["inputs"]:
                        workflow[node_id]["inputs"]["height"] = img_height
            workflow = _apply_manifest_route_inputs(
                manifest,
                workflow,
                {"width": img_width, "height": img_height},
            )

            workflow, extension_response = await _apply_workflow_extensions_to_image_request(
                request_body=form_dict,
                manifest=manifest,
                backend_request=workflow,
                standard_fields=STANDARD_IMAGE_EDIT_FIELDS,
                receipt=receipt,
                form=form,
                client=client,
                headers=headers,
                extension_asset_fields=extension_asset_fields,
                auth_context=auth_context,
            )
            if extension_response is not None:
                return extension_response

            image_transform: Optional[Callable[[bytes], bytes]] = None
            image_response_headers: dict[str, str] = {}
            if mask_context is not None:
                if mask_uses_workflow:
                    image_response_headers["X-Image-Mask"] = "workflow"
                    image_response_headers["X-Image-Mask-Source"] = mask_context.mask_mode
                else:
                    image_transform = lambda raw: _apply_image_edit_mask_composite(mask_context, raw)
                    image_response_headers["X-Image-Mask"] = "post-composite"
                    image_response_headers["X-Image-Mask-Source"] = mask_context.mask_mode
                    if receipt is not None:
                        receipt.add_warning("mask_mode:post_composite")

            fallback_workflow = copy.deepcopy(workflow)
            set_workflow_background_removal(fallback_workflow, meta, False)
            if background_decision.remove_background:
                if set_workflow_background_removal(workflow, meta, True):
                    print(
                        f"[proxy] Image edit background removal enabled for {workflow_name} "
                        f"(confidence={background_decision.confidence})",
                        flush=True,
                    )
                    firehose_metrics.record_event(
                        "image_background_removal",
                        "edit background removal enabled",
                        workflow=workflow_name,
                        confidence=background_decision.confidence,
                        source=background_decision.source,
                    )
                    workflow_name_for_batching = f"{workflow_name}+rmbg"
                else:
                    print(f"[proxy] background removal requested but edit workflow {workflow_name} has no background_removal wiring", flush=True)
                    firehose_metrics.record_event(
                        "image_background_removal_fallback",
                        "edit workflow has no RMBG wiring",
                        workflow=workflow_name,
                    )
                    background_decision = ImageBackgroundRemovalDecision(
                        remove_background=False,
                        confidence=0.0,
                        reason="workflow has no RMBG wiring",
                        source="workflow_unsupported",
                    )
                    workflow_name_for_batching = workflow_name
            else:
                set_workflow_background_removal(workflow, meta, False)
                workflow_name_for_batching = workflow_name

            # V2 uses RandomNoise with noise_seed, V1 used KSampler with seed.
            seed_field = "seed"
            if seed_node and seed_node in workflow:
                inputs = workflow[seed_node].get("inputs", {})
                if "noise_seed" in inputs:
                    seed_field = "noise_seed"

            return await _generate_images_with_adaptive_batching(
                request=request,
                client=client,
                base_workflow=workflow,
                meta=meta,
                seed_node=seed_node,
                seed_field=seed_field,
                workflow_name=workflow_name_for_batching,
                width=img_width,
                height=img_height,
                n_requested=n,
                response_format=response_format,
                headers=headers,
                background_removal_decision=background_decision,
                fallback_workflow=fallback_workflow,
                fallback_workflow_name=workflow_name,
                image_transform=image_transform,
                image_response_headers=image_response_headers,
                auth_context=auth_context,
            )

    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": {"message": "ComfyUI request timeout", "type": "api_error"}}),
            status_code=504,
            media_type="application/json"
        )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": {"message": f"ComfyUI unavailable at {COMFY_HOST}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"ComfyUI edit error: {str(e)}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )


# =============================================================================
# ComfyUI Backend (Video Generation — Sora-shaped async API)
# =============================================================================
#
# Endpoints (mirror Sora 2):
#   POST /v1/videos                     -> {video_id, status: "queued", ...}
#   GET  /v1/videos/{video_id}          -> {video_id, status, progress, ...}
#   GET  /v1/videos/{video_id}/content  -> binary mp4 (Content-Type: video/mp4)
#
# `video_id` is `video_<comfy_prompt_id>`. We round-trip ComfyUI's prompt id and
# look it up via /history/{prompt_id} on poll. The proxy is stateless across
# restarts — clients are expected to keep the id and re-poll. ComfyUI persists
# history for the life of its process.

VIDEO_ID_PREFIX = "video_"


# =============================================================================
# ComfyUI websocket listener for real-time progress
# =============================================================================
#
# A single long-lived websocket subscription per proxy process. ComfyUI emits
# events keyed by `prompt_id`; we cache the latest progress so /v1/videos/{id}
# can return real step/total/percent without polling /history every time.
#
# Phase heuristic: encoding ~0-5%, sampling 5-90%, decoding 90-100%. The
# sampler is the long-running phase, so its step/total drives most of the bar.
# When the listener is down, callers fall back to /history polling.

# Stable client_id used for both the websocket connection and the /prompt POST
# body. ComfyUI routes events for a submission to the matching client_id.
COMFY_CLIENT_ID = uuid.uuid4().hex

# prompt_id -> {status, percent, step, total, node, phase, error, updated_at}
_video_progress: dict[str, dict] = {}
_video_progress_lock = asyncio.Lock()

# Garbage-collect entries older than this many seconds (default 6h).
_VIDEO_PROGRESS_TTL = 6 * 3600

# Heuristic sampler-node class identifiers. We treat anything containing
# "Sampler" or "KSampler" in the node class as the sampling phase.
_SAMPLER_HINT_SUBSTRINGS = ("sampler", "ksampler")
# VAE decode / save / output nodes => post-sampling.
_DECODE_HINT_SUBSTRINGS = ("vaedecode", "savevideo", "videocombine", "save_video", "save")


def _node_class_from_workflow(workflow: dict | None, node_id: str | None) -> str:
    if not workflow or not node_id:
        return ""
    node = workflow.get(node_id) if isinstance(workflow, dict) else None
    if isinstance(node, dict):
        return str(node.get("class_type") or "").lower()
    return ""


# prompt_id -> workflow dict (kept alongside progress state to classify nodes
# into phases without re-fetching /history).
_video_workflows: dict[str, dict] = {}


def _classify_phase(node_class: str) -> str:
    nc = (node_class or "").lower()
    if any(s in nc for s in _SAMPLER_HINT_SUBSTRINGS):
        return "sampling"
    if any(s in nc for s in _DECODE_HINT_SUBSTRINGS):
        return "decoding"
    return "encoding"


def _phase_percent(phase: str, sampler_frac: float) -> int:
    """Map (phase, sampler progress 0..1) to overall percent.

    encoding => 0..5, sampling => 5..90, decoding => 90..99 (100 reserved for
    terminal success). Conservative because LTX I/O after sampling is fast.
    """
    sampler_frac = max(0.0, min(1.0, sampler_frac))
    if phase == "encoding":
        return 5
    if phase == "sampling":
        return int(round(5 + sampler_frac * 85))
    if phase == "decoding":
        return 92
    return 5


async def _record_event(event: dict) -> None:
    etype = event.get("type")
    data = event.get("data") or {}
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        return

    async with _video_progress_lock:
        entry = _video_progress.setdefault(prompt_id, {
            "status": "running",
            "percent": 0,
            "step": 0,
            "total": 0,
            "node": None,
            "node_class": None,
            "phase": "encoding",
            "error": None,
            "updated_at": time.time(),
        })

        wf = _video_workflows.get(prompt_id)

        if etype == "executing":
            node_id = data.get("node")
            if node_id is None:
                # null node => execution complete for this prompt (legacy signal).
                pass
            else:
                node_class = _node_class_from_workflow(wf, str(node_id))
                phase = _classify_phase(node_class)
                entry["node"] = str(node_id)
                entry["node_class"] = node_class or None
                entry["phase"] = phase
                # When entering a new node we don't yet know its step/total;
                # keep prior step/total but recompute phase percent.
                if phase != "sampling":
                    entry["step"] = 0
                    entry["total"] = 0
                    entry["percent"] = max(entry["percent"], _phase_percent(phase, 0.0))
        elif etype == "progress":
            value = int(data.get("value") or 0)
            maxv = int(data.get("max") or 0)
            node_id = str(data.get("node") or entry.get("node") or "")
            node_class = _node_class_from_workflow(wf, node_id) or entry.get("node_class") or ""
            phase = _classify_phase(node_class) if node_class else entry.get("phase", "sampling")
            entry["node"] = node_id or entry.get("node")
            entry["node_class"] = node_class or entry.get("node_class")
            entry["phase"] = phase
            entry["step"] = value
            entry["total"] = maxv
            frac = (value / maxv) if maxv > 0 else 0.0
            entry["percent"] = _phase_percent(phase, frac)
        elif etype == "progress_state":
            # Modern ComfyUI per-node progress map. Pick the running node with
            # the highest fractional progress as the representative step/total.
            nodes = data.get("nodes") or {}
            best = None  # (frac, value, max, node_id, node_class)
            for node_id, ns in nodes.items():
                state = (ns or {}).get("state")
                if state != "running":
                    continue
                v = float(ns.get("value") or 0)
                m = float(ns.get("max") or 0)
                frac = (v / m) if m > 0 else 0.0
                node_class = _node_class_from_workflow(wf, str(node_id))
                # Prefer sampler nodes when present; else keep best by frac.
                weight = (1 if any(s in node_class for s in _SAMPLER_HINT_SUBSTRINGS) else 0)
                key = (weight, frac)
                if best is None or key > best[0]:
                    best = (key, int(v), int(m), str(node_id), node_class)
            if best is not None:
                _, v_i, m_i, node_id, node_class = best
                phase = _classify_phase(node_class)
                entry["node"] = node_id
                entry["node_class"] = node_class or None
                entry["phase"] = phase
                entry["step"] = v_i
                entry["total"] = m_i
                frac = (v_i / m_i) if m_i > 0 else 0.0
                entry["percent"] = _phase_percent(phase, frac)
        elif etype == "execution_success":
            entry["status"] = "succeeded"
            entry["percent"] = 100
            entry["phase"] = "done"
            entry["ended_at"] = time.time()
        elif etype == "execution_error":
            entry["status"] = "failed"
            err = {k: data.get(k) for k in ("exception_message", "exception_type", "node_id", "node_type") if k in data}
            entry["error"] = err or "execution_error"
            entry["ended_at"] = time.time()

        entry["updated_at"] = time.time()


async def _gc_video_progress() -> None:
    cutoff = time.time() - _VIDEO_PROGRESS_TTL
    async with _video_progress_lock:
        stale = [pid for pid, e in _video_progress.items() if e.get("updated_at", 0) < cutoff]
        for pid in stale:
            _video_progress.pop(pid, None)
            _video_workflows.pop(pid, None)


# How often the reconciliation loop polls ComfyUI for authoritative state.
_VIDEO_RECONCILE_INTERVAL = 15
# How long to keep entries with a terminal status after ended_at before GC.
_VIDEO_RECONCILE_RETAIN = 5 * 60
# How many recent /history items to consider when reconciling.
_VIDEO_RECONCILE_HISTORY_LIMIT = 200


def _extract_prompt_ids(queue_block: list) -> set[str]:
    """ComfyUI /queue entries are tuple-like lists: [priority, prompt_id, ...]."""
    out: set[str] = set()
    if not isinstance(queue_block, list):
        return out
    for entry in queue_block:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            pid = entry[1]
            if isinstance(pid, str) and pid:
                out.add(pid)
    return out


def _history_terminal_state(job: dict) -> tuple[str, Optional[str]]:
    """Return (terminal_status, error_text) for a /history entry.

    terminal_status is 'succeeded', 'failed', or 'running' (not yet terminal).
    """
    error_message = _comfy_execution_error_message(job)
    if error_message:
        return ("failed", error_message)

    status_block = (job or {}).get("status", {}) or {}
    if not status_block.get("completed"):
        return ("running", None)
    return ("succeeded", None)


async def _fetch_comfy_state() -> tuple[Optional[set[str]], Optional[dict]]:
    """Fetch ComfyUI authoritative state: (active_prompt_ids, history_map).

    Returns (None, None) on any error. Uses 2s timeouts per the spec.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=2.0)) as client:
            q_resp = await client.get(f"{COMFY_HOST}/queue")
            h_resp = await client.get(f"{COMFY_HOST}/history?max_items={_VIDEO_RECONCILE_HISTORY_LIMIT}")
    except Exception as e:
        print(f"[proxy] video reconcile: ComfyUI fetch failed ({e})", flush=True)
        return (None, None)
    if q_resp.status_code != 200 or h_resp.status_code != 200:
        return (None, None)
    try:
        q_data = q_resp.json() or {}
    except Exception:
        q_data = {}
    try:
        h_data = h_resp.json() or {}
    except Exception:
        h_data = {}
    active: set[str] = set()
    active |= _extract_prompt_ids(q_data.get("queue_running") or [])
    active |= _extract_prompt_ids(q_data.get("queue_pending") or [])
    return (active, h_data if isinstance(h_data, dict) else {})


async def _reconcile_video_progress() -> None:
    """Reconcile _video_progress against ComfyUI's /queue and /history.

    - In /queue (running or pending): leave as-is.
    - In /history: mark succeeded/failed with terminal info.
    - In neither: mark failed/orphaned (ComfyUI does not know the prompt_id).
    - Entries with ended_at older than _VIDEO_RECONCILE_RETAIN: garbage-collect.
    """
    active, history = await _fetch_comfy_state()
    now = time.time()

    async with _video_progress_lock:
        if not _video_progress:
            return

        if active is None or history is None:
            # ComfyUI unreachable: only run the time-based GC, do not mutate
            # statuses (could be a transient blip).
            _gc_video_progress_terminal_locked(now)
            return

        for prompt_id in list(_video_progress.keys()):
            entry = _video_progress.get(prompt_id)
            if not isinstance(entry, dict):
                continue
            current_status = entry.get("status")

            if prompt_id in active:
                # Real job in flight; touch updated_at so it doesn't look stale
                # but otherwise let the ws listener drive it.
                continue

            if prompt_id in history:
                job = history.get(prompt_id) or {}
                terminal, err_text = _history_terminal_state(job)
                if terminal == "succeeded" and current_status != "succeeded":
                    entry["status"] = "succeeded"
                    entry["percent"] = 100
                    entry["phase"] = "done"
                    entry.setdefault("ended_at", now)
                    entry["updated_at"] = now
                elif terminal == "failed" and current_status != "failed":
                    entry["status"] = "failed"
                    if err_text and not entry.get("error"):
                        entry["error"] = err_text
                    entry.setdefault("ended_at", now)
                    entry["updated_at"] = now
                continue

            # Not in queue, not in history => orphaned. Only mark non-terminal
            # entries; leave existing succeeded/failed alone (they may already
            # have been GC'd from /history but already terminal locally).
            if current_status in ("queued", "running", None):
                entry["status"] = "failed"
                entry["error"] = "orphaned: ComfyUI does not report this prompt_id"
                entry.setdefault("ended_at", now)
                entry["updated_at"] = now

        _gc_video_progress_terminal_locked(now)


def _gc_video_progress_terminal_locked(now: float) -> None:
    """Drop terminal entries whose ended_at is older than retain window.

    Caller must hold _video_progress_lock.
    """
    cutoff = now - _VIDEO_RECONCILE_RETAIN
    drop: list[str] = []
    for pid, e in _video_progress.items():
        if not isinstance(e, dict):
            drop.append(pid)
            continue
        if e.get("status") in ("succeeded", "failed"):
            ended = e.get("ended_at")
            # If terminal but no ended_at (legacy entry), seed it so it has
            # a fair retention window starting now instead of being dropped.
            if not ended:
                e["ended_at"] = now
                continue
            if ended < cutoff:
                drop.append(pid)
    for pid in drop:
        _video_progress.pop(pid, None)
        _video_workflows.pop(pid, None)


async def _reconcile_video_progress_loop(stop_event: asyncio.Event) -> None:
    """Long-running reconciliation loop. Never crashes on transient errors."""
    while not stop_event.is_set():
        try:
            await _reconcile_video_progress()
        except Exception as e:  # noqa: BLE001
            print(f"[proxy] video reconcile loop error: {e}", flush=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_VIDEO_RECONCILE_INTERVAL)
        except asyncio.TimeoutError:
            continue


# Listener state (so handlers can tell whether the ws is up).
_comfy_ws_connected = False


async def _comfy_ws_listener(stop_event: asyncio.Event) -> None:
    """Long-lived websocket subscription to ComfyUI with reconnect+backoff."""
    global _comfy_ws_connected
    try:
        from websockets.asyncio.client import connect as ws_connect
    except Exception as e:
        print(f"[proxy] ComfyUI ws listener disabled (websockets import failed: {e})", flush=True)
        return

    # ComfyUI ws lives at the same host/port as the REST API.
    ws_url = COMFY_HOST.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/ws?clientId={COMFY_CLIENT_ID}"
    backoff = 1.0
    last_gc = time.time()

    while not stop_event.is_set():
        try:
            async with ws_connect(ws_url, max_size=8 * 1024 * 1024, ping_interval=20, ping_timeout=20) as ws:
                _comfy_ws_connected = True
                backoff = 1.0
                print(f"[proxy] ComfyUI ws connected: {ws_url}", flush=True)
                async for raw in ws:
                    if stop_event.is_set():
                        break
                    if isinstance(raw, (bytes, bytearray)):
                        # Binary frames (preview images). Ignore.
                        continue
                    try:
                        evt = json.loads(raw)
                    except Exception:
                        continue
                    try:
                        await _record_event(evt)
                    except Exception as e:
                        print(f"[proxy] ws event handler error: {e}", flush=True)
                    if time.time() - last_gc > 600:
                        await _gc_video_progress()
                        last_gc = time.time()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _comfy_ws_connected = False
            print(f"[proxy] ComfyUI ws disconnected ({e}); reconnect in {backoff:.0f}s", flush=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
        else:
            _comfy_ws_connected = False
    _comfy_ws_connected = False


def _video_progress_snapshot(prompt_id: str) -> Optional[dict]:
    # Synchronous read; the dict is mutated under lock by the listener but a
    # plain copy here is acceptable (we only read scalars).
    entry = _video_progress.get(prompt_id)
    if entry is None:
        return None
    return dict(entry)


def _resolve_video_workflow(model: str) -> tuple[Optional[str], Optional[dict]]:
    """Map a client-supplied `model` to (workflow_name, workflow_cfg).

    Resolution order:
      1. Explicit workflow name match in `videos.workflows`.
      2. Alias match in `videos.aliases`.
      3. Empty -> default workflow.
    Returns (None, None) if the resolved name doesn't exist.
    """
    candidate = (model or "").strip()
    if not candidate:
        candidate = VIDEO_DEFAULT_WORKFLOW

    # 1) direct workflow name
    if candidate in VIDEO_WORKFLOWS:
        return candidate, VIDEO_WORKFLOWS[candidate]

    # 2) alias
    alias_target = VIDEO_ALIASES.get(candidate.lower())
    if alias_target and alias_target in VIDEO_WORKFLOWS:
        return alias_target, VIDEO_WORKFLOWS[alias_target]

    return None, None


def _load_video_workflow_file(workflow_cfg: dict) -> Optional[dict]:
    """Load the JSON for a configured video workflow.

    Attaches the UI-format twin under `__ui_workflow__` so the submit site can
    embed it via `extra_data.extra_pnginfo.workflow` for mp4 PNGInfo metadata.
    """
    file_path = workflow_cfg.get("file")
    if not file_path:
        return None
    p = Path(file_path)
    if not p.is_absolute():
        # Resolve relative paths against the proxy's directory.
        p = Path(__file__).parent / p
    if not p.exists():
        return None
    with open(p) as f:
        workflow = json.load(f)
    errors = validate_comfyui_workflow_graph(workflow)
    if errors:
        print(
            f"[proxy] invalid video ComfyUI workflow {p}: {'; '.join(errors)}",
            flush=True,
        )
        return None
    ui = _load_ui_workflow(p)
    if ui is not None:
        workflow["__ui_workflow__"] = ui
    return workflow


def _find_video_manifest(workflow_name: str, workflow_cfg: dict) -> Optional[dict]:
    manifest_path = workflow_cfg.get("manifest")
    candidates = []
    if manifest_path:
        candidates.append(Path(manifest_path))
    candidates.append(Path("manifests") / "workflows" / f"{workflow_name}.yml")

    for candidate in candidates:
        p = candidate
        if not p.is_absolute():
            p = Path(__file__).parent / p
        if not p.exists():
            continue
        try:
            return load_workflow_manifest(p)
        except ManifestError:
            raise
        except Exception as e:
            raise ManifestError(f"failed to load video workflow manifest {p}: {e}") from e
    return None


def _parse_size_clamped(size_str: str, max_size_str: str) -> tuple[int, int]:
    """Parse 'WxH' and clamp to per-workflow max_size."""
    width, height = parse_size(size_str or "1280x720")
    if max_size_str:
        try:
            mw, mh = parse_size(max_size_str)
            if mw and mh:
                width = min(width, mw)
                height = min(height, mh)
        except Exception:
            pass
    # LTX requires multiples of 32 for width/height.
    width = max(64, (width // 32) * 32)
    height = max(64, (height // 32) * 32)
    return width, height


def _seconds_to_length(seconds: float, frame_rate: float) -> int:
    """Translate request `seconds` to LTX latent `length` (must be 8k+1).

    Rounds to the *nearest* representable 8k+1 length (vs flooring). For
    fps=24 this gives ~0.333s granularity, so a request like seconds=3
    produces length=73 (3.0417s actual, +0.0417s drift) instead of the
    old length=65 (2.7083s, -0.2917s drift). Callers should compare
    `length / frame_rate` against `seconds` to decide whether to surface
    `X-Duration-Adjusted`.
    """
    frames = max(1, int(round(seconds * frame_rate)))
    # LTX latent length convention: lengths of form 8k+1 (1, 9, 17, ...)
    # Round to nearest k, not floor.
    k = max(1, int(round((frames - 1) / 8.0)))
    length = k * 8 + 1
    if length < 9:
        length = 9
    return length


def _long_form_chunks(seconds: float, frame_rate: float,
                      max_length_sec: int = 10, max_loops: int = 50) -> tuple[int, int, float]:
    """For ltx-2.3-long: search for (LENGTH_seconds, LOOPS) closest to `seconds`.

    Empirically verified formula (req=2s → cf=49 frames → 97 rendered frames):
      chunk0_frames = ((round(LENGTH * fps - 1)/8) * 8 + 1)   # 8N+1 stride
      total_frames  = chunk0_frames * (LOOPS + 2) - 1         # see note
      total_seconds = total_frames / fps

    The (LOOPS + 2) factor (rather than +1) reflects the RuneXX easy
    forLoopStart node, whose body executes once even at total=LOOPS=0; the
    workflow then runs `LOOPS` body iterations on top of that, plus the
    initial chunk0 outside the loop. The trailing `- 1` is the latent-stride
    boundary subtraction observed at the VAE concat point. This matches the
    long-form bug report (req=8s with default LENGTH=10, LOOPS=0 produced
    241*2-1 = 481 frames @ 24fps = 20.04s).

    LENGTH is an INTConstant (integer seconds) so granularity in `total_seconds`
    is determined by which (LENGTH, LOOPS) pairs we can hit. We pick the (L, N)
    minimizing |total_seconds - seconds|; ties broken by smaller N (faster,
    closer to single-shot quality), then smaller L.

    Returns: (LENGTH_seconds, LOOPS, actual_seconds)
    """
    def chunk_frames(L_sec: int) -> int:
        a = L_sec * frame_rate
        k = max(1, int(round((a - 1) / 8.0)))
        return k * 8 + 1

    best = None  # (key, L, n, total)
    for L in range(1, max_length_sec + 1):
        cf = chunk_frames(L)
        for n in range(0, max_loops + 1):
            total_frames = cf * (n + 2) - 1
            total = total_frames / frame_rate
            err = abs(total - seconds)
            key = (round(err, 6), n, L)
            if best is None or key < best[0]:
                best = (key, L, n, total)
    _, L, n, total = best
    return L, n, total


def _video_id_from_prompt_id(prompt_id: str) -> str:
    return f"{VIDEO_ID_PREFIX}{prompt_id}"


def _prompt_id_from_video_id(video_id: str) -> Optional[str]:
    if not video_id or not video_id.startswith(VIDEO_ID_PREFIX):
        return None
    pid = video_id[len(VIDEO_ID_PREFIX):]
    return pid or None


async def _comfy_history(client: httpx.AsyncClient, prompt_id: str, headers: dict) -> Optional[dict]:
    try:
        resp = await client.get(f"{COMFY_HOST}/history/{prompt_id}", headers=headers, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data.get(prompt_id) if isinstance(data, dict) else None
    except Exception:
        return None


async def _comfy_queue_status(client: httpx.AsyncClient, prompt_id: str, headers: dict) -> str:
    """Return one of: queued, running, unknown.

    ComfyUI's `/queue` returns {queue_running: [...], queue_pending: [...]}, where
    each entry is a tuple-like list `[priority, prompt_id, ...]`.
    """
    try:
        resp = await client.get(f"{COMFY_HOST}/queue", headers=headers, timeout=10)
        if resp.status_code != 200:
            return "unknown"
        data = resp.json() or {}
        for entry in data.get("queue_running", []) or []:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[1] == prompt_id:
                return "running"
        for entry in data.get("queue_pending", []) or []:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[1] == prompt_id:
                return "queued"
    except Exception:
        return "unknown"
    return "unknown"


def _job_status_from_history(job: dict) -> tuple[str, int]:
    """Translate a ComfyUI history entry to (sora_status, progress_0_to_100)."""
    if _comfy_execution_error_message(job):
        return "failed", 100

    status_block = (job or {}).get("status", {}) or {}
    if status_block.get("completed"):
        return "succeeded", 100
    # If history has the entry but not yet completed, treat as running.
    return "running", 50


def _extract_video_output(job: dict) -> Optional[dict]:
    """Find the first video file produced by the workflow.

    ComfyUI's SaveVideo node writes to outputs[node_id]['videos'] = [{filename, subfolder, type}].
    Some older workflows surface the file under 'gifs' or 'images' (frame sequences) instead.
    """
    outputs = (job or {}).get("outputs", {}) or {}
    for node_id, node_output in outputs.items():
        for key in ("videos", "gifs", "images"):
            entries = node_output.get(key) or []
            for entry in entries:
                filename = entry.get("filename") or ""
                if filename.lower().endswith((".mp4", ".webm", ".mov", ".mkv")):
                    return {
                        "filename": filename,
                        "subfolder": entry.get("subfolder", ""),
                        "type": entry.get("type", "output"),
                    }
    return None


class _InputReferenceError(ValueError):
    """Raised when an input_reference value can't be parsed/fetched."""


_DATA_URI_PREFIX = "data:"


def _resolve_cached_image_asset(
    asset_id: str,
    auth_context: AuthContext | None = None,
) -> tuple[bytes, str]:
    parsed = _parse_asset_id(asset_id)
    if parsed is None or parsed[0] != "img":
        raise _InputReferenceError("asset_id must reference a cached image asset")
    path = asset_cache_path(asset_id)
    if path is None or not path.exists():
        raise _InputReferenceError("asset_id not found (may have been evicted)")
    if not _asset_access_allowed(asset_id, auth_context):
        raise _InputReferenceError("asset_id is not accessible for this API key")
    return path.read_bytes(), f"{asset_id}{path.suffix or '.png'}"


async def _resolve_input_reference(
    value,
    auth_context: AuthContext | None = None,
) -> tuple[bytes, str]:
    """Resolve an OpenAI-shaped input_reference into (bytes, filename).

    Accepts (in priority order):
      * dict {"type": "input_image", "image_url": {...}} or {"image_url": "..."}
        (OpenAI vision-style — pull image_url out and recurse)
      * absolute filesystem path (must exist and be readable)
      * http:// or https:// URL (downloaded with httpx)
      * data: URI ("data:image/png;base64,...")
      * raw base64 string (fallback when the above don't match)

    Returns the raw bytes plus a filename suitable for /upload/image.
    """
    if value is None:
        raise _InputReferenceError("input_reference is empty")

    # Unwrap OpenAI vision-style content blocks. We don't need full schema
    # support — pull out a string we can resolve.
    if isinstance(value, dict):
        asset_id = value.get("asset_id")
        if isinstance(asset_id, str):
            return _resolve_cached_image_asset(asset_id.strip(), auth_context)
        for key in ("image_url", "url", "input_image"):
            inner = value.get(key)
            if isinstance(inner, dict):
                inner = inner.get("url") or inner.get("image_url")
            if isinstance(inner, str):
                return await _resolve_input_reference(inner, auth_context)
        raise _InputReferenceError(
            "input_reference dict missing a recognizable image_url/url field"
        )

    if not isinstance(value, str):
        raise _InputReferenceError(f"input_reference must be a string, got {type(value).__name__}")

    s = value.strip()
    if not s:
        raise _InputReferenceError("input_reference is empty")

    if s.startswith("asset:"):
        return _resolve_cached_image_asset(s[len("asset:"):].strip(), auth_context)
    if s.startswith("img_"):
        return _resolve_cached_image_asset(s, auth_context)

    # Filesystem path.
    try:
        p = Path(s)
        if p.is_absolute() and p.is_file():
            return p.read_bytes(), p.name
    except OSError:
        pass

    # HTTP(S) URL.
    if s.startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=True) as c:
                r = await c.get(s)
                r.raise_for_status()
                # Derive a filename from the URL path; fall back to a safe default.
                fname = s.rsplit("/", 1)[-1].split("?", 1)[0] or "input_reference.png"
                if "." not in fname:
                    fname += ".png"
                return r.content, fname
        except Exception as e:
            raise _InputReferenceError(f"failed to download input_reference: {e}") from e

    # data: URI.
    if s.startswith(_DATA_URI_PREFIX):
        try:
            header, _, b64 = s.partition(",")
            data = base64.b64decode(b64, validate=False)
            ext = "png"
            # data:image/jpeg;base64,...
            if "image/" in header:
                mime = header.split("image/", 1)[1].split(";", 1)[0].strip().lower()
                if mime in ("jpeg", "jpg", "png", "webp", "gif"):
                    ext = "jpg" if mime == "jpeg" else mime
            return data, f"input_reference.{ext}"
        except Exception as e:
            raise _InputReferenceError(f"failed to decode data: URI: {e}") from e

    # Raw base64 fallback. Only accept if it decodes to non-trivial bytes.
    try:
        data = base64.b64decode(s, validate=True)
        if len(data) >= 64:  # Heuristic: any image is bigger than 64 bytes.
            return data, "input_reference.png"
    except Exception:
        pass

    raise _InputReferenceError(
        "input_reference must be an absolute path, http(s) URL, data: URI, or base64 string"
    )


class _VideoFormParseError(Exception):
    """Raised when an incoming multipart /v1/videos request can't be normalized."""


_VIDEO_FORM_FILE_FIELDS = frozenset({"input_reference"})


async def _read_video_create_form_body(request: Request) -> dict[str, Any]:
    """Normalize an openai-python multipart /v1/videos request into a JSON body.

    The OpenAI Python SDK always submits videos.create as multipart/form-data so
    that `input_reference` can be uploaded as a file. The rest of the video
    handler is written against a JSON-shaped dict (prompt/model/size/seconds/...
    as scalars, input_reference as path / URL / data: URI / base64). Here we
    flatten the form back into that shape so the downstream pipeline is unchanged.

    File uploads for `input_reference` are converted to a data: URI so the
    existing input_reference resolver can decode them without any new branch.
    """
    try:
        form = await request.form()
    except Exception as exc:
        raise _VideoFormParseError(f"Invalid multipart body: {exc}") from exc

    body: dict[str, Any] = {}
    for name, value in form.multi_items():
        if name in _VIDEO_FORM_FILE_FIELDS and hasattr(value, "read") and hasattr(value, "filename"):
            try:
                raw = await value.read()
            except Exception as exc:
                raise _VideoFormParseError(
                    f"failed to read uploaded {name}: {exc}"
                ) from exc
            mime = getattr(value, "content_type", None) or "application/octet-stream"
            body[name] = "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
            continue
        # Plain scalar form field. OpenAI's SDK sends everything else as text;
        # callers may send numeric/bool-looking strings, which the existing JSON
        # path also accepts as strings (it coerces seconds/seed via float()/int()).
        body[name] = value if not hasattr(value, "read") else str(value)
    return body


async def proxy_comfy_video_create(request: Request, auth_context: AuthContext) -> Response:
    """POST /v1/videos — submit a job and return {video_id, status: queued}."""
    if not COMFY_ENABLED:
        return Response(
            content=json.dumps({"error": {"message": "Video generation not enabled", "type": "service_unavailable"}}),
            status_code=503,
            media_type="application/json"
        )
    service_response = _service_availability_response(
        "comfyui",
        service_label="ComfyUI",
    )
    if service_response is not None:
        return service_response

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        # openai-python's videos.create always uploads as multipart/form-data so
        # that input_reference can be a binary upload. Normalize the form into
        # the JSON body shape the rest of this handler expects.
        try:
            body = await _read_video_create_form_body(request)
        except _VideoFormParseError as exc:
            return Response(
                content=json.dumps({"error": {
                    "message": str(exc),
                    "type": "invalid_request_error",
                }}),
                status_code=400,
                media_type="application/json",
            )
    else:
        try:
            body = await request.json()
        except Exception:
            return Response(
                content=json.dumps({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}),
                status_code=400,
                media_type="application/json"
            )
    unknown_video_fields = detect_unknown_top_level_fields(
        body,
        STANDARD_VIDEO_CREATE_FIELDS,
    )
    if unknown_video_fields:
        first_name, first_reason = next(iter(unknown_video_fields.items()))
        return Response(
            content=json.dumps({"error": {
                "message": first_reason,
                "type": "invalid_request_error",
                "param": first_name,
                "code": "unknown_parameter",
            }}),
            status_code=400,
            media_type="application/json",
        )

    prompt = (body.get("prompt") or "").strip()
    requested_model = body.get("model") or VIDEO_DEFAULT_WORKFLOW
    size_from_body = body.get("size")
    size = size_from_body or "1280x720"
    seconds = body.get("seconds")
    seed = body.get("seed")
    input_reference = body.get("input_reference")  # optional path / URL / data-URI / raw b64 (I2V)
    image_strength = body.get("image_strength")  # optional float 0..1, default 1.0 when input_reference present

    if not prompt:
        return Response(
            content=json.dumps({"error": {"message": "prompt is required", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )

    # OpenAI-compat: rewrite sora-* aliases to the local default before workflow lookup.
    try:
        model_resolution = resolve_model_for_operation("videos", requested_model)
    except ModelCompatibilityError as e:
        return model_compatibility_error(str(e))
    if model_resolution.is_openai_alias:
        print(
            f"[proxy] Video model alias {requested_model} -> {model_resolution.effective_model}",
            flush=True,
        )
        requested_model = model_resolution.effective_model

    workflow_name, workflow_cfg = _resolve_video_workflow(requested_model)
    if not workflow_cfg:
        return Response(
            content=json.dumps({"error": {"message": f"Unknown video model/workflow: {requested_model}", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )
    policy_decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="openai.video",
            intent=INTENT_VIDEO_GENERATE,
            model=str(requested_model) if requested_model is not None else None,
            workflow=f"video.generate.{workflow_name}",
            backend="comfyui",
            capability_exposure="default",
            capability_trust_tier="stable",
            capability_resource_tier="expensive",
        ),
    )
    if policy_decision.denied:
        return _policy_denied_response(policy_decision)
    target_decision = _workflow_target_policy_decision(
        auth_context,
        requested=str(requested_model) if requested_model is not None else None,
        resolved=str(workflow_name),
        extra_ids=(f"video.generate.{workflow_name}",),
    )
    if target_decision is not None and target_decision.denied:
        return _policy_denied_response(target_decision)

    workflow_template = _load_video_workflow_file(workflow_cfg)
    if workflow_template is None:
        return Response(
            content=json.dumps({"error": {"message": f"Workflow file missing for {workflow_name}", "type": "api_error"}}),
            status_code=500,
            media_type="application/json"
        )

    # Validate / clamp request inputs.
    max_seconds = float(workflow_cfg.get("max_seconds", 8))
    min_seconds = float(workflow_cfg.get("min_seconds", 1))
    try:
        seconds_val = float(seconds) if seconds is not None else min(5.0, max_seconds)
    except (TypeError, ValueError):
        return Response(
            content=json.dumps({"error": {"message": "seconds must be a number", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )
    if seconds_val < min_seconds or seconds_val > max_seconds:
        return Response(
            content=json.dumps({
                "error": {
                    "message": (
                        f"seconds must be between {min_seconds} and {max_seconds} for model {workflow_name}"
                    ),
                    "type": "invalid_request_error",
                }
            }),
            status_code=400,
            media_type="application/json"
        )

    width, height = _parse_size_clamped(size, workflow_cfg.get("max_size", "1280x720"))
    frame_rate = float(workflow_cfg.get("frame_rate", 25))
    length = _seconds_to_length(seconds_val, frame_rate)
    # `actual_seconds` is what the rendered mp4 will actually contain (within
    # codec/container rounding). It starts as the LTX 8N+1-snapped duration for
    # short workflows; long-form overrides this further down.
    actual_seconds = length / frame_rate
    duration_adjustment_reason = None

    # Inject parameters into a deep copy of the workflow.
    workflow = copy.deepcopy(workflow_template)
    meta = workflow.pop("_meta", {})

    prompt_node = meta.get("prompt_node")
    neg_node = meta.get("negative_prompt_node")
    size_node = meta.get("size_node")
    # length_node must be explicit. Falling back to size_node is unsafe because
    # source-of-truth Primitive width/height nodes use the same `value` key as a
    # PrimitiveInt length node, so the size value would be overwritten by length.
    # Workflows that don't drive length per-request (e.g. long-form chunked loops
    # that compute it from chunk_seconds) should leave length_node unset.
    length_node = meta.get("length_node")
    seed_node = meta.get("seed_node")
    fr_node = meta.get("frame_rate_node")

    if prompt_node and prompt_node in workflow:
        workflow[prompt_node]["inputs"]["text"] = prompt

    # negative prompt left at template default unless body overrides
    negative_prompt = body.get("negative_prompt")
    if negative_prompt and neg_node and neg_node in workflow:
        workflow[neg_node]["inputs"]["text"] = negative_prompt

    # Helpers to accept either a single node id or a list of node ids in _meta.
    # Some workflows have a single source-of-truth Primitive node feeding many
    # consumers; others spread the field across consumer nodes directly. Accept
    # both so workflow JSONs can stay close to ComfyUI's native format.
    def _as_list(v):
        if v is None:
            return []
        return list(v) if isinstance(v, (list, tuple)) else [v]

    def _set_field(node_id, candidate_keys, value):
        if not node_id or node_id not in workflow:
            return False
        inputs = workflow[node_id].get("inputs", {})
        for k in candidate_keys:
            if k in inputs:
                inputs[k] = value
                return True
        return False

    # size_node forms supported:
    #   "7"                   -> single node with both width/height keys (e.g. EmptyLatentImage)
    #   ["W_NID", "H_NID"]    -> two separate INTConstant/PrimitiveInt source nodes; index 0 is
    #                            width, index 1 is height. Each takes a single `value` key.
    #   width_node/height_node in _meta override the list/single behavior.
    width_node = meta.get("width_node")
    height_node = meta.get("height_node")
    size_list = _as_list(size_node)
    if width_node is None and isinstance(size_node, (list, tuple)) and len(size_list) >= 2:
        width_node = size_list[0]
        height_node = size_list[1]
    if width_node is not None or height_node is not None:
        if width_node:
            _set_field(width_node, ["width", "value"], width)
        if height_node:
            _set_field(height_node, ["height", "value"], height)
    else:
        for nid in size_list:
            _set_field(nid, ["width"], width)
            _set_field(nid, ["height"], height)

    # length_node: also accept "value" (PrimitiveInt source-of-truth pattern)
    # and "frames_number" (LTXVEmptyLatentAudio variant). length_nodes (plural)
    # in _meta is honored too for fan-out when multiple consumers need the same
    # length without a Primitive node.
    length_targets = _as_list(meta.get("length_nodes")) or _as_list(length_node)
    for nid in length_targets:
        _set_field(nid, ["length", "value", "frames_number"], length)
    if length_targets and abs(actual_seconds - seconds_val) > 0.05:
        # 8N+1 LTX latent stride => ~0.333s granularity at fps=24. Surface it.
        duration_adjustment_reason = "ltx_latent_8np1_stride"

    fr_targets = _as_list(meta.get("frame_rate_nodes")) or _as_list(fr_node)
    for nid in fr_targets:
        _set_field(nid, ["frame_rate", "value"], frame_rate)

    try:
        seed_val = int(seed) if seed is not None else random.randint(0, 2**32 - 1)
    except (TypeError, ValueError):
        seed_val = random.randint(0, 2**32 - 1)
    seed_targets = _as_list(meta.get("seed_nodes")) or _as_list(seed_node)
    for nid in seed_targets:
        # `value` covers INTConstant nodes used as a shared seed source that
        # downstream RandomNoise/Math nodes derive from (e.g. ltx-2.3-long's
        # node 1527 → 1322 RandomNoise + 1715:1664 ComfyMathExpression →
        # 1715:1551 per-chunk RandomNoise). Without `value` the seed silently
        # never reaches the graph and ComfyUI's full-graph cache returns
        # byte-identical mp4s for repeat prompts.
        _set_field(nid, ["noise_seed", "seed", "value"], seed_val)

    # Long-form chunked-loop workflows (RuneXX single-pass loop). The duration is
    # driven by two integers: `LENGTH` seconds per chunk (snapped to LTX 8N+1
    # frames internally by the workflow's SimpleCalculatorKJ at node 1651) and
    # `LOOPS` (additional iterations of the easy-forLoop body). Empirically:
    #
    #   chunk_frames = ((round(LENGTH*fps - 1)/8) * 8) + 1
    #   total_frames = chunk_frames * (LOOPS + 2) - 1   # see _long_form_chunks
    #   total_seconds = total_frames / fps
    #
    # The (LOOPS + 2) accounts for chunk0 (one chunk outside the loop) plus the
    # easy forLoopStart body, which always executes once even when total=0,
    # plus `LOOPS` further iterations. The trailing -1 is the latent-stride
    # boundary subtraction at the VAE concat. See bug report 2026-05-08:
    # seconds=8 with workflow defaults (LENGTH=10, LOOPS=0) → 241*2-1=481
    # frames @ 24 fps = 20.04 s actual.
    #
    # We search the (LENGTH, LOOPS) integer grid for the closest total_seconds
    # to the request and write both. Single-shot (LOOPS=0) is the fast path
    # for any seconds that fits within a single chunk's max_length_sec.
    chunk_count_node = meta.get("chunk_count_node")
    chunk_length_node = meta.get("chunk_length_node")
    if chunk_count_node:
        max_length_sec = int(meta.get("chunk_max_seconds", 10))
        length_sec, loops_needed, total_seconds = _long_form_chunks(
            seconds_val, frame_rate, max_length_sec=max_length_sec
        )
        _set_field(chunk_count_node, ["value"], loops_needed)
        if chunk_length_node:
            _set_field(chunk_length_node, ["value"], length_sec)
        actual_seconds = total_seconds
        # Long-form chunks give granularity ~max_length_sec/(8N+1) seconds, so
        # the adjustment is usually within 0.05s for ≤chunk_max but can be up
        # to ~0.13s for multi-chunk requests. The header surfaces it always.
        if abs(actual_seconds - seconds_val) > 0.05:
            duration_adjustment_reason = "long_form_chunk_quantization"

    # I2V: when input_reference is supplied, resolve to bytes, upload to ComfyUI,
    # and wire it into the workflow via the keys exposed in _meta.
    #
    # Workflow contract (uniform across aliases — Option A: one workflow per alias
    # handles both T2V and I2V):
    #   * `image_node`: LoadImage node id whose `image` field gets set to the
    #     uploaded filename. Fields: ["image"].
    #   * `image_strength_node` (LTX): PrimitiveFloat node id whose `value`
    #     field gates conditioning strength. 0.0 = pure T2V passthrough,
    #     1.0 = first frame pinned to the image. Fields: ["value"].
    #   * `image_target_node` + `image_target_field` (Wan): conditional wiring
    #     mode. The proxy sets `inputs[<field>] = [image_node, 0]` to enable
    #     I2V; absent for T2V (lets the I2V latent node fall through its
    #     start_image=None T2V branch). No strength knob on Wan.
    #
    # T2V path: leave image_strength_node at 0.0 (LTX) / leave start_image
    # unwired (Wan). The placeholder image stays in the LoadImage field —
    # harmless because the LTX inplace node operates at strength=0 (mask=1.0,
    # full noise) and the Wan LoadImage is orphaned (executor skips it).
    if input_reference:
        if not meta.get("image_node") or not (
            meta.get("image_strength_node") or meta.get("image_target_node")
        ):
            return Response(
                content=json.dumps({
                    "error": {
                        "message": (
                            f"Workflow {workflow_name} does not support I2V "
                            "(missing _meta.image_node or image_strength_node/image_target_node)."
                        ),
                        "type": "invalid_request_error",
                    }
                }),
                status_code=400,
                media_type="application/json"
            )

        try:
            image_bytes, image_filename = await _resolve_input_reference(
                input_reference,
                auth_context,
            )
        except _InputReferenceError as e:
            return Response(
                content=json.dumps({
                    "error": {"message": str(e), "type": "invalid_request_error"}
                }),
                status_code=400,
                media_type="application/json"
            )

        # Upload to ComfyUI's input cache (same endpoint /upload/image used by image-edit).
        upload_headers = configured_downstream_headers(auth_context, "comfyui")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as upload_client:
                files = {"image": (image_filename, image_bytes, "image/png")}
                upload_resp = await upload_client.post(
                    f"{COMFY_HOST}/upload/image",
                    headers=upload_headers,
                    files=files,
                )
        except Exception as e:
            return Response(
                content=json.dumps({
                    "error": {"message": f"Image upload failed: {e}", "type": "api_error"}
                }),
                status_code=502,
                media_type="application/json"
            )
        if upload_resp.status_code != 200:
            return Response(
                content=json.dumps({
                    "error": {
                        "message": f"ComfyUI image upload failed: {upload_resp.text[:300]}",
                        "type": "api_error",
                    }
                }),
                status_code=502,
                media_type="application/json"
            )
        try:
            uploaded_filename = upload_resp.json().get("name")
        except Exception:
            uploaded_filename = None
        if not uploaded_filename:
            return Response(
                content=json.dumps({
                    "error": {"message": "ComfyUI did not return uploaded filename", "type": "api_error"}
                }),
                status_code=502,
                media_type="application/json"
            )
        print(f"[proxy] Video I2V uploaded image: {uploaded_filename}", flush=True)

        # Wire the uploaded filename into the LoadImage node.
        image_node = meta["image_node"]
        if not _set_field(image_node, ["image"], uploaded_filename):
            return Response(
                content=json.dumps({
                    "error": {
                        "message": f"Workflow {workflow_name} image_node {image_node} missing or has no `image` input",
                        "type": "api_error",
                    }
                }),
                status_code=500,
                media_type="application/json"
            )

        # Strength: clamp to [0,1]. Default 1.0 when an image is provided.
        try:
            strength_val = float(image_strength) if image_strength is not None else 1.0
        except (TypeError, ValueError):
            strength_val = 1.0
        strength_val = max(0.0, min(1.0, strength_val))

        strength_node = meta.get("image_strength_node")
        if strength_node:
            # LTX: set the PrimitiveFloat that feeds LTXVImgToVideoInplaceKJ.strength_*.
            _set_field(strength_node, ["value"], strength_val)

        target_node = meta.get("image_target_node")
        target_field = meta.get("image_target_field")
        if target_node and target_field:
            # Wan: connect start_image link from image_node into the I2V latent node.
            if target_node in workflow:
                workflow[target_node].setdefault("inputs", {})[target_field] = [image_node, 0]

    has_workflow_extensions = bool(body.get("inputs")) or bool(
        (body.get("extensions") or {}).get(WORKFLOW_EXTENSION_NAMESPACE)
        if isinstance(body.get("extensions"), Mapping)
        else body.get("extensions")
    )
    if has_workflow_extensions:
        try:
            manifest = _find_video_manifest(str(workflow_name), workflow_cfg)
            if manifest is None:
                return Response(
                    content=json.dumps({"error": {
                        "message": f"Workflow {workflow_name} does not have a video workflow manifest",
                        "type": "invalid_request_error",
                        "code": "workflow_manifest_missing",
                    }}),
                    status_code=400,
                    media_type="application/json",
                )
            extension_binding = bind_video_workflow_extensions(
                body,
                manifest,
                workflow,
            )
        except (ExtensionError, ManifestError) as e:
            return Response(
                content=json.dumps({"error": {
                    "message": str(e),
                    "type": "invalid_request_error",
                    "code": "invalid_workflow_extension",
                }}),
                status_code=400,
                media_type="application/json",
            )
        if not extension_binding.ok:
            rejected = dict(extension_binding.validation.rejected)
            rejected.update(extension_binding.unknown_top_level)
            first_name, first_reason = next(iter(rejected.items()))
            return Response(
                content=json.dumps({"error": {
                    "message": first_reason,
                    "type": "invalid_request_error",
                    "param": first_name,
                    "code": "invalid_workflow_extension",
                }}),
                status_code=400,
                media_type="application/json",
            )
        workflow = extension_binding.request

    headers = configured_downstream_headers(auth_context, "comfyui")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
            # client_id routes ws events for this submission to our listener.
            resp = await client.post(
                f"{COMFY_HOST}/prompt",
                headers=headers,
                json=_build_comfy_submit_payload(workflow, client_id=COMFY_CLIENT_ID),
            )
            if resp.status_code != 200:
                # Do NOT record _video_progress here: no prompt_id means there
                # is nothing to reconcile against, and a partial entry would
                # leak as a stale "running" job. Surface the error instead.
                return Response(
                    content=json.dumps({"error": {"message": f"ComfyUI error: {resp.text[:500]}", "type": "api_error"}}),
                    status_code=502,
                    media_type="application/json"
                )
            try:
                result = resp.json()
            except Exception:
                result = {}
            prompt_id = result.get("prompt_id") if isinstance(result, dict) else None
            if not prompt_id or not isinstance(prompt_id, str):
                # Same rationale: never seed _video_progress without a valid
                # prompt_id from ComfyUI, otherwise the entry can never be
                # reconciled and will show forever as a phantom job.
                return Response(
                    content=json.dumps({"error": {"message": "No prompt_id returned", "type": "api_error"}}),
                    status_code=502,
                    media_type="application/json"
                )

            # Seed progress state so /v1/videos/{id} can phase-classify nodes
            # by class type before any ws events arrive.
            try:
                async with _video_progress_lock:
                    _video_workflows[prompt_id] = workflow
                    _video_progress.setdefault(prompt_id, {
                        "status": "queued",
                        "percent": 0,
                        "step": 0,
                        "total": 0,
                        "node": None,
                        "node_class": None,
                        "phase": "encoding",
                        "error": None,
                        "updated_at": time.time(),
                    })
            except Exception:
                pass

            video_id = _video_id_from_prompt_id(prompt_id)
            print(
                f"[proxy] Video job submitted: workflow={workflow_name} id={video_id} "
                f"size={width}x{height} length={length} seed={seed_val}",
                flush=True,
            )

            # Firehose: capture submit metadata (alias, size, seconds, key) for
            # the live-video-job panel and the activity timeline.
            try:
                key_name_for_event = (
                    auth_context.identity.name
                    if auth_context and getattr(auth_context, "identity", None)
                    else None
                )
                firehose_metrics.capture_video_submit(
                    prompt_id,
                    alias=requested_model,
                    workflow=workflow_name,
                    size=f"{width}x{height}",
                    seconds=seconds_val,
                    key_name=key_name_for_event,
                    prompt_text=prompt,
                )
                firehose_metrics.record_event(
                    "video_submit",
                    f"{requested_model} · {width}x{height} · {seconds_val:g}s",
                    prompt_id=prompt_id,
                    alias=requested_model,
                    workflow=workflow_name,
                    size=f"{width}x{height}",
                    seconds=seconds_val,
                    key=key_name_for_event,
                )
            except Exception:
                pass

            now = int(time.time())
            response_headers = {}
            if duration_adjustment_reason is not None:
                response_headers["X-Duration-Adjusted"] = (
                    f"requested={seconds_val:g} actual={actual_seconds:.4f} "
                    f"reason={duration_adjustment_reason}"
                )
            # `object` and `seconds` are typed against the OpenAI Python SDK's
            # `Video` model (object: Literal["video"], seconds: Union[str, ...]).
            # Sending a string keeps the SDK's loose-construct parser happy
            # across SDK versions; the precise float lives in `actual_seconds`.
            seconds_str = str(int(seconds_val)) if float(seconds_val).is_integer() else str(seconds_val)
            payload = {
                "id": video_id,
                "object": "video",
                "model": workflow_name,
                "status": "queued",
                "progress": 0,
                "created_at": now,
                "expires_at": now + 86400,
                "size": f"{width}x{height}",
                "seconds": seconds_str,
                "actual_seconds": round(actual_seconds, 4),
            }
            return Response(
                content=json.dumps(payload),
                status_code=200,
                media_type="application/json",
                headers=response_headers,
            )

    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": {"message": f"ComfyUI unavailable at {COMFY_HOST}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"Video submit error: {str(e)}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )


async def proxy_comfy_video_status(video_id: str, auth_context: AuthContext) -> Response:
    """GET /v1/videos/{video_id} — poll status."""
    if not COMFY_ENABLED:
        return Response(
            content=json.dumps({"error": {"message": "Video generation not enabled", "type": "service_unavailable"}}),
            status_code=503,
            media_type="application/json"
        )
    prompt_id = _prompt_id_from_video_id(video_id)
    if not prompt_id:
        return Response(
            content=json.dumps({"error": {"message": "invalid video_id", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )

    headers = configured_downstream_headers(auth_context, "comfyui")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
            job = await _comfy_history(client, prompt_id, headers)
            if job is None:
                # Not in history yet — prefer live ws state, fall back to /queue.
                live = _video_progress_snapshot(prompt_id)
                if live and live.get("status") in ("queued", "running"):
                    payload = {
                        "id": video_id,
                        "object": "video.generation",
                        "status": "running" if live.get("percent", 0) > 0 or live.get("node") else live.get("status", "running"),
                        "progress": int(live.get("percent") or 0),
                        "step": int(live.get("step") or 0),
                        "total": int(live.get("total") or 0),
                        "node": live.get("node"),
                        "phase": live.get("phase"),
                    }
                    return Response(
                        content=json.dumps(payload),
                        status_code=200,
                        media_type="application/json",
                    )
                queue_status = await _comfy_queue_status(client, prompt_id, headers)
                if queue_status == "unknown":
                    return Response(
                        content=json.dumps({"error": {"message": "video_id not found", "type": "not_found"}}),
                        status_code=404,
                        media_type="application/json"
                    )
                return Response(
                    content=json.dumps({
                        "id": video_id,
                        "object": "video.generation",
                        "status": queue_status,
                        "progress": 25 if queue_status == "running" else 0,
                    }),
                    status_code=200,
                    media_type="application/json"
                )

            sora_status, progress = _job_status_from_history(job)
            # Enrich with last-known live progress if available (e.g. step/total
            # captured during the run). For terminal states, the history wins.
            live = _video_progress_snapshot(prompt_id) or {}
            payload = {
                "id": video_id,
                "object": "video.generation",
                "status": sora_status,
                "progress": progress,
            }
            if sora_status == "running" and live:
                payload["progress"] = int(live.get("percent") or progress)
                payload["step"] = int(live.get("step") or 0)
                payload["total"] = int(live.get("total") or 0)
                payload["node"] = live.get("node")
                payload["phase"] = live.get("phase")
            if sora_status == "failed":
                payload["error"] = _comfy_execution_error_message(job) or "ComfyUI execution_error"
            return Response(
                content=json.dumps(payload),
                status_code=200,
                media_type="application/json"
            )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": {"message": f"ComfyUI unavailable at {COMFY_HOST}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"Video status error: {str(e)}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )


async def proxy_comfy_video_content(video_id: str, auth_context: AuthContext) -> Response:
    """GET /v1/videos/{video_id}/content — return mp4 bytes."""
    if not COMFY_ENABLED:
        return Response(
            content=json.dumps({"error": {"message": "Video generation not enabled", "type": "service_unavailable"}}),
            status_code=503,
            media_type="application/json"
        )
    prompt_id = _prompt_id_from_video_id(video_id)
    if not prompt_id:
        return Response(
            content=json.dumps({"error": {"message": "invalid video_id", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )

    headers = configured_downstream_headers(auth_context, "comfyui")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10)) as client:
            job = await _comfy_history(client, prompt_id, headers)
            if job is None:
                return Response(
                    content=json.dumps({"error": {"message": "video_id not found", "type": "not_found"}}),
                    status_code=404,
                    media_type="application/json"
                )
            sora_status, _ = _job_status_from_history(job)
            if sora_status != "succeeded":
                return Response(
                    content=json.dumps({
                        "error": {
                            "message": f"video not ready (status={sora_status})",
                            "type": "invalid_request_error",
                        }
                    }),
                    status_code=409,
                    media_type="application/json"
                )

            file_info = _extract_video_output(job)
            if not file_info:
                return Response(
                    content=json.dumps({"error": {"message": "No video output found in job", "type": "api_error"}}),
                    status_code=500,
                    media_type="application/json"
                )

            params = {"filename": file_info["filename"], "type": file_info["type"]}
            if file_info["subfolder"]:
                params["subfolder"] = file_info["subfolder"]
            view_resp = await client.get(f"{COMFY_HOST}/view", headers=headers, params=params)
            if view_resp.status_code != 200:
                return Response(
                    content=json.dumps({"error": {"message": f"ComfyUI /view error: {view_resp.status_code}", "type": "api_error"}}),
                    status_code=502,
                    media_type="application/json"
                )

            ext = Path(file_info["filename"]).suffix.lower()
            content_type = {
                ".mp4": "video/mp4",
                ".webm": "video/webm",
                ".mov": "video/quicktime",
                ".mkv": "video/x-matroska",
            }.get(ext, "application/octet-stream")

            return Response(
                content=view_resp.content,
                status_code=200,
                media_type=content_type,
                headers={
                    "Content-Disposition": f'inline; filename="{file_info["filename"]}"',
                },
            )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": {"message": f"ComfyUI unavailable at {COMFY_HOST}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"Video content error: {str(e)}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )


# =============================================================================
# Model management
# =============================================================================

async def is_model_loaded() -> bool:
    """Check if a model is currently loaded."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{TABBY_HOST}/v1/model",
                headers={"x-api-key": API_KEY},
                timeout=HEALTH_CHECK_TIMEOUT
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("id") is not None
    except:
        pass
    return False


async def load_model() -> bool:
    """Load the default model with proper concurrency handling."""
    if not DEFAULT_MODEL:
        print(f"[proxy] No default model configured", flush=True)
        return False

    if not state.can_attempt_load():
        wait_time = LOAD_FAILURE_BACKOFF * state.load_failure_count
        print(f"[proxy] Load backoff active, retry in {wait_time}s", flush=True)
        return False

    if state.model_unloading and state.unload_event:
        print(f"[proxy] Waiting for unload to complete...", flush=True)
        try:
            await asyncio.wait_for(state.unload_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            print(f"[proxy] Unload wait timed out", flush=True)
            return False

    if state.model_loading and state.load_event:
        print(f"[proxy] Load in progress, waiting...", flush=True)
        try:
            await asyncio.wait_for(state.load_event.wait(), timeout=TABBY_LOAD_TIMEOUT)
            return await is_model_loaded()
        except asyncio.TimeoutError:
            print(f"[proxy] Load wait timed out", flush=True)
            return False

    async with state.load_lock:
        if await is_model_loaded():
            state.reset_load_failures()
            return True

        if state.model_loading:
            return False

        state.model_loading = True
        state.load_event = asyncio.Event()

    # Read config fresh to pick up any changes
    _, _, model_params = get_tabby_model_config()
    print(f"[proxy] Loading model: {DEFAULT_MODEL}", flush=True)
    success = False
    try:
        load_payload = {"model_name": DEFAULT_MODEL, **model_params}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TABBY_HOST}/v1/model/load",
                headers={"x-admin-key": ADMIN_KEY, "Content-Type": "application/json"},
                json=load_payload,
                timeout=TABBY_LOAD_TIMEOUT
            )
            response_text = ""
            async for chunk in resp.aiter_bytes():
                response_text += chunk.decode("utf-8", errors="replace")

            # TabbyAPI returns SSE with status updates, check for completion
            # Success: contains "finished" status, Failure: contains "error" status
            if resp.status_code == 200 and '"status":"finished"' in response_text:
                print(f"[proxy] Model loaded successfully", flush=True)
                state.update_activity()
                state.reset_load_failures()
                success = True
            elif '"status":"error"' in response_text or '"error":' in response_text:
                print(f"[proxy] Load failed: {response_text[-500:]}", flush=True)
                state.record_load_failure()
            else:
                # Verify by checking if model is actually loaded
                if await is_model_loaded():
                    print(f"[proxy] Model loaded (verified)", flush=True)
                    state.update_activity()
                    state.reset_load_failures()
                    success = True
                else:
                    print(f"[proxy] Load unclear, response: {response_text[-300:]}", flush=True)
                    state.record_load_failure()

    except Exception as e:
        print(f"[proxy] Load error: {e}", flush=True)
        state.record_load_failure()
    finally:
        state.model_loading = False
        if state.load_event:
            state.load_event.set()
        state.load_event = None

    return success


async def unload_model() -> bool:
    """Unload the current model."""
    async with state.load_lock:
        if state.model_loading:
            return False
        if state.model_unloading:
            return False
        state.model_unloading = True
        state.unload_event = asyncio.Event()

    success = False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TABBY_HOST}/v1/model/unload",
                headers={"x-admin-key": ADMIN_KEY},
                timeout=60
            )
            if resp.status_code == 200:
                print(f"[proxy] Model unloaded (idle {int(state.get_idle_seconds())}s)", flush=True)
                success = True
            else:
                print(f"[proxy] Unload failed: {resp.status_code}", flush=True)
    except Exception as e:
        print(f"[proxy] Unload error: {e}", flush=True)
    finally:
        state.model_unloading = False
        if state.unload_event:
            state.unload_event.set()
        state.unload_event = None

    return success


# =============================================================================
# Background tasks
# =============================================================================

async def maintenance_loop():
    """Background task for idle checking and health monitoring.

    Backends are activated lazily by request handlers (see ensure_target). This
    loop intentionally does NOT poll-and-revive backends — the Conflicts= rule
    between tabbyapi.service and llama-turbo.service means only one LLM backend
    is up at a time, so trying to "ensure tabby is healthy" while llama-turbo
    is the active LLM is wrong and produces log spam.

    Responsibilities kept:
      - Idle-timeout unload of an in-process loaded tabby model.

    The gateway never revives backends here: it does not own engine existence
    (crash-restart belongs to the operator's supervisor — systemd / docker /
    CommandDriver), and backend selection is request-driven via ensure_target.
    """
    while not state.shutdown_requested:
        await asyncio.sleep(CHECK_INTERVAL)

        if state.shutdown_requested:
            break

        # Check idle timeout (only meaningful when something is loaded on tabby)
        if state.model_loading or state.model_unloading:
            continue

        idle = state.get_idle_seconds()
        if TABBY_IDLE_TIMEOUT > 0 and idle >= TABBY_IDLE_TIMEOUT:
            # Probe tabby cheaply; if it's not even up, nothing to unload.
            if await check_tabby_health() and await is_model_loaded():
                print(f"[proxy] Idle timeout ({int(idle)}s), unloading...", flush=True)
                await unload_model()


# =============================================================================
# FastAPI app
# =============================================================================

_DRIFT_CACHE_PATH = Path(__file__).parent / "cache" / "drift_status.json"
_DRIFT_SCRIPT_PATH = Path(__file__).parent / "scripts" / "check_workflow_drift.py"
# Match status_endpoint._DRIFT_REPORT_PATH so the renderer reads the same file.
_DRIFT_REPORT_PATH = Path(__file__).parent / "cache" / "drift_status.json"


async def _run_workflow_drift_once() -> dict:
    """Run check_workflow_drift.py once with a hard timeout. Returns a status
    dict suitable for serialising into ``cache/drift_status.json``.

    Uses the proxy's own venv interpreter so playwright is reachable. If the
    script isn't present, ComfyUI is offline, or playwright errors out, the
    failure is recorded as ``{"status": "error", "reason": ...}`` and the
    proxy keeps running."""
    if not _DRIFT_SCRIPT_PATH.is_file():
        return {
            "status": "error",
            "reason": f"drift script missing: {_DRIFT_SCRIPT_PATH}",
            "checked_at": int(time.time()),
        }
    interpreter = sys.executable or "python3"
    try:
        proc = await asyncio.create_subprocess_exec(
            interpreter, str(_DRIFT_SCRIPT_PATH), "--verbose",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return {
                "status": "error",
                "reason": "drift script timed out after 120s",
                "checked_at": int(time.time()),
            }
        rc = proc.returncode
        out = (stdout or b"").decode("utf-8", errors="replace")
        err = (stderr or b"").decode("utf-8", errors="replace")
        return {
            "status": "ok" if rc == 0 else "drift" if rc == 1 else "error",
            "exit_code": rc,
            "checked_at": int(time.time()),
            "stdout_tail": out[-2000:],
            "stderr_tail": err[-2000:],
        }
    except FileNotFoundError as e:
        return {
            "status": "error",
            "reason": f"interpreter not found: {e}",
            "checked_at": int(time.time()),
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "reason": f"{type(e).__name__}: {str(e)[:200]}",
            "checked_at": int(time.time()),
        }


async def _workflow_drift_loop(stop_event: asyncio.Event) -> None:
    """Refresh the drift cache every 6h. First run after a 30s grace period
    so we don't compete with backend warm-up."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=30)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        result = await _run_workflow_drift_once()
        try:
            _DRIFT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _DRIFT_CACHE_PATH.write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            print(
                f"[proxy] drift cache refreshed: status={result.get('status')} "
                f"exit_code={result.get('exit_code')}",
                flush=True,
            )
        except Exception as e:  # noqa: BLE001
            print(f"[proxy] drift cache write failed: {e}", flush=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=6 * 3600)
            return
        except asyncio.TimeoutError:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifespan manager."""
    print(f"[proxy] Vivid Fish AI Gateway starting on port {PROXY_PORT}", flush=True)
    print(f"[proxy] TabbyAPI backend: {TABBY_HOST}", flush=True)
    print(f"[proxy] TTS backend: {TTS_HOST} (enabled: {TTS_ENABLED})", flush=True)
    print(f"[proxy] STT backend: {STT_HOST} (enabled: {STT_ENABLED})", flush=True)
    print(f"[proxy] ComfyUI backend: {COMFY_HOST} (enabled: {COMFY_ENABLED})", flush=True)
    print(f"[proxy] Proxy auth: {'enabled' if AUTH_ENABLED else 'disabled'}", flush=True)
    if not AUTH_ENABLED and PROXY_HOST not in ("127.0.0.1", "::1", "localhost"):
        print(
            f"[proxy] WARNING: auth is disabled and the gateway is bound to "
            f"{PROXY_HOST!r} (non-loopback). Any client that can reach this "
            f"host:port has unauthenticated access to all API endpoints and the "
            f"admin UI. Set auth.enabled=true or bind to 127.0.0.1 unless this "
            f"is intentional and the network is trusted.",
            flush=True,
        )
    print(f"[proxy] Model compatibility: {'enabled' if MODEL_COMPAT_ENABLED else 'disabled'}", flush=True)

    # Replay persisted firehose metrics so per-key counters and the recent-event
    # timeline survive proxy restarts. The window stays bounded at 24h; older
    # records are filtered out at replay time.
    try:
        replay_stats = firehose_metrics.replay_persisted()
        print(
            f"[proxy] firehose replay: requests={replay_stats.get('requests_replayed')} "
            f"events={replay_stats.get('events_replayed')} skipped={replay_stats.get('skipped')}",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] firehose replay failed: {e}", flush=True)

    # The gateway does not start engines. Tabby is brought up by the operator's
    # supervisor (systemd / docker / a `command` LifecycleDriver) and reached
    # lazily via ensure_target on the first request that needs it.
    print(f"[proxy] Idle timeout: {TABBY_IDLE_TIMEOUT}s", flush=True)
    print(f"[proxy] Default model: {DEFAULT_MODEL}", flush=True)

    maintenance_task = asyncio.create_task(maintenance_loop())

    # ComfyUI websocket listener for real-time video progress. Optional —
    # /v1/videos/{id} falls back to /history polling if this can't connect.
    comfy_ws_stop = asyncio.Event()
    comfy_ws_task = None
    video_reconcile_stop = asyncio.Event()
    video_reconcile_task = None
    if COMFY_ENABLED:
        comfy_ws_task = asyncio.create_task(_comfy_ws_listener(comfy_ws_stop))
        print(f"[proxy] ComfyUI ws listener started (client_id={COMFY_CLIENT_ID})", flush=True)

        # Startup reconciliation: safety net for any persisted/hydrated
        # _video_progress (currently in-memory only, so this is usually a
        # no-op, but the hook is wired for future persistence).
        if _video_progress:
            try:
                await _reconcile_video_progress()
                print(
                    f"[proxy] video progress startup reconcile complete "
                    f"({len(_video_progress)} entries remain)",
                    flush=True,
                )
            except Exception as e:  # noqa: BLE001
                print(f"[proxy] video progress startup reconcile failed: {e}", flush=True)

        video_reconcile_task = asyncio.create_task(
            _reconcile_video_progress_loop(video_reconcile_stop)
        )
        print(
            f"[proxy] video progress reconciliation started "
            f"(every {_VIDEO_RECONCILE_INTERVAL}s, retain terminal {_VIDEO_RECONCILE_RETAIN}s)",
            flush=True,
        )

    # Image cache eviction (Fix 2): periodically delete cache files past TTL.
    image_cache_stop = asyncio.Event()
    image_cache_task = asyncio.create_task(evict_image_cache_loop(image_cache_stop))
    print(
        f"[proxy] Image cache eviction started (dir={IMAGE_CACHE_DIR}, "
        f"ttl={IMAGE_CACHE_TTL}s, interval={IMAGE_CACHE_EVICT_INTERVAL}s)",
        flush=True,
    )

    # Workflow drift cache: run scripts/check_workflow_drift.py every 6h and
    # write the structured outcome to cache/drift_status.json. The script
    # depends on Playwright + a live ComfyUI; failures are recorded into the
    # cache (status=error, reason=...) instead of crashing.
    drift_stop = asyncio.Event()
    drift_task = asyncio.create_task(_workflow_drift_loop(drift_stop))
    print("[proxy] Workflow drift periodic task started (every 6h)", flush=True)

    # Metrics persistence: SQLite-backed time-series for the status page.
    # The sampler writes; readers (status_endpoint, firehose_render) open
    # short-lived connections via metrics_store.reader().
    metrics_task = None
    try:
        metrics_store.init_db()
        metrics_task = asyncio.create_task(metrics_sampler.sampler_loop())
        print(f"[proxy] Metrics sampler started (db={metrics_store.DB_PATH})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Metrics sampler failed to start: {e}", flush=True)

    # Responses API persistence: SQLite-backed store for stateful
    # /v1/responses conversations. Pruned hourly; rows expire after 7 days.
    responses_prune_stop = asyncio.Event()
    responses_prune_task = None
    try:
        responses_store.init_db()
        responses_prune_task = asyncio.create_task(
            _responses_prune_loop(responses_prune_stop)
        )
        print(f"[proxy] Responses store initialized (db={responses_store.DB_PATH})", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Responses store failed to start: {e}", flush=True)

    try:
        receipt_store.init_db()
        pruned = receipt_store.prune()
        print(
            f"[proxy] Receipt store initialized (db={receipt_store.DB_PATH}, pruned={pruned})",
            flush=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Receipt store failed to start: {e}", flush=True)

    try:
        media_jobs.init_db()
        pruned = media_jobs.prune()
        print(
            f"[proxy] Media job store initialized (db={media_jobs.DB_PATH}, "
            f"pruned_jobs={pruned.get('jobs', 0)} pruned_events={pruned.get('events', 0)})",
            flush=True,
        )
        if COMFY_ENABLED and IMAGE_MEDIA_JOB_RESUME_ON_START:
            resumed = _resume_pending_image_media_jobs()
            if resumed:
                print(
                    f"[proxy] Resumed {resumed} pending image media job(s)",
                    flush=True,
                )
            resumed_audio = _resume_pending_audio_media_jobs()
            if resumed_audio:
                print(
                    f"[proxy] Resumed {resumed_audio} pending audio media job(s)",
                    flush=True,
                )
            resumed_video = _resume_pending_video_media_jobs()
            if resumed_video:
                print(
                    f"[proxy] Resumed {resumed_video} pending video media job(s)",
                    flush=True,
                )
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Media job store failed to start: {e}", flush=True)

    try:
        ADMIN_STORE.init_db()
        print(f"[proxy] Admin store initialized (db={ADMIN_DB_PATH})", flush=True)
        app_key_count = active_app_credential_count()
        print(
            f"[proxy] Runtime App credentials: {app_key_count} active",
            flush=True,
        )
        if AUTH_ENABLED and not app_key_count:
            print(
                "[proxy] WARNING: No active App credentials in admin.db; "
                "proxied requests will be rejected",
                flush=True,
            )
        seeded_profiles = default_profiles.seed_default_profiles(
            path=ADMIN_STORE.path
        )
        if seeded_profiles:
            print(
                f"[proxy] Seeded {len(seeded_profiles)} default profile(s) "
                f"into admin.db: "
                f"{', '.join(row['name'] for row in seeded_profiles)}",
                flush=True,
            )
        seeded_defaults = _seed_user_defaults_from_bootstrap_config()
        if seeded_defaults["settings"] or seeded_defaults["mappings"]:
            print(
                "[proxy] Seeded admin.user_defaults into admin.db: "
                f"settings={seeded_defaults['settings']} "
                f"mappings={seeded_defaults['mappings']}",
                flush=True,
            )
        provisioned = _provision_existing_user_personal_apps()
        if provisioned:
            print(
                f"[proxy] Provisioned {provisioned} self-service personal app(s)",
                flush=True,
            )
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Admin store failed to start: {e}", flush=True)

    yield

    state.shutdown_requested = True
    for task in list(_image_media_job_tasks):
        task.cancel()
    if _image_media_job_tasks:
        await asyncio.gather(*list(_image_media_job_tasks), return_exceptions=True)
    maintenance_task.cancel()
    try:
        await maintenance_task
    except asyncio.CancelledError:
        pass
    if comfy_ws_task is not None:
        comfy_ws_stop.set()
        comfy_ws_task.cancel()
        try:
            await comfy_ws_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    if video_reconcile_task is not None:
        video_reconcile_stop.set()
        video_reconcile_task.cancel()
        try:
            await video_reconcile_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    image_cache_stop.set()
    image_cache_task.cancel()
    try:
        await image_cache_task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    drift_stop.set()
    drift_task.cancel()
    try:
        await drift_task
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    if metrics_task is not None:
        metrics_task.cancel()
        try:
            await metrics_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    try:
        metrics_sampler.close_writer()
    except Exception:
        pass
    if responses_prune_task is not None:
        responses_prune_stop.set()
        responses_prune_task.cancel()
        try:
            await responses_prune_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    # No engine processes to stop — the gateway never owned tabby's existence.


app = FastAPI(lifespan=lifespan)

# -----------------------------------------------------------------------------
# Static React UI (web/out) — strangler surfaces under /ui
# (docs/design/WEB_UI_STEERING.md). The export is a build artifact
# (`cd web && npm run build`), so a source checkout without it must still
# boot: a missing directory means "no mount", never a crash.
# -----------------------------------------------------------------------------

WEB_UI_DIR = Path(__file__).resolve().parent / "web" / "out"


def mount_web_ui(target_app: FastAPI, directory: Path = WEB_UI_DIR) -> bool:
    """Mount the static Next.js export at /ui when it exists.

    Returns True if mounted. ``check_dir=False`` keeps StaticFiles itself from
    raising if the directory disappears after boot (e.g. a rebuild in place);
    the existence guard here is what makes boot-without-build safe.
    """
    if not directory.is_dir():
        return False
    target_app.mount(
        "/ui",
        StaticFiles(directory=str(directory), html=True, check_dir=False),
        name="web_ui",
    )
    return True


WEB_UI_MOUNTED = mount_web_ui(app)

_PROTOCOL_ROUTE_REGISTRY = (
    service_registry.load_service_registry(path=ADMIN_DB_PATH)
    or service_registry.build_service_registry_seed(config)
)
CANONICAL_PROTOCOL_ROUTE_ALIASES: tuple[dict[str, object], ...] = (
    route_registry.canonical_protocol_route_aliases(
        registry=_PROTOCOL_ROUTE_REGISTRY,
        proxy_config=config,
    )
)

_MANIFEST_WORKFLOWS_DIR = Path(__file__).resolve().parent / "manifests" / "workflows"


def _load_gateway_workflow_registry() -> WorkflowRegistry:
    try:
        registry = WorkflowRegistry.from_directory(_MANIFEST_WORKFLOWS_DIR)
        admin_registry = service_registry.load_service_registry(path=ADMIN_DB_PATH)
        if admin_registry is not None:
            for record in service_registry.workflow_records_from_registry(admin_registry):
                if registry.get(record.id) is not None:
                    continue
                registry.add_manifest(record.manifest, source_path=record.source_path)
        if registry.errors:
            print(
                f"[proxy] Gateway workflow registry loaded with {len(registry.errors)} error(s)",
                flush=True,
            )
        return registry
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Gateway workflow registry unavailable: {e}", flush=True)
        return WorkflowRegistry()


GATEWAY_WORKFLOW_REGISTRY = _load_gateway_workflow_registry()


def _auth_context_from_scope(request: Request) -> AuthContext:
    auth_context = request.scope.get("auth_context")
    if isinstance(auth_context, AuthContext):
        return auth_context
    return NO_AUTH_CONTEXT


def _profile_for_auth_context(auth_context: AuthContext):
    if auth_context.identity is None:
        return DEFAULT_PROFILE
    return auth_context.identity.profile


def _profile_source_id(auth_context: AuthContext) -> str:
    return getattr(_profile_for_auth_context(auth_context), "id", "default-local")


def _profile_target_visibility(auth_context: AuthContext) -> str:
    profile = _profile_for_auth_context(auth_context)
    target_policy = getattr(profile, "target_policy", None)
    if target_policy is not None:
        return str(getattr(target_policy, "visibility", "catalog") or "catalog")
    model_access = getattr(profile, "model_access", None)
    legacy_mode = str(getattr(model_access, "mode", "catalog") or "catalog")
    return "available" if legacy_mode == "current_loaded" else "catalog"


def _profile_target_activation(auth_context: AuthContext) -> str:
    profile = _profile_for_auth_context(auth_context)
    target_policy = getattr(profile, "target_policy", None)
    if target_policy is not None:
        return str(getattr(target_policy, "activation", "may_activate") or "may_activate")
    model_access = getattr(profile, "model_access", None)
    legacy_mode = str(getattr(model_access, "mode", "catalog") or "catalog")
    return "existing_only" if legacy_mode == "current_loaded" else "may_activate"


def _profile_model_access_mode(auth_context: AuthContext) -> str:
    if (
        _profile_target_visibility(auth_context) == "available"
        and _profile_target_activation(auth_context) == "existing_only"
    ):
        return "current_loaded"
    return "catalog"


def _profile_model_allowlist(auth_context: AuthContext):
    return getattr(_profile_for_auth_context(auth_context), "models", None)


def _comfy_target_available() -> bool:
    if not COMFY_ENABLED:
        return False
    override = _operator_service_availability_overrides().get("comfyui", {})
    state = str((override or {}).get("state") or "available").strip().lower()
    return state not in {"disabled", "maintenance", "draining"}


def _target_activation_policy_decision(
    auth_context: AuthContext,
    *,
    requested_target: str,
    available_ids: Iterable[str],
    available_label: str,
    empty_label: str,
) -> PolicyDecision | None:
    if _profile_target_activation(auth_context) != "existing_only":
        return None
    requested = str(requested_target or "").strip()
    if not requested:
        return None

    available = {str(item).strip() for item in available_ids if str(item).strip()}
    normalized_available = {normalize_model_id(item) for item in available}
    if normalize_model_id(requested) in normalized_available:
        return None

    profile = _profile_for_auth_context(auth_context)
    visible_ids = sorted(item for item in available if not item.startswith("/"))
    if visible_ids:
        reason = (
            f"target {requested!r} is not one of the {available_label}: "
            + ", ".join(visible_ids[:20])
        )
    else:
        reason = (
            "existing-only target policy is enabled, but "
            f"{empty_label}"
        )
    return PolicyDecision(
        decision=DENIED,
        sources=(f"profile:{profile.id}", "policy:target-activation"),
        reasons=(reason,),
        visibility_level=profile.status_visibility.level,
    )


def _workflow_target_ids(
    *,
    requested: str | None = None,
    resolved: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    extra_ids: Iterable[str] = (),
) -> set[str]:
    ids = {str(item).strip() for item in extra_ids if str(item).strip()}
    for item in (requested, resolved):
        if item:
            ids.add(str(item).strip())
    if isinstance(manifest, Mapping):
        manifest_id = manifest.get("id")
        if isinstance(manifest_id, str) and manifest_id:
            ids.add(manifest_id)
        model_id = _gateway_image_model_id(manifest)
        if model_id:
            ids.add(model_id)
    return ids


def _workflow_target_policy_decision(
    auth_context: AuthContext,
    *,
    requested: str | None,
    resolved: str | None = None,
    manifest: Mapping[str, Any] | None = None,
    extra_ids: Iterable[str] = (),
) -> PolicyDecision | None:
    available_ids = (
        _workflow_target_ids(
            requested=requested,
            resolved=resolved,
            manifest=manifest,
            extra_ids=extra_ids,
        )
        if _comfy_target_available()
        else set()
    )
    return _target_activation_policy_decision(
        auth_context,
        requested_target=str(requested or resolved or ""),
        available_ids=available_ids,
        available_label="available workflow ids",
        empty_label="no available workflow was reported",
    )


def _same_model_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
        os.path.normpath(right)
    )


def _role_catalog_ids_for_loaded_path(
    *, backend: str, loaded_path: str | None, loaded_model_id: str | None
) -> set[str]:
    ids: set[str] = set()
    loaded_norm = normalize_model_id(loaded_model_id or "")
    loaded_base = normalize_model_id(os.path.basename(loaded_path or ""))
    loaded_path_norm = os.path.normcase(os.path.normpath(loaded_path or ""))

    def matches_path(path: Any) -> bool:
        raw = str(path or "").strip()
        if not raw:
            return False
        raw_norm = os.path.normcase(os.path.normpath(raw))
        return raw_norm == loaded_path_norm or normalize_model_id(os.path.basename(raw)) in {
            loaded_norm,
            loaded_base,
        }

    if isinstance(ROLES, dict):
        for role_name, role_def in ROLES.items():
            if not isinstance(role_def, Mapping) or role_def.get("backend") != backend:
                continue
            role_model = role_def.get("model")
            if role_model:
                if matches_path(role_model):
                    ids.add(str(role_name))
            elif backend == "tabby" and loaded_model_id:
                # Tabby roles often name a backend without pinning a specific
                # model path. When that backend is hot, the role is a valid
                # handle for the current loaded model.
                ids.add(str(role_name))

    for model_id, spec in MODELS_CATALOG.items():
        if not isinstance(spec, Mapping) or spec.get("backend") != backend:
            continue
        if matches_path(spec.get("path")):
            ids.add(str(model_id))

    return ids


async def _current_loaded_llm_model_ids(active_backend: str | None = None) -> set[str]:
    """Return public ids that resolve to the currently loaded LLM backend."""
    try:
        active = active_backend or await get_active_backend()
    except Exception:
        active = None

    ids: set[str] = set()
    if active == "tabby":
        loaded = await get_tabby_loaded_model_id()
        if loaded:
            ids.update(USE_LOADED_MODEL_ALIASES)
            ids.add(loaded)
            ids.update(
                _role_catalog_ids_for_loaded_path(
                    backend="tabby",
                    loaded_path=None,
                    loaded_model_id=loaded,
                )
            )
        return {item for item in ids if item}

    loaded_path = currently_loaded_kobold_model()
    if loaded_path:
        ids.update(USE_LOADED_MODEL_ALIASES)
        ids.add(loaded_path)
        basename = os.path.basename(loaded_path)
        if basename:
            ids.add(basename)
        ids.update(
            _role_catalog_ids_for_loaded_path(
                backend="kobold",
                loaded_path=loaded_path,
                loaded_model_id=basename,
            )
        )
    return {item for item in ids if item}


async def _current_loaded_model_policy_decision(
    auth_context: AuthContext,
    *,
    requested_model: str,
) -> PolicyDecision | None:
    loaded_ids = await _current_loaded_llm_model_ids()
    return _target_activation_policy_decision(
        auth_context,
        requested_target=requested_model,
        available_ids=loaded_ids,
        available_label="currently loaded LLM ids",
        empty_label="no loaded LLM was reported",
    )


async def _filter_model_list_for_profile(
    data: list[Any],
    auth_context: AuthContext,
) -> tuple[list[Any], dict[str, Any]]:
    metadata: dict[str, Any] = {}
    filtered = [item for item in data if isinstance(item, dict)]

    allowlist = _profile_model_allowlist(auth_context)
    if allowlist is not None:
        allowed_ids = {normalize_model_id(item) for item in allowlist}
        filtered = [
            item
            for item in filtered
            if normalize_model_id(str(item.get("id") or "")) in allowed_ids
        ]
        metadata["vivid_fish_profile_model_allowlist"] = sorted(allowlist)

    if _profile_target_visibility(auth_context) == "available":
        loaded_ids = await _current_loaded_llm_model_ids()
        loaded_norm = {normalize_model_id(item) for item in loaded_ids}
        comfy_available = _comfy_target_available()
        filtered = [
            item
            for item in filtered
            if (
                normalize_model_id(str(item.get("id") or "")) in loaded_norm
                or (
                    bool(item.get("vivid_fish_workflow"))
                    and str(item.get("vivid_fish_backend") or "") in {"comfyui", "gateway"}
                    and comfy_available
                )
                or (
                    str(item.get("vivid_fish_backend") or "") == "embeddings"
                    and EMBEDDINGS_ENABLED
                )
            )
        ]
        metadata["vivid_fish_target_visibility"] = "available"
        metadata["vivid_fish_target_activation"] = _profile_target_activation(auth_context)
        metadata["vivid_fish_model_access"] = "current_loaded"
        metadata["vivid_fish_current_model_ids"] = sorted(
            item for item in loaded_ids if item and not item.startswith("/")
        )

    return filtered, metadata


def _protocol_for_intent(intent: str) -> str:
    if intent.startswith("image."):
        return "openai.images"
    if intent.startswith("video."):
        return "openai.video"
    if intent.startswith(("audio.", "speech.", "sound.", "music.")):
        return "openai.audio"
    if intent.startswith("responses."):
        return "openai.responses"
    return "openai.chat"


def _capability_for_profile(
    capability: Capability,
    auth_context: AuthContext,
    *,
    protocol: str | None = None,
) -> Capability:
    profile = _profile_for_auth_context(auth_context)
    return capability_with_policy_view(
        capability,
        profile,
        request=make_policy_request(
            protocol=protocol or _protocol_for_intent(capability.intent),
            intent=capability.intent,
            workflow=capability.workflow,
            model=capability.model,
            backend=capability.backend,
            capability_exposure=capability.exposure,
            capability_trust_tier=capability.trust_tier,
            capability_resource_tier=capability.resource_tier,
            capability_tags=capability.tags,
        ),
    )


def _gateway_capabilities_for_auth_context(auth_context: AuthContext) -> list[Capability]:
    try:
        _live_backends: Mapping[str, Any] | None = service_registry.runtime_backends(
            ADMIN_STORE.get_service_registry()
        )
    except Exception:  # noqa: BLE001
        _live_backends = None
    capabilities = list(build_local_capabilities(
        config,
        enabled_flags={
            "llm": bool(kobold_cfg) or bool(tabby_cfg),
            "embeddings": EMBEDDINGS_ENABLED and bool(embeddings_cfg),
            "tts": TTS_ENABLED,
            "stt": STT_ENABLED,
            "images": COMFY_ENABLED,
            "videos": COMFY_ENABLED,
            "audio": COMFY_ENABLED,
        },
        backends=_live_backends,
    ))

    for record in GATEWAY_WORKFLOW_REGISTRY.records():
        for backend in record.supported_backends:
            try:
                capabilities.append(capability_from_workflow_record(
                    record,
                    backend=backend,
                    execution="job",
                    resources=ResourceEstimate(gpu_required=backend == "comfyui"),
                ))
            except Exception:
                continue

    try:
        admin_registry = service_registry.load_service_registry(path=ADMIN_DB_PATH)
        if admin_registry is not None:
            capabilities.extend(service_registry.capabilities_from_registry(admin_registry))
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Registry capabilities unavailable: {e}", flush=True)

    return [
        _capability_for_profile(capability, auth_context)
        for capability in capabilities
    ]


def _gateway_capabilities_provider(request: Request) -> list[Capability]:
    return _gateway_capabilities_for_auth_context(_auth_context_from_scope(request))


def _user_access_catalog_for_auth_context(auth_context: AuthContext) -> dict[str, Any]:
    capabilities = _gateway_capabilities_for_auth_context(auth_context)
    allowed = [
        capability
        for capability in capabilities
        if capability.policy_available is not False
    ]
    catalog = summarize_capabilities(allowed, audience="client")
    model_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cap in catalog.get("capabilities", []):
        if not isinstance(cap, Mapping):
            continue
        for field, object_type in (("model", "model"), ("workflow", "workflow")):
            value = cap.get(field)
            if not value:
                continue
            key = str(value)
            if key in seen:
                continue
            seen.add(key)
            model_rows.append({
                "id": key,
                "object": object_type,
                "intent": cap.get("intent"),
                "available": cap.get("available"),
            })
    catalog["models"] = {"object": "list", "data": model_rows}
    catalog["allowed_count"] = len(allowed)
    catalog["denied_count"] = max(0, len(capabilities) - len(allowed))
    return catalog


def _gateway_audience_resolver(request: Request) -> str:
    auth_context = _auth_context_from_scope(request)
    return discovery_audience_for_profile(_profile_for_auth_context(auth_context))


async def _gateway_media_job_submitter(
    *,
    request: Request,
    payload: Mapping[str, Any],
    audience: str,
) -> Response | None:
    return await _dispatch_gateway_media_job_submission(
        request=request,
        payload=payload,
        audience=audience,
    )


_HTML_CONTENT_TYPES = ("text/html",)
_BROWSER_UI_PREFIXES = ("/admin", "/setup", "/login", "/me", "/status-page")

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Inline styles/scripts are used in the admin UI, so unsafe-inline is
    # required. eval is not used. This is a hardening baseline, not a full
    # CSP lockdown; see docs/REVERSE_PROXY.md for tightening guidance.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'"
    ),
}


@app.middleware("http")
async def _security_headers_middleware(request: Request, call_next):
    """Inject browser security headers on HTML/admin/portal responses."""
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    path = request.url.path
    is_html = any(ct.startswith(t) for t in _HTML_CONTENT_TYPES)
    is_ui_path = any(path == p or path.startswith(p + "/") or path.startswith(p + "?")
                     for p in _BROWSER_UI_PREFIXES)
    if is_html or is_ui_path:
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
    return response


@app.middleware("http")
async def _gateway_auth_middleware(request: Request, call_next):
    """Gate gateway-native discovery/receipt APIs behind the proxy key layer."""
    if request.url.path.startswith("/gateway/v1"):
        auth_context = authenticate_request(request, request.url.path)
        if isinstance(auth_context, Response):
            return auth_context
        request.scope["auth_context"] = auth_context
        request.scope["gateway_audience"] = _gateway_audience_resolver(request)
    return await call_next(request)


def _gateway_resource_lister(*, audience: str) -> list:
    try:
        reg = ADMIN_STORE.get_service_registry()
    except Exception:  # noqa: BLE001
        return []
    return service_registry.list_resource_summaries(reg, audience=audience)


app.include_router(
    create_gateway_router(
        GATEWAY_WORKFLOW_REGISTRY,
        audience_resolver=_gateway_audience_resolver,
        capabilities_provider=_gateway_capabilities_provider,
        receipt_store_path=receipt_store.DB_PATH,
        resource_lister=_gateway_resource_lister,
        asset_url_ttl_s=IMAGE_SIGNED_URL_TTL,
        media_job_store_path=media_jobs.DB_PATH,
        media_job_submitter=_gateway_media_job_submitter,
    )
)


def _effective_admin_oidc_config():
    """Return the OIDC config, preferring DB settings over YAML bootstrap.

    DB-stored fields layer on top of the bootstrap config. The session
    signing key always comes from the browser-session key resolver so that
    the admin cookie remains valid across DB toggles (changing the signing
    key on the fly would silently log everyone out).
    """
    try:
        db_value = ADMIN_STORE.get_oidc_config_setting_raw()
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] admin OIDC db read failed: {exc}", flush=True)
        db_value = None
    if not db_value:
        return ADMIN_OIDC_CONFIG
    merged = dict(_bootstrap_admin_oidc_cfg())
    for key, value in db_value.items():
        if value in (None, "", [], ()):
            continue
        merged[key] = value
    if not merged.get("signing_key") and not merged.get("signing_key_env"):
        if ADMIN_SESSION_SIGNING_KEY:
            merged["signing_key"] = ADMIN_SESSION_SIGNING_KEY
    try:
        cfg = load_oidc_config(merged, env=os.environ)
    except OIDCConfigError as exc:
        print(f"[proxy] WARNING: admin OIDC db config invalid: {exc}", flush=True)
        return ADMIN_OIDC_CONFIG
    # Preserve the established signing key/cookie so admin sessions survive
    # DB-driven provider changes.
    return replace(
        cfg,
        signing_key=ADMIN_SESSION_SIGNING_KEY or cfg.signing_key,
        cookie=ADMIN_OIDC_CONFIG.cookie,
    )


async def _admin_oidc_config_with_discovery():
    cfg = _effective_admin_oidc_config()
    if not cfg.enabled:
        return cfg
    if cfg.authorization_endpoint and cfg.token_endpoint and cfg.jwks_uri:
        return cfg
    discovery_url = f"{cfg.issuer.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(discovery_url)
        response.raise_for_status()
        metadata = response.json()
    return replace(
        cfg,
        authorization_endpoint=cfg.authorization_endpoint
        or str(metadata.get("authorization_endpoint", "")),
        token_endpoint=cfg.token_endpoint or str(metadata.get("token_endpoint", "")),
        jwks_uri=cfg.jwks_uri or str(metadata.get("jwks_uri", "")),
    )


async def _oidc_exchange_code(code: str, *, config, pkce: PKCEPair) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            config.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code_verifier": pkce.verifier,
            },
            headers={"accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("OIDC token endpoint returned non-object JSON")
    return payload


async def _oidc_verify_id_token(id_token: str, *, config) -> Mapping[str, Any]:
    jwks = ADMIN_OIDC_JWKS_CACHE.get(config.jwks_uri)
    if jwks is None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(config.jwks_uri)
            response.raise_for_status()
            jwks = response.json()
        ADMIN_OIDC_JWKS_CACHE[config.jwks_uri] = jwks
    from authlib.jose import JsonWebKey, jwt

    key_set = JsonWebKey.import_key_set(jwks)
    claims = jwt.decode(id_token, key_set)
    claims.validate(leeway=config.clock_skew_seconds)
    return dict(claims)


def _existing_admin_roles_by_email() -> dict[str, str]:
    roles_by_email: dict[str, str] = {}
    for user in admin_store.list_users(path=ADMIN_STORE.path):
        email = str(user.get("email") or "").strip().lower()
        if not email:
            continue
        roles = admin_store.list_admin_roles(
            subject_kind="user",
            subject_id=user["id"],
            path=ADMIN_STORE.path,
        )
        role_names = {str(role["role"]) for role in roles}
        if "owner" in role_names:
            roles_by_email[email] = "owner"
        elif "admin" in role_names:
            roles_by_email[email] = "admin"
    service_roles = admin_store.list_admin_roles(
        subject_kind="service_account",
        path=ADMIN_STORE.path,
    )
    service_role_names = {str(role["role"]) for role in service_roles}
    if "owner" in service_role_names:
        roles_by_email["__service_account_owner__"] = "owner"
    elif "admin" in service_role_names:
        roles_by_email["__service_account_admin__"] = "admin"
    return roles_by_email


def _safe_admin_return_to(value: str | None, *, default: str = "/admin") -> str:
    if not value:
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    if value in {"/", "/me"}:
        return "/"
    if value in {"/status", "/setup"}:
        return value
    if value.startswith("/admin"):
        return value
    return default


def _login_url(*, return_to: str) -> str:
    safe_return_to = _safe_admin_return_to(return_to, default="/")
    return f"/login?return_to={quote(safe_return_to, safe='')}"


def _browser_admin_session_signing_key() -> bytes | None:
    return ADMIN_AUTH.signing_key or ADMIN_SESSION_SIGNING_KEY


def _browser_admin_cookie_config():
    return ADMIN_AUTH.cookie or ADMIN_OIDC_CONFIG.cookie


def _oidc_session_from_request(request: Request):
    signing_key = _browser_admin_session_signing_key()
    if not signing_key:
        return None
    cookie = _browser_admin_cookie_config()
    raw = request.cookies.get(cookie.name)
    if not raw:
        return None
    try:
        return decode_session_cookie(
            raw,
            signing_key=signing_key,
        )
    except SessionError:
        return None


def _csrf_token_for_oidc_session(session) -> str:
    signing_key = ADMIN_AUTH.signing_key or ADMIN_SESSION_SIGNING_KEY
    if not signing_key:
        return ""
    try:
        return csrf_token_for_session(
            session,
            signing_key=signing_key,
        )
    except SessionError:
        return ""


def _require_user_session(request: Request):
    session = _oidc_session_from_request(request)
    if session is None:
        raise UserPortalError(
            "login_required",
            "Sign in before managing account keys",
            status_code=401,
        )
    return session


def _user_action_response_error(exc: UserPortalError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def _require_user_action_header(request: Request, session) -> JSONResponse | None:
    if request.headers.get("x-ai-gateway-user-action") == "1":
        expected = _csrf_token_for_oidc_session(session)
        supplied = request.headers.get("x-ai-gateway-csrf") or ""
        if expected and hmac.compare_digest(supplied, expected):
            return None
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "code": "user_csrf_required",
                    "message": "valid x-ai-gateway-csrf header required",
                }
            },
        )
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "code": "user_action_header_required",
                "message": "x-ai-gateway-user-action header required",
            }
        },
    )


def _require_user_self_service() -> None:
    if not _user_self_service_enabled():
        raise UserPortalError(
            "self_service_disabled",
            "App self-service is disabled for this gateway.",
            status_code=403,
        )


app.include_router(
    create_admin_router(
        ADMIN_STORE,
        auth=ADMIN_AUTH,
        profile_resolver=ADMIN_STORE.profile_for_id,
        service_probe=probe_backend,
    )
)


@app.get("/me/login", include_in_schema=False)
async def user_portal_login():
    return RedirectResponse(_login_url(return_to="/"), status_code=302)


@app.get("/me/logout", include_in_schema=False)
async def user_portal_logout():
    response = RedirectResponse("/", status_code=302)
    cookie = _browser_admin_cookie_config()
    response.delete_cookie(
        cookie.name,
        path=cookie.path,
        domain=cookie.domain,
    )
    return response


@app.get("/logout", include_in_schema=False)
async def browser_logout_alias():
    return await user_portal_logout()


def _render_user_portal_response(request: Request, *, login_return_to: str) -> Response:
    session = _oidc_session_from_request(request)
    account = build_user_portal_snapshot(
        session,
        default_profile_id=(
            _default_profile_id_for_session(session) if session else None
        ),
        path=ADMIN_STORE.path,
    )
    brand_label, brand_wordmark = _brand_settings()
    body = render_user_portal(
        title="AI Gateway",
        brand=brand_label,
        wordmark=brand_wordmark,
        public_base_url=_public_base_url(request) or "https://ai.vivid.fish",
        account=account,
        oidc_enabled=_effective_admin_oidc_config().enabled,
        owner_exists=_owner_or_admin_exists(),
        login_url=_login_url(return_to=login_return_to),
        csrf_token=_csrf_token_for_oidc_session(session) if session else "",
    )
    return Response(
        content=body,
        status_code=200,
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-store"},
    )


@app.get("/me", include_in_schema=False)
async def user_portal(request: Request):
    return RedirectResponse("/", status_code=302)


@app.get("/me/api/session", include_in_schema=False)
async def user_portal_session(request: Request):
    session = _oidc_session_from_request(request)
    return JSONResponse(
        status_code=200,
        content=build_user_portal_snapshot(
            session,
            default_profile_id=(
                _default_profile_id_for_session(session) if session else None
            ),
            path=ADMIN_STORE.path,
        ),
        headers={"cache-control": "no-store"},
    )


@app.get("/me/api/activity", include_in_schema=False)
async def user_portal_activity(request: Request):
    """The signed-in user's own recent API-call receipts (owner-scoped).

    Read-only, so no user-action/CSRF header is required — but a session IS
    required, and the feed is scoped server-side to the user's owned Apps. An
    unauthenticated request returns an empty (unauthenticated) snapshot rather
    than leaking anyone's activity.
    """
    session = _oidc_session_from_request(request)
    try:
        limit = int(request.query_params.get("limit") or 0) or None
    except (TypeError, ValueError):
        limit = None
    snapshot = build_user_activity_snapshot(
        session,
        limit=limit if limit else 20,
        path=ADMIN_STORE.path,
        receipts_path=receipt_store.DB_PATH,
    )
    return JSONResponse(
        status_code=200,
        content=snapshot,
        headers={"cache-control": "no-store"},
    )


@app.get("/me/api/receipts/{request_id}", include_in_schema=False)
async def user_portal_receipt_detail(request_id: str, request: Request):
    """One receipt, IF it belongs to the signed-in user (else 404).

    Backs the addressable receipt-detail view (``/r/{id}``): a shareable
    "why did this call fail" URL. Ownership is verified server-side; a receipt
    the caller doesn't own is reported as not found, never 403.
    """
    session = _oidc_session_from_request(request)
    try:
        receipt = get_user_receipt(
            session,
            request_id,
            path=ADMIN_STORE.path,
            receipts_path=receipt_store.DB_PATH,
        )
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    return JSONResponse(
        status_code=200,
        content=receipt,
        headers={"cache-control": "no-store"},
    )


@app.post("/me/api/apps", include_in_schema=False)
async def user_portal_create_app(request: Request):
    try:
        session = _require_user_session(request)
        _require_user_self_service()
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    header_error = _require_user_action_header(request, session)
    if header_error is not None:
        return header_error
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name") if isinstance(body, Mapping) else ""
    try:
        payload = create_user_app(
            session,
            name=str(name or ""),
            default_profile_id=_default_profile_id_for_session(session),
            path=ADMIN_STORE.path,
        )
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    return JSONResponse(
        status_code=201,
        content=payload,
        headers={"cache-control": "no-store"},
    )


@app.delete("/me/api/apps/{app_id}", include_in_schema=False)
async def user_portal_delete_app(app_id: str, request: Request):
    try:
        session = _require_user_session(request)
        _require_user_self_service()
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    header_error = _require_user_action_header(request, session)
    if header_error is not None:
        return header_error
    try:
        payload = delete_user_app(
            session,
            app_id,
            path=ADMIN_STORE.path,
        )
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    return JSONResponse(
        status_code=200,
        content=payload,
        headers={"cache-control": "no-store"},
    )


@app.post("/me/api/apps/{app_id}/keys", include_in_schema=False)
async def user_portal_mint_key(app_id: str, request: Request):
    try:
        session = _require_user_session(request)
        _require_user_self_service()
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    header_error = _require_user_action_header(request, session)
    if header_error is not None:
        return header_error
    try:
        body = await request.json()
    except Exception:
        body = {}
    label = body.get("label") if isinstance(body, Mapping) else None
    try:
        credential = mint_user_app_key(
            session,
            app_id,
            label=str(label).strip() if label else None,
            default_profile_id=_default_profile_id_for_session(session),
            path=ADMIN_STORE.path,
        )
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    return JSONResponse(
        status_code=201,
        content={"credential": credential},
        headers={"cache-control": "no-store"},
    )


@app.post("/me/api/access-request", include_in_schema=False)
async def user_portal_request_access(request: Request):
    try:
        session = _require_user_session(request)
        _require_user_self_service()
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    header_error = _require_user_action_header(request, session)
    if header_error is not None:
        return header_error
    try:
        payload = request_user_access(
            session,
            default_profile_id=_default_profile_id_for_session(session),
            path=ADMIN_STORE.path,
        )
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    return JSONResponse(
        status_code=201,
        content=payload,
        headers={"cache-control": "no-store"},
    )


@app.get("/me/api/credentials/{credential_id}/access", include_in_schema=False)
async def user_portal_credential_access(credential_id: str, request: Request):
    try:
        session = _require_user_session(request)
        credential = get_user_app_credential(
            session,
            credential_id,
            path=ADMIN_STORE.path,
        )
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    identity = runtime_identity_for_app_credential_hash(
        str(credential.get("hash") or ""),
        inherited_profile_id_for_app=_inherited_profile_id_for_app,
        path=ADMIN_STORE.path,
    )
    if identity is None:
        return _user_action_response_error(
            UserPortalError(
                "credential_unavailable",
                "API key access is unavailable for this account",
                status_code=404,
            )
        )
    catalog = _user_access_catalog_for_auth_context(AuthContext(identity=identity))
    return JSONResponse(
        status_code=200,
        content={
            "credential": {
                "id": credential.get("id"),
                "prefix": credential.get("prefix"),
                "display_name": credential.get("display_name"),
                "app_id": credential.get("app_id") or credential.get("subject_id"),
            },
            **catalog,
        },
        headers={"cache-control": "no-store"},
    )


@app.delete("/me/api/credentials/{credential_id}", include_in_schema=False)
@app.post("/me/api/credentials/{credential_id}/revoke", include_in_schema=False)
async def user_portal_revoke_key(credential_id: str, request: Request):
    try:
        session = _require_user_session(request)
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    header_error = _require_user_action_header(request, session)
    if header_error is not None:
        return header_error
    try:
        credential = revoke_user_app_key(
            session,
            credential_id,
            path=ADMIN_STORE.path,
        )
    except UserPortalError as exc:
        return _user_action_response_error(exc)
    return JSONResponse(
        status_code=200,
        content={"credential": credential},
        headers={"cache-control": "no-store"},
    )


def _admin_api_surfaces() -> list[dict[str, Any]]:
    return admin_api_surfaces(registry=ADMIN_STORE.get_service_registry())


def _status_by_service_name(status_payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for row in status_payload.get("backends") or []:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "")
        if name:
            out[name] = row
    return out


def _admin_service_runtime_summary(
    key: str,
    *,
    status_payload: Mapping[str, Any],
) -> str:
    models = status_payload.get("models") if isinstance(status_payload, Mapping) else {}
    if not isinstance(models, Mapping):
        models = {}
    if key in {"kobold", "tabby"}:
        loaded = models.get("loaded_llm_name")
        ctx = models.get("ctx_size")
        if loaded and ctx:
            return f"loaded LLM: {loaded}; context {ctx}"
        if loaded:
            return f"loaded LLM: {loaded}"
        return "loaded LLM: unknown"
    if key == "tts":
        tts = models.get("tts")
        if isinstance(tts, Mapping):
            label = tts.get("label") or tts.get("unit")
            if label:
                return f"active TTS: {label}"
        return "speech synthesis backend"
    if key == "stt":
        return "speech transcription backend"
    if key == "comfyui":
        image_workflows = models.get("image_workflows")
        video_aliases = models.get("video_aliases")
        image_count = len(image_workflows) if isinstance(image_workflows, list) else 0
        video_count = len(video_aliases) if isinstance(video_aliases, list) else 0
        queue = status_payload.get("comfy_queue")
        queue_text = ""
        if isinstance(queue, Mapping):
            queue_text = (
                f"; queue {int(queue.get('running') or 0)} running/"
                f"{int(queue.get('pending') or 0)} pending"
            )
        return f"{image_count} image workflows; {video_count} video aliases{queue_text}"
    return ""


def _admin_services(status_payload: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    payload: Mapping[str, Any] = status_payload or {}
    status_by_name = _status_by_service_name(payload)
    service_overrides = _operator_service_availability_overrides()
    rows: list[dict[str, Any]] = []
    # The DB service registry is the live source of truth for backends; iterate
    # it as the primary source. Services not yet in the running config["backends"]
    # (frozen at boot) are shown as "pending" so operators can see/edit them
    # immediately after adding via the dashboard. Fall back to config["backends"]
    # if the registry is unavailable to keep the dashboard non-fatal.
    try:
        registry = ADMIN_STORE.get_service_registry()
        registry_services: Mapping[str, Any] = registry.get("services") or {}
        if not isinstance(registry_services, Mapping):
            registry_services = {}
    except Exception:  # noqa: BLE001 — never break the dashboard over this
        registry_services = None  # type: ignore[assignment]
    if registry_services is None:
        # Fallback: registry unavailable — iterate frozen boot config
        registry_services = {}
        for key, raw in (config.get("backends") or {}).items():
            if isinstance(raw, Mapping):
                registry_services[key] = raw
    live_config_keys: set[str] = set((config.get("backends") or {}).keys())
    for service_id, service in registry_services.items():
        if not isinstance(service, Mapping):
            continue
        key = str(service_id)
        cfg = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        name = str(service.get("name") or key)
        is_live = key in live_config_keys
        if is_live:
            live = status_by_name.get(name) or {}
            live_status = str(live.get("live_status") or live.get("status") or "unknown")
            last_error = live.get("last_error") or ""
        else:
            live = {}
            live_status = "pending"
            last_error = ""
        role = str(service.get("role") or "required")
        if role == "standby" and live_status == "down":
            live_status = "standby"
        override = service_overrides.get(key, {})
        override_state = str(override.get("state") or "").strip()
        note = service.get("note") or last_error or ""
        if not is_live and not note:
            note = "Added in the dashboard — routing is live; status will appear once the first health probe completes."
        if override_state and override_state != "available":
            live_status = override_state
            override_reason = str(override.get("reason") or "").strip()
            note = override_reason or f"Operator mode: {override_state}"
        rows.append(
            {
                "key": key,
                "id": key,
                "name": name,
                "source": str(service.get("source") or "ui"),
                "editable": bool(service.get("editable", service.get("source") == "ui")),
                "enabled": bool_from_config(service.get("enabled"), True),
                "config": dict(cfg),
                "priority": service.get("priority"),
                "role": role,
                "capabilities": service.get("capabilities") or [],
                "status": live_status,
                "last_error": last_error,
                "note": note,
                "runtime": _admin_service_runtime_summary(
                    key,
                    status_payload=payload,
                ) if is_live else "pending health probe",
            }
        )
    return rows


def _admin_config_sources(
    snapshot: Mapping[str, Any],
    *,
    workflow_inventory: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    admin_summary = {
        "users": len(snapshot.get("users") or []),
        "apps": len(snapshot.get("apps") or []),
        "profiles": len(snapshot.get("profiles") or []),
        "credentials": len(snapshot.get("credentials") or []),
        "runtime_profile_mappings": len(_admin_identity_mappings()),
        "operator_settings": len(admin_store.list_operator_settings(path=ADMIN_STORE.path)),
    }
    env_names = [name for name in KNOWN_ENV_SOURCE_NAMES if name in os.environ]
    manifest_sources: list[Mapping[str, Any]] = []
    if workflow_inventory:
        manifest_sources.append(
            {
                "id": "workflow-manifests",
                "label": "Workflow manifests",
                "kind": "workflow",
                "source_ref": f"{len(workflow_inventory)} discovered workflows",
            }
        )
    oidc_db_setting = ADMIN_STORE.get_oidc_config_setting()
    return inventory_config_sources(
        config,
        env=os.environ,
        env_names=env_names,
        manifest_rows=manifest_sources,
        admin_db_summary=admin_summary,
        admin_db_oidc=oidc_db_setting,
        admin_db_service_registry=ADMIN_STORE.get_service_registry(),
    )


def _admin_oidc_config_snapshot() -> Mapping[str, Any] | None:
    db_setting = ADMIN_STORE.get_oidc_config_setting()
    if db_setting is not None:
        return db_setting
    cfg = _effective_admin_oidc_config()
    if not (
        cfg.enabled
        or cfg.issuer
        or cfg.client_id
        or cfg.redirect_uri
        or cfg.authorization_endpoint
        or cfg.token_endpoint
        or cfg.jwks_uri
    ):
        return None
    return {
        "source": "bootstrap",
        "enabled": cfg.enabled,
        "issuer": cfg.issuer,
        "client_id": cfg.client_id,
        "client_secret": "<redacted>" if cfg.client_secret else "",
        "client_secret_set": bool(cfg.client_secret),
        "redirect_uri": cfg.redirect_uri,
        "scopes": list(cfg.scopes),
        "allowed_email_domains": sorted(cfg.allowed_email_domains),
        "admin_groups": sorted(cfg.admin_groups),
        "admin_roles": sorted(cfg.admin_roles),
        "authorization_endpoint": cfg.authorization_endpoint,
        "token_endpoint": cfg.token_endpoint,
        "jwks_uri": cfg.jwks_uri,
        "clock_skew_seconds": cfg.clock_skew_seconds,
    }


def _operator_override_docs() -> dict[str, Any]:
    try:
        overrides = ADMIN_STORE.operator_overrides()
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] admin operator override read failed: {exc}", flush=True)
        return {"service_availability": {}, "media_job_admission": {}}
    if not isinstance(overrides, Mapping):
        return {"service_availability": {}, "media_job_admission": {}}
    return {
        "service_availability": (
            overrides.get("service_availability")
            if isinstance(overrides.get("service_availability"), Mapping)
            else {}
        ),
        "media_job_admission": (
            overrides.get("media_job_admission")
            if isinstance(overrides.get("media_job_admission"), Mapping)
            else {}
        ),
    }


def _operator_service_availability_overrides() -> dict[str, dict[str, Any]]:
    raw = _operator_override_docs().get("service_availability")
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(value, Mapping):
            out[str(key)] = dict(value)
    return out


def _service_availability_response(
    service_id: str,
    *,
    service_label: str | None = None,
) -> Response | None:
    override = _operator_service_availability_overrides().get(service_id, {})
    if not isinstance(override, Mapping):
        return None
    state = str(override.get("state") or "available").strip().lower()
    if state not in {"disabled", "maintenance", "draining"}:
        return None

    label = service_label or service_id
    message = str(override.get("reason") or "").strip() or f"{label} is {state}."
    retry_after = _optional_int_config(override, "retry_after_seconds") or 10
    return Response(
        content=json.dumps({
            "error": {
                "message": message,
                "type": "service_unavailable",
                "code": "backend_unavailable",
                "service": service_id,
                "state": state,
            }
        }),
        status_code=503,
        media_type="application/json",
        headers={"Retry-After": str(retry_after)},
    )


def _operator_media_job_admission_overrides() -> dict[str, Any]:
    raw = _operator_override_docs().get("media_job_admission")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _effective_media_job_admission_cfg() -> dict[str, Any]:
    cfg = dict(MEDIA_JOB_ADMISSION_CFG) if isinstance(MEDIA_JOB_ADMISSION_CFG, Mapping) else {}
    overrides = _operator_media_job_admission_overrides()
    for key in (
        "disabled_intents",
        "disabled_resource_classes",
        "reserved_tokens",
        "max_queue_depth",
        "retry_after_seconds",
        "queue_retry_after_seconds",
        "scan_limit",
    ):
        if key in overrides:
            cfg[key] = overrides[key]
    return cfg


def _admin_service_override_controls(
    status_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    overrides = _operator_service_availability_overrides()
    rows: list[dict[str, Any]] = []
    # Use the live registry as the canonical service source; fall back to
    # config["backends"] (frozen at boot) if the registry is unavailable.
    try:
        registry = ADMIN_STORE.get_service_registry()
        registry_services: Mapping[str, Any] = registry.get("services") or {}
        if not isinstance(registry_services, Mapping):
            registry_services = {}
    except Exception:  # noqa: BLE001
        registry_services = {}
        for key, raw in (config.get("backends") or {}).items():
            if isinstance(raw, Mapping):
                registry_services[key] = raw
    registry_keys = [str(k) for k in registry_services if isinstance(registry_services[k], Mapping)]
    for key in sorted(set(registry_keys) | set(overrides.keys())):
        svc = registry_services.get(key) or {}
        name = str(svc.get("name") or key) if isinstance(svc, Mapping) else key
        override = overrides.get(key, {})
        state = str(override.get("state") or "available")
        reason = override.get("reason")
        rows.append(
            {
                "key": key,
                "service_id": key,
                "label": f"{name} operator mode",
                "form": "service_availability",
                "action_url": "/admin/api/operator/service-availability",
                "method": "PUT",
                "current": state,
                "scope": "service availability",
                "source": "admin.db override" if key in overrides else "runtime default",
                "reason": reason,
                "retry_after_seconds": override.get("retry_after_seconds"),
                "summary": (
                    f"{state}; {reason}"
                    if reason
                    else f"{state}; applies to routes that use this service"
                ),
            }
        )
    return rows


def _admin_media_job_admission_controls() -> list[dict[str, Any]]:
    effective = _effective_media_job_admission_cfg()
    override_present = bool(_operator_media_job_admission_overrides())
    summary_parts = []
    for label, key in (
        ("Disabled intents", "disabled_intents"),
        ("Disabled resources", "disabled_resource_classes"),
        ("Reserved tokens", "reserved_tokens"),
        ("Max queue", "max_queue_depth"),
    ):
        value = effective.get(key)
        if value not in (None, "", [], {}, ()):
            summary_parts.append(f"{label}: {_format_admin_value(value)}")
    return [
        {
            "key": "media-job-admission",
            "label": "Media job admission",
            "form": "media_job_admission",
            "action_url": "/admin/api/operator/media-job-admission",
            "method": "PUT",
            "current": effective,
            "scope": "image, audio, and video jobs",
            "source": "admin.db override" if override_present else "proxy_config.yml default",
            "summary": "; ".join(summary_parts) or "No runtime media admission limits",
        }
    ]


def _admin_user_access_controls() -> list[dict[str, Any]]:
    defaults = _runtime_user_access_defaults()
    rows: list[dict[str, Any]] = []
    for key, label, summary in (
        (
            "self_service_apps",
            "User-owned Apps and keys",
            "Allows signed-in users to create their own Personal Apps and opk_ keys.",
        ),
        (
            "auto_create_personal_app",
            "Auto-create Personal App",
            "Creates a first Personal App automatically when a signed-in user needs one.",
        ),
    ):
        enabled = bool_from_config(defaults.get(key), True)
        rows.append(
            {
                "key": key,
                "label": label,
                "action_url": "/admin/api/operator/user-access-defaults",
                "method": "PUT",
                "field": key,
                "current": "true" if enabled else "false",
                "scope": "signed-in user access",
                "source": (
                    "admin.db user access defaults"
                    if _user_defaults_seeded_in_admin_db()
                    else "bootstrap default pending seed"
                ),
                "summary": summary,
                "supported_values": [
                    {"value": "true", "label": "Enabled"},
                    {"value": "false", "label": "Disabled"},
                ],
            }
        )
    return rows


def _format_admin_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set, frozenset)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _inventory_item(
    item_id: Any,
    *,
    label: Any = "",
    description: Any = "",
) -> dict[str, str]:
    out = {"id": str(item_id)}
    if label:
        out["label"] = str(label)
    if description:
        out["description"] = str(description)
    return out


def _dedupe_inventory_items(items: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        seen.add(item_id)
        out.append({
            key: str(value)
            for key, value in item.items()
            if key in {"id", "label", "description"} and value not in (None, "")
        })
    return out


def _admin_model_inventory(status_payload: Mapping[str, Any]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in _build_role_catalog_entries() + _build_workflow_model_entries():
        model_id = entry.get("id")
        if not model_id:
            continue
        label = entry.get("description") or entry.get("owned_by") or ""
        items.append(_inventory_item(model_id, label=label))

    loaded = ""
    models = status_payload.get("models") if isinstance(status_payload, Mapping) else {}
    if isinstance(models, Mapping):
        loaded = str(models.get("loaded_llm_name") or "")
    if loaded:
        items.append(_inventory_item(loaded, label="currently loaded LLM"))

    for name in AUDIO_WORKFLOWS_CFG:
        items.append(_inventory_item(name, label="audio workflow model"))
    for alias, target in (AUDIO_ALIASES or {}).items():
        items.append(_inventory_item(alias, label=f"audio alias for {target}"))

    for name in VIDEO_WORKFLOWS:
        items.append(_inventory_item(name, label="video workflow model"))
    for alias, target in (videos_cfg.get("aliases", {}) or {}).items():
        items.append(_inventory_item(alias, label=f"video alias for {target}"))

    return _dedupe_inventory_items(items)


def _admin_workflow_inventory() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in GATEWAY_WORKFLOW_REGISTRY.records():
        manifest = record.manifest
        rows.append({
            "id": record.id,
            "label": manifest.get("display_name") or record.id,
            "description": manifest.get("description") or "",
            "intent": record.intent,
            "backends": record.supported_backends,
            "resource_tier": manifest.get("resource_tier") or "batch",
            "trust_tier": manifest.get("trust_tier")
            or ("stable" if record.identity.trusted else "experimental"),
        })
    return rows


def _admin_backend_inventory(status_payload: Mapping[str, Any]) -> list[dict[str, str]]:
    status_by_name = _status_by_service_name(status_payload)
    items: list[dict[str, str]] = []
    # Read live registry; fall back to frozen boot config if unavailable.
    try:
        registry = ADMIN_STORE.get_service_registry()
        registry_services: Mapping[str, Any] = registry.get("services") or {}
        if not isinstance(registry_services, Mapping):
            registry_services = {}
    except Exception:  # noqa: BLE001
        registry_services = {}
        for k, v in (config.get("backends") or {}).items():
            if isinstance(v, Mapping):
                registry_services[k] = v
    for service_id, service in registry_services.items():
        if not isinstance(service, Mapping):
            continue
        key = str(service_id)
        cfg = service.get("config") if isinstance(service.get("config"), Mapping) else service
        name = str(service.get("name") or key)
        live = status_by_name.get(name) or {}
        status = live.get("live_status") or live.get("status") or "unknown"
        capabilities = service.get("capabilities") or (cfg.get("capabilities") if isinstance(cfg, Mapping) else None) or []
        description = ", ".join(str(item) for item in capabilities) if capabilities else ""
        if status:
            description = (description + "; " if description else "") + f"status {status}"
        items.append(_inventory_item(key, label=name, description=description))
    return _dedupe_inventory_items(items)


def _admin_profile_inventory(
    status_payload: Mapping[str, Any] | None = None,
    *,
    workflows: list[Mapping[str, Any]] | None = None,
) -> dict[str, list[dict[str, str]]]:
    payload: Mapping[str, Any] = status_payload or {}
    workflow_rows = workflows if workflows is not None else _admin_workflow_inventory()
    workflow_items = [
        _inventory_item(row.get("id"), label=row.get("label"), description=row.get("intent"))
        for row in workflow_rows
    ]
    return {
        "models": _admin_model_inventory(payload),
        "workflows": _dedupe_inventory_items(workflow_items),
        "backends": _admin_backend_inventory(payload),
    }


def _admin_model_visibility(status_payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload: Mapping[str, Any] = status_payload or {}
    models = payload.get("models") if isinstance(payload, Mapping) else {}
    loaded = ""
    if isinstance(models, Mapping):
        loaded = str(models.get("loaded_llm_name") or "")
    if not loaded:
        loaded_path = currently_loaded_kobold_model()
        loaded = os.path.basename(loaded_path) if loaded_path else ""
    return {
        "mode": "per-Profile catalog or current-loaded LLM",
        "loaded_model": loaded or "unknown",
        "list_behavior": (
            "/v1/models is filtered by the caller's Profile; current-loaded "
            "Profiles only see active loaded LLM ids."
        ),
        "route_behavior": (
            "Allowed role/catalog requests may swap models"
            if not PIN_LOADED_MODEL
            else "Global pin_loaded_model prevents automatic LLM swaps"
        ),
    }


def _admin_identity_mappings() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    source_labels = {
        "email": "email",
        "domain": "email domain",
        "oidc_group": "OIDC group",
        "oidc_role": "OIDC role",
        "gateway_role": "gateway admin role",
        "all_users": "all signed-in users",
    }
    active_mapping_keys: set[tuple[str, str, str]] = set()
    for mapping in admin_store.list_runtime_profile_mappings(path=ADMIN_STORE.path):
        source_kind = str(mapping.get("source_kind") or "")
        source_value = str(mapping.get("source_value") or "")
        profile_id = _profile_ref_to_store_id(mapping.get("profile_id")) or "default-local"
        active_mapping_keys.add((source_kind, source_value, profile_id))
        label = source_labels.get(source_kind, source_kind or "identity")
        source = label if source_kind == "all_users" else f"{label}: {source_value}"
        rows.append(
            {
                "id": mapping.get("id"),
                "source": source,
                "scope": "runtime Profile mapping",
                "profile_id": profile_id,
                "note": "Managed here. This does not grant admin access.",
            }
        )

    bootstrap_defaults_seeded = _user_defaults_seeded_in_admin_db()

    def add_mapping(scope: str, source_kind: str, mapping_name: str) -> None:
        if bootstrap_defaults_seeded:
            return
        mapping = user_defaults_cfg.get(mapping_name)
        if not isinstance(mapping, Mapping):
            return
        for source, profile in sorted(mapping.items()):
            profile_id = _mapping_profile_id(profile)
            if not profile_id:
                continue
            source_value = " ".join(str(source or "").strip().lower().strip("/").split())
            if (source_kind, source_value, profile_id) in active_mapping_keys:
                continue
            rows.append(
                {
                    "source": f"{scope}: {source}",
                    "scope": "bootstrap fallback",
                    "profile_id": profile_id,
                    "note": "Imported into admin.db on startup when no matching runtime mapping exists.",
                }
            )

    add_mapping("gateway role", "gateway_role", "role_profile_ids")
    add_mapping("email domain", "domain", "domain_profile_ids")
    add_mapping("email", "email", "email_profile_ids")
    default_profile = _mapping_profile_id(user_defaults_cfg.get("default_profile_id"))
    if (
        default_profile
        and not bootstrap_defaults_seeded
        and ("all_users", "*", default_profile) not in active_mapping_keys
    ):
        rows.append(
            {
                "source": "all signed-in users",
                "scope": "bootstrap fallback",
                "profile_id": default_profile,
                "note": "Imported into admin.db on startup when no all-users runtime mapping exists.",
            }
        )
    if not rows:
        rows.append(
            {
                "source": "all signed-in users",
                "scope": "runtime default",
                "profile_id": "default-local",
                "note": "No custom runtime default mapping is configured.",
            }
        )
    return rows


@app.get("/admin", include_in_schema=False)
async def admin_dashboard(request: Request):
    try:
        actor = ADMIN_AUTH(request)
    except AdminAuthError as exc:
        # A BROWSER (no Authorization header) should never see a raw JSON 401/403
        # dead end on /admin. Only genuine API callers (presenting a Bearer
        # token) get the JSON error.
        is_browser = not request.headers.get("authorization")
        if is_browser and exc.status_code == 401:
            # Not signed in -> send to the right sign-in entry for the auth mode.
            if _effective_admin_oidc_config().enabled:
                return RedirectResponse(
                    _login_url(return_to="/admin"), status_code=302
                )
            return RedirectResponse("/setup", status_code=302)
        if is_browser and exc.status_code == 403:
            # Signed in but not an admin -> their home is the portal, not /admin.
            return RedirectResponse("/", status_code=302)
        return Response(
            content=json.dumps({
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                }
            }),
            status_code=exc.status_code,
            media_type="application/json",
            headers={"WWW-Authenticate": "Bearer"},
        )
    snapshot = ADMIN_STORE.dashboard_snapshot(actor)
    try:
        status_payload = await status_endpoint.get_cached_status(config)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] admin status enrichment failed: {e}", flush=True)
        status_payload = {}
    snapshot["api_surfaces"] = _admin_api_surfaces()
    snapshot["services"] = _admin_services(status_payload)
    workflow_inventory = _admin_workflow_inventory()
    snapshot["workflow_inventory"] = workflow_inventory
    snapshot["config_sources"] = _admin_config_sources(
        snapshot,
        workflow_inventory=workflow_inventory,
    )
    snapshot["service_overrides"] = _admin_service_override_controls(status_payload)
    snapshot["operator_overrides"] = _admin_user_access_controls()
    snapshot["resource_overrides"] = _admin_media_job_admission_controls()
    snapshot["profile_inventory"] = _admin_profile_inventory(
        status_payload,
        workflows=workflow_inventory,
    )
    snapshot["policy_catalog"] = profile_policy_catalog()
    snapshot["model_visibility"] = _admin_model_visibility(status_payload)
    snapshot["identity_mappings"] = _admin_identity_mappings()
    try:
        snapshot["oidc_config"] = _admin_oidc_config_snapshot()
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] admin OIDC setting read failed: {exc}", flush=True)
        snapshot["oidc_config"] = None
    # "Things that want a look" — derived from the same data the tables show
    # (now including live service health). This is the operator's landing list.
    snapshot["attention"] = derive_admin_attention(snapshot)
    brand_label, brand_wordmark = _brand_settings()
    body = render_admin_dashboard(
        snapshot,
        title="AI Gateway Admin",
        brand=brand_label,
        wordmark=brand_wordmark,
    )
    return Response(
        content=body,
        status_code=200,
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-store"},
    )


async def _start_browser_login(request: Request, *, default_return_to: str) -> Response:
    if not _effective_admin_oidc_config().enabled:
        return Response(
            content=(
                "OIDC browser login is not configured on this gateway yet. "
                "Open /setup to see what to configure. "
                "(`python proxy.py generate-admin-key <name>` is supported "
                "for recovery and headless automation, not normal first-run "
                "setup.)"
            ),
            status_code=501,
            media_type="text/plain; charset=utf-8",
        )
    try:
        cfg = await _admin_oidc_config_with_discovery()
        nonce = generate_nonce()
        pkce = generate_pkce_pair()
        return_to = _safe_admin_return_to(
            request.query_params.get("return_to"),
            default=default_return_to,
        )
        state = record_state(
            ADMIN_OIDC_STATES,
            nonce=nonce,
            pkce_verifier=pkce.verifier,
            return_to=return_to,
        )
        return RedirectResponse(
            authorization_url(cfg, state=state.state, nonce=nonce, pkce=pkce),
            status_code=302,
        )
    except Exception as e:  # noqa: BLE001
        return Response(
            content=f"OIDC login failed to start: {str(e)[:200]}",
            status_code=500,
            media_type="text/plain; charset=utf-8",
        )


def _owner_or_admin_exists() -> bool:
    """True if any non-revoked owner or admin role is assigned.

    Covers both OIDC user roles and service-account admin keys, since either
    is enough to manage the gateway.
    """
    return bool(_existing_admin_roles_by_email())


def _admin_credential_active(record: Mapping[str, Any]) -> bool:
    if record.get("revoked_at"):
        return False
    expires_at = record.get("expires_at")
    return expires_at is None or int(expires_at) > int(time.time())


def _setup_admin_counts() -> tuple[int, int]:
    apps = admin_store.list_apps(path=ADMIN_STORE.path)
    credentials = admin_store.list_credentials(path=ADMIN_STORE.path)
    active_runtime_keys = sum(
        1
        for credential in credentials
        if credential.get("subject_kind") == "app"
        and _admin_credential_active(credential)
    )
    return len([app for app in apps if not app.get("disabled_at")]), active_runtime_keys


def _setup_personal_app_for_session(session) -> Mapping[str, Any] | None:
    if session is None:
        return None
    for app in admin_store.list_apps_for_user(
        str(getattr(session, "subject", "") or ""),
        path=ADMIN_STORE.path,
    ):
        if app.get("kind") == "personal" and not app.get("disabled_at"):
            return app
    return None


def _setup_active_key_count_for_app(app_id: str | None) -> int:
    if not app_id:
        return 0
    return len(
        admin_store.list_active_credentials(
            subject_kind="app",
            app_id=str(app_id),
            path=ADMIN_STORE.path,
        )
    )


def _setup_doctor_items(
    *,
    signed_in_admin: bool,
    app_count: int,
    active_key_count: int,
    status_payload: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = [
        {
            "label": "Owner/Admin",
            "state": "ok" if _owner_or_admin_exists() else "bad",
            "detail": "Control-plane owner or admin exists.",
        },
        {
            "label": "Browser session",
            "state": "ok" if signed_in_admin else "warn",
            "detail": (
                "This browser can administer the gateway."
                if signed_in_admin
                else "Sign in or claim with an oak_ admin key."
            ),
        },
        {
            "label": "Runtime App key",
            "state": "ok" if active_key_count else "warn",
            "detail": (
                f"{active_key_count} active runtime key(s) across {app_count} App(s)."
                if active_key_count
                else "Create a first App key before connecting SDKs or clients."
            ),
        },
        _setup_config_source_item(),
    ]
    if not status_payload:
        items.append(
            {
                "label": "Gateway health",
                "state": "warn",
                "detail": "Status cache was unavailable; use /status for operator detail.",
            }
        )
        return items
    health_state = "ok" if not status_payload.get("error") else "warn"
    items.append(
        {
            "label": "Gateway health",
            "state": health_state,
            "detail": "Health/status cache loaded." if health_state == "ok" else "Status cache reported an error.",
        }
    )
    backends = status_payload.get("backends") if isinstance(status_payload, Mapping) else []
    if isinstance(backends, list) and backends:
        up = sum(1 for row in backends if isinstance(row, Mapping) and row.get("status") == "up")
        degraded = sum(
            1
            for row in backends
            if isinstance(row, Mapping) and row.get("status") == "degraded"
        )
        down = sum(1 for row in backends if isinstance(row, Mapping) and row.get("status") == "down")
        state = "ok" if down == 0 and degraded == 0 else ("warn" if up else "bad")
        items.append(
            {
                "label": "Backends",
                "state": state,
                "detail": f"{up} up, {degraded} degraded, {down} down.",
            }
        )
    return items


def _setup_config_source_item() -> dict[str, str]:
    try:
        access_defaults = admin_store.get_operator_setting(
            USER_ACCESS_DEFAULTS_SETTING_KEY,
            path=ADMIN_STORE.path,
        )
    except Exception:  # noqa: BLE001
        access_defaults = None
    has_bootstrap_defaults = _has_bootstrap_user_access_defaults()
    if access_defaults is not None:
        state = "ok"
        detail = (
            "Runtime Apps, keys, Profiles, and identity defaults are admin.db-backed."
        )
    elif has_bootstrap_defaults:
        state = "warn"
        detail = (
            "Bootstrap user defaults have not been seeded into admin.db yet; restart "
            "or check the admin store."
        )
    else:
        state = "ok"
        detail = "Runtime defaults are admin.db-backed."
    return {
        "label": "Config source of truth",
        "state": state,
        "detail": detail,
    }


def _setup_admin_context(request: Request) -> AdminContext | None:
    try:
        actor = ADMIN_AUTH(request)
    except AdminAuthError:
        return None
    return actor if actor.is_admin else None


async def _setup_page_response(
    request: Request,
    *,
    claim_error: str | None = None,
    app_error: str | None = None,
    create_error: str | None = None,
    create_email: str | None = None,
    login_error: str | None = None,
    login_email: str | None = None,
    one_time_key: Mapping[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    session = _oidc_session_from_request(request)
    is_admin_signed_in = False
    actor_label: str | None = None
    if session is not None and session.role in {"owner", "admin", "superadmin"}:
        is_admin_signed_in = True
        actor_label = session.email or session.subject or None
    app_count, active_key_count = _setup_admin_counts()
    personal_app = _setup_personal_app_for_session(session) if is_admin_signed_in else None
    personal_app_name = (
        str(personal_app.get("name") or "") if personal_app is not None else None
    )
    personal_active_key_count = _setup_active_key_count_for_app(
        str(personal_app.get("id") or "") if personal_app is not None else None
    )
    status_payload: Mapping[str, Any] | None = None
    if is_admin_signed_in:
        try:
            status_payload = await status_endpoint.get_cached_status(config)
        except Exception as e:  # noqa: BLE001
            print(f"[proxy] setup status enrichment failed: {e}", flush=True)
            status_payload = None
    brand_label, brand_wordmark = _brand_settings()
    body = render_setup_page(
        brand=brand_label,
        wordmark=brand_wordmark,
        owner_or_admin_exists=_owner_or_admin_exists(),
        oidc_enabled=_effective_admin_oidc_config().enabled,
        login_url=_login_url(return_to="/setup"),
        is_admin_signed_in=is_admin_signed_in,
        actor_label=actor_label,
        logout_url="/logout" if is_admin_signed_in else None,
        claim_error=claim_error,
        can_claim_with_admin_key=bool(_browser_admin_session_signing_key()),
        create_error=create_error,
        create_email=create_email,
        login_error=login_error,
        login_email=login_email,
        csrf_token=_csrf_token_for_oidc_session(session) if session else "",
        public_base_url=_public_base_url(request) or "http://127.0.0.1:5000",
        app_count=app_count,
        active_key_count=active_key_count,
        personal_app_name=personal_app_name,
        personal_active_key_count=personal_active_key_count,
        app_error=app_error,
        one_time_key=one_time_key,
        doctor_items=_setup_doctor_items(
            signed_in_admin=is_admin_signed_in,
            app_count=app_count,
            active_key_count=active_key_count,
            status_payload=status_payload,
        )
        if is_admin_signed_in
        else [],
    )
    return Response(
        content=body,
        status_code=status_code,
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-store"},
    )


@app.get("/setup", include_in_schema=False)
async def setup_page(request: Request):
    # A signed-in owner/admin has no reason to LAND on the setup console — setup
    # is done. Send them to their portal instead of an "operator setup" card
    # that only costs attention to dismiss. The console's tools (mint a key from
    # the console, gateway doctor) stay reachable deliberately via ?operator=1
    # — for headless/recovery use — but they're never the default landing.
    session = _oidc_session_from_request(request)
    wants_operator_console = request.query_params.get("operator") == "1"
    # Only redirect when the cookie resolves to a REAL, still-existing user.
    # A stale/orphaned cookie (e.g. from a prior instance whose DB was reset)
    # passes the signature check but its user is gone — redirecting it to "/"
    # would loop (/ can't resolve the user → shows the door → click sign-in →
    # /setup → redirect → …). Falling through to the sign-in form breaks the
    # loop and lets them sign in fresh.
    session_user_exists = bool(
        session is not None
        and admin_store.get_user(session.subject, path=ADMIN_STORE.path)
    )
    if (
        session is not None
        and session.role in {"owner", "admin", "superadmin"}
        and session_user_exists
        and not wants_operator_console
    ):
        return RedirectResponse("/", status_code=303)
    return await _setup_page_response(request)


async def _setup_form_values(request: Request) -> dict[str, str]:
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, Mapping):
            return {}
        return {str(key): str(value).strip() for key, value in body.items()}
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: str((values or [""])[0]).strip() for key, values in parsed.items()}


async def _setup_claim_admin_key_value(request: Request) -> str:
    return (await _setup_form_values(request)).get("admin_key", "")


@app.post("/setup/claim", include_in_schema=False)
async def setup_claim_admin_key(request: Request):
    signing_key = _browser_admin_session_signing_key()
    if not signing_key:
        return await _setup_page_response(
            request,
            claim_error=(
                "Browser admin sessions are unavailable: this gateway "
                "cannot read or write its session signing key file."
            ),
            status_code=500,
        )
    admin_key = await _setup_claim_admin_key_value(request)
    if admin_key.startswith("opk_"):
        return await _setup_page_response(
            request,
            claim_error=(
                "That looks like a runtime App key (opk_). Setup needs an "
                "admin key (oak_) from generate-admin-key."
            ),
            status_code=400,
        )
    if not admin_key.startswith("oak_"):
        return await _setup_page_response(
            request,
            claim_error="Paste the oak_ admin key printed by generate-admin-key.",
            status_code=400,
        )
    credential = admin_store.find_credential_by_hash(
        hash_api_key(admin_key),
        path=ADMIN_STORE.path,
    )
    if credential is None or not _admin_credential_active(credential):
        return await _setup_page_response(
            request,
            claim_error="That admin key was not found, expired, or has been revoked.",
            status_code=400,
        )
    subject_kind = str(credential.get("subject_kind") or "")
    subject_id = str(credential.get("subject_id") or "")
    if subject_kind not in {"user", "service_account"} or not subject_id:
        return await _setup_page_response(
            request,
            claim_error="That key is not an admin key.",
            status_code=400,
        )
    roles = admin_store.list_admin_roles(
        subject_kind=subject_kind,
        subject_id=subject_id,
        path=ADMIN_STORE.path,
    )
    role_names = {str(role.get("role") or "") for role in roles}
    if "owner" in role_names:
        role = "owner"
    elif "admin" in role_names or "superadmin" in role_names:
        role = "admin"
    else:
        return await _setup_page_response(
            request,
            claim_error="That admin key does not have owner or admin permission.",
            status_code=403,
        )
    user = admin_store.upsert_oidc_user(
        "local-admin-key",
        f"{subject_kind}:{subject_id}",
        email="local-admin",
        display_name="Local admin key",
        path=ADMIN_STORE.path,
    )
    admin_store.assign_admin_role(
        role=role,
        subject_kind="user",
        subject_id=str(user["id"]),
        granted_by="setup-claim",
        path=ADMIN_STORE.path,
    )
    admin_store.record_audit(
        "setup.claim",
        actor_kind=subject_kind,
        actor_id=subject_id,
        target_kind="user",
        target_id=str(user["id"]),
        metadata={"role": role, "credential_prefix": credential.get("prefix")},
        path=ADMIN_STORE.path,
    )
    session = session_from_claims(
        {"sub": str(user["id"]), "email": "local-admin"},
        role=role,
        cookie=_browser_admin_cookie_config(),
    )
    response = RedirectResponse("/setup", status_code=303)
    cookie = _browser_admin_cookie_config()
    response.set_cookie(
        cookie.name,
        encode_session_cookie(session, signing_key=signing_key),
        max_age=cookie.max_age_seconds,
        secure=cookie.secure,
        httponly=cookie.http_only,
        samesite=cookie.same_site,
        path=cookie.path,
        domain=cookie.domain,
    )
    return response


# ---------------------------------------------------------------------------
# Local owner: web-first claim + sign-in
# ---------------------------------------------------------------------------


_LOCAL_OWNER_ISSUER = "local"


def _normalize_local_email(value: str) -> str:
    return (value or "").strip().lower()


def _local_owner_user_record() -> Mapping[str, Any] | None:
    """Return the local-issuer user that owns this gateway, if any.

    Walks active owner/admin role assignments and returns the first that
    belongs to a local-issuer user. We don't cache because the result is
    only consulted during the unauthenticated /setup/local-login path and
    when bootstrapping, both of which are cold by definition.
    """
    for role_row in admin_store.list_admin_roles(path=ADMIN_STORE.path):
        if role_row.get("subject_kind") != "user":
            continue
        if role_row.get("role") not in {"owner", "admin", "superadmin"}:
            continue
        user_id = str(role_row.get("subject_id") or "")
        if not user_id:
            continue
        user = admin_store.get_user(user_id, path=ADMIN_STORE.path)
        if user is not None and user.get("issuer") == _LOCAL_OWNER_ISSUER:
            return user
    return None


def _issue_browser_admin_session(
    *,
    user_id: str,
    email: str,
    role: str,
    signing_key: bytes,
    redirect_to: str = "/setup",
) -> Response:
    cookie = _browser_admin_cookie_config()
    session = session_from_claims(
        {"sub": user_id, "email": email},
        role=role,
        cookie=cookie,
    )
    response = RedirectResponse(redirect_to, status_code=303)
    response.set_cookie(
        cookie.name,
        encode_session_cookie(session, signing_key=signing_key),
        max_age=cookie.max_age_seconds,
        secure=cookie.secure,
        httponly=cookie.http_only,
        samesite=cookie.same_site,
        path=cookie.path,
        domain=cookie.domain,
    )
    return response


@app.post("/setup/local-owner", include_in_schema=False)
async def setup_create_local_owner(request: Request):
    """Create the first local owner account from the browser.

    Only honored when no owner/admin role exists yet. After the account is
    created it gets the ``owner`` role and the browser receives a signed
    admin session; further calls land in the ``owner_or_admin_exists``
    branch of /setup.
    """
    signing_key = _browser_admin_session_signing_key()
    if not signing_key:
        return await _setup_page_response(
            request,
            create_error=(
                "Browser admin sessions are unavailable: this gateway "
                "cannot read or write its session signing key file."
            ),
            status_code=500,
        )
    if _owner_or_admin_exists():
        # First-run is over. Redirect through /setup so the operator sees the
        # signed-in landing card and can sign in if needed.
        return RedirectResponse("/setup", status_code=303)

    values = await _setup_form_values(request)
    email = _normalize_local_email(values.get("email", ""))
    password = values.get("password", "")
    confirm = values.get("password_confirm", "")

    if not email:
        return await _setup_page_response(
            request,
            create_error="Enter an email or username for the owner account.",
            create_email=email or None,
            status_code=400,
        )
    if not password:
        return await _setup_page_response(
            request,
            create_error="Enter a password for the owner account.",
            create_email=email,
            status_code=400,
        )
    if password != confirm:
        return await _setup_page_response(
            request,
            create_error="The two passwords did not match.",
            create_email=email,
            status_code=400,
        )

    try:
        verifier = local_hash_password(password)
    except ValueError as exc:
        return await _setup_page_response(
            request,
            create_error=f"Could not store that password: {exc}",
            create_email=email,
            status_code=400,
        )

    user = admin_store.upsert_oidc_user(
        _LOCAL_OWNER_ISSUER,
        email,
        email=email,
        display_name=email,
        path=ADMIN_STORE.path,
    )
    admin_store.set_local_password_verifier(
        str(user["id"]),
        verifier,
        path=ADMIN_STORE.path,
    )
    admin_store.assign_admin_role(
        role="owner",
        subject_kind="user",
        subject_id=str(user["id"]),
        granted_by="setup-local-owner",
        path=ADMIN_STORE.path,
    )
    admin_store.record_audit(
        "setup.local_owner_create",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="user",
        target_id=str(user["id"]),
        metadata={"email": email, "via": "setup_page"},
        path=ADMIN_STORE.path,
    )
    # Parity with OIDC login: give the new owner a personal App up front so the
    # portal can hand them a working key without a separate "create App" step.
    _ensure_personal_app_for_owner(user, role="owner")
    # Setup is done — hand the new owner to the portal, where their key is
    # auto-minted and revealed once. Landing back on /setup would greet them
    # with the operator "this gateway has an owner" recovery card (third-person,
    # plus a redundant second key-mint flow). The portal is the real home.
    return _issue_browser_admin_session(
        user_id=str(user["id"]),
        email=email,
        role="owner",
        signing_key=signing_key,
        redirect_to="/",
    )


@app.post("/setup/local-login", include_in_schema=False)
async def setup_local_login(request: Request):
    """Sign an existing local owner / admin back in from /setup."""
    signing_key = _browser_admin_session_signing_key()
    if not signing_key:
        return await _setup_page_response(
            request,
            login_error=(
                "Browser admin sessions are unavailable: this gateway "
                "cannot read or write its session signing key file."
            ),
            status_code=500,
        )

    values = await _setup_form_values(request)
    email = _normalize_local_email(values.get("email", ""))
    password = values.get("password", "")
    generic_error = (
        "Sign-in name or password did not match a local owner account."
    )

    if not email or not password:
        return await _setup_page_response(
            request,
            login_error=generic_error,
            login_email=email or None,
            status_code=400,
        )

    user = admin_store.find_user_by_email(
        email, issuer=_LOCAL_OWNER_ISSUER, path=ADMIN_STORE.path
    )
    if user is None:
        return await _setup_page_response(
            request,
            login_error=generic_error,
            login_email=email,
            status_code=401,
        )
    verifier_row = admin_store.get_local_password_verifier(
        str(user["id"]), path=ADMIN_STORE.path
    )
    if verifier_row is None or not local_verify_password(
        password, str(verifier_row.get("verifier") or "")
    ):
        return await _setup_page_response(
            request,
            login_error=generic_error,
            login_email=email,
            status_code=401,
        )
    roles = admin_store.list_admin_roles(
        subject_kind="user",
        subject_id=str(user["id"]),
        path=ADMIN_STORE.path,
    )
    role_names = {str(role.get("role") or "") for role in roles}
    if "owner" in role_names:
        role = "owner"
    elif "admin" in role_names or "superadmin" in role_names:
        role = "admin"
    else:
        return await _setup_page_response(
            request,
            login_error=(
                "That account no longer has owner or admin permission. "
                "Ask another admin to restore your role."
            ),
            login_email=email,
            status_code=403,
        )
    admin_store.record_audit(
        "setup.local_owner_login",
        actor_kind="user",
        actor_id=str(user["id"]),
        target_kind="user",
        target_id=str(user["id"]),
        metadata={"email": email, "via": "setup_page"},
        path=ADMIN_STORE.path,
    )
    # A returning owner/admin signing in from /setup lands on the portal, their
    # home — not back on the operator setup console.
    return _issue_browser_admin_session(
        user_id=str(user["id"]),
        email=email,
        role=role,
        signing_key=signing_key,
        redirect_to="/",
    )


@app.post("/setup/first-app", include_in_schema=False)
async def setup_create_first_app(request: Request):
    actor = _setup_admin_context(request)
    session = _oidc_session_from_request(request)
    if actor is None or actor.auth_method != "session":
        return await _setup_page_response(
            request,
            claim_error="Sign in or claim an admin browser session before creating an App.",
            status_code=401,
        )
    values = await _setup_form_values(request)
    expected = actor.csrf_token or ""
    supplied = values.get("csrf_token") or request.headers.get("x-ai-gateway-csrf") or ""
    if expected and not hmac.compare_digest(supplied, expected):
        return await _setup_page_response(
            request,
            app_error="The setup form expired. Reload /setup and try again.",
            status_code=403,
        )
    app_name = (values.get("app_name") or "first-app").strip()
    key_label = (values.get("key_label") or "Setup key").strip()
    if not app_name:
        return await _setup_page_response(
            request,
            app_error="App name is required.",
            status_code=400,
        )
    try:
        if session is not None and _user_self_service_enabled():
            app_record = _setup_personal_app_for_session(session)
            if app_record is None:
                payload = request_user_access(
                    session,
                    default_profile_id=_default_profile_id_for_session(session),
                    path=ADMIN_STORE.path,
                )
                app_record = payload["app"]
            credential = mint_user_app_key(
                session,
                str(app_record["id"]),
                label=key_label,
                default_profile_id=_default_profile_id_for_session(session),
                path=ADMIN_STORE.path,
            )
            admin_store.record_audit(
                "setup.first_app_key",
                actor_kind="user",
                actor_id=str(getattr(session, "subject", "") or ""),
                target_kind="credential",
                target_id=str(credential.get("id") or ""),
                metadata={
                    "via": "setup_page",
                    "app_id": app_record.get("id"),
                    "prefix": credential.get("prefix"),
                },
                path=ADMIN_STORE.path,
            )
        else:
            app_record = ADMIN_STORE.create_app(
                data={"name": app_name, "kind": "service"},
                actor=actor,
            )
            credential = ADMIN_STORE.create_credential(
                app_id=str(app_record["id"]),
                data={"label": key_label},
                actor=actor,
            )
            admin_store.record_audit(
                "setup.first_app_key",
                actor_kind="admin",
                actor_id=actor.subject,
                target_kind="credential",
                target_id=str(credential.get("id") or ""),
                metadata={
                    "via": "setup_page",
                    "app_id": app_record.get("id"),
                    "prefix": credential.get("prefix"),
                },
                path=ADMIN_STORE.path,
            )
    except UserPortalError as e:
        return await _setup_page_response(
            request,
            app_error=e.message,
            status_code=e.status_code,
        )
    except Exception as e:  # noqa: BLE001
        return await _setup_page_response(
            request,
            app_error=f"Could not create the App key: {str(e)[:180]}",
            status_code=400,
        )
    return await _setup_page_response(
        request,
        one_time_key={
            "api_key": credential.get("api_key"),
            "app_name": app_record.get("name") or app_name,
            "credential_prefix": credential.get("prefix"),
        },
        status_code=201,
    )


@app.get("/login", include_in_schema=False)
async def browser_login(request: Request):
    return await _start_browser_login(request, default_return_to="/")


@app.get("/admin/login", include_in_schema=False)
async def admin_login_compat(request: Request):
    return_to = _safe_admin_return_to(
        request.query_params.get("return_to"),
        default="/admin",
    )
    return RedirectResponse(_login_url(return_to=return_to), status_code=302)


@app.get("/admin/oidc/callback", include_in_schema=False)
async def admin_oidc_callback(request: Request):
    if not _effective_admin_oidc_config().enabled:
        return Response(status_code=404)
    error = request.query_params.get("error")
    if error:
        return Response(
            content=f"OIDC provider returned error: {error}",
            status_code=400,
            media_type="text/plain; charset=utf-8",
        )
    code = request.query_params.get("code")
    state_value = request.query_params.get("state")
    if not code or not state_value:
        return Response(
            content="OIDC callback missing code or state",
            status_code=400,
            media_type="text/plain; charset=utf-8",
        )
    try:
        state = consume_state(ADMIN_OIDC_STATES, state_value)
        cfg = await _admin_oidc_config_with_discovery()
        tokens = await _oidc_exchange_code(
            code,
            config=cfg,
            pkce=PKCEPair(
                verifier=state.pkce_verifier,
                challenge="already-sent",
            ),
        )
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise ValueError("token endpoint did not return id_token")
        claims = await _oidc_verify_id_token(id_token, config=cfg)
        validate_id_token_claims(claims, config=cfg, expected_nonce=state.nonce)
        user = admin_store.upsert_oidc_user(
            cfg.issuer,
            str(claims["sub"]),
            email=str(claims.get("email") or ""),
            display_name=str(claims.get("name") or claims.get("email") or ""),
            path=ADMIN_STORE.path,
        )
        oidc_access_names = access_names_from_oidc_claims(
            claims,
            client_id=cfg.client_id,
            groups_claim=cfg.groups_claim,
        )
        admin_store.replace_user_identity_claims(
            str(user["id"]),
            oidc_access_names,
            path=ADMIN_STORE.path,
        )
        role = bootstrap_admin_role(
            str(claims.get("email") or ""),
            config=cfg,
            existing_roles=_existing_admin_roles_by_email(),
        )
        if role == "none":
            role = admin_role_from_oidc_claims(claims, config=cfg)
        if role in {"owner", "admin"}:
            admin_store.assign_admin_role(
                role=role,
                subject_kind="user",
                subject_id=user["id"],
                granted_by="oidc",
                path=ADMIN_STORE.path,
            )
        if _user_self_service_enabled() and _auto_create_personal_app_enabled():
            label = user.get("email") or user.get("display_name") or user["id"]
            default_profile_id = _default_profile_id_for_user(
                email=str(user.get("email") or ""),
                role=role,
                oidc_groups=oidc_access_names["oidc_group"],
                oidc_roles=oidc_access_names["oidc_role"],
            )
            app = admin_store.ensure_personal_app_for_user(
                str(user["id"]),
                name=f"{label} personal access",
                default_profile_id=default_profile_id,
                path=ADMIN_STORE.path,
            )
            admin_store.record_audit(
                "app.self_create",
                actor_kind="user",
                actor_id=str(user["id"]),
                target_kind="app",
                target_id=str(app["id"]),
                metadata={
                    "email": user.get("email"),
                    "via": "oidc_login",
                    "effective_profile_id": default_profile_id or "default-local",
                },
                path=ADMIN_STORE.path,
            )
        session_claims = {
            "sub": user["id"],
            "email": claims.get("email") or "",
        }
        session = session_from_claims(
            session_claims,
            role=role,
            cookie=cfg.cookie,
        )
        response = RedirectResponse(_safe_admin_return_to(state.return_to), status_code=302)
        response.set_cookie(
            cfg.cookie.name,
            encode_session_cookie(session, signing_key=cfg.signing_key),
            max_age=cfg.cookie.max_age_seconds,
            secure=cfg.cookie.secure,
            httponly=cfg.cookie.http_only,
            samesite=cfg.cookie.same_site,
            path=cfg.cookie.path,
            domain=cfg.cookie.domain,
        )
        return response
    except (SessionError, IDTokenClaimsError, ValueError, httpx.HTTPError) as e:
        return Response(
            content=f"OIDC callback failed: {str(e)[:200]}",
            status_code=400,
            media_type="text/plain; charset=utf-8",
        )


@app.post("/admin/logout", include_in_schema=False)
async def admin_logout():
    target = _login_url(return_to="/admin") if _effective_admin_oidc_config().enabled else "/setup"
    response = RedirectResponse(target, status_code=302)
    cookie = _browser_admin_cookie_config()
    response.delete_cookie(
        cookie.name,
        path=cookie.path,
        domain=cookie.domain,
    )
    return response


@app.post("/admin/api/config/reload", include_in_schema=False)
async def admin_config_reload(request: Request):
    """Hot-reload the runtime config from the DB service registry.

    Re-derives all backend host constants (TABBY_HOST, KOBOLD_HOST,
    LLM_BACKEND_HOSTS, TTS_HOST, STT_HOST, EMBEDDINGS_HOST, COMFY_HOST …)
    without a process restart.  Requests that arrive after this returns will
    see the new values.

    Note: the LiteLLM subprocess still requires a separate restart to pick up
    its config-file changes.
    """
    try:
        ADMIN_AUTH(request)
    except AdminAuthError as exc:
        return Response(
            content=json.dumps({"error": {"code": exc.code, "message": exc.message}}),
            status_code=exc.status_code,
            media_type="application/json",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        updates = reload_runtime_config()
        return JSONResponse(
            status_code=200,
            content={
                "reloaded": True,
                "backends": {
                    "llm_hosts": updates["LLM_BACKEND_HOSTS"],
                    "tabby": updates["TABBY_HOST"],
                    "kobold": updates["KOBOLD_HOST"],
                    "tts": updates["TTS_HOST"],
                    "stt": updates["STT_HOST"],
                    "embeddings": updates["EMBEDDINGS_HOST"],
                    "comfyui": updates["COMFY_HOST"],
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "reload_failed", "message": str(exc)}},
        )


# Ephemeral keys that must NOT appear in the config export (they are runtime/
# health state, not operator-authored configuration).
_EXPORT_EPHEMERAL_KEYS = frozenset(
    {
        "health",
        "last_used",
        "last_used_at",
        "vram",
        "vram_used",
        "vram_gb_used",
        "probed_at",
        "probe_window",
        "health_history",
        "last_error",
        "live_status",
        "runtime",
    }
)


def _service_record_for_export(service_id: str, record: dict) -> dict:
    """Return a clean, export-safe copy of one service record.

    Strips ephemeral runtime keys so the export is reproducible and
    round-trippable via the seed-import path.
    """
    out: dict = {}
    for key, value in record.items():
        if key in _EXPORT_EPHEMERAL_KEYS:
            continue
        if isinstance(value, dict):
            # Recursively strip ephemeral keys from nested dicts (e.g. config).
            out[key] = {
                k: v for k, v in value.items() if k not in _EXPORT_EPHEMERAL_KEYS
            }
        else:
            out[key] = value
    return out


def _build_config_export_yaml() -> str:
    """Serialize the current DB service registry to canonical YAML.

    Returns a UTF-8 string suitable for committing to git.  Only
    operator-authored configuration is included; ephemeral runtime state
    (health history, last_used_at, probed windows, live VRAM readings) is
    explicitly excluded.

    Shape: matches the ``services`` block the seed-import path already
    understands, so ``import-from-file`` (existing seed path) can round-trip
    this export without modification.
    """
    registry = ADMIN_STORE.get_service_registry()
    services_raw = (registry.get("services") or {}) if isinstance(registry, dict) else {}

    services_export: dict = {}
    for svc_id, record in sorted(services_raw.items()):
        if not isinstance(record, dict):
            continue
        services_export[svc_id] = _service_record_for_export(svc_id, record)

    export_doc = {
        "services": services_export,
    }

    header = (
        "# Gateway service registry — canonical export\n"
        "# Generated by GET /admin/api/config/export\n"
        "# This file reproduces the running gateway's service registry.\n"
        "# Ephemeral runtime state (health, last_used_at, VRAM) is excluded.\n"
        "# Round-trippable: use as a seed file via the existing import-seed path.\n"
        "#\n"
        "# NOTE: full file-wins git reconcile (services.yml as the authoritative\n"
        "# source with UI-only overlay in services.local.yml) is roadmap.\n"
        "# For now: export is read-only; edits go through the admin UI or DB.\n"
        "#\n"
    )
    body = yaml.dump(
        export_doc,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=True,
    )
    return header + body


@app.get("/admin/api/config/export", include_in_schema=False)
async def admin_config_export(request: Request):
    """Export the canonical YAML reproducing the running gateway's service registry.

    Returns clean YAML containing every service's connection, capabilities,
    models, priority, role, and lifecycle — everything an operator would commit
    to reproduce this gateway.  Ephemeral runtime state (health history,
    last_used_at, probed windows, live VRAM) is excluded.

    The output is round-trippable: it matches the shape the existing
    seed-import path already understands.

    Admin-auth required (same guard as all /admin/api/* routes).
    """
    try:
        ADMIN_AUTH(request)
    except AdminAuthError as exc:
        return Response(
            content=json.dumps({"error": {"code": exc.code, "message": exc.message}}),
            status_code=exc.status_code,
            media_type="application/json",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        yaml_text = _build_config_export_yaml()
        return Response(
            content=yaml_text,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="gateway-services.yml"',
            },
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "export_failed", "message": str(exc)}},
        )


# Endpoint paths excluded from the firehose per-key request counter.
# These are health/status probes hammered by the dashboard itself or
# routes that don't represent real "API caller" activity.
_FIREHOSE_NO_RECORD_PREFIXES = (
    "/health",
    "/admin",
    "/me/",
    "/v1/status",
    "/v1/audio/health",
    "/_next/",
    "/ui/",
    "/firehose",
    "/favicon.ico",
    "/static/",
    "/SKILL.md",
    "/robots.txt",
    "/.well-known/",
)
# Exact paths (not prefixes) that should be excluded too.
_FIREHOSE_NO_RECORD_EXACT = {"/", "/me", "/ui", "/llms.txt"}

# Map known successful-response paths to a synthesized event-type for the
# Recent activity timeline. We don't peek at request bodies — the path tells
# us enough ("chat completion happened") and the middleware already has the
# key name. Token counts are not visible here; we just stamp the path & status.
_FIREHOSE_EVENT_TYPES = {
    "/v1/chat/completions": "chat_completion",
    "/v1/completions": "chat_completion",
    "/v1/images/generations": "image_submit",
    "/v1/images/edits": "image_submit",
    "/v1/audio/speech": "tts_synthesize",
    "/v1/audio/transcriptions": "stt_transcribe",
    "/v1/audio/translations": "stt_translate",
}


@app.middleware("http")
async def _firehose_request_counter(request: Request, call_next):
    """Record per-API-key request activity for the firehose status panel.

    Identity is resolved best-effort by hashing the bearer token against
    admin.db App credentials. Body is never read; this only inspects path +
    status.
    """
    path = request.url.path
    skip = (
        path in _FIREHOSE_NO_RECORD_EXACT
        or any(path == p or path.startswith(p) for p in _FIREHOSE_NO_RECORD_PREFIXES)
    )
    t0 = time.monotonic()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        if not skip:
            try:
                key_name: Optional[str] = None
                if AUTH_ENABLED:
                    raw = get_client_api_key_from_headers(request.headers)
                    if raw:
                        ident = runtime_identity_for_app_credential_hash(
                            hash_api_key(raw),
                            inherited_profile_id_for_app=_inherited_profile_id_for_app,
                            path=ADMIN_STORE.path,
                        )
                        if ident:
                            key_name = ident.name
                latency_ms = (time.monotonic() - t0) * 1000.0
                status_code = getattr(response, "status_code", 0) or 0
                firehose_metrics.record_request(key_name, path, status_code, latency_ms)
                # Mirror into the SQLite time-series so the status page can
                # render historical request rates / latencies. Best-effort.
                metrics_sampler.record_request(
                    "request",
                    latency_ms,
                    key=key_name,
                    path=path,
                    status=status_code,
                )
                # Synthesize a timeline event for known content endpoints when
                # the response was a success. Skip videos here — capture for
                # those happens deeper in the handler with workflow/size/seconds
                # metadata that isn't visible at the middleware boundary.
                if status_code and status_code < 400:
                    evt_type = _FIREHOSE_EVENT_TYPES.get(path)
                    if evt_type:
                        # Pull a model id from the response header if the
                        # downstream handler stamped one (chat completions do).
                        model_hdr = (
                            response.headers.get("x-model-resolved")
                            if response is not None and hasattr(response, "headers")
                            else None
                        )
                        firehose_metrics.record_event(
                            evt_type,
                            f"{path} · {model_hdr or '—'}",
                            path=path,
                            status=status_code,
                            latency_ms=round(latency_ms, 2),
                            key=key_name or "anonymous",
                            model=model_hdr,
                        )
            except Exception:
                pass


@app.head("/", include_in_schema=False)
async def head_root():
    """HEAD / — for reverse-proxy health checks that probe the root path."""
    return Response(status_code=200)


@app.head("/admin", include_in_schema=False)
async def head_admin():
    """HEAD /admin — for reverse-proxy health checks that probe the admin path."""
    return Response(status_code=200)


@app.get("/health")
async def health():
    """Proxy service health. Public so external health checks do not need API keys."""
    return {
        "status": "healthy",
        "auth_enabled": AUTH_ENABLED,
        "app_credentials_configured": active_app_credential_count(),
    }


def _public_base_url(request: Request) -> str | None:
    """Best-effort public origin for absolute discovery links.

    Operators can pin the public origin via ``OPENAI_PROXY_PUBLIC_URL`` or
    a ``proxy.public_url`` config key. Otherwise we honor standard reverse
    proxy headers, then fall back to the request's host. We return
    ``None`` if no candidate is available so the discovery helper can
    emit relative links rather than fabricate an origin.
    """
    pinned = os.environ.get("OPENAI_PROXY_PUBLIC_URL")
    if not pinned:
        proxy_cfg = config.get("proxy") or {}
        pinned = proxy_cfg.get("public_url") or None
    if pinned:
        return pinned
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
    )
    if not host:
        return None
    scheme = (
        request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    )
    return f"{scheme}://{host}"


def _brand_settings() -> tuple[str, tuple[str, str] | None]:
    """Return ``(product_label, wordmark)`` for the shared header chrome.

    The deployment brand is data, not baked into product code. Operators set
    it via ``admin.brand`` (or ``AI_GATEWAY_BRAND`` / ``AI_GATEWAY_WORDMARK``):

      admin:
        brand:
          product: "AI Gateway"      # neutral product label (default)
          wordmark: "vivid.fish"     # optional deployment mark, "name.tld"

    A generic install with no wordmark shows only the neutral product mark, so
    nothing says "vivid.fish" unless this deployment opts in.
    """
    brand_cfg = admin_cfg.get("brand") if isinstance(admin_cfg, Mapping) else {}
    brand_cfg = brand_cfg if isinstance(brand_cfg, Mapping) else {}
    product = (
        os.environ.get("AI_GATEWAY_BRAND")
        or brand_cfg.get("product")
        or "AI Gateway"
    )
    raw_wordmark = (
        os.environ.get("AI_GATEWAY_WORDMARK")
        or brand_cfg.get("wordmark")
        or ""
    )
    wordmark: tuple[str, str] | None = None
    raw_wordmark = str(raw_wordmark).strip()
    if raw_wordmark and "." in raw_wordmark:
        name, _, tld = raw_wordmark.partition(".")
        name, tld = name.strip(), tld.strip()
        if name and tld:
            wordmark = (name, tld)
    return str(product), wordmark


@app.get("/.well-known/ai-gateway", include_in_schema=False)
async def well_known_ai_gateway(request: Request):
    """Unauthenticated agent-client discovery for this AI gateway."""
    return build_gateway_discovery_metadata(
        base_url=_public_base_url(request),
        registry=ADMIN_STORE.get_service_registry(),
    )


@app.get("/.well-known/openai-proxy", include_in_schema=False)
async def well_known_openai_proxy(request: Request):
    """Alias of /.well-known/ai-gateway for OpenAI-style SDK clients."""
    return build_gateway_discovery_metadata(
        base_url=_public_base_url(request),
        registry=ADMIN_STORE.get_service_registry(),
    )


@app.get("/.well-known/skills", include_in_schema=False)
async def well_known_skills(request: Request):
    """Public catalog of discoverable agent skills served by this proxy."""
    return build_skill_catalog(base_url=_public_base_url(request))


_SKILL_ROOT = Path(__file__).resolve().parent / "skills"


@app.get(
    "/.well-known/skills/{skill_id}/SKILL.md",
    include_in_schema=False,
)
async def well_known_skill_file(skill_id: str):
    """Serve the raw SKILL.md for a published agent skill."""
    # Tight allowlist: only flat ``skills/<id>/SKILL.md`` paths, no traversal.
    if "/" in skill_id or skill_id.startswith(".") or not skill_id:
        return Response(status_code=404)
    path = _SKILL_ROOT / skill_id / "SKILL.md"
    try:
        path = path.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return Response(status_code=404)
    if _SKILL_ROOT.resolve() not in path.parents:
        return Response(status_code=404)
    return FileResponse(path, media_type="text/markdown; charset=utf-8")


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt(request: Request):
    body = build_llms_txt(base_url=_public_base_url(request))
    return Response(content=body, media_type="text/plain; charset=utf-8")


@app.get("/openai/v1/voices", openapi_extra=VOICES_LIST_OPENAPI)
@app.get("/openai/v1/audio/voices", openapi_extra=VOICES_LIST_OPENAPI)
@app.get("/v1/voices", openapi_extra=VOICES_LIST_OPENAPI)
@app.get("/v1/audio/voices", openapi_extra=VOICES_LIST_OPENAPI)
async def list_voices(request: Request):
    """Return available TTS voices (OpenAI-compatible names)."""
    canonical_path = "/v1/audio/voices" if request.url.path.endswith("/audio/voices") else "/v1/voices"
    auth_context = authenticate_request(request, canonical_path)
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(auth_context, canonical_path)
    if policy_response is not None:
        return policy_response

    # Single source of truth: derive every voice the speech endpoint accepts.
    # The voice map lives in the voxtral-voices transform (gateway/transforms.py).
    # If you can pass it to /v1/audio/speech, you can find it here.
    _vmap = _voxtral_voice_map()
    voices = []
    for voice_id in sorted(_vmap.keys()):
        mapped = _vmap[voice_id]
        meta = _voice_metadata(voice_id, mapped)
        voices.append({
            "voice_id": voice_id,
            "name": meta["name"],
            "language": meta["language"],
            "gender": meta["gender"],
            "description": meta["description"],
            "mapped_to": mapped,
        })
    return {"voices": voices}


@app.post("/openai/v1/audio/speech", openapi_extra=AUDIO_SPEECH_OPENAPI, response_class=Response)
@app.post("/v1/audio/speech", openapi_extra=AUDIO_SPEECH_OPENAPI, response_class=Response)
async def audio_speech(request: Request):
    """OpenAI-compatible TTS endpoint - routes to TTS backend with voice mapping."""
    auth_context = authenticate_request(request, "/v1/audio/speech")
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(auth_context, "/v1/audio/speech")
    if policy_response is not None:
        return policy_response
    return await proxy_tts_request(request, auth_context)


@app.post("/openai/v1/audio/transcriptions", openapi_extra=AUDIO_TRANSCRIPTIONS_OPENAPI)
@app.post("/v1/audio/transcriptions", openapi_extra=AUDIO_TRANSCRIPTIONS_OPENAPI)
async def audio_transcriptions(request: Request):
    """OpenAI-compatible STT endpoint - routes to STT backend."""
    auth_context = authenticate_request(request, "/v1/audio/transcriptions")
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(
        auth_context, "/v1/audio/transcriptions"
    )
    if policy_response is not None:
        return policy_response
    return await proxy_stt_request(request, auth_context)


@app.post("/openai/v1/audio/translations", openapi_extra=AUDIO_TRANSLATIONS_OPENAPI)
@app.post("/v1/audio/translations", openapi_extra=AUDIO_TRANSLATIONS_OPENAPI)
async def audio_translations(request: Request):
    """OpenAI-compatible audio translation endpoint, if the STT backend supports it."""
    auth_context = authenticate_request(request, "/v1/audio/translations")
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(
        auth_context, "/v1/audio/translations"
    )
    if policy_response is not None:
        return policy_response
    return await proxy_stt_request(request, auth_context, operation="translations")


# Map image route paths to canonical intents for receipt construction.
_IMAGE_ROUTE_INTENTS = {
    "/v1/images/generations": "image.generate",
    "/v1/images/edits": "image.edit",
    "/v1/images/variations": "image.variation",
}


def start_image_route_receipt(
    path: str,
    auth_context: AuthContext,
) -> RouteReceipt:
    """Build an observational route receipt for an image endpoint after auth.

    The receipt records routing facts only: protocol, path, intent, key name,
    backend, and a placeholder policy decision rooted in the current proxy
    auth/profile model. It never reads the request body, so no prompts, file
    names, or uploaded bytes can leak into the receipt.
    """
    intent = _IMAGE_ROUTE_INTENTS.get(path, "image.generate")
    key_name = auth_context.identity.name if auth_context.identity else None
    app_id = auth_context.identity.app_id if auth_context.identity else None
    profile = _profile_for_auth_context(auth_context)
    receipt = RouteReceipt(
        protocol="openai.images",
        path=path,
        intent=intent,
        backend="comfyui",
        api_key_name=key_name,
        app_id=app_id,
        app_profile=profile.id,
        visibility=profile.status_visibility.level,
    )
    _apply_image_route_policy(receipt, auth_context)
    return receipt


def _apply_image_route_policy(
    receipt: RouteReceipt,
    auth_context: AuthContext,
    *,
    model: str | None = None,
    workflow: str | None = None,
    manifest: Mapping[str, Any] | None = None,
) -> PolicyDecision:
    metadata = _image_manifest_policy_metadata(manifest)
    return evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="openai.images",
            intent=receipt.intent,
            model=model,
            workflow=workflow,
            backend=receipt.backend,
            capability_exposure=metadata["exposure"],
            capability_trust_tier=metadata["trust_tier"],
            capability_resource_tier=metadata["resource_tier"],
            capability_tags=metadata["tags"],
        ),
        receipt,
    )


def _image_manifest_policy_metadata(
    manifest: Mapping[str, Any] | None,
) -> dict[str, object]:
    if manifest is None:
        return {
            "exposure": "default",
            "trust_tier": "stable",
            "resource_tier": "batch",
            "tags": (),
        }

    trusted = bool(manifest.get("trusted", False))
    tags_raw = manifest.get("tags")
    tags: list[str] = []
    if isinstance(tags_raw, list):
        tags = [tag for tag in tags_raw if isinstance(tag, str) and tag]
    return {
        "exposure": _manifest_string_field(manifest, "exposure", "default"),
        "trust_tier": _manifest_string_field(
            manifest, "trust_tier", "stable" if trusted else "experimental"
        ),
        "resource_tier": _manifest_string_field(
            manifest, "resource_tier", "batch"
        ),
        "tags": tuple(dict.fromkeys(tags)),
    }


def _manifest_string_field(
    manifest: Mapping[str, Any], key: str, default: str
) -> str:
    value = manifest.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _policy_workflow_for_receipt(receipt: RouteReceipt | None) -> str | None:
    if receipt is None:
        return None
    return receipt.manifest_id or receipt.resolved_workflow or receipt.requested_workflow


def _policy_denied_response(decision: PolicyDecision) -> Response:
    reason = decision.reasons[0] if decision.reasons else "request denied by app profile"
    return Response(
        content=json.dumps({
            "error": {
                "message": reason,
                "type": "permission_error",
                "code": "policy_denied",
            }
        }),
        status_code=403,
        media_type="application/json",
    )


_RUNTIME_ROUTE_POLICY: dict[str, dict[str, Any]] = route_registry.runtime_route_policy(
    registry=_PROTOCOL_ROUTE_REGISTRY,
    proxy_config=config,
)


_ASSET_CONTENT_ROUTE_POLICY_BY_KIND: dict[str, dict[str, Any]] = (
    route_registry.asset_content_route_policy_by_kind(
        registry=_PROTOCOL_ROUTE_REGISTRY,
        proxy_config=config,
    )
)


def _asset_content_route_policy(asset_id: str) -> dict[str, Any] | None:
    parsed = _parse_asset_id(asset_id)
    if parsed is None:
        return None
    kind, _ = parsed
    return _ASSET_CONTENT_ROUTE_POLICY_BY_KIND.get(kind)


def _runtime_route_policy_decision(
    auth_context: AuthContext,
    request_path: str,
    *,
    policy: Mapping[str, Any] | None = None,
) -> PolicyDecision | None:
    route_policy = policy or _RUNTIME_ROUTE_POLICY.get(request_path)
    if route_policy is None:
        return None
    return evaluate_and_apply(
        auth_context.identity,
        make_policy_request(**route_policy),
    )


def _enforce_runtime_route_policy(
    auth_context: AuthContext,
    request_path: str,
    *,
    policy: Mapping[str, Any] | None = None,
) -> Response | None:
    decision = _runtime_route_policy_decision(
        auth_context,
        request_path,
        policy=policy,
    )
    if decision is not None and decision.denied:
        return _policy_denied_response(decision)
    return None


_SAFE_IMAGE_INPUT_NAMES = ("model", "size", "n", "response_format", "background", "prompt", "image")
_MASK_ROUTE_MANAGED_MANIFEST_INPUTS = ("source_mask", "mask_asset", "mask")


@dataclass(frozen=True)
class ImageEditMaskContext:
    source_image: bytes
    edit_mask: bytes
    source_size: tuple[int, int]
    mask_mode: str


def _image_error_response(message: str, *, param: str, code: str) -> Response:
    return Response(
        content=json.dumps({
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": param,
                "code": code,
            }
        }),
        status_code=400,
        media_type="application/json",
    )


def _open_image_for_route(content: bytes, *, param: str):
    try:
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(content))
        image.load()
        return image, None
    except Exception:
        return None, _image_error_response(
            f"{param} must be a valid image file",
            param=param,
            code="invalid_image",
        )


def _edit_mask_from_image(mask_image) -> tuple[Any, str]:
    """Return an L mask where white means "take generated pixels"."""
    if "A" in mask_image.getbands():
        alpha = mask_image.getchannel("A")
        return alpha.point(lambda value: 255 - value), "alpha"
    return mask_image.convert("L"), "grayscale"


def _prepare_image_edit_mask_context(
    *,
    source_image_content: bytes,
    mask_content: bytes,
) -> tuple[ImageEditMaskContext | None, Response | None]:
    source_image, source_error = _open_image_for_route(
        source_image_content,
        param="image",
    )
    if source_error is not None:
        return None, source_error

    mask_image, mask_error = _open_image_for_route(mask_content, param="mask")
    if mask_error is not None:
        return None, mask_error

    if mask_image.size != source_image.size:
        return None, _image_error_response(
            "mask must have the same dimensions as image",
            param="mask",
            code="invalid_mask_dimensions",
        )

    edit_mask, mode = _edit_mask_from_image(mask_image)
    return (
        ImageEditMaskContext(
            source_image=source_image_content,
            edit_mask=_image_to_png_bytes(edit_mask),
            source_size=source_image.size,
            mask_mode=mode,
        ),
        None,
    )


def _image_to_png_bytes(image) -> bytes:
    import io

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _apply_image_edit_mask_composite(
    context: ImageEditMaskContext,
    generated_content: bytes,
) -> bytes:
    from PIL import Image
    import io

    generated = Image.open(io.BytesIO(generated_content)).convert("RGBA")
    generated.load()
    source = Image.open(io.BytesIO(context.source_image)).convert("RGBA")
    source.load()
    mask = Image.open(io.BytesIO(context.edit_mask)).convert("L")
    mask.load()

    if source.size != generated.size:
        source = source.resize(generated.size, Image.Resampling.LANCZOS)
    if mask.size != generated.size:
        mask = mask.resize(generated.size, Image.Resampling.LANCZOS)

    composited = Image.composite(generated, source, mask)
    return _image_to_png_bytes(composited)


def _offset_image_bytes(
    content: bytes,
    *,
    x_fraction: float = 0.5,
    y_fraction: float = 0.5,
) -> bytes:
    from PIL import Image, ImageChops
    import io

    image = Image.open(io.BytesIO(content)).convert("RGBA")
    image.load()
    x_offset = int(round(image.width * x_fraction))
    y_offset = int(round(image.height * y_fraction))
    return _image_to_png_bytes(ImageChops.offset(image, x_offset, y_offset))


def _make_seam_cross_mask_png(
    width: int,
    height: int,
    *,
    seam_width: int,
) -> bytes:
    from PIL import Image, ImageDraw

    safe_width = max(1, min(int(seam_width), max(1, min(width, height))))
    image = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(image)

    x0 = max(0, (width - safe_width) // 2)
    x1 = min(width - 1, x0 + safe_width - 1)
    y0 = max(0, (height - safe_width) // 2)
    y1 = min(height - 1, y0 + safe_width - 1)
    draw.rectangle((x0, 0, x1, height - 1), fill=255)
    draw.rectangle((0, y0, width - 1, y1), fill=255)
    return _image_to_png_bytes(image)


def _image_size_from_bytes(content: bytes) -> tuple[int, int]:
    from PIL import Image
    import io

    image = Image.open(io.BytesIO(content))
    image.load()
    return image.size


def _manifest_input_default(
    manifest: Mapping[str, Any] | None,
    name: str,
    fallback: Any,
) -> Any:
    if isinstance(manifest, Mapping):
        inputs = manifest.get("inputs")
        if isinstance(inputs, Mapping):
            spec = inputs.get(name)
            if isinstance(spec, Mapping) and "default" in spec:
                return spec["default"]
    return fallback


async def _upload_comfy_image_bytes(
    client: Any,
    headers: Mapping[str, str],
    content: bytes,
    *,
    filename: str,
    media_type: str = "image/png",
) -> tuple[str | None, Response | None]:
    files = {"image": (filename, content, media_type)}
    upload_resp = await client.post(
        f"{COMFY_HOST}/upload/image",
        headers=dict(headers),
        files=files,
    )
    if upload_resp.status_code != 200:
        return None, Response(
            content=json.dumps({
                "error": {
                    "message": f"Image upload failed: {upload_resp.text}",
                    "type": "api_error",
                }
            }),
            status_code=502,
            media_type="application/json",
        )

    uploaded_filename = upload_resp.json().get("name")
    if not uploaded_filename:
        return None, Response(
            content=json.dumps({
                "error": {
                    "message": "No filename returned from upload",
                    "type": "api_error",
                }
            }),
            status_code=502,
            media_type="application/json",
        )
    return str(uploaded_filename), None


def _gateway_image_generate_compose(manifest: Mapping[str, Any] | None) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    binding = (manifest.get("bindings") or {}).get("gateway")
    if not isinstance(binding, Mapping):
        return None
    compose = binding.get("compose")
    return str(compose) if isinstance(compose, str) and compose else None


def _gateway_image_model_id(record_manifest: Mapping[str, Any]) -> str | None:
    binding = (record_manifest.get("bindings") or {}).get("gateway")
    if not isinstance(binding, Mapping):
        return None
    model_id = binding.get("model_id")
    return str(model_id) if isinstance(model_id, str) and model_id else None


def _gateway_binding_value(
    manifest: Mapping[str, Any],
    name: str,
    *,
    fallback: str,
) -> str:
    binding = (manifest.get("bindings") or {}).get("gateway")
    if isinstance(binding, Mapping):
        value = binding.get(name)
        if isinstance(value, str) and value:
            return value
    return fallback


def _gateway_workflow_extension_values(
    *,
    body: Mapping[str, Any],
    manifest: Mapping[str, Any],
    receipt: RouteReceipt | None,
    standard_fields: frozenset[str],
) -> tuple[dict[str, Any], Response | None]:
    try:
        result = bind_workflow_extensions(
            body,
            manifest,
            "gateway",
            {},
            supported_names=_workflow_extension_supported_names(manifest, "gateway"),
            standard_fields=standard_fields,
        )
        if receipt is not None:
            record_image_extension_binding(receipt, result)
    except (ExtensionError, ManifestError, ValueError) as e:
        return {}, _image_extension_error_response(receipt, str(e))

    if result.unknown_top_level:
        name, reason = next(iter(result.unknown_top_level.items()))
        return {}, _image_extension_error_response(
            receipt,
            reason,
            param=name,
        )
    if result.validation.rejected:
        name, reason = next(iter(result.validation.rejected.items()))
        return {}, _image_extension_error_response(
            receipt,
            reason,
            param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{name}",
        )
    return dict(result.validation.accepted), None


def _seed_field_for_workflow(workflow: Mapping[str, Any], seed_node: str | None) -> str:
    if seed_node and seed_node in workflow:
        inputs = workflow[seed_node].get("inputs", {})
        if isinstance(inputs, Mapping) and "noise_seed" in inputs:
            return "noise_seed"
    return "seed"


def _set_workflow_seed(
    workflow: Mapping[str, Any],
    *,
    seed_node: str | None,
    seed_field: str,
    seed: int | None,
) -> None:
    if not seed_node or seed_node not in workflow:
        return
    inputs = workflow[seed_node].get("inputs", {})
    if not isinstance(inputs, dict):
        return
    if seed is None:
        seed = random.randint(0, 2**32 - 1)
    inputs[seed_field] = int(seed)


async def _run_single_comfy_image(
    *,
    client: httpx.AsyncClient,
    workflow: dict[str, Any],
    meta: Mapping[str, Any],
    headers: Mapping[str, str],
    label: str,
    seed: int | None = None,
) -> tuple[Response | None, bytes | None]:
    round_workflow = copy.deepcopy(workflow)
    apply_batch_size(round_workflow, dict(meta), 1)
    seed_node = meta.get("seed_node")
    seed_node_str = str(seed_node) if seed_node is not None else None
    _set_workflow_seed(
        round_workflow,
        seed_node=seed_node_str,
        seed_field=_seed_field_for_workflow(round_workflow, seed_node_str),
        seed=seed,
    )
    err, images = await _comfy_run_round(
        client,
        round_workflow,
        dict(headers),
        label,
    )
    if err is not None:
        return err, None
    if not images:
        return Response(
            content=json.dumps({
                "error": {
                    "message": "ComfyUI returned no images",
                    "type": "api_error",
                }
            }),
            status_code=502,
            media_type="application/json",
        ), None
    return None, images[0]


def _tile_seam_prompt(prompt: str, seam_prompt: Any) -> str:
    if isinstance(seam_prompt, str) and seam_prompt.strip():
        return seam_prompt.strip()
    return (
        f"{prompt}. Repair only the masked center seam cross so the texture "
        "wraps seamlessly on all edges. Preserve the source material, style, "
        "scale, lighting, and brush texture. Do not add borders, text, logos, "
        "objects, frames, or directional features."
    )


async def _proxy_offset_inpaint_tile_request(
    *,
    request: Request,
    body: Mapping[str, Any],
    auth_context: AuthContext,
    receipt: RouteReceipt | None,
    manifest: Mapping[str, Any] | None,
    prompt: str,
    model: str,
    size: str,
    n: int,
    response_format: str,
    headers: Mapping[str, str],
) -> Response:
    if manifest is None:
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"Unknown gateway image workflow: {model}",
                    "type": "invalid_request_error",
                }
            }),
            status_code=400,
            media_type="application/json",
        )

    if receipt is not None:
        receipt.resolved_model = model
        receipt.requested_workflow = model
        receipt.resolved_workflow = str(manifest.get("id") or model)
        identity = manifest_identity(dict(manifest))
        receipt.attach_manifest(
            manifest_id=identity.id,
            manifest_version=identity.version,
            manifest_hash=identity.manifest_hash,
        )
        decision = _apply_image_route_policy(
            receipt,
            auth_context,
            model=model,
            workflow=_policy_workflow_for_receipt(receipt),
            manifest=manifest,
        )
        if decision.denied:
            return _policy_denied_response(decision)

    controls, control_error = _gateway_workflow_extension_values(
        body=body,
        manifest=manifest,
        receipt=receipt,
        standard_fields=STANDARD_IMAGE_GENERATE_FIELDS,
    )
    if control_error is not None:
        return control_error

    base_model = _gateway_binding_value(
        manifest,
        "base_model",
        fallback="flux2-klein-4b",
    )
    inpaint_model = _gateway_binding_value(
        manifest,
        "inpaint_model",
        fallback="flux2-klein-4b-inpaint",
    )
    base_workflow = load_comfy_workflow(base_model)
    inpaint_workflow = load_comfy_workflow(inpaint_model)
    base_manifest = _find_image_manifest("image.generate", base_model)
    inpaint_manifest = _find_image_manifest("image.edit", inpaint_model)
    if not base_workflow or not inpaint_workflow or base_manifest is None or inpaint_manifest is None:
        if receipt is not None:
            receipt.add_fallback_reason("offset_inpaint_tile_prerequisite_missing")
        return Response(
            content=json.dumps({
                "error": {
                    "message": (
                        "offset-inpaint tile generation requires the Flux2 "
                        "base and inpaint workflows plus their manifests"
                    ),
                    "type": "api_error",
                }
            }),
            status_code=503,
            media_type="application/json",
        )

    width, height = parse_size(size)

    base_workflow = copy.deepcopy(base_workflow)
    base_meta = base_workflow.pop("_meta", {})
    base_prompt_node = base_meta.get("prompt_node")
    if base_prompt_node and base_prompt_node in base_workflow:
        base_workflow[base_prompt_node]["inputs"]["text"] = prompt
    base_size_node = base_meta.get("size_node")
    if base_size_node and base_size_node in base_workflow:
        base_workflow[base_size_node]["inputs"]["width"] = width
        base_workflow[base_size_node]["inputs"]["height"] = height
    base_workflow = _apply_manifest_route_inputs(
        base_manifest,
        base_workflow,
        {"prompt": prompt, "width": width, "height": height},
    )
    base_extension_inputs = {
        name: value
        for name, value in controls.items()
        if name in {"steps", "guidance", "seed"}
    }
    if base_extension_inputs:
        base_workflow = bind_backend_request(
            base_manifest,
            "comfyui",
            base_workflow,
            base_extension_inputs,
        ).request
    set_workflow_background_removal(base_workflow, base_meta, False)

    fixed_seed = controls.get("seed")
    fixed_seed_int = int(fixed_seed) if isinstance(fixed_seed, int) and not isinstance(fixed_seed, bool) else None
    seam_width = int(_manifest_input_default(manifest, "seam_mask_width", 96))
    if "seam_mask_width" in controls:
        seam_width = int(controls["seam_mask_width"])
    seam_expand = int(_manifest_input_default(manifest, "seam_mask_expand", 0))
    if "seam_mask_expand" in controls:
        seam_expand = int(controls["seam_mask_expand"])
    seam_feather = int(_manifest_input_default(manifest, "seam_mask_feather", 32))
    if "seam_mask_feather" in controls:
        seam_feather = int(controls["seam_mask_feather"])
    seam_steps = int(_manifest_input_default(manifest, "seam_steps", 8))
    if "seam_steps" in controls:
        seam_steps = int(controls["seam_steps"])
    seam_guidance = float(_manifest_input_default(manifest, "seam_guidance", 4.0))
    if "seam_guidance" in controls:
        seam_guidance = float(controls["seam_guidance"])
    seam_denoise = float(_manifest_input_default(manifest, "seam_denoise", 0.58))
    if "seam_denoise" in controls:
        seam_denoise = float(controls["seam_denoise"])
    seam_prompt = _tile_seam_prompt(prompt, controls.get("seam_prompt"))

    images: list[bytes] = []
    response_headers: dict[str, str] = {
        "X-Image-Seamless-Tile-Strategy": "offset-inpaint",
        "X-Image-Seam-Mask-Width": str(seam_width),
    }
    rounds = 0

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(COMFY_TIMEOUT, connect=10)) as client:
            for index in range(n):
                base_seed = (
                    fixed_seed_int + (index * 2)
                    if fixed_seed_int is not None
                    else None
                )
                seam_seed = base_seed + 1 if base_seed is not None else None
                err, base_image = await _run_single_comfy_image(
                    client=client,
                    workflow=base_workflow,
                    meta=base_meta,
                    headers=headers,
                    label=f"offset tile base {index + 1}/{n}",
                    seed=base_seed,
                )
                rounds += 1
                if err is not None:
                    return err
                assert base_image is not None

                offset_image = _offset_image_bytes(base_image)
                actual_width, actual_height = _image_size_from_bytes(offset_image)
                mask = _make_seam_cross_mask_png(
                    actual_width,
                    actual_height,
                    seam_width=seam_width,
                )

                uploaded_image, upload_error = await _upload_comfy_image_bytes(
                    client,
                    headers,
                    offset_image,
                    filename=f"offset-tile-{index + 1}.png",
                )
                if upload_error is not None:
                    return upload_error
                uploaded_mask, upload_error = await _upload_comfy_image_bytes(
                    client,
                    headers,
                    mask,
                    filename=f"offset-tile-mask-{index + 1}.png",
                )
                if upload_error is not None:
                    return upload_error
                assert uploaded_image is not None
                assert uploaded_mask is not None

                edit_workflow = copy.deepcopy(inpaint_workflow)
                edit_meta = edit_workflow.pop("_meta", {})
                set_workflow_background_removal(edit_workflow, edit_meta, False)
                edit_workflow = bind_backend_request(
                    inpaint_manifest,
                    "comfyui",
                    edit_workflow,
                    {
                        "prompt": seam_prompt,
                        "source_image": uploaded_image,
                        "mask": uploaded_mask,
                        "width": actual_width,
                        "height": actual_height,
                        "steps": seam_steps,
                        "guidance": seam_guidance,
                        "denoise": seam_denoise,
                        "mask_expand": seam_expand,
                        "mask_feather": seam_feather,
                        "seed": seam_seed if seam_seed is not None else random.randint(0, 2**32 - 1),
                    },
                ).request

                err, repaired_offset = await _run_single_comfy_image(
                    client=client,
                    workflow=edit_workflow,
                    meta=edit_meta,
                    headers=headers,
                    label=f"offset tile seam inpaint {index + 1}/{n}",
                    seed=seam_seed,
                )
                rounds += 1
                if err is not None:
                    return err
                assert repaired_offset is not None
                images.append(
                    _offset_image_bytes(
                        repaired_offset,
                        x_fraction=-0.5,
                        y_fraction=-0.5,
                    )
                )
    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": {"message": "ComfyUI request timeout", "type": "api_error"}}),
            status_code=504,
            media_type="application/json",
        )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": {"message": f"ComfyUI unavailable at {COMFY_HOST}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json",
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"Gateway tile composition error: {e}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json",
        )

    response_headers["X-Image-Rounds"] = str(rounds)
    response_headers["X-Image-Returned"] = str(len(images))
    return _build_image_response_payload(
        request,
        images,
        response_format,
        response_headers,
        auth_context=auth_context,
    )


def _manifest_route_managed_mask_input(manifest: Mapping[str, Any] | None) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    inputs = manifest.get("inputs")
    binding = (manifest.get("bindings") or {}).get("comfyui")
    mapping = binding.get("map") if isinstance(binding, Mapping) else None
    if not isinstance(inputs, Mapping) or not isinstance(mapping, Mapping):
        return None
    for name in _MASK_ROUTE_MANAGED_MANIFEST_INPUTS:
        if name in inputs and name in mapping:
            return name
    return None


def _manifest_required_route_mask_input(manifest: Mapping[str, Any] | None) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    inputs = manifest.get("inputs")
    mask_name = _manifest_route_managed_mask_input(manifest)
    if not mask_name or not isinstance(inputs, Mapping):
        return None
    spec = inputs.get(mask_name)
    if isinstance(spec, Mapping) and spec.get("required"):
        return mask_name
    return None


def _apply_manifest_route_inputs(
    manifest: Mapping[str, Any] | None,
    workflow: Mapping[str, Any],
    values: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply route-owned portable fields through a manifest when mapped.

    The image route owns OpenAI fields like ``size`` and turns them into
    portable ``width``/``height`` values. Some workflows need those values on
    more nodes than the historical hard-coded Flux ids, so reuse manifest
    bindings when they exist.
    """
    if not isinstance(manifest, Mapping):
        return dict(workflow)
    binding = (manifest.get("bindings") or {}).get("comfyui")
    mapping = binding.get("map") if isinstance(binding, Mapping) else None
    if not isinstance(mapping, Mapping):
        return dict(workflow)
    mapped_values = {
        name: value
        for name, value in values.items()
        if name in mapping
    }
    if not mapped_values:
        return dict(workflow)
    return bind_backend_request(
        dict(manifest),
        "comfyui",
        dict(workflow),
        mapped_values,
    ).request


def _prefer_inpaint_workflow_for_mask(model: str) -> str:
    if model.endswith("-inpaint"):
        return model
    base = model.removesuffix("-edit")
    candidate = f"{base}-inpaint"
    return candidate if load_comfy_workflow(candidate) else model


def _attach_image_manifest_if_available(
    receipt: RouteReceipt | None,
    intent: str,
    resolved_model: str,
) -> None:
    """Best-effort: attach manifest identity if a manifest file matches the
    intent/model. Never raises; records a warning when unavailable."""
    if receipt is None:
        return
    try:
        manifest = _find_image_manifest(intent, resolved_model)
        if manifest is None:
            if not _MANIFEST_WORKFLOWS_DIR.is_dir():
                receipt.add_warning("manifest_identity_deferred:no_manifests_dir")
            else:
                receipt.add_warning("manifest_identity_deferred:no_match")
            return
        identity = manifest_identity(manifest)
        receipt.attach_manifest(
            manifest_id=identity.id,
            manifest_version=identity.version,
            manifest_hash=identity.manifest_hash,
        )
    except Exception:
        # Manifest attach is observational; never break the request.
        try:
            receipt.add_warning("manifest_identity_deferred:error")
        except Exception:
            pass


def _find_image_manifest(intent: str, resolved_model: str) -> dict[str, Any] | None:
    candidate_ids = {
        f"{intent}.{resolved_model}",
        f"{intent}.{resolved_model.removesuffix('-edit')}",
    }
    for record in GATEWAY_WORKFLOW_REGISTRY.records():
        manifest = record.manifest
        mid = str(manifest.get("id") or "")
        if manifest.get("intent") != intent:
            continue
        if mid in candidate_ids or mid.endswith(f".{resolved_model}"):
            return copy.deepcopy(dict(manifest))
    return None


def _record_unknown_image_fields(
    receipt: RouteReceipt | None,
    body: Mapping[str, Any],
    standard_fields: frozenset[str],
    *,
    extra_allowed_fields: set[str] | None = None,
) -> Response | None:
    allowed_fields = set(standard_fields)
    if extra_allowed_fields:
        allowed_fields.update(extra_allowed_fields)
    unknown = detect_unknown_top_level_fields(body, allowed_fields)
    if not unknown:
        return None
    for name, reason in unknown.items():
        _record_image_input(receipt, name, rejected_reason=reason)
    first_name = next(iter(unknown))
    return Response(
        content=json.dumps({
            "error": {
                "message": unknown[first_name],
                "type": "invalid_request_error",
                "param": first_name,
                "code": "unknown_parameter",
            }
        }),
        status_code=400,
        media_type="application/json",
    )


def _extension_asset_field_references(body: Mapping[str, Any]) -> dict[str, str]:
    """Return ``{extension_input: multipart_field}`` references from a body."""
    extensions = body.get("extensions")
    if not isinstance(extensions, Mapping):
        return {}
    block = extensions.get(WORKFLOW_EXTENSION_NAMESPACE)
    if not isinstance(block, Mapping):
        return {}

    references: dict[str, str] = {}
    for input_name, value in block.items():
        field_name = _extension_asset_multipart_field(value)
        if field_name:
            references[str(input_name)] = field_name
    return references


def _allowed_image_extension_asset_fields(
    *,
    request_body: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
    receipt: RouteReceipt | None,
) -> tuple[set[str], Response | None]:
    """Return multipart transport fields that are valid for this workflow.

    OpenAI image edits are multipart. Gateway workflow controls still belong
    under ``extensions.com.vividfish.workflow``, but asset-valued controls such
    as a future ``control_image`` need a way to point at an additional file
    part without becoming top-level OpenAI parameters themselves.
    """
    references = _extension_asset_field_references(request_body)
    if not references:
        return set(), None

    if manifest is None:
        input_name = next(iter(references))
        return set(), _image_extension_error_response(
            receipt,
            "workflow extensions are not supported for the resolved image workflow",
            param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{input_name}",
        )

    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        return set(), _image_extension_error_response(
            receipt,
            "manifest must define an 'inputs' mapping",
        )

    supported = set(_workflow_extension_supported_names(manifest, "comfyui"))
    fields: set[str] = set()
    for input_name, field_name in references.items():
        spec = inputs.get(input_name)
        if input_name not in supported:
            return set(), _image_extension_error_response(
                receipt,
                f"{input_name} is not a supported asset extension input",
                param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{input_name}",
            )
        if not isinstance(spec, Mapping) or spec.get("type") != "asset":
            return set(), _image_extension_error_response(
                receipt,
                f"{input_name} is not an asset extension input",
                param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{input_name}",
            )
        if field_name in STANDARD_IMAGE_EDIT_FIELDS:
            return set(), _image_extension_error_response(
                receipt,
                f"multipart field {field_name!r} is reserved for OpenAI image edits",
                param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{input_name}",
            )
        fields.add(field_name)
    return fields, None


def _extension_asset_multipart_field(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    field = value.get("multipart_field") or value.get("form_field")
    if not isinstance(field, str):
        return None
    field = field.strip()
    return field or None


def _extension_asset_id(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    asset_id = value.get("asset_id")
    if not isinstance(asset_id, str):
        return None
    asset_id = asset_id.strip()
    return asset_id or None


def _request_without_top_level_fields(
    body: Mapping[str, Any],
    field_names: set[str],
) -> dict[str, Any]:
    if not field_names:
        return dict(body)
    return {
        str(key): value
        for key, value in body.items()
        if str(key) not in field_names
    }


def _workflow_extension_supported_names(manifest: Mapping[str, Any], backend: str) -> tuple[str, ...]:
    binding = (manifest.get("bindings") or {}).get(backend)
    if not isinstance(binding, Mapping):
        return ()
    mapping = binding.get("map")
    if not isinstance(mapping, Mapping):
        return ()
    # These are already handled by the OpenAI-shaped route itself. Keeping
    # them out of the extension supported set prevents manifest-required
    # portable fields from rejecting normal OpenAI requests.
    standard_portable_names = {
        "prompt",
        "source_image",
        "width",
        "height",
        *_MASK_ROUTE_MANAGED_MANIFEST_INPUTS,
    }
    return tuple(name for name in mapping.keys() if name not in standard_portable_names)


def _workflow_extensions_present(body: Mapping[str, Any]) -> bool:
    extensions = body.get("extensions")
    if not isinstance(extensions, Mapping):
        return extensions is not None
    return WORKFLOW_EXTENSION_NAMESPACE in extensions


def _workflow_extension_integer(body: Mapping[str, Any], name: str) -> Optional[int]:
    extensions = body.get("extensions")
    if not isinstance(extensions, Mapping):
        return None
    block = extensions.get(WORKFLOW_EXTENSION_NAMESPACE)
    if not isinstance(block, Mapping):
        return None
    value = block.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _image_extension_error_response(
    receipt: RouteReceipt | None,
    message: str,
    *,
    param: str = "extensions",
    status_code: int = 400,
) -> Response:
    _record_image_input(receipt, param, rejected_reason=message)
    return Response(
        content=json.dumps({
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": param,
                "code": "invalid_extension",
            }
        }),
        status_code=status_code,
        media_type="application/json",
    )


async def _upload_image_workflow_extension_assets(
    *,
    form: Any,
    request_body: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
    client: Any,
    headers: Mapping[str, str],
    receipt: RouteReceipt | None,
    auth_context: AuthContext | None = None,
) -> tuple[dict[str, Any], Response | None]:
    """Upload extension-referenced image assets and replace them with filenames.

    The extension value shape is:

    ``{"multipart_field": "control_image"}``

    The top-level multipart field is only transport. It is removed before
    extension validation so it never becomes a public OpenAI-compatible field.
    """
    updated = dict(request_body)
    if manifest is None:
        return updated, None

    extensions = request_body.get("extensions")
    if not isinstance(extensions, Mapping):
        return updated, None
    block = extensions.get(WORKFLOW_EXTENSION_NAMESPACE)
    if not isinstance(block, Mapping):
        return updated, None

    inputs = manifest.get("inputs")
    if not isinstance(inputs, Mapping):
        return updated, None

    supported = set(_workflow_extension_supported_names(manifest, "comfyui"))
    allowed_fields, allow_error = _allowed_image_extension_asset_fields(
        request_body=request_body,
        manifest=manifest,
        receipt=receipt,
    )
    if allow_error is not None:
        return updated, allow_error
    next_block = dict(block)
    uploaded_any = False
    for input_name, value in block.items():
        field_name = _extension_asset_multipart_field(value)
        asset_id = _extension_asset_id(value)
        if field_name is None and asset_id is None:
            continue

        input_key = str(input_name)
        spec = inputs.get(input_key)
        if field_name is not None and field_name not in allowed_fields:
            continue
        if input_key not in supported or not isinstance(spec, Mapping):
            return updated, _image_extension_error_response(
                receipt,
                f"{input_key} is not a supported asset extension input",
                param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{input_key}",
            )
        if spec.get("type") != "asset":
            return updated, _image_extension_error_response(
                receipt,
                f"{input_key} is not an asset extension input",
                param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{input_key}",
            )

        if asset_id is not None:
            try:
                content, filename = _resolve_cached_image_asset(
                    asset_id,
                    auth_context,
                )
            except _InputReferenceError as exc:
                return updated, _image_extension_error_response(
                    receipt,
                    str(exc),
                    param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{input_key}",
                )
            content_type = asset_media_type(asset_id, asset_cache_path(asset_id))
        else:
            upload = form.get(field_name)
            read = getattr(upload, "read", None)
            if upload is None or not callable(read):
                return updated, _image_extension_error_response(
                    receipt,
                    f"multipart field {field_name!r} is required for {input_key}",
                    param=f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}.{input_key}",
                )

            try:
                content = await read()
            except Exception as exc:  # noqa: BLE001
                return updated, _image_extension_error_response(
                    receipt,
                    f"could not read multipart field {field_name!r}: {exc}",
                    param=field_name,
                )

            filename = getattr(upload, "filename", None) or f"{field_name}.png"
            content_type = getattr(upload, "content_type", None) or "image/png"
        files = {"image": (filename, content, content_type)}
        upload_resp = await client.post(
            f"{COMFY_HOST}/upload/image",
            headers=dict(headers),
            files=files,
        )
        if upload_resp.status_code != 200:
            return updated, Response(
                content=json.dumps({
                    "error": {
                        "message": f"Image upload failed: {upload_resp.text}",
                        "type": "api_error",
                    }
                }),
                status_code=502,
                media_type="application/json",
            )

        uploaded_filename = upload_resp.json().get("name")
        if not uploaded_filename:
            return updated, Response(
                content=json.dumps({
                    "error": {
                        "message": "No filename returned from upload",
                        "type": "api_error",
                    }
                }),
                status_code=502,
                media_type="application/json",
            )

        next_block[input_key] = str(uploaded_filename)
        uploaded_any = True
        if field_name is not None:
            _record_image_input(receipt, field_name)

    if uploaded_any:
        next_extensions = dict(extensions)
        next_extensions[WORKFLOW_EXTENSION_NAMESPACE] = next_block
        updated["extensions"] = next_extensions
    return updated, None


async def _apply_workflow_extensions_to_image_request(
    *,
    request_body: Mapping[str, Any],
    manifest: Mapping[str, Any] | None,
    backend_request: Mapping[str, Any],
    standard_fields: frozenset[str],
    receipt: RouteReceipt | None,
    form: Any | None = None,
    client: Any | None = None,
    headers: Mapping[str, str] | None = None,
    extension_asset_fields: set[str] | None = None,
    auth_context: AuthContext | None = None,
) -> tuple[dict[str, Any] | None, Response | None]:
    asset_fields = extension_asset_fields or set()
    unknown_response = _record_unknown_image_fields(
        receipt,
        request_body,
        standard_fields,
        extra_allowed_fields=asset_fields,
    )
    if unknown_response is not None:
        return None, unknown_response

    extension_body = dict(request_body)
    if form is not None and client is not None:
        extension_body, upload_response = await _upload_image_workflow_extension_assets(
            form=form,
            request_body=request_body,
            manifest=manifest,
            client=client,
            headers=headers or {},
            receipt=receipt,
            auth_context=auth_context,
        )
        if upload_response is not None:
            return None, upload_response
    extension_body = _request_without_top_level_fields(extension_body, asset_fields)

    if manifest is None:
        if _workflow_extensions_present(extension_body):
            return None, _image_extension_error_response(
                receipt,
                "workflow extensions are not supported for the resolved image workflow",
            )
        return dict(backend_request), None

    try:
        result = bind_workflow_extensions(
            extension_body,
            manifest,
            "comfyui",
            backend_request,
            supported_names=_workflow_extension_supported_names(manifest, "comfyui"),
            standard_fields=standard_fields,
        )
        if receipt is not None:
            record_image_extension_binding(receipt, result)
    except (ExtensionError, ManifestError, ValueError) as e:
        return None, _image_extension_error_response(receipt, str(e))

    if result.unknown_top_level:
        name, reason = next(iter(result.unknown_top_level.items()))
        return None, _image_extension_error_response(receipt, reason, param=name)
    if result.validation.rejected:
        name, reason = next(iter(result.validation.rejected.items()))
        return None, _image_extension_error_response(
            receipt,
            reason,
            param=f"extensions.{result.namespace}.{name}",
        )
    return result.request or dict(backend_request), None


def _record_image_input(
    receipt: RouteReceipt | None,
    name: str,
    *,
    accepted: bool = True,
    rejected_reason: str | None = None,
    ignored_reason: str | None = None,
) -> None:
    if receipt is None:
        return
    try:
        if rejected_reason is not None:
            receipt.reject_input(name, rejected_reason)
        elif ignored_reason is not None:
            receipt.ignore_input(name, ignored_reason)
        elif accepted:
            receipt.accept_input(name)
    except Exception:
        pass


def _image_receipt_status(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "ok"
    if 400 <= status_code < 500:
        return "rejected"
    return "error"


def finish_image_route_receipt(
    receipt: RouteReceipt,
    response: Response,
) -> None:
    """Finish the receipt and emit a body-free firehose event."""
    status_code = int(getattr(response, "status_code", 0) or 0)
    receipt.finish(_image_receipt_status(status_code))
    try:
        receipt_store.record(receipt)
    except Exception as e:
        try:
            print(f"[proxy] receipt_store.record failed: {e}", flush=True)
        except Exception:
            pass
    try:
        firehose_metrics.record_event(
            "image_route_receipt",
            f"{receipt.intent} {receipt.status}",
            request_id=receipt.request_id,
            path=receipt.path,
            intent=receipt.intent,
            backend=receipt.backend,
            api_key_name=receipt.api_key_name,
            policy_decision=receipt.policy_decision,
            status=receipt.status,
            http_status=status_code,
            latency_ms=receipt.latency_ms,
            requested_model=receipt.requested_model,
            resolved_model=receipt.resolved_model,
            requested_workflow=receipt.requested_workflow,
            resolved_workflow=receipt.resolved_workflow,
            manifest_id=receipt.manifest_id,
            manifest_version=receipt.manifest_version,
            accepted_inputs=list(receipt.accepted_inputs),
            rejected_inputs=dict(receipt.rejected_inputs),
            ignored_inputs=dict(receipt.ignored_inputs),
            fallback_reasons=list(receipt.fallback_reasons),
            warnings=list(receipt.warnings),
        )
    except Exception:
        # Receipts are observational; never break the request path.
        pass


class _RequestUrlShim:
    def __init__(self, *, scheme: str, netloc: str) -> None:
        self.scheme = scheme
        self.netloc = netloc


class _JsonBodyRequestShim:
    """Small Request-shaped object for replaying JSON jobs off the HTTP path."""

    query_params: dict[str, str] = {}
    method = "POST"

    def __init__(
        self,
        *,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        scheme: str,
        netloc: str,
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        self._body = copy.deepcopy(dict(body))
        self.headers = dict(headers)
        self.url = _RequestUrlShim(scheme=scheme, netloc=netloc)
        self.scope = dict(scope or {})

    async def json(self) -> dict[str, Any]:
        return copy.deepcopy(self._body)


def _gateway_auth_context(request: Request, path: str) -> AuthContext | Response:
    auth_context = _auth_context_from_scope(request)
    if auth_context is not NO_AUTH_CONTEXT or not AUTH_ENABLED:
        return auth_context
    return authenticate_request(request, path)


def _safe_image_job_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name in ("host", "x-forwarded-host", "x-forwarded-proto"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


def _image_media_job_payload_path(job_id: str) -> Path:
    return IMAGE_MEDIA_JOB_PAYLOAD_DIR / f"{job_id}.json"


def _persist_image_media_job_payload(
    job_id: str,
    *,
    body: Mapping[str, Any],
    request: Request,
    auth_context: AuthContext,
) -> None:
    IMAGE_MEDIA_JOB_PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
    identity = auth_context.identity
    payload = {
        "schema_version": 1,
        "body": copy.deepcopy(dict(body)),
        "headers": _safe_image_job_headers(request),
        "url": {
            "scheme": request.url.scheme or "http",
            "netloc": getattr(request.url, "netloc", "") or request.headers.get("host", "127.0.0.1"),
        },
        "auth": {
            "name": identity.name if identity else None,
            "key_hash": identity.key_hash if identity else None,
        },
        "created_at": int(time.time()),
    }
    path = _image_media_job_payload_path(job_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(path)


def _load_image_media_job_payload(job_id: str) -> dict[str, Any] | None:
    path = _image_media_job_payload_path(job_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] image media job payload read failed for {job_id}: {exc}", flush=True)
        return None
    return data if isinstance(data, dict) else None


def _delete_image_media_job_payload(job_id: str) -> None:
    try:
        _image_media_job_payload_path(job_id).unlink(missing_ok=True)
    except Exception:
        pass


def _image_job_body_has_seed(body: Mapping[str, Any]) -> bool:
    if isinstance(body.get("seed"), int) and not isinstance(body.get("seed"), bool):
        return True
    extensions = body.get("extensions")
    if not isinstance(extensions, Mapping):
        return False
    block = extensions.get(WORKFLOW_EXTENSION_NAMESPACE)
    if not isinstance(block, Mapping):
        return False
    seed = block.get("seed")
    return isinstance(seed, int) and not isinstance(seed, bool)


def _image_job_request_digest(body: Mapping[str, Any], auth_context: AuthContext) -> str:
    owner = _asset_owner_key(auth_context) or "anonymous"
    canonical = json.dumps(
        {"owner": owner, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hmac.new(_image_signing_secret(), canonical, hashlib.sha256).hexdigest()


def _image_media_job_request_id(
    *,
    body: Mapping[str, Any],
    auth_context: AuthContext,
    idempotency_key: str | None,
    fallback_request_id: str,
) -> tuple[str, str, str]:
    owner = _asset_owner_key(auth_context) or "anonymous"
    if idempotency_key:
        canonical = json.dumps(
            {"owner": owner, "idempotency_key": idempotency_key},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hmac.new(_image_signing_secret(), canonical, hashlib.sha256).hexdigest()
        return f"imgjob_idem_{digest[:40]}", digest, "idempotency_key"
    if _image_job_body_has_seed(body):
        digest = _image_job_request_digest(body, auth_context)
        return f"imgjob_seeded_{digest[:40]}", digest, "seeded_request"
    digest = _image_job_request_digest(body, auth_context)
    return fallback_request_id, digest, "request"


def _safe_image_media_job_metadata(
    *,
    body: Mapping[str, Any],
    request_hash: str,
    idempotency: str,
) -> dict[str, Any]:
    try:
        n_value = int(body.get("n", 1))
    except (TypeError, ValueError):
        n_value = 1
    return {
        "mode": "async_image_generation",
        "request_hash": request_hash,
        "idempotency": idempotency,
        "requested_model": str(body.get("model", COMFY_DEFAULT_MODEL))[:160],
        "size": str(body.get("size", "1024x1024"))[:32],
        "n": max(1, min(n_value, 10)),
        "response_format": "url",
    }


def _image_media_job_client_payload(
    job: Mapping[str, Any],
    request: Request,
) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    client_job = {
        key: job[key]
        for key in media_jobs.CLIENT_VISIBLE_FIELDS
        if key in job
    }
    return {
        "object": "media.job",
        "id": job_id,
        **client_job,
        "poll_url": f"/gateway/v1/media/jobs/{job_id}",
        "content_url": f"/gateway/v1/media/jobs/{job_id}/content",
    }


def _image_media_job_response(
    job: Mapping[str, Any],
    request: Request,
    *,
    status_code: int | None = None,
) -> Response:
    status_value = str(job.get("status") or "")
    if status_code is None:
        status_code = 200 if status_value in media_jobs.TERMINAL_STATUSES else 202
    job_id = str(job.get("job_id") or "")
    return Response(
        content=json.dumps(_image_media_job_client_payload(job, request)),
        status_code=status_code,
        media_type="application/json",
        headers={"Location": f"/gateway/v1/media/jobs/{job_id}"},
    )


def _image_response_json(response: Response) -> dict[str, Any]:
    body = getattr(response, "body", b"")
    if isinstance(body, str):
        raw = body
    else:
        raw = bytes(body or b"").decode("utf-8", errors="replace")
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _image_response_error(response: Response) -> tuple[str, str | None]:
    parsed = _image_response_json(response)
    error = parsed.get("error")
    if isinstance(error, Mapping):
        message = str(error.get("message") or "image generation failed")
        code = error.get("code")
        return message[:500], str(code) if code else None
    return f"image generation failed with HTTP {response.status_code}", None


def _retry_after_seconds(response: Response) -> float | None:
    raw = response.headers.get("retry-after") if hasattr(response, "headers") else None
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return min(value, IMAGE_MEDIA_JOB_RETRY_MAX_SECONDS)


def _image_media_job_retry_decision(
    response: Response,
    *,
    attempt: int,
    max_attempts: int = IMAGE_MEDIA_JOB_MAX_ATTEMPTS,
) -> tuple[str, float] | None:
    if attempt >= max_attempts:
        return None
    status_code = int(getattr(response, "status_code", 0) or 0)
    message, code = _image_response_error(response)
    text = message.lower()
    reason: str | None = None
    if status_code == 503 and (code == "vram_exhausted" or "vram" in text):
        reason = "vram_exhausted"
    elif status_code == 502 and "comfyui unavailable" in text:
        reason = "comfyui_unavailable"
    elif status_code == 502 and "gateway tile composition error" in text:
        reason = "tile_composition_error"
    elif status_code == 502 and "no prompt_id returned" in text:
        reason = "missing_prompt_id"
    elif status_code == 504:
        reason = "comfyui_timeout"
    elif status_code == 503 and "unavailable" in text:
        reason = "service_unavailable"

    if reason is None:
        return None
    retry_after = _retry_after_seconds(response)
    if retry_after is not None:
        return reason, retry_after
    backoff = IMAGE_MEDIA_JOB_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1))
    return reason, min(backoff, IMAGE_MEDIA_JOB_RETRY_MAX_SECONDS)


def _assets_from_image_response(response: Response) -> list[dict[str, Any]]:
    parsed = _image_response_json(response)
    data = parsed.get("data")
    if not isinstance(data, list):
        return []
    assets: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        asset: dict[str, Any] = {}
        for key in ("asset_id", "asset_url", "url", "media_type"):
            value = item.get(key)
            if isinstance(value, str) and value:
                asset[key] = value
        if asset.get("asset_id"):
            assets.append(asset)
    return assets


def _openai_image_payload_from_media_job(job: Mapping[str, Any]) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    for asset in job.get("assets") or []:
        if not isinstance(asset, Mapping):
            continue
        item = {
            key: asset[key]
            for key in ("asset_id", "asset_url", "url", "media_type")
            if isinstance(asset.get(key), str) and asset.get(key)
        }
        if item:
            data.append(item)
    return {
        "created": int(job.get("updated_at") or time.time()),
        "job_id": job.get("job_id"),
        "data": data,
    }


def _media_job_asset_items(
    job: Mapping[str, Any],
    request: Request,
    *,
    kind: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for asset in job.get("assets") or []:
        if not isinstance(asset, Mapping):
            continue
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str):
            continue
        parsed = _parse_asset_id(asset_id)
        if parsed is None or parsed[0] != kind:
            continue
        signed_url = build_signed_asset_url(request, asset_id)
        item = {
            key: asset[key]
            for key in ("asset_id", "media_type", "size_bytes", "index")
            if asset.get(key) is not None
        }
        item["url"] = signed_url
        item["asset_url"] = signed_url
        item["content_url"] = signed_url
        items.append(item)
    return items


def _openai_audio_payload_from_media_job(
    job: Mapping[str, Any],
    request: Request,
) -> dict[str, Any]:
    return {
        "object": "audio.generation",
        "created": int(job.get("updated_at") or time.time()),
        "job_id": job.get("job_id"),
        "data": _media_job_asset_items(job, request, kind="aud"),
    }


def _openai_video_payload_from_media_job(
    job: Mapping[str, Any],
    request: Request,
) -> dict[str, Any]:
    return {
        "object": "video.generation",
        "created": int(job.get("updated_at") or time.time()),
        "job_id": job.get("job_id"),
        "data": _media_job_asset_items(job, request, kind="vid"),
    }


def _media_job_content_route_policy(job: Mapping[str, Any]) -> dict[str, Any] | None:
    intent = str(job.get("intent") or "")
    if intent == "image.generate":
        return _RUNTIME_ROUTE_POLICY["/gateway/v1/media/jobs/{job_id}/content"]
    if intent in _AUDIO_MEDIA_JOB_INTENTS:
        return _ASSET_CONTENT_ROUTE_POLICY_BY_KIND.get("aud")
    if intent == INTENT_VIDEO_GENERATE:
        return _ASSET_CONTENT_ROUTE_POLICY_BY_KIND.get("vid")
    return None


def _media_job_content_preflight_response(auth_context: AuthContext) -> Response | None:
    media_protocols = ("openai.images", "openai.audio", "openai.video")
    decisions = [
        evaluate_and_apply(
            auth_context.identity,
            make_policy_request(protocol=protocol),
        )
        for protocol in media_protocols
    ]
    if any(decision.allowed for decision in decisions):
        return None

    profile = _profile_for_auth_context(auth_context)
    return _policy_denied_response(
        PolicyDecision(
            decision=DENIED,
            sources=(f"profile:{profile.id}", "policy:media-job-content"),
            reasons=("profile does not permit media job content",),
            visibility_level=profile.status_visibility.level,
        )
    )


def _auth_context_from_image_job_payload(payload: Mapping[str, Any]) -> AuthContext:
    auth = payload.get("auth")
    if not isinstance(auth, Mapping):
        return NO_AUTH_CONTEXT
    key_hash = auth.get("key_hash")
    if not isinstance(key_hash, str) or not key_hash:
        return NO_AUTH_CONTEXT
    identity = runtime_identity_for_app_credential_hash(
        key_hash,
        inherited_profile_id_for_app=_inherited_profile_id_for_app,
        path=ADMIN_STORE.path,
    )
    if identity is None:
        return NO_AUTH_CONTEXT
    return AuthContext(identity=identity)


def _request_shim_from_image_job_payload(payload: Mapping[str, Any]) -> _JsonBodyRequestShim | None:
    body = payload.get("body")
    if not isinstance(body, Mapping):
        return None
    url = payload.get("url")
    headers = payload.get("headers")
    if not isinstance(url, Mapping):
        url = {}
    if not isinstance(headers, Mapping):
        headers = {}
    replay_body = copy.deepcopy(dict(body))
    replay_body["response_format"] = "url"
    return _JsonBodyRequestShim(
        body=replay_body,
        headers={str(k): str(v) for k, v in headers.items()},
        scheme=str(url.get("scheme") or "http"),
        netloc=str(url.get("netloc") or headers.get("host") or "127.0.0.1"),
    )


def _mark_image_media_job_failed(
    job_id: str,
    message: str,
    *,
    http_status: int | None = None,
) -> None:
    metadata_patch: dict[str, Any] = {}
    if http_status is not None:
        metadata_patch["last_http_status"] = int(http_status)
    try:
        media_jobs.update_status(
            job_id,
            media_jobs.STATUS_FAILED,
            error=message[:500],
            metadata_patch=metadata_patch or None,
            path=media_jobs.DB_PATH,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] image media job fail update failed for {job_id}: {exc}", flush=True)
    try:
        media_jobs.append_event(
            job_id,
            "failed",
            {
                "phase": "failed",
                "status": media_jobs.STATUS_FAILED,
                "progress": 100,
                "http_status": http_status or 0,
            },
            path=media_jobs.DB_PATH,
        )
    except Exception:
        pass


async def _run_image_media_job(job_id: str) -> None:
    async with _image_media_job_semaphore:
        job = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
        if not job:
            return
        if job.get("status") in media_jobs.TERMINAL_STATUSES:
            return

        payload = _load_image_media_job_payload(job_id)
        request_shim = _request_shim_from_image_job_payload(payload or {})
        if payload is None or request_shim is None:
            _mark_image_media_job_failed(job_id, "Image media job payload is missing or invalid")
            return

        auth_context = _auth_context_from_image_job_payload(payload)
        try:
            media_jobs.update_status(
                job_id,
                media_jobs.STATUS_RUNNING,
                progress=5,
                metadata_patch={"started_at": int(time.time())},
                path=media_jobs.DB_PATH,
            )
            media_jobs.append_event(
                job_id,
                "started",
                {
                    "phase": "running",
                    "status": media_jobs.STATUS_RUNNING,
                    "progress": 5,
                },
                path=media_jobs.DB_PATH,
            )
        except ValueError:
            return

        max_attempts = max(1, IMAGE_MEDIA_JOB_MAX_ATTEMPTS)
        for attempt in range(1, max_attempts + 1):
            receipt = start_image_route_receipt("/v1/images/generations", auth_context)
            try:
                media_jobs.append_event(
                    job_id,
                    "attempt",
                    {
                        "phase": "running",
                        "status": media_jobs.STATUS_RUNNING,
                        "progress": min(95, 5 + (attempt - 1) * 10),
                        "attempt": attempt,
                        "total": max_attempts,
                    },
                    path=media_jobs.DB_PATH,
                )
                response = await proxy_comfy_request(
                    request_shim,
                    auth_context,
                    receipt=receipt,
                )
                finish_image_route_receipt(receipt, response)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                finish_image_route_receipt(
                    receipt,
                    Response(
                        content=json.dumps({
                            "error": {
                                "message": f"image media job executor error: {type(exc).__name__}",
                                "type": "api_error",
                            }
                        }),
                        status_code=502,
                        media_type="application/json",
                    ),
                )
                if attempt < max_attempts:
                    delay = min(
                        IMAGE_MEDIA_JOB_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)),
                        IMAGE_MEDIA_JOB_RETRY_MAX_SECONDS,
                    )
                    media_jobs.append_event(
                        job_id,
                        "retry",
                        {
                            "phase": "retrying",
                            "status": media_jobs.STATUS_RUNNING,
                            "progress": min(95, 10 + attempt * 10),
                            "attempt": attempt,
                            "total": max_attempts,
                            "reason": type(exc).__name__,
                            "retry_after_s": delay,
                        },
                        path=media_jobs.DB_PATH,
                    )
                    await asyncio.sleep(delay)
                    continue
                _mark_image_media_job_failed(
                    job_id,
                    f"Image media job executor error: {type(exc).__name__}",
                    http_status=502,
                )
                return

            status_code = int(getattr(response, "status_code", 0) or 0)
            if 200 <= status_code < 300:
                assets = _assets_from_image_response(response)
                if not assets:
                    _mark_image_media_job_failed(
                        job_id,
                        "Image generation succeeded but returned no reusable assets",
                        http_status=status_code,
                    )
                    return
                media_jobs.update_status(
                    job_id,
                    media_jobs.STATUS_SUCCEEDED,
                    progress=100,
                    add_assets=assets,
                    metadata_patch={
                        "attempts": attempt,
                        "last_http_status": status_code,
                        "completed_at": int(time.time()),
                    },
                    path=media_jobs.DB_PATH,
                )
                media_jobs.append_event(
                    job_id,
                    "succeeded",
                    {
                        "phase": "complete",
                        "status": media_jobs.STATUS_SUCCEEDED,
                        "progress": 100,
                        "attempt": attempt,
                        "total": max_attempts,
                    },
                    path=media_jobs.DB_PATH,
                )
                _delete_image_media_job_payload(job_id)
                return

            retry = _image_media_job_retry_decision(
                response,
                attempt=attempt,
                max_attempts=max_attempts,
            )
            if retry is not None:
                reason, delay = retry
                media_jobs.update_status(
                    job_id,
                    media_jobs.STATUS_RUNNING,
                    progress=min(95, 10 + attempt * 10),
                    metadata_patch={
                        "attempts": attempt,
                        "last_http_status": status_code,
                        "last_retry_reason": reason,
                    },
                    path=media_jobs.DB_PATH,
                )
                media_jobs.append_event(
                    job_id,
                    "retry",
                    {
                        "phase": "retrying",
                        "status": media_jobs.STATUS_RUNNING,
                        "progress": min(95, 10 + attempt * 10),
                        "attempt": attempt,
                        "total": max_attempts,
                        "http_status": status_code,
                        "reason": reason,
                        "retry_after_s": delay,
                    },
                    path=media_jobs.DB_PATH,
                )
                await asyncio.sleep(delay)
                continue

            message, _code = _image_response_error(response)
            _mark_image_media_job_failed(
                job_id,
                message,
                http_status=status_code,
            )
            return


def _schedule_image_media_job(job_id: str) -> bool:
    if job_id in _image_media_job_scheduled:
        return False
    _image_media_job_scheduled.add(job_id)
    task = asyncio.create_task(_run_image_media_job(job_id))
    _image_media_job_tasks.add(task)

    def _done(done_task: asyncio.Task) -> None:
        _image_media_job_tasks.discard(done_task)
        _image_media_job_scheduled.discard(job_id)
        try:
            exc = done_task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            print(f"[proxy] image media job task crashed for {job_id}: {exc}", flush=True)

    task.add_done_callback(_done)
    return True


def _resume_pending_image_media_jobs() -> int:
    resumed = 0
    for status_value in (media_jobs.STATUS_RUNNING, media_jobs.STATUS_QUEUED):
        try:
            rows = media_jobs.recent(
                limit=200,
                audience="internal",
                status=status_value,
                path=media_jobs.DB_PATH,
            )
        except Exception:
            continue
        for job in rows:
            if job.get("intent") != "image.generate":
                continue
            metadata = job.get("metadata") or {}
            if not isinstance(metadata, Mapping) or metadata.get("mode") != "async_image_generation":
                continue
            job_id = str(job.get("job_id") or "")
            if not job_id:
                continue
            if not _image_media_job_payload_path(job_id).exists():
                _mark_image_media_job_failed(
                    job_id,
                    "Image media job payload missing after proxy restart",
                )
                continue
            if _schedule_image_media_job(job_id):
                resumed += 1
    return resumed


def _image_generation_body_from_media_job_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if payload.get("intent") != "image.generate":
        return None
    request_body = payload.get("request")
    if not isinstance(request_body, Mapping):
        request_body = payload.get("parameters")
    if isinstance(request_body, Mapping):
        return dict(request_body)
    if "prompt" not in payload:
        return None
    return {
        str(key): value
        for key, value in payload.items()
        if str(key) in STANDARD_IMAGE_GENERATE_FIELDS
    }


_MEDIA_JOB_QUEUE_KEY = "comfyui-media"
_MEDIA_JOB_SUPPORTED_INTENTS = frozenset({
    "image.generate",
    "audio.generate",
    "music.generate",
    "sfx.generate",
    INTENT_VIDEO_GENERATE,
})
_MEDIA_JOB_RESOURCE_CLASS_BY_INTENT = {
    "image.generate": "image",
    "audio.generate": "audio",
    "music.generate": "audio",
    "sfx.generate": "audio",
    INTENT_VIDEO_GENERATE: "video",
}


def _optional_int_config(mapping: Mapping[str, Any], key: str) -> int | None:
    value = mapping.get(key)
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        print(f"[proxy] ignoring non-integer {key} in media job admission config", flush=True)
        return None
    return max(0, parsed)


def _string_tuple_config(mapping: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = mapping.get(key)
    if value is None:
        return ()
    if isinstance(value, (str, bytes)):
        items = [str(value)]
    elif isinstance(value, Mapping):
        return ()
    elif isinstance(value, Iterable):
        items = [str(item) for item in value]
    else:
        return ()
    return tuple(item.strip() for item in items if item.strip())


def _media_job_queue_depth(
    max_queue_depth: int | None = None,
    *,
    admission_cfg: Mapping[str, Any] | None = None,
) -> int:
    cfg = admission_cfg if isinstance(admission_cfg, Mapping) else MEDIA_JOB_ADMISSION_CFG
    configured_scan_limit = _optional_int_config(cfg, "scan_limit") or 1000
    scan_limit = max(configured_scan_limit, max_queue_depth or 0)
    depth = 0
    for status_value in (media_jobs.STATUS_QUEUED, media_jobs.STATUS_RUNNING):
        try:
            rows = media_jobs.recent(
                limit=scan_limit,
                audience="internal",
                status=status_value,
                path=media_jobs.DB_PATH,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[proxy] media job admission load read failed: {exc}", flush=True)
            continue
        depth += sum(
            1
            for row in rows
            if str(row.get("intent") or "") in _MEDIA_JOB_SUPPORTED_INTENTS
        )
    return depth


def _media_job_admission_response(intent: str) -> Response | None:
    admission_cfg = _effective_media_job_admission_cfg()
    service_override = _operator_service_availability_overrides().get("comfyui", {})
    override_state = str(service_override.get("state") or "available")
    service_blocked = override_state in {"disabled", "maintenance", "draining"}
    service_degraded = override_state == "degraded"
    resource_class = _MEDIA_JOB_RESOURCE_CLASS_BY_INTENT.get(intent, "media")
    service_reasons_list: list[str] = []
    if not COMFY_ENABLED:
        service_reasons_list.append("ComfyUI media backend disabled")
    if service_blocked or service_degraded:
        reason = str(service_override.get("reason") or "").strip()
        service_reasons_list.append(reason or f"ComfyUI operator mode: {override_state}")
    max_queue_depth = _optional_int_config(admission_cfg, "max_queue_depth")
    service_retry_after = (
        _optional_int_config(service_override, "retry_after_seconds")
        if isinstance(service_override, Mapping)
        else None
    )
    decision = resource_admission_http.advise_route_admission(
        intent=intent,
        service_available=COMFY_ENABLED and not service_blocked,
        service_degraded=service_degraded,
        service_retry_after=service_retry_after
        or (30 if (not COMFY_ENABLED or service_blocked) else None),
        service_reasons=tuple(service_reasons_list),
        resource_class=resource_class,
        queue_key=_MEDIA_JOB_QUEUE_KEY,
        requires_gpu=True,
        disabled_intents=_string_tuple_config(admission_cfg, "disabled_intents"),
        disabled_resource_classes=_string_tuple_config(
            admission_cfg,
            "disabled_resource_classes",
        ),
        reserved_tokens=_string_tuple_config(admission_cfg, "reserved_tokens"),
        max_queue_depth=max_queue_depth,
        constraint_retry_after=_optional_int_config(
            admission_cfg,
            "retry_after_seconds",
        ),
        queue_depths={
            _MEDIA_JOB_QUEUE_KEY: _media_job_queue_depth(
                max_queue_depth,
                admission_cfg=admission_cfg,
            )
        },
        load_retry_after=_optional_int_config(
            admission_cfg,
            "queue_retry_after_seconds",
        ) or 30,
    )
    if decision.allowed:
        return None
    advised = resource_admission_http.admission_http_response(decision)
    return Response(
        content=json.dumps(advised.body),
        status_code=advised.status_code,
        media_type="application/json",
        headers=dict(advised.headers),
    )


_AUDIO_MEDIA_JOB_INTENTS = frozenset({"audio.generate", "music.generate", "sfx.generate"})
_audio_media_job_tasks: set[asyncio.Task] = set()
_audio_media_job_scheduled: set[str] = set()
_audio_media_job_semaphore = asyncio.Semaphore(IMAGE_MEDIA_JOB_CONCURRENCY)


def _audio_media_job_policy() -> dict[str, Any]:
    return _ASSET_CONTENT_ROUTE_POLICY_BY_KIND["aud"]


def _audio_generation_body_from_media_job_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    intent = payload.get("intent")
    if intent not in _AUDIO_MEDIA_JOB_INTENTS:
        return None
    request_body = payload.get("request")
    if not isinstance(request_body, Mapping):
        request_body = payload.get("parameters")
    return dict(request_body) if isinstance(request_body, Mapping) else None


def _persist_audio_media_job_payload(
    job_id: str,
    *,
    body: Mapping[str, Any],
    request: Request,
    auth_context: AuthContext,
) -> None:
    _persist_image_media_job_payload(
        job_id,
        body=body,
        request=request,
        auth_context=auth_context,
    )


def _load_audio_media_job_payload(job_id: str) -> dict[str, Any] | None:
    return _load_image_media_job_payload(job_id)


def _delete_audio_media_job_payload(job_id: str) -> None:
    _delete_image_media_job_payload(job_id)


def _request_shim_from_audio_job_payload(payload: Mapping[str, Any]) -> _JsonBodyRequestShim | None:
    body = payload.get("body")
    if not isinstance(body, Mapping):
        return None
    url = payload.get("url")
    headers = payload.get("headers")
    if not isinstance(url, Mapping):
        url = {}
    if not isinstance(headers, Mapping):
        headers = {}
    return _JsonBodyRequestShim(
        body=copy.deepcopy(dict(body)),
        headers={str(k): str(v) for k, v in headers.items()},
        scheme=str(url.get("scheme") or "http"),
        netloc=str(url.get("netloc") or headers.get("host") or "127.0.0.1"),
    )


def _mark_audio_media_job_failed(
    job_id: str,
    message: str,
    *,
    http_status: int | None = None,
) -> None:
    metadata_patch: dict[str, Any] = {}
    if http_status is not None:
        metadata_patch["last_http_status"] = int(http_status)
    try:
        media_jobs.update_status(
            job_id,
            media_jobs.STATUS_FAILED,
            error=message[:500],
            metadata_patch=metadata_patch or None,
            path=media_jobs.DB_PATH,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] audio media job fail update failed for {job_id}: {exc}", flush=True)
    try:
        media_jobs.append_event(
            job_id,
            "failed",
            {
                "phase": "failed",
                "status": media_jobs.STATUS_FAILED,
                "progress": 100,
                "http_status": http_status or 0,
            },
            path=media_jobs.DB_PATH,
        )
    except Exception:
        pass


async def _run_audio_media_job(job_id: str) -> None:
    async with _audio_media_job_semaphore:
        job = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
        if not job or job.get("status") in media_jobs.TERMINAL_STATUSES:
            return

        payload = _load_audio_media_job_payload(job_id)
        request_shim = _request_shim_from_audio_job_payload(payload or {})
        if payload is None or request_shim is None:
            _mark_audio_media_job_failed(job_id, "Audio media job payload is missing or invalid")
            return

        auth_context = _auth_context_from_image_job_payload(payload)
        try:
            media_jobs.update_status(
                job_id,
                media_jobs.STATUS_RUNNING,
                progress=5,
                metadata_patch={"started_at": int(time.time())},
                path=media_jobs.DB_PATH,
            )
            media_jobs.append_event(
                job_id,
                "started",
                {
                    "phase": "running",
                    "status": media_jobs.STATUS_RUNNING,
                    "progress": 5,
                },
                path=media_jobs.DB_PATH,
            )
            response = await proxy_audio_generate(request_shim, auth_context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _mark_audio_media_job_failed(
                job_id,
                f"Audio media job executor error: {type(exc).__name__}",
                http_status=502,
            )
            return

        status_code = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status_code < 300:
            message, _code = _image_response_error(response)
            _mark_audio_media_job_failed(job_id, message, http_status=status_code)
            return

        content = bytes(getattr(response, "body", b"") or b"")
        media_type = str(getattr(response, "media_type", None) or "audio/wav")
        body = payload.get("body") if isinstance(payload, Mapping) else {}
        response_format = body.get("response_format") if isinstance(body, Mapping) else None
        artifacts = audio_media_jobs.audio_artifacts_from_result(
            content,
            response_format=str(response_format) if response_format else None,
            media_type=media_type,
        )
        assets = audio_media_jobs.store_audio_artifacts(
            artifacts,
            store_asset=store_asset_bytes,
            owner_key_hash=_asset_owner_key(auth_context),
        )
        if not assets:
            _mark_audio_media_job_failed(
                job_id,
                "Audio generation succeeded but returned no reusable assets",
                http_status=status_code,
            )
            return

        media_jobs.update_status(
            job_id,
            media_jobs.STATUS_SUCCEEDED,
            progress=100,
            add_assets=assets,
            metadata_patch={"completed_at": int(time.time())},
            path=media_jobs.DB_PATH,
        )
        media_jobs.append_event(
            job_id,
            "completed",
            {
                **audio_media_jobs.complete_audio_job_payload(assets),
                "status": media_jobs.STATUS_SUCCEEDED,
                "progress": 100,
            },
            path=media_jobs.DB_PATH,
        )
        _delete_audio_media_job_payload(job_id)


def _schedule_audio_media_job(job_id: str) -> bool:
    if job_id in _audio_media_job_scheduled:
        return False
    _audio_media_job_scheduled.add(job_id)
    task = asyncio.create_task(_run_audio_media_job(job_id))
    _audio_media_job_tasks.add(task)

    def _done(done_task: asyncio.Task) -> None:
        _audio_media_job_tasks.discard(done_task)
        _audio_media_job_scheduled.discard(job_id)
        try:
            exc = done_task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            print(f"[proxy] audio media job task crashed for {job_id}: {exc}", flush=True)

    task.add_done_callback(_done)
    return True


def _resume_pending_audio_media_jobs() -> int:
    resumed = 0
    for status_value in (media_jobs.STATUS_RUNNING, media_jobs.STATUS_QUEUED):
        try:
            rows = media_jobs.recent(
                limit=200,
                audience="internal",
                status=status_value,
                path=media_jobs.DB_PATH,
            )
        except Exception:
            continue
        for job in rows:
            if job.get("intent") not in _AUDIO_MEDIA_JOB_INTENTS:
                continue
            metadata = job.get("metadata") or {}
            if not isinstance(metadata, Mapping) or metadata.get("mode") != "async_audio_generation":
                continue
            job_id = str(job.get("job_id") or "")
            if not job_id:
                continue
            if not _image_media_job_payload_path(job_id).exists():
                _mark_audio_media_job_failed(
                    job_id,
                    "Audio media job payload missing after proxy restart",
                )
                continue
            if _schedule_audio_media_job(job_id):
                resumed += 1
    return resumed


async def _submit_audio_media_job(
    *,
    request: Request,
    intent: str,
    body: Mapping[str, Any],
) -> Response:
    auth_context = _gateway_auth_context(request, "/gateway/v1/media/jobs")
    if isinstance(auth_context, Response):
        return auth_context

    policy_response = _enforce_runtime_route_policy(
        auth_context,
        "/gateway/v1/media/jobs",
        policy=_audio_media_job_policy(),
    )
    if policy_response is not None:
        return policy_response

    profile = _profile_for_auth_context(auth_context)
    request_id = f"audjob_{uuid.uuid4().hex}"
    workflow_name, _workflow_cfg = _resolve_audio_workflow(str(body.get("model", AUDIO_DEFAULT_WORKFLOW)))
    if not workflow_name:
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"Unknown audio model: {body.get('model', AUDIO_DEFAULT_WORKFLOW)}",
                    "type": "invalid_request_error",
                }
            }),
            status_code=400,
            media_type="application/json",
        )
    requested_model = body.get("model", AUDIO_DEFAULT_WORKFLOW)
    manifest = _load_audio_workflow_manifest(workflow_name)
    policy_decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="openai.audio",
            intent=intent,
            model=str(requested_model) if requested_model is not None else None,
            workflow=f"audio.generate.{workflow_name}",
            backend="comfyui",
            capability_exposure="default",
            capability_trust_tier="stable",
            capability_resource_tier="batch",
        ),
    )
    if policy_decision.denied:
        return _policy_denied_response(policy_decision)
    admission_response = _media_job_admission_response(intent)
    if admission_response is not None:
        return admission_response
    target_decision = _workflow_target_policy_decision(
        auth_context,
        requested=str(requested_model) if requested_model is not None else None,
        resolved=workflow_name,
        manifest=manifest,
        extra_ids=(f"audio.generate.{workflow_name}",),
    )
    if target_decision is not None and target_decision.denied:
        return _policy_denied_response(target_decision)
    job = audio_media_jobs.audio_job_from_request(
        body,
        request_id=request_id,
        workflow=f"audio.generate.{workflow_name}",
        backend="comfyui",
        app_profile=profile.id,
        api_key_name=auth_context.identity.name if auth_context.identity else None,
        parent_request_id=request_id,
    )
    job.intent = intent
    metadata = dict(job.metadata or {})
    metadata["mode"] = "async_audio_generation"
    job.metadata = metadata

    try:
        _persist_audio_media_job_payload(
            job.job_id,
            body=body,
            request=request,
            auth_context=auth_context,
        )
        created = media_jobs.create(job, path=media_jobs.DB_PATH)
    except Exception as exc:  # noqa: BLE001
        _delete_audio_media_job_payload(job.job_id)
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"Could not create audio media job: {type(exc).__name__}",
                    "type": "api_error",
                }
            }),
            status_code=500,
            media_type="application/json",
        )

    try:
        media_jobs.append_event(
            job.job_id,
            "queued",
            {
                "phase": "queued",
                "status": media_jobs.STATUS_QUEUED,
                "progress": 0,
            },
            path=media_jobs.DB_PATH,
        )
    except Exception:
        pass
    _schedule_audio_media_job(job.job_id)
    return _image_media_job_response(created.to_dict("internal"), request, status_code=202)


_VIDEO_MEDIA_JOB_INTENTS = frozenset({INTENT_VIDEO_GENERATE})
_video_media_job_tasks: set[asyncio.Task] = set()
_video_media_job_scheduled: set[str] = set()
_video_media_job_semaphore = asyncio.Semaphore(1)
_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS = 5.0


def _video_media_job_policy() -> dict[str, Any]:
    return _ASSET_CONTENT_ROUTE_POLICY_BY_KIND["vid"]


def _video_generation_body_from_media_job_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    if payload.get("intent") != INTENT_VIDEO_GENERATE:
        return None
    request_body = payload.get("request")
    if not isinstance(request_body, Mapping):
        request_body = payload.get("parameters")
    return dict(request_body) if isinstance(request_body, Mapping) else None


def _validate_video_media_job_body(body: Mapping[str, Any]) -> tuple[str | None, Response | None]:
    unknown_video_fields = detect_unknown_top_level_fields(
        body,
        STANDARD_VIDEO_CREATE_FIELDS,
    )
    if unknown_video_fields:
        first_name, first_reason = next(iter(unknown_video_fields.items()))
        return None, Response(
            content=json.dumps({"error": {
                "message": first_reason,
                "type": "invalid_request_error",
                "param": first_name,
                "code": "unknown_parameter",
            }}),
            status_code=400,
            media_type="application/json",
        )
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None, Response(
            content=json.dumps({"error": {"message": "prompt is required", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )

    requested_model = body.get("model") or VIDEO_DEFAULT_WORKFLOW
    try:
        model_resolution = resolve_model_for_operation("videos", requested_model)
    except ModelCompatibilityError as exc:
        return None, model_compatibility_error(str(exc))
    workflow_candidate = (
        model_resolution.effective_model
        if model_resolution.is_openai_alias
        else requested_model
    )
    workflow_name, workflow_cfg = _resolve_video_workflow(workflow_candidate)
    if not workflow_cfg:
        return None, Response(
            content=json.dumps({
                "error": {
                    "message": f"Unknown video model/workflow: {workflow_candidate}",
                    "type": "invalid_request_error",
                }
            }),
            status_code=400,
            media_type="application/json",
        )
    return str(workflow_name), None


def _persist_video_media_job_payload(
    job_id: str,
    *,
    body: Mapping[str, Any],
    request: Request,
    auth_context: AuthContext,
) -> None:
    _persist_image_media_job_payload(
        job_id,
        body=body,
        request=request,
        auth_context=auth_context,
    )


def _load_video_media_job_payload(job_id: str) -> dict[str, Any] | None:
    return _load_image_media_job_payload(job_id)


def _delete_video_media_job_payload(job_id: str) -> None:
    _delete_image_media_job_payload(job_id)


def _request_shim_from_video_job_payload(payload: Mapping[str, Any]) -> _JsonBodyRequestShim | None:
    body = payload.get("body")
    if not isinstance(body, Mapping):
        return None
    url = payload.get("url")
    headers = payload.get("headers")
    if not isinstance(url, Mapping):
        url = {}
    if not isinstance(headers, Mapping):
        headers = {}
    return _JsonBodyRequestShim(
        body=copy.deepcopy(dict(body)),
        headers={str(k): str(v) for k, v in headers.items()},
        scheme=str(url.get("scheme") or "http"),
        netloc=str(url.get("netloc") or headers.get("host") or "127.0.0.1"),
    )


def _mark_video_media_job_failed(
    job_id: str,
    message: str,
    *,
    http_status: int | None = None,
) -> None:
    metadata_patch: dict[str, Any] = {}
    if http_status is not None:
        metadata_patch["last_http_status"] = int(http_status)
    try:
        media_jobs.update_status(
            job_id,
            media_jobs.STATUS_FAILED,
            error=message[:500],
            metadata_patch=metadata_patch or None,
            path=media_jobs.DB_PATH,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[proxy] video media job fail update failed for {job_id}: {exc}", flush=True)
    try:
        media_jobs.append_event(
            job_id,
            "failed",
            {
                "phase": "failed",
                "status": media_jobs.STATUS_FAILED,
                "progress": 100,
                "http_status": http_status or 0,
            },
            path=media_jobs.DB_PATH,
        )
    except Exception:
        pass


def _video_response_status(response: Response) -> tuple[str | None, int]:
    parsed = _image_response_json(response)
    status_value = parsed.get("status")
    progress = parsed.get("progress", 0)
    try:
        progress_value = max(0, min(int(progress), 100))
    except (TypeError, ValueError):
        progress_value = 0
    return str(status_value) if status_value else None, progress_value


def _video_suffix_for_response(response: Response, *, filename: str | None = None) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in {".mp4", ".webm", ".mov", ".mkv"}:
            return suffix
    media_type = str(getattr(response, "media_type", None) or response.headers.get("content-type", ""))
    media_type = media_type.split(";", 1)[0].strip().lower()
    return {
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
    }.get(media_type, ".mp4")


def _video_filename_from_response(response: Response, *, default: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    marker = "filename="
    if marker in disposition:
        raw = disposition.split(marker, 1)[1].strip().strip('"')
        if raw:
            return Path(raw).name
    suffix = _video_suffix_for_response(response)
    return f"{default}{suffix}"


async def _run_video_media_job(job_id: str) -> None:
    async with _video_media_job_semaphore:
        job = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
        if not job or job.get("status") in media_jobs.TERMINAL_STATUSES:
            return

        payload = _load_video_media_job_payload(job_id)
        request_shim = _request_shim_from_video_job_payload(payload or {})
        if payload is None or request_shim is None:
            _mark_video_media_job_failed(job_id, "Video media job payload is missing or invalid")
            return

        auth_context = _auth_context_from_image_job_payload(payload)
        try:
            media_jobs.update_status(
                job_id,
                media_jobs.STATUS_RUNNING,
                progress=2,
                metadata_patch={"started_at": int(time.time())},
                path=media_jobs.DB_PATH,
            )
            media_jobs.append_event(
                job_id,
                "started",
                video_media_jobs.video_status_event(
                    status=media_jobs.STATUS_RUNNING,
                    progress=2,
                    phase="submitting",
                ),
                path=media_jobs.DB_PATH,
            )
            submit_response = await proxy_comfy_video_create(request_shim, auth_context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _mark_video_media_job_failed(
                job_id,
                f"Video media job executor error: {type(exc).__name__}",
                http_status=502,
            )
            return

        submit_status = int(getattr(submit_response, "status_code", 0) or 0)
        if not 200 <= submit_status < 300:
            message, _code = _image_response_error(submit_response)
            _mark_video_media_job_failed(job_id, message, http_status=submit_status)
            return

        submit_payload = _image_response_json(submit_response)
        video_id = submit_payload.get("id")
        if not isinstance(video_id, str) or not video_id:
            _mark_video_media_job_failed(
                job_id,
                "Video generation succeeded at submit but returned no video id",
                http_status=submit_status,
            )
            return

        media_jobs.update_status(
            job_id,
            media_jobs.STATUS_RUNNING,
            progress=5,
            metadata_patch={
                "upstream_video_id": video_id,
                "last_http_status": submit_status,
            },
            path=media_jobs.DB_PATH,
        )
        media_jobs.append_event(
            job_id,
            "submitted",
            video_media_jobs.video_status_event(
                status=media_jobs.STATUS_RUNNING,
                progress=5,
                phase="queued",
            ),
            path=media_jobs.DB_PATH,
        )

        deadline = time.monotonic() + max(1, COMFY_VIDEO_TIMEOUT)
        last_progress = 5
        while time.monotonic() < deadline:
            await asyncio.sleep(_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS)
            status_response = await proxy_comfy_video_status(video_id, auth_context)
            status_code = int(getattr(status_response, "status_code", 0) or 0)
            if status_code >= 500:
                message, _code = _image_response_error(status_response)
                _mark_video_media_job_failed(job_id, message, http_status=status_code)
                return
            status_value, progress = _video_response_status(status_response)
            if status_value in {"queued", "running"}:
                progress = max(last_progress, min(progress or last_progress, 95))
                if progress != last_progress:
                    last_progress = progress
                    media_jobs.update_status(
                        job_id,
                        media_jobs.STATUS_RUNNING,
                        progress=progress,
                        path=media_jobs.DB_PATH,
                    )
                    media_jobs.append_event(
                        job_id,
                        "progress",
                        video_media_jobs.video_status_event(
                            status=media_jobs.STATUS_RUNNING,
                            progress=progress,
                            phase=status_value,
                        ),
                        path=media_jobs.DB_PATH,
                    )
                continue
            if status_value == "failed":
                message, _code = _image_response_error(status_response)
                if message.startswith("image generation failed"):
                    parsed = _image_response_json(status_response)
                    message = str(parsed.get("error") or "Video generation failed")
                _mark_video_media_job_failed(job_id, message, http_status=status_code)
                return
            if status_value != "succeeded":
                continue

            content_response = await proxy_comfy_video_content(video_id, auth_context)
            content_status = int(getattr(content_response, "status_code", 0) or 0)
            if not 200 <= content_status < 300:
                message, _code = _image_response_error(content_response)
                _mark_video_media_job_failed(job_id, message, http_status=content_status)
                return

            content = bytes(getattr(content_response, "body", b"") or b"")
            filename = _video_filename_from_response(
                content_response,
                default=video_id,
            )
            suffix = _video_suffix_for_response(content_response, filename=filename)
            asset_id = store_asset_bytes(
                content,
                kind="vid",
                suffix=suffix,
                owner_key_hash=_asset_owner_key(auth_context),
            )
            asset = video_media_job_http.completed_asset_from_stored_video(
                filename=filename,
                asset_id=asset_id,
                content_url=build_signed_asset_url(request_shim, asset_id),
                media_type=asset_media_type(asset_id),
            )
            media_jobs.update_status(
                job_id,
                media_jobs.STATUS_SUCCEEDED,
                progress=100,
                add_assets=[asset],
                metadata_patch={
                    "completed_at": int(time.time()),
                    "upstream_video_id": video_id,
                },
                path=media_jobs.DB_PATH,
            )
            media_jobs.append_event(
                job_id,
                "completed",
                video_media_jobs.video_status_event(
                    status=media_jobs.STATUS_SUCCEEDED,
                    progress=100,
                    phase="complete",
                    asset_count=1,
                ),
                path=media_jobs.DB_PATH,
            )
            _delete_video_media_job_payload(job_id)
            return

        _mark_video_media_job_failed(
            job_id,
            "Video media job timed out",
            http_status=504,
        )


def _schedule_video_media_job(job_id: str) -> bool:
    if job_id in _video_media_job_scheduled:
        return False
    _video_media_job_scheduled.add(job_id)
    task = asyncio.create_task(_run_video_media_job(job_id))
    _video_media_job_tasks.add(task)

    def _done(done_task: asyncio.Task) -> None:
        _video_media_job_tasks.discard(done_task)
        _video_media_job_scheduled.discard(job_id)
        try:
            exc = done_task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            print(f"[proxy] video media job task crashed for {job_id}: {exc}", flush=True)

    task.add_done_callback(_done)
    return True


def _resume_pending_video_media_jobs() -> int:
    resumed = 0
    for status_value in (media_jobs.STATUS_RUNNING, media_jobs.STATUS_QUEUED):
        try:
            rows = media_jobs.recent(
                limit=200,
                audience="internal",
                status=status_value,
                path=media_jobs.DB_PATH,
            )
        except Exception:
            continue
        for job in rows:
            if job.get("intent") != INTENT_VIDEO_GENERATE:
                continue
            metadata = job.get("metadata") or {}
            if not isinstance(metadata, Mapping) or metadata.get("mode") != "async_video_generation":
                continue
            job_id = str(job.get("job_id") or "")
            if not job_id:
                continue
            if not _image_media_job_payload_path(job_id).exists():
                _mark_video_media_job_failed(
                    job_id,
                    "Video media job payload missing after proxy restart",
                )
                continue
            if _schedule_video_media_job(job_id):
                resumed += 1
    return resumed


async def _submit_video_media_job(
    *,
    request: Request,
    body: Mapping[str, Any],
) -> Response:
    auth_context = _gateway_auth_context(request, "/gateway/v1/media/jobs")
    if isinstance(auth_context, Response):
        return auth_context

    policy_response = _enforce_runtime_route_policy(
        auth_context,
        "/gateway/v1/media/jobs",
        policy=_video_media_job_policy(),
    )
    if policy_response is not None:
        return policy_response

    workflow_name, error_response = _validate_video_media_job_body(body)
    if error_response is not None:
        return error_response
    assert workflow_name is not None
    requested_model = body.get("model") or VIDEO_DEFAULT_WORKFLOW
    policy_decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="openai.video",
            intent=INTENT_VIDEO_GENERATE,
            model=str(requested_model) if requested_model is not None else None,
            workflow=f"video.generate.{workflow_name}",
            backend="comfyui",
            capability_exposure="default",
            capability_trust_tier="stable",
            capability_resource_tier="expensive",
        ),
    )
    if policy_decision.denied:
        return _policy_denied_response(policy_decision)
    admission_response = _media_job_admission_response(INTENT_VIDEO_GENERATE)
    if admission_response is not None:
        return admission_response
    target_decision = _workflow_target_policy_decision(
        auth_context,
        requested=str(requested_model) if requested_model is not None else None,
        resolved=workflow_name,
        extra_ids=(f"video.generate.{workflow_name}",),
    )
    if target_decision is not None and target_decision.denied:
        return _policy_denied_response(target_decision)

    profile = _profile_for_auth_context(auth_context)
    request_id = f"vidjob_{uuid.uuid4().hex}"
    job = video_media_job_http.job_from_openai_video_request(
        body,
        video_media_job_http.VideoRequestContext(
            workflow=workflow_name,
            backend="comfyui",
            request_id=request_id,
            idempotency_key=request.headers.get("idempotency-key"),
            app_profile=profile.id,
            api_key_name=auth_context.identity.name if auth_context.identity else None,
            parent_request_id=request_id,
        ),
    )

    try:
        _persist_video_media_job_payload(
            job.job_id,
            body=body,
            request=request,
            auth_context=auth_context,
        )
        created = media_jobs.create(job, path=media_jobs.DB_PATH)
    except Exception as exc:  # noqa: BLE001
        _delete_video_media_job_payload(job.job_id)
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"Could not create video media job: {type(exc).__name__}",
                    "type": "api_error",
                }
            }),
            status_code=500,
            media_type="application/json",
        )

    try:
        media_jobs.append_event(
            job.job_id,
            "queued",
            video_media_jobs.video_status_event(
                status=media_jobs.STATUS_QUEUED,
                progress=0,
                phase="queued",
            ),
            path=media_jobs.DB_PATH,
        )
    except Exception:
        pass
    _schedule_video_media_job(job.job_id)
    return _image_media_job_response(created.to_dict("internal"), request, status_code=202)


def _unsupported_gateway_media_job_response(intent: str) -> Response:
    supported_intents = [
        "image.generate",
        "audio.generate",
        "music.generate",
        "sfx.generate",
        "video.generate",
    ]
    return Response(
        content=json.dumps({
            "error": {
                "message": (
                    f"Media job intent {intent!r} is not executable yet. "
                    f"Currently supported: {', '.join(supported_intents)}."
                ),
                "type": "invalid_request_error",
                "code": "unsupported_media_job_intent",
                "supported_intents": supported_intents,
            }
        }),
        status_code=400,
        media_type="application/json",
    )


async def _dispatch_gateway_media_job_submission(
    *,
    request: Request,
    payload: Mapping[str, Any],
    audience: str,
) -> Response | None:
    del audience
    intent = payload.get("intent")
    if not isinstance(intent, str) or not intent.strip():
        return None
    intent = intent.strip()
    audio_body = _audio_generation_body_from_media_job_payload({**payload, "intent": intent})
    if audio_body is not None:
        return await _submit_audio_media_job(request=request, intent=intent, body=audio_body)
    video_body = _video_generation_body_from_media_job_payload({**payload, "intent": intent})
    if video_body is not None:
        return await _submit_video_media_job(request=request, body=video_body)
    body = _image_generation_body_from_media_job_payload(payload)
    if body is None:
        if intent == "image.generate":
            return None
        return _unsupported_gateway_media_job_response(intent)
    headers = _safe_image_job_headers(request)
    idempotency_key = request.headers.get("idempotency-key")
    if idempotency_key:
        headers["idempotency-key"] = idempotency_key
    submit_request = _JsonBodyRequestShim(
        body=body,
        headers=headers,
        scheme=request.url.scheme or "http",
        netloc=getattr(request.url, "netloc", "") or request.headers.get("host", "127.0.0.1"),
        scope=getattr(request, "scope", {}),
    )
    return await gateway_image_generations_media_job(
        submit_request,  # type: ignore[arg-type]
    )


@app.post("/gateway/v1/media/jobs/image-generations")
async def gateway_image_generations_media_job(request: Request):
    """Submit durable, pollable image generation work through media jobs."""
    auth_context = _gateway_auth_context(
        request,
        "/gateway/v1/media/jobs/image-generations",
    )
    if isinstance(auth_context, Response):
        return auth_context

    receipt = start_image_route_receipt(
        "/gateway/v1/media/jobs/image-generations",
        auth_context,
    )
    decision = _apply_image_route_policy(receipt, auth_context)
    if decision.denied:
        response = _policy_denied_response(decision)
        finish_image_route_receipt(receipt, response)
        return response

    try:
        body = await request.json()
    except Exception:
        _record_image_input(receipt, "body", rejected_reason="invalid_json")
        response = Response(
            content=json.dumps({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
        finish_image_route_receipt(receipt, response)
        return response
    if not isinstance(body, Mapping):
        _record_image_input(receipt, "body", rejected_reason="not_object")
        response = Response(
            content=json.dumps({"error": {"message": "JSON body must be an object", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
        finish_image_route_receipt(receipt, response)
        return response

    idempotency_key = request.headers.get("idempotency-key")
    request_id, request_hash, idempotency = _image_media_job_request_id(
        body=body,
        auth_context=auth_context,
        idempotency_key=idempotency_key.strip() if idempotency_key else None,
        fallback_request_id=receipt.request_id,
    )
    existing = media_jobs.get_by_request_id(
        request_id,
        audience="internal",
        path=media_jobs.DB_PATH,
    )
    if existing is not None:
        if existing.get("status") in {media_jobs.STATUS_QUEUED, media_jobs.STATUS_RUNNING}:
            job_id = str(existing.get("job_id") or "")
            if job_id and _image_media_job_payload_path(job_id).exists():
                _schedule_image_media_job(job_id)
        response = _image_media_job_response(existing, request)
        finish_image_route_receipt(receipt, response)
        return response

    model = str(body.get("model", COMFY_DEFAULT_MODEL))
    manifest = _find_image_manifest("image.generate", model)
    if receipt is not None:
        receipt.requested_workflow = model
        if manifest is not None:
            receipt.resolved_workflow = str(manifest.get("id") or model)
            identity = manifest_identity(manifest)
            receipt.attach_manifest(
                manifest_id=identity.id,
                manifest_version=identity.version,
                manifest_hash=identity.manifest_hash,
            )
        else:
            _attach_image_manifest_if_available(receipt, "image.generate", model)
    decision = _apply_image_route_policy(
        receipt,
        auth_context,
        model=model,
        workflow=_policy_workflow_for_receipt(receipt),
        manifest=manifest,
    )
    if decision.denied:
        response = _policy_denied_response(decision)
        finish_image_route_receipt(receipt, response)
        return response
    admission_response = _media_job_admission_response("image.generate")
    if admission_response is not None:
        finish_image_route_receipt(receipt, admission_response)
        return admission_response
    target_decision = _workflow_target_policy_decision(
        auth_context,
        requested=model,
        resolved=receipt.resolved_workflow if receipt is not None else None,
        manifest=manifest,
        extra_ids=(
            [_policy_workflow_for_receipt(receipt)]
            if _policy_workflow_for_receipt(receipt)
            else ()
        ),
    )
    if target_decision is not None and target_decision.denied:
        response = _policy_denied_response(target_decision)
        finish_image_route_receipt(receipt, response)
        return response

    profile = _profile_for_auth_context(auth_context)
    job = media_jobs.MediaJob(
        intent="image.generate",
        workflow=f"image.generate.{model}",
        backend="comfyui",
        request_id=request_id,
        app_profile=profile.id,
        api_key_name=auth_context.identity.name if auth_context.identity else None,
        parent_request_id=receipt.request_id,
        metadata=_safe_image_media_job_metadata(
            body=body,
            request_hash=request_hash,
            idempotency=idempotency,
        ),
    )

    try:
        _persist_image_media_job_payload(
            job.job_id,
            body=body,
            request=request,
            auth_context=auth_context,
        )
        created = media_jobs.create(job, path=media_jobs.DB_PATH)
    except Exception as exc:  # noqa: BLE001
        _delete_image_media_job_payload(job.job_id)
        response = Response(
            content=json.dumps({
                "error": {
                    "message": f"Could not create image media job: {type(exc).__name__}",
                    "type": "api_error",
                }
            }),
            status_code=500,
            media_type="application/json",
        )
        finish_image_route_receipt(receipt, response)
        return response

    if created.job_id != job.job_id:
        _delete_image_media_job_payload(job.job_id)
        created_dict = created.to_dict("internal")
        response = _image_media_job_response(created_dict, request)
        finish_image_route_receipt(receipt, response)
        return response

    try:
        media_jobs.append_event(
            job.job_id,
            "queued",
            {
                "phase": "queued",
                "status": media_jobs.STATUS_QUEUED,
                "progress": 0,
            },
            path=media_jobs.DB_PATH,
        )
    except Exception:
        pass
    _schedule_image_media_job(job.job_id)
    created_dict = created.to_dict("internal")
    response = _image_media_job_response(created_dict, request, status_code=202)
    finish_image_route_receipt(receipt, response)
    return response


@app.get("/gateway/v1/media/jobs/{job_id}/content")
async def gateway_media_job_content(job_id: str, request: Request):
    """Return signed output asset handles for a completed gateway media job."""
    auth_context = _gateway_auth_context(
        request,
        "/gateway/v1/media/jobs/{job_id}/content",
    )
    if isinstance(auth_context, Response):
        return auth_context
    preflight_response = _media_job_content_preflight_response(auth_context)
    if preflight_response is not None:
        return preflight_response
    job = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
    if job is None:
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"media job {job_id!r} not found",
                    "type": "invalid_request_error",
                    "code": "media_job_not_found",
                }
            }),
            status_code=404,
            media_type="application/json",
        )
    route_policy = _media_job_content_route_policy(job)
    if route_policy is not None:
        policy_response = _enforce_runtime_route_policy(
            auth_context,
            "/gateway/v1/media/jobs/{job_id}/content",
            policy=route_policy,
        )
        if policy_response is not None:
            return policy_response
    status_value = str(job.get("status") or "")
    if status_value != media_jobs.STATUS_SUCCEEDED:
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"media job is {status_value or 'unknown'}",
                    "type": "invalid_request_error",
                    "code": "media_job_not_ready",
                    "status": status_value,
                }
            }),
            status_code=409,
            media_type="application/json",
        )
    intent = str(job.get("intent") or "")
    if intent == "image.generate":
        payload = _openai_image_payload_from_media_job(job)
        missing_message = "media job has no image assets"
    elif intent in _AUDIO_MEDIA_JOB_INTENTS:
        payload = _openai_audio_payload_from_media_job(job, request)
        missing_message = "media job has no audio assets"
    elif intent == INTENT_VIDEO_GENERATE:
        payload = _openai_video_payload_from_media_job(job, request)
        missing_message = "media job has no video assets"
    else:
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"media job content is not implemented for {intent or 'unknown'} jobs",
                    "type": "invalid_request_error",
                    "code": "unsupported_media_job_content",
                }
            }),
            status_code=400,
            media_type="application/json",
        )
    if not payload["data"]:
        return Response(
            content=json.dumps({
                "error": {
                    "message": missing_message,
                    "type": "api_error",
                    "code": "media_job_assets_missing",
                }
            }),
            status_code=502,
            media_type="application/json",
        )
    return Response(
        content=json.dumps(payload),
        status_code=200,
        media_type="application/json",
    )


@app.post("/openai/v1/images/generations", openapi_extra=IMAGES_GENERATIONS_OPENAPI)
@app.post("/v1/images/generations", openapi_extra=IMAGES_GENERATIONS_OPENAPI)
async def images_generations(request: Request):
    """OpenAI-compatible image generation endpoint - routes to ComfyUI backend."""
    auth_context = authenticate_request(request, "/v1/images/generations")
    if isinstance(auth_context, Response):
        return auth_context
    receipt = start_image_route_receipt("/v1/images/generations", auth_context)
    decision = _apply_image_route_policy(receipt, auth_context)
    if decision.denied:
        response = _policy_denied_response(decision)
        finish_image_route_receipt(receipt, response)
        return response
    response = await proxy_comfy_request(request, auth_context, receipt=receipt)
    finish_image_route_receipt(receipt, response)
    return response


@app.post("/openai/v1/images/variations", openapi_extra=IMAGES_VARIATIONS_OPENAPI)
@app.post("/v1/images/variations", openapi_extra=IMAGES_VARIATIONS_OPENAPI)
async def images_variations(request: Request):
    """OpenAI-compatible image variations endpoint - explicitly unsupported."""
    auth_context = authenticate_request(request, "/v1/images/variations")
    if isinstance(auth_context, Response):
        return auth_context
    receipt = start_image_route_receipt("/v1/images/variations", auth_context)
    decision = _apply_image_route_policy(receipt, auth_context)
    if decision.denied:
        response = _policy_denied_response(decision)
        finish_image_route_receipt(receipt, response)
        return response
    response = openai_error_response(
        "Local image variation is not configured or supported.",
        code="unsupported_image_variations",
    )
    finish_image_route_receipt(receipt, response)
    return response


@app.post("/openai/v1/images/edits", openapi_extra=IMAGES_EDITS_OPENAPI)
@app.post("/v1/images/edits", openapi_extra=IMAGES_EDITS_OPENAPI)
async def images_edits(request: Request):
    """OpenAI-compatible image edit endpoint - routes to ComfyUI backend."""
    auth_context = authenticate_request(request, "/v1/images/edits")
    if isinstance(auth_context, Response):
        return auth_context
    receipt = start_image_route_receipt("/v1/images/edits", auth_context)
    decision = _apply_image_route_policy(receipt, auth_context)
    if decision.denied:
        response = _policy_denied_response(decision)
        finish_image_route_receipt(receipt, response)
        return response
    response = await proxy_comfy_edit_request(request, auth_context, receipt=receipt)
    finish_image_route_receipt(receipt, response)
    return response


@app.get("/openai/v1/images/{image_id}/content", openapi_extra=SIGNED_IMAGE_CONTENT_OPENAPI, response_class=Response)
@app.get("/v1/images/{image_id}/content", openapi_extra=SIGNED_IMAGE_CONTENT_OPENAPI, response_class=Response)
async def images_content(image_id: str, request: Request):
    """Serve a cached image referenced by a signed URL.

    Auth model: the HMAC signature and non-expired `expires` query param are
    the bearer capability for this byte fetch. This matches OpenAI-style image
    URL responses, where SDKs and browsers fetch the returned URL without
    adding the original API key.
    """
    return _signed_asset_content_response(
        image_id,
        request,
        invalid_message="Invalid image id",
        missing_message="Image not found (may have been evicted)",
        image_only=True,
    )


@app.get("/openai/v1/assets/{asset_id}/content", openapi_extra=SIGNED_IMAGE_CONTENT_OPENAPI, response_class=Response)
@app.get("/v1/assets/{asset_id}/content", openapi_extra=SIGNED_IMAGE_CONTENT_OPENAPI, response_class=Response)
async def assets_content(asset_id: str, request: Request):
    """Serve a cached generated asset referenced by a signed URL."""
    return _signed_asset_content_response(
        asset_id,
        request,
        invalid_message="Invalid asset id",
        missing_message="Asset not found (may have been evicted)",
    )


@app.get("/gateway/v1/assets/{asset_id}")
async def gateway_asset_metadata(asset_id: str, request: Request):
    """Return gateway-native metadata for a generated asset handle."""
    auth_context = _gateway_auth_context(request, "/gateway/v1/assets/{asset_id}")
    if isinstance(auth_context, Response):
        return auth_context
    parsed = _parse_asset_id(asset_id)
    if parsed is None:
        return Response(
            content=json.dumps({"error": {"message": "Invalid asset id", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    route_policy = _asset_content_route_policy(asset_id)
    if route_policy is not None:
        policy_response = _enforce_runtime_route_policy(
            auth_context,
            "/gateway/v1/assets/{asset_id}",
            policy=route_policy,
        )
        if policy_response is not None:
            return policy_response
    cache_path = asset_cache_path(asset_id)
    if cache_path is None or not cache_path.exists():
        return Response(
            content=json.dumps({"error": {"message": "Asset not found (may have been evicted)", "type": "invalid_request_error"}}),
            status_code=404,
            media_type="application/json",
        )
    if auth_context is not None and not _asset_access_allowed(asset_id, auth_context):
        return Response(
            content=json.dumps({
                "error": {
                    "message": "Asset is not accessible for this API key",
                    "type": "invalid_request_error",
                }
            }),
            status_code=403,
            media_type="application/json",
        )
    return Response(
        content=json.dumps(_gateway_asset_metadata_payload(asset_id, request, cache_path=cache_path)),
        status_code=200,
        media_type="application/json",
    )


@app.get("/gateway/v1/assets/{asset_id}/content")
async def gateway_asset_content(asset_id: str, request: Request):
    """Serve a cached generated asset through the gateway-native asset surface."""
    return _signed_asset_content_response(
        asset_id,
        request,
        invalid_message="Invalid asset id",
        missing_message="Asset not found (may have been evicted)",
    )


def _signed_asset_content_response(
    asset_id: str,
    request: Request,
    *,
    invalid_message: str,
    missing_message: str,
    image_only: bool = False,
    auth_context: AuthContext | None = None,
) -> Response:
    expires_raw = request.query_params.get("expires", "")
    sig = request.query_params.get("sig", "")
    try:
        expires = int(expires_raw)
    except (TypeError, ValueError):
        return Response(
            content=json.dumps({"error": {"message": "expires must be an integer", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    if not sig:
        return Response(
            content=json.dumps({"error": {"message": "Signature required", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    if not verify_asset_signature(asset_id, expires, sig):
        return Response(
            content=json.dumps({"error": {"message": "Invalid or expired signature", "type": "invalid_request_error"}}),
            status_code=403,
            media_type="application/json",
        )

    cache_path = image_cache_path(asset_id) if image_only else asset_cache_path(asset_id)
    if cache_path is None:
        return Response(
            content=json.dumps({"error": {"message": invalid_message, "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    if not cache_path.exists():
        return Response(
            content=json.dumps({"error": {"message": missing_message, "type": "invalid_request_error"}}),
            status_code=404,
            media_type="application/json",
        )
    if auth_context is not None and not _asset_access_allowed(asset_id, auth_context):
        return Response(
            content=json.dumps({
                "error": {
                    "message": "Asset is not accessible for this API key",
                    "type": "invalid_request_error",
                }
            }),
            status_code=403,
            media_type="application/json",
        )

    data = cache_path.read_bytes()
    return Response(
        content=data,
        status_code=200,
        media_type=asset_media_type(asset_id, cache_path),
        headers={
            "content-length": str(len(data)),
            "cache-control": "private, max-age=3600",
        },
    )


@app.post("/openai/v1/videos", openapi_extra=VIDEOS_CREATE_OPENAPI)
@app.post("/v1/videos", openapi_extra=VIDEOS_CREATE_OPENAPI)
async def videos_create(request: Request):
    """Sora-shaped video generation submit. Returns {id, status: queued}."""
    auth_context = authenticate_request(request, "/v1/videos")
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(auth_context, "/v1/videos")
    if policy_response is not None:
        return policy_response
    return await proxy_comfy_video_create(request, auth_context)


@app.get("/openai/v1/videos/{video_id}", openapi_extra=VIDEOS_STATUS_OPENAPI)
@app.get("/v1/videos/{video_id}", openapi_extra=VIDEOS_STATUS_OPENAPI)
async def videos_status(video_id: str, request: Request):
    """Poll a previously-submitted video job."""
    auth_context = authenticate_request(request, "/v1/videos/{video_id}")
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(
        auth_context, "/v1/videos/{video_id}"
    )
    if policy_response is not None:
        return policy_response
    return await proxy_comfy_video_status(video_id, auth_context)


@app.get("/openai/v1/videos/{video_id}/content", openapi_extra=VIDEOS_CONTENT_OPENAPI, response_class=Response)
@app.get("/v1/videos/{video_id}/content", openapi_extra=VIDEOS_CONTENT_OPENAPI, response_class=Response)
async def videos_content(video_id: str, request: Request):
    """Download the rendered mp4 once status=succeeded."""
    auth_context = authenticate_request(request, "/v1/videos/{video_id}/content")
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(
        auth_context, "/v1/videos/{video_id}/content"
    )
    if policy_response is not None:
        return policy_response
    return await proxy_comfy_video_content(video_id, auth_context)


# =============================================================================
# Audio Generation via ComfyUI (ACE-Step + TangoFlux)
# =============================================================================

_audio_cfg = config.get("audio", {}) or {}
AUDIO_DEFAULT_WORKFLOW = _audio_cfg.get("default_workflow", "ace-step-1.5-2b-base")
_AUDIO_WORKFLOWS_DIR_CFG = _audio_cfg.get("workflows_dir", "./workflows/audio")
AUDIO_WORKFLOWS_DIR = Path(_AUDIO_WORKFLOWS_DIR_CFG)
if not AUDIO_WORKFLOWS_DIR.is_absolute():
    AUDIO_WORKFLOWS_DIR = (Path(__file__).parent / _AUDIO_WORKFLOWS_DIR_CFG).resolve()
AUDIO_WORKFLOWS_CFG = (_audio_cfg.get("workflows", {}) or {})
AUDIO_ALIASES = (_audio_cfg.get("aliases", {}) or {})


def _resolve_audio_workflow(model: str) -> tuple:
    """Map model to (name, cfg). Returns (None, None) if not found."""
    candidate = (model or "").strip()
    if not candidate:
        candidate = AUDIO_DEFAULT_WORKFLOW
    if candidate in AUDIO_WORKFLOWS_CFG:
        return candidate, AUDIO_WORKFLOWS_CFG[candidate]
    alias_target = AUDIO_ALIASES.get(candidate.lower())
    if alias_target and alias_target in AUDIO_WORKFLOWS_CFG:
        return alias_target, AUDIO_WORKFLOWS_CFG[alias_target]
    return None, None


def _load_audio_workflow_file(workflow_cfg: dict):
    """Load audio workflow JSON."""
    file_path = workflow_cfg.get("file")
    if not file_path:
        return None
    p = Path(file_path)
    if not p.is_absolute():
        p = Path(__file__).parent / file_path
    if not p.exists():
        return None
    with open(p) as f:
        workflow = json.load(f)
    errors = validate_comfyui_workflow_graph(workflow)
    if errors:
        print(
            f"[proxy] invalid audio ComfyUI workflow {p}: {'; '.join(errors)}",
            flush=True,
        )
        return None
    return workflow


def _load_audio_workflow_manifest(workflow_name: str) -> dict[str, Any] | None:
    for record in GATEWAY_WORKFLOW_REGISTRY.records():
        if not record.intent.startswith(("audio.", "music.", "sfx.")):
            continue
        if record.id == workflow_name or record.id.endswith(f".{workflow_name}"):
            return copy.deepcopy(dict(record.manifest))
        bindings = record.manifest.get("bindings")
        if not isinstance(bindings, Mapping):
            continue
        comfy = bindings.get("comfyui")
        if not isinstance(comfy, Mapping):
            continue
        artifact = comfy.get("workflow")
        if isinstance(artifact, str) and Path(artifact).stem == workflow_name:
            return copy.deepcopy(dict(record.manifest))
    return None


def _audio_extension_error_response(message: str, *, param: str = "extensions") -> Response:
    return Response(
        content=json.dumps(
            {
                "error": {
                    "message": message,
                    "type": "invalid_request_error",
                    "param": param,
                }
            }
        ),
        status_code=400,
        media_type="application/json",
    )


def _extract_audio_output(job: dict):
    """Extract audio file info from ComfyUI job outputs."""
    outputs = (job or {}).get("outputs", {}) or {}
    for node_id, node_output in outputs.items():
        for key in ("audios", "audio", "wav", "mp3"):
            entries = node_output.get(key) or []
            for entry in entries:
                if isinstance(entry, dict):
                    return {
                        "filename": entry.get("filename"),
                        "subfolder": entry.get("subfolder", ""),
                        "type": entry.get("type", "output"),
                    }
    return None


async def proxy_audio_generate(request: Request, auth_context):
    """POST /v1/audio/generate — submit audio generation via ComfyUI."""
    if not COMFY_ENABLED:
        return Response(
            content=json.dumps({"error": {"message": "Audio generation not enabled", "type": "service_unavailable"}}),
            status_code=503,
            media_type="application/json"
        )
    service_response = _service_availability_response(
        "comfyui",
        service_label="ComfyUI",
    )
    if service_response is not None:
        return service_response
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )

    unknown_top_level = detect_unknown_audio_fields(body)
    if unknown_top_level:
        name, reason = next(iter(unknown_top_level.items()))
        return _audio_extension_error_response(reason, param=name)

    text_fields: dict[str, str | None] = {}
    for field_name in ("prompt", "input", "tags", "lyrics"):
        value = body.get(field_name)
        if value is None:
            text_fields[field_name] = None
            continue
        if not isinstance(value, str):
            return Response(
                content=json.dumps({
                    "error": {
                        "message": f"{field_name} must be a string",
                        "type": "invalid_request_error",
                        "param": field_name,
                    }
                }),
                status_code=400,
                media_type="application/json",
            )
        text_fields[field_name] = value.strip()

    prompt = (
        text_fields.get("prompt")
        or text_fields.get("input")
        or text_fields.get("tags")
        or ""
    )
    tags = text_fields.get("tags")
    lyrics = text_fields.get("lyrics")
    model = body.get("model", AUDIO_DEFAULT_WORKFLOW)
    duration = body.get("duration", 30)
    seed = body.get("seed")
    audio_type = body.get("type", "music")  # music | sfx | ambient

    if not prompt:
        return Response(
            content=json.dumps({
                "error": {
                    "message": "prompt, input, or tags is required",
                    "type": "invalid_request_error",
                }
            }),
            status_code=400,
            media_type="application/json"
        )

    workflow_name, workflow_cfg = _resolve_audio_workflow(model)
    if not workflow_cfg:
        return Response(
            content=json.dumps({"error": {"message": f"Unknown audio model: {model}", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )

    workflow = _load_audio_workflow_file(workflow_cfg)
    if not workflow:
        return Response(
            content=json.dumps({"error": {"message": f"Workflow file missing for {workflow_name}", "type": "api_error"}}),
            status_code=500,
            media_type="application/json"
        )
    manifest = _load_audio_workflow_manifest(workflow_name)
    policy_decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="openai.audio",
            intent=INTENT_AUDIO_GENERATE,
            model=str(model) if model is not None else None,
            workflow=f"audio.generate.{workflow_name}",
            backend="comfyui",
            capability_exposure="default",
            capability_trust_tier="stable",
            capability_resource_tier="batch",
        ),
    )
    if policy_decision.denied:
        return _policy_denied_response(policy_decision)
    target_decision = _workflow_target_policy_decision(
        auth_context,
        requested=str(model) if model is not None else None,
        resolved=str(workflow_name),
        manifest=manifest,
        extra_ids=(f"audio.generate.{workflow_name}",),
    )
    if target_decision is not None and target_decision.denied:
        return _policy_denied_response(target_decision)

    workflow = copy.deepcopy(workflow)
    meta = workflow.pop("_meta", {})

    # Inject parameters. Each *_node entry may be a single node id (legacy)
    # or a list of {"node": id, "key": fieldname} mappings so one workflow
    # can write the same logical value into multiple node inputs with
    # different key names (e.g. ACE-Step's `seconds` vs TangoFlux's `duration`).
    def _injection_targets(meta_value, default_key):
        if not meta_value:
            return []
        if isinstance(meta_value, list):
            out = []
            for entry in meta_value:
                if isinstance(entry, dict) and entry.get("node"):
                    out.append((entry["node"], entry.get("key", default_key)))
                elif isinstance(entry, str):
                    out.append((entry, default_key))
            return out
        return [(meta_value, default_key)]

    for node_id, key in _injection_targets(meta.get("prompt_node"), "text"):
        if node_id in workflow:
            workflow[node_id]["inputs"][key] = prompt

    if tags is not None:
        for node_id, key in _injection_targets(meta.get("tags_node"), "tags"):
            if node_id in workflow:
                workflow[node_id]["inputs"][key] = tags

    if lyrics is not None:
        for node_id, key in _injection_targets(meta.get("lyrics_node"), "lyrics"):
            if node_id in workflow:
                workflow[node_id]["inputs"][key] = lyrics

    for node_id, key in _injection_targets(meta.get("duration_node"), "duration"):
        if node_id in workflow:
            try:
                workflow[node_id]["inputs"][key] = float(duration)
            except (TypeError, ValueError):
                pass

    seed_targets = _injection_targets(meta.get("seed_node"), "seed")
    if seed_targets:
        try:
            seed_val = int(seed) if seed is not None else random.randint(0, 2**32 - 1)
        except (TypeError, ValueError):
            seed_val = random.randint(0, 2**32 - 1)
        for node_id, key in seed_targets:
            if node_id in workflow:
                workflow[node_id]["inputs"][key] = seed_val

    if manifest is not None:
        try:
            extension_result = bind_audio_workflow_extensions(
                body,
                manifest,
                "comfyui",
                workflow,
            )
        except (ExtensionError, ManifestError) as e:
            return _audio_extension_error_response(str(e))
        if extension_result.unknown_top_level:
            name, reason = next(iter(extension_result.unknown_top_level.items()))
            return _audio_extension_error_response(reason, param=name)
        if extension_result.validation.rejected:
            name, reason = next(iter(extension_result.validation.rejected.items()))
            return _audio_extension_error_response(
                reason,
                param=f"extensions.{extension_result.namespace}.{name}",
            )
        workflow = extension_result.request

    headers = configured_downstream_headers(auth_context, "comfyui")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
            resp = await client.post(
                f"{COMFY_HOST}/prompt",
                headers=headers,
                json=_build_comfy_submit_payload(workflow, client_id=COMFY_CLIENT_ID),
            )
            if resp.status_code != 200:
                return Response(
                    content=json.dumps({"error": {"message": f"ComfyUI error: {resp.text[:500]}", "type": "api_error"}}),
                    status_code=502,
                    media_type="application/json"
                )
            prompt_id = resp.json().get("prompt_id")
            if not prompt_id:
                return Response(
                    content=json.dumps({"error": {"message": "No prompt_id returned", "type": "api_error"}}),
                    status_code=502,
                    media_type="application/json"
                )

            audio_id = f"audio_{prompt_id}"
            print(f"[proxy] Audio job submitted: workflow={workflow_name} id={audio_id}", flush=True)

            # Wait for completion (simple polling)
            for _ in range(600):  # 10 min max
                await asyncio.sleep(2)
                history_resp = await client.get(f"{COMFY_HOST}/history/{prompt_id}", headers=headers)
                if history_resp.status_code != 200:
                    continue
                history = history_resp.json()
                if prompt_id not in history:
                    continue
                job = history[prompt_id]
                status_obj = job.get("status", {}) or {}
                status = status_obj.get("status_str") or status_obj.get("status") or ""
                if status == "success":
                    file_info = _extract_audio_output(job)
                    if file_info and file_info.get("filename"):
                        # Fetch the audio file
                        params = {"filename": file_info["filename"], "type": file_info.get("type", "output")}
                        if file_info.get("subfolder"):
                            params["subfolder"] = file_info["subfolder"]
                        audio_resp = await client.get(f"{COMFY_HOST}/view", headers=headers, params=params)
                        if audio_resp.status_code == 200:
                            content_type = audio_resp.headers.get("content-type", "audio/wav")
                            return Response(
                                content=audio_resp.content,
                                status_code=200,
                                media_type=content_type,
                                headers={"X-Audio-Model": workflow_name, "X-Audio-Type": audio_type}
                            )
                    return Response(
                        content=json.dumps({"error": {"message": "No audio output found", "type": "api_error"}}),
                        status_code=502,
                        media_type="application/json"
                    )
                elif status == "error":
                    err_msg = "ComfyUI generation failed"
                    for tag, payload in (status_obj.get("messages") or []):
                        if tag == "execution_error" and isinstance(payload, dict):
                            err_msg = f"ComfyUI {payload.get('node_type','')}: {payload.get('exception_message','').strip() or payload.get('exception_type','')}"
                            break
                    return Response(
                        content=json.dumps({"error": {"message": err_msg, "type": "api_error"}}),
                        status_code=502,
                        media_type="application/json"
                    )

            return Response(
                content=json.dumps({"error": {"message": "Audio generation timeout", "type": "api_error"}}),
                status_code=504,
                media_type="application/json"
            )
    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": {"message": "ComfyUI request timeout", "type": "api_error"}}),
            status_code=504,
            media_type="application/json"
        )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": {"message": f"ComfyUI unavailable at {COMFY_HOST}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": {"message": f"Audio proxy error: {str(e)}", "type": "api_error"}}),
            status_code=502,
            media_type="application/json"
        )


@app.post("/openai/v1/audio/generate", openapi_extra=AUDIO_GENERATE_OPENAPI)
@app.post("/v1/audio/generate", openapi_extra=AUDIO_GENERATE_OPENAPI)
async def audio_generate(request: Request):
    """OpenAI-compatible audio generation endpoint."""
    auth_context = authenticate_request(request, "/v1/audio/generate")
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(auth_context, "/v1/audio/generate")
    if policy_response is not None:
        return policy_response
    return await proxy_audio_generate(request, auth_context)


@app.get("/openai/v1/audio/models", openapi_extra=AUDIO_MODELS_OPENAPI)
@app.get("/v1/audio/models", openapi_extra=AUDIO_MODELS_OPENAPI)
async def audio_models(request: Request):
    """List available audio generation models."""
    auth_context = authenticate_request(request, "/v1/audio/models")
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(auth_context, "/v1/audio/models")
    if policy_response is not None:
        return policy_response
    models = []
    allowlist = _profile_model_allowlist(auth_context)
    include_available = (
        _profile_target_visibility(auth_context) != "available"
        or _comfy_target_available()
    )
    for name, cfg in AUDIO_WORKFLOWS_CFG.items():
        if not include_available:
            continue
        if allowlist is not None and name not in allowlist:
            continue
        models.append({
            "id": name,
            "owned_by": "local",
            "permission": "generate",
            "max_seconds": cfg.get("max_seconds", 300),
            "min_seconds": cfg.get("min_seconds", 1),
        })
    payload = {"data": models}
    if _profile_target_visibility(auth_context) == "available":
        payload["vivid_fish_target_visibility"] = "available"
        payload["vivid_fish_target_activation"] = _profile_target_activation(auth_context)
    return Response(
        content=json.dumps(payload),
        status_code=200,
        media_type="application/json"
    )


def _entry_created_ts(path: Optional[str]) -> int:
    """mtime of a GGUF path when stat-able, else current time. Silent on EBADF/etc.
    is bad — log when the file is unreadable so missing mounts surface."""
    if path:
        try:
            return int(os.path.getmtime(path))
        except OSError as e:
            print(f"[proxy] /v1/models: could not stat {path}: {e}; using current time", flush=True)
    return int(time.time())


_MODEL_CONTEXT_CACHE_TTL_S = 30.0
_MODEL_CONTEXT_CACHE: dict[str, tuple[float, int | None]] = {}


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _context_window_from_model_info(model_info: Mapping[str, Any] | None) -> int | None:
    if not isinstance(model_info, Mapping):
        return None
    for key, value in model_info.items():
        key_s = str(key).lower()
        if key_s.endswith(".context_length") or key_s in {
            "context_length",
            "context_window",
            "max_context_tokens",
            "n_ctx",
        }:
            found = _positive_int(value)
            if found is not None:
                return found
    return None


def _context_window_from_mapping(data: Mapping[str, Any] | None) -> int | None:
    if not isinstance(data, Mapping):
        return None
    for key in (
        "context_window",
        "context_length",
        "max_context_tokens",
        "max_model_len",
        "max_seq_len",
        "n_ctx",
    ):
        found = _positive_int(data.get(key))
        if found is not None:
            return found
    meta = data.get("meta")
    if isinstance(meta, Mapping):
        found = _context_window_from_mapping(meta)
        if found is not None:
            return found
    found = _context_window_from_model_info(
        data.get("model_info") if isinstance(data.get("model_info"), Mapping) else None
    )
    if found is not None:
        return found
    top_provider = data.get("top_provider")
    if isinstance(top_provider, Mapping):
        return _context_window_from_mapping(top_provider)
    return None


def _configured_context_window_for_backend(backend: str | None) -> int | None:
    if not backend:
        return None
    if backend == "tabby":
        _, _, params = get_tabby_model_config()
        return _context_window_from_mapping(params)
    backends = config.get("backends") if isinstance(config.get("backends"), Mapping) else {}
    raw = backends.get(backend) if isinstance(backends, Mapping) else None
    if isinstance(raw, Mapping):
        return _context_window_from_mapping(raw)
    return None


async def _kobold_context_window() -> int | None:
    cached = _MODEL_CONTEXT_CACHE.get("kobold")
    now = time.time()
    if cached and now - cached[0] < _MODEL_CONTEXT_CACHE_TTL_S:
        return cached[1]

    value: int | None = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
            resp = await client.get(f"{KOBOLD_HOST}/v1/models")
        if resp.status_code == 200:
            payload = resp.json()
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, Mapping):
                        continue
                    value = _context_window_from_mapping(item)
                    if value is not None:
                        break
            if value is None and isinstance(payload, Mapping):
                value = _context_window_from_mapping(payload)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] /v1/models: could not read kobold context window: {e}", flush=True)

    _MODEL_CONTEXT_CACHE["kobold"] = (now, value)
    return value


def _configured_model_record_for_id(model_id: str) -> tuple[str | None, Mapping[str, Any] | None]:
    normalized = normalize_model_id(model_id)
    if not normalized:
        return None, None

    role = ROLES.get(normalized) if isinstance(ROLES, Mapping) else None
    if isinstance(role, Mapping):
        backend = str(role.get("backend") or "").strip() or None
        return backend, role

    catalog = MODELS_CATALOG.get(normalized)
    if isinstance(catalog, Mapping):
        backend = str(catalog.get("backend") or "").strip() or None
        return backend, catalog

    backends = config.get("backends") if isinstance(config.get("backends"), Mapping) else {}
    if isinstance(backends, Mapping):
        for backend, service_cfg in backends.items():
            if not isinstance(service_cfg, Mapping):
                continue
            service_model = normalize_model_id(str(service_cfg.get("model_id") or ""))
            if service_model == normalized:
                return str(backend), service_cfg
            for item in _service_model_records(service_cfg):
                names = {
                    normalize_model_id(str(item.get("id") or "")),
                    normalize_model_id(str(item.get("model_group") or "")),
                    normalize_model_id(str(item.get("public_name") or "")),
                    normalize_model_id(str(item.get("upstream_model") or "")),
                }
                if normalized in names:
                    return str(backend), item

    return None, None


async def _context_window_for_model_id(
    model_id: str,
    entry: Mapping[str, Any] | None = None,
) -> int | None:
    found = _context_window_from_mapping(entry)
    if found is not None:
        return found

    backend = None
    if isinstance(entry, Mapping):
        backend = str(entry.get("vivid_fish_backend") or "").strip() or None
    configured_backend, record = _configured_model_record_for_id(model_id)
    backend = backend or configured_backend

    found = _context_window_from_mapping(record)
    if found is not None:
        return found

    found = _configured_context_window_for_backend(backend)
    if found is not None:
        return found
    if backend == "kobold":
        return await _kobold_context_window()
    return None


async def _annotate_model_context_windows(data: list[Any]) -> None:
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        found = await _context_window_for_model_id(model_id, item)
        if found is not None:
            item["context_window"] = found


def _build_role_catalog_entries() -> list[dict]:
    """Return configured LLM role handles for admin inventory and policy checks.

    Accepts any backend that is known in config["backends"] (or is a legacy
    local-swap backend such as kobold/tabby).  Unknown backends are silently
    skipped so that a typo in the config does not crash the listing.

    For local-swap backends (kobold/tabby or any role entry with a ``path``
    key), ``model`` is a GGUF file path and is surfaced as
    ``vivid_fish_model_path``.  For remote/routed backends the ``model`` field
    is the upstream model name and is surfaced as ``vivid_fish_model``.  Remote
    role entries that omit ``model`` entirely are skipped (malformed).
    """
    known_backends: frozenset[str] = frozenset(
        (config.get("backends") or {}).keys()
    ) | _LOCAL_SWAP_BACKENDS
    entries: list[dict] = []

    if isinstance(ROLES, dict):
        for role_name, role_def in ROLES.items():
            if not isinstance(role_def, dict):
                continue
            backend = role_def.get("backend")
            # Skip entries whose backend is not known in the registry.
            if not backend or backend not in known_backends:
                continue
            is_local_swap = backend in _LOCAL_SWAP_BACKENDS or bool(role_def.get("path"))
            model_value = role_def.get("model")
            if is_local_swap:
                # Local-swap: model is a GGUF path (may be absent for unpinned tabby roles).
                model_path = model_value if isinstance(model_value, str) else None
                entry = {
                    "id": str(role_name),
                    "object": "model",
                    "created": _entry_created_ts(model_path),
                    "owned_by": "vivid-fish",
                    "description": str(role_def.get("description", "") or ""),
                    "vivid_fish_role": True,
                    "vivid_fish_backend": backend,
                }
                if model_path:
                    entry["vivid_fish_model_path"] = model_path
            else:
                # Remote/routed backend: model is the upstream model name (required).
                if not model_value or not str(model_value).strip():
                    # Skip malformed remote role entry (no model name to route to).
                    continue
                upstream_model = str(model_value).strip()
                entry = {
                    "id": str(role_name),
                    "object": "model",
                    "created": _entry_created_ts(None),
                    "owned_by": "vivid-fish",
                    "description": str(role_def.get("description", "") or ""),
                    "vivid_fish_role": True,
                    "vivid_fish_backend": backend,
                    "vivid_fish_model": upstream_model,
                }
            entries.append(entry)

    for mid, cat in MODELS_CATALOG.items():
        entries.append({
            "id": mid,
            "object": "model",
            "created": _entry_created_ts(cat.get("path")),
            "owned_by": "vivid-fish",
            "description": cat.get("description", ""),
            "vivid_fish_alias": True,
            "vivid_fish_backend": cat.get("backend"),
            "vivid_fish_model_path": cat.get("path"),
        })

    if EMBEDDINGS_ENABLED and EMBEDDINGS_MODEL:
        entries.append({
            "id": EMBEDDINGS_MODEL,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "vivid-fish",
            "description": "CPU embedding model",
            "vivid_fish_alias": True,
            "vivid_fish_backend": "embeddings",
            "vivid_fish_intent": "text.embeddings",
            "capabilities": ["embedding"],
        })

    return entries


def _workflow_model_entry_created(workflow_artifact: str) -> int:
    path = Path(workflow_artifact)
    if not path.is_absolute():
        path = (Path(__file__).resolve().parent / path).resolve()
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return int(time.time())


def _build_workflow_model_entries(
    registry: WorkflowRegistry = GATEWAY_WORKFLOW_REGISTRY,
) -> list[dict]:
    """Construct OpenAI-shaped model entries for directly invocable workflows.

    Direct ComfyUI workflows use the workflow JSON basename because the image
    routes load `workflows/images/<model>.json` directly. Gateway-composed
    workflows can declare an explicit `bindings.gateway.model_id`. Keep local
    paths out of the public payload; gateway-native discovery has the fuller
    contract.
    """
    entries: list[dict] = []
    for record in registry.records():
        if not record.intent.startswith("image."):
            continue
        bindings = record.manifest.get("bindings")
        if not isinstance(bindings, dict):
            continue
        backend = "comfyui"
        model_id = _gateway_image_model_id(record.manifest)
        workflow_artifact = ""
        if model_id:
            backend = "gateway"
            workflow_artifact = str(record.source_path) if record.source_path else ""
        else:
            comfy = bindings.get("comfyui")
            if not isinstance(comfy, dict):
                continue
            artifact = comfy.get("workflow")
            if not isinstance(artifact, str) or not artifact:
                continue
            workflow_artifact = artifact
            model_id = Path(workflow_artifact).stem
            if not model_id:
                continue
        entry = {
            "id": model_id,
            "object": "model",
            "created": _workflow_model_entry_created(workflow_artifact),
            "owned_by": "vivid-fish",
            "description": str(record.manifest.get("description", "") or ""),
            "vivid_fish_workflow": True,
            "vivid_fish_workflow_id": record.id,
            "vivid_fish_intent": record.intent,
            "vivid_fish_backend": backend,
        }
        version = record.manifest.get("version")
        if version:
            entry["vivid_fish_manifest_version"] = str(version)
        entries.append(entry)
    return entries


def _anthropic_created_at(created: Any) -> str:
    try:
        ts = int(created)
    except (TypeError, ValueError):
        ts = int(time.time())
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _anthropic_display_name(model_id: str) -> str:
    display = model_id.replace("_", " ").replace("-", " ").strip().title()
    return display or model_id


def _anthropic_model_info(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    return {
        "id": model_id,
        "type": "model",
        "display_name": str(entry.get("description") or _anthropic_display_name(model_id)),
        "created_at": _anthropic_created_at(entry.get("created")),
    }


async def _anthropic_model_info_list(
    request: Request,
    auth_context: AuthContext,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    litellm_runtime = _litellm_runtime_config()
    if litellm_runtime.enabled:
        payload = await _litellm_models_payload(
            request,
            auth_context,
            runtime_config=litellm_runtime,
        )
        if isinstance(payload, Mapping):
            entries.extend(_litellm_model_summaries_from_payload(payload))

    entries.extend(_current_loaded_model_entries())
    filtered, metadata = await _filter_model_list_for_profile(entries, auth_context)
    seen: set[str] = set()
    models: list[dict[str, Any]] = []
    for entry in filtered:
        if not isinstance(entry, Mapping):
            continue
        item = _anthropic_model_info(entry)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        models.append(item)
    models.sort(key=lambda item: item["id"])
    return models, metadata


async def _litellm_models_payload(
    request: Request,
    auth_context: AuthContext,
    *,
    runtime_config,
) -> dict[str, Any] | None:
    execution_request = LlmExecutionRequest(
        method="GET",
        url=litellm_url(base_url=runtime_config.base_url, path="v1/models"),
        headers=litellm_headers(
            request.headers,
            api_key=runtime_config.api_key,
            gateway_metadata=_litellm_gateway_metadata(
                auth_context,
                request_path="/v1/models",
                service_id=runtime_config.service_id,
            ),
        ),
        body=b"",
        read_timeout=10.0,
    )
    client = None
    resp = None
    try:
        client, resp = await send_llm_execution_request(execution_request)
        if resp.status_code != 200:
            return None
        content = await resp.aread()
        payload = json.loads(content.decode("utf-8") or "{}")
        if isinstance(payload, dict):
            payload["x_ai_gateway_llm_execution"] = "litellm"
            return payload
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] LiteLLM model catalog fetch failed: {e}", flush=True)
        return None
    finally:
        if resp is not None:
            await resp.aclose()
        if client is not None:
            await client.aclose()


def _litellm_model_summaries_from_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    summaries: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        created = item.get("created")
        modified_at = None
        if isinstance(created, (int, float)):
            modified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created))
        summary = {
            "id": model_id,
            "object": str(item.get("object") or "model"),
            "owned_by": str(item.get("owned_by") or "litellm"),
            "modified_at": modified_at,
            "format": str(item.get("format") or ""),
            "family": str(item.get("family") or ""),
            "parameter_size": str(item.get("parameter_size") or ""),
            "quantization_level": str(item.get("quantization_level") or ""),
            "capabilities": item.get("capabilities") or ["completion"],
            "model_info": item.get("model_info") if isinstance(item.get("model_info"), Mapping) else {},
        }
        context_window = _context_window_from_mapping(item)
        if context_window is not None:
            summary["context_window"] = context_window
        summaries.append(summary)
    return summaries


def _current_loaded_model_entry(model_id: str = USE_LOADED_MODEL_ALIAS) -> dict[str, Any]:
    fast = normalize_model_id(model_id) == USE_LOADED_FAST_MODEL_ALIAS
    return {
        "id": model_id,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "gateway",
        "description": (
            "Use the currently loaded local LLM with Qwen thinking off; do "
            "not activate a backend or swap models."
            if fast
            else "Use the currently loaded local LLM without activating a "
            "backend or swapping models."
        ),
        "capabilities": ["chat", "completion"],
        "vivid_fish_alias": True,
        "vivid_fish_current_loaded": True,
        "vivid_fish_current_loaded_fast": fast,
        "vivid_fish_backend": "current",
    }


def _current_loaded_model_entries() -> list[dict[str, Any]]:
    return [
        _current_loaded_model_entry(USE_LOADED_MODEL_ALIAS),
        _current_loaded_model_entry(USE_LOADED_FAST_MODEL_ALIAS),
    ]


@app.get("/anthropic/v1/models")
async def anthropic_models(request: Request):
    """Anthropic-compatible model list projected from the gateway catalog."""
    auth_context = authenticate_request(request, "/anthropic/v1/models")
    if isinstance(auth_context, Response):
        return auth_context

    policy_response = _enforce_runtime_route_policy(
        auth_context,
        "/anthropic/v1/models",
        policy={"protocol": "anthropic.messages", "intent": "models.list"},
    )
    if policy_response is not None:
        return _anthropic_error_response(
            _response_error_message(policy_response, "request denied by app profile"),
            status_code=policy_response.status_code,
            error_type="permission_error",
        )

    data, metadata = await _anthropic_model_info_list(request, auth_context)
    payload = {
        "object": "list",
        "data": data,
        "has_more": False,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
    }
    payload.update(metadata)
    response = _json_error_response(payload, status_code=200)
    response.headers["X-AI-Gateway-Token-Counting"] = "estimated"
    return response


@app.get("/openai/v1/models")
@app.get("/v1/models")
async def list_models(request: Request):
    """OpenAI-compatible model list. Returns:
      1. LiteLLM's OpenAI-shaped LLM model catalog when the substrate is enabled
      2. Gateway-native media workflow model IDs
    """
    auth_context = authenticate_request(request, "/v1/models")
    if isinstance(auth_context, Response):
        return auth_context
    policy_response = _enforce_runtime_route_policy(auth_context, "/v1/models")
    if policy_response is not None:
        return policy_response

    upstream_payload: dict = {"object": "list", "data": []}
    litellm_runtime = _litellm_runtime_config()
    if litellm_runtime.enabled:
        service_response = _service_availability_response(
            litellm_runtime.service_id or "litellm",
            service_label="LiteLLM",
        )
        if service_response is not None:
            return service_response
        payload = await _litellm_models_payload(
            request,
            auth_context,
            runtime_config=litellm_runtime,
        )
        if isinstance(payload, dict):
            upstream_payload = payload
        else:
            upstream_payload["vivid_fish_catalog_warning"] = (
                "LLM model catalog is temporarily unavailable; gateway-native "
                "aliases and workflows may still be listed."
            )

    data = upstream_payload.get("data")
    if not isinstance(data, list):
        data = []
        upstream_payload["data"] = data

    # Drop the full-absolute-path duplicate from kobold's payload — it's
    # redundant with the filename-only entry and LibreChat shows both as
    # separate options, which is confusing.
    data = [
        item for item in data
        if not (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].startswith("/")
            and item["id"].endswith(".gguf")
        )
    ]

    workflow_entries = _build_workflow_model_entries()
    existing_ids = {item.get("id") for item in data if isinstance(item, dict)}
    for current_entry in reversed(_current_loaded_model_entries()):
        if current_entry["id"] not in existing_ids:
            data.insert(0, current_entry)
            existing_ids.add(current_entry["id"])
    for entry in workflow_entries:
        if entry["id"] not in existing_ids:
            data.insert(0, entry)
            existing_ids.add(entry["id"])

    data, policy_metadata = await _filter_model_list_for_profile(data, auth_context)

    # Annotate each model entry with a description sourced from roles: or
    # models: in proxy_config.yml. Non-standard field — ignored by spec-
    # compliant clients, readable by agents querying the catalog.
    roles_cfg = config.get("roles") or {}
    extra_models_cfg = {
        normalize_model_id(str(m.get("id") or "")): m
        for m in (config.get("models") or [])
        if isinstance(m, Mapping) and m.get("id")
    }
    for item in data:
        if not isinstance(item, dict) or item.get("description"):
            continue
        mid = normalize_model_id(str(item.get("id") or ""))
        role = roles_cfg.get(mid) if isinstance(roles_cfg, Mapping) else None
        desc = str(role.get("description") or "") if isinstance(role, Mapping) else ""
        if not desc:
            extra = extra_models_cfg.get(mid)
            desc = str(extra.get("description") or "") if isinstance(extra, Mapping) else ""
        if desc:
            item["description"] = desc

    await _annotate_model_context_windows(data)

    upstream_payload.update(policy_metadata)
    upstream_payload["data"] = data
    upstream_payload["object"] = upstream_payload.get("object") or "list"
    return Response(
        content=json.dumps(upstream_payload),
        status_code=200,
        media_type="application/json",
    )


@app.get("/v1/status")
async def v1_status(request: Request):
    """Consolidated admin-only status JSON for the dashboard."""
    try:
        ADMIN_AUTH(request)
    except AdminAuthError as exc:
        return Response(
            content=json.dumps({
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                }
            }),
            status_code=exc.status_code,
            media_type="application/json",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = await status_endpoint.get_cached_status(config)
    except Exception as e:  # noqa: BLE001
        # Never leak tracebacks. Return a minimal degraded payload instead.
        print(f"[proxy] /v1/status fatal: {e}", flush=True)
        return Response(
            content=json.dumps({
                "generated_at": int(time.time()),
                "banner": "bad",
                "backends": [],
                "gpus": None,
                "comfy_queue": None,
                "models": None,
                "capabilities": [],
                "recent_renders": [],
                "driver": None,
                "errors": {"top": "status build failed"},
            }),
            status_code=200,
            media_type="application/json",
        )

    return Response(
        content=json.dumps(payload),
        status_code=200,
        media_type="application/json",
        headers={"cache-control": "no-store"},
    )


# -----------------------------------------------------------------------------
# Status dashboard
#
# The product root is the browser account portal. `/me` remains a legacy
# browser alias that redirects here; `/me/api/*` stays the user account API.
# The maximalist server-rendered operator status page lives at `/status`.
# The old `/firehose` URL is kept as a 301 → `/status` for compatibility.
# The previous Next.js page under status-page/out is no longer served.
# -----------------------------------------------------------------------------

async def _render_status_html(request: Request) -> Response:
    """Build a payload from /v1/status and render the dashboard HTML."""
    try:
        actor = ADMIN_AUTH(request)
    except AdminAuthError as exc:
        if (
            exc.status_code == 401
            and _effective_admin_oidc_config().enabled
            and not request.headers.get("authorization")
        ):
            return RedirectResponse(_login_url(return_to="/status"), status_code=302)
        return Response(
            content=json.dumps({
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                }
            }),
            status_code=exc.status_code,
            media_type="application/json",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = await status_endpoint.get_cached_status(config)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] status render fatal: {e}", flush=True)
        payload = {"error": "status build failed"}
    try:
        from firehose_render import render_html
        # Use the request's Host header so the page header reflects the
        # address bar (loopback dev vs. external ai.vivid.fish).
        host_hdr = request.headers.get("host") or ""
        actor_label = actor.email or actor.subject
        logout_url = "/me/logout" if actor.auth_method == "session" else None
        body = render_html(
            payload,
            host=host_hdr,
            actor_label=actor_label,
            logout_url=logout_url,
        )
    except Exception as e:  # noqa: BLE001
        import traceback as _tb
        _tb_str = _tb.format_exc()
        print(f"[proxy] status render error: {e}\n{_tb_str}", flush=True)
        body = (
            "<html><body><pre>status render failed: "
            + str(e)[:200]
            + "</pre></body></html>"
        )
    return Response(
        content=body,
        status_code=200,
        media_type="text/html; charset=utf-8",
        headers={"cache-control": "no-store"},
    )


@app.get("/", include_in_schema=False)
async def root_browser_entrypoint(request: Request):
    return _render_user_portal_response(request, login_return_to="/")


@app.get("/r/{request_id}", include_in_schema=False)
async def receipt_deep_link(request_id: str, request: Request):
    # Addressable receipt detail: a shareable "why did this fail" URL. It serves
    # the same portal page; the page's JS reads /r/:id from the path and opens
    # the receipt sheet (fetching the owner-scoped /me/api/receipts/:id). The id
    # is not used server-side here — ownership is enforced by that JSON endpoint.
    return _render_user_portal_response(
        request, login_return_to=f"/r/{request_id}"
    )


@app.get("/status", include_in_schema=False)
async def status_dashboard_index(request: Request):
    return await _render_status_html(request)


@app.get("/firehose", include_in_schema=False)
async def status_dashboard_firehose(request: Request):
    """Backwards compatibility: redirect old `/firehose` URL to `/status`."""
    return Response(
        status_code=301,
        headers={"location": "/status", "cache-control": "no-store"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def status_favicon():
    # No bundled favicon now that the Next.js export is gone. 204 No Content.
    return Response(status_code=204)


# =============================================================================
# /v1/responses — OpenAI Responses API (spring 2025 surface).
# Implemented as a translation layer over /v1/chat/completions. Stateful
# conversations (previous_response_id + store=true) are persisted in
# data/responses.db. Hosted tools (web_search, file_search, code_interpreter,
# computer_use) are rejected with 400 — the proxy backs onto a local llama
# server with no first-party hosted-tool surface.
# =============================================================================

async def _responses_prune_loop(stop_event: asyncio.Event) -> None:
    """Hourly prune of expired Responses rows. Bounded disk."""
    interval_s = 3600
    while not stop_event.is_set():
        try:
            deleted = responses_store.prune()
            if deleted:
                print(f"[proxy] responses_store: pruned {deleted} expired row(s)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[proxy] responses_store: prune failed: {e}", flush=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


def _responses_error(message: str, *, code: str = "invalid_request_error", param: Optional[str] = None, status: int = 400) -> Response:
    payload = {"error": {"message": message, "type": code, "param": param, "code": code}}
    return Response(
        content=json.dumps(payload),
        status_code=status,
        media_type="application/json",
    )


async def _build_prior_messages(previous_response_id: Optional[str], auth_context) -> list[dict]:
    """Walk the stored chain for previous_response_id and rebuild messages.
    No auth scoping (any client with the response id can chain); revisit when
    multi-tenant becomes a concern."""
    if not previous_response_id:
        return []
    chain = responses_store.walk_chain(previous_response_id)
    if not chain:
        return []
    return responses_translator.stored_chain_to_messages(chain)


def _api_key_name(auth_context) -> Optional[str]:
    try:
        ident = getattr(auth_context, "identity", None)
        if ident is None:
            return None
        return getattr(ident, "name", None)
    except Exception:
        return None


async def _forward_chat_completions(
    request: Request,
    auth_context,
    cc_body: dict,
    *,
    stream: bool,
) -> httpx.Response | tuple[httpx.AsyncClient, httpx.Response]:
    """Send a translated Chat Completions request through the same in-process
    routing the catch-all proxy_request uses, by making an HTTP loopback call
    to ourselves on 127.0.0.1:PROXY_PORT. This reuses every routing decision
    (model resolution, vision detection, backend swapping) without
    duplicating logic.

    For streaming, returns (client, resp) so the caller can iterate. For
    non-streaming, returns the httpx.Response with the body already read.
    """
    # Loopback to /v1/chat/completions — inner forwarder already enforces
    # UPSTREAM_READ_TIMEOUT against the LLM backend, so the outer read here
    # only needs to bound the in-process round-trip (loopback is localhost
    # and proxy code is non-blocking). Match UPSTREAM_READ_TIMEOUT so this
    # never trips before the inner timeout does.
    loopback_request = build_chat_loopback_request(
        proxy_port=PROXY_PORT,
        body=cc_body,
        internal_forward_header=_INTERNAL_PROTOCOL_FORWARD_HEADER,
        internal_forward_secret=_INTERNAL_PROTOCOL_FORWARD_SECRET,
        source_headers=request.headers,
        read_timeout=UPSTREAM_READ_TIMEOUT,
    )
    client = httpx.AsyncClient(timeout=loopback_request.timeout)
    if stream:
        req = client.build_request(
            "POST",
            loopback_request.url,
            headers=loopback_request.headers,
            content=loopback_request.body,
        )
        resp = await client.send(req, stream=True)
        return client, resp
    try:
        resp = await client.post(
            loopback_request.url,
            headers=loopback_request.headers,
            content=loopback_request.body,
        )
        return resp
    finally:
        if not stream:
            await client.aclose()


async def _forward_embeddings(
    request: Request,
    auth_context,
    embeddings_body: dict,
) -> httpx.Response:
    """Loop an embeddings request through the OpenAI-compatible route.

    Ollama and future protocol frontends should not know which concrete
    embedding provider is hot; the `/v1/embeddings` route owns model alias
    resolution, profile policy, and backend health.
    """
    loopback_request = build_embeddings_loopback_request(
        proxy_port=PROXY_PORT,
        body=embeddings_body,
        internal_forward_header=_INTERNAL_PROTOCOL_FORWARD_HEADER,
        internal_forward_secret=_INTERNAL_PROTOCOL_FORWARD_SECRET,
        source_headers=request.headers,
        read_timeout=EMBEDDINGS_TIMEOUT,
    )
    client = httpx.AsyncClient(timeout=loopback_request.timeout)
    try:
        return await client.post(
            loopback_request.url,
            headers=loopback_request.headers,
            content=loopback_request.body,
        )
    finally:
        await client.aclose()


async def _drain_streaming_upstream_error(
    client: httpx.AsyncClient,
    upstream: httpx.Response,
    *,
    fallback: str,
) -> str:
    body_bytes = b""
    try:
        async for chunk in upstream.aiter_bytes():
            body_bytes += chunk
            if len(body_bytes) > 8192:
                break
    finally:
        try:
            await upstream.aclose()
        finally:
            await client.aclose()
    text = body_bytes.decode("utf-8", errors="replace")
    if not text:
        return fallback
    try:
        payload = json.loads(text)
    except Exception:
        return text[:1000]
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        if isinstance(error, str) and error:
            return error
    return text[:1000] or fallback


async def _iter_openai_sse_json(upstream: httpx.Response):
    """Yield decoded JSON payloads from OpenAI-style ``data:`` SSE lines."""
    buffer = ""

    async def parse_line(line: str):
        line = line.strip()
        if not line or not line.startswith("data:"):
            return None
        payload = line.removeprefix("data:").strip()
        if payload == "[DONE]":
            return StopAsyncIteration
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    async for chunk in upstream.aiter_bytes():
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            parsed = await parse_line(line)
            if parsed is StopAsyncIteration:
                return
            if parsed is not None:
                yield parsed

    if buffer.strip():
        parsed = await parse_line(buffer)
        if parsed is not None and parsed is not StopAsyncIteration:
            yield parsed


def _response_json_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        raw = getattr(response, "body", None)
        if raw is None:
            raw = getattr(response, "text", "") or ""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw or "{}")
        except Exception:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _embedding_dimensions_error(message: str) -> Response:
    return openai_error_response(
        message,
        status_code=400,
        error_type="invalid_request_error",
        param="dimensions",
        code="invalid_value",
    )


def _requested_embedding_dimensions(body: Mapping[str, Any] | None) -> tuple[int | None, Response | None]:
    if not isinstance(body, Mapping) or "dimensions" not in body:
        return None, None
    dimensions = body.get("dimensions")
    if not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0:
        return None, _embedding_dimensions_error("dimensions must be a positive integer")
    return dimensions, None


def _truncate_and_normalize_embedding_values(values: list[Any], dimensions: int) -> list[float] | None:
    if dimensions > len(values):
        return None
    try:
        truncated = [float(value) for value in values[:dimensions]]
    except (TypeError, ValueError):
        return None
    norm = math.sqrt(sum(value * value for value in truncated))
    if norm <= 0:
        return truncated
    return [value / norm for value in truncated]


def _apply_embedding_dimensions(response_json: dict[str, Any], dimensions: int) -> Response | None:
    data = response_json.get("data")
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        embedding = item.get("embedding")
        if not isinstance(embedding, list):
            continue
        normalized = _truncate_and_normalize_embedding_values(embedding, dimensions)
        if normalized is None:
            return _embedding_dimensions_error(
                "dimensions must be less than or equal to the native embedding length"
            )
        item["embedding"] = normalized
    return None


def _response_error_message(response: Any, fallback: str) -> str:
    payload = _response_json_payload(response)
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    if isinstance(error, str) and error:
        return error
    text = getattr(response, "text", "") or ""
    if isinstance(text, str) and text:
        return text[:1000]
    return fallback


def _json_error_response(
    payload: Mapping[str, Any],
    *,
    status_code: int,
) -> Response:
    return Response(
        content=json.dumps(dict(payload)),
        status_code=status_code,
        media_type="application/json",
    )


def _extension_validation_error_response(
    rejected: Mapping[str, str],
    *,
    param: str = "extensions",
) -> Response:
    return _json_error_response(
        {
            "error": {
                "message": "Invalid gateway extension controls.",
                "type": "invalid_request_error",
                "param": param,
                "code": "invalid_gateway_extensions",
                "rejected": dict(rejected),
            }
        },
        status_code=400,
    )


def _anthropic_error_response(
    message: str,
    *,
    status_code: int = 400,
    error_type: str = "invalid_request_error",
) -> Response:
    return _json_error_response(
        {
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            },
        },
        status_code=status_code,
    )


def _ollama_error_response(message: str, *, status_code: int = 400) -> Response:
    return _json_error_response({"error": message}, status_code=status_code)


def _protocol_policy_decision(
    auth_context: AuthContext,
    *,
    protocol: str,
    intent: str,
    model: str | None = None,
) -> PolicyDecision:
    return evaluate_and_apply(
        auth_context.identity,
        make_policy_request(protocol=protocol, intent=intent, model=model),
    )


async def _read_json_object(request: Request) -> dict[str, Any] | Response:
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({
                "error": {
                    "message": "Invalid JSON body",
                    "type": "invalid_request_error",
                }
            }),
            status_code=400,
            media_type="application/json",
        )
    if not isinstance(body, dict):
        return Response(
            content=json.dumps({
                "error": {
                    "message": "Request body must be a JSON object",
                    "type": "invalid_request_error",
                }
            }),
            status_code=400,
            media_type="application/json",
        )
    return body


@app.post("/anthropic/v1/messages/count_tokens")
@app.post("/v1/messages/count_tokens")
async def anthropic_count_tokens(request: Request):
    """Anthropic Messages count_tokens-compatible estimator."""
    auth_context = authenticate_request(request, "/anthropic/v1/messages/count_tokens")
    if isinstance(auth_context, Response):
        return auth_context

    body = await _read_json_object(request)
    if isinstance(body, Response):
        return _anthropic_error_response("Invalid JSON body", status_code=400)

    try:
        payload = anthropic_protocol.estimate_count_tokens(body)
        canonical = anthropic_protocol.messages_request_to_canonical(
            {**body, "max_tokens": body.get("max_tokens") or 1}
        )
    except anthropic_protocol.AnthropicTranslationError as e:
        return _json_error_response(e.error_body, status_code=e.http_status)

    decision = _protocol_policy_decision(
        auth_context,
        protocol="anthropic.messages",
        intent="chat.messages.count_tokens",
        model=canonical.get("model"),
    )
    if decision.denied:
        reason = decision.reasons[0] if decision.reasons else "request denied by app profile"
        return _anthropic_error_response(
            reason,
            status_code=403,
            error_type="permission_error",
        )

    return _json_error_response(payload, status_code=200)


@app.post("/anthropic/v1/messages")
@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages-compatible frontend over chat completions."""
    auth_context = authenticate_request(request, "/v1/messages")
    if isinstance(auth_context, Response):
        return auth_context

    body = await _read_json_object(request)
    if isinstance(body, Response):
        return _anthropic_error_response("Invalid JSON body", status_code=400)

    try:
        canonical = anthropic_protocol.messages_request_to_canonical(body)
    except anthropic_protocol.AnthropicTranslationError as e:
        return _json_error_response(e.error_body, status_code=e.http_status)

    decision = _protocol_policy_decision(
        auth_context,
        protocol="anthropic.messages",
        intent="chat.messages",
        model=canonical.get("model"),
    )
    if decision.denied:
        reason = decision.reasons[0] if decision.reasons else "request denied by app profile"
        return _anthropic_error_response(
            reason,
            status_code=403,
            error_type="permission_error",
        )

    state.update_activity()
    if canonical.get("stream"):
        client, upstream = await _forward_chat_completions(
            request,
            auth_context,
            canonical,
            stream=True,
        )
        if upstream.status_code >= 400:
            return _anthropic_error_response(
                await _drain_streaming_upstream_error(
                    client,
                    upstream,
                    fallback="Upstream chat-completions error.",
                ),
                status_code=upstream.status_code,
                error_type="api_error",
            )

        translator = anthropic_protocol.MessagesStreamTranslator(
            request_model=canonical.get("model")
        )

        async def event_stream():
            try:
                async for event in _iter_openai_sse_json(upstream):
                    for frame in translator.feed(event):
                        yield frame
                for frame in translator.finish():
                    yield frame
            except Exception as e:  # noqa: BLE001
                payload = {
                    "type": "error",
                    "error": {"type": "api_error", "message": str(e)},
                }
                yield f"event: error\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
            finally:
                try:
                    await upstream.aclose()
                finally:
                    await client.aclose()

        return StreamingResponse(
            event_stream(),
            status_code=200,
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    upstream = await _forward_chat_completions(
        request,
        auth_context,
        canonical,
        stream=False,
    )
    if getattr(upstream, "status_code", 500) >= 400:
        return _anthropic_error_response(
            _response_error_message(upstream, "Upstream chat-completions error."),
            status_code=int(getattr(upstream, "status_code", 502) or 502),
            error_type="api_error",
        )

    try:
        payload = anthropic_protocol.canonical_response_to_messages(
            _response_json_payload(upstream),
            request_model=body.get("model"),
        )
    except anthropic_protocol.AnthropicTranslationError as e:
        return _json_error_response(e.error_body, status_code=e.http_status)
    return _json_error_response(payload, status_code=200)


def _ollama_native_annotation_error(canonical: Mapping[str, Any]) -> Response | None:
    if canonical.get("__ollama_raw__"):
        return _ollama_error_response(
            "unsupported operation: raw prompting is not available through this frontend",
            status_code=400,
        )
    native_options = canonical.get("__ollama_options__")
    if isinstance(native_options, Mapping) and native_options:
        names = ", ".join(sorted(str(k) for k in native_options.keys()))
        return _ollama_error_response(
            f"unsupported operation: native Ollama options are not mapped yet: {names}",
            status_code=400,
        )
    return None


def _ollama_policy_error_response(
    auth_context: AuthContext,
    canonical: dict[str, Any],
    *,
    protocol: str,
    intent: str,
) -> Response | None:
    annotation_error = _ollama_native_annotation_error(canonical)
    if annotation_error is not None:
        return annotation_error

    decision = _protocol_policy_decision(
        auth_context,
        protocol=protocol,
        intent=intent,
        model=canonical.get("model"),
    )
    if decision.denied:
        reason = decision.reasons[0] if decision.reasons else "request denied by app profile"
        return _ollama_error_response(reason, status_code=403)
    return None


async def _ollama_forward_non_streaming(
    request: Request,
    auth_context: AuthContext,
    canonical: dict[str, Any],
    *,
    protocol: str,
    intent: str,
) -> Response | dict[str, Any]:
    policy_error = _ollama_policy_error_response(
        auth_context,
        canonical,
        protocol=protocol,
        intent=intent,
    )
    if policy_error is not None:
        return policy_error

    state.update_activity()
    upstream = await _forward_chat_completions(
        request,
        auth_context,
        canonical,
        stream=False,
    )
    if getattr(upstream, "status_code", 500) >= 400:
        return _ollama_error_response(
            _response_error_message(upstream, "upstream chat-completions error"),
            status_code=int(getattr(upstream, "status_code", 502) or 502),
        )
    return _response_json_payload(upstream)


async def _ollama_forward_streaming(
    request: Request,
    auth_context: AuthContext,
    canonical: dict[str, Any],
    *,
    protocol: str,
    intent: str,
    translate_chunk: Callable[[dict[str, Any]], dict[str, Any]],
) -> Response:
    policy_error = _ollama_policy_error_response(
        auth_context,
        canonical,
        protocol=protocol,
        intent=intent,
    )
    if policy_error is not None:
        return policy_error

    state.update_activity()
    client, upstream = await _forward_chat_completions(
        request,
        auth_context,
        canonical,
        stream=True,
    )
    if upstream.status_code >= 400:
        return _ollama_error_response(
            await _drain_streaming_upstream_error(
                client,
                upstream,
                fallback="upstream chat-completions error",
            ),
            status_code=upstream.status_code,
        )

    async def ndjson_stream():
        try:
            async for event in _iter_openai_sse_json(upstream):
                payload = translate_chunk(event)
                yield json.dumps(payload, separators=(",", ":")) + "\n"
        finally:
            try:
                await upstream.aclose()
            finally:
                await client.aclose()

    return StreamingResponse(
        ndjson_stream(),
        status_code=200,
        media_type="application/x-ndjson",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


async def _ollama_forward_embeddings(
    request: Request,
    auth_context: AuthContext,
    canonical: dict[str, Any],
    *,
    protocol: str,
) -> Response | dict[str, Any]:
    policy_error = _ollama_policy_error_response(
        auth_context,
        canonical,
        protocol=protocol,
        intent="text.embeddings",
    )
    if policy_error is not None:
        return policy_error

    state.update_activity()
    upstream = await _forward_embeddings(request, auth_context, canonical)
    if getattr(upstream, "status_code", 500) >= 400:
        return _ollama_error_response(
            _response_error_message(upstream, "upstream embeddings error"),
            status_code=int(getattr(upstream, "status_code", 502) or 502),
        )
    return _response_json_payload(upstream)


async def _ollama_model_catalog_summaries(
    request: Request | None,
    auth_context: AuthContext,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    litellm_runtime = _litellm_runtime_config()
    if litellm_runtime.enabled and request is not None:
        payload = await _litellm_models_payload(
            request,
            auth_context,
            runtime_config=litellm_runtime,
        )
        if isinstance(payload, Mapping):
            entries.extend(_litellm_model_summaries_from_payload(payload))

    entries.extend(_current_loaded_model_entries())
    filtered, _ = await _filter_model_list_for_profile(entries, auth_context)
    await _annotate_model_context_windows(filtered)
    return filtered


def _ollama_summary_matches_model(summary: Mapping[str, Any], model: str) -> bool:
    requested = normalize_model_id(model)
    if requested.endswith(":latest"):
        requested_without_latest = requested.removesuffix(":latest")
    else:
        requested_without_latest = requested
    for key in ("id", "name", "model"):
        value = summary.get(key)
        if not isinstance(value, str) or not value:
            continue
        normalized = normalize_model_id(value)
        if normalized in {requested, requested_without_latest}:
            return True
        if ":" not in normalized and f"{normalized}:latest" == requested:
            return True
    return False


async def _ollama_loaded_model_summaries(
    auth_context: AuthContext,
) -> list[dict[str, Any]]:
    if _litellm_runtime_config().enabled:
        return []

    try:
        active = await get_active_backend()
    except Exception:
        active = None
    loaded_ids = await _current_loaded_llm_model_ids(active)

    summaries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for loaded_id in sorted(loaded_ids):
        name = os.path.basename(loaded_id) if loaded_id.startswith("/") else loaded_id
        if not name or normalize_model_id(name) in seen:
            continue
        entry: dict[str, Any] = {
            "id": name,
            "object": "model",
            "owned_by": active or "vivid-fish",
            "created": _entry_created_ts(loaded_id if loaded_id.startswith("/") else None),
            "vivid_fish_loaded": True,
            "vivid_fish_backend": active or "",
        }
        if loaded_id.startswith("/"):
            try:
                entry["size"] = os.path.getsize(loaded_id)
                entry["size_vram"] = entry["size"]
            except OSError:
                pass
        summaries.append(entry)
        seen.add(normalize_model_id(name))

    return summaries


def _ollama_model_policy_response(
    auth_context: AuthContext,
    *,
    intent: str,
    model: str | None = None,
) -> Response | None:
    decision = _protocol_policy_decision(
        auth_context,
        protocol="ollama.models",
        intent=intent,
        model=model,
    )
    if decision.denied:
        reason = decision.reasons[0] if decision.reasons else "request denied by app profile"
        return _ollama_error_response(reason, status_code=403)
    return None


@app.post("/ollama/api/chat")
@app.post("/api/chat")
async def ollama_chat(request: Request):
    """Ollama `/api/chat` frontend over chat completions."""
    auth_context = authenticate_request(request, "/api/chat")
    if isinstance(auth_context, Response):
        return auth_context

    body = await _read_json_object(request)
    if isinstance(body, Response):
        return _ollama_error_response("invalid JSON body", status_code=400)

    try:
        canonical = ollama_protocol.chat_request_to_canonical(body)
    except ollama_protocol.OllamaTranslationError as e:
        return _ollama_error_response(str(e), status_code=e.http_status)

    if canonical.get("stream"):
        return await _ollama_forward_streaming(
            request,
            auth_context,
            canonical,
            protocol="ollama.chat",
            intent="chat.completions",
            translate_chunk=lambda event: ollama_protocol.canonical_stream_chunk_to_chat(
                event,
                request_model=body.get("model"),
            ),
        )

    upstream = await _ollama_forward_non_streaming(
        request,
        auth_context,
        canonical,
        protocol="ollama.chat",
        intent="chat.completions",
    )
    if isinstance(upstream, Response):
        return upstream
    return _json_error_response(
        ollama_protocol.canonical_response_to_chat(
            upstream,
            request_model=body.get("model"),
        ),
        status_code=200,
    )


@app.post("/ollama/api/generate")
@app.post("/api/generate")
async def ollama_generate(request: Request):
    """Ollama `/api/generate` frontend over chat completions."""
    auth_context = authenticate_request(request, "/api/generate")
    if isinstance(auth_context, Response):
        return auth_context

    body = await _read_json_object(request)
    if isinstance(body, Response):
        return _ollama_error_response("invalid JSON body", status_code=400)

    try:
        canonical = ollama_protocol.generate_request_to_canonical(body)
    except ollama_protocol.OllamaTranslationError as e:
        return _ollama_error_response(str(e), status_code=e.http_status)

    if canonical.get("stream"):
        return await _ollama_forward_streaming(
            request,
            auth_context,
            canonical,
            protocol="ollama.generate",
            intent="chat.generate",
            translate_chunk=lambda event: ollama_protocol.canonical_stream_chunk_to_generate(
                event,
                request_model=body.get("model"),
            ),
        )

    upstream = await _ollama_forward_non_streaming(
        request,
        auth_context,
        canonical,
        protocol="ollama.generate",
        intent="chat.generate",
    )
    if isinstance(upstream, Response):
        return upstream
    return _json_error_response(
        ollama_protocol.canonical_response_to_generate(
            upstream,
            request_model=body.get("model"),
        ),
        status_code=200,
    )


@app.post("/ollama/api/embed")
@app.post("/api/embed")
async def ollama_embed(request: Request):
    """Ollama current `/api/embed` frontend over OpenAI embeddings."""
    auth_context = authenticate_request(request, "/api/embed")
    if isinstance(auth_context, Response):
        return auth_context

    body = await _read_json_object(request)
    if isinstance(body, Response):
        return _ollama_error_response("invalid JSON body", status_code=400)

    try:
        canonical = ollama_protocol.embed_request_to_canonical(body)
    except ollama_protocol.OllamaTranslationError as e:
        return _ollama_error_response(str(e), status_code=e.http_status)

    upstream = await _ollama_forward_embeddings(
        request,
        auth_context,
        canonical,
        protocol="ollama.embeddings",
    )
    if isinstance(upstream, Response):
        return upstream
    return _json_error_response(
        ollama_protocol.canonical_response_to_embed(
            upstream,
            request_model=body.get("model"),
        ),
        status_code=200,
    )


@app.post("/ollama/api/embeddings")
@app.post("/api/embeddings")
async def ollama_embeddings(request: Request):
    """Ollama legacy `/api/embeddings` frontend over OpenAI embeddings."""
    auth_context = authenticate_request(request, "/api/embeddings")
    if isinstance(auth_context, Response):
        return auth_context

    body = await _read_json_object(request)
    if isinstance(body, Response):
        return _ollama_error_response("invalid JSON body", status_code=400)

    try:
        canonical = ollama_protocol.embeddings_request_to_canonical(body)
    except ollama_protocol.OllamaTranslationError as e:
        return _ollama_error_response(str(e), status_code=e.http_status)

    upstream = await _ollama_forward_embeddings(
        request,
        auth_context,
        canonical,
        protocol="ollama.embeddings",
    )
    if isinstance(upstream, Response):
        return upstream
    return _json_error_response(
        ollama_protocol.canonical_response_to_embeddings(
            upstream,
            request_model=body.get("model"),
        ),
        status_code=200,
    )


@app.get("/ollama/api/tags")
@app.get("/api/tags")
async def ollama_tags(request: Request):
    """Ollama model-list frontend over the proxy's configured model catalog."""
    auth_context = authenticate_request(request, "/api/tags")
    if isinstance(auth_context, Response):
        return auth_context

    decision = _protocol_policy_decision(
        auth_context,
        protocol="ollama.tags",
        intent="models.list",
    )
    if decision.denied:
        reason = decision.reasons[0] if decision.reasons else "request denied by app profile"
        return _ollama_error_response(reason, status_code=403)

    return _json_error_response(
        ollama_protocol.tags_response_from_model_summaries(
            await _ollama_model_catalog_summaries(request, auth_context)
        ),
        status_code=200,
    )


@app.post("/ollama/api/show")
@app.post("/api/show")
async def ollama_show(request: Request):
    """Ollama `/api/show` projected from the gateway's visible LLM catalog."""
    auth_context = authenticate_request(request, "/api/show")
    if isinstance(auth_context, Response):
        return auth_context

    body = await _read_json_object(request)
    if isinstance(body, Response):
        return _ollama_error_response("invalid JSON body", status_code=400)
    model = body.get("model")
    if not isinstance(model, str) or not model:
        return _ollama_error_response("'model' is required", status_code=400)

    policy_error = _ollama_model_policy_response(
        auth_context,
        intent="models.show",
        model=model,
    )
    if policy_error is not None:
        return policy_error

    for summary in await _ollama_model_catalog_summaries(request, auth_context):
        if _ollama_summary_matches_model(summary, model):
            return _json_error_response(
                ollama_protocol.show_response_from_model_summary(summary),
                status_code=200,
            )
    return _ollama_error_response(f"model not found: {model}", status_code=404)


@app.get("/ollama/api/ps")
@app.get("/api/ps")
async def ollama_ps(request: Request):
    """Ollama `/api/ps` projected from the gateway's currently loaded LLM."""
    auth_context = authenticate_request(request, "/api/ps")
    if isinstance(auth_context, Response):
        return auth_context
    policy_error = _ollama_model_policy_response(
        auth_context,
        intent="models.running",
    )
    if policy_error is not None:
        return policy_error
    return _json_error_response(
        ollama_protocol.ps_response_from_model_summaries(
            await _ollama_loaded_model_summaries(auth_context)
        ),
        status_code=200,
    )


@app.get("/ollama/api/version")
@app.get("/api/version")
async def ollama_version(request: Request):
    """Ollama `/api/version` compatibility response."""
    auth_context = authenticate_request(request, "/api/version")
    if isinstance(auth_context, Response):
        return auth_context
    policy_error = _ollama_model_policy_response(
        auth_context,
        intent="models.version",
    )
    if policy_error is not None:
        return policy_error
    return _json_error_response(
        ollama_protocol.version_response("vivid-fish-ai-gateway"),
        status_code=200,
    )


async def _ollama_unsupported_model_mutation(
    request: Request,
    *,
    canonical_path: str,
    intent: str,
) -> Response:
    auth_context = authenticate_request(request, canonical_path)
    if isinstance(auth_context, Response):
        return auth_context
    policy_error = _ollama_model_policy_response(auth_context, intent=intent)
    if policy_error is not None:
        return policy_error
    return _json_error_response(
        ollama_protocol.unsupported_operation_error(
            canonical_path,
            detail=(
                "the gateway exposes model catalog and running-model status, "
                "but does not mutate local model files through the Ollama API"
            ),
        ),
        status_code=501,
    )


@app.post("/ollama/api/create")
@app.post("/api/create")
async def ollama_create(request: Request):
    return await _ollama_unsupported_model_mutation(
        request,
        canonical_path="/api/create",
        intent="models.create",
    )


@app.post("/ollama/api/copy")
@app.post("/api/copy")
async def ollama_copy(request: Request):
    return await _ollama_unsupported_model_mutation(
        request,
        canonical_path="/api/copy",
        intent="models.copy",
    )


@app.post("/ollama/api/pull")
@app.post("/api/pull")
async def ollama_pull(request: Request):
    return await _ollama_unsupported_model_mutation(
        request,
        canonical_path="/api/pull",
        intent="models.pull",
    )


@app.post("/ollama/api/push")
@app.post("/api/push")
async def ollama_push(request: Request):
    return await _ollama_unsupported_model_mutation(
        request,
        canonical_path="/api/push",
        intent="models.push",
    )


@app.delete("/ollama/api/delete")
@app.delete("/api/delete")
async def ollama_delete(request: Request):
    return await _ollama_unsupported_model_mutation(
        request,
        canonical_path="/api/delete",
        intent="models.delete",
    )


@app.post("/openai/v1/responses")
@app.post("/v1/responses")
async def create_response(request: Request):
    """Create a Response. Translates to Chat Completions and forwards."""
    auth_context = authenticate_request(request, "/v1/responses")
    if isinstance(auth_context, Response):
        return auth_context

    state.update_activity()

    try:
        raw = await request.body()
        try:
            req_body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            return _responses_error(f"Invalid JSON in request body: {e}", code="invalid_request_error")
        if not isinstance(req_body, dict):
            return _responses_error("Request body must be a JSON object.", code="invalid_request_error")

        decision = _protocol_policy_decision(
            auth_context,
            protocol="openai.chat",
            intent="chat.responses",
            model=req_body.get("model") if isinstance(req_body.get("model"), str) else None,
        )
        if decision.denied:
            return _policy_denied_response(decision)

        try:
            extension_result = validate_responses_extensions(req_body)
        except ExtensionError as e:
            return _responses_error(str(e), code="invalid_request_error", param="extensions")
        if not extension_result.ok:
            return _extension_validation_error_response(extension_result.rejected)
        req_body = extension_result.request

        # Translate
        try:
            prior_messages = await _build_prior_messages(
                req_body.get("previous_response_id"), auth_context
            )
            cc_body = responses_translator.to_chat_completions(
                req_body, prior_messages=prior_messages
            )
        except responses_translator.ResponsesTranslationError as e:
            return _responses_error(e.message, code=e.code, param=e.param)

        # Reject obviously-malformed: no messages at all.
        if not cc_body.get("messages"):
            return _responses_error(
                "Request must include `input` (or a chain via previous_response_id).",
                param="input",
            )

        response_id = responses_translator.new_response_id()
        created_at = int(time.time())
        stream = bool(req_body.get("stream"))
        store = req_body.get("store")
        if store is None:
            store = True

        if stream:
            # Streaming: open upstream stream, translate chunks, emit Responses SSE.
            client, upstream = await _forward_chat_completions(
                request, auth_context, cc_body, stream=True
            )
            if upstream.status_code >= 400:
                # Drain the body for a useful error.
                body_bytes = b""
                try:
                    async for chunk in upstream.aiter_bytes():
                        body_bytes += chunk
                        if len(body_bytes) > 8192:
                            break
                finally:
                    await upstream.aclose()
                    await client.aclose()
                try:
                    upstream_json = json.loads(body_bytes.decode("utf-8", errors="replace") or "{}")
                except Exception:
                    upstream_json = {"error": {"message": body_bytes.decode("utf-8", errors="replace")[:1000]}}
                return Response(
                    content=json.dumps({"error": {
                        "message": (upstream_json.get("error") or {}).get("message") or "Upstream chat-completions error.",
                        "type": "upstream_error",
                        "code": "upstream_error",
                    }}),
                    status_code=upstream.status_code,
                    media_type="application/json",
                )

            translator = responses_translator.StreamTranslator(
                response_id=response_id,
                created_at=created_at,
                original_request=req_body,
            )

            async def event_stream():
                final_obj: Optional[dict] = None
                try:
                    for ev in translator.start():
                        yield ev
                    async for chunk in upstream.aiter_bytes():
                        for ev in translator.feed(chunk):
                            yield ev
                    for ev in translator.finish():
                        yield ev
                    final_obj = translator.build_final_response()
                except Exception as e:  # noqa: BLE001
                    err_ev = responses_translator.sse_pack("response.error", {
                        "type": "response.error",
                        "sequence_number": 9999,
                        "error": {"message": str(e), "type": "internal_error", "code": "internal_error"},
                    })
                    yield err_ev
                finally:
                    try:
                        await upstream.aclose()
                    except Exception:
                        pass
                    try:
                        await client.aclose()
                    except Exception:
                        pass
                    # Persist if requested
                    if store and final_obj is not None:
                        try:
                            responses_store.save(
                                response_id,
                                created_at=created_at,
                                model=final_obj.get("model"),
                                previous_response_id=req_body.get("previous_response_id"),
                                request_json=req_body,
                                response_json=final_obj,
                                api_key_name=_api_key_name(auth_context),
                            )
                        except Exception as e:  # noqa: BLE001
                            print(f"[proxy] responses_store.save failed: {e}", flush=True)

            return StreamingResponse(
                event_stream(),
                status_code=200,
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
            )

        # Non-streaming
        upstream = await _forward_chat_completions(request, auth_context, cc_body, stream=False)
        if upstream.status_code >= 400:
            try:
                upstream_json = upstream.json()
            except Exception:
                upstream_json = {"error": {"message": upstream.text[:1000]}}
            return Response(
                content=json.dumps({"error": {
                    "message": (upstream_json.get("error") or {}).get("message") or "Upstream chat-completions error.",
                    "type": "upstream_error",
                    "code": "upstream_error",
                }}),
                status_code=upstream.status_code,
                media_type="application/json",
            )

        try:
            cc_json = upstream.json()
        except Exception as e:
            return _responses_error(
                f"Upstream returned non-JSON: {e}",
                code="upstream_error",
                status=502,
            )

        resp_json = responses_translator.from_chat_completions(
            cc_json,
            response_id=response_id,
            created_at=created_at,
            original_request=req_body,
        )

        if store:
            try:
                responses_store.save(
                    response_id,
                    created_at=created_at,
                    model=resp_json.get("model"),
                    previous_response_id=req_body.get("previous_response_id"),
                    request_json=req_body,
                    response_json=resp_json,
                    api_key_name=_api_key_name(auth_context),
                )
            except Exception as e:  # noqa: BLE001
                print(f"[proxy] responses_store.save failed: {e}", flush=True)

        return Response(
            content=json.dumps(resp_json),
            status_code=200,
            media_type="application/json",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] /v1/responses internal error: {e}", flush=True)
        return _responses_error(f"Internal error: {e}", code="internal_error", status=500)


@app.get("/openai/v1/responses/{response_id}")
@app.get("/v1/responses/{response_id}")
async def retrieve_response(response_id: str, request: Request):
    auth_context = authenticate_request(request, "/v1/responses")
    if isinstance(auth_context, Response):
        return auth_context
    decision = _protocol_policy_decision(
        auth_context,
        protocol="openai.chat",
        intent="chat.responses",
    )
    if decision.denied:
        return _policy_denied_response(decision)
    try:
        row = responses_store.get(response_id)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] responses_store.get failed: {e}", flush=True)
        return _responses_error(f"Internal error: {e}", code="internal_error", status=500)
    if not row:
        return _responses_error(
            f"Response with id '{response_id}' not found.",
            code="not_found",
            status=404,
        )
    resp_json = row.get("response_json") or {}
    return Response(
        content=json.dumps(resp_json),
        status_code=200,
        media_type="application/json",
    )


@app.post("/openai/v1/responses/{response_id}/cancel")
@app.post("/v1/responses/{response_id}/cancel")
async def cancel_response(response_id: str, request: Request):
    auth_context = authenticate_request(request, "/v1/responses")
    if isinstance(auth_context, Response):
        return auth_context
    decision = _protocol_policy_decision(
        auth_context,
        protocol="openai.chat",
        intent="chat.responses",
    )
    if decision.denied:
        return _policy_denied_response(decision)
    return _responses_error(
        "Response cancellation is not supported for locally translated Responses.",
        code="unsupported_operation",
        status=400,
    )


@app.delete("/openai/v1/responses/{response_id}")
@app.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, request: Request):
    auth_context = authenticate_request(request, "/v1/responses")
    if isinstance(auth_context, Response):
        return auth_context
    decision = _protocol_policy_decision(
        auth_context,
        protocol="openai.chat",
        intent="chat.responses",
    )
    if decision.denied:
        return _policy_denied_response(decision)
    try:
        existed = responses_store.delete(response_id)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] responses_store.delete failed: {e}", flush=True)
        return _responses_error(f"Internal error: {e}", code="internal_error", status=500)
    if not existed:
        return _responses_error(
            f"Response with id '{response_id}' not found.",
            code="not_found",
            status=404,
        )
    return Response(
        content=json.dumps({
            "id": response_id,
            "object": "response.deleted",
            "deleted": True,
        }),
        status_code=200,
        media_type="application/json",
    )


_OPENAI_COMPAT_JSON_BODY_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": True,
                }
            }
        },
    },
    "responses": {
        "200": {
            "description": "OpenAI-compatible response proxied from the active local backend.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": True,
                    }
                }
            },
        }
    },
}

_OPENAI_PROXY_POLICY = route_registry.openai_proxy_policy(
    registry=_PROTOCOL_ROUTE_REGISTRY,
    proxy_config=config,
)

_INTERNAL_PROTOCOL_FORWARD_HEADER = "x-vivid-fish-internal-protocol-forward"
_INTERNAL_PROTOCOL_FORWARD_SECRET = secrets.token_urlsafe(32)
_INTERNAL_PROTOCOL_FORWARD_HOSTS = frozenset({"127.0.0.1", "::1"})


def _is_internal_protocol_forward(request: Request) -> bool:
    """Allow first-class protocol frontends to reuse chat routing internally.

    Anthropic and Ollama routes have already passed their own profile policy.
    Their loopback call to `/v1/chat/completions` should not add a second
    `openai.chat` permission requirement. The bypass is secret-bearing and
    loopback-only so public clients cannot opt out of OpenAI route policy.
    """
    if request.headers.get(_INTERNAL_PROTOCOL_FORWARD_HEADER) != _INTERNAL_PROTOCOL_FORWARD_SECRET:
        return False
    client = getattr(request, "client", None)
    return getattr(client, "host", None) in _INTERNAL_PROTOCOL_FORWARD_HOSTS


def _openai_proxy_policy_decision(
    auth_context: AuthContext,
    request_path: str,
    body: bytes,
) -> PolicyDecision | None:
    spec = _OPENAI_PROXY_POLICY.get(request_path)
    if spec is None:
        return None

    model: str | None = None
    try:
        body_json = json.loads(body)
        if isinstance(body_json, Mapping) and isinstance(body_json.get("model"), str):
            model = body_json["model"]
    except Exception:
        pass

    protocol, intent = spec
    return evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol=protocol,
            intent=intent,
            model=model,
            capability_exposure="default",
            capability_trust_tier="stable",
            capability_resource_tier="interactive",
        ),
    )


def _litellm_unconfigured_response() -> Response:
    return Response(
        content=json.dumps({
            "error": {
                "message": (
                    "LiteLLM execution is enabled, but the gateway service "
                    "registry has no enabled LiteLLM service with a base URL."
                ),
                "type": "service_unavailable",
                "code": "litellm_unconfigured",
            }
        }),
        status_code=503,
        media_type="application/json",
    )


def _litellm_runtime_config():
    try:
        registry = ADMIN_STORE.get_service_registry()
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] LiteLLM registry lookup failed: {e}", flush=True)
        registry = None
    return litellm_runtime_from_registry(
        registry,
        env=os.environ,
    )


def _toolcall_backend_hint(service_id: str) -> str:
    """Derive the backend hint string used for advisory messages.

    Prefers the stored ``preset`` field in config["backends"][service_id] when
    present (e.g. "vllm", "ollama", "tabbyapi").  Falls back to the service_id
    itself — many operators name their service after the backend (e.g. "vllm",
    "tabby").  The detection module does case-insensitive substring matching so
    either form works.
    """
    if not service_id:
        return "unknown"
    bcfg = (config.get("backends") or {}).get(service_id) or {}
    return str(bcfg.get("preset") or service_id)


def _per_backend_transform_ids(service_id: str) -> list[str]:
    """Return the list of transform ids declared on a backend's config (data).

    A service record may carry config["transforms"]: a list of registered
    transform ids (the engine-agnostic primitives + named built-ins). Pure DATA
    on the service — resolved and run by id, no engine-specific code. See
    docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md. Returns [] when absent/malformed.
    """
    if not service_id:
        return []
    bcfg = (config.get("backends") or {}).get(service_id) or {}
    raw = bcfg.get("transforms")
    if not isinstance(raw, list):
        return []
    return [str(t) for t in raw if isinstance(t, str) and t]


def _litellm_gateway_metadata(
    auth_context: AuthContext,
    *,
    request_path: str,
    service_id: str = "",
) -> dict[str, str]:
    metadata = {
        "execution": "litellm",
        "request-path": request_path,
        "profile": _profile_source_id(auth_context),
    }
    if service_id:
        metadata["service-id"] = service_id
    identity = auth_context.identity
    if identity is not None:
        metadata["app"] = str(identity.name)
        metadata["key-prefix"] = str(identity.key_hash or "")[:18]
    return metadata


def _start_litellm_route_receipt(
    *,
    request_path: str,
    auth_context: AuthContext,
    requested_model: str,
    protocol: str | None = None,
    intent: str | None = None,
) -> RouteReceipt:
    if protocol is None or intent is None:
        protocol, intent = _OPENAI_PROXY_POLICY.get(
            request_path,
            ("openai.chat", "chat.generate"),
        )
    profile = _profile_for_auth_context(auth_context)
    receipt = RouteReceipt(
        protocol=protocol,
        path=request_path,
        intent=intent,
        backend="litellm",
        requested_model=requested_model,
        app_profile=profile.id,
        app_id=auth_context.identity.app_id if auth_context.identity else None,
        api_key_name=auth_context.identity.name if auth_context.identity else None,
        visibility=profile.status_visibility.level,
    )
    receipt.set_policy_decision("allowed", source="gateway:profile")
    receipt.metadata.update({"execution_backend": "litellm"})
    return receipt


def _attach_litellm_response_metadata(
    receipt: RouteReceipt,
    headers: Mapping[str, str],
) -> None:
    keys = (
        "x-litellm-call-id",
        "x-litellm-model-id",
        "x-litellm-model-group",
        "x-litellm-model-api-base",
        "x-litellm-response-cost",
        "x-litellm-response-duration-ms",
        "x-litellm-attempted-retries",
        "x-litellm-attempted-fallbacks",
    )
    facts: dict[str, str] = {}
    lowered = {key.lower(): value for key, value in headers.items()}
    for key in keys:
        value = lowered.get(key)
        if value:
            facts[key.removeprefix("x-litellm-").replace("-", "_")] = value
    if facts:
        receipt.metadata["litellm"] = facts


def _normalize_litellm_error_envelope(
    payload: dict[str, Any],
    *,
    status_code: int,
) -> None:
    error = payload.get("error")
    if not isinstance(error, dict):
        return

    def is_empty(value: Any) -> bool:
        return value is None or value == "" or value == "None"

    message = str(error.get("message") or "")
    if "Invalid model name passed" in message:
        if is_empty(error.get("type")):
            error["type"] = "invalid_request_error"
        if is_empty(error.get("param")):
            error["param"] = "model"
        if is_empty(error.get("code")) or str(error.get("code")).isdigit():
            error["code"] = "model_not_found"
        return

    if is_empty(error.get("type")):
        error["type"] = "invalid_request_error" if 400 <= status_code < 500 else "api_error"
    if is_empty(error.get("param")):
        error["param"] = None


def _record_litellm_route_receipt(receipt: RouteReceipt) -> None:
    try:
        receipt_store.record(receipt)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Could not record LiteLLM route receipt: {e}", flush=True)


def _forward_query_without_client_api_key(raw_query: str) -> str:
    if not raw_query:
        return ""
    pairs = [
        (key, value)
        for key, value in parse_qsl(raw_query, keep_blank_values=True)
        if key != "api_key"
    ]
    return urlencode(pairs)


async def _forward_current_loaded_openai_request(
    request: Request,
    *,
    request_path: str,
    auth_context: AuthContext,
    body_json: Mapping[str, Any],
) -> Response:
    requested_alias = body_json.get("model")
    target, target_error = await _current_loaded_llm_target(auth_context)
    if target_error is not None:
        return target_error
    assert target is not None

    body_for_backend: Mapping[str, Any] = body_json
    if request_path == "/v1/chat/completions":
        try:
            extension_result = validate_chat_completions_extensions(body_json)
        except ExtensionError as e:
            return _extension_validation_error_response({"extensions": str(e)})
        if not extension_result.ok:
            return _extension_validation_error_response(extension_result.rejected)
        body_for_backend = extension_result.request

    body = copy.deepcopy(dict(body_for_backend))
    body.pop("_vf_requested_model", None)
    body["model"] = target.model

    if request_path == "/v1/chat/completions":
        if _is_use_loaded_fast_model_alias(requested_alias):
            body["reasoning_effort"] = "none"
        reasoning_error = _ReasoningEffortTransform().apply(body)
        if reasoning_error:
            return Response(
                content=json.dumps({"error": {
                    "message": reasoning_error,
                    "type": "invalid_request_error",
                    "param": "reasoning_effort",
                    "code": "invalid_value",
                }}),
                status_code=400,
                media_type="application/json",
            )

    query = _forward_query_without_client_api_key(request.url.query)
    url = f"{target.host}{request_path}"
    if query:
        url = f"{url}?{query}"

    headers = prepare_downstream_headers(
        request.headers,
        auth_context,
        target.backend,
        request_path,
    )
    headers.setdefault("content-type", "application/json")
    headers.setdefault("accept", "application/json")

    execution_request = LlmExecutionRequest(
        method=request.method,
        url=url,
        headers=headers,
        body=json.dumps(body).encode(),
        read_timeout=UPSTREAM_READ_TIMEOUT,
    )

    try:
        client, resp = await send_llm_execution_request(execution_request)
        content_type = resp.headers.get("content-type", "") or "application/json"
        excluded = ("content-length", "transfer-encoding", "content-type")
        response_headers = {
            k: v for k, v in resp.headers.items() if k.lower() not in excluded
        }
        response_headers["X-AI-Gateway-LLM-Execution"] = "current-loaded"
        response_headers["X-AI-Gateway-Backend"] = target.backend
        response_headers["X-AI-Gateway-Resolved-Model"] = target.model

        _tc_backend_hint = _toolcall_backend_hint(target.backend)
        _tc_had_tools = (
            request_path == "/v1/chat/completions"
            and _request_had_tools(body)
        )

        if "text/event-stream" in content_type:
            _tc_detector = _ToolcallStreamDetector(
                had_tools=_tc_had_tools,
                backend_hint=_tc_backend_hint,
            )

            async def stream_response():
                try:
                    async for chunk in _tc_detector.wrap(resp.aiter_bytes()):
                        if request_path == "/v1/chat/completions":
                            chunk = _normalize_chat_completion_sse_chunk(chunk)
                        yield chunk
                finally:
                    _tc_advisory = _tc_detector.advisory()
                    if _tc_advisory:
                        import logging as _logging
                        _logging.getLogger("gateway.toolcall").warning(
                            "event=toolcall.unlifted backend=%s family=%s remediation=%r",
                            _tc_backend_hint,
                            _tc_detector.detected_family,
                            _tc_advisory,
                        )
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                stream_response(),
                status_code=resp.status_code,
                headers=response_headers,
                media_type=content_type,
            )

        content = await resp.aread()
        await resp.aclose()
        await client.aclose()
        if "application/json" in content_type:
            try:
                response_json = json.loads(content)
                if isinstance(response_json, dict):
                    if 200 <= resp.status_code < 300 and request_path == "/v1/chat/completions":
                        _normalize_chat_completion_payload(response_json)
                        _normalize_toolcall_args(response_json)
                        _normalize_toolcall_finish_reason(response_json)
                        # Per-backend response transforms (data-driven; see ENGINE_KNOWLEDGE_BOUNDARY.md).
                        _resp_tx = _per_backend_transform_ids(target.backend)
                        if _resp_tx:
                            response_json = _run_response_pipeline(
                                _resp_tx, response_json, _TransformContext(), _TRANSFORM_REGISTRY,
                            )
                        _tc_family = _detect_unlifted_toolcalls(response_json, _tc_had_tools)
                        if _tc_family:
                            _tc_advisory = _toolcall_advisory_for_backend(_tc_backend_hint, _tc_family)
                            import logging as _logging
                            _logging.getLogger("gateway.toolcall").warning(
                                "event=toolcall.unlifted backend=%s family=%s remediation=%r",
                                _tc_backend_hint,
                                _tc_family,
                                _tc_advisory,
                            )
                            response_headers["x-gateway-toolcall-advisory"] = _tc_advisory
                    content = json.dumps(response_json).encode()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        return Response(
            content=content,
            status_code=resp.status_code,
            headers=response_headers,
            media_type=content_type,
        )
    except httpx.TimeoutException:
        return openai_error_response(
            "Current loaded LLM request timed out.",
            status_code=504,
            error_type="api_error",
            code="timeout",
        )
    except httpx.ConnectError:
        response = openai_error_response(
            f"Active backend {target.backend!r} is unavailable.",
            status_code=503,
            error_type="service_unavailable",
            code="backend_unavailable",
        )
        response.headers["Retry-After"] = "10"
        return response


async def _forward_litellm_proxy_request(
    request: Request,
    *,
    request_path: str,
    auth_context: AuthContext,
    body_json: Mapping[str, Any],
    runtime_config,
    requested_embedding_dimensions: int | None = None,
) -> Response:
    model_name = body_json.get("model")
    if not isinstance(model_name, str) or not model_name:
        return openai_error_response(
            "Missing required parameter: 'model'.",
            status_code=400,
            param="model",
            code="missing_required_parameter",
        )

    if _profile_target_activation(auth_context) == "existing_only":
        current_loaded_decision = await _current_loaded_model_policy_decision(
            auth_context,
            requested_model=model_name,
        )
        if current_loaded_decision is not None and current_loaded_decision.denied:
            return _policy_denied_response(current_loaded_decision)

    runtime = runtime_config
    if not runtime.enabled:
        return _litellm_unconfigured_response()

    receipt = _start_litellm_route_receipt(
        request_path=request_path,
        auth_context=auth_context,
        requested_model=model_name,
    )
    body_for_litellm = body_json
    if request_path == "/v1/chat/completions":
        try:
            extension_result = validate_chat_completions_extensions(body_json)
        except ExtensionError as e:
            return _extension_validation_error_response({"extensions": str(e)})
        if not extension_result.ok:
            return _extension_validation_error_response(extension_result.rejected)
        body_for_litellm = extension_result.request
    if (
        request_path == "/v1/embeddings"
        and requested_embedding_dimensions is not None
    ):
        body_for_litellm = dict(body_for_litellm)
        body_for_litellm.pop("dimensions", None)

    # Per-backend request transforms (engine-agnostic, data-driven):
    # a service record may declare config["transforms"] — a list of transform
    # ids resolved from the registry. Applied here so a backend's quirk handling
    # is DATA on the service, never engine-specific code in the dispatch path.
    # See docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md.
    _backend_transforms = _per_backend_transform_ids(runtime.service_id)
    if _backend_transforms:
        body_for_litellm = _run_request_pipeline(
            _backend_transforms,
            dict(body_for_litellm),
            _TransformContext(),
            _TRANSFORM_REGISTRY,
        )

    body = json.dumps(dict(body_for_litellm)).encode()
    gateway_metadata = _litellm_gateway_metadata(
        auth_context,
        request_path=request_path,
        service_id=runtime.service_id,
    )
    read_timeout = (
        EMBEDDINGS_TIMEOUT
        if request_path == "/v1/embeddings"
        else UPSTREAM_READ_TIMEOUT
    )
    execution_request = LlmExecutionRequest(
        method=request.method,
        url=litellm_url(
            base_url=runtime.base_url,
            path=request_path,
            query=request.url.query,
        ),
        headers=litellm_headers(
            request.headers,
            api_key=runtime.api_key,
            gateway_metadata=gateway_metadata,
        ),
        body=body,
        read_timeout=read_timeout,
    )

    try:
        client, resp = await send_llm_execution_request(execution_request)
        content_type = resp.headers.get("content-type", "") or "application/json"
        excluded = ("content-length", "transfer-encoding", "content-type")
        response_headers = {
            k: v for k, v in resp.headers.items() if k.lower() not in excluded
        }
        response_headers["X-AI-Gateway-LLM-Execution"] = "litellm"
        _attach_litellm_response_metadata(receipt, resp.headers)

        # Derive backend hint for toolcall advisory (preset id or service id).
        _tc_backend_hint = _toolcall_backend_hint(runtime.service_id)
        _tc_had_tools = (
            request_path == "/v1/chat/completions"
            and _request_had_tools(dict(body_json))
        )

        if "text/event-stream" in content_type:
            _tc_detector = _ToolcallStreamDetector(
                had_tools=_tc_had_tools,
                backend_hint=_tc_backend_hint,
            )

            async def stream_response():
                try:
                    async for chunk in _tc_detector.wrap(resp.aiter_bytes()):
                        if request_path == "/v1/chat/completions":
                            chunk = _normalize_chat_completion_sse_chunk(chunk)
                        yield chunk
                finally:
                    receipt.finish(
                        "succeeded" if 200 <= resp.status_code < 400 else "failed"
                    )
                    _record_litellm_route_receipt(receipt)
                    # Emit toolcall advisory via log at stream close.
                    _tc_advisory = _tc_detector.advisory()
                    if _tc_advisory:
                        import logging as _logging
                        _logging.getLogger("gateway.toolcall").warning(
                            "event=toolcall.unlifted backend=%s family=%s remediation=%r",
                            _tc_backend_hint,
                            _tc_detector.detected_family,
                            _tc_advisory,
                        )
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                stream_response(),
                status_code=resp.status_code,
                headers=response_headers,
                media_type=content_type,
            )

        content = await resp.aread()
        await resp.aclose()
        await client.aclose()
        if "application/json" in content_type:
            try:
                response_json = json.loads(content)
                if isinstance(response_json, dict):
                    if 200 <= resp.status_code < 300:
                        if request_path == "/v1/chat/completions":
                            # Step 4a: delegate to the registered transform function.
                            _normalize_chat_completion_payload(response_json)
                            # §6: structural normalization (args-stringify + finish-reason repair).
                            _normalize_toolcall_args(response_json)
                            _normalize_toolcall_finish_reason(response_json)
                            # Per-backend response transforms (data-driven; see ENGINE_KNOWLEDGE_BOUNDARY.md).
                            _resp_tx = _per_backend_transform_ids(runtime.service_id)
                            if _resp_tx:
                                response_json = _run_response_pipeline(
                                    _resp_tx, response_json, _TransformContext(), _TRANSFORM_REGISTRY,
                                )
                            # §5.2: detect unlifted tool calls and surface advisory.
                            _tc_family = _detect_unlifted_toolcalls(response_json, _tc_had_tools)
                            if _tc_family:
                                _tc_advisory = _toolcall_advisory_for_backend(_tc_backend_hint, _tc_family)
                                import logging as _logging
                                _logging.getLogger("gateway.toolcall").warning(
                                    "event=toolcall.unlifted backend=%s family=%s remediation=%r",
                                    _tc_backend_hint,
                                    _tc_family,
                                    _tc_advisory,
                                )
                                response_headers["x-gateway-toolcall-advisory"] = _tc_advisory
                        if (
                            request_path == "/v1/embeddings"
                            and requested_embedding_dimensions is not None
                        ):
                            dimensions_error = _apply_embedding_dimensions(
                                response_json,
                                requested_embedding_dimensions,
                            )
                            if dimensions_error is not None:
                                return dimensions_error
                    else:
                        _normalize_litellm_error_envelope(
                            response_json,
                            status_code=resp.status_code,
                        )
                    content = json.dumps(response_json).encode()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        receipt.finish("succeeded" if 200 <= resp.status_code < 400 else "failed")
        _record_litellm_route_receipt(receipt)
        return Response(
            content=content,
            status_code=resp.status_code,
            headers=response_headers,
            media_type=content_type,
        )
    except httpx.TimeoutException:
        receipt.finish("failed")
        receipt.add_warning("LiteLLM execution timed out")
        _record_litellm_route_receipt(receipt)
        return Response(
            content=json.dumps({"detail": "Request timeout"}),
            status_code=504,
            media_type="application/json",
        )
    except httpx.ConnectError:
        receipt.finish("failed")
        receipt.add_warning("LiteLLM execution backend is unavailable")
        _record_litellm_route_receipt(receipt)
        return Response(
            content=json.dumps({
                "error": {
                    "message": "LiteLLM execution backend is unavailable.",
                    "type": "service_unavailable",
                    "code": "backend_unavailable",
                }
            }),
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": "10"},
        )


async def _handle_openai_litellm_request(
    request: Request,
    request_path: str,
) -> Response:
    auth_context = authenticate_request(request, request_path)
    if isinstance(auth_context, Response):
        return auth_context

    if not any(request_path.startswith(p) for p in NO_ACTIVITY_PATHS):
        state.update_activity()

    body_peek = await request.body()
    parsed_body, parse_error = parse_openai_json_body(body_peek)
    if parse_error is not None:
        return parse_error

    requested_embedding_dimensions: int | None = None
    if request_path == "/v1/embeddings":
        requested_embedding_dimensions, dimensions_error = (
            _requested_embedding_dimensions(parsed_body)
        )
        if dimensions_error is not None:
            return dimensions_error

    if not _is_internal_protocol_forward(request):
        policy_decision = _openai_proxy_policy_decision(
            auth_context,
            request_path,
            body_peek,
        )
        if policy_decision is not None and policy_decision.denied:
            return _policy_denied_response(policy_decision)

    if (
        request_path in {"/v1/chat/completions", "/v1/completions"}
        and _is_use_loaded_model_alias(parsed_body.get("model"))
    ):
        return await _forward_current_loaded_openai_request(
            request,
            request_path=request_path,
            auth_context=auth_context,
            body_json=parsed_body,
        )

    litellm_runtime = _litellm_runtime_config()
    litellm_body: Mapping[str, Any] = parsed_body
    if litellm_runtime.enabled:
        litellm_service_response = _service_availability_response(
            litellm_runtime.service_id or "litellm",
            service_label="LiteLLM",
        )
        if litellm_service_response is None and request_path in {
            "/v1/chat/completions",
            "/v1/completions",
        }:
            activation_error, litellm_body = await _prepare_litellm_local_llm_request(
                auth_context,
                request_path=request_path,
                body_json=parsed_body,
            )
            if activation_error is not None:
                return activation_error

    return await _forward_litellm_proxy_request(
        request,
        request_path=request_path,
        auth_context=auth_context,
        body_json=litellm_body,
        runtime_config=litellm_runtime,
        requested_embedding_dimensions=requested_embedding_dimensions,
    )


@app.post("/openai/v1/chat/completions", openapi_extra=_OPENAI_COMPAT_JSON_BODY_OPENAPI)
@app.post("/v1/chat/completions", openapi_extra=_OPENAI_COMPAT_JSON_BODY_OPENAPI)
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions executed through LiteLLM."""
    return await _handle_openai_litellm_request(request, "/v1/chat/completions")


@app.post("/openai/v1/completions", openapi_extra=_OPENAI_COMPAT_JSON_BODY_OPENAPI)
@app.post("/v1/completions", openapi_extra=_OPENAI_COMPAT_JSON_BODY_OPENAPI)
async def completions(request: Request):
    """OpenAI-compatible text completions executed through LiteLLM."""
    return await _handle_openai_litellm_request(request, "/v1/completions")


@app.post("/openai/v1/embeddings", openapi_extra=_OPENAI_COMPAT_JSON_BODY_OPENAPI)
@app.post("/v1/embeddings", openapi_extra=_OPENAI_COMPAT_JSON_BODY_OPENAPI)
async def embeddings(request: Request):
    """OpenAI-compatible embeddings executed through LiteLLM."""
    return await _handle_openai_litellm_request(request, "/v1/embeddings")


def _openai_unsupported_api_response(api_name: str) -> Response:
    return openai_error_response(
        f"{api_name} is not implemented by this local gateway.",
        status_code=400,
        error_type="invalid_request_error",
        code="unsupported_operation",
    )


async def _openai_unsupported_api(request: Request, canonical_path: str, api_name: str) -> Response:
    auth_context = authenticate_request(request, canonical_path)
    if isinstance(auth_context, Response):
        return auth_context
    return _openai_unsupported_api_response(api_name)


@app.api_route(
    "/openai/v1/assistants{rest:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    include_in_schema=False,
)
@app.api_route(
    "/v1/assistants{rest:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    include_in_schema=False,
)
async def openai_assistants_unsupported(request: Request, rest: str = "") -> Response:
    """OpenAI Assistants compatibility surface - explicitly unsupported."""
    return await _openai_unsupported_api(request, "/v1/assistants", "Assistants API")


@app.api_route(
    "/openai/v1/files{rest:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
@app.api_route(
    "/v1/files{rest:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
async def openai_files_unsupported(request: Request, rest: str = "") -> Response:
    """OpenAI Files compatibility surface - explicitly unsupported."""
    return await _openai_unsupported_api(request, "/v1/files", "Files API")


@app.api_route(
    "/openai/v1/batches{rest:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
@app.api_route(
    "/v1/batches{rest:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
async def openai_batches_unsupported(request: Request, rest: str = "") -> Response:
    """OpenAI Batches compatibility surface - explicitly unsupported."""
    return await _openai_unsupported_api(request, "/v1/batches", "Batches API")


@app.api_route(
    "/openai/v1/fine_tuning{rest:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
@app.api_route(
    "/v1/fine_tuning{rest:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
async def openai_fine_tuning_unsupported(request: Request, rest: str = "") -> Response:
    """OpenAI Fine-tuning compatibility surface - explicitly unsupported."""
    return await _openai_unsupported_api(request, "/v1/fine_tuning", "Fine-tuning API")


@app.api_route(
    "/openai/v1/realtime{rest:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
@app.api_route(
    "/v1/realtime{rest:path}",
    methods=["GET", "POST", "DELETE"],
    include_in_schema=False,
)
async def openai_realtime_unsupported(request: Request, rest: str = "") -> Response:
    """OpenAI Realtime compatibility surface - explicitly unsupported."""
    return await _openai_unsupported_api(request, "/v1/realtime", "Realtime API")


def _should_return_not_found_before_backend(request_path: str) -> bool:
    """Return True for unmatched public routes.

    Provider-native APIs must be explicitly namespaced and policy-gated. The
    catch-all exists only so known routes mounted before it can coexist with
    FastAPI's routing order; it must not become public Tabby passthrough.
    """
    return True


def _normalize_chat_completion_sse_chunk(chunk: bytes) -> bytes:
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return chunk
    if "data:" not in text or ("reasoning_content" not in text and "</think>" not in text.lower()):
        return chunk
    out_lines: list[str] = []
    changed = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped.startswith("data:"):
            out_lines.append(line)
            continue
        prefix, data = line.split("data:", 1)
        newline = ""
        if data.endswith("\r\n"):
            data = data[:-2]
            newline = "\r\n"
        elif data.endswith("\n"):
            data = data[:-1]
            newline = "\n"
        data = data.strip()
        if not data or data == "[DONE]":
            out_lines.append(line)
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            out_lines.append(line)
            continue
        if isinstance(payload, dict) and _normalize_chat_completion_payload(payload):
            out_lines.append(f"{prefix}data: {json.dumps(payload, separators=(',', ':'))}{newline}")
            changed = True
        else:
            out_lines.append(line)
    return "".join(out_lines).encode("utf-8") if changed else chunk


def _provider_native_host_from_config(provider: str, cfg: Mapping[str, Any]) -> str:
    raw_url = str(cfg.get("url") or cfg.get("base_url") or "").strip()
    if raw_url:
        return raw_url.rstrip("/")
    host = str(cfg.get("host", "127.0.0.1")).strip()
    port = cfg.get("port")
    if not host or port is None:
        return ""
    scheme = str(cfg.get("scheme", "http")).strip() or "http"
    return f"{scheme}://{host}:{port}".rstrip("/")


def _build_provider_native_targets() -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {}
    for raw_name, raw_cfg in (config.get("backends") or {}).items():
        provider = str(raw_name).strip().lower()
        if not provider or not isinstance(raw_cfg, Mapping):
            continue
        if not bool_from_config(raw_cfg.get("enabled"), True):
            continue
        if raw_cfg.get("native_passthrough") is not True:
            continue
        host = _provider_native_host_from_config(provider, raw_cfg)
        if not host:
            continue
        targets[provider] = {
            "backend": provider,
            "service": str(raw_cfg.get("downstream_service") or provider),
            "host": host,
            "enabled": True,
            "timeout": float(raw_cfg.get("timeout") or UPSTREAM_READ_TIMEOUT),
        }
    return targets


_PROVIDER_NATIVE_TARGETS = _build_provider_native_targets()

_PROVIDER_NATIVE_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _native_passthrough_receipt(
    *,
    auth_context: AuthContext,
    provider: str,
    gateway_path: str,
    native_path: str,
) -> RouteReceipt:
    receipt = RouteReceipt(
        protocol="provider-native",
        intent="provider.native",
        path=gateway_path,
        backend=provider,
    )
    if auth_context.identity is not None:
        receipt.app_profile = auth_context.identity.profile.id
        receipt.app_id = auth_context.identity.app_id
        receipt.api_key_name = auth_context.identity.name
    receipt.metadata.update({
        "provider": provider,
        "native_path": f"/{native_path.lstrip('/')}",
    })
    return receipt


def _record_native_passthrough_receipt(receipt: RouteReceipt) -> None:
    try:
        receipt_store.record(receipt)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Could not record provider-native receipt: {e}", flush=True)


def _provider_native_summary(
    provider: str,
    target: Mapping[str, Any],
    auth_context: AuthContext,
) -> dict[str, Any]:
    decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="provider-native",
            intent="provider.native",
            backend=str(target["backend"]),
            native_passthrough=True,
            native_backend=str(target["backend"]),
        ),
    )
    profile = auth_context.identity.profile if auth_context.identity else DEFAULT_PROFILE
    native_policy = profile.native_passthrough
    cfg = (config.get("backends") or {}).get(provider, {})
    capabilities = []
    if isinstance(cfg, Mapping):
        raw_capabilities = cfg.get("capabilities") or ()
        if isinstance(raw_capabilities, (str, bytes)):
            capabilities = [str(raw_capabilities)]
        else:
            capabilities = [
                str(capability)
                for capability in raw_capabilities
                if str(capability).strip()
            ]
    service = str(target.get("service") or target["backend"])
    downstream_cfg = service_auth_config(auth_context, service)
    downstream_auth_classes = _provider_native_downstream_auth_classes(
        service,
        downstream_cfg,
    )
    return {
        "id": provider,
        "object": "provider",
        "name": str(cfg.get("name") or provider) if isinstance(cfg, Mapping) else provider,
        "backend": str(target["backend"]),
        "capabilities": capabilities,
        "policy": {
            "native_passthrough": {
                "enabled": decision.allowed,
                "denied": decision.denied,
                "allowed_backends": [str(target["backend"])],
                "allowed_operations": sorted(native_policy.allowed_operations) or ["*"],
                "audit": native_policy.audit,
            },
        },
        "auth": {
            "required": "gateway_api_key",
            "downstream_service": service,
            "downstream_auth_classes": downstream_auth_classes,
            "downstream_auth_configured": bool(downstream_auth_classes),
        },
        "live": {
            "enabled": bool(target.get("enabled", True)),
        },
        "native_passthrough_path": f"/gateway/v1/providers/{provider}/{{path}}",
    }


def _provider_native_downstream_auth_classes(
    service: str,
    service_cfg: Mapping[str, Any],
) -> list[str]:
    auth_classes: set[str] = set()
    if "api_key" in service_cfg or "api_key_env" in service_cfg:
        auth_classes.add("api_key")
    if "admin_key" in service_cfg or "admin_key_env" in service_cfg:
        auth_classes.add("admin_key")
    if isinstance(service_cfg.get("headers"), Mapping) and service_cfg.get("headers"):
        auth_classes.add("configured_headers")
    # Each auth_inject rule names a downstream secret it injects; report that
    # secret as an auth class (DATA-driven — no engine name in code).
    rules = service_cfg.get("auth_inject")
    if isinstance(rules, (list, tuple)):
        for rule in rules:
            if isinstance(rule, Mapping) and rule.get("secret"):
                auth_classes.add(str(rule["secret"]))
    return sorted(auth_classes)


def _provider_native_visible(
    provider: str,
    target: Mapping[str, Any],
    auth_context: AuthContext,
) -> bool:
    decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="provider-native",
            intent="provider.native",
            backend=str(target["backend"]),
            native_passthrough=True,
            native_backend=str(target["backend"]),
        ),
    )
    return decision.allowed


@app.get("/gateway/v1/providers")
async def provider_native_list(request: Request):
    """List provider-native lanes visible to this authenticated caller."""
    auth_context = _auth_context_from_scope(request)
    providers = [
        _provider_native_summary(provider, target, auth_context)
        for provider, target in sorted(_PROVIDER_NATIVE_TARGETS.items())
        if _provider_native_visible(provider, target, auth_context)
    ]
    return {
        "object": "list",
        "data": providers,
    }


@app.get("/gateway/v1/providers/{provider}")
async def provider_native_detail(request: Request, provider: str):
    """Describe one provider-native lane without forwarding to the provider."""
    provider_key = provider.strip().lower()
    target = _PROVIDER_NATIVE_TARGETS.get(provider_key)
    if target is None:
        return openai_error_response(
            f"Provider {provider!r} is not configured for native passthrough.",
            status_code=404,
            code="provider_not_found",
        )
    if not _provider_native_visible(provider_key, target, _auth_context_from_scope(request)):
        return openai_error_response(
            f"Provider {provider!r} is not visible to this App profile.",
            status_code=404,
            code="provider_not_found",
        )
    return _provider_native_summary(provider_key, target, _auth_context_from_scope(request))


@app.api_route(
    "/gateway/v1/providers/{provider}/{native_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    include_in_schema=False,
)
async def provider_native_request(
    request: Request,
    provider: str,
    native_path: str = "",
):
    """Policy-gated provider-native passthrough for explicitly configured providers."""
    provider_key = provider.strip().lower()
    gateway_path = f"/gateway/v1/providers/{provider}/{native_path}"
    target = _PROVIDER_NATIVE_TARGETS.get(provider_key)
    auth_context = _auth_context_from_scope(request)

    if target is None:
        return openai_error_response(
            f"Provider {provider!r} is not configured for native passthrough.",
            status_code=404,
            code="provider_not_found",
        )

    receipt = _native_passthrough_receipt(
        auth_context=auth_context,
        provider=provider_key,
        gateway_path=gateway_path,
        native_path=native_path,
    )
    native_request_path = f"/{native_path.lstrip('/')}"
    native_operation = f"{request.method.upper()} {native_request_path}"
    decision = evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="provider-native",
            intent="provider.native",
            backend=str(target["backend"]),
            native_passthrough=True,
            native_backend=str(target["backend"]),
            native_operation=native_operation,
        ),
        receipt,
    )
    if decision.denied:
        receipt.finish("denied")
        _record_native_passthrough_receipt(receipt)
        return _policy_denied_response(decision)

    body = await request.body()
    headers = prepare_downstream_headers(
        request.headers,
        auth_context,
        str(target["service"]),
        native_request_path,
    )
    url = f"{target['host']}{native_request_path}"
    if request.url.query:
        url += f"?{request.url.query}"

    client = httpx.AsyncClient(timeout=httpx.Timeout(
        connect=10.0,
        read=float(target.get("timeout") or UPSTREAM_READ_TIMEOUT),
        write=60.0,
        pool=None,
    ))
    stream_context = client.stream(
        request.method,
        url,
        headers=headers,
        content=body,
    )
    try:
        upstream = await stream_context.__aenter__()
    except httpx.TimeoutException:
        await client.aclose()
        receipt.finish("timeout")
        _record_native_passthrough_receipt(receipt)
        return openai_error_response(
            f"Provider {provider_key!r} timed out while handling {native_request_path}.",
            status_code=504,
            error_type="service_unavailable",
            code="provider_timeout",
        )
    except httpx.ConnectError:
        await client.aclose()
        receipt.finish("backend_unavailable")
        _record_native_passthrough_receipt(receipt)
        return openai_error_response(
            f"Provider {provider_key!r} is unavailable for native passthrough.",
            status_code=503,
            error_type="service_unavailable",
            code="provider_unavailable",
        )
    except Exception as e:  # noqa: BLE001
        await client.aclose()
        receipt.finish("error")
        _record_native_passthrough_receipt(receipt)
        return openai_error_response(
            f"Provider-native passthrough failed: {str(e)[:200]}",
            status_code=502,
            error_type="api_error",
            code="provider_passthrough_error",
        )

    response_headers = {
        k: v
        for k, v in upstream.headers.items()
        if k.lower() not in _PROVIDER_NATIVE_HOP_BY_HOP_HEADERS
    }
    receipt.finish("ok" if upstream.status_code < 400 else "error")
    receipt.metadata["status_code"] = upstream.status_code
    _record_native_passthrough_receipt(receipt)

    async def _provider_native_body_stream():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await stream_context.__aexit__(None, None, None)
            await client.aclose()

    return StreamingResponse(
        _provider_native_body_stream(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_request(request: Request, path: str, *, mapped_route: bool = False):
    """Proxy mapped OpenAI requests to a configured local backend."""
    request_path = f"/{path}"

    # Authenticate FIRST, against the original path so a per-device path-prefix
    # token (/<token>/v1/...) is still visible to the token check.
    auth_context = authenticate_request(request, request_path)
    if isinstance(auth_context, Response):
        return auth_context

    # Then strip the token prefix so the upstream sees a clean path and the token
    # never leaks into backend logs/routing.
    _path_token = _path_token_from_request(request_path)
    if _path_token is not None:
        stripped = request_path.lstrip("/")[len(str(_path_token)):]
        request_path = "/" + stripped.lstrip("/")
        path = request_path.lstrip("/")

    if not mapped_route and _should_return_not_found_before_backend(request_path):
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"Path not found: {request_path}",
                    "type": "invalid_request_error",
                    "code": "not_found",
                }
            }),
            status_code=404,
            media_type="application/json",
        )

    if request_path in LITELLM_BODY_EXECUTION_PATHS:
        return await _handle_openai_litellm_request(request, request_path)

    # Update activity (unless it's a health/status/tts endpoint)
    is_activity = not any(request_path.startswith(p) for p in NO_ACTIVITY_PATHS)
    if is_activity:
        state.update_activity()

    body_peek = await request.body()
    if (
        request.method.upper() in {"POST", "PUT", "PATCH"}
        and any(request_path.startswith(p) for p in MODEL_REQUIRED_PATHS)
    ):
        parsed_body, parse_error = parse_openai_json_body(body_peek)
        if parse_error is not None:
            return parse_error
        if _is_use_loaded_model_alias(parsed_body.get("model")):
            return await _forward_current_loaded_openai_request(
                request,
                request_path=request_path,
                auth_context=auth_context,
                body_json=parsed_body,
            )

    # Registry-aware backend selection for the catch-all native route.
    # Prefer tabby when it exists and is enabled (zero behavior change for
    # today's deployment).  When tabby is absent or disabled, fall back to
    # the highest-priority enabled chat-capable backend from LLM_BACKEND_HOSTS.
    _tabby_cfg = (config.get("backends") or {}).get("tabby")
    _tabby_present_and_enabled = isinstance(_tabby_cfg, Mapping) and bool_from_config(
        _tabby_cfg.get("enabled"), True
    ) and "tabby" in LLM_BACKEND_HOSTS
    if _tabby_present_and_enabled:
        target_host = TABBY_HOST
    else:
        # tabby absent or disabled — pick the highest-priority chat-capable backend.
        _fallback_id = _highest_priority_chat_backend()
        target_host = LLM_BACKEND_HOSTS[_fallback_id] if _fallback_id else TABBY_HOST

    target_service_for_admission = backend_for_host(target_host)
    if target_service_for_admission:
        service_response = _service_availability_response(
            target_service_for_admission,
            service_label=target_service_for_admission,
        )
        if service_response is not None:
            return service_response

    if target_host == TABBY_HOST:
        if not await check_tabby_health():
            # Backend-native catch-all paths are service-specific. They never
            # fall through to Kobold because native Tabby/Kobold surfaces are
            # not interchangeable. The gateway does not revive tabby — return
            # 503 and let the operator's supervisor bring it back.
            if not await tabby_backend_healthy():
                return Response(
                    content=json.dumps({
                        "error": {
                            "message": (
                                "Configured backend 'tabby' is unavailable for this catch-all route. "
                                "Retry after Retry-After."
                            ),
                            "type": "service_unavailable",
                            "code": "backend_unavailable",
                    }
                }),
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": "10"},
            )

    # Auto-load model if needed (TabbyAPI only)
    if target_host == TABBY_HOST and any(request_path.startswith(p) for p in MODEL_REQUIRED_PATHS):
        if not await is_model_loaded():
            if not await load_model():
                return Response(
                    content=json.dumps({"detail": "Failed to load model"}),
                    status_code=503,
                    media_type="application/json"
                )

    # Forward to target backend. Strip our own ?api_key= before forwarding so the
    # gateway credential never reaches the backend (or its access logs); all other
    # query params are preserved.
    url = f"{target_host}/{path}"
    if request.url.query:
        _fwd_qs = [
            (k, v) for k, v in parse_qsl(request.url.query, keep_blank_values=True)
            if k != "api_key"
        ]
        if _fwd_qs:
            url += f"?{urlencode(_fwd_qs)}"

    body = body_peek  # Already read above for model detection
    # backend_for_host resolves known hosts; for an unknown host (shouldn't
    # happen given the registry-derived target above) we fall back to the
    # highest-priority chat backend id rather than a bare literal.
    target_service = backend_for_host(target_host) or _highest_priority_chat_backend() or "kobold"
    headers = prepare_downstream_headers(
        request.headers,
        auth_context,
        target_service,
        request_path,
    )

    try:
        # connect tight (local backend should accept instantly), read generous
        # (covers slow prompt-processing on reasoning/vision), write tight
        # (request bodies are small even with base64 images), no pool budget
        # (fresh client per request — no shared pool to wait on).
        client = httpx.AsyncClient(timeout=httpx.Timeout(
            connect=10.0,
            read=UPSTREAM_READ_TIMEOUT,
            write=60.0,
            pool=None,
        ))
        req = client.build_request(method=request.method, url=url, headers=headers, content=body)
        resp = await client.send(req, stream=True)

        content_type = resp.headers.get("content-type", "") or "application/json"
        excluded = ('content-length', 'transfer-encoding', 'content-type')
        response_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        if "text/event-stream" in content_type:
            async def stream_response():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                except:
                    pass
                finally:
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                stream_response(),
                status_code=resp.status_code,
                headers=response_headers,
                media_type=content_type
            )

        content = await resp.aread()
        await resp.aclose()
        await client.aclose()
        return Response(
            content=content,
            status_code=resp.status_code,
            headers=response_headers,
            media_type=content_type
        )

    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"detail": "Request timeout"}),
            status_code=504,
            media_type="application/json"
        )
    except httpx.ConnectError:
        # A connect error can be a transient blip or the supervisor mid-restart.
        # Re-probe health: if tabby is reachable again, tell the client to retry
        # (503); otherwise it's a hard connection failure (502). The gateway does
        # not restart tabby — its supervisor owns that.
        if target_service == "tabby" and await tabby_backend_healthy():
            return Response(
                content=json.dumps({"detail": "TabbyAPI is reachable again, please retry"}),
                status_code=503,
                media_type="application/json"
            )
        return Response(
            content=json.dumps({"detail": f"{target_service} connection failed"}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"detail": f"Proxy error: {str(e)}"}),
            status_code=502,
            media_type="application/json"
        )


# =============================================================================
# Main
# =============================================================================

def signal_handler(signum, frame):
    print(f"\n[proxy] Received signal {signum}, shutting down...", flush=True)
    state.shutdown_requested = True
    sys.exit(0)


if __name__ == "__main__":
    import uvicorn

    if handle_cli_command():
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    uvicorn.run(
        app,
        host=PROXY_HOST,
        port=PROXY_PORT,
        log_level="warning",
        access_log=False
    )

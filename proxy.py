#!/usr/bin/env python3
"""
OpenAI Proxy - Unified OpenAI-compatible API for multiple backends.
Served externally at openai.vivid.fish (Traefik on 192.168.1.4 → 192.168.1.180:5000).

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
import threading
import time
import atexit
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
import contextlib
from contextlib import asynccontextmanager
from typing import Any, Callable, Mapping, Optional

import httpx
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Local: status dashboard backend.
import status_endpoint
import firehose_metrics
import metrics_store
import metrics_sampler
import responses_store
import responses_translator
from gateway import media_jobs, receipt_store
from gateway.api import create_gateway_router
from gateway.audio_extensions import bind_audio_workflow_extensions
from gateway.capabilities import (
    Capability,
    ResourceEstimate,
    capability_from_workflow_record,
    capability_with_policy_view,
)
from gateway.chat_extensions import (
    validate_chat_completions_extensions,
    validate_responses_extensions,
)
from gateway.discovery import WorkflowRegistry
from gateway.discovery_metadata import (
    build_gateway_discovery_metadata,
    build_llms_txt,
    build_skill_catalog,
)
from gateway.local_capabilities import build_local_capabilities
from gateway.extensions import ExtensionError
from gateway.image_extensions import (
    STANDARD_IMAGE_EDIT_FIELDS,
    STANDARD_IMAGE_GENERATE_FIELDS,
    WORKFLOW_EXTENSION_NAMESPACE,
    bind_workflow_extensions,
    detect_unknown_top_level_fields,
    record_image_extension_binding,
)
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
from gateway.policy import DEFAULT_PROFILE, PolicyDecision
from gateway.receipts import RouteReceipt
from gateway.runtime_auth import (
    RuntimeIdentity,
    build_identity_registry,
    discovery_audience_for_profile,
    evaluate_and_apply,
    hash_api_key as runtime_hash_api_key,
    key_hashes_for_entry as runtime_key_hashes_for_entry,
    make_policy_request,
    normalize_key_hash as runtime_normalize_key_hash,
)
from gateway.protocols import anthropic as anthropic_protocol
from gateway.protocols import ollama as ollama_protocol

# =============================================================================
# Configuration
# =============================================================================

CONFIG_PATH = Path(__file__).parent / "proxy_config.yml"


def load_config() -> dict:
    """Load configuration from YAML file."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    return {}


config = load_config()


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
TABBY_MANAGED = tabby_cfg.get("managed", True)
TABBY_START_CMD = tabby_cfg.get("start_command", "")

# KoboldCpp backend
kobold_cfg = config.get("backends", {}).get("kobold", {})
KOBOLD_HOST = f"http://{kobold_cfg.get('host', '127.0.0.1')}:{kobold_cfg.get('port', 5051)}"
KOBOLD_HEALTH_ENDPOINT = kobold_cfg.get("health_endpoint", "/api/v1/model")
KOBOLD_SERVICE = kobold_cfg.get("service_name", "koboldcpp.service")

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
# hardcoding a filename. Roles map to a backend type ("kobold" or "tabby") — never to
# a specific model file. Whichever model the chosen backend has loaded is what gets used.
ROLES = config.get("roles", {})

# Optional catalog of additional GGUF aliases (parallel to ROLES). Each entry has
# id/backend/path[/description] and is treated like a role with a pinned model:
# clients send `model: "<id>"` and the proxy resolves it via ensure_target().
# This lets us surface arbitrary GGUFs on /v1/models without inventing roles.
def _normalize_models_catalog(raw) -> dict:
    """Normalize config['models'] (list of dicts) into {normalized_id: entry}."""
    out: dict = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id", "")).strip().lower()
        backend = item.get("backend")
        path = item.get("path")
        if not mid or backend not in ("kobold", "tabby") or not path:
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
TTS_ENABLED = os.environ.get("TTS_ENABLED", "true").lower() in ("true", "1", "yes")
TTS_HOST = os.environ.get("TTS_HOST", f"http://{tts_cfg.get('host', '127.0.0.1')}:{tts_cfg.get('port', 8880)}")
TTS_TIMEOUT = int(os.environ.get("TTS_TIMEOUT", tts_cfg.get("timeout", 120)))

# STT backend
stt_cfg = config.get("backends", {}).get("stt", {})
STT_ENABLED = os.environ.get("STT_ENABLED", "true").lower() in ("true", "1", "yes")
STT_HOST = os.environ.get("STT_HOST", f"http://{stt_cfg.get('host', '127.0.0.1')}:{stt_cfg.get('port', 5092)}")
STT_TIMEOUT = int(os.environ.get("STT_TIMEOUT", stt_cfg.get("timeout", 120)))

# ComfyUI backend
comfy_cfg = config.get("backends", {}).get("comfyui", {})
COMFY_ENABLED = os.environ.get("COMFY_ENABLED", "true").lower() in ("true", "1", "yes")
COMFY_HOST = os.environ.get("COMFY_HOST", f"http://{comfy_cfg.get('host', '127.0.0.1')}:{comfy_cfg.get('port', 7890)}")
COMFY_TIMEOUT = int(os.environ.get("COMFY_TIMEOUT", comfy_cfg.get("timeout", 300)))
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

# Tracks model_paths we've already warned about (when PIN_LOADED_MODEL=True)
# so we don't spam the journal with one warning per request.
_pin_mismatch_warned: set = set()

# Auth
auth_cfg = config.get("auth", {})

# Health
HEALTH_CHECK_TIMEOUT = 5
MAX_LOAD_FAILURES = 3
LOAD_FAILURE_BACKOFF = 30

# Endpoints that require model to be loaded
MODEL_REQUIRED_PATHS = [
    "/v1/completions",
    "/v1/chat/completions",
    "/v1/embeddings",
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

def detect_model_backend(model_name: str) -> Optional[str]:
    """Detect whether a model should be served by TabbyAPI (EXL3/EXL2) or KoboldCpp (GGUF).
    Returns 'tabby', 'kobold', or None if not found."""
    if not model_name:
        return None

    # Check GGUF directory for .gguf files
    gguf_dir = Path(GGUF_DIR)
    if gguf_dir.exists():
        for f in gguf_dir.iterdir():
            if f.suffix == '.gguf' and model_name.lower() in f.name.lower():
                return 'kobold'

    # Check model directory for EXL3/EXL2 directories (have .safetensors + quantization_config.json)
    model_dir = Path(MODEL_DIR)
    if model_dir.exists():
        for d in model_dir.iterdir():
            if d.is_dir() and model_name.lower() in d.name.lower():
                if (d / 'quantization_config.json').exists():
                    return 'tabby'
                # Also check for .gguf files in model dir
                if any(f.suffix == '.gguf' for f in d.iterdir() if f.is_file()):
                    return 'kobold'

    return None


async def get_active_backend() -> Optional[str]:
    """Check which LLM backend is currently running. Returns 'tabby', 'kobold', or None."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5)) as client:
            resp = await client.get(f"{TABBY_HOST}{TABBY_HEALTH_ENDPOINT}")
            if resp.status_code == 200:
                return 'tabby'
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5)) as client:
            resp = await client.get(f"{KOBOLD_HOST}{KOBOLD_HEALTH_ENDPOINT}")
            if resp.status_code == 200:
                return 'kobold'
    except Exception:
        pass

    return None


async def switch_backend(target: str) -> bool:
    """Switch to the target backend ('tabby' or 'kobold') using systemd Conflicts=.
    Returns True if successful."""
    if target == 'tabby':
        service = TABBY_SERVICE
    elif target == 'kobold':
        service = KOBOLD_SERVICE_NAME
    else:
        return False

    print(f"[proxy] Switching LLM backend to {target}...", flush=True)

    proc = await asyncio.create_subprocess_exec(
        'systemctl', '--user', 'start', service,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        print(f"[proxy] Failed to start {service}: {stderr.decode()}", flush=True)
        return False

    # Wait for the backend to become healthy
    target_host = TABBY_HOST if target == 'tabby' else KOBOLD_HOST
    health_path = TABBY_HEALTH_ENDPOINT if target == 'tabby' else KOBOLD_HEALTH_ENDPOINT

    for i in range(SWITCH_TIMEOUT):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3)) as client:
                resp = await client.get(f"{target_host}{health_path}")
                if resp.status_code == 200:
                    print(f"[proxy] Backend {target} is ready (took {i+1}s)", flush=True)
                    return True
        except Exception:
            pass
        await asyncio.sleep(1)

    print(f"[proxy] Backend {target} did not become ready within {SWITCH_TIMEOUT}s", flush=True)
    return False


# Drop-in file used to override LLAMA_TURBO_MODEL for a specific GGUF.
# Numbered higher than 10-qwen27b.conf so systemd's last-wins semantics apply.
KOBOLD_MODEL_DROPIN = Path.home() / ".config/systemd/user/llama-turbo.service.d/20-model.conf"

# Lock to serialize concurrent kobold model swaps. In-flight callers await the
# in-progress swap so we don't restart llama-turbo.service twice for the same target.
kobold_swap_lock = asyncio.Lock()

# Short TTL cache for the currently-loaded GGUF path so the request hot path doesn't
# pgrep on every call.
_kobold_loaded_cache: dict = {"value": None, "ts": 0.0}
_KOBOLD_LOADED_TTL = 5.0


def kobold_has_mmproj() -> Optional[bool]:
    """Return True iff the running llama-server includes an --mmproj flag.

    Used by Fix 3 (vision routing): if mmproj is loaded, the kobold backend is
    natively vision-capable and we don't need to switch to tabby. Returns None
    if we couldn't determine (no llama-server running, pgrep failed).
    """
    try:
        result = subprocess.run(
            ["pgrep", "-af", "llama-server"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in result.stdout.splitlines():
            if "llama-server" not in line:
                continue
            return "--mmproj" in line
    except Exception as e:
        print(f"[proxy] kobold_has_mmproj: pgrep failed: {e}", flush=True)
    return None


def currently_loaded_kobold_mmproj() -> Optional[str]:
    """Return the absolute path passed to llama-server's --mmproj flag, or None.

    Parsed from the running llama-server cmdline via pgrep. Returns None if no
    --mmproj flag is set (text-only model) or no llama-server is running.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-af", "llama-server"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in result.stdout.splitlines():
            if "llama-server" not in line:
                continue
            idx = line.find("--mmproj ")
            if idx == -1:
                return None
            tail = line[idx + len("--mmproj "):]
            end = tail.find(" --")
            if end != -1:
                tail = tail[:end]
            path = tail.strip()
            return path or None
    except Exception as e:
        print(f"[proxy] currently_loaded_kobold_mmproj: pgrep failed: {e}", flush=True)
    return None


def currently_loaded_kobold_model() -> Optional[str]:
    """Return the absolute path of the GGUF llama-server is currently serving, or None.

    Parses --model from the running llama-server cmdline via pgrep. Cached for
    _KOBOLD_LOADED_TTL seconds to avoid spawning pgrep on every request.
    """
    now = time.time()
    if _kobold_loaded_cache["value"] is not None and (now - _kobold_loaded_cache["ts"]) < _KOBOLD_LOADED_TTL:
        return _kobold_loaded_cache["value"]

    path: Optional[str] = None
    try:
        result = subprocess.run(
            ["pgrep", "-af", "llama-server"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in result.stdout.splitlines():
            # Look for `--model <path>` followed by another flag (typically `--port`).
            # Paths can contain spaces, so split on " --" to bound the value.
            idx = line.find("--model ")
            if idx == -1:
                continue
            tail = line[idx + len("--model "):]
            # Strip the next flag if present
            end = tail.find(" --")
            if end != -1:
                tail = tail[:end]
            path = tail.strip()
            if path:
                break
    except Exception as e:
        print(f"[proxy] currently_loaded_kobold_model: pgrep failed: {e}", flush=True)

    _kobold_loaded_cache["value"] = path
    _kobold_loaded_cache["ts"] = now
    return path


async def swap_kobold_model(target_path: str, target_mmproj: Optional[str] = None) -> bool:
    """Restart llama-turbo.service so it loads the given GGUF (and mmproj).

    Writes a higher-numbered drop-in (20-model.conf) overriding LLAMA_TURBO_MODEL
    AND LLAMA_TURBO_MMPROJ, runs daemon-reload + restart, then polls /v1/models
    until ready. Concurrent callers serialize on kobold_swap_lock; in-flight
    requests await the in-progress swap.

    target_mmproj behavior:
      - non-empty str: must exist on disk; written as the mmproj path
      - None / empty: written as an empty LLAMA_TURBO_MMPROJ so start_service.sh
        omits --mmproj (text-only model)

    Returns True on success, False on timeout or error.
    """
    if not Path(target_path).is_file():
        print(f"[proxy] swap_kobold_model: target not found: {target_path}", flush=True)
        return False

    # Validate mmproj exists on disk if specified. Don't try to validate that the
    # mmproj is architecturally compatible with the model — llama-server does that
    # and produces a clear error.
    if target_mmproj:
        if not Path(target_mmproj).is_file():
            print(f"[proxy] swap_kobold_model: mmproj not found: {target_mmproj}", flush=True)
            return False
    mmproj_value = target_mmproj or ""

    async with kobold_swap_lock:
        # Re-check inside the lock — another caller may have already loaded this model.
        current = currently_loaded_kobold_model()
        current_mmproj = currently_loaded_kobold_mmproj() if current == target_path else None
        if current == target_path and (current_mmproj or "") == mmproj_value:
            return True

        print(
            f"[proxy] swapping kobold model: {current} -> {target_path} "
            f"(mmproj={mmproj_value or '<none>'})",
            flush=True,
        )
        start = time.time()

        try:
            KOBOLD_MODEL_DROPIN.parent.mkdir(parents=True, exist_ok=True)
            KOBOLD_MODEL_DROPIN.write_text(
                "# Managed by openai-proxy. Do not edit by hand; set role `model:`/`mmproj:` in proxy_config.yml.\n"
                "[Service]\n"
                f'Environment="LLAMA_TURBO_MODEL={target_path}"\n'
                f'Environment="LLAMA_TURBO_MMPROJ={mmproj_value}"\n'
            )
        except Exception as e:
            print(f"[proxy] swap_kobold_model: failed to write drop-in: {e}", flush=True)
            return False

        for cmd in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "restart", "llama-turbo.service"],
        ):
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                print(f"[proxy] swap_kobold_model: `{' '.join(cmd)}` failed: {stderr.decode()}", flush=True)
                return False

        # Invalidate the loaded-model cache so the next read re-checks pgrep.
        _kobold_loaded_cache["value"] = None
        _kobold_loaded_cache["ts"] = 0.0

        for i in range(SWITCH_TIMEOUT):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(3)) as client:
                    resp = await client.get(f"{KOBOLD_HOST}/v1/models")
                    if resp.status_code == 200:
                        elapsed = time.time() - start
                        print(f"[proxy] swap complete in {elapsed:.1f}s", flush=True)
                        try:
                            firehose_metrics.record_event(
                                "model_swap",
                                f"kobold {current or '—'} → {target_path}",
                                backend="kobold",
                                elapsed_s=round(elapsed, 2),
                                target=target_path,
                            )
                        except Exception:
                            pass
                        return True
            except Exception:
                pass
            await asyncio.sleep(1)

        print(f"[proxy] swap failed: kobold did not become ready within {SWITCH_TIMEOUT}s", flush=True)
        return False


async def ensure_backend_for_model(model_name: str) -> Optional[str]:
    """Ensure the right backend is running for the given model.
    Returns the backend host URL, or None if switching failed."""
    needed = detect_model_backend(model_name)
    if not needed:
        # Unknown model — use whatever's running
        active = await get_active_backend()
        return TABBY_HOST if active == 'tabby' else KOBOLD_HOST if active == 'kobold' else None

    active = await get_active_backend()
    if active == needed:
        return TABBY_HOST if needed == 'tabby' else KOBOLD_HOST

    # Need to switch
    if await switch_backend(needed):
        return TABBY_HOST if needed == 'tabby' else KOBOLD_HOST

    return None


# Lock to serialize concurrent tabby model swaps so two requests don't fight over
# /v1/model/load and /v1/model/unload at the same time.
tabby_swap_lock = asyncio.Lock()


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


async def swap_tabby_model(target_model: str) -> bool:
    """Ensure TabbyAPI has `target_model` loaded. Unloads any current model, then
    issues /v1/model/load with the target. Returns True on success.

    `target_model` is the TabbyAPI model_name (directory under tabby's model_dir),
    matching the format the existing `load_model()` path uses.
    """
    if not target_model:
        return False

    async with tabby_swap_lock:
        current = await currently_loaded_tabby_model()
        if current == target_model:
            return True

        print(f"[proxy] swapping tabby model: {current} -> {target_model}", flush=True)
        start = time.time()

        # Unload whatever's currently there. Idempotent — if nothing is loaded,
        # TabbyAPI returns ok or a benign error.
        if current:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{TABBY_HOST}/v1/model/unload",
                        headers={"x-admin-key": ADMIN_KEY},
                        timeout=60,
                    )
                    if resp.status_code not in (200, 204):
                        print(f"[proxy] swap_tabby_model: unload returned {resp.status_code}: {resp.text[:200]}", flush=True)
            except Exception as e:
                print(f"[proxy] swap_tabby_model: unload error: {e}", flush=True)
                return False

        # Load the target. Reuse model_params from tabby's config (cache_size,
        # max_seq_len, etc.) to mirror the idle-reload path.
        _, _, model_params = get_tabby_model_config()
        load_payload = {"model_name": target_model, **model_params}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{TABBY_HOST}/v1/model/load",
                    headers={"x-admin-key": ADMIN_KEY, "Content-Type": "application/json"},
                    json=load_payload,
                    timeout=TABBY_LOAD_TIMEOUT,
                )
                response_text = ""
                async for chunk in resp.aiter_bytes():
                    response_text += chunk.decode("utf-8", errors="replace")

                if resp.status_code == 200 and '"status":"finished"' in response_text:
                    elapsed = time.time() - start
                    print(f"[proxy] swap_tabby_model: success in {elapsed:.1f}s", flush=True)
                    try:
                        firehose_metrics.record_event(
                            "model_swap",
                            f"tabby {current or '—'} → {target_model}",
                            backend="tabby",
                            elapsed_s=round(elapsed, 2),
                            target=target_model,
                        )
                    except Exception:
                        pass
                    return True

                if '"status":"error"' in response_text or '"error":' in response_text:
                    print(f"[proxy] swap_tabby_model: load reported error: {response_text[-300:]}", flush=True)
                    return False

                # Verify by querying /v1/model.
                loaded_now = await currently_loaded_tabby_model()
                if loaded_now == target_model:
                    elapsed = time.time() - start
                    print(f"[proxy] swap_tabby_model: success (verified) in {elapsed:.1f}s", flush=True)
                    try:
                        firehose_metrics.record_event(
                            "model_swap",
                            f"tabby {current or '—'} → {target_model}",
                            backend="tabby",
                            elapsed_s=round(elapsed, 2),
                            target=target_model,
                            verified=True,
                        )
                    except Exception:
                        pass
                    return True

                print(f"[proxy] swap_tabby_model: outcome unclear, loaded={loaded_now}, response={response_text[-200:]}", flush=True)
                return False
        except Exception as e:
            print(f"[proxy] swap_tabby_model: load error: {e}", flush=True)
            return False


async def ensure_target(backend: str, model: Optional[str], mmproj: Optional[str] = None) -> bool:
    """Make a (backend, model, mmproj) target real before forwarding a request.

    Steps:
      1. If `backend` isn't currently the active LLM backend, call switch_backend()
         to start it (cold start). For tabby with TABBY_MANAGED, skip — that's the
         lifespan's job, but we still verify health after.
      2. If `model` is provided and the loaded model OR mmproj on `backend` differs,
         swap via the appropriate primitive (swap_kobold_model or swap_tabby_model).

    `mmproj` is kobold-only. Pass None for text-only models — the proxy writes an
    empty LLAMA_TURBO_MMPROJ so start_service.sh omits --mmproj.

    Returns True only if both steps succeed. Caller turns False into a 503.
    """
    if backend not in ("kobold", "tabby"):
        print(f"[proxy] ensure_target: unknown backend '{backend}'", flush=True)
        return False

    label = f"ensure_target({backend}, {model}, mmproj={mmproj or '<none>'})"

    # Step 1: cold-start the backend if needed.
    active = await get_active_backend()
    if active != backend:
        print(f"[proxy] {label} -> backend cold (active={active}), starting...", flush=True)
        if not await switch_backend(backend):
            print(f"[proxy] {label} failed: backend '{backend}' did not start", flush=True)
            return False

    # Step 2: swap model if pinned and mismatched.
    if model:
        if backend == "kobold":
            if not is_literal_gguf_path(model):
                # role_model_path is supposed to be an absolute path. If the file
                # is missing, fail loudly rather than restart llama-turbo into a
                # broken state.
                if not (os.path.isabs(model) and model.endswith(".gguf") and os.path.exists(model)):
                    print(f"[proxy] {label} failed: GGUF not found at {model}", flush=True)
                    return False
            # Pre-validate mmproj path if provided — caller asked for vision support,
            # so refuse to proceed if the projector file is missing.
            if mmproj and not (os.path.isabs(mmproj) and os.path.exists(mmproj)):
                print(f"[proxy] {label} failed: mmproj not found at {mmproj}", flush=True)
                return False

            loaded = currently_loaded_kobold_model()
            loaded_mmproj = currently_loaded_kobold_mmproj()
            target_mmproj = mmproj or None
            mmproj_mismatch = (loaded_mmproj or None) != target_mmproj
            if loaded != model or mmproj_mismatch:
                if PIN_LOADED_MODEL:
                    # Pinned mode: never auto-restart llama-turbo. Serve the request
                    # with whatever's loaded. Warn once per distinct requested path.
                    if model not in _pin_mismatch_warned:
                        _pin_mismatch_warned.add(model)
                        print(
                            f"[proxy] {label} pin_loaded_model=true: skipping switch "
                            f"(loaded={loaded}, requested={model}, "
                            f"loaded_mmproj={loaded_mmproj}, requested_mmproj={target_mmproj}); "
                            f"serving with current model",
                            flush=True,
                        )
                else:
                    print(
                        f"[proxy] {label} -> swap from {loaded} (mmproj {loaded_mmproj} -> {target_mmproj})",
                        flush=True,
                    )
                    ok = await swap_kobold_model(model, target_mmproj)
                    if not ok:
                        print(f"[proxy] {label} failed: kobold swap to {model} did not complete", flush=True)
                        return False
                    print(f"[proxy] {label} success", flush=True)
        elif backend == "tabby":
            loaded = await currently_loaded_tabby_model()
            if loaded != model:
                print(f"[proxy] {label} -> swap from {loaded}", flush=True)
                ok = await swap_tabby_model(model)
                if not ok:
                    print(f"[proxy] {label} failed: tabby swap to {model} did not complete", flush=True)
                    return False
                print(f"[proxy] {label} success", flush=True)

    return True


# =============================================================================
# Load credentials
# =============================================================================

TABBY_ADMIN_KEY_ENV = auth_cfg.get("admin_key_env", "TABBY_ADMIN_KEY")
TABBY_API_KEY_ENV = auth_cfg.get("api_key_env", "TABBY_API_KEY")
ADMIN_KEY = os.environ.get(TABBY_ADMIN_KEY_ENV, "") or os.environ.get("TABBY_ADMIN_KEY", "")
API_KEY = os.environ.get(TABBY_API_KEY_ENV, "") or os.environ.get("TABBY_API_KEY", "")

# Try loading from tabbyapi's api_tokens.yml
tokens_file = Path("/home/tnunamak/applications/tabbyapi/api_tokens.yml")
if tokens_file.exists():
    for line in tokens_file.read_text().splitlines():
        if line.startswith("admin_key:") and not ADMIN_KEY:
            ADMIN_KEY = line.split(":", 1)[1].strip()
        elif line.startswith("api_key:") and not API_KEY:
            API_KEY = line.split(":", 1)[1].strip()


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


AUTH_KEYS_FILE = config_path(
    os.environ.get("OPENAI_PROXY_KEYS_FILE", auth_cfg.get("keys_file")),
    "proxy_api_keys.yml",
)
AUTH_PUBLIC_PATHS = auth_cfg.get("public_paths") or ["/health"]
AUTH_PROFILE_STRICT = bool_from_config(
    os.environ.get("OPENAI_PROXY_AUTH_PROFILE_STRICT", auth_cfg.get("profile_strict")),
    False,
)


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


def load_proxy_key_entries() -> list[dict]:
    """Load client-facing proxy key records from config and the local key file."""
    entries = []
    config_entries = auth_cfg.get("keys") or auth_cfg.get("proxy_keys") or []
    if isinstance(config_entries, list):
        entries.extend(config_entries)

    if AUTH_KEYS_FILE.exists():
        try:
            with open(AUTH_KEYS_FILE) as f:
                file_data = yaml.safe_load(f) or {}
            file_entries = file_data if isinstance(file_data, list) else file_data.get("keys", [])
            if isinstance(file_entries, list):
                entries.extend(file_entries)
        except Exception as e:
            print(f"[proxy] Warning: Failed to load proxy auth keys: {e}", flush=True)

    return entries


def key_hashes_for_entry(entry: dict) -> list[str]:
    """Return every usable hash for one auth entry."""
    return runtime_key_hashes_for_entry(entry, env=os.environ)


def build_auth_registry(entries: list[dict]) -> dict[str, AuthIdentity]:
    """Build a hash-indexed registry of valid proxy keys."""
    return build_identity_registry(
        entries,
        env=os.environ,
        strict=AUTH_PROFILE_STRICT,
    )


AUTH_REGISTRY: dict[str, AuthIdentity] = {}
AUTH_KEYS_FILE_MTIME: Optional[int] = None
AUTH_REGISTRY_LOADED = False


def auth_keys_file_mtime() -> Optional[int]:
    if not AUTH_KEYS_FILE.exists():
        return None
    return AUTH_KEYS_FILE.stat().st_mtime_ns


def refresh_auth_registry(force: bool = False):
    """Reload proxy keys when the key file changes."""
    global AUTH_REGISTRY, AUTH_KEYS_FILE_MTIME, AUTH_REGISTRY_LOADED

    current_mtime = auth_keys_file_mtime()
    if AUTH_REGISTRY_LOADED and not force and current_mtime == AUTH_KEYS_FILE_MTIME:
        return

    AUTH_REGISTRY = build_auth_registry(load_proxy_key_entries())
    AUTH_KEYS_FILE_MTIME = current_mtime
    AUTH_REGISTRY_LOADED = True


refresh_auth_registry(force=True)
AUTH_ENABLED = bool_from_config(
    os.environ.get("OPENAI_PROXY_AUTH_ENABLED", auth_cfg.get("enabled")),
    True,
)


def is_public_path(path: str) -> bool:
    """Allow health checks without client auth."""
    for public_path in AUTH_PUBLIC_PATHS:
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
    """Authenticate a client-facing proxy key when auth is configured."""
    if not AUTH_ENABLED or is_public_path(request_path):
        return NO_AUTH_CONTEXT

    refresh_auth_registry()

    if not AUTH_REGISTRY:
        return auth_error("Proxy auth is enabled, but no proxy API keys are configured.", 503)

    api_key = get_client_api_key_from_headers(request.headers)
    if not api_key:
        return auth_error("Missing API key.")

    identity = AUTH_REGISTRY.get(hash_api_key(api_key))
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


def apply_tabby_auth(headers: dict, auth_context: AuthContext, request_path: str):
    service_cfg = service_auth_config(auth_context, "tabby")
    api_key = downstream_secret(service_cfg, "api_key", API_KEY)
    admin_key = downstream_secret(service_cfg, "admin_key", ADMIN_KEY)
    needs_admin = request_path in ["/v1/models", "/v1/model/list"]

    if needs_admin and admin_key:
        headers["x-admin-key"] = admin_key
        return

    if api_key:
        headers["x-api-key"] = api_key


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

    if service == "tabby":
        apply_tabby_auth(headers, auth_context, request_path)

    return headers


def generate_proxy_api_key() -> str:
    return f"opk_{secrets.token_urlsafe(32)}"


def create_proxy_key(name: str, keys_file: Path = AUTH_KEYS_FILE) -> str:
    """Create or rotate a named proxy key, storing only its hash."""
    api_key = generate_proxy_api_key()
    data = {}

    if keys_file.exists():
        with open(keys_file) as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, list):
            data = {"keys": data}

    entries = data.setdefault("keys", [])
    if not isinstance(entries, list):
        entries = []
        data["keys"] = entries

    for idx, entry in enumerate(entries):
        if isinstance(entry, dict) and entry.get("name") == name:
            downstream = entry.get("downstream")
            if not isinstance(downstream, dict):
                downstream = {"tabby": {}}
            new_entry = {
                "name": name,
                "key_hash": hash_api_key(api_key),
                "downstream": downstream,
            }
            if "profile" in entry:
                new_entry["profile"] = entry.get("profile")
            entries[idx] = new_entry
            break
    else:
        new_entry = {
            "name": name,
            "key_hash": hash_api_key(api_key),
            "downstream": {
                "tabby": {},
            },
        }
        entries.append(new_entry)

    keys_file.parent.mkdir(parents=True, exist_ok=True)
    with open(keys_file, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    os.chmod(keys_file, 0o600)

    return api_key


def handle_cli_command() -> bool:
    if len(sys.argv) < 2:
        return False

    if sys.argv[1] == "refresh-openai-models":
        if len(sys.argv) != 2:
            print("Usage: python3 proxy.py refresh-openai-models", file=sys.stderr)
            sys.exit(2)
        sys.exit(asyncio.run(refresh_openai_model_aliases()))

    if sys.argv[1] != "generate-key":
        return False

    if len(sys.argv) != 3:
        print("Usage: python3 proxy.py generate-key <name>", file=sys.stderr)
        sys.exit(2)

    name = sys.argv[2].strip()
    if not name:
        print("Key name cannot be empty.", file=sys.stderr)
        sys.exit(2)

    api_key = create_proxy_key(name)
    print(f"Created proxy API key for {name}.")
    print(f"Stored key hash in {AUTH_KEYS_FILE}.")
    print("Use this key as the client-facing OpenAI API key:")
    print(api_key)
    return True


# TabbyAPI config path
TABBY_CONFIG_FILE = Path("/home/tnunamak/applications/tabbyapi/config.yml")

def get_tabby_model_config() -> tuple[str, str, dict]:
    """Read TabbyAPI config live. Returns (default_model, model_dir, model_params)."""
    default_model = os.environ.get("TABBY_DEFAULT_MODEL", "")
    model_dir = ""
    model_params = {}

    if TABBY_CONFIG_FILE.exists():
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
STT_BACKEND_MODEL = MODEL_COMPAT_DEFAULTS.get("transcriptions") or stt_cfg.get("default_model", "parakeet-tdt-0.6b-v3")
IMAGE_EDIT_DEFAULT_MODEL = MODEL_COMPAT_DEFAULTS.get("image_edits", "flux2-klein-4b-edit")

# Voice mapping: any voice name -> a Voxtral voice the backend accepts.
# Single source of truth used by both `/v1/audio/speech` (request rewriting)
# and `/v1/voices` (listing). Listing reflects every key in this map.
VOICE_MAP = {
    # OpenAI classic voices
    "alloy": "neutral_female", "echo": "casual_male", "fable": "cheerful_female",
    "nova": "casual_female", "onyx": "neutral_male", "shimmer": "cheerful_female",
    # OpenAI newer voices -> best match
    "sophia": "neutral_female", "isabella": "cheerful_female", "lily": "casual_female",
    "evan": "casual_male", "adam": "neutral_male", "ash": "casual_male",
    "coral": "casual_female", "sage": "neutral_female", "verse": "neutral_male",
    "ballad": "cheerful_female", "juniper": "casual_female",
    # Voxtral native voices (pass through)
    "casual_female": "casual_female", "casual_male": "casual_male",
    "cheerful_female": "cheerful_female",
    "neutral_female": "neutral_female", "neutral_male": "neutral_male",
    "ar_male": "ar_male",
    "de_female": "de_female", "de_male": "de_male",
    "es_female": "es_female", "es_male": "es_male",
    "fr_female": "fr_female", "fr_male": "fr_male",
    "hi_female": "hi_female", "hi_male": "hi_male",
    "it_female": "it_female", "it_male": "it_male",
    "nl_female": "nl_female", "nl_male": "nl_male",
    "pt_female": "pt_female", "pt_male": "pt_male",
}
DEFAULT_VOICE = "neutral_female"

# Voice metadata derived from the id. Keep in sync with VOICE_MAP keys.
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
    # If set, the resolved role's preferred backend ("kobold" or "tabby"). The
    # request will route there if that backend is hot, otherwise fall back to
    # whichever backend is hot (same graceful behavior as OpenAI-alias routing).
    role_backend: Optional[str] = None
    # If set, the role pinned a specific GGUF path. The request handler will swap
    # llama-turbo to this model (if needed) before forwarding.
    role_model_path: Optional[str] = None
    # If set, the role/catalog entry pinned an mmproj path that should be paired
    # with role_model_path on swap. None means the model is text-only — the proxy
    # will write an empty LLAMA_TURBO_MMPROJ so start_service.sh omits --mmproj.
    role_mmproj_path: Optional[str] = None


def normalize_model_id(model_name: str) -> str:
    return (model_name or "").strip().lower()


def resolve_role_for_operation(operation: str, requested_model: str) -> Optional[ModelResolution]:
    """Recognize a logical role alias (e.g. "default", "vision", "rp") and return a
    ModelResolution that hints which backend should serve it. Returns None if the
    requested name isn't a defined role.

    Roles are checked BEFORE OpenAI-style alias resolution so that "default" is
    treated as a role even if some future OpenAI model happens to share the name.
    """
    if not requested_model:
        return None
    normalized = normalize_model_id(requested_model)
    role_def = ROLES.get(normalized) if isinstance(ROLES, dict) else None
    if isinstance(role_def, dict):
        backend = role_def.get("backend")
        role_model = role_def.get("model")
        role_mmproj = role_def.get("mmproj")
    else:
        # Fall through to the optional models: catalog. Same shape but uses `path`
        # instead of `model` and is keyed by id rather than position.
        cat_entry = MODELS_CATALOG.get(normalized)
        if not isinstance(cat_entry, dict):
            return None
        backend = cat_entry.get("backend")
        role_model = cat_entry.get("path")
        role_mmproj = cat_entry.get("mmproj")
    if backend not in ("kobold", "tabby"):
        return None
    role_model_path = str(role_model).strip() if role_model else None
    role_mmproj_path = str(role_mmproj).strip() if role_mmproj else None
    # Reuse is_openai_alias=True for the downstream rewrite path that swaps the
    # `model` field to the backend's actually-loaded model id.
    return ModelResolution(
        requested_model=requested_model,
        effective_model=requested_model,
        operation=operation,
        category="role",
        is_openai_alias=True,
        role_backend=backend,
        role_model_path=role_model_path or None,
        role_mmproj_path=role_mmproj_path or None,
    )


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
    if operation == "images":
        return COMFY_DEFAULT_MODEL
    if operation == "image_edits":
        return IMAGE_EDIT_DEFAULT_MODEL
    if operation == "videos":
        return VIDEO_DEFAULT_WORKFLOW
    if operation == "embeddings":
        return ""
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

        self.tabby_process: Optional[subprocess.Popen] = None
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

def start_tabby_process() -> subprocess.Popen:
    """Start TabbyAPI as subprocess."""
    script_dir = Path("/home/tnunamak/applications/tabbyapi")
    env = os.environ.copy()
    env["LD_PRELOAD"] = "/usr/lib/x86_64-linux-gnu/libstdc++.so.6"
    env["PYTORCH_ALLOC_CONF"] = "backend:native"

    proc = subprocess.Popen(
        [str(script_dir / "venv/bin/python"), "start.py"],
        cwd=script_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(f"[proxy] Started TabbyAPI subprocess (PID: {proc.pid})", flush=True)
    return proc


def stop_tabby_process():
    """Stop TabbyAPI subprocess gracefully."""
    if state.tabby_process and state.tabby_process.poll() is None:
        print(f"[proxy] Stopping TabbyAPI (PID: {state.tabby_process.pid})...", flush=True)
        state.tabby_process.terminate()
        try:
            state.tabby_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"[proxy] Force killing TabbyAPI...", flush=True)
            state.tabby_process.kill()
            state.tabby_process.wait()
        print(f"[proxy] TabbyAPI stopped", flush=True)


def log_tabby_output():
    """Thread to log TabbyAPI stdout."""
    if not state.tabby_process or not state.tabby_process.stdout:
        return
    for line in iter(state.tabby_process.stdout.readline, b''):
        if state.shutdown_requested:
            break
        try:
            print(line.decode('utf-8', errors='replace'), end='', flush=True)
        except:
            pass


async def wait_for_tabby_ready(timeout: int = 60) -> bool:
    """Wait for TabbyAPI to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        if state.shutdown_requested:
            return False
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{TABBY_HOST}/health", timeout=2)
                if resp.status_code == 200:
                    return True
        except:
            pass
        await asyncio.sleep(1)
    return False


async def check_tabby_health() -> bool:
    """Check if TabbyAPI is healthy."""
    if TABBY_MANAGED and state.tabby_process:
        if state.tabby_process.poll() is not None:
            return False

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{TABBY_HOST}/health", timeout=HEALTH_CHECK_TIMEOUT)
            return resp.status_code == 200
    except:
        return False


async def restart_tabby_if_needed() -> bool:
    """Restart TabbyAPI if it crashed."""
    if not TABBY_MANAGED:
        return await check_tabby_health()

    if state.tabby_process and state.tabby_process.poll() is None:
        return True

    if state.shutdown_requested:
        return False

    print(f"[proxy] TabbyAPI died, restarting...", flush=True)
    stop_tabby_process()
    state.tabby_process = start_tabby_process()
    log_thread = threading.Thread(target=log_tabby_output, daemon=True)
    log_thread.start()

    if await wait_for_tabby_ready():
        print(f"[proxy] TabbyAPI restarted successfully", flush=True)
        state.reset_load_failures()
        return True
    else:
        print(f"[proxy] TabbyAPI failed to restart", flush=True)
        return False


# =============================================================================
# TTS Backend
# =============================================================================

async def check_tts_health() -> bool:
    """Check if TTS backend is healthy."""
    if not TTS_ENABLED:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{TTS_HOST}/health", timeout=HEALTH_CHECK_TIMEOUT)
            return resp.status_code == 200
    except:
        return False


async def proxy_tts_request(request: Request, auth_context: AuthContext) -> Response:
    """Forward TTS request to TTS backend."""
    if not TTS_ENABLED:
        return Response(
            content=json.dumps({"error": "TTS not enabled"}),
            status_code=503,
            media_type="application/json"
        )

    # Voice mapping is module-scoped (single source of truth shared with /v1/voices).
    body = await request.body()
    headers = prepare_downstream_headers(request.headers, auth_context, "tts")

    # Map voice in request body
    try:
        body_json = json.loads(body)
        requested_model = body_json.get("model", "tts-1")
        try:
            model_resolution = resolve_model_for_operation("speech", requested_model)
        except ModelCompatibilityError as e:
            return model_compatibility_error(str(e))

        requested_voice = body_json.get("voice", "").lower()
        body_json["voice"] = VOICE_MAP.get(requested_voice, DEFAULT_VOICE)
        body_json["model"] = model_resolution.effective_model or TTS_BACKEND_MODEL
        body = json.dumps(body_json).encode()
        pop_header(headers, "content-type")
        headers["content-type"] = "application/json"
    except (json.JSONDecodeError, AttributeError):
        pass

    url = f"{TTS_HOST}/v1/audio/speech"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TTS_TIMEOUT, connect=10)) as client:
            resp = await client.post(url, headers=headers, content=body)

            # TTS returns audio data, stream it back
            content_type = resp.headers.get("content-type", "audio/mpeg")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=content_type
            )

    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": "TTS request timeout"}),
            status_code=504,
            media_type="application/json"
        )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": f"TTS backend unavailable at {TTS_HOST}"}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": f"TTS proxy error: {str(e)}"}),
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
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{STT_HOST}/health", timeout=HEALTH_CHECK_TIMEOUT)
            return resp.status_code == 200
    except:
        return False


async def proxy_stt_request(request: Request, auth_context: AuthContext) -> Response:
    """Forward STT request to STT backend."""
    if not STT_ENABLED:
        return Response(
            content=json.dumps({"error": "STT not enabled"}),
            status_code=503,
            media_type="application/json"
        )

    headers = prepare_downstream_headers(request.headers, auth_context, "stt")

    url = f"{STT_HOST}/v1/audio/transcriptions"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(STT_TIMEOUT, connect=10)) as client:
            if "multipart/form-data" in request.headers.get("content-type", ""):
                form = await request.form()
                requested_model = str(form.get("model", "whisper-1"))
                try:
                    model_resolution = resolve_model_for_operation("transcriptions", requested_model)
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

                pop_header(headers, "content-type")
                pop_header(headers, "content-length")
                def send_multipart_stt_request():
                    with httpx.Client(timeout=httpx.Timeout(STT_TIMEOUT, connect=10)) as sync_client:
                        return sync_client.post(url, headers=headers, data=data, files=files)

                resp = await asyncio.to_thread(send_multipart_stt_request)
            else:
                body = await request.body()
                resp = await client.post(url, headers=headers, content=body)

            content_type = resp.headers.get("content-type", "application/json")
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=content_type
            )

    except httpx.TimeoutException:
        return Response(
            content=json.dumps({"error": "STT request timeout"}),
            status_code=504,
            media_type="application/json"
        )
    except httpx.ConnectError:
        return Response(
            content=json.dumps({"error": f"STT backend unavailable at {STT_HOST}"}),
            status_code=502,
            media_type="application/json"
        )
    except Exception as e:
        return Response(
            content=json.dumps({"error": f"STT proxy error: {str(e)}"}),
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
        return None
    with open(workflow_path) as f:
        workflow = json.load(f)
    ui = _load_ui_workflow(workflow_path)
    if ui is not None:
        workflow["__ui_workflow__"] = ui
    return workflow


async def check_comfy_health() -> bool:
    """Check if ComfyUI backend is healthy."""
    if not COMFY_ENABLED:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{COMFY_HOST}/system_stats", timeout=HEALTH_CHECK_TIMEOUT)
            return resp.status_code == 200
    except:
        return False


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
#     they can dereference (signed + auth-checked); clients that ask for `n=4`
#     get 4 images (or a 503 with Retry-After if VRAM can't fit even one).
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
    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "system",
                "content": (
                    "/no_think\n"
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
        "chat_template_kwargs": {"enable_thinking": False},
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
    msg = f"{image_id}:{expires}".encode("utf-8")
    return hashlib.sha256(_image_signing_secret() + msg).hexdigest()


def verify_image_signature(image_id: str, expires: int, sig: str) -> bool:
    """Constant-time HMAC verification (sha256(secret || msg))."""
    if expires < int(time.time()):
        return False
    expected = _sign_image_token(image_id, expires)
    if len(expected) != len(sig):
        return False
    diff = 0
    for a, b in zip(expected, sig):
        diff |= ord(a) ^ ord(b)
    return diff == 0


def store_image_bytes(content: bytes) -> str:
    """Write `content` to the content-addressed cache. Returns the image_id."""
    digest = hashlib.sha256(content).hexdigest()
    image_id = f"img_{digest}"
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = IMAGE_CACHE_DIR / f"{digest}.png"
    if not target.exists():
        # Atomic write: tmp + rename.
        tmp = target.with_suffix(".png.tmp")
        tmp.write_bytes(content)
        tmp.replace(target)
    return image_id


def image_cache_path(image_id: str) -> Optional[Path]:
    if not image_id.startswith("img_"):
        return None
    digest = image_id[len("img_"):]
    if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        return None
    return IMAGE_CACHE_DIR / f"{digest}.png"


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
                for entry in IMAGE_CACHE_DIR.iterdir():
                    if not entry.is_file() or entry.suffix != ".png":
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


# OpenAI's documented set of valid reasoning_effort values on /v1/chat/completions.
# Per OpenAI docs (May 2026): gpt-5.1+ accepts "none", gpt-5 accepts "minimal",
# o-series/GPT-5 accept "low"/"medium"/"high", gpt-5.2 and codex variants add
# "xhigh". We accept the union and return 400 on anything else.
_REASONING_EFFORT_VALUES = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

# Effort values that should suppress thinking on local reasoning models that have
# a binary thinking switch (Qwen3.6's /no_think + chat_template_kwargs.enable_thinking).
_REASONING_EFFORT_SUPPRESS = frozenset({"none", "minimal"})


def apply_chat_completions_reasoning_effort(body_json: dict) -> Optional[str]:
    """Translate OpenAI's `reasoning_effort` on /v1/chat/completions into signals
    the local backend (llama-server) understands. Mutates body_json in place.

    Returns an error message string if the value is invalid (caller should emit
    a 400 in OpenAI's error shape). Returns None on success or no-op.

    Local models (Qwen3.6 family) have a binary thinking switch, not graded effort
    tiers, so we map:
      - "none" / "minimal" -> /no_think + chat_template_kwargs.enable_thinking=false
      - "low" / "medium" / "high" / "xhigh" -> no-op (model thinks normally)
    The field itself is always stripped before forwarding so llama-server doesn't
    see an unknown parameter.
    """
    if "reasoning_effort" not in body_json:
        return None
    effort = body_json.pop("reasoning_effort")
    if effort is None:
        return None
    if not isinstance(effort, str) or effort not in _REASONING_EFFORT_VALUES:
        return (
            f"Invalid value for 'reasoning_effort': {effort!r}. "
            f"Expected one of: {sorted(_REASONING_EFFORT_VALUES)}."
        )
    if effort not in _REASONING_EFFORT_SUPPRESS:
        return None

    # Suppress thinking. Belt-and-suspenders: chat template kwarg (preferred,
    # llama-server understands it for Qwen3 family) plus /no_think system hint
    # (fallback for backends that ignore the kwarg).
    kwargs = body_json.setdefault("chat_template_kwargs", {})
    if isinstance(kwargs, dict):
        kwargs.setdefault("enable_thinking", False)

    messages = body_json.get("messages")
    if isinstance(messages, list):
        hint = "/no_think"
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "system":
                existing = m.get("content") or ""
                if isinstance(existing, str) and hint not in existing:
                    messages[i] = {**m, "content": (existing + ("\n" if existing else "") + hint)}
                return None
        messages.insert(0, {"role": "system", "content": hint})
    return None


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
) -> Response:
    """Shape `data[]` for either b64_json or url, honoring the client's choice."""
    data: list[dict] = []
    for raw in images:
        if response_format == "url":
            image_id = store_image_bytes(raw)
            data.append({"url": build_signed_image_url(request, image_id)})
        else:
            b64 = base64.b64encode(raw).decode("utf-8")
            data.append({"b64_json": b64})
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

    return _build_image_response_payload(request, images_collected, response_format, response_headers)


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
    response_format = body.get("response_format", "b64_json")
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
    manifest = _find_image_manifest("image.generate", str(model))
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
        )
        if decision.denied:
            return _policy_denied_response(decision)
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
    if size_node and size_node in workflow:
        workflow[size_node]["inputs"]["width"] = width
        workflow[size_node]["inputs"]["height"] = height
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

    image_file = form.get("image")
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
    _record_image_input(receipt, "image")

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
                )
                if decision.denied:
                    return _policy_denied_response(decision)
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


async def _resolve_input_reference(value) -> tuple[bytes, str]:
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
        for key in ("image_url", "url", "input_image"):
            inner = value.get(key)
            if isinstance(inner, dict):
                inner = inner.get("url") or inner.get("image_url")
            if isinstance(inner, str):
                return await _resolve_input_reference(inner)
        raise _InputReferenceError(
            "input_reference dict missing a recognizable image_url/url field"
        )

    if not isinstance(value, str):
        raise _InputReferenceError(f"input_reference must be a string, got {type(value).__name__}")

    s = value.strip()
    if not s:
        raise _InputReferenceError("input_reference is empty")

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


async def proxy_comfy_video_create(request: Request, auth_context: AuthContext) -> Response:
    """POST /v1/videos — submit a job and return {video_id, status: queued}."""
    if not COMFY_ENABLED:
        return Response(
            content=json.dumps({"error": {"message": "Video generation not enabled", "type": "service_unavailable"}}),
            status_code=503,
            media_type="application/json"
        )

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
            image_bytes, image_filename = await _resolve_input_reference(input_reference)
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
            payload = {
                "id": video_id,
                "object": "video.generation",
                "model": workflow_name,
                "status": "queued",
                "progress": 0,
                "created_at": now,
                "expires_at": now + 86400,
                "size": f"{width}x{height}",
                "seconds": seconds_val,
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
      - When the proxy itself manages tabby as a child subprocess
        (TABBY_MANAGED=True), restart it if it crashes. Throttled logging.
      - Idle-timeout unload of an in-process loaded tabby model.
    """
    # Throttle "TabbyAPI failed to restart" logging to once per failure-streak.
    tabby_restart_failure_logged = False

    while not state.shutdown_requested:
        await asyncio.sleep(CHECK_INTERVAL)

        if state.shutdown_requested:
            break

        # Only revive tabby if WE own its lifecycle (subprocess mode). When
        # tabby runs as a separate systemd unit (managed: false), backend
        # selection is request-driven via ensure_target — don't poll here.
        if TABBY_MANAGED:
            if not await check_tabby_health():
                if await restart_tabby_if_needed():
                    tabby_restart_failure_logged = False
                else:
                    if not tabby_restart_failure_logged:
                        print(
                            "[proxy] Failed to restart managed TabbyAPI subprocess; "
                            "will keep trying silently. Investigate if this persists.",
                            flush=True,
                        )
                        tabby_restart_failure_logged = True
                continue
            else:
                tabby_restart_failure_logged = False

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
    print(f"[proxy] OpenAI Proxy starting on port {PROXY_PORT}", flush=True)
    print(f"[proxy] TabbyAPI backend: {TABBY_HOST}", flush=True)
    print(f"[proxy] TTS backend: {TTS_HOST} (enabled: {TTS_ENABLED})", flush=True)
    print(f"[proxy] STT backend: {STT_HOST} (enabled: {STT_ENABLED})", flush=True)
    print(f"[proxy] ComfyUI backend: {COMFY_HOST} (enabled: {COMFY_ENABLED})", flush=True)
    print(f"[proxy] Proxy auth: {'enabled' if AUTH_ENABLED else 'disabled'} ({len(AUTH_REGISTRY)} key(s))", flush=True)
    print(f"[proxy] Model compatibility: {'enabled' if MODEL_COMPAT_ENABLED else 'disabled'}", flush=True)
    if AUTH_ENABLED and not AUTH_REGISTRY:
        print(f"[proxy] WARNING: No proxy API keys configured; proxied requests will be rejected", flush=True)

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

    if TABBY_MANAGED:
        print(f"[proxy] Starting TabbyAPI...", flush=True)
        state.tabby_process = start_tabby_process()
        log_thread = threading.Thread(target=log_tabby_output, daemon=True)
        log_thread.start()

        if not await wait_for_tabby_ready():
            print(f"[proxy] ERROR: TabbyAPI failed to start", flush=True)
            stop_tabby_process()
            sys.exit(1)

        print(f"[proxy] TabbyAPI ready", flush=True)

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
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] Media job store failed to start: {e}", flush=True)

    yield

    state.shutdown_requested = True
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
    if TABBY_MANAGED:
        stop_tabby_process()


app = FastAPI(lifespan=lifespan)

_MANIFEST_WORKFLOWS_DIR = Path(__file__).resolve().parent / "manifests" / "workflows"


def _load_gateway_workflow_registry() -> WorkflowRegistry:
    try:
        registry = WorkflowRegistry.from_directory(_MANIFEST_WORKFLOWS_DIR)
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
        ),
    )


def _gateway_capabilities_provider(request: Request) -> list[Capability]:
    auth_context = _auth_context_from_scope(request)
    capabilities = list(build_local_capabilities(
        config,
        enabled_flags={
            "llm": bool(kobold_cfg) or bool(tabby_cfg),
            "embeddings": True,
            "tts": TTS_ENABLED,
            "stt": STT_ENABLED,
            "images": COMFY_ENABLED,
            "videos": COMFY_ENABLED,
            "audio": COMFY_ENABLED,
        },
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

    return [
        _capability_for_profile(capability, auth_context)
        for capability in capabilities
    ]


def _gateway_audience_resolver(request: Request) -> str:
    auth_context = _auth_context_from_scope(request)
    return discovery_audience_for_profile(_profile_for_auth_context(auth_context))


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


app.include_router(
    create_gateway_router(
        GATEWAY_WORKFLOW_REGISTRY,
        audience_resolver=_gateway_audience_resolver,
        capabilities_provider=_gateway_capabilities_provider,
        receipt_store_path=receipt_store.DB_PATH,
        media_job_store_path=media_jobs.DB_PATH,
    )
)


# Endpoint paths excluded from the firehose per-key request counter.
# These are health/status probes hammered by the dashboard itself or
# routes that don't represent real "API caller" activity.
_FIREHOSE_NO_RECORD_PREFIXES = (
    "/health",
    "/v1/status",
    "/v1/audio/health",
    "/_next/",
    "/firehose",
    "/favicon.ico",
    "/static/",
    "/SKILL.md",
    "/robots.txt",
    "/.well-known/",
)
# Exact paths (not prefixes) that should be excluded too.
_FIREHOSE_NO_RECORD_EXACT = {"/", "/llms.txt"}

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
}


@app.middleware("http")
async def _firehose_request_counter(request: Request, call_next):
    """Record per-API-key request activity for the firehose status panel.

    Identity is resolved best-effort by hashing the bearer token against
    AUTH_REGISTRY. Body is never read; this only inspects path + status.
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
                if AUTH_ENABLED and AUTH_REGISTRY:
                    raw = get_client_api_key_from_headers(request.headers)
                    if raw:
                        ident = AUTH_REGISTRY.get(hash_api_key(raw))
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


@app.get("/health")
async def health():
    """Proxy service health. Public so external health checks do not need API keys."""
    return {
        "status": "healthy",
        "auth_enabled": AUTH_ENABLED,
        "proxy_keys_configured": len(AUTH_REGISTRY),
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


@app.get("/.well-known/ai-gateway", include_in_schema=False)
async def well_known_ai_gateway(request: Request):
    """Unauthenticated agent-client discovery for this AI gateway."""
    return build_gateway_discovery_metadata(base_url=_public_base_url(request))


@app.get("/.well-known/openai-proxy", include_in_schema=False)
async def well_known_openai_proxy(request: Request):
    """Alias of /.well-known/ai-gateway for OpenAI-style SDK clients."""
    return build_gateway_discovery_metadata(base_url=_public_base_url(request))


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


@app.get("/v1/voices")
@app.get("/v1/audio/voices")
async def list_voices(request: Request):
    """Return available TTS voices (OpenAI-compatible names)."""
    auth_context = authenticate_request(request, request.url.path)
    if isinstance(auth_context, Response):
        return auth_context

    # Single source of truth: derive every voice the speech endpoint accepts.
    # If you can pass it to /v1/audio/speech, you can find it here.
    voices = []
    for voice_id in sorted(VOICE_MAP.keys()):
        mapped = VOICE_MAP[voice_id]
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


@app.post("/v1/audio/speech")
async def audio_speech(request: Request):
    """OpenAI-compatible TTS endpoint - routes to TTS backend with voice mapping."""
    auth_context = authenticate_request(request, "/v1/audio/speech")
    if isinstance(auth_context, Response):
        return auth_context
    return await proxy_tts_request(request, auth_context)


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request):
    """OpenAI-compatible STT endpoint - routes to STT backend."""
    auth_context = authenticate_request(request, "/v1/audio/transcriptions")
    if isinstance(auth_context, Response):
        return auth_context
    return await proxy_stt_request(request, auth_context)


# Map image route paths to canonical intents for receipt construction.
_IMAGE_ROUTE_INTENTS = {
    "/v1/images/generations": "image.generate",
    "/v1/images/edits": "image.edit",
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
    profile = _profile_for_auth_context(auth_context)
    receipt = RouteReceipt(
        protocol="openai.images",
        path=path,
        intent=intent,
        backend="comfyui",
        api_key_name=key_name,
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
) -> PolicyDecision:
    return evaluate_and_apply(
        auth_context.identity,
        make_policy_request(
            protocol="openai.images",
            intent=receipt.intent,
            model=model,
            workflow=workflow,
            backend=receipt.backend,
        ),
        receipt,
    )


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
    manifests_dir = _MANIFEST_WORKFLOWS_DIR
    if not manifests_dir.is_dir():
        return None
    candidate_ids = {
        f"{intent}.{resolved_model}",
        f"{intent}.{resolved_model.removesuffix('-edit')}",
    }
    for path in sorted(manifests_dir.glob("*.yml")):
        try:
            manifest = load_workflow_manifest(path)
        except (ManifestError, OSError, ValueError):
            continue
        mid = str(manifest.get("id") or "")
        if manifest.get("intent") != intent:
            continue
        if mid in candidate_ids or mid.endswith(f".{resolved_model}"):
            return manifest
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
        if field_name is None:
            continue

        input_key = str(input_name)
        spec = inputs.get(input_key)
        if field_name not in allowed_fields:
            continue
        if input_key not in supported or not isinstance(spec, Mapping):
            continue
        if spec.get("type") != "asset":
            continue

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


@app.post("/v1/images/generations")
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


@app.post("/v1/images/edits")
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


@app.get("/v1/images/{image_id}/content")
async def images_content(image_id: str, request: Request):
    """Serve a cached image referenced by a signed URL.

    Auth model: same as the rest of `/v1/*` (a valid proxy key) AND a valid
    HMAC signature with a non-expired `expires` query param. Both checks are
    required: the bearer token gates the proxy itself, the signature binds
    the URL to a specific image_id and TTL.
    """
    auth_context = authenticate_request(request, "/v1/images/{image_id}/content")
    if isinstance(auth_context, Response):
        return auth_context

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
    if not verify_image_signature(image_id, expires, sig):
        return Response(
            content=json.dumps({"error": {"message": "Invalid or expired signature", "type": "invalid_request_error"}}),
            status_code=403,
            media_type="application/json",
        )

    cache_path = image_cache_path(image_id)
    if cache_path is None:
        return Response(
            content=json.dumps({"error": {"message": "Invalid image id", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json",
        )
    if not cache_path.exists():
        return Response(
            content=json.dumps({"error": {"message": "Image not found (may have been evicted)", "type": "invalid_request_error"}}),
            status_code=404,
            media_type="application/json",
        )

    data = cache_path.read_bytes()
    return Response(
        content=data,
        status_code=200,
        media_type="image/png",
        headers={
            "content-length": str(len(data)),
            "cache-control": "private, max-age=3600",
        },
    )


@app.post("/v1/videos")
async def videos_create(request: Request):
    """Sora-shaped video generation submit. Returns {id, status: queued}."""
    auth_context = authenticate_request(request, "/v1/videos")
    if isinstance(auth_context, Response):
        return auth_context
    return await proxy_comfy_video_create(request, auth_context)


@app.get("/v1/videos/{video_id}")
async def videos_status(video_id: str, request: Request):
    """Poll a previously-submitted video job."""
    auth_context = authenticate_request(request, "/v1/videos/{video_id}")
    if isinstance(auth_context, Response):
        return auth_context
    return await proxy_comfy_video_status(video_id, auth_context)


@app.get("/v1/videos/{video_id}/content")
async def videos_content(video_id: str, request: Request):
    """Download the rendered mp4 once status=succeeded."""
    auth_context = authenticate_request(request, "/v1/videos/{video_id}/content")
    if isinstance(auth_context, Response):
        return auth_context
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
AUDIO_MANIFESTS_BY_WORKFLOW = {
    "tangoflux": Path(__file__).resolve().parent / "manifests" / "workflows" / "tangoflux.yml",
}


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
    return workflow


def _load_audio_workflow_manifest(workflow_name: str) -> dict[str, Any] | None:
    manifest_path = AUDIO_MANIFESTS_BY_WORKFLOW.get(workflow_name)
    if manifest_path is None:
        return None
    return load_workflow_manifest(manifest_path)


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
    try:
        body = await request.json()
    except Exception:
        return Response(
            content=json.dumps({"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}),
            status_code=400,
            media_type="application/json"
        )

    prompt = (body.get("prompt") or body.get("input") or "").strip()
    model = body.get("model", AUDIO_DEFAULT_WORKFLOW)
    duration = body.get("duration", 30)
    seed = body.get("seed")
    audio_type = body.get("type", "music")  # music | sfx | ambient

    if not prompt:
        return Response(
            content=json.dumps({"error": {"message": "prompt is required", "type": "invalid_request_error"}}),
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

    manifest = _load_audio_workflow_manifest(workflow_name)
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


@app.post("/v1/audio/generate")
async def audio_generate(request: Request):
    """OpenAI-compatible audio generation endpoint."""
    auth_context = authenticate_request(request, "/v1/audio/generate")
    if isinstance(auth_context, Response):
        return auth_context
    return await proxy_audio_generate(request, auth_context)


@app.get("/v1/audio/models")
async def audio_models(request: Request):
    """List available audio generation models."""
    auth_context = authenticate_request(request, "/v1/audio/models")
    if isinstance(auth_context, Response):
        return auth_context
    models = []
    for name, cfg in AUDIO_WORKFLOWS_CFG.items():
        models.append({
            "id": name,
            "owned_by": "local",
            "permission": "generate",
            "max_seconds": cfg.get("max_seconds", 300),
            "min_seconds": cfg.get("min_seconds", 1),
        })
    return Response(
        content=json.dumps({"data": models}),
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


def _build_role_catalog_entries() -> list[dict]:
    """Construct OpenAI-shaped /v1/models entries for each configured role and
    each entry in the optional models: catalog. `id` is the role/alias name so
    clients can send `model: "<id>"` and resolve_role_for_operation will route
    them through ensure_target() (cold-swap on first use).

    `vivid_fish_*` extension fields are advisory — unknown keys are ignored by
    OpenAI-shaped clients. Model paths are exposed only in the extension field;
    no client should need them to invoke the model.
    """
    entries: list[dict] = []

    if isinstance(ROLES, dict):
        for role_name, role_def in ROLES.items():
            if not isinstance(role_def, dict):
                continue
            backend = role_def.get("backend")
            if backend not in ("kobold", "tabby"):
                continue
            model_path = role_def.get("model")
            entry = {
                "id": str(role_name),
                "object": "model",
                "created": _entry_created_ts(model_path if isinstance(model_path, str) else None),
                "owned_by": "vivid-fish",
                "description": str(role_def.get("description", "") or ""),
                "vivid_fish_role": True,
                "vivid_fish_backend": backend,
            }
            if isinstance(model_path, str) and model_path:
                entry["vivid_fish_model_path"] = model_path
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

    The model id is the workflow JSON basename because the image routes load
    `workflows/images/<model>.json` directly. Keep local paths out of the
    public payload; gateway-native discovery has the fuller contract.
    """
    entries: list[dict] = []
    for record in registry.records():
        if not record.intent.startswith("image."):
            continue
        bindings = record.manifest.get("bindings")
        if not isinstance(bindings, dict):
            continue
        comfy = bindings.get("comfyui")
        if not isinstance(comfy, dict):
            continue
        workflow_artifact = comfy.get("workflow")
        if not isinstance(workflow_artifact, str) or not workflow_artifact:
            continue
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
            "vivid_fish_backend": "comfyui",
        }
        version = record.manifest.get("version")
        if version:
            entry["vivid_fish_manifest_version"] = str(version)
        entries.append(entry)
    return entries


@app.get("/v1/models")
async def list_models(request: Request):
    """OpenAI-compatible model list. Returns:
      1. One entry per role in proxy_config.yml's roles: block (id = role name)
      2. One entry per alias in the optional models: catalog (id = alias)
      3. One entry per directly invocable image workflow manifest
      4. The active backend's own /v1/models response (Tabby's catalog, kobold's
         currently-loaded GGUF metadata)
      5. The currently-loaded kobold GGUF (filename form only — full-path
         duplicate dropped; clients should select via role/alias names)
    """
    auth_context = authenticate_request(request, "/v1/models")
    if isinstance(auth_context, Response):
        return auth_context

    # Forward to whichever backend is active so OpenAI-style aliases stay intact.
    active = await get_active_backend()
    target_host = TABBY_HOST if active == "tabby" else KOBOLD_HOST
    target_service = "tabby" if active == "tabby" else "kobold"
    headers = prepare_downstream_headers(
        request.headers,
        auth_context,
        target_service,
        "/v1/models",
    )

    upstream_payload: dict = {"object": "list", "data": []}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as client:
            resp = await client.get(f"{target_host}/v1/models", headers=headers)
            if resp.status_code == 200:
                try:
                    payload = resp.json()
                    if isinstance(payload, dict):
                        upstream_payload = payload
                except Exception:
                    pass
    except Exception as e:
        print(f"[proxy] /v1/models upstream fetch failed: {e}", flush=True)

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

    # Prepend role/catalog and workflow entries so they appear at the top of
    # the list. Workflow entries let OpenAI-shaped image clients discover ids
    # such as `sdxl-seamless-tile` without knowing the gateway manifest API.
    role_entries = _build_role_catalog_entries()
    workflow_entries = _build_workflow_model_entries()
    existing_ids = {item.get("id") for item in data if isinstance(item, dict)}
    for entry in role_entries + workflow_entries:
        if entry["id"] not in existing_ids:
            data.insert(0, entry)
            existing_ids.add(entry["id"])

    # Surface the currently-loaded GGUF (filename form only) so clients can see
    # what's actually hot. Skipped if some upstream entry already has it.
    loaded_gguf = currently_loaded_kobold_model()
    if loaded_gguf:
        loaded_name = os.path.basename(loaded_gguf)
        if loaded_name and loaded_name not in existing_ids:
            data.append({
                "id": loaded_name,
                "object": "model",
                "owned_by": "kobold",
                "created": _entry_created_ts(loaded_gguf),
                "vivid_fish_loaded": True,
                "vivid_fish_model_path": loaded_gguf,
            })

    upstream_payload["data"] = data
    upstream_payload["object"] = upstream_payload.get("object") or "list"
    return Response(
        content=json.dumps(upstream_payload),
        status_code=200,
        media_type="application/json",
    )


@app.get("/v1/status")
async def v1_status(request: Request):
    """Consolidated status JSON for the dashboard.

    Auth: bearer token required UNLESS the request originates from 127.0.0.1
    (loopback bypass — local browsers + curl from the host get a friction-free
    view; remote users authenticate with a proxy key as usual).
    """
    client_host = (request.client.host if request.client else "") or ""
    is_loopback = client_host in ("127.0.0.1", "::1", "localhost")
    if not is_loopback:
        auth_context = authenticate_request(request, "/v1/status")
        if isinstance(auth_context, Response):
            return auth_context

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
# One page only: the maximalist server-rendered HTML lives at `/`. The old
# `/firehose` URL is kept as a 301 → `/` for backwards compatibility. The
# previous Next.js page under status-page/out is no longer served.
# -----------------------------------------------------------------------------

async def _render_status_html(request: Request) -> Response:
    """Build a payload from /v1/status and render the dashboard HTML."""
    client_host = (request.client.host if request.client else "") or ""
    is_loopback = client_host in ("127.0.0.1", "::1", "localhost")
    if not is_loopback:
        auth_context = authenticate_request(request, request.url.path)
        if isinstance(auth_context, Response):
            return auth_context
    try:
        payload = await status_endpoint.get_cached_status(config)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] status render fatal: {e}", flush=True)
        payload = {"error": "status build failed"}
    try:
        from firehose_render import render_html
        # Use the request's Host header so the page header reflects the
        # address bar (loopback dev vs. external openai.vivid.fish).
        host_hdr = request.headers.get("host") or ""
        body = render_html(payload, host=host_hdr)
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
async def status_dashboard_index(request: Request):
    return await _render_status_html(request)


@app.get("/firehose", include_in_schema=False)
async def status_dashboard_firehose(request: Request):
    """Backwards compatibility: redirect old `/firehose` URL to `/`."""
    return Response(
        status_code=301,
        headers={"location": "/", "cache-control": "no-store"},
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
    url = f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions"
    # Carry the original Authorization header so authenticate_request on the
    # loopback call resolves to the same identity.
    headers = {"content-type": "application/json"}
    auth_hdr = request.headers.get("authorization")
    if auth_hdr:
        headers["authorization"] = auth_hdr
    api_key_hdr = request.headers.get("x-api-key")
    if api_key_hdr:
        headers["x-api-key"] = api_key_hdr

    body = json.dumps(cc_body).encode("utf-8")
    # Loopback to /v1/chat/completions — inner forwarder already enforces
    # UPSTREAM_READ_TIMEOUT against the LLM backend, so the outer read here
    # only needs to bound the in-process round-trip (loopback is localhost
    # and proxy code is non-blocking). Match UPSTREAM_READ_TIMEOUT so this
    # never trips before the inner timeout does.
    client = httpx.AsyncClient(timeout=httpx.Timeout(
        connect=10.0,
        read=UPSTREAM_READ_TIMEOUT,
        write=60.0,
        pool=None,
    ))
    if stream:
        req = client.build_request("POST", url, headers=headers, content=body)
        resp = await client.send(req, stream=True)
        return client, resp
    try:
        resp = await client.post(url, headers=headers, content=body)
        return resp
    finally:
        if not stream:
            await client.aclose()


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


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    """Anthropic Messages-compatible non-streaming frontend over chat completions."""
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

    if canonical.get("stream"):
        return _json_error_response(
            anthropic_protocol.unsupported_feature_error(
                "streaming",
                detail="the Anthropic frontend currently supports non-streaming requests only",
            ),
            status_code=400,
        )

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


async def _ollama_forward_non_streaming(
    request: Request,
    auth_context: AuthContext,
    canonical: dict[str, Any],
    *,
    protocol: str,
    intent: str,
) -> Response | dict[str, Any]:
    if canonical.get("stream"):
        return _ollama_error_response(
            "unsupported operation: streaming is not available through this frontend",
            status_code=400,
        )
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


@app.post("/api/chat")
async def ollama_chat(request: Request):
    """Ollama `/api/chat` non-streaming frontend over chat completions."""
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


@app.post("/api/generate")
async def ollama_generate(request: Request):
    """Ollama `/api/generate` non-streaming frontend over chat completions."""
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
            _build_role_catalog_entries()
        ),
        status_code=200,
    )


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


@app.get("/v1/responses/{response_id}")
async def retrieve_response(response_id: str, request: Request):
    auth_context = authenticate_request(request, "/v1/responses")
    if isinstance(auth_context, Response):
        return auth_context
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


@app.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, request: Request):
    auth_context = authenticate_request(request, "/v1/responses")
    if isinstance(auth_context, Response):
        return auth_context
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


@app.post("/v1/chat/completions", openapi_extra=_OPENAI_COMPAT_JSON_BODY_OPENAPI)
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions proxied to the active LLM backend."""
    return await proxy_request(request, "v1/chat/completions")


@app.post("/v1/completions", openapi_extra=_OPENAI_COMPAT_JSON_BODY_OPENAPI)
async def completions(request: Request):
    """OpenAI-compatible text completions proxied to the active LLM backend."""
    return await proxy_request(request, "v1/completions")


@app.post("/v1/embeddings", openapi_extra=_OPENAI_COMPAT_JSON_BODY_OPENAPI)
async def embeddings(request: Request):
    """OpenAI-compatible embeddings proxied to the configured embedding backend."""
    return await proxy_request(request, "v1/embeddings")


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_request(request: Request, path: str):
    """Proxy all other requests to TabbyAPI."""
    request_path = f"/{path}"

    auth_context = authenticate_request(request, request_path)
    if isinstance(auth_context, Response):
        return auth_context

    # Update activity (unless it's a health/status/tts endpoint)
    is_activity = not any(request_path.startswith(p) for p in NO_ACTIVITY_PATHS)
    if is_activity:
        state.update_activity()

    # Determine target backend based on model in request
    target_host = TABBY_HOST  # default
    body_peek = await request.body()
    model_resolution: Optional[ModelResolution] = None
    # Fix 3: vision pre-flight + routing flag. Values: "not-needed", "true",
    # "false". Stamped on the response as X-Vision-Routed.
    vision_routed_state: str = "not-needed"

    if request_path in ["/v1/chat/completions", "/v1/completions"]:
        try:
            body_json = json.loads(body_peek)
            if request_path == "/v1/chat/completions":
                try:
                    extension_result = validate_chat_completions_extensions(body_json)
                except ExtensionError as e:
                    return _extension_validation_error_response({"extensions": str(e)})
                if not extension_result.ok:
                    return _extension_validation_error_response(extension_result.rejected)
                body_json = extension_result.request
                body_peek = json.dumps(body_json).encode()
            model_name = body_json.get("model", "")
            operation = "chat" if request_path == "/v1/chat/completions" else "completions"
            # Fix 3: scan for image content blocks BEFORE routing. We need this
            # decision early because soft-path routing (OpenAI alias on a hot
            # text-only backend) would otherwise drop the images silently.
            vision_needed = has_vision_content(body_json.get("messages")) if request_path == "/v1/chat/completions" else False
            if model_name:
                # Resolution priority:
                #   1. Literal absolute path to a .gguf file -> kobold-pinned ad-hoc model
                #   2. Role name ("default", "vision", "rp", ...)
                #   3. OpenAI-style alias / filename match
                role_res = None
                if is_literal_gguf_path(model_name):
                    role_res = ModelResolution(
                        requested_model=model_name,
                        effective_model=model_name,
                        operation=operation,
                        category="literal_gguf",
                        is_openai_alias=True,
                        role_backend="kobold",
                        role_model_path=model_name,
                    )
                else:
                    role_res = resolve_role_for_operation(operation, model_name)

                if role_res:
                    model_resolution = role_res
                else:
                    try:
                        model_resolution = resolve_model_for_operation(operation, model_name)
                    except ModelCompatibilityError as e:
                        return model_compatibility_error(str(e))

                if model_resolution.is_openai_alias:
                    preferred = model_resolution.role_backend
                    pinned_model = model_resolution.role_model_path
                    pinned_mmproj = model_resolution.role_mmproj_path

                    if preferred and pinned_model:
                        # Hard contract: backend MUST be `preferred` and the loaded
                        # model+mmproj MUST match `pinned_model`/`pinned_mmproj`.
                        # Cold-start + swap as needed. Any failure -> 503 (no silent fallback).
                        ok = await ensure_target(preferred, pinned_model, pinned_mmproj)
                        if not ok:
                            # Diagnose: was this a Conflicts=-blocked switch? If
                            # the OTHER LLM backend is currently hot, switching
                            # would have stopped it. Surface that explicitly.
                            active_now = await get_active_backend()
                            other = "tabby" if preferred == "kobold" else "kobold"
                            if active_now == other:
                                msg = (
                                    f"Cannot satisfy request for '{model_resolution.requested_model}': "
                                    f"required backend '{preferred}' is not running, and starting it "
                                    f"would stop the active '{other}' backend (systemd Conflicts=). "
                                    f"Either request a model served by '{other}', or run "
                                    f"'llm-switch {preferred}' to swap backends explicitly."
                                )
                            else:
                                msg = (
                                    f"Failed to satisfy request for model "
                                    f"'{model_resolution.requested_model}': could not start "
                                    f"backend '{preferred}' or load model '{pinned_model}'."
                                )
                            return Response(
                                content=json.dumps({
                                    "error": {
                                        "message": msg,
                                        "type": "service_unavailable",
                                    }
                                }),
                                status_code=503,
                                media_type="application/json",
                            )
                        target_host = TABBY_HOST if preferred == "tabby" else KOBOLD_HOST
                    else:
                        # Soft path: role with no pinned model (e.g. `vision`) or a
                        # plain OpenAI alias. Honor preferred backend if hot, else
                        # fall back to whichever backend is hot. No auto-swap.
                        active = await get_active_backend()
                        chosen = preferred if (preferred and active == preferred) else active
                        if chosen == "kobold":
                            target_host = KOBOLD_HOST
                        elif chosen == "tabby":
                            target_host = TABBY_HOST
                else:
                    backend_host = await ensure_backend_for_model(model_name)
                    if backend_host:
                        target_host = backend_host
                    else:
                        return Response(
                            content=json.dumps({"detail": f"No backend available for model '{model_name}'"}),
                            status_code=503,
                            media_type="application/json"
                        )
            else:
                default_model = default_model_for_operation(operation)
                if not default_model:
                    return model_compatibility_error(f"No local default model is configured for {operation} requests.")
                model_resolution = ModelResolution(
                    requested_model="",
                    effective_model=default_model,
                    operation=operation,
                    category="local_default",
                    is_openai_alias=True,
                )
                active = await get_active_backend()
                if active == "kobold":
                    target_host = KOBOLD_HOST
                elif active == "tabby":
                    target_host = TABBY_HOST

            # Fix 3: vision routing decision. If the request carries images,
            # ensure the chosen backend can actually serve them. Order:
            #   - kobold + mmproj loaded -> vision="true" (no switch)
            #   - tabby (always vision-capable in this deployment) -> vision="true"
            #   - else, attempt to switch to roles.vision's backend
            #   - else, 400 — better than a silent drop.
            if vision_needed:
                kobold_capable = (target_host == KOBOLD_HOST) and (kobold_has_mmproj() is True)
                tabby_capable = (target_host == TABBY_HOST)
                if kobold_capable or tabby_capable:
                    vision_routed_state = "true"
                else:
                    vision_role = ROLES.get("vision") if isinstance(ROLES, dict) else None
                    vision_backend = (vision_role or {}).get("backend") if isinstance(vision_role, dict) else None
                    vision_model = (vision_role or {}).get("model") if isinstance(vision_role, dict) else None
                    vision_mmproj = (vision_role or {}).get("mmproj") if isinstance(vision_role, dict) else None
                    if vision_backend == "tabby":
                        ok = await ensure_target("tabby", vision_model)
                        if ok:
                            target_host = TABBY_HOST
                            vision_routed_state = "true"
                        else:
                            return Response(
                                content=json.dumps({
                                    "error": {
                                        "message": (
                                            "Request includes images but the vision-capable backend "
                                            "(roles.vision) could not be started."
                                        ),
                                        "type": "service_unavailable",
                                        "code": "vision_backend_unavailable",
                                    }
                                }),
                                status_code=503,
                                media_type="application/json",
                                headers={"Retry-After": "10", "X-Vision-Routed": "false"},
                            )
                    elif vision_backend == "kobold" and vision_model:
                        ok = await ensure_target("kobold", vision_model, vision_mmproj)
                        if ok and (kobold_has_mmproj() is True):
                            target_host = KOBOLD_HOST
                            vision_routed_state = "true"
                        else:
                            return Response(
                                content=json.dumps({
                                    "error": {
                                        "message": (
                                            "Request includes images but the kobold backend is not "
                                            "vision-capable (no --mmproj loaded)."
                                        ),
                                        "type": "invalid_request_error",
                                        "code": "vision_unsupported",
                                    }
                                }),
                                status_code=400,
                                media_type="application/json",
                                headers={"X-Vision-Routed": "false"},
                            )
                    else:
                        return Response(
                            content=json.dumps({
                                "error": {
                                    "message": (
                                        "Request includes images but no vision-capable backend is "
                                        "configured. Set roles.vision in proxy_config.yml or load "
                                        "an mmproj for the kobold backend."
                                    ),
                                    "type": "invalid_request_error",
                                    "code": "vision_unsupported",
                                }
                            }),
                            status_code=400,
                            media_type="application/json",
                            headers={"X-Vision-Routed": "false"},
                        )
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    elif request_path == "/v1/embeddings":
        try:
            body_json = json.loads(body_peek)
            model_name = body_json.get("model", "")
            if model_name:
                try:
                    model_resolution = resolve_model_for_operation("embeddings", model_name)
                except ModelCompatibilityError as e:
                    return model_compatibility_error(str(e))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    # For non-completions requests, check if any backend is healthy.
    # IMPORTANT (Fix 5): Do NOT silently fall through from tabby to kobold.
    # Catch-all routes (`POST /v1/model/load`, etc.) targeted at tabby are not
    # interchangeable with kobold's surface — silently flipping returns wildly
    # different output. Require explicit opt-in via `allow_backend_fallback: true`
    # in the body.
    if target_host == TABBY_HOST:
        if not await check_tabby_health():
            allow_fallback = False
            if body_peek:
                try:
                    bj = json.loads(body_peek)
                    allow_fallback = bool(bj.get("allow_backend_fallback", False)) if isinstance(bj, dict) else False
                except (json.JSONDecodeError, UnicodeDecodeError):
                    allow_fallback = False

            active = await get_active_backend()
            # The chat-completions path above already chose tabby explicitly
            # (e.g. vision role, ensure_target). Honor that contract: if tabby
            # is dead and the route requires it, surface 503 instead of a flip.
            is_completion_path = request_path in ("/v1/chat/completions", "/v1/completions", "/v1/embeddings")

            if is_completion_path:
                # Completions-shaped routes: opt-in fallback only.
                if allow_fallback and active == "kobold":
                    target_host = KOBOLD_HOST
                elif not await restart_tabby_if_needed():
                    return Response(
                        content=json.dumps({
                            "error": {
                                "message": (
                                    f"Configured backend 'tabby' is unavailable for {request_path}. "
                                    "Retry after Retry-After. To allow automatic fallback to kobold "
                                    "when applicable, pass `allow_backend_fallback: true` in the body."
                                ),
                                "type": "service_unavailable",
                                "code": "backend_unavailable",
                            }
                        }),
                        status_code=503,
                        media_type="application/json",
                        headers={"Retry-After": "10"},
                    )
            else:
                # Non-completion catch-all (model/load, admin, etc.): never flip
                # silently — those endpoints are tabby-specific. 503 with retry.
                if not await restart_tabby_if_needed():
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

    # Forward to target backend
    url = f"{target_host}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    body = body_peek  # Already read above for model detection
    effective_request_model = None
    target_service = "tabby" if target_host == TABBY_HOST else "kobold"
    headers = prepare_downstream_headers(
        request.headers,
        auth_context,
        target_service,
        request_path,
    )

    # Rewrite OpenAI model aliases to the active local model and inject Tabby vision hints.
    if request_path in ["/v1/chat/completions", "/v1/completions"] and body:
        try:
            body_json = json.loads(body)
            model_name = body_json.get("model", "")
            if model_resolution and model_resolution.is_openai_alias:
                effective_model = await effective_llm_model_for_host(target_host)
                if not effective_model:
                    effective_model = model_resolution.effective_model
                if effective_model:
                    effective_request_model = effective_model
                    body_json["model"] = effective_model
                    if model_resolution.requested_model:
                        print(f"[proxy] Model alias {model_resolution.requested_model} -> {effective_model} for {request_path}", flush=True)
                    else:
                        print(f"[proxy] Missing model -> {effective_model} for {request_path}", flush=True)
            if target_host == TABBY_HOST and body_json.get("model") and "vision" not in body_json:
                # Inject Tabby's `vision` hint. If we routed *because* the
                # request carries images, vision must be true regardless of
                # the model's name. Otherwise fall back to name pattern.
                effective_name = body_json.get("model", model_name)
                body_json["vision"] = vision_routed_state == "true" or is_vision_model(effective_name)
                print(f"[proxy] Auto-detected vision={body_json['vision']} for model {effective_name}", flush=True)
            # Translate OpenAI's `reasoning_effort` parameter into local-backend
            # signals. /v1/chat/completions only; /v1/completions doesn't accept it
            # per the OpenAI spec, but we treat both the same way (passive strip)
            # since llama-server's /v1/completions wouldn't recognize the field
            # either. Invalid values produce a 400 in OpenAI's error shape.
            err = apply_chat_completions_reasoning_effort(body_json)
            if err:
                return Response(
                    content=json.dumps({"error": {
                        "message": err,
                        "type": "invalid_request_error",
                        "param": "reasoning_effort",
                        "code": "invalid_value",
                    }}),
                    status_code=400,
                    media_type="application/json",
                )
            body = json.dumps(body_json).encode()
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # Not JSON or can't parse, forward as-is
    elif request_path == "/v1/embeddings" and model_resolution and model_resolution.is_openai_alias:
        try:
            body_json = json.loads(body)
            body_json["model"] = model_resolution.effective_model
            body = json.dumps(body_json).encode()
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

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
        # Fix 3: stamp the routing decision on every chat-completion response.
        if request_path == "/v1/chat/completions":
            response_headers["X-Vision-Routed"] = vision_routed_state

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
        if 200 <= resp.status_code < 300 and model_resolution and model_resolution.is_openai_alias and "application/json" in content_type:
            try:
                response_json = json.loads(content)
                if isinstance(response_json, dict):
                    # Surface the actually-loaded model in the response (matching
                    # OpenAI's own behavior — "gpt-4o-mini" requests get back the
                    # specific dated model that served them). Both role aliases
                    # ("default", "vision") and OpenAI aliases ("gpt-4o-mini") use
                    # this rule. Useful for observability and debugging.
                    response_model = effective_request_model or model_resolution.requested_model or model_resolution.effective_model
                    if response_model:
                        response_json["model"] = response_model
                    content = json.dumps(response_json).encode()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
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
        if await restart_tabby_if_needed():
            return Response(
                content=json.dumps({"detail": "TabbyAPI restarted, please retry"}),
                status_code=503,
                media_type="application/json"
            )
        return Response(
            content=json.dumps({"detail": "TabbyAPI connection failed"}),
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
    if TABBY_MANAGED:
        stop_tabby_process()
    sys.exit(0)


if __name__ == "__main__":
    import uvicorn

    if handle_cli_command():
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    atexit.register(lambda: stop_tabby_process() if TABBY_MANAGED else None)

    uvicorn.run(
        app,
        host=PROXY_HOST,
        port=PROXY_PORT,
        log_level="warning",
        access_log=False
    )

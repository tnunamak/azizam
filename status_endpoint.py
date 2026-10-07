"""
/v1/status endpoint for the azizam status dashboard ("firehose" build).

Design:
- Every section is wrapped so a failing probe never propagates.
- Blocking sections run in workers; network probes stay asynchronous.
- Each subprocess call has a 1-2s timeout.
- Errors return null sections + a short safe message in `errors{}`.
- Server-side cache (12-15s TTL) keeps the dashboard's 8s polling cheap.

Privacy:
- Never expose the bearer token, raw API keys, or full tracebacks.
- Only App/Credential display names from the admin store are surfaced.
- Prompt text is truncated to 200 chars and only shown for in-flight video jobs.
"""

from __future__ import annotations

from gateway.compat import get_env

import asyncio
import copy
import importlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx
import yaml

from gateway import lifecycle_drivers
from gateway.models_catalog import _availability_from_health_payload, _status_from_health_payload
from gateway.model_presentation import declared_health_success, service_health_endpoint

# Loop-owned cache and shared rebuild; all callers use the gateway's event loop.
_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL_S = 12.0
_BUILD_TASK: asyncio.Task | None = None


def _resolve_commit_hash() -> Optional[str]:
    """Resolve the proxy's git short SHA once at import time. Used for the
    page footer so the user knows which build is rendering."""
    try:
        repo_dir = Path(__file__).parent
        out = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=1.0,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except Exception:
        pass
    return None


_COMMIT_HASH: Optional[str] = _resolve_commit_hash()
_HTTP_TIMEOUT = httpx.Timeout(2.0, connect=1.0)
_SUBPROC_TIMEOUT = 1.5  # seconds per shell probe

# Cached budget read (60s TTL — file rarely changes).
_BUDGET_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_BUDGET_CACHE_TTL_S = 60.0
_SELF_DIR = Path(__file__).parent

_GPU_BUDGET_PATH = Path(
    os.environ.get("AZIZAM_GPU_BUDGET_PATH", "")
) if os.environ.get("AZIZAM_GPU_BUDGET_PATH") else None
_VRAM_ESTIMATES_PATH = Path(
    os.environ.get("AZIZAM_VRAM_ESTIMATES_PATH", str(_SELF_DIR / "cache" / "vram_estimates.json"))
)
_DRIFT_REPORT_PATH = Path(
    os.environ.get("AZIZAM_DRIFT_REPORT_PATH", str(_SELF_DIR / "cache" / "drift_status.json"))
)
_IMAGE_CACHE_DIR = Path(
    os.environ.get("AZIZAM_IMAGE_CACHE_DIR", str(_SELF_DIR / "cache" / "images"))
)
_DAISY_AUDIT_PATH = Path(
    os.environ.get("AZIZAM_DAISY_AUDIT_PATH", "")
) if os.environ.get("AZIZAM_DAISY_AUDIT_PATH") else None

# Host machine telemetry (disk/thermal) is operator-machine-specific and must
# never be assumed present. `AZIZAM_HOST_TELEMETRY_DISABLED` is the isolation seam
# any isolated launch mode (harness, dev, tests) sets to skip the `df`/`sensors`
# shell-outs entirely rather than leaking the runner's own host state into a
# fresh instance's health projection. `AZIZAM_DISK_PARTITIONS` lets an operator
# declare which mount points they actually want watched; it defaults to just
# `/` rather than a maintainer-specific hardcoded list.
_HOST_TELEMETRY_DISABLED = os.environ.get("AZIZAM_HOST_TELEMETRY_DISABLED", "") not in (
    "", "0", "false", "False",
)
_DISK_PARTITIONS_CONFIG = [
    p.strip() for p in os.environ.get("AZIZAM_DISK_PARTITIONS", "/").split(",") if p.strip()
]

# Hot-spot directories the firehose should surface separately from the root partition.
def _optional_path(env_key: str, default: str | None = None) -> Path | None:
    val = get_env(env_key, default or "")
    return Path(val) if val else None


_COMFY_OUTPUT_ROOT = _optional_path(
    "AZIZAM_COMFY_OUTPUT_DIR",
    os.environ.get("AZIZAM_COMFY_DIR", "") and os.path.join(os.environ["AZIZAM_COMFY_DIR"], "output") or "",
)
_HOTSPOT_DIRS = [
    entry for entry in [
        ("comfy_output_root", _COMFY_OUTPUT_ROOT),
        ("comfy_output_video", Path(_COMFY_OUTPUT_ROOT / "video") if _COMFY_OUTPUT_ROOT else None),
        ("openai_image_cache", _IMAGE_CACHE_DIR),
        ("daisy_audit", _optional_path("AZIZAM_DAISY_DIR")),
    ]
    if entry[1] is not None
]

# Service units we want to attribute VRAM to. Anything outside this list is
# bucketed into "desktop" (Plasma/Xorg/Chromium/Sunshine/etc.).
#
# Sunshine is intentionally NOT a service here — it's a streaming front-end for
# the desktop session and shares the desktop's compositor surfaces. Bucketing
# its VRAM under "desktop" matches GPU0_DESKTOP budget intent.
_SERVICE_UNITS = [
    "comfyui",
    "llama-turbo",
    "voxtral-int4-tts",
    "voxtral-tts",
    "parakeet-stt",
    "tabbyapi",
    "azizam",
    "daisy",
]


# --- subprocess helpers (1-2s budgets, never raise) ----------------------

def _run(cmd: list[str], timeout: float = _SUBPROC_TIMEOUT) -> tuple[int, str, str]:
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except Exception:
                pass
            return 124, "", "timeout"
        return p.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", f"not found: {e}"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)[:120]


def _safe_err(e: BaseException) -> str:
    s = str(e)
    if len(s) > 160:
        s = s[:160] + "..."
    return s


def _truncate(s: Optional[str], n: int = 160) -> Optional[str]:
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[:n] + "..."


def _attach_unique_app_ids_to_auth_keys(
    auth_keys: list[dict],
    app_ids_by_label: Mapping[str, set[str]],
) -> None:
    """Attach app_id to per-key status rows only when the label is unambiguous."""
    for row in auth_keys:
        if not isinstance(row, dict) or row.get("app_id"):
            continue
        ids = app_ids_by_label.get(str(row.get("name") or "").strip()) or set()
        if len(ids) == 1:
            row["app_id"] = next(iter(ids))


# --- backend probes -------------------------------------------------------

_ROLE_ORDER = {"required": 0, "optional": 1, "standby": 2}
_VALID_ROLES = {"required", "optional", "standby"}


async def _probe(
    client: httpx.AsyncClient,
    key: str,
    name: str,
    url: str,
    role: str,
    note: Optional[str],
    service: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    base = {
        "key": key,
        "service_id": key,
        "name": name,
        "url": url,
        "role": role,
        "note": note,
    }
    t0 = time.monotonic()
    try:
        r = await client.get(url)
        dt = int((time.monotonic() - t0) * 1000)
        if 200 <= r.status_code < 300:
            payload = None
            try:
                payload = r.json()
            except ValueError:
                payload = None
            if isinstance(payload, Mapping):
                parsed_status = _status_from_health_payload(payload)
                available = _availability_from_health_payload(payload)
                if parsed_status == "ready" or available is True or (parsed_status == "unknown" and declared_health_success(service or {}, payload)):
                    return {**base, "status": "up", "live_status": "up", "latency_ms": dt, "last_error": None}
                if parsed_status in {"starting", "busy"}:
                    return {**base, "status": parsed_status, "live_status": parsed_status, "latency_ms": dt, "last_error": None}
                if parsed_status == "unavailable" or available is False:
                    return {**base, "status": "down", "live_status": "down", "latency_ms": dt, "last_error": "readiness false"}
                return {**base, "status": "unknown", "live_status": "unknown", "latency_ms": dt, "last_error": None}
            return {**base, "status": "unknown", "live_status": "unknown", "latency_ms": dt, "last_error": None}
        if r.status_code in (404, 405):
            return {
                **base,
                "status": "unknown",
                "live_status": "unknown",
                "latency_ms": dt,
                "last_error": f"HTTP {r.status_code}",
                "kind": "no_response",
            }
        if r.status_code in (502, 503, 504):
            return {
                **base,
                "status": "degraded",
                "live_status": "degraded",
                "latency_ms": dt,
                "last_error": f"HTTP {r.status_code}",
            }
        return {
            **base,
            "status": "down",
            "live_status": "down",
            "latency_ms": dt,
            "last_error": f"HTTP {r.status_code}",
        }
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return {**base, "status": "down", "live_status": "down", "latency_ms": None, "last_error": "connect refused", "kind": "no_response"}
    except httpx.TimeoutException:
        return {**base, "status": "degraded", "live_status": "degraded", "latency_ms": None, "last_error": "timeout", "kind": "no_response"}
    except Exception as e:  # noqa: BLE001
        return {**base, "status": "down", "live_status": "down", "latency_ms": None, "last_error": _safe_err(e), "kind": "no_response"}


def _resolve_backend_targets(config: dict) -> list[dict[str, Any]]:
    backends = config.get("backends", {}) or {}
    targets: list[dict[str, Any]] = []
    for key, cfg in backends.items():
        if not isinstance(cfg, dict):
            continue
        url = service_health_endpoint(cfg)
        if not url:
            continue
        name = cfg.get("name") or key
        role = cfg.get("role", "required")
        if role not in _VALID_ROLES:
            role = "required"
        note = cfg.get("note")
        targets.append({"key": key, "name": name, "url": url, "role": role, "note": note})
    targets.sort(key=lambda t: _ROLE_ORDER.get(t["role"], 99))
    return targets


def _lifecycle_backend_targets(config: dict) -> list[dict[str, Any]]:
    backends = config.get("backends", {}) or {}
    targets: list[dict[str, Any]] = []
    for key, cfg in backends.items():
        if not isinstance(cfg, dict):
            continue
        lifecycle = cfg.get("lifecycle")
        if not isinstance(lifecycle, Mapping):
            continue
        driver_name = str(lifecycle.get("driver") or "external").strip().lower()
        if not driver_name or driver_name in {"external", "http-load-unload"}:
            continue
        name = cfg.get("name") or key
        role = cfg.get("role", "required")
        if role not in _VALID_ROLES:
            role = "required"
        targets.append({"key": key, "name": name, "role": role, "note": cfg.get("note"), "service": cfg})
    targets.sort(key=lambda t: _ROLE_ORDER.get(t["role"], 99))
    return targets


def _row_for_lifecycle_state(target: Mapping[str, Any], state: str, warning: str | None = None) -> dict[str, Any]:
    status = str(state or "unknown").strip().lower()
    if warning:
        live_status = "unknown"
        error = warning
    elif status == lifecycle_drivers.ProcessState.STARTING:
        live_status = "starting"
        error = None
    elif status == lifecycle_drivers.ProcessState.DRAINING:
        live_status = "busy"
        error = None
    elif status == lifecycle_drivers.ProcessState.STOPPED:
        live_status = "stopped"
        error = None
    elif status == lifecycle_drivers.ProcessState.FAILED:
        live_status = "down"
        error = "lifecycle state failed"
    else:
        live_status = "unknown"
        error = None
    return {
        "key": target["key"],
        "service_id": target["key"],
        "name": target["name"],
        "url": None,
        "role": target["role"],
        "note": target.get("note"),
        "status": live_status,
        "live_status": live_status,
        "latency_ms": None,
        "last_error": error,
        "source": "lifecycle",
        "kind": "process",
    }


async def _lifecycle_probe(target: Mapping[str, Any], config_backends: Mapping[str, Any]) -> dict[str, Any]:
    driver = lifecycle_drivers.driver_for_service(str(target["key"]), config_backends=config_backends)
    service = target["service"]
    warning: str | None = None
    try:
        warning = await asyncio.wait_for(driver.preflight(service), timeout=1.5)
    except Exception as exc:  # noqa: BLE001
        warning = _safe_err(exc)
    try:
        state = await asyncio.wait_for(driver.status(service), timeout=1.5)
    except Exception as exc:  # noqa: BLE001
        return _row_for_lifecycle_state(target, "unknown", _safe_err(exc))
    return _row_for_lifecycle_state(target, state, warning)


def _merge_backend_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("service_id") or row.get("key") or "")
        if not key:
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = row
            continue
        incoming_status = str(row.get("live_status") or row.get("status") or "")
        if row.get("source") == "lifecycle" and incoming_status in {"stopped", "starting", "busy"}:
            merged[key] = row
        elif current.get("source") == "lifecycle" and str(current.get("live_status") or current.get("status") or "") in {"stopped", "starting", "busy"}:
            continue
        elif row.get("source") == "lifecycle" and incoming_status == "unknown":
            continue
        else:
            merged[key] = row
    return sorted(merged.values(), key=lambda row: _ROLE_ORDER.get(str(row.get("role") or "required"), 99))


async def _backend_grid(config: dict) -> list[dict]:
    targets = _resolve_backend_targets(config)
    lifecycle_targets = _lifecycle_backend_targets(config)
    backends = config.get("backends", {}) or {}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        async def configured_probe(target):
            service = backends.get(target["key"], {})
            row = await _probe(client, target["key"], target["name"], target["url"], target["role"], target["note"], service=service)
            row["observed_at_s"] = int(time.time())
            return row
        http_results, lifecycle_results = await asyncio.gather(
            asyncio.gather(
                *[
                    configured_probe(t)
                    for t in targets
                ],
                return_exceptions=False,
            ),
            asyncio.gather(
                *[_lifecycle_probe(t, backends) for t in lifecycle_targets],
                return_exceptions=False,
            ),
        )
    return _merge_backend_observations([*http_results, *lifecycle_results])


# --- llama.cpp /props + /slots -------------------------------------------

async def _llama_props_and_slots() -> tuple[Optional[dict], Optional[list[dict]]]:
    """Fetch non-invasive llama-server metadata.

    ``/slots`` is deliberately not queried here. llama-server implements that
    endpoint by posting a high-priority task to the inference queue. If a
    monitoring client times out while generation is busy, disconnect handling
    posts cancellation work back into the same queue. Repeated polling can
    therefore perturb or wedge the workload it is meant to observe.

    Slot activity must come from gateway-owned request telemetry, never by
    probing the engine's inference queue. Keep the tuple return shape for the
    existing status renderer; slot data is unavailable rather than guessed.
    """
    async def _get(client, url):
        try:
            r = await client.get(url)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            props = await _get(client, "http://127.0.0.1:5051/props")
            return props, None
    except Exception:
        return None, None


def _llama_server_args() -> Optional[list[str]]:
    rc, out, _ = _run(["pgrep", "-fa", "llama-server"])
    if rc != 0 or not out.strip():
        return None
    for line in out.splitlines():
        if "llama-server" in line and "--model" in line:
            return line.strip().split()
    return None


def _extract_arg(tokens: list[str], flag: str) -> Optional[str]:
    out: list[str] = []
    found = False
    for i, t in enumerate(tokens):
        if t == flag:
            for nxt in tokens[i + 1:]:
                if nxt.startswith("--"):
                    break
                out.append(nxt)
            found = True
            break
    if not found:
        return None
    return " ".join(out) if out else None


def _annotate_tensor_split(raw: Optional[str]) -> Optional[dict]:
    """Compute human-friendly explanation for ``--tensor-split`` value.

    llama.cpp interprets a comma-separated list as a per-GPU ratio: each value
    is a relative weight, llama-server normalizes them to fractions and uses
    them to split layers across devices. So ``52,12`` means "GPU0 gets
    52/(52+12) ≈ 81% of layers, GPU1 gets 12/64 ≈ 19%".

    Returns ``{"raw": "52,12", "weights": [52,12], "fractions": [0.8125,
    0.1875], "summary": "≈81% GPU0 / 19% GPU1", "interpretation":
    "ratio (llama.cpp default)"}`` or ``None`` if the value can't be parsed.
    """
    if not raw:
        return None
    try:
        parts = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
        weights = [float(p) for p in parts]
    except (ValueError, AttributeError):
        return None
    if not weights:
        return None
    total = sum(weights)
    if total <= 0:
        return None
    fractions = [w / total for w in weights]
    summary = " / ".join(
        f"≈{round(f * 100):d}% GPU{i}" for i, f in enumerate(fractions)
    )
    return {
        "raw": raw,
        "weights": weights,
        "fractions": [round(f, 4) for f in fractions],
        "summary": summary,
        "interpretation": "ratio (llama.cpp default; values are relative weights)",
    }


def _resolve_parallel_arg(
    cmdline_arg: Optional[str], props_total_slots: Optional[int]
) -> dict:
    """Resolve the effective ``--parallel`` value.

    llama.cpp defaults to ``--parallel 4`` when the flag is omitted and uses
    that to size the slot pool. ``/props.total_slots`` is the authoritative
    runtime value. Returns a dict shape so the renderer can display both the
    effective number and where it came from.
    """
    if cmdline_arg:
        try:
            return {
                "value": int(cmdline_arg.strip().split()[0]),
                "source": "cmdline",
            }
        except (ValueError, IndexError):
            return {"value": None, "source": "cmdline-unparsable", "raw": cmdline_arg}
    if isinstance(props_total_slots, int):
        return {
            "value": props_total_slots,
            "source": "props.total_slots (default; --parallel not set on cmdline)",
        }
    return {"value": None, "source": "unknown"}


def _resolve_tts_engine(
    cmdline_args: Optional[list[str]], voxtral_health: Optional[dict]
) -> dict:
    """Return labeled service-unit / engine framework / model triple for TTS.

    The previous status payload conflated these three things ("Active TTS
    engine: voxtral-int4-tts" was actually the *systemd unit name*, while the
    Voxtral health endpoint reports the *model* name). Surface each
    independently so the dashboard can render three rows.

    - ``unit`` — systemd unit (without ``.service`` suffix), if any is active.
    - ``engine`` — the framework actually loading the model, derived from the
      unit's ExecStart (``vllm``, ``serve.py`` int4 server, etc.).
    - ``model`` — the model name reported by the backend's ``/health``.
    """
    unit: Optional[str] = None
    engine: Optional[str] = None
    exec_start: Optional[str] = None
    candidates = ("voxtral-int4-tts.service", "voxtral-tts.service", "tts.service")
    for u in candidates:
        rc, out, _ = _run(["systemctl", "--user", "is-active", u], timeout=1.0)
        if rc == 0 and out.strip() == "active":
            unit = u.removesuffix(".service")
            # Inspect ExecStart so we can name the framework honestly.
            rc2, out2, _ = _run(
                ["systemctl", "--user", "show", u, "-p", "ExecStart", "--value"],
                timeout=1.0,
            )
            if rc2 == 0 and out2:
                raw = out2.strip().split("\n", 1)[0]
                # systemd-show emits a structured one-liner like
                #   { path=... ; argv[]=cmd arg1 arg2 ; ... }
                # We just want the human-readable argv portion.
                m = re.search(r"argv\[\]=([^;]+)", raw)
                exec_start = m.group(1).strip() if m else raw
            break
    if exec_start:
        es_lower = exec_start.lower()
        if "vllm" in es_lower:
            engine = "vLLM"
        elif "voxtral-int4" in es_lower or "/voxtral-int4/" in exec_start:
            engine = "voxtral-int4 (custom serve.py, flow-matching)"
        elif "voxtral" in es_lower:
            engine = "Voxtral (vLLM-Omni)"
        elif "tts" in es_lower:
            engine = "tts (custom)"
    model: Optional[str] = None
    if isinstance(voxtral_health, dict):
        model = voxtral_health.get("model")
    return {
        "unit": unit,
        "engine": engine,
        "model": model,
        "exec_start": exec_start,
    }


async def _models_section(config: dict, props: Optional[dict], slots: Optional[list[dict]]) -> dict:
    args = _llama_server_args()
    loaded_path = _extract_arg(args, "--model") if args else None
    mmproj = _extract_arg(args, "--mmproj") if args else None
    ctx_size_str = _extract_arg(args, "--ctx-size") if args else None
    cache_k_arg = _extract_arg(args, "--cache-type-k") if args else None
    cache_v_arg = _extract_arg(args, "--cache-type-v") if args else None
    tensor_split = _extract_arg(args, "--tensor-split") if args else None
    parallel_arg = _extract_arg(args, "--parallel") if args else None

    try:
        ctx_size = int(ctx_size_str) if ctx_size_str else None
    except ValueError:
        ctx_size = None

    # Prefer /props over cmdline parsing.
    props_total_slots: Optional[int] = None
    props_n_ctx: Optional[int] = None
    props_model_alias: Optional[str] = None
    props_model_path: Optional[str] = None
    vision_modality: Optional[bool] = None
    speculative_type: Optional[str] = None

    if isinstance(props, dict):
        props_total_slots = props.get("total_slots")
        props_model_alias = props.get("model_alias")
        props_model_path = props.get("model_path")
        modalities = props.get("modalities") or {}
        if isinstance(modalities, dict):
            vision_modality = modalities.get("vision")
        gs = props.get("default_generation_settings") or {}
        if isinstance(gs, dict):
            props_n_ctx = gs.get("n_ctx")
            params = gs.get("params") or {}
            if isinstance(params, dict):
                speculative_type = params.get("speculative.type")

    # Per-slot detail.
    slot_details: list[dict] = []
    active_slots = 0
    ctx_used: Optional[int] = None
    if isinstance(slots, list):
        used_vals: list[int] = []
        for s in slots:
            if not isinstance(s, dict):
                continue
            is_processing = bool(s.get("is_processing"))
            n_past = int(s.get("n_past", 0)) if isinstance(s.get("n_past"), (int, float)) else 0
            n_decoded = int(s.get("n_decoded", 0)) if isinstance(s.get("n_decoded"), (int, float)) else 0
            slot_details.append({
                "id": s.get("id"),
                "is_processing": is_processing,
                "n_ctx": s.get("n_ctx"),
                "n_past": n_past,
                "n_decoded": n_decoded,
                "speculative": s.get("speculative"),
                "id_task": s.get("id_task"),
                "prompt_processing_progress": s.get("prompt_processing_progress"),
                "state": "processing" if is_processing else "idle",
            })
            if is_processing:
                active_slots += 1
            used_vals.append(n_past)
        if used_vals:
            ctx_used = max(used_vals)

    videos_cfg = config.get("videos", {})
    aliases = sorted((videos_cfg.get("aliases") or {}).keys())

    # Image workflows: list ComfyUI api_workflows dir.
    image_workflows: list[str] = []
    try:
        comfy_cfg = config.get("backends", {}).get("comfyui", {})
        wf_dir = comfy_cfg.get("workflows_dir")
        if wf_dir:
            p = Path(wf_dir)
            if p.is_dir():
                stems = []
                for f in p.glob("*.json"):
                    name = f.name
                    if name.endswith(".ui.json"):
                        continue
                    if ".obsolete" in name or ".bak" in name:
                        continue
                    stems.append(f.stem)
                image_workflows = sorted(set(stems))[:50]
    except Exception:
        pass

    # Active TTS engine: read systemd ActiveState of voxtral-int4-tts.service.
    tts_engine: Optional[str] = None
    rc, out, _ = _run(["systemctl", "--user", "is-active", "voxtral-int4-tts.service"])
    if rc == 0 and out.strip() == "active":
        tts_engine = "voxtral-int4-tts"
    else:
        for unit in ("voxtral-tts.service", "tts.service"):
            rc2, out2, _ = _run(["systemctl", "--user", unit])
            if rc2 == 0 and out2.strip() == "active":
                tts_engine = unit.removesuffix(".service")
                break

    loaded_name = props_model_alias or (Path(loaded_path).name if loaded_path else None)

    # Honest vision-capable: prefer /props.modalities.vision.
    if vision_modality is not None:
        vision_capable = bool(vision_modality)
    else:
        vision_capable = bool(mmproj)

    return {
        "loaded_llm_path": props_model_path or loaded_path,
        "loaded_llm_name": loaded_name,
        "mmproj_path": mmproj,
        "vision_capable": vision_capable,
        "vision_source": "props" if vision_modality is not None else ("cmdline" if mmproj else "unknown"),
        "ctx_size": props_n_ctx or ctx_size,
        "ctx_used": ctx_used,
        "active_tts_engine": tts_engine,
        "image_workflows": image_workflows,
        "video_aliases": aliases,
        "total_slots": props_total_slots if props_total_slots is not None else len(slot_details),
        "active_slots": active_slots,
        "slots": slot_details,
        "speculative_type": speculative_type,
        "cache_type_k": cache_k_arg,
        "cache_type_v": cache_v_arg,
        "tensor_split": tensor_split,
        "parallel_arg": parallel_arg,
    }


# --- GPU / queue ----------------------------------------------------------

def _gpus_raw() -> Optional[list[dict]]:
    """nvidia-smi snapshot incl. temp, power, clocks, pcie, throttle.

    memory_used_mb comes from per-GPU `memory.used` (authoritative total —
    includes kernel/driver overhead and display surfaces, not just compute
    apps), so attribution can compute unattributed = total − sum(apps).
    """
    rc, out, _err = _run([
        "nvidia-smi",
        "--query-gpu="
        "index,uuid,name,memory.used,memory.total,utilization.gpu,"
        "temperature.gpu,power.draw,power.limit,power.default_limit,power.max_limit,"
        "clocks.gr,clocks.mem,fan.speed,clocks_throttle_reasons.active,"
        "pcie.link.gen.current,pcie.link.gen.max,"
        "pcie.link.width.current,pcie.link.width.max",
        "--format=csv,noheader,nounits",
    ])
    if rc != 0 or not out.strip():
        return None
    gpus: list[dict] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 19:
            continue
        try:
            gpus.append({
                "index": int(parts[0]),
                "uuid": parts[1],
                "name": parts[2],
                "memory_used_mb": int(parts[3]),
                "memory_total_mb": int(parts[4]),
                "instantaneous_util_pct": _safe_int(parts[5]),
                "temp_c": _safe_int(parts[6]),
                "power_draw_w": _safe_float(parts[7]),
                "power_limit_w": _safe_float(parts[8]),
                "power_default_limit_w": _safe_float(parts[9]),
                "power_max_limit_w": _safe_float(parts[10]),
                "clocks_gr_mhz": _safe_int(parts[11]),
                "clocks_mem_mhz": _safe_int(parts[12]),
                "fan_speed_pct": _safe_int(parts[13]),
                "throttle_reasons_hex": parts[14],
                "pcie_link_gen": _safe_int(parts[15]),
                "pcie_link_gen_max": _safe_int(parts[16]),
                "pcie_link_width": _safe_int(parts[17]),
                "pcie_link_width_max": _safe_int(parts[18]),
            })
        except ValueError:
            continue
    return gpus or None


def _safe_int(s: str) -> Optional[int]:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _safe_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


async def _comfy_queue_and_stats() -> tuple[Optional[dict], Optional[dict]]:
    async def _get(client, url):
        try:
            r = await client.get(url)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            queue, stats = await asyncio.gather(
                _get(client, "http://127.0.0.1:7890/queue"),
                _get(client, "http://127.0.0.1:7890/system_stats"),
            )
        if isinstance(queue, dict):
            running = queue.get("queue_running") or []
            pending = queue.get("queue_pending") or []
            running_ids = []
            for q in running:
                if isinstance(q, list) and len(q) >= 2:
                    running_ids.append(q[1])
            pending_ids = []
            for q in pending:
                if isinstance(q, list) and len(q) >= 2:
                    pending_ids.append(q[1])
            queue_summary = {
                "running": len(running),
                "pending": len(pending),
                "running_ids": running_ids,
                "pending_ids": pending_ids,
            }
        else:
            queue_summary = None
        return queue_summary, stats if isinstance(stats, dict) else None
    except Exception:
        return None, None


async def _gpu_workloads(
    gpus: Optional[list[dict]],
    comfy_queue: Optional[dict],
    slots: Optional[list[dict]],
) -> Optional[list[dict]]:
    if gpus is None:
        return None
    out: list[dict] = []
    for g in gpus:
        idx = g.get("index")
        workload: dict[str, Any] = {"label": "idle", "detail": None}
        if idx == 0:
            if comfy_queue is None:
                workload = {"label": "unknown", "detail": "comfyui queue unavailable"}
            else:
                running = int(comfy_queue.get("running", 0) or 0)
                pending = int(comfy_queue.get("pending", 0) or 0)
                if running == 0 and pending == 0:
                    workload = {
                        "label": "idle",
                        "detail": "ComfyUI: 0 running, 0 pending",
                        "running": 0,
                        "pending": 0,
                    }
                else:
                    workload = {
                        "label": "running",
                        "detail": f"ComfyUI: {running} running, {pending} pending",
                        "running": running,
                        "pending": pending,
                    }
        elif idx == 1:
            if slots is not None:
                active = [s for s in slots if isinstance(s, dict) and s.get("is_processing")]
                total = len(slots)
                workload = {
                    "label": "processing" if active else "loaded",
                    "detail": f"LLM: {len(active)} active / {total} slots",
                    "active_slots": len(active),
                    "total_slots": total,
                }
            else:
                used_mb = int(g.get("memory_used_mb") or 0)
                if used_mb > 5 * 1024:
                    workload = {"label": "loaded", "detail": "LLM: model resident (slots endpoint unavailable)"}
                else:
                    workload = {"label": "idle", "detail": "LLM: no model resident"}
        out.append({**g, "active_workload": workload})
    return out


# --- per-service VRAM attribution ----------------------------------------

def _get_compute_apps() -> list[dict]:
    """nvidia-smi compute apps: (pid, gpu_uuid, used_memory_mb)."""
    rc, out, _ = _run([
        "nvidia-smi",
        "--query-compute-apps=pid,gpu_uuid,used_memory,process_name",
        "--format=csv,noheader,nounits",
    ])
    if rc != 0 or not out.strip():
        return []
    rows: list[dict] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            rows.append({
                "pid": int(parts[0]),
                "gpu_uuid": parts[1],
                "used_mb": int(parts[2]),
                "process_name": parts[3] if len(parts) > 3 else "",
            })
        except ValueError:
            continue
    return rows


def _service_pids() -> dict[str, set[int]]:
    """Map service name -> set of PIDs running under its cgroup."""
    out: dict[str, set[int]] = {}
    for unit in _SERVICE_UNITS:
        rc, ctrl, _ = _run(["systemctl", "--user", "show", "-p", "ControlGroup", f"{unit}.service"])
        if rc != 0 or not ctrl.strip():
            continue
        cg = ctrl.strip().split("=", 1)[1] if "=" in ctrl else ""
        if not cg:
            continue
        procs_path = Path(f"/sys/fs/cgroup{cg}/cgroup.procs")
        if not procs_path.is_file():
            continue
        try:
            pids = {int(x) for x in procs_path.read_text().split() if x.strip().isdigit()}
            if pids:
                out[unit] = pids
        except Exception:
            continue
    return out


def _read_proc_comm(pid: int) -> Optional[str]:
    """Read /proc/<pid>/comm — short, stable kernel-level process name. Best-effort."""
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip() or None
    except Exception:
        return None


_REPO_PATH = os.environ.get("AZIZAM_REPO_PATH", str(Path(__file__).parent))
_COMFY_CUSTOM_NODES = _optional_path(
    "AZIZAM_COMFY_CUSTOM_NODES_DIR",
    os.environ.get("AZIZAM_COMFY_DIR", "") and os.path.join(os.environ["AZIZAM_COMFY_DIR"], "custom_nodes") or "",
)


def _ram_attribution() -> dict:
    """RAM analogue of _vram_attribution: per-service RSS plus a desktop bucket.

    No RAM budgets configured yet, so the budget column stays null and this is
    purely informational. Walks the same systemd cgroup PID sets as VRAM, sums
    RSS from /proc/<pid>/statm pages, and bins everything else as "desktop".
    Returns total_kb, free_kb, available_kb, and per-service rows. Degrades
    gracefully: unreadable /proc entries are skipped silently.
    """
    page_kb = 4
    try:
        import os as _os
        page_kb = int(_os.sysconf("SC_PAGESIZE")) // 1024
    except Exception:
        pass
    total_kb = free_kb = available_kb = None
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for raw in f:
                if raw.startswith("MemTotal:"):
                    total_kb = int(raw.split()[1])
                elif raw.startswith("MemFree:"):
                    free_kb = int(raw.split()[1])
                elif raw.startswith("MemAvailable:"):
                    available_kb = int(raw.split()[1])
    except Exception:
        pass
    svc_pids = _service_pids()
    pid_to_service: dict[int, str] = {}
    for svc, pids in svc_pids.items():
        for pid in pids:
            pid_to_service[pid] = svc
    svc_rss_kb: dict[str, int] = {svc: 0 for svc in _SERVICE_UNITS}
    desktop_kb = 0
    try:
        for entry in Path("/proc").iterdir():
            n = entry.name
            if not n.isdigit():
                continue
            pid = int(n)
            try:
                parts = (entry / "statm").read_text().split()
                rss_kb = int(parts[1]) * page_kb
            except Exception:
                continue
            svc = pid_to_service.get(pid)
            if svc:
                svc_rss_kb[svc] = svc_rss_kb.get(svc, 0) + rss_kb
            else:
                desktop_kb += rss_kb
    except Exception as e:  # noqa: BLE001
        return {"total_kb": total_kb, "free_kb": free_kb,
                "available_kb": available_kb, "rows": [],
                "error": _safe_err(e)}
    rows: list[dict] = []
    for svc, kb in sorted(svc_rss_kb.items(), key=lambda kv: -kv[1]):
        if kb <= 0:
            continue
        rows.append({"service": svc, "rss_kb": kb, "budget_kb": None})
    rows.append({"service": "desktop", "rss_kb": desktop_kb, "budget_kb": None})
    return {"total_kb": total_kb, "free_kb": free_kb,
            "available_kb": available_kb, "rows": rows}


def _repo_state() -> dict:
    """Snapshot the azizam git repo: branch, dirty/clean, ahead/behind
    origin/main, latest commit subject + age, and up to 5 modified files.
    All git invocations have a 2s timeout and the function never raises.
    """
    out: dict[str, Any] = {}

    def _git(*args: str) -> tuple[int, str, str]:
        return _run(["git", "-C", _REPO_PATH, *args], timeout=2.0)

    rc, branch, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    out["branch"] = branch.strip() if rc == 0 else None
    rc, porcelain, _ = _git("status", "--porcelain")
    if rc == 0:
        lines = [l for l in porcelain.splitlines() if l.strip()]
        out["dirty"] = bool(lines)
        out["modified_files"] = [l[3:] if len(l) > 3 else l for l in lines[:5]]
        out["modified_count"] = len(lines)
    else:
        out["dirty"] = None
        out["modified_files"] = []
        out["modified_count"] = 0
    rc, counts, _ = _git("rev-list", "--left-right", "--count",
                         "HEAD...origin/main")
    if rc == 0 and counts.strip():
        try:
            ahead, behind = counts.strip().split()
            out["ahead"] = int(ahead)
            out["behind"] = int(behind)
        except Exception:
            out["ahead"] = out["behind"] = None
    else:
        out["ahead"] = out["behind"] = None
    rc, log_line, _ = _git("log", "-1", "--format=%h\t%s\t%ar\t%at")
    if rc == 0 and log_line.strip():
        try:
            sha, subj, ago, at = log_line.strip().split("\t", 3)
            out["latest_commit"] = {"sha": sha, "subject": subj,
                                    "ago": ago, "ts": int(at)}
        except Exception:
            out["latest_commit"] = None
    else:
        out["latest_commit"] = None
    return out


def _comfy_custom_nodes() -> list[dict]:
    """List immediate child dirs of ComfyUI/custom_nodes with the latest
    activity timestamp we can reach (.git/HEAD mtime if it's a git checkout,
    falling back to the dir's own mtime). Cheap: just stats — no git fork.
    """
    if not _COMFY_CUSTOM_NODES or not _COMFY_CUSTOM_NODES.is_dir():
        return []
    out: list[dict] = []
    for child in _COMFY_CUSTOM_NODES.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        head = child / ".git" / "HEAD"
        if head.exists():
            try:
                mtime = int(head.stat().st_mtime)
            except Exception:
                mtime = None
        else:
            try:
                mtime = int(child.stat().st_mtime)
            except Exception:
                mtime = None
        source = ""
        cfg = child / ".git" / "config"
        if cfg.is_file():
            try:
                txt = cfg.read_text(encoding="utf-8", errors="replace")
                for line in txt.splitlines():
                    line = line.strip()
                    if line.startswith("url ="):
                        source = line.split("=", 1)[1].strip()
                        break
            except Exception:
                pass
        out.append({"name": child.name, "mtime": mtime, "source": source})
    out.sort(key=lambda n: -(n.get("mtime") or 0))
    return out


def _vram_attribution(gpus: Optional[list[dict]]) -> dict:
    """Per-GPU per-service VRAM usage with budget comparison.

    Synthesized rows (prefixed with `__`) are appended after real services so
    every visible MiB is accounted for:
      - __unattributed: total_used_mb − sum(compute-apps on this GPU). Captures
        kernel/driver overhead and display surfaces nvidia-smi doesn't itemize
        (e.g. KMS framebuffers, EGLStreams). No budget comparison.
      - __headroom:     total_mb − total_used_mb (free VRAM). Compared against
        the per-GPU headroom budget when configured (under-budget = bad).
      - __total:        total_used_mb (table footer).

    Tensor-split spillover: when a single PID maps to multiple GPUs (e.g.
    llama-server with --tensor-split 52,12), the row on the smaller share is
    flagged so the budget question stays meaningful — the PID's full VRAM
    is governed by its primary GPU's budget, not split per-card.

    Desktop bucket: any compute-app whose PID isn't owned by a tracked
    service systemd cgroup is bucketed under "desktop" (Plasma, Xorg,
    Chromium, Sunshine, kwin, etc.). Top contributing PIDs are returned as
    `top_contributors` for tooltip/sub-table rendering.
    """
    if not gpus:
        return {"per_gpu": [], "errors": ["gpus unavailable"]}
    apps = _get_compute_apps()
    pid_to_service: dict[int, str] = {}
    svc_pids = _service_pids()
    for svc, pids in svc_pids.items():
        for pid in pids:
            pid_to_service[pid] = svc

    # Build uuid -> index map.
    uuid_to_index = {g.get("uuid"): g.get("index") for g in gpus}

    # Pre-pass: per-PID per-GPU usage (detect multi-GPU spread).
    pid_gpu_usage: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for app in apps:
        idx = uuid_to_index.get(app["gpu_uuid"])
        if idx is None:
            continue
        pid_gpu_usage[app["pid"]][idx] += app["used_mb"]

    # PID's primary GPU = whichever has the most VRAM for that PID.
    pid_primary_gpu: dict[int, int] = {}
    for pid, gpu_map in pid_gpu_usage.items():
        if len(gpu_map) >= 2:
            pid_primary_gpu[pid] = max(gpu_map.items(), key=lambda kv: kv[1])[0]

    per_gpu_svc: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    spillover_flags: dict[int, dict[str, bool]] = defaultdict(dict)
    spillover_primary: dict[int, dict[str, int]] = defaultdict(dict)
    desktop_top: dict[int, list[dict]] = defaultdict(list)

    for app in apps:
        idx = uuid_to_index.get(app["gpu_uuid"])
        if idx is None:
            continue
        pid = app["pid"]
        svc = pid_to_service.get(pid, "desktop")
        per_gpu_svc[idx][svc] += app["used_mb"]

        if (
            svc != "desktop"
            and pid in pid_primary_gpu
            and pid_primary_gpu[pid] != idx
        ):
            spillover_flags[idx][svc] = True
            spillover_primary[idx][svc] = pid_primary_gpu[pid]

        if svc == "desktop":
            desktop_top[idx].append({
                "pid": pid,
                "used_mb": app["used_mb"],
                "comm": _read_proc_comm(pid) or app.get("process_name") or "?",
            })

    # Pull budget for comparison.
    budget = _read_gpu_budget()

    def budget_for(idx: int, svc: str) -> Optional[int]:
        if not budget:
            return None
        raw = budget.get("raw") or {}
        if idx == 0:
            mapping = {
                "comfyui": raw.get("GPU0_COMFYUI"),
                "voxtral-int4-tts": raw.get("GPU0_TTS"),
                "voxtral-tts": raw.get("GPU0_TTS"),
                "desktop": raw.get("GPU0_DESKTOP"),
                "__headroom": raw.get("GPU0_HEADROOM"),
            }
            return mapping.get(svc)
        if idx == 1:
            mapping = {
                "llama-turbo": raw.get("GPU1_LLM"),
                "__headroom": raw.get("GPU1_HEADROOM"),
            }
            return mapping.get(svc)
        return None

    out_gpus: list[dict] = []
    for g in gpus:
        idx = g.get("index")
        total_used_mb = int(g.get("memory_used_mb") or 0)
        total_mb = int(g.get("memory_total_mb") or 0)
        svc_map = per_gpu_svc.get(idx, {})
        services: list[dict] = []

        accounted_mb = 0
        for svc, mb in sorted(svc_map.items(), key=lambda x: -x[1]):
            is_spillover = spillover_flags.get(idx, {}).get(svc, False)
            # Spillover rows aren't compared against the local-GPU budget —
            # the PID's allocation is governed by its primary GPU's budget.
            budget_gb = None if is_spillover else budget_for(idx, svc)
            used_gb = mb / 1024.0
            over_budget = (
                budget_gb is not None and used_gb > float(budget_gb) + 0.5
            )
            pct_of_budget = (
                (used_gb / float(budget_gb) * 100.0)
                if (budget_gb and budget_gb > 0) else None
            )
            row: dict[str, Any] = {
                "service": svc,
                "used_mb": mb,
                "used_gb": round(used_gb, 2),
                "budget_gb": budget_gb,
                "pct_of_budget": round(pct_of_budget, 1) if pct_of_budget is not None else None,
                "over_budget": bool(over_budget),
                "spillover": is_spillover,
            }
            if is_spillover:
                row["spillover_from_gpu"] = spillover_primary.get(idx, {}).get(svc)
                row["note"] = "tensor-split spillover"
            if svc == "desktop":
                tops = sorted(desktop_top.get(idx, []), key=lambda x: -x["used_mb"])[:5]
                row["top_contributors"] = tops
            services.append(row)
            accounted_mb += mb

        # Synthesized rows.
        unattributed_mb = max(0, total_used_mb - accounted_mb)
        services.append({
            "service": "__unattributed",
            "used_mb": unattributed_mb,
            "used_gb": round(unattributed_mb / 1024.0, 2),
            "budget_gb": None,
            "pct_of_budget": None,
            "over_budget": False,
            "spillover": False,
            "note": "kernel/driver/display surfaces (not in compute-apps)",
        })

        headroom_mb = max(0, total_mb - total_used_mb)
        headroom_budget_gb = budget_for(idx, "__headroom")
        headroom_pct = (
            ((headroom_mb / 1024.0) / float(headroom_budget_gb) * 100.0)
            if (headroom_budget_gb and headroom_budget_gb > 0) else None
        )
        # Under-budget headroom = bad (less free than planned).
        headroom_short = (
            headroom_budget_gb is not None
            and (headroom_mb / 1024.0) < float(headroom_budget_gb) - 0.5
        )
        services.append({
            "service": "__headroom",
            "used_mb": headroom_mb,
            "used_gb": round(headroom_mb / 1024.0, 2),
            "budget_gb": headroom_budget_gb,
            "pct_of_budget": round(headroom_pct, 1) if headroom_pct is not None else None,
            "over_budget": False,
            "headroom_short": bool(headroom_short),
            "spillover": False,
            "note": "free VRAM (total − used)",
        })

        total_budget_gb = None
        if budget:
            scope = budget.get("gpu0" if idx == 0 else "gpu1") or {}
            total_budget_gb = scope.get("total_gb")
        services.append({
            "service": "__total",
            "used_mb": total_used_mb,
            "used_gb": round(total_used_mb / 1024.0, 2),
            "budget_gb": total_budget_gb,
            "pct_of_budget": None,
            "over_budget": False,
            "spillover": False,
            "note": "GPU memory.used (authoritative)",
        })

        out_gpus.append({
            "index": idx,
            "uuid": g.get("uuid"),
            "memory_used_mb": total_used_mb,
            "memory_total_mb": total_mb,
            "services": services,
        })
    return {
        "per_gpu": out_gpus,
        "raw_compute_apps": apps,
        "service_pids": {k: sorted(v) for k, v in svc_pids.items()},
        "multi_gpu_pids": {
            str(pid): {str(idx): mb for idx, mb in gmap.items()}
            for pid, gmap in pid_gpu_usage.items()
            if len(gmap) >= 2
        },
    }


# --- budgets --------------------------------------------------------------

def _read_gpu_budget() -> Optional[dict]:
    now = time.monotonic()
    cached = _BUDGET_CACHE.get("data")
    if cached is not None and (now - _BUDGET_CACHE.get("ts", 0.0)) < _BUDGET_CACHE_TTL_S:
        return cached
    try:
        if _GPU_BUDGET_PATH is None:
            raise FileNotFoundError("AZIZAM_GPU_BUDGET_PATH not configured")
        text = _GPU_BUDGET_PATH.read_text(encoding="utf-8")
    except Exception:
        _BUDGET_CACHE["data"] = None
        _BUDGET_CACHE["ts"] = now
        return None

    keys_of_interest = {
        "GPU_TOTAL_GB", "GPU0_DESKTOP", "GPU0_TTS", "GPU0_COMFYUI", "GPU0_HEADROOM",
        "GPU1_LLM", "GPU1_HEADROOM",
    }
    found: dict[str, int] = {}
    pattern = re.compile(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(\d+)")
    for line in text.splitlines():
        s = line.lstrip()
        if s.startswith("#"):
            continue
        m = pattern.match(line)
        if not m:
            continue
        key = m.group(1)
        if key not in keys_of_interest:
            continue
        try:
            found[key] = int(m.group(2))
        except ValueError:
            continue

    if not found:
        _BUDGET_CACHE["data"] = None
        _BUDGET_CACHE["ts"] = now
        return None

    total = found.get("GPU_TOTAL_GB")
    gpu0 = {
        "total_gb": total,
        "desktop_gb": found.get("GPU0_DESKTOP"),
        "tts_gb": found.get("GPU0_TTS"),
        "comfyui_gb": found.get("GPU0_COMFYUI"),
        "headroom_gb": found.get("GPU0_HEADROOM"),
    }
    gpu1 = {
        "total_gb": total,
        "llm_gb": found.get("GPU1_LLM"),
        "headroom_gb": found.get("GPU1_HEADROOM"),
    }
    result = {"gpu0": gpu0, "gpu1": gpu1, "raw": found}
    _BUDGET_CACHE["data"] = result
    _BUDGET_CACHE["ts"] = now
    return result


def _read_vram_estimates() -> Optional[dict]:
    """Read cached per-workflow per-image VRAM estimates.

    Schema v3: ``{"version": 3, "entries": {key: {"peak_bytes",
    "marginal_bytes", "comfy_baseline_bytes", "model_state",
    "samples_count", "trusted", "measured_at", "source"}}}``.

    Trusted entries come from the in-run per-process probe, which samples
    ComfyUI's GPU0 VRAM via ``nvidia-smi --query-compute-apps`` filtered to
    the ComfyUI PID set. ``marginal_bytes`` is flux's *contribution* to
    ComfyUI's allocation (peak - before for warm runs, peak - baseline for
    cold runs) — insensitive to concurrent voxtral / desktop activity.

    v2 entries (whole-GPU ``total - min_free``) and v1 entries (flat int =
    endpoint diff) are surfaced for visibility but marked untrusted so the
    batcher re-measures.

    The status renderer reads ``trustworthy`` to decide whether to surface
    the "Measurement unavailable" banner: only when *no* entries are
    trusted do we flag the section.
    """
    try:
        if not _VRAM_ESTIMATES_PATH.is_file():
            return None
        raw = json.loads(_VRAM_ESTIMATES_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        st = _VRAM_ESTIMATES_PATH.stat()

        # Accept v3 (wrapped, with marginal_bytes), v2 (wrapped, peak only),
        # and v1 (flat) shapes; migrate v1/v2 on the fly to display shape.
        version = raw.get("version") if isinstance(raw.get("version"), int) else 1
        entries_blob = (
            raw.get("entries") if version >= 2 and isinstance(raw.get("entries"), dict)
            else raw
        )

        entries = []
        for k, v in entries_blob.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, dict):
                try:
                    peak = int(v.get("peak_bytes", 0))
                except (TypeError, ValueError):
                    peak = 0
                # v3 fields (present on records written by the per-process probe).
                # v2 records lack these; we synthesize defaults.
                has_v3 = "marginal_bytes" in v and "model_state" in v
                try:
                    marginal = int(v.get("marginal_bytes", peak))
                except (TypeError, ValueError):
                    marginal = peak
                try:
                    baseline = int(v.get("comfy_baseline_bytes", 0))
                except (TypeError, ValueError):
                    baseline = 0
                try:
                    samples_count = int(v.get("samples_count", 0))
                except (TypeError, ValueError):
                    samples_count = 0
                model_state = str(v.get("model_state", "unknown"))
                # v2 records used whole-GPU semantics — force untrusted so
                # callers don't plan against the old absolute numbers.
                trusted = bool(v.get("trusted", False)) and has_v3
                source = str(v.get("source", "unknown"))
                measured_at = int(v.get("measured_at", 0) or 0)
            elif isinstance(v, (int, float)):
                # Legacy v1 flat int — always untrusted, and the value is
                # the endpoint diff (residual) not the peak. Treat sub-64
                # MiB values as effectively zero so they don't pollute the
                # table.
                peak_raw = int(v)
                peak = peak_raw if peak_raw >= 64 * 1024 * 1024 else 0
                marginal = peak
                baseline = 0
                samples_count = 0
                model_state = "unknown"
                trusted = False
                source = "endpoint_diff_v1"
                measured_at = 0
            else:
                continue
            entries.append({
                "key": k,
                "peak_bytes": peak,
                "marginal_bytes": marginal,
                "comfy_baseline_bytes": baseline,
                "model_state": model_state,
                "samples_count": samples_count,
                "trusted": trusted,
                "source": source,
                "measured_at": measured_at,
            })
        entries.sort(key=lambda e: (not e["trusted"], -e["peak_bytes"]))

        any_trusted = any(e["trusted"] and e["peak_bytes"] > 0 for e in entries)
        return {
            "last_modified": int(st.st_mtime),
            "entries": entries,
            "total_entries": len(entries),
            "trusted_count": sum(1 for e in entries if e["trusted"]),
            "schema_version": version,
            "unavailable_reason": (
                None if any_trusted else (
                    "No trusted VRAM marginal measurements yet. The in-run "
                    "probe samples ComfyUI's per-process VRAM on GPU0 via "
                    "nvidia-smi during inference; entries appear here after "
                    "the next image generation completes. Untrusted rows "
                    "(if any) are legacy v1/v2 values or probe-fallback "
                    "values and should not be used for capacity planning."
                )
            ),
            "trustworthy": any_trusted,
        }
    except Exception:
        return None


def _images_budget(config: dict) -> dict:
    images = config.get("images", {}) or {}
    return {
        "max_batch_size": images.get("max_batch_size"),
        "vram_reserve_bytes": images.get("vram_reserve_bytes"),
        "signed_url_ttl": images.get("signed_url_ttl"),
        "cache_ttl": images.get("cache_ttl"),
        "cache_eviction_interval": images.get("cache_eviction_interval"),
        "cache_dir": images.get("cache_dir"),
    }


# --- capabilities matrix --------------------------------------------------

# Per-workflow-file capability cache keyed by (path, mtime). Re-reads
# automatically on file edit so capability rows stay honest (e.g. removing
# the ImageScaleBy chain from a workflow is reflected on the next request,
# not whenever the longer-lived 12s status cache rolls over).
_WORKFLOW_CAPS_CACHE: dict[tuple[str, int], dict] = {}


def _read_workflow_caps(path: Path) -> dict:
    """Parse a ComfyUI workflow JSON; cached per (path, mtime).

    Returns a dict with class-type counts pre-computed at multiple cardinalities
    so the renderer can show a compact row plus an expandable full breakdown.
    """
    try:
        st = path.stat()
    except OSError:
        return {
            "supports_i2v": False, "supports_audio": False, "two_stage": False,
            "node_count": 0, "top_class_types": {}, "all_class_types": {},
        }
    key = (str(path), int(st.st_mtime))
    cached = _WORKFLOW_CAPS_CACHE.get(key)
    if cached is not None:
        return cached
    supports_i2v = False
    supports_audio = False
    two_stage = False
    class_types: Counter = Counter()
    try:
        wf_json = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        wf_json = {}
    if isinstance(wf_json, dict):
        for v in wf_json.values():
            if not isinstance(v, dict):
                continue
            ct = v.get("class_type")
            if ct:
                class_types[str(ct)] += 1
            meta = v.get("_meta")
            if isinstance(meta, dict) and "image_node" in meta:
                supports_i2v = True
            if ct == "LoadImage":
                supports_i2v = True
            if ct == "LTXVAudioVAEDecode":
                supports_audio = True
            if ct == "LTXVCropGuides":
                two_stage = True
    result = {
        "supports_i2v": supports_i2v,
        "supports_audio": supports_audio,
        "two_stage": two_stage,
        "node_count": sum(class_types.values()),
        "top_class_types": dict(class_types.most_common(3)),
        "all_class_types": dict(class_types.most_common(50)),
        "mtime": int(st.st_mtime),
    }
    _WORKFLOW_CAPS_CACHE[key] = result
    if len(_WORKFLOW_CAPS_CACHE) > 64:
        for k in list(_WORKFLOW_CAPS_CACHE.keys())[:16]:
            _WORKFLOW_CAPS_CACHE.pop(k, None)
    return result


def _capabilities(config: dict) -> list[dict]:
    out: list[dict] = []
    videos_cfg = config.get("videos", {})
    workflows_cfg = videos_cfg.get("workflows", {}) or {}
    aliases = videos_cfg.get("aliases", {}) or {}

    workflow_caps: dict[str, dict] = {}
    base_dir = Path(__file__).parent

    for wf_name, wf_spec in workflows_cfg.items():
        try:
            file_rel = wf_spec.get("file", "")
            wf_path = (base_dir / file_rel).resolve()
            if wf_path.is_file():
                workflow_caps[wf_name] = _read_workflow_caps(wf_path)
            else:
                workflow_caps[wf_name] = {
                    "supports_i2v": False, "supports_audio": False, "two_stage": False,
                    "node_count": 0, "top_class_types": {}, "all_class_types": {},
                }
        except Exception:
            workflow_caps[wf_name] = {
                "supports_i2v": False, "supports_audio": False, "two_stage": False,
                "node_count": 0, "top_class_types": {}, "all_class_types": {},
            }

    for alias, target_wf in sorted(aliases.items()):
        wf_spec = workflows_cfg.get(target_wf, {}) or {}
        caps = workflow_caps.get(target_wf, {})
        out.append({
            "alias": alias,
            "workflow": target_wf,
            "max_seconds": wf_spec.get("max_seconds", 0),
            "min_seconds": wf_spec.get("min_seconds", 0),
            "max_size": wf_spec.get("max_size", "—"),
            "frame_rate": wf_spec.get("frame_rate", 0),
            "supports_i2v": bool(caps.get("supports_i2v")),
            "supports_audio": bool(caps.get("supports_audio")),
            "two_stage": bool(caps.get("two_stage")),
            "node_count": caps.get("node_count", 0),
            "top_class_types": caps.get("top_class_types") or {},
            "all_class_types": caps.get("all_class_types") or {},
            # Back-compat for any consumers still reading ``class_types``.
            "class_types": caps.get("top_class_types") or {},
        })
    return out


def _image_capabilities(config: dict) -> list[dict]:
    out: list[dict] = []
    try:
        comfy_cfg = config.get("backends", {}).get("comfyui", {})
        wf_dir = comfy_cfg.get("workflows_dir")
        if not wf_dir:
            return out
        p = Path(wf_dir)
        if not p.is_dir():
            return out
        for f in sorted(p.glob("*.json")):
            name = f.name
            if name.endswith(".ui.json") or ".obsolete" in name or ".bak" in name:
                continue
            caps = _read_workflow_caps(f)
            out.append({
                "name": f.stem,
                "supports_edit": "-edit" in f.stem,
                "class_type_count": caps.get("node_count", 0),
                "top_class_types": caps.get("top_class_types") or {},
                "all_class_types": caps.get("all_class_types") or {},
            })
    except Exception:
        pass
    return out


# --- driver / system ------------------------------------------------------

_XID_RE = re.compile(r"NVRM: Xid \(PCI:[^)]+\): (\d+)")


def _journal_xids() -> dict:
    """Parse journalctl -k for Xid events. Returns counts and last samples."""
    rc, out, _ = _run(
        ["journalctl", "-k", "--since", "24 hours ago", "--no-pager"],
        timeout=2.0,
    )
    if rc != 0 or not out:
        return {
            "last_xid_109": [],
            "by_code_24h": {},
            "by_code_60min": {},
            "last_5_events": [],
            "unavailable_reason": "journalctl returned no data" if rc == 0 else f"journalctl rc={rc}",
        }
    by_code_24h: Counter = Counter()
    by_code_60min: Counter = Counter()
    last_xid_109: list[str] = []
    last_events: list[dict] = []
    sixty_min_ago = time.time() - 3600
    for line in out.splitlines():
        m = _XID_RE.search(line)
        if not m:
            # Catch bare "Xid 109" form too.
            if "Xid 109" in line:
                code = "109"
            else:
                continue
        else:
            code = m.group(1)
        ts_match = re.match(r"^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})", line)
        ts_str = ts_match.group(1) if ts_match else line[:32]
        by_code_24h[code] += 1
        # Approximate "in last hour" by parsing month/day.
        try:
            yr = time.localtime().tm_year
            parsed = time.strptime(f"{ts_str} {yr}", "%b %d %H:%M:%S %Y")
            if time.mktime(parsed) >= sixty_min_ago:
                by_code_60min[code] += 1
        except Exception:
            pass
        last_events.append({"ts": ts_str, "code": code, "line": line[:200]})
        if code == "109":
            last_xid_109.append(ts_str)
    return {
        "last_xid_109": last_xid_109[-5:],
        "by_code_24h": dict(by_code_24h),
        "by_code_60min": dict(by_code_60min),
        "last_5_events": last_events[-5:],
    }


def _kernel_recent_events(n: int = 5) -> list[dict]:
    """Return the last N *non-Xid* kernel events that look like real
    hardware/driver issues — and the lead WARNING line of any kernel oops,
    with the full register dump tucked into a ``context`` block.

    Xid bursts are surfaced separately, so we skip them here. We also drop
    benign info dumps and oops-trace continuation lines (``Modules linked in:``
    and the leading-whitespace module-name continuations,
    ``NVRM: GPU at PCI:``, ``NVRM: GPU Board Serial Number``, register
    dumps) that would otherwise drown the signal.

    Returns ``list[dict]`` with ``ts``, ``summary``, and optional ``context``.
    The renderer treats string entries as legacy and dicts as the new shape.
    """
    # Bound to the current boot — older entries are stale and dilute the signal.
    rc, out, _ = _run(
        ["journalctl", "-k", "-b", "0", "--no-pager", "--since", "24 hours ago"],
        timeout=2.5,
    )
    if rc != 0 or not out:
        return []

    NOISE_PATTERNS = (
        re.compile(r"\bModules linked in:\b"),
        re.compile(r"\bNVRM: GPU at PCI:"),
        re.compile(r"\bNVRM: GPU Board Serial Number"),
        re.compile(r"\b(RIP|RSP|RBP|RAX|RBX|RCX|RDX|RSI|RDI|FS:|GS:|CR\d|DR\d|Code:):"),
        re.compile(r"\b(Call Trace:|Hardware name:|Stack:)"),
        # Boot-time benign noise that recurs on every boot — filter aggressively.
        re.compile(r"amdgpu.*Cannot find any crtc or sizes", re.IGNORECASE),
        re.compile(r"nvme.*using unchecked data buffer", re.IGNORECASE),
        re.compile(r"path_noexec\+0x"),
        re.compile(r"\bfs/exec\.c:\d+ path_noexec"),
        re.compile(r"\[drm\] Initialized amdgpu"),
        re.compile(r"\[drm\] Registered \d+ planes"),
        # Benign amdgpu init messages (driver coming up; not a hardware concern).
        re.compile(r"amdgpu.*ring \S+ uses VM inv eng", re.IGNORECASE),
        re.compile(r"amdgpu.*Runtime PM not available", re.IGNORECASE),
        re.compile(r"amdgpu.*SE \d+,?\s+SH per SE", re.IGNORECASE),
        re.compile(r"amdgpu.*RAS:.*supported", re.IGNORECASE),
        re.compile(r"amdgpu.*VRAM:.*\d+M", re.IGNORECASE),
        re.compile(r"amdgpu.*GTT:.*\d+M", re.IGNORECASE),
        re.compile(r"amdgpu.*will run gpu_recover", re.IGNORECASE),
        re.compile(r"amdgpu.*PSP runtime database", re.IGNORECASE),
        re.compile(r"amdgpu.*HMM registered", re.IGNORECASE),
        re.compile(r"amdgpu.*kfd device", re.IGNORECASE),
        # KFD / amdgpu topology init noise.
        re.compile(r"\bkfd kfd: amdgpu:", re.IGNORECASE),
        re.compile(r"amdgpu: Topology:", re.IGNORECASE),
        re.compile(r"amdgpu.*active_cu_number", re.IGNORECASE),
    )
    WARN_LEAD = re.compile(r"\b(WARNING:|BUG:|Oops:|kernel BUG at|panic:)")
    HW_KEYWORDS = (
        "amdgpu", "iommu", " mce", "mce:", "pcieport", " aer ",
        "thermal_zone", "edac", "nvme: ", "nvme0", "io error",
        "i/o error", "hardware error",
    )
    NVIDIA_HW_KEYWORDS = ("nvrm:", "nvidia-")

    def _is_module_continuation(raw_line: str) -> bool:
        idx = raw_line.find("kernel:")
        body = raw_line[idx + len("kernel:"):] if idx >= 0 else raw_line
        if not body.startswith(" "):
            return False
        body_stripped = body.strip()
        if not body_stripped:
            return False
        tokens = body_stripped.split()
        if len(tokens) < 6:
            return False
        # Cheap module-name heuristic: identifier-ish tokens with optional
        # "(POE)"/"(O)" markings, no normal sentence punctuation.
        mod_re = re.compile(r"^[a-zA-Z][\w\-]*(?:\([A-Z]+\))?$")
        return sum(1 for t in tokens if mod_re.match(t)) >= len(tokens) * 0.85

    keep: list[dict] = []
    pending_warn: Optional[dict] = None
    pending_context: list[str] = []

    def _flush_pending() -> None:
        nonlocal pending_warn, pending_context
        if pending_warn is not None:
            if pending_context:
                pending_warn["context"] = "\n".join(pending_context)
            keep.append(pending_warn)
        pending_warn = None
        pending_context = []

    for line in out.splitlines():
        l = line.lower()
        if "xid" in l or _XID_RE.search(line):
            _flush_pending()
            continue
        if any(p.search(line) for p in NOISE_PATTERNS):
            if pending_warn is not None:
                pending_context.append(line)
            continue
        if _is_module_continuation(line):
            if pending_warn is not None:
                pending_context.append(line)
            continue
        if WARN_LEAD.search(line):
            _flush_pending()
            ts, summary = _split_kernel_line(line)
            # All WARNING lines are treated as benign-by-default: only keep
            # them when followed by oops/trace context. Lone WARNINGs are
            # almost always sticky-on-driver-load noise and waste a slot.
            pending_warn = {"ts": ts, "summary": summary, "_benign": True}
            continue
        if any(s in l for s in HW_KEYWORDS) or any(
            s in l for s in NVIDIA_HW_KEYWORDS
        ):
            # Only keep if the line carries an error-class signal — bare amdgpu
            # init prints don't qualify per the user's spec.
            # Use word-boundary regex — substrings match too liberally
            # (e.g. "default" contains "fault", "panic_on_warn=0" contains "panic").
            ERROR_PATTERNS = (
                re.compile(r"\b(error|errors)\b", re.IGNORECASE),
                re.compile(r"\bfail(?:ed|ure|s)?\b", re.IGNORECASE),
                re.compile(r"\bfault(?:s)?\b", re.IGNORECASE),
                re.compile(r"\btimeout(?:s|ed)?\b", re.IGNORECASE),
                re.compile(r"\bpanic(?:ked|king)?\b", re.IGNORECASE),
                re.compile(r"\boops\b", re.IGNORECASE),
                re.compile(r"\bbug(?:s)?\b", re.IGNORECASE),
                re.compile(r"uncorrect", re.IGNORECASE),
                re.compile(r"correctable", re.IGNORECASE),
                re.compile(r"machine check", re.IGNORECASE),
                re.compile(r"thermal trip", re.IGNORECASE),
                re.compile(r"\bthrottle(?:d)?\b", re.IGNORECASE),
                re.compile(r"i/o error", re.IGNORECASE),
                re.compile(r"\bio error\b", re.IGNORECASE),
                re.compile(r"controller failed", re.IGNORECASE),
                re.compile(r"pcie bus error", re.IGNORECASE),
                re.compile(r"pcie[\s\-]?aer", re.IGNORECASE),
                re.compile(r"edac (?:mc\d+|m\d+):.*(error|csrow)", re.IGNORECASE),
            )
            # Skip if the line contains kernel cmdline syntax (panic_on_warn,
            # oops=continue, etc.) which matches our error words but is config.
            if "command line:" in l or "boot_image=" in l:
                continue
            if not any(p.search(line) for p in ERROR_PATTERNS):
                continue
            _flush_pending()
            ts, summary = _split_kernel_line(line)
            keep.append({"ts": ts, "summary": summary[:200]})
            continue
        # Resolve any pending benign WARNING: keep only if oops context
        # accumulated; otherwise drop.
        if pending_warn is not None and pending_warn.get("_benign"):
            if not pending_context:
                pending_warn = None
                pending_context = []
            else:
                pending_warn.pop("_benign", None)
                _flush_pending()
        else:
            _flush_pending()

    _flush_pending()
    keep = [k for k in keep if not k.get("_benign") or k.get("context")]
    for k in keep:
        k.pop("_benign", None)
    return keep[-n:]


def _split_kernel_line(line: str) -> tuple[str, str]:
    """Split a journalctl line into ``(timestamp, body)``. Returns the raw
    line as body if the prefix can't be located."""
    idx = line.find("kernel:")
    if idx < 0:
        return ("", line[:200])
    prefix = line[:idx].strip().rstrip(":")
    parts = prefix.split()
    # ["May", "08", "10:21:00", "peregrine"] → first three are the timestamp.
    ts = " ".join(parts[:3]) if len(parts) >= 4 else prefix
    body = line[idx + len("kernel:"):].strip()
    return (ts, body[:200])


def _reboot_history(n: int = 5) -> list[dict]:
    """Return up to N most recent boots as structured records.

    `journalctl --list-boots` prints rows of:
        <index> <boot_id> <first_msg_ts> <last_msg_ts>
    where index 0 is the current boot and last_msg_ts for the current boot is
    "now" (last journal entry). For older boots, last_msg_ts approximates the
    shutdown moment.

    Returns dicts with parsed timestamps so the renderer can compute deltas
    (and tooltip the boot_id rather than dump it inline).
    """
    rc, out, _ = _run(["journalctl", "--list-boots", "--no-pager"], timeout=1.5)
    if rc != 0 or not out:
        return []
    rows: list[dict] = []
    # journalctl format: "<idx> <boot_id> <first> — <last>" (some versions
    # use an em-dash separator) or whitespace-only between the two timestamps.
    # Be permissive: capture index, boot_id, then split the rest into two
    # halves by " — " or " - " or by a recognizable date prefix.
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        boot_id = parts[1]
        rest = parts[2]
        # Split timestamps. They look like "Fri 2026-05-08 10:16:07 CDT".
        # Use the position of the second weekday token to find the split.
        m = re.match(
            r"^(?P<first>[A-Za-z]{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\S+)"
            r"\s*[—\-]?\s*"
            r"(?P<last>[A-Za-z]{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\S+)?",
            rest,
        )
        first_ts = last_ts = None
        first_epoch = last_epoch = None
        if m:
            first_ts = m.group("first")
            last_ts = m.group("last")
            for label, value in (("first", first_ts), ("last", last_ts)):
                if not value:
                    continue
                try:
                    # Strip TZ for strptime; record raw string as-is.
                    bare, _, _tz = value.rpartition(" ")
                    parsed = time.strptime(bare, "%a %Y-%m-%d %H:%M:%S")
                    epoch = int(time.mktime(parsed))
                    if label == "first":
                        first_epoch = epoch
                    else:
                        last_epoch = epoch
                except Exception:
                    pass
        rows.append({
            "index": idx,
            "boot_id": boot_id,
            "first_msg_ts": first_ts,
            "last_msg_ts": last_ts,
            "first_msg_epoch": first_epoch,
            "last_msg_epoch": last_epoch,
            "duration_s": (
                last_epoch - first_epoch
                if first_epoch and last_epoch and last_epoch >= first_epoch
                else None
            ),
            "is_current": idx == 0,
        })
    # journalctl prints oldest→newest; reverse and trim.
    rows.reverse()
    # Filter out boot loops / recovery cycles: any historical boot that
    # lasted < 60s is almost always a kernel-panic-restart, an emergency
    # initramfs prompt, or a `systemctl reboot` that fired during early
    # boot. Including them in the table makes the user think the host is
    # unstable when really the previous boot just didn't complete. We keep
    # the current boot (index 0) regardless so a freshly-booted host still
    # shows up. duration_s=None is also kept (we don't know it failed).
    filtered: list[dict] = []
    for r in rows:
        if r.get("is_current"):
            filtered.append(r)
            continue
        d = r.get("duration_s")
        if d is None or d >= 60:
            filtered.append(r)
    return filtered[:n]


def _proxy_restart_count() -> Optional[int]:
    rc, out, _ = _run(
        ["systemctl", "--user", "show", "azizam.service", "-p", "NRestarts", "-p", "LoadState"],
        timeout=1.0,
    )
    if rc != 0 or "LoadState=not-found" in out:
        return None
    for line in out.splitlines():
        if line.startswith("NRestarts="):
            try:
                return int(line.split("=", 1)[1])
            except ValueError:
                return None
    return None


def _uptime_since() -> Optional[str]:
    rc, out, _ = _run(["uptime", "-s"])
    if rc == 0:
        return out.strip() or None
    return None


def _load_average() -> Optional[list[float]]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except Exception:
        return None


# --- disk -----------------------------------------------------------------

def _disk_pressure_reason(part: dict, config: dict) -> str:
    """Name the monitored volume from declared paths, without assuming its role."""
    target = str(part.get("target") or "/")
    label = str(part.get("label") or "").strip()
    model_dir = (config.get("model_routing") or {}).get("model_dir")
    if not label and isinstance(model_dir, str) and model_dir:
        try:
            if Path(model_dir).stat().st_dev == Path(target).stat().st_dev:
                label = "models volume"
        except OSError:
            pass
    subject = f"The {label}" if label else ("System disk (/)" if target == "/" else f"Disk ({target})")
    percent = float(part.get("pct_used") or 0)
    return (
        f"{subject} is almost full ({percent:.0f}%). "
        + ("Free space so new models and outputs can be saved." if label == "models volume"
           else "Free space on this volume so the gateway can continue to save files.")
    )


def _disk_partitions() -> list[dict]:
    """df for the operator-declared partitions (``AZIZAM_DISK_PARTITIONS``, default `/`)."""
    if _HOST_TELEMETRY_DISABLED:
        return []
    out_rows: list[dict] = []
    for target in _DISK_PARTITIONS_CONFIG:
        rc, out, _ = _run(["df", "-B1", "--output=source,size,used,avail,pcent,target", target], timeout=1.0)
        if rc != 0:
            continue
        lines = [l for l in out.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        parts = re.split(r"\s+", lines[1].strip(), maxsplit=5)
        if len(parts) < 6:
            continue
        try:
            size = int(parts[1])
            used = int(parts[2])
            avail = int(parts[3])
        except ValueError:
            continue
        # Match `df` semantics: pct = used / (used + avail). The naive
        # used/size form ignores reserved blocks and underreports fullness
        # by ~5 percentage points, hiding banner escalation.
        denom = used + avail
        out_rows.append({
            "source": parts[0],
            "target": parts[5],
            "display_name": "System disk (/)" if parts[5] == "/" else f"Disk ({parts[5]})",
            "size_bytes": size,
            "used_bytes": used,
            "avail_bytes": avail,
            "pct_used": round((used / denom) * 100.0, 1) if denom > 0 else 0.0,
        })
    # Dedup if /home and /var/log are bind-mounts of the same source as /.
    seen_sources: set[str] = set()
    deduped: list[dict] = []
    # Prefer to keep "/" (root) when duplicates appear.
    out_rows.sort(key=lambda r: 0 if r["target"] == "/" else 1)
    for r in out_rows:
        if r["source"] in seen_sources:
            continue
        deduped.append(r)
        seen_sources.add(r["source"])
    deduped.sort(key=lambda r: r["target"])
    return deduped


def _dir_summary(name: str, path: Path) -> dict:
    """Compute total size, file count, oldest/newest mtime for path (shallow walk capped)."""
    if not path.exists():
        return {"name": name, "path": str(path), "exists": False}
    try:
        if path.is_file():
            st = path.stat()
            return {
                "name": name,
                "path": str(path),
                "exists": True,
                "is_file": True,
                "size_bytes": st.st_size,
                "file_count": 1,
                "oldest_mtime": int(st.st_mtime),
                "newest_mtime": int(st.st_mtime),
            }
        # Walk; cap at 50k files to avoid runaway scans.
        total = 0
        count = 0
        oldest = None
        newest = None
        biggest = None
        biggest_size = 0
        deadline = time.monotonic() + 1.5
        for root, _dirs, files in os.walk(path):
            for fn in files:
                if time.monotonic() > deadline:
                    break
                full = os.path.join(root, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                total += st.st_size
                count += 1
                if oldest is None or st.st_mtime < oldest:
                    oldest = st.st_mtime
                if newest is None or st.st_mtime > newest:
                    newest = st.st_mtime
                if st.st_size > biggest_size:
                    biggest_size = st.st_size
                    biggest = full
            if time.monotonic() > deadline:
                break
            if count >= 50000:
                break
        return {
            "name": name,
            "path": str(path),
            "exists": True,
            "is_file": False,
            "size_bytes": total,
            "file_count": count,
            "oldest_mtime": int(oldest) if oldest else None,
            "newest_mtime": int(newest) if newest else None,
            "biggest_file": biggest,
            "biggest_file_size_bytes": biggest_size,
        }
    except Exception as e:
        return {"name": name, "path": str(path), "exists": True, "error": _safe_err(e)}


def _journal_disk_usage() -> Optional[int]:
    rc, out, _ = _run(["journalctl", "--disk-usage"], timeout=1.0)
    if rc != 0 or not out:
        return None
    # Output contains "Archived and active journals take up X.YG ..."
    m = re.search(r"take up ([\d.]+)([KMGT])", out)
    if not m:
        return None
    val = float(m.group(1))
    suffix = m.group(2)
    mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}[suffix]
    return int(val * mult)


# --- thermal / CPU --------------------------------------------------------

def _read_sensors() -> dict:
    if _HOST_TELEMETRY_DISABLED:
        return {"unavailable_reason": "host telemetry disabled for this instance"}
    rc, out, _ = _run(["sensors", "-j"], timeout=1.5)
    if rc != 0 or not out:
        return {"unavailable_reason": "sensors not found or failed"}
    try:
        data = json.loads(out)
    except Exception:
        return {"unavailable_reason": "sensors output not JSON"}

    # Extract the high-signal numbers.
    cpu_temps = []
    dimm_temps = []
    nvme_temp = None
    amdgpu_edge_temp = None
    for chip, payload in data.items():
        if not isinstance(payload, dict):
            continue
        if chip.startswith("k10temp"):
            for k, v in payload.items():
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if "input" in kk and isinstance(vv, (int, float)):
                            cpu_temps.append({"label": k, "temp_c": vv})
        elif chip.startswith("spd5118"):
            for k, v in payload.items():
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if "input" in kk and isinstance(vv, (int, float)):
                            dimm_temps.append({"chip": chip, "label": k, "temp_c": vv})
        elif chip.startswith("nvme"):
            for k, v in payload.items():
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if "input" in kk and isinstance(vv, (int, float)):
                            if nvme_temp is None or vv > nvme_temp:
                                nvme_temp = vv
        elif chip.startswith("amdgpu"):
            for k, v in payload.items():
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        if "input" in kk and isinstance(vv, (int, float)) and "edge" in k.lower():
                            amdgpu_edge_temp = vv

    cpu_max = max((c["temp_c"] for c in cpu_temps), default=None)
    dimm_max = max((d["temp_c"] for d in dimm_temps), default=None)

    # Per-component thresholds (tightened for desktop AMD CPUs):
    #   CPU (AMD desktop): warn 80, critical 90 (sustained 86C+ is concerning).
    #   DDR5 DIMM:         Tcasemax ~85C, critical ~95C; warn 80, crit 90.
    #   NVMe SSD:          most controllers throttle 70-75C, critical 80C+;
    #                      warn 70, crit 80.
    return {
        "cpu_temps": cpu_temps,
        "cpu_max_c": cpu_max,
        "cpu_thermal_state": _thermal_state(cpu_max, warn=80, crit=90),
        "dimm_temps": dimm_temps,
        "dimm_max_c": dimm_max,
        "dimm_thermal_state": _thermal_state(dimm_max, warn=80, crit=90),
        "nvme_max_c": nvme_temp,
        "nvme_thermal_state": _thermal_state(nvme_temp, warn=70, crit=80),
        "amdgpu_edge_c": amdgpu_edge_temp,
        "amdgpu_thermal_state": _thermal_state(amdgpu_edge_temp, warn=90, crit=100),
    }


def _thermal_state(temp: Optional[float], warn: float, crit: float) -> str:
    if temp is None:
        return "unknown"
    if temp >= crit:
        return "critical"
    if temp >= warn:
        return "warn"
    return "ok"


def _cpu_usage_quick() -> dict:
    """Top-N processes by CPU/memory plus a load summary. ~200ms.

    Filter rules (avoid noise from idle kernel threads):
      - Drop rows with cpu_pct < 0.5 AND mem_pct < 0.1.
      - Sort by cpu_pct desc, then mem_pct desc.
      - Cap at 10 visible rows.
    """
    rc, out, _ = _run(["top", "-b", "-n", "1", "-w", "200"], timeout=1.0)
    info: dict[str, Any] = {"top_procs": [], "load": _load_average()}
    if rc != 0 or not out:
        return info
    lines = out.splitlines()
    in_table = False
    candidates: list[dict] = []
    # Pull a generous number of candidates from `top` so the filter still
    # gives us 10 substantive rows after dropping idle kworkers.
    for line in lines:
        if line.strip().startswith("PID"):
            in_table = True
            continue
        if not in_table:
            continue
        parts = line.split()
        if len(parts) < 12:
            continue
        try:
            row = {
                "pid": int(parts[0]),
                "user": parts[1],
                "cpu_pct": float(parts[8]),
                "mem_pct": float(parts[9]),
                "command": " ".join(parts[11:])[:120],
            }
        except (ValueError, IndexError):
            continue
        candidates.append(row)
        if len(candidates) >= 60:
            break

    procs = [
        p for p in candidates
        if not (p["cpu_pct"] < 0.5 and p["mem_pct"] < 0.1)
    ]
    procs.sort(key=lambda p: (-p["cpu_pct"], -p["mem_pct"]))

    # Bucket: surface proxy-managed services first, lump desktop/UI noise into
    # one combined row. The user cares about which AI service is busy, not
    # which Brave window is on top.
    SERVICE_PATTERNS = (
        "proxy.py", "llama-server", "comfyui", "comfy_server",
        "voxtral", "parakeet", "daisy", "claude", "tabby",
        "main.py", "uvicorn",
    )

    def _is_service(cmd: str) -> bool:
        c = cmd.lower()
        return any(pat in c for pat in SERVICE_PATTERNS)

    services: list[dict] = []
    desktop: list[dict] = []
    for p in procs:
        if _is_service(p.get("command") or ""):
            services.append(p)
        else:
            desktop.append(p)

    # Cap services at 10. If desktop is non-empty, append a bucketed row.
    visible = services[:10]
    if desktop:
        cpu_sum = round(sum(p["cpu_pct"] for p in desktop), 1)
        mem_sum = round(sum(p["mem_pct"] for p in desktop), 1)
        visible.append({
            "pid": 0,
            "user": "—",
            "cpu_pct": cpu_sum,
            "mem_pct": mem_sum,
            "command": f"desktop (combined: {len(desktop)} procs — brave/neko/codex/etc.)",
        })
    info["top_procs"] = visible
    return info


# --- recent renders -------------------------------------------------------

_VIDEO_DIR = Path(_COMFY_OUTPUT_ROOT / "video") if _COMFY_OUTPUT_ROOT else None
_IMAGE_DIR = _COMFY_OUTPUT_ROOT

# File identity includes size and nanosecond mtime, including in-place rewrites.
_FFPROBE_CACHE: OrderedDict[tuple[str, int, int], dict] = OrderedDict()
_FFPROBE_CACHE_LOCK = threading.Lock()
_FFPROBE_CACHE_MAX = 200
_IMG_DIMS_CACHE: dict[tuple[str, int], dict] = {}
# Cached extracted positive-prompt text per image file. Keyed by (path, mtime)
# so a re-encode of the same filename invalidates the cache. Bounded.
_IMG_PROMPT_CACHE: dict[tuple[str, int], Optional[str]] = {}


def _ffprobe_video(path: Path, *, size: int, mtime_ns: int) -> dict:
    """Return ``{duration_s, width, height, has_audio}`` or ``{error}``."""
    key = (str(path), size, mtime_ns)
    with _FFPROBE_CACHE_LOCK:
        cached = _FFPROBE_CACHE.get(key)
        if cached is not None:
            _FFPROBE_CACHE.move_to_end(key)
            return cached
    rc, out, err = _run(
        [
            "ffprobe",
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=1.0,
    )
    info: dict = {}
    if rc != 0 or not out:
        info = {"error": (err or "ffprobe failed")[:80]}
    else:
        try:
            data = json.loads(out)
            streams = data.get("streams") or []
            fmt = data.get("format") or {}
            dur = fmt.get("duration")
            try:
                info["duration_s"] = round(float(dur), 2) if dur is not None else None
            except (TypeError, ValueError):
                info["duration_s"] = None
            v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            if v_stream:
                info["width"] = v_stream.get("width")
                info["height"] = v_stream.get("height")
            info["has_audio"] = any(s.get("codec_type") == "audio" for s in streams)
        except Exception as e:  # noqa: BLE001
            info = {"error": _safe_err(e)[:80]}
    with _FFPROBE_CACHE_LOCK:
        _FFPROBE_CACHE[key] = info
        _FFPROBE_CACHE.move_to_end(key)
        while len(_FFPROBE_CACHE) > _FFPROBE_CACHE_MAX:
            _FFPROBE_CACHE.popitem(last=False)
    return info


def _image_dims(path: Path, mtime: int) -> dict:
    """Return ``{width, height}`` from PNG/WEBP/JPG header without decoding."""
    key = (str(path), mtime)
    cached = _IMG_DIMS_CACHE.get(key)
    if cached is not None:
        return cached
    info: dict = {}
    try:
        # Use Pillow if available — it lazy-reads the header. Falls back to
        # ffprobe (always present) if Pillow isn't on the path.
        try:
            from PIL import Image  # type: ignore
            with Image.open(path) as im:
                info["width"], info["height"] = im.size
        except Exception:
            rc, out, _ = _run(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "v:0",
                    "-show_entries", "stream=width,height",
                    "-of", "csv=p=0:s=,",
                    str(path),
                ],
                timeout=1.0,
            )
            if rc == 0 and out.strip():
                w, _, h = out.strip().partition(",")
                try:
                    info["width"], info["height"] = int(w), int(h)
                except ValueError:
                    pass
    except Exception as e:  # noqa: BLE001
        info["error"] = _safe_err(e)[:80]
    _IMG_DIMS_CACHE[key] = info
    if len(_IMG_DIMS_CACHE) > 200:
        for k in list(_IMG_DIMS_CACHE.keys())[:50]:
            _IMG_DIMS_CACHE.pop(k, None)
    return info


def _extract_image_prompt(path: Path, mtime: int) -> Optional[str]:
    """Return the positive-prompt text embedded in a ComfyUI PNG, or None.

    ComfyUI stores the full submitted graph in the ``prompt`` PNG-info chunk.
    We pick the first non-empty ``CLIPTextEncode.inputs.text`` (the positive
    prompt). Falls back to a sidecar JSON next to the file if the PNG chunk
    isn't there.

    Cached per (path, mtime). Returns truncated to 240 chars; the caller can
    further trim for display. Best-effort: any error returns None silently.
    """
    key = (str(path), mtime)
    if key in _IMG_PROMPT_CACHE:
        return _IMG_PROMPT_CACHE[key]
    text: Optional[str] = None
    try:
        # 1) PNG-info chunk via Pillow (lazy header read).
        try:
            from PIL import Image  # type: ignore
            with Image.open(path) as im:
                raw = im.info.get("prompt")
            if raw:
                graph = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(graph, dict):
                    for _, node in graph.items():
                        if not isinstance(node, dict):
                            continue
                        ct = node.get("class_type") or ""
                        if "CLIPText" not in ct and "TextEncode" not in ct:
                            continue
                        t = (node.get("inputs") or {}).get("text")
                        if isinstance(t, str) and t.strip():
                            text = t.strip()
                            break
        except Exception:
            pass
        # 2) Sidecar JSON fallback (`<file>.json` next to the image).
        if not text:
            sidecar = path.with_suffix(path.suffix + ".json")
            if not sidecar.is_file():
                sidecar = path.with_suffix(".json")
            if sidecar.is_file():
                try:
                    data = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    data = None
                if isinstance(data, dict):
                    # Common shapes: {"prompt": "..."} or {"positive": "..."}
                    for k in ("prompt", "positive", "text"):
                        v = data.get(k)
                        if isinstance(v, str) and v.strip():
                            text = v.strip()
                            break
    except Exception:
        text = None
    if text is not None and len(text) > 240:
        text = text[:240]
    _IMG_PROMPT_CACHE[key] = text
    if len(_IMG_PROMPT_CACHE) > 200:
        for k in list(_IMG_PROMPT_CACHE.keys())[:50]:
            _IMG_PROMPT_CACHE.pop(k, None)
    return text


def _video_prompt_for(
    *,
    alias: Optional[str],
    mtime: int,
    video_meta: dict[str, dict],
    recent_events: list[dict],
) -> Optional[str]:
    """Best-effort lookup of the prompt text that produced a recent video.

    Strategy: find the most-recent ``video_submit`` event whose ``alias``
    matches and whose ``ts`` is just before this file's mtime. Then resolve
    its ``prompt_id`` against ``firehose_metrics._VIDEO_META`` (which holds
    the truncated prompt for ~6h after submission). Older renders return
    None — the in-memory window has GCed.
    """
    if not alias or not isinstance(recent_events, list):
        return None
    candidates = [
        e for e in recent_events
        if e.get("type") == "video_submit"
        and e.get("alias") == alias
        and e.get("prompt_id")
        and (e.get("ts") or 0) <= float(mtime) + 5  # 5s slack: event before save
    ]
    if not candidates:
        return None
    # Closest event with ts <= mtime (latest before).
    candidates.sort(key=lambda e: float(e.get("ts") or 0), reverse=True)
    pid = candidates[0].get("prompt_id")
    meta = (video_meta or {}).get(pid) or {}
    pt = meta.get("prompt_truncated")
    if isinstance(pt, str) and pt.strip():
        return pt.strip()
    return None


def _recent_renders_videos(
    n: int = 20,
    *,
    video_meta: Optional[dict[str, dict]] = None,
    recent_events: Optional[list[dict]] = None,
) -> list[dict]:
    if not _VIDEO_DIR or not _VIDEO_DIR.is_dir():
        return []
    try:
        files = [f for f in _VIDEO_DIR.iterdir() if f.suffix == ".mp4" and f.is_file()]
    except Exception:
        return []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out: list[dict] = []
    meta = video_meta or {}
    events = recent_events or []
    for f in files[:n]:
        try:
            st = f.stat()
        except OSError:
            continue
        m = re.match(r"^([A-Za-z0-9.\-]+?)_\d{4,}_", f.name)
        alias = m.group(1) if m else None
        # Derive video_id from filename: ComfyUI emits `<alias>_<seq>_.mp4`,
        # but the proxy's /v1/videos/{id}/content uses opaque IDs registered
        # at submission time. We surface the bare filename so the renderer can
        # build a static content URL (proxy serves cached files).
        probe = _ffprobe_video(f, size=st.st_size, mtime_ns=st.st_mtime_ns)
        prompt = _video_prompt_for(
            alias=alias,
            mtime=int(st.st_mtime),
            video_meta=meta,
            recent_events=events,
        )
        out.append({
            "filename": f.name,
            "path": str(f),
            "size_bytes": st.st_size,
            "mtime": int(st.st_mtime),
            "alias": alias,
            "duration_s": probe.get("duration_s"),
            "width": probe.get("width"),
            "height": probe.get("height"),
            "has_audio": probe.get("has_audio"),
            "probe_error": probe.get("error"),
            "prompt": prompt,
        })
    return out


def _recent_renders_images(n: int = 10) -> list[dict]:
    if not _IMAGE_DIR or not _IMAGE_DIR.is_dir():
        return []
    try:
        files = [
            f for f in _IMAGE_DIR.iterdir()
            if f.suffix.lower() in (".png", ".webp", ".jpg") and f.is_file()
        ]
    except Exception:
        return []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for f in files[:n]:
        try:
            st = f.stat()
        except OSError:
            continue
        dims = _image_dims(f, int(st.st_mtime))
        prompt = _extract_image_prompt(f, int(st.st_mtime))
        out.append({
            "filename": f.name,
            "path": str(f),
            "size_bytes": st.st_size,
            "mtime": int(st.st_mtime),
            "width": dims.get("width"),
            "height": dims.get("height"),
            "probe_error": dims.get("error"),
            "prompt": prompt,
        })
    return out


# --- voxtral / parakeet enriched ------------------------------------------

async def _enriched_aux_health() -> dict:
    out: dict[str, Any] = {"voxtral": None, "parakeet": None}
    async def _get(client, url):
        try:
            r = await client.get(url)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            voxtral, parakeet = await asyncio.gather(
                _get(client, "http://127.0.0.1:8001/health"),
                _get(client, "http://127.0.0.1:5092/health"),
            )
        out["voxtral"] = voxtral
        out["parakeet"] = parakeet
    except Exception:
        pass
    return out


def _voices_summary(proxy_module) -> Optional[dict]:
    """Summary of /v1/audio/voices: count + language coverage + alias breakdown.

    Reads VOICE_MAP + _voice_metadata directly from the proxy module to
    avoid a self-loopback HTTP call (the /v1/audio/voices endpoint requires
    bearer auth, so the previous httpx call returned 401).

    Adds per-voice classification (alias vs native) and the OpenAI alias
    mapping table so the renderer can show "alloy → neutral_female" etc.
    """
    if proxy_module is None:
        return None
    try:
        voice_map = getattr(proxy_module, "VOICE_MAP", None) or {}
        meta_fn = getattr(proxy_module, "_voice_metadata", None)
        openai_alias_names = getattr(proxy_module, "_OPENAI_VOICE_NAMES", None) or {}
        if not voice_map or not callable(meta_fn):
            return None
        langs: Counter = Counter()
        genders: Counter = Counter()
        sources: Counter = Counter()
        openai_alias_map: dict[str, str] = {}
        native_voices: list[str] = []
        count = 0
        for voice_id, mapped in voice_map.items():
            try:
                meta = meta_fn(voice_id, mapped) or {}
            except Exception:
                meta = {}
            langs[meta.get("language") or "unknown"] += 1
            genders[meta.get("gender") or "unknown"] += 1
            count += 1
            # Classify: an "OpenAI alias" is anything in _OPENAI_VOICE_NAMES;
            # everything else is treated as a native Voxtral voice.
            if voice_id in openai_alias_names:
                sources["openai_alias"] += 1
                openai_alias_map[voice_id] = mapped
            else:
                sources["voxtral_native"] += 1
                native_voices.append(voice_id)
        return {
            "count": count,
            "languages": dict(langs),
            "genders": dict(genders),
            "sources": dict(sources),
            "openai_alias_map": dict(sorted(openai_alias_map.items())),
            "native_voices": sorted(native_voices),
            "example_curl": (
                "curl -s http://127.0.0.1:5000/v1/audio/speech "
                "-H 'Authorization: Bearer $AZIZAM_KEY' "
                "-H 'Content-Type: application/json' "
                "-d '{\"model\":\"voxtral-4b-tts\",\"voice\":\"nova\",\"input\":\"hello\"}' "
                "--output out.mp3"
            ),
            "list_endpoint": "/v1/audio/voices",
        }
    except Exception:
        return None


# --- daisy ---------------------------------------------------------------

_DAISY_MODELS_JSON = _optional_path(
    "AZIZAM_DAISY_MODELS_JSON",
    os.environ.get("AZIZAM_DAISY_DIR", "") and os.path.join(os.environ["AZIZAM_DAISY_DIR"], ".pi", "agent", "models.json") or "",
)
_DAISY_TMUX_TARGET = "main:daisy"


def _daisy_pi_context() -> dict:
    """Read pi context budget from daisy's models.json and estimate current
    usage from the daisy tmux pane status line.

    Both lookups are best-effort. If models.json is missing the section is
    returned with ``unavailable_reason``; if tmux is unreachable we still
    return the configured ``context_window`` and stamp ``tmux_unavailable_reason``.
    """
    info: dict[str, Any] = {}
    try:
        if _DAISY_MODELS_JSON and _DAISY_MODELS_JSON.is_file():
            data = json.loads(_DAISY_MODELS_JSON.read_text(encoding="utf-8")) or {}
            ctx_window = None
            for prov in (data.get("providers") or {}).values():
                if not isinstance(prov, dict):
                    continue
                models = prov.get("models") or []
                if models and isinstance(models[0], dict):
                    ctx_window = models[0].get("contextWindow")
                    break
            info["context_window"] = ctx_window
        else:
            info["unavailable_reason"] = f"models.json not found: {_DAISY_MODELS_JSON}"
            return info
    except Exception as e:
        info["unavailable_reason"] = _safe_err(e)
        return info

    # tmux pane status — daisy renders something like "0.5%/131k (auto)" on
    # one of the bottom lines. We look for that pattern in the pane tail.
    try:
        proc = subprocess.run(
            ["tmux", "capture-pane", "-t", _DAISY_TMUX_TARGET, "-p"],
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )
        if proc.returncode == 0 and proc.stdout:
            tail = proc.stdout.rstrip().splitlines()[-12:]
            pct_used = None
            status_line = None
            for line in tail:
                m = re.search(r"(\d+(?:\.\d+)?)\s*%/(\d+(?:\.\d+)?)\s*([kKmM])\b", line)
                if m:
                    pct_used = float(m.group(1))
                    status_line = m.group(0)
                    break
            info["percent_used"] = pct_used
            info["status_line"] = status_line
            ctx_w = info.get("context_window")
            if isinstance(ctx_w, (int, float)) and pct_used is not None:
                info["current_estimated"] = int(ctx_w * pct_used / 100.0)
        else:
            info["tmux_unavailable_reason"] = (
                f"tmux capture-pane returned rc={proc.returncode}"
            )
    except FileNotFoundError:
        info["tmux_unavailable_reason"] = "tmux not on PATH"
    except subprocess.TimeoutExpired:
        info["tmux_unavailable_reason"] = "tmux capture timed out"
    except Exception as e:  # noqa: BLE001
        info["tmux_unavailable_reason"] = _safe_err(e)
    return info


def _journal_last_telegram_ts() -> Optional[str]:
    """Return the ISO timestamp of the most recent daisy telegram-related
    journal line, or None on failure. 1.5s timeout."""
    try:
        proc = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u", "daisy.service",
                "-n", "200",
                "--no-pager",
                "-o", "short-iso",
            ],
            capture_output=True, text=True, timeout=1.5,
        )
        if proc.returncode != 0:
            return None
        last_ts: Optional[str] = None
        for line in proc.stdout.splitlines():
            if "telegram" not in line.lower():
                continue
            m = re.match(
                r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{2}:?\d{2})",
                line,
            )
            if m:
                last_ts = m.group(1)
        return last_ts
    except Exception:
        return None


def _daisy_telegram_bridge() -> dict:
    """Inspect daisy's tmux pane and ``daisy.service`` journal for the most
    recent Telegram bridge transition. Returns
    ``{state, last_seen, last_line, source, unavailable_reason}``.

    Tmux is preferred because daisy's status line ("telegram connected") is
    the freshest signal — the journal often only carries the original failed
    start from a previous boot.
    """
    info: dict[str, Any] = {}
    # 1) Tmux pane — freshest signal when daisy is actively running.
    try:
        proc = subprocess.run(
            ["tmux", "capture-pane", "-t", _DAISY_TMUX_TARGET, "-p"],
            capture_output=True, text=True, timeout=_SUBPROC_TIMEOUT,
        )
        if proc.returncode == 0 and proc.stdout:
            for line in proc.stdout.rstrip().splitlines()[-12:]:
                low = line.lower()
                if "telegram connected" in low:
                    info["state"] = "connected"
                    info["source"] = "tmux"
                    info["last_line"] = line.strip()[:300]
                    info["last_seen"] = _journal_last_telegram_ts() or None
                    return info
                if "telegram disconnected" in low or "telegram error" in low:
                    info["state"] = "disconnected"
                    info["source"] = "tmux"
                    info["last_line"] = line.strip()[:300]
                    info["last_seen"] = _journal_last_telegram_ts() or None
                    return info
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass

    # 2) Fallback: scan journal for the most recent transition.
    try:
        proc = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u", "daisy.service",
                "-n", "200",
                "--no-pager",
                "-o", "short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=_SUBPROC_TIMEOUT,
        )
        if proc.returncode != 0:
            info["unavailable_reason"] = (
                f"journalctl rc={proc.returncode}: {(proc.stderr or '')[:160]}"
            )
            return info
        last_relevant: Optional[str] = None
        for line in proc.stdout.splitlines():
            ll = line.lower()
            if "telegram" in ll and (
                "connect" in ll or "disconnect" in ll or "error" in ll or "fail" in ll
            ):
                last_relevant = line
        if not last_relevant:
            info["state"] = "unknown"
            info["last_line"] = None
            return info
        info["last_line"] = last_relevant[-300:]
        info["source"] = "journal"
        ll = last_relevant.lower()
        if "disconnect" in ll or "fail" in ll or "error" in ll:
            info["state"] = "disconnected"
        elif "connected" in ll:
            info["state"] = "connected"
        else:
            info["state"] = "unknown"
        m = re.match(
            r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{2}:?\d{2})",
            last_relevant,
        )
        if m:
            info["last_seen"] = m.group(1)
    except FileNotFoundError:
        info["unavailable_reason"] = "journalctl not on PATH"
    except subprocess.TimeoutExpired:
        info["unavailable_reason"] = "journalctl timed out"
    except Exception as e:  # noqa: BLE001
        info["unavailable_reason"] = _safe_err(e)
    return info


def _daisy_audit_summary() -> dict:
    """Read the last few audit entries from daisy's audit log."""
    if _DAISY_AUDIT_PATH is None:
        return {"path": None, "exists": False, "unavailable_reason": "AZIZAM_DAISY_AUDIT_PATH not configured"}
    info: dict[str, Any] = {"path": str(_DAISY_AUDIT_PATH), "exists": _DAISY_AUDIT_PATH.is_file()}
    if not _DAISY_AUDIT_PATH.is_file():
        return info
    try:
        st = _DAISY_AUDIT_PATH.stat()
        info["last_modified"] = int(st.st_mtime)
        info["size_bytes"] = st.st_size
        # Read last ~32k bytes; parse JSON lines for last 5 prompts.
        with open(_DAISY_AUDIT_PATH, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 32 * 1024))
            tail = f.read().decode("utf-8", errors="replace")
        lines = [l for l in tail.splitlines() if l.strip()]
        info["line_count_tail"] = len(lines)
        recent = []
        for line in lines[-5:]:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            # Daisy's audit log shape (current): {ts, tool, cwd, command,
            # risk_rule, decision, decision_ts, ...}. Older shapes used
            # {timestamp, kind|type, summary|prompt|action}. Try both.
            kind = (
                rec.get("kind")
                or rec.get("type")
                or rec.get("tool")
                or rec.get("risk_rule")
            )
            summary = (
                rec.get("summary")
                or rec.get("prompt")
                or rec.get("action")
                or rec.get("command")
                or ""
            )
            entry = {
                "ts": rec.get("timestamp") or rec.get("ts"),
                "kind": kind,
                "summary": _truncate(str(summary), 160),
            }
            if rec.get("decision"):
                entry["decision"] = rec["decision"]
            recent.append(entry)
        info["recent_entries"] = recent
    except Exception as e:
        info["error"] = _safe_err(e)

    # Live tmux scrollback peek: surface "are we currently working" by
    # scanning the daisy pane for the spinner glyph. 1s timeout; failure is
    # silent (most hosts won't have the pane). Distinct keys so it's clear
    # this is a live activity probe vs the audit-log summary above.
    try:
        rc, out, _ = _run(
            ["tmux", "capture-pane", "-t", "main:daisy", "-p", "-S", "-100"],
            timeout=1.0,
        )
        if rc == 0 and out:
            text = out
            working = (
                "⠸ Working" in text
                or "⠹ Working" in text
                or "⠴ Working" in text
                or "Working..." in text
            )
            info["tmux_state"] = "working" if working else "idle"
            last_user_at = last_assistant_at = None
            now = int(time.time())
            user_markers = ("[telegram]", "[user]", "user:")
            assistant_markers = ("[assistant]", "<!--", "assistant:", "claude:")
            matched_any = False
            for line in text.splitlines():
                low = line.lower()
                if any(m in low for m in user_markers) or low.lstrip().startswith(">"):
                    last_user_at = now
                    matched_any = True
                if any(m in low for m in assistant_markers):
                    last_assistant_at = now
                    matched_any = True
            info["tmux_last_user_at"] = last_user_at
            info["tmux_last_assistant_at"] = last_assistant_at
            if not matched_any:
                info["tmux_unavailable_reason"] = (
                    "tmux scrollback parse: no recognizable activity markers"
                )
        else:
            info["tmux_state"] = None
            info["tmux_unavailable_reason"] = (
                f"tmux capture-pane rc={rc}" if rc != 0 else "tmux capture-pane empty"
            )
    except Exception as e:
        info["tmux_state"] = None
        info["tmux_unavailable_reason"] = f"tmux peek failed: {_safe_err(e)}"
    return info


# --- workflow drift ------------------------------------------------------

def _workflow_drift_status() -> Optional[dict]:
    """Read cached workflow drift report if available."""
    if not _DRIFT_REPORT_PATH.is_file():
        return {"unavailable_reason": "no cached drift report; run scripts/check_workflow_drift.py"}
    try:
        st = _DRIFT_REPORT_PATH.stat()
        data = json.loads(_DRIFT_REPORT_PATH.read_text(encoding="utf-8"))
        return {
            "last_modified": int(st.st_mtime),
            "report": data,
        }
    except Exception as e:
        return {"unavailable_reason": _safe_err(e)}


# --- swap state ----------------------------------------------------------

def _swap_state(proxy_module) -> dict:
    """Tabby/Kobold model-swap lock state from proxy module globals."""
    out: dict[str, Any] = {}
    try:
        tabby_lock = getattr(proxy_module, "tabby_swap_lock", None)
        out["tabby_swap_in_progress"] = tabby_lock.locked() if tabby_lock else None
    except Exception:
        out["tabby_swap_in_progress"] = None
    try:
        kobold_lock = getattr(proxy_module, "kobold_swap_lock", None)
        out["kobold_swap_in_progress"] = kobold_lock.locked() if kobold_lock else None
    except Exception:
        out["kobold_swap_in_progress"] = None
    return out


def _comfy_ws_state(proxy_module) -> dict:
    return {
        "connected": bool(getattr(proxy_module, "_comfy_ws_connected", False)),
        "client_id": getattr(proxy_module, "COMFY_CLIENT_ID", None),
    }


# --- live video jobs -----------------------------------------------------

def _live_video_jobs(proxy_module, video_meta: dict[str, dict]) -> list[dict]:
    """Combine in-process _video_progress with submit-time metadata."""
    progress = getattr(proxy_module, "_video_progress", {}) or {}
    out: list[dict] = []
    for prompt_id, p in progress.items():
        if not isinstance(p, dict):
            continue
        meta = video_meta.get(prompt_id) or {}
        submitted_at = meta.get("submitted_at")
        updated_at = p.get("updated_at")
        elapsed = (
            int(time.time() - submitted_at)
            if submitted_at else None
        )
        # ETA: if percent>5, project linearly from elapsed time.
        pct = p.get("percent") or 0
        eta = None
        if elapsed and pct and 5 < pct < 100:
            try:
                eta = int(elapsed / (pct / 100.0)) - elapsed
                if eta < 0:
                    eta = 0
            except Exception:
                eta = None
        # Sanitize error.
        err = p.get("error")
        if isinstance(err, dict):
            err = _truncate(json.dumps(err), 200)
        elif err is not None:
            err = _truncate(str(err), 200)
        out.append({
            "prompt_id": prompt_id,
            "video_id": f"video_{prompt_id}",
            "status": p.get("status"),
            "percent": pct,
            "step": p.get("step"),
            "total": p.get("total"),
            "phase": p.get("phase"),
            "node": p.get("node"),
            "node_class": p.get("node_class"),
            "error": err,
            "updated_at": int(updated_at) if updated_at else None,
            "submitted_at": int(submitted_at) if submitted_at else None,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "alias": meta.get("alias"),
            "workflow": meta.get("workflow"),
            "size": meta.get("size"),
            "seconds": meta.get("seconds"),
            "key_name": meta.get("key_name"),
            "prompt_truncated": meta.get("prompt_truncated"),
        })
    # Sort: running first by recency, then queued, then terminal.
    def _rank(j):
        s = j.get("status")
        if s == "running":
            return (0, -(j.get("updated_at") or 0))
        if s == "queued":
            return (1, -(j.get("updated_at") or 0))
        return (2, -(j.get("updated_at") or 0))
    out.sort(key=_rank)
    return out


# --- top-level builder ----------------------------------------------------

_SERVICE_STATUS_VALUE = {"up": 1.0, "degraded": 0.5, "down": 0.0}

# Error kind codes stored as a float in metrics_store (no string column).
_ERROR_KIND_NONE = 0.0
_ERROR_KIND_CONNECT_REFUSED = 1.0
_ERROR_KIND_TIMEOUT = 2.0
_ERROR_KIND_HTTP_5XX = 3.0
_ERROR_KIND_OTHER = 4.0
_ERROR_KIND_LABELS = {
    _ERROR_KIND_NONE: None,
    _ERROR_KIND_CONNECT_REFUSED: "connect_refused",
    _ERROR_KIND_TIMEOUT: "timeout",
    _ERROR_KIND_HTTP_5XX: "http_5xx",
    _ERROR_KIND_OTHER: "error",
}


def _classify_backend_error(status: str, last_error: Optional[str]) -> float:
    """Map probe outcome to a numeric error-kind code for time-series storage."""
    if status == "up":
        return _ERROR_KIND_NONE
    err = (last_error or "").lower()
    if "connect" in err or "refused" in err:
        return _ERROR_KIND_CONNECT_REFUSED
    if "timeout" in err:
        return _ERROR_KIND_TIMEOUT
    if err.startswith("http 5") or err.startswith("http 502") or err.startswith("http 503") or err.startswith("http 504"):
        return _ERROR_KIND_HTTP_5XX
    return _ERROR_KIND_OTHER


def _backend_history_tags(backend: Mapping[str, Any]) -> dict[str, str]:
    key = str(backend.get("key") or backend.get("name") or "unknown")
    name = str(backend.get("name") or key)
    role = str(backend.get("role") or "required")
    return {"backend": key, "name": name, "role": role}


def _record_backend_health_samples(backends: Optional[list[dict]]) -> None:
    if not backends:
        return
    try:
        import metrics_store  # local import; metrics_store is in the same package
    except Exception:
        return

    now = int(time.time())
    samples: list[tuple[int, str, str, float]] = []
    for backend in backends:
        if not isinstance(backend, Mapping):
            continue
        status = str(
            backend.get("live_status") or backend.get("status") or "down"
        ).lower()
        status_value = _SERVICE_STATUS_VALUE.get(status, 0.0)
        tags = json.dumps(
            _backend_history_tags(backend), sort_keys=True, separators=(",", ":")
        )
        samples.append((now, "service_up", tags, 1.0 if status == "up" else 0.0))
        samples.append((now, "service_status_code", tags, status_value))
        latency = backend.get("latency_ms")
        if isinstance(latency, (int, float)) and latency >= 0:
            samples.append((now, "service_latency_ms", tags, float(latency)))
        error_kind = _classify_backend_error(status, backend.get("last_error"))
        samples.append((now, "service_error_kind", tags, error_kind))
    if not samples:
        return
    conn = None
    try:
        conn = metrics_store.open_writer()
        metrics_store.record_samples(conn, samples)
        conn.commit()
    except Exception:
        return
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _backend_p95_latency_ms(tags_filter: dict, since_s: int) -> Optional[int]:
    """Compute p95 latency from raw samples for one backend over the given window.

    Returns None when fewer than 2 latency samples exist (no meaningful p95).
    """
    try:
        import metrics_store as _ms
        cutoff = int(time.time()) - since_s
        tags_json = json.dumps(tags_filter, sort_keys=True, separators=(",", ":"))
        with _ms.reader() as c:
            rows = c.execute(
                "SELECT value FROM samples WHERE metric='service_latency_ms' AND ts >= ? AND tags = ? "
                "ORDER BY value",
                (cutoff, tags_json),
            ).fetchall()
        if len(rows) < 2:
            return None
        vals = [r[0] for r in rows]
        p95_idx = max(0, int(len(vals) * 0.95) - 1)
        return int(vals[p95_idx])
    except Exception:
        return None


def _metrics_section(gpus: Optional[list[dict]], disk: Optional[dict], backends: Optional[list[dict]] = None) -> dict:
    """Build the time-series-derived `metrics` block.

    Reads from ``metrics_store`` (SQLite). Each query opens a short-lived
    connection — WAL makes that essentially free. Failures degrade silently;
    the section returns whatever we could compute.
    """
    try:
        import metrics_store  # local import; metrics_store is in the same package
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": _safe_err(e)}

    out: dict[str, Any] = {
        "available": True,
        "gpus": [],
        "cpu": {},
        "disk": {},
        "services": {},
        "trends": {},
    }

    def _agg_fallback(metric: str, since_s: int, op: str, tags: Optional[dict] = None):
        """Try samples_1min first; fall back to raw samples if rollup is empty.

        New install / first 60s of operation: the rollup table is empty but
        the raw table already has data. Without this fallback, every 24h
        widget would render '—' for a full minute on a fresh proxy.
        """
        try:
            v = metrics_store.query_aggregate(
                metric, since_s=since_s, tags_filter=tags, op=op, table="samples_1min"
            )
            if v is not None:
                return v
        except Exception:
            pass
        try:
            return metrics_store.query_aggregate(
                metric, since_s=since_s, tags_filter=tags, op=op
            )
        except Exception:
            return None

    def _series_fallback(metric: str, since_s: int, points: int, op: str = "avg",
                         tags: Optional[dict] = None):
        try:
            s = metrics_store.query_series(
                metric, since_s=since_s, points=points, tags_filter=tags,
                table="samples_1min", op=op,
            )
            if s:
                return s
        except Exception:
            pass
        try:
            return metrics_store.query_series(
                metric, since_s=since_s, points=points, tags_filter=tags, op=op,
            )
        except Exception:
            return []

    # Per-GPU thermal + VRAM-pct windows.
    for g in gpus or []:
        idx = g.get("index")
        if idx is None:
            continue
        gpu_tag = {"gpu": int(idx)}
        gpu_entry = {"index": idx}
        try:
            gpu_entry["temp_max_5min_c"] = metrics_store.query_aggregate(
                "gpu_temp_c", since_s=300, tags_filter=gpu_tag, op="max"
            )
            gpu_entry["temp_max_1h_c"] = metrics_store.query_aggregate(
                "gpu_temp_c", since_s=3600, tags_filter=gpu_tag, op="max"
            )
            gpu_entry["temp_max_24h_c"] = _agg_fallback(
                "gpu_temp_c", since_s=86400, op="max", tags=gpu_tag,
            )
            gpu_entry["vram_pct_max_5min"] = metrics_store.query_aggregate(
                "vram_pct", since_s=300, tags_filter=gpu_tag, op="max"
            )
            gpu_entry["vram_pct_max_1h"] = metrics_store.query_aggregate(
                "vram_pct", since_s=3600, tags_filter=gpu_tag, op="max"
            )
            gpu_entry["vram_pct_max_24h"] = _agg_fallback(
                "vram_pct", since_s=86400, op="max", tags=gpu_tag,
            )
            # Sparkline series for the dashboard (24h, 96 points).
            gpu_entry["vram_pct_24h_series"] = _series_fallback(
                "vram_pct", since_s=86400, points=96, tags=gpu_tag, op="avg",
            )
            gpu_entry["temp_24h_series"] = _series_fallback(
                "gpu_temp_c", since_s=86400, points=96, tags=gpu_tag, op="max",
            )
        except Exception as e:  # noqa: BLE001
            gpu_entry["error"] = _safe_err(e)
        out["gpus"].append(gpu_entry)

    # CPU thermal windows.
    try:
        out["cpu"] = {
            "temp_max_5min_c": metrics_store.query_aggregate("cpu_temp_c", since_s=300, op="max"),
            "temp_max_1h_c": metrics_store.query_aggregate("cpu_temp_c", since_s=3600, op="max"),
            "temp_max_24h_c": _agg_fallback("cpu_temp_c", since_s=86400, op="max"),
            "load_avg_24h_series": _series_fallback("load_avg_1min", since_s=86400, points=96),
        }
    except Exception as e:  # noqa: BLE001
        out["cpu"] = {"error": _safe_err(e)}

    # Disk: change over 24h for root + time-to-full estimate when filling.
    disk_entry: dict[str, Any] = {}
    try:
        # Identify root partition and synth a per-mount block for each known mount.
        partitions = (disk or {}).get("partitions") or []
        for part in partitions:
            tgt = part.get("target")
            if not tgt:
                continue
            tag_filter = {"mount": tgt}
            cur = part.get("avail_bytes")
            avail_24h_ago = _agg_fallback(
                "disk_avail_bytes", since_s=86400, op="min", tags=tag_filter,
            )
            change_b = None
            ttf_hours: Optional[float] = None
            if isinstance(cur, (int, float)) and isinstance(avail_24h_ago, (int, float)):
                # Use min over 24h as a conservative "earliest" baseline.
                change_b = float(cur) - float(avail_24h_ago)
                if change_b < -1024 * 1024:
                    # Filling: linear extrapolation.
                    rate_per_h = abs(change_b) / 24.0
                    if rate_per_h > 0:
                        ttf_hours = float(cur) / rate_per_h
            avail_series = _series_fallback(
                "disk_avail_bytes", since_s=86400, points=96, tags=tag_filter,
            )
            disk_entry[tgt] = {
                "avail_change_24h_bytes": change_b,
                "avail_change_24h_gb": (change_b / (1024 ** 3)) if isinstance(change_b, float) else None,
                "time_to_full_estimate_hours": ttf_hours,
                "series_24h": avail_series,
            }
    except Exception as e:  # noqa: BLE001
        disk_entry = {"error": _safe_err(e)}
    out["disk"] = disk_entry

    # Trends used by the renderer.
    trends: dict[str, Any] = {}
    try:
        trends["xid_109_24h_count"] = _agg_fallback("xid_109_24h", since_s=86400, op="max")
        trends["xid_109_60min_count"] = metrics_store.query_aggregate(
            "xid_109_60min", since_s=3600, op="max",
        )
        # Bar chart: hourly Xid 109 counts over last 24h.
        trends["xid_109_hourly_24h"] = _series_fallback(
            "xid_109_60min", since_s=86400, points=24, op="max",
        )
        # Per-key request rate over last hour.
        # Unique keys come from samples directly.
        try:
            with metrics_store.reader() as rconn:
                rows = rconn.execute(
                    "SELECT DISTINCT tags FROM samples WHERE metric='request' AND ts >= ?",
                    (int(time.time()) - 3600,),
                ).fetchall()
            keys_seen: set[str] = set()
            per_key: dict[str, list[tuple[int, float]]] = {}
            per_key_rpm: dict[str, Optional[float]] = {}
            for (tags_json,) in rows:
                if not tags_json:
                    continue
                try:
                    tag_dict = json.loads(tags_json)
                except Exception:
                    continue
                k = tag_dict.get("key")
                if not k or k in keys_seen:
                    continue
                keys_seen.add(k)
                # rpm over last 5 minutes (mean count per minute).
                count_5min = metrics_store.query_aggregate(
                    "request", since_s=300, tags_filter={"key": k}, op="count",
                )
                per_key_rpm[k] = (count_5min / 5.0) if isinstance(count_5min, (int, float)) else None
                series = metrics_store.query_series(
                    "request", since_s=3600, points=60,
                    tags_filter={"key": k}, op="count",
                )
                per_key[k] = series
            trends["per_key_rpm_5min"] = per_key_rpm
            trends["per_key_request_series_1h"] = per_key
        except Exception as e:  # noqa: BLE001
            trends["per_key_error"] = _safe_err(e)
    except Exception as e:  # noqa: BLE001
        trends["error"] = _safe_err(e)
    out["trends"] = trends

    # Service health history. This is intentionally small product-native
    # operational memory, not a general metrics dashboard.
    services_history: dict[str, Any] = {"window_s": 86400, "backends": {}}
    try:
        for backend in backends or []:
            if not isinstance(backend, Mapping):
                continue
            key = str(backend.get("key") or backend.get("name") or "")
            if not key:
                continue
            tags = _backend_history_tags(backend)
            uptime_24h = _agg_fallback("service_up", since_s=86400, op="avg", tags=tags)
            uptime_7d = _agg_fallback("service_up", since_s=7 * 86400, op="avg", tags=tags)
            sample_count_24h = _agg_fallback(
                "service_status_code", since_s=86400, op="count", tags=tags
            )
            avg_latency_24h = _agg_fallback("service_latency_ms", since_s=86400, op="avg", tags=tags)
            p95_latency_24h = _backend_p95_latency_ms(tags, since_s=86400)
            avg_latency_7d = _agg_fallback("service_latency_ms", since_s=7 * 86400, op="avg", tags=tags)
            p95_latency_7d = _backend_p95_latency_ms(tags, since_s=7 * 86400)
            services_history["backends"][key] = {
                "key": key,
                "name": backend.get("name") or key,
                "role": backend.get("role"),
                "uptime_pct_24h": round(float(uptime_24h) * 100.0, 3)
                if isinstance(uptime_24h, (int, float))
                else None,
                "uptime_pct_7d": round(float(uptime_7d) * 100.0, 3)
                if isinstance(uptime_7d, (int, float))
                else None,
                "sample_count_24h": int(sample_count_24h)
                if isinstance(sample_count_24h, (int, float))
                else 0,
                "avg_latency_ms_24h": int(avg_latency_24h) if isinstance(avg_latency_24h, (int, float)) else None,
                "p95_latency_ms_24h": p95_latency_24h,
                "avg_latency_ms_7d": int(avg_latency_7d) if isinstance(avg_latency_7d, (int, float)) else None,
                "p95_latency_ms_7d": p95_latency_7d,
                "status_24h_series": _series_fallback(
                    "service_status_code", since_s=86400, points=96, tags=tags, op="avg"
                ),
                "latency_ms_24h_series": _series_fallback(
                    "service_latency_ms", since_s=86400, points=96, tags=tags, op="avg"
                ),
            }
    except Exception as e:  # noqa: BLE001
        services_history["error"] = _safe_err(e)
    out["services"] = services_history

    # DB health.
    try:
        out["db_health"] = metrics_store.db_health()
    except Exception as e:  # noqa: BLE001
        out["db_health"] = {"error": _safe_err(e)}

    return out


def _status_auth_key_names(db_path: Path | None) -> tuple[list[str], dict[str, set[str]]]:
    """Read display labels and unambiguous app identities with worker-owned connections."""
    from gateway import admin_store

    db_path = db_path if db_path is not None else admin_store.DB_PATH
    credentials = admin_store.list_active_credentials(subject_kind="app", path=db_path)
    names: set[str] = set()
    app_ids_by_label: dict[str, set[str]] = {}
    for credential in credentials:
        app_id = credential.get("app_id") or credential.get("subject_id")
        app = admin_store.get_app(str(app_id), path=db_path) if app_id else None
        app_name = app.get("name") if app else None
        label = str(app_name or credential.get("display_name") or "").strip()
        if label:
            names.add(label)
            if app_id:
                app_ids_by_label.setdefault(label, set()).add(str(app_id))
    return sorted(names), app_ids_by_label


def _status_config_snapshot(config: dict) -> dict:
    # Configuration snapshot (load-bearing values).
    config_snapshot = {
        "llm": config.get("llm") or {},
        "images": config.get("images") or {},
        "videos_default_workflow": (config.get("videos") or {}).get("default_workflow"),
        "routing": config.get("routing") or {},
        "auth_enabled": (config.get("auth") or {}).get("enabled"),
        "auth_public_paths": (config.get("auth") or {}).get("public_paths"),
        "model_compatibility": config.get("model_compatibility") or {},
        "openai_aliases_file": (config.get("model_compatibility") or {}).get("generated_aliases_file"),
    }
    # Augment with mtime + size of the generated aliases file.
    try:
        aliases_file = Path(__file__).parent / str(config_snapshot.get("openai_aliases_file") or "")
        if aliases_file.is_file():
            st = aliases_file.stat()
            config_snapshot["openai_aliases_file_size_bytes"] = st.st_size
            config_snapshot["openai_aliases_file_mtime"] = int(st.st_mtime)
            try:
                aliases_yaml = yaml.safe_load(aliases_file.read_text(encoding="utf-8")) or {}
                # The generator emits {generated_at, source, models: [...]}.
                # Older variants keyed by category-dict. Handle both.
                if isinstance(aliases_yaml, dict):
                    total = 0
                    sample: list[dict] = []
                    by_category: Counter = Counter()
                    # New shape: {generated_at, source, models: [{id, category, ...}]}
                    models_list = aliases_yaml.get("models")
                    if isinstance(models_list, list):
                        total = len(models_list)
                        for item in models_list:
                            if not isinstance(item, dict):
                                continue
                            by_category[item.get("category") or "unknown"] += 1
                        # Sample first 20 alphabetically by id for the dashboard.
                        sorted_models = sorted(
                            (m for m in models_list if isinstance(m, dict)),
                            key=lambda m: str(m.get("id") or ""),
                        )
                        sample = [
                            {"id": m.get("id"), "category": m.get("category")}
                            for m in sorted_models[:20]
                        ]
                    else:
                        # Legacy shape: top-level keys map to lists/dicts of ids.
                        for v in aliases_yaml.values():
                            if isinstance(v, (list, tuple, set)):
                                total += len(v)
                            elif isinstance(v, dict):
                                total += len(v)
                    config_snapshot["openai_aliases_count"] = total
                    config_snapshot["openai_aliases_by_category"] = dict(by_category)
                    config_snapshot["openai_aliases_sample"] = sample
            except Exception:
                pass
    except Exception:
        pass

    return config_snapshot


async def build_status_payload(config: dict) -> dict:
    errors: dict[str, str] = {}

    async def section(name: str, coro):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            errors[name] = _safe_err(e)
            return None

    def loop_section(name: str, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors[name] = _safe_err(e)
            return None

    async def sync_section(name: str, fn):
        try:
            return await asyncio.to_thread(fn)
        except Exception as e:  # noqa: BLE001
            errors[name] = _safe_err(e)
            return None

    # Hot reload and request telemetry belong to this loop. Workers receive a
    # detached config; the small live-state projections below never await.
    config = copy.deepcopy(config)

    # Startup has already imported the proxy. Avoid importing it here: an
    # isolated status build must not initialize stores or services on the loop.
    candidate = sys.modules.get("__main__")
    proxy_module = (
        candidate if candidate is not None and hasattr(candidate, "_video_progress")
        else sys.modules.get("proxy")
    )

    try:
        firehose_metrics = await asyncio.to_thread(importlib.import_module, "firehose_metrics")
    except Exception:
        firehose_metrics = None  # type: ignore

    # Run independent probes in parallel.
    backends_task = section("backends", _backend_grid(config))
    queue_task = section("comfy_queue", _comfy_queue_and_stats())
    llama_task = section("llama_props_slots", _llama_props_and_slots())
    aux_task = section("aux_health", _enriched_aux_health())

    backends, queue_result, llama_result, aux_health = await asyncio.gather(
        backends_task, queue_task, llama_task, aux_task,
    )
    voices_summary = loop_section("voices_summary", lambda: _voices_summary(proxy_module))
    comfy_queue, comfy_stats = (queue_result or (None, None))
    props, slots = (llama_result or (None, None))

    voxtral_health = (aux_health or {}).get("voxtral") if isinstance(aux_health, dict) else None
    models = await sync_section(
        "models",
        lambda: _build_models(config, props, slots, voxtral_health=voxtral_health),
    )

    gpus_raw = await sync_section("gpus", _gpus_raw)
    try:
        gpus = await _gpu_workloads(gpus_raw, comfy_queue, slots)
    except Exception as e:  # noqa: BLE001
        errors["gpu_workloads"] = _safe_err(e)
        gpus = gpus_raw

    vram_attr = await sync_section("vram_attribution", lambda: _vram_attribution(gpus_raw))
    ram_attr = await sync_section("ram_attribution", _ram_attribution)
    repo_state = await sync_section("repo_state", _repo_state)
    comfy_custom_nodes = await sync_section("comfy_custom_nodes", _comfy_custom_nodes) or []
    capabilities = await sync_section("capabilities", lambda: _capabilities(config)) or []
    image_capabilities = await sync_section("image_capabilities", lambda: _image_capabilities(config)) or []

    driver = {
        **(await sync_section("xids", _journal_xids) or {}),
        "uptime_since": await sync_section("uptime", _uptime_since),
        "kernel_recent": await sync_section("kernel_recent", _kernel_recent_events) or [],
        "reboot_history": await sync_section("reboot_history", _reboot_history) or [],
        "proxy_restarts": await sync_section("proxy_restarts", _proxy_restart_count),
    }

    disk = {
        "partitions": await sync_section("disk_partitions", _disk_partitions) or [],
        "hotspots": [
            await sync_section(f"hotspot_{name}", lambda n=name, p=p: _dir_summary(n, p))
            for name, p in _HOTSPOT_DIRS
        ],
        "journal_disk_usage_bytes": await sync_section("journal_disk", _journal_disk_usage),
    }
    disk["hotspots"] = [h for h in disk["hotspots"] if h]
    # Compute deduped (non-overlapping) hot-spot total. When one path is a
    # subdirectory of another we attribute its bytes only to the deepest
    # ancestor present, so the total reflects unique on-disk bytes.
    try:
        hotspot_paths = [(h.get("path") or "", int(h.get("size_bytes") or 0)) for h in disk["hotspots"] if h.get("exists")]
        non_overlap = 0
        for path, size in hotspot_paths:
            # If another hotspot is a strict prefix (ancestor) of this one, it
            # already accounts for these bytes — skip the child to avoid
            # double-counting.
            is_child = any(
                other_path
                and other_path != path
                and (path == other_path or path.startswith(other_path.rstrip("/") + "/"))
                for other_path, _ in hotspot_paths
            )
            if not is_child:
                non_overlap += size
        disk["hotspots_total_non_overlapping_bytes"] = non_overlap
    except Exception:
        disk["hotspots_total_non_overlapping_bytes"] = None

    thermal = await sync_section("sensors", _read_sensors) or {}
    cpu = await sync_section("cpu_usage", _cpu_usage_quick) or {}

    # Pull video submit metadata + recent events early so recent_videos can
    # join filenames against the prompt_text recorded at submission time. Both
    # are best-effort; failures degrade to "no prompt" rather than blocking
    # the whole panel.
    _video_meta_for_join: dict[str, dict] = {}
    _events_for_join: list[dict] = []
    if firehose_metrics is not None:
        try:
            _video_meta_for_join = await asyncio.to_thread(firehose_metrics.snapshot_video_meta) or {}
        except Exception:
            _video_meta_for_join = {}
        try:
            _events_for_join = await asyncio.to_thread(firehose_metrics.snapshot_recent_events, 200) or []
        except Exception:
            _events_for_join = []
    recent_videos = await sync_section(
        "recent_videos",
        lambda: _recent_renders_videos(
            20,
            video_meta=_video_meta_for_join,
            recent_events=_events_for_join,
        ),
    ) or []
    recent_images = await sync_section("recent_images", lambda: _recent_renders_images(10)) or []

    budgets = {
        "gpu0": None,
        "gpu1": None,
        "images": await sync_section("images_budget", lambda: _images_budget(config)) or {},
        "vram_estimates": await sync_section("vram_estimates", _read_vram_estimates),
    }
    gpu_budget = await sync_section("gpu_budget", _read_gpu_budget)
    if gpu_budget:
        budgets["gpu0"] = gpu_budget.get("gpu0")
        budgets["gpu1"] = gpu_budget.get("gpu1")
        budgets["raw"] = gpu_budget.get("raw")

    # Image cache snapshot.
    image_cache = await sync_section("image_cache", lambda: _dir_summary("image_cache", _IMAGE_CACHE_DIR))

    # Live video jobs: bounded in-process projection only, with no I/O or
    # await while iterating loop-owned progress. Keep it on the loop.
    if proxy_module is not None and firehose_metrics is not None:
        try:
            video_meta = await asyncio.to_thread(firehose_metrics.snapshot_video_meta)
        except Exception as e:
            errors["video_meta"] = _safe_err(e)
            video_meta = {}
    else:
        video_meta = {}
    video_jobs = loop_section(
        "video_jobs",
        lambda: _live_video_jobs(proxy_module, video_meta) if proxy_module else [],
    ) or []

    swap_state = loop_section(
        "swap_state",
        lambda: _swap_state(proxy_module) if proxy_module else {},
    )
    comfy_ws_state = loop_section(
        "comfy_ws_state",
        lambda: _comfy_ws_state(proxy_module) if proxy_module else {},
    )

    # Per-key activity from in-process counters.
    auth_keys: list[dict] = []
    auth_keys_window: dict[str, Any] = {}
    known_names: list[str] = []
    app_ids_by_label: dict[str, set[str]] = {}
    if proxy_module is not None:
        try:
            store = getattr(proxy_module, "ADMIN_STORE", None)
            db_path = getattr(store, "path", None)
            known_names, app_ids_by_label = await asyncio.to_thread(_status_auth_key_names, db_path)
        except Exception:
            known_names = []
            app_ids_by_label = {}
    if firehose_metrics is not None:
        try:
            auth_keys = await asyncio.to_thread(firehose_metrics.snapshot_per_key, known_names)
        except Exception as e:
            errors["auth_keys"] = _safe_err(e)
            auth_keys = []
        try:
            # Tells the renderer how far back the counters actually cover so
            # the section title can be honest after a restart.
            auth_keys_window = await asyncio.to_thread(firehose_metrics.snapshot_window) or {}
        except Exception:
            auth_keys_window = {}
    else:
        auth_keys = [
            {
                "name": n,
                "requests_24h": None,
                "errors_4xx_24h": None,
                "errors_5xx_24h": None,
                "last_seen": None,
                "latency_ms_p50": None,
                "latency_ms_p95": None,
                "top_endpoints": [],
                "active": None,
                "unavailable_reason": "firehose_metrics not loaded",
            }
            for n in known_names
        ]
    _attach_unique_app_ids_to_auth_keys(auth_keys, app_ids_by_label)

    recent_events: list[dict] = []
    if firehose_metrics is not None:
        try:
            recent_events = await asyncio.to_thread(firehose_metrics.snapshot_recent_events, 50)
        except Exception as e:
            errors["recent_events"] = _safe_err(e)
            recent_events = []

    # Aux health enrichment.
    aux_health = aux_health or {}

    # ComfyUI system_stats — VRAM headroom from comfy's perspective.
    comfy_view: Optional[dict] = None
    if comfy_stats:
        try:
            devices = comfy_stats.get("devices") or []
            sysd = comfy_stats.get("system") or {}
            comfy_view = {
                "comfyui_version": sysd.get("comfyui_version"),
                "python_version": sysd.get("python_version"),
                "pytorch_version": sysd.get("pytorch_version"),
                "ram_total_bytes": sysd.get("ram_total"),
                "ram_free_bytes": sysd.get("ram_free"),
                "argv": sysd.get("argv"),
                "device": devices[0] if devices else None,
            }
        except Exception as e:
            errors["comfy_stats"] = _safe_err(e)

    config_snapshot = await sync_section("config_snapshot", lambda: _status_config_snapshot(config)) or {}

    await sync_section("service_health_samples", lambda: _record_backend_health_samples(backends))
    metrics_block = await sync_section(
        "metrics", lambda: _metrics_section(gpus_raw, disk, backends)
    ) or {}

    # Enrich each backend entry with typed history for UI service rows.
    # Sourced from the already-computed metrics.services block — no extra I/O.
    try:
        services_history_map = (metrics_block.get("services") or {}).get("backends") or {}
        if backends and services_history_map:
            enriched_backends: list[dict] = []
            for b in backends:
                key = str(b.get("key") or b.get("name") or "")
                hist = services_history_map.get(key)
                if hist is not None:
                    enriched_backends.append({
                        **b,
                        "history": {
                            "uptime_pct_24h": hist.get("uptime_pct_24h"),
                            "uptime_pct_7d": hist.get("uptime_pct_7d"),
                            "avg_latency_ms_24h": hist.get("avg_latency_ms_24h"),
                            "p95_latency_ms_24h": hist.get("p95_latency_ms_24h"),
                            "avg_latency_ms_7d": hist.get("avg_latency_ms_7d"),
                            "p95_latency_ms_7d": hist.get("p95_latency_ms_7d"),
                            "sample_count_24h": hist.get("sample_count_24h", 0),
                            "status_series": hist.get("status_24h_series", []),
                            "latency_series": hist.get("latency_ms_24h_series", []),
                        },
                    })
                else:
                    enriched_backends.append(b)
            backends = enriched_backends
    except Exception:
        pass  # degrade silently — live status still present without history

    daisy = await sync_section("daisy_audit", _daisy_audit_summary) or {}
    # Augment the daisy summary with two extra signals the audit identified.
    # Each probe runs in a worker and degrades independently on failure.
    pi_ctx = await sync_section("daisy_pi_context", _daisy_pi_context)
    if pi_ctx is not None:
        daisy["pi_context"] = pi_ctx
    bridge = await sync_section("daisy_telegram_bridge", _daisy_telegram_bridge)
    if bridge is not None:
        daisy["telegram_bridge"] = bridge
    drift = await sync_section("workflow_drift", _workflow_drift_status)

    # ------------------------------------------------------------------
    # Banner severity.
    #
    # Two independent tiers, scored separately:
    #   `banner` (primary)        — API health: can /v1/* serve traffic?
    #   `system_health` (secondary) — host health: disk/thermal/audit/etc.
    #
    # An overflowing disk does not break /v1/chat/completions, so it should
    # NOT turn the API banner red. The system-health badge surfaces those
    # concerns without conflating them with API outages.
    # ------------------------------------------------------------------
    sev_rank = {"ok": 0, "warn": 1, "bad": 2}

    def _bump(current: str, new: str) -> str:
        return new if sev_rank.get(new, 0) > sev_rank.get(current, 0) else current

    # --- API banner (primary) ---
    banner = "ok"
    api_reasons: list[tuple[str, str]] = []  # (severity, message)
    if backends:
        any_required_down = any(
            b.get("role") == "required" and b.get("live_status") == "down" for b in backends
        )
        any_required_degraded = any(
            b.get("role") == "required" and b.get("live_status") == "degraded" for b in backends
        )
        any_optional_down = any(
            b.get("role") == "optional" and b.get("live_status") == "down" for b in backends
        )
        if any_required_down:
            banner = _bump(banner, "bad")
            api_reasons.append(("bad", "required backend down"))
        elif any_required_degraded:
            banner = _bump(banner, "warn")
            api_reasons.append(("warn", "required backend degraded"))
        elif any_optional_down:
            banner = _bump(banner, "warn")
            api_reasons.append(("warn", "optional backend down"))
    else:
        banner = _bump(banner, "bad")
        errors.setdefault("backends", "no probe results")
        api_reasons.append(("bad", "no backend probes"))

    # Keep independent API facts separate from the backwards-compatible
    # aggregate banner. Consumers that understand health_sources can retain
    # host/API failures without treating disabled or standby probe noise as a
    # system failure.
    api_health = "ok"
    api_health_reasons: list[tuple[str, str]] = []

    # Xid 109 surge in last 60min affects request reliability — API tier.
    by60 = (driver or {}).get("by_code_60min") or {}
    if by60:
        total_60 = sum(by60.values())
        if total_60 >= 5:
            banner = _bump(banner, "warn")
            reason = f"Xid surge: {total_60} in last 60min"
            api_reasons.append(("warn", reason))
            api_health = _bump(api_health, "warn")
            api_health_reasons.append(("warn", reason))

    # Model swap in progress → transient unavailability for one provider.
    if swap_state and (swap_state.get("tabby_swap_in_progress") or swap_state.get("kobold_swap_in_progress")):
        banner = _bump(banner, "warn")
        reason = "model swap in progress"
        api_reasons.append(("warn", reason))
        api_health = _bump(api_health, "warn")
        api_health_reasons.append(("warn", reason))

    # Sort API reasons by severity (bad first), preserve insertion order
    # within the same severity.
    api_reasons.sort(key=lambda r: -sev_rank.get(r[0], 0))
    banner_reasons: list[str] = [m for _s, m in api_reasons]
    api_health_reasons.sort(key=lambda r: -sev_rank.get(r[0], 0))

    # --- system_health badge (secondary) ---
    system_health = "ok"
    sh_reasons: list[tuple[str, str]] = []

    # Disk pressure (warn 85, bad 95).
    for part in disk.get("partitions") or []:
        pct = part.get("pct_used") or 0
        if pct >= 95:
            system_health = _bump(system_health, "bad")
            sh_reasons.append(("bad", await asyncio.to_thread(_disk_pressure_reason, part, config)))
        elif pct >= 85:
            system_health = _bump(system_health, "warn")
            sh_reasons.append(("warn", await asyncio.to_thread(_disk_pressure_reason, part, config)))

    # Thermal — CPU.
    cpu_state = thermal.get("cpu_thermal_state")
    if cpu_state == "critical":
        system_health = _bump(system_health, "bad")
        sh_reasons.append(("bad", f"CPU temp critical ({thermal.get('cpu_max_c')}C)"))
    elif cpu_state == "warn":
        system_health = _bump(system_health, "warn")
        sh_reasons.append(("warn", f"CPU temp warn ({thermal.get('cpu_max_c')}C)"))

    # Thermal — DIMM.
    dimm_state = thermal.get("dimm_thermal_state")
    if dimm_state == "critical":
        system_health = _bump(system_health, "bad")
        sh_reasons.append(("bad", f"DIMM temp critical ({thermal.get('dimm_max_c')}C)"))
    elif dimm_state == "warn":
        system_health = _bump(system_health, "warn")
        sh_reasons.append(("warn", f"DIMM temp warn ({thermal.get('dimm_max_c')}C)"))

    # Thermal — NVMe.
    nvme_state = thermal.get("nvme_thermal_state")
    if nvme_state == "critical":
        system_health = _bump(system_health, "bad")
        sh_reasons.append(("bad", f"NVMe temp critical ({thermal.get('nvme_max_c')}C)"))
    elif nvme_state == "warn":
        system_health = _bump(system_health, "warn")
        sh_reasons.append(("warn", f"NVMe temp warn ({thermal.get('nvme_max_c')}C)"))

    # Xid 24h history (any code) — ambient hardware-instability signal.
    by24 = (driver or {}).get("by_code_24h") or {}
    if isinstance(by24, dict):
        total_24 = sum(v for v in by24.values() if isinstance(v, int))
        if total_24 >= 1:
            # Recent history without a 60-min surge is just info, surface as warn.
            if not by60 or sum(by60.values()) < 5:
                system_health = _bump(system_health, "warn")
                sh_reasons.append(("warn", f"Xid events in last 24h: {total_24}"))

    # Daisy audit stale → informational, only escalate to warn if very stale.
    try:
        last_mod = (daisy or {}).get("last_modified")
        if last_mod:
            age_s = max(0, int(time.time()) - int(last_mod))
            # >24h of no audit activity → warn.
            if age_s > 86400:
                system_health = _bump(system_health, "warn")
                sh_reasons.append(("warn", f"daisy audit stale ({age_s // 3600}h)"))
    except Exception:
        pass

    sh_reasons.sort(key=lambda r: -sev_rank.get(r[0], 0))
    system_health_reasons: list[str] = [m for _s, m in sh_reasons]

    return {
        "generated_at": int(time.time()),
        "cache_age_s": 0,
        "banner": banner,
        "banner_reasons": banner_reasons,
        "system_health": system_health,
        "system_health_reasons": system_health_reasons,
        "health_sources": {
            "api": {
                "tone": api_health,
                "reasons": [m for _s, m in api_health_reasons],
            },
            "host": {
                "tone": system_health,
                "reasons": system_health_reasons,
            },
        },
        "backends": backends or [],
        "gpus": gpus,
        "vram_attribution": vram_attr,
        "ram_attribution": ram_attr,
        "repo_state": repo_state,
        "comfy_custom_nodes": comfy_custom_nodes,
        "comfy_queue": comfy_queue,
        "comfy_view": comfy_view,
        "comfy_ws_state": comfy_ws_state,
        "swap_state": swap_state,
        "models": models,
        "video_jobs": video_jobs,
        "capabilities": capabilities,
        "image_capabilities": image_capabilities,
        "voices_summary": voices_summary,
        "aux_health": aux_health,
        "recent_videos": recent_videos,
        "recent_images": recent_images,
        # Backwards-compatible alias for the existing Next.js page.
        "recent_renders": recent_videos[:5],
        "driver": driver,
        "disk": disk,
        "thermal": thermal,
        "cpu": cpu,
        "budgets": budgets,
        "image_cache": image_cache,
        "auth_keys": auth_keys,
        "auth_keys_window": auth_keys_window,
        "recent_events": recent_events,
        "daisy": daisy,
        "workflow_drift": drift,
        "config_snapshot": config_snapshot,
        "metrics": metrics_block,
        "errors": errors,
        "commit_hash": _COMMIT_HASH,
    }


def _build_models(
    config: dict,
    props: Optional[dict],
    slots: Optional[list[dict]],
    voxtral_health: Optional[dict] = None,
) -> dict:
    """Synchronous wrapper around the async-friendly _models_section."""
    # The function above is async; we re-implement the sync portion since props/slots
    # already came in. Keep the body identical to _models_section.
    args = _llama_server_args()
    loaded_path = _extract_arg(args, "--model") if args else None
    mmproj = _extract_arg(args, "--mmproj") if args else None
    ctx_size_str = _extract_arg(args, "--ctx-size") if args else None
    cache_k_arg = _extract_arg(args, "--cache-type-k") if args else None
    cache_v_arg = _extract_arg(args, "--cache-type-v") if args else None
    tensor_split_raw = _extract_arg(args, "--tensor-split") if args else None
    parallel_cmdline = _extract_arg(args, "--parallel") if args else None

    try:
        ctx_size = int(ctx_size_str) if ctx_size_str else None
    except ValueError:
        ctx_size = None

    props_total_slots = None
    props_n_ctx = None
    props_model_alias = None
    props_model_path = None
    vision_modality = None
    speculative_type = None
    if isinstance(props, dict):
        props_total_slots = props.get("total_slots")
        props_model_alias = props.get("model_alias")
        props_model_path = props.get("model_path")
        modalities = props.get("modalities") or {}
        if isinstance(modalities, dict):
            vision_modality = modalities.get("vision")
        gs = props.get("default_generation_settings") or {}
        if isinstance(gs, dict):
            props_n_ctx = gs.get("n_ctx")
            params = gs.get("params") or {}
            if isinstance(params, dict):
                speculative_type = params.get("speculative.type")

    slot_details: list[dict] = []
    active_slots = 0
    ctx_used: Optional[int] = None
    if isinstance(slots, list):
        used_vals: list[int] = []
        for s in slots:
            if not isinstance(s, dict):
                continue
            is_processing = bool(s.get("is_processing"))
            n_past = int(s.get("n_past", 0)) if isinstance(s.get("n_past"), (int, float)) else 0
            n_decoded = int(s.get("n_decoded", 0)) if isinstance(s.get("n_decoded"), (int, float)) else 0
            slot_details.append({
                "id": s.get("id"),
                "is_processing": is_processing,
                "n_ctx": s.get("n_ctx"),
                "n_past": n_past,
                "n_decoded": n_decoded,
                "speculative": s.get("speculative"),
                "id_task": s.get("id_task"),
                "prompt_processing_progress": s.get("prompt_processing_progress"),
                "state": "processing" if is_processing else "idle",
            })
            if is_processing:
                active_slots += 1
            used_vals.append(n_past)
        if used_vals:
            ctx_used = max(used_vals)

    videos_cfg = config.get("videos", {})
    aliases = sorted((videos_cfg.get("aliases") or {}).keys())

    image_workflows: list[str] = []
    try:
        comfy_cfg = config.get("backends", {}).get("comfyui", {})
        wf_dir = comfy_cfg.get("workflows_dir")
        if wf_dir:
            p = Path(wf_dir)
            if p.is_dir():
                stems = []
                for f in p.glob("*.json"):
                    name = f.name
                    if name.endswith(".ui.json") or ".obsolete" in name or ".bak" in name:
                        continue
                    stems.append(f.stem)
                image_workflows = sorted(set(stems))[:50]
    except Exception:
        pass

    tts = _resolve_tts_engine(args, voxtral_health)

    loaded_name = props_model_alias or (Path(loaded_path).name if loaded_path else None)
    if vision_modality is not None:
        vision_capable = bool(vision_modality)
    else:
        vision_capable = bool(mmproj)

    total_slots_value = (
        props_total_slots if props_total_slots is not None else len(slot_details)
    )
    parallel = _resolve_parallel_arg(parallel_cmdline, props_total_slots)
    tensor_split_info = _annotate_tensor_split(tensor_split_raw)

    return {
        "loaded_llm_path": props_model_path or loaded_path,
        "loaded_llm_name": loaded_name,
        "mmproj_path": mmproj,
        "vision_capable": vision_capable,
        "vision_source": "props" if vision_modality is not None else ("cmdline" if mmproj else "unknown"),
        "ctx_size": props_n_ctx or ctx_size,
        "ctx_used": ctx_used,
        # Back-compat: keep flat field for any consumers still reading the
        # legacy unit name. Renderer prefers the structured ``tts`` block.
        "active_tts_engine": tts.get("unit"),
        "tts": tts,
        "image_workflows": image_workflows,
        "video_aliases": aliases,
        "total_slots": total_slots_value,
        "active_slots": active_slots,
        "slots": slot_details,
        "speculative_type": speculative_type,
        "cache_type_k": cache_k_arg,
        "cache_type_v": cache_v_arg,
        # ``tensor_split`` retained as raw string for back-compat; structured
        # info (weights/fractions/summary) lives under ``tensor_split_info``.
        "tensor_split": tensor_split_raw,
        "tensor_split_info": tensor_split_info,
        # ``parallel_arg`` is now an effective value (defaults to props.total_slots
        # when --parallel is absent on the cmdline) wrapped with provenance.
        "parallel_arg": parallel.get("value"),
        "parallel_arg_source": parallel.get("source"),
    }


async def _rebuild_cached_status(config: dict) -> dict:
    global _BUILD_TASK
    try:
        data = await build_status_payload(config)
        # A long rebuild must not use up the TTL before it becomes available.
        _CACHE["data"] = data
        _CACHE["ts"] = time.monotonic()
        return data
    finally:
        _BUILD_TASK = None


def _consume_build_exception(task: asyncio.Task) -> None:
    # A disconnected sole caller leaves the shielded build running. Retrieve
    # failures even then; awaiting callers still receive the same exception.
    if not task.cancelled():
        task.exception()


async def get_cached_status(config: dict) -> dict:
    global _BUILD_TASK
    now = time.monotonic()
    if _CACHE["data"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL_S:
        data = dict(_CACHE["data"])
        data["cache_age_s"] = round(now - _CACHE["ts"], 2)
        return data
    if _BUILD_TASK is None:
        _BUILD_TASK = asyncio.create_task(_rebuild_cached_status(config))
        _BUILD_TASK.add_done_callback(_consume_build_exception)
    # One cancelled HTTP request must not cancel every other waiter.
    return await asyncio.shield(_BUILD_TASK)

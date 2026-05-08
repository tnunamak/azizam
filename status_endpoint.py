"""
/v1/status endpoint for the openai-proxy status dashboard ("firehose" build).

Design:
- Every section is wrapped so a failing probe never propagates.
- Each subprocess call has a 1-2s timeout; total cache miss budget < 3s.
- Errors return null sections + a short safe message in `errors{}`.
- Server-side cache (12-15s TTL) keeps the dashboard's 8s polling cheap.

Privacy:
- Never expose the bearer token, raw API keys, or full tracebacks.
- Only key NAMES from AUTH_REGISTRY are surfaced.
- Prompt text is truncated to 200 chars and only shown for in-flight video jobs.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

# Module-level cache (single in-process consumer is fine).
_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL_S = 12.0
_HTTP_TIMEOUT = httpx.Timeout(2.0, connect=1.0)
_SUBPROC_TIMEOUT = 1.5  # seconds per shell probe

# Cached budget read (60s TTL — file rarely changes).
_BUDGET_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_BUDGET_CACHE_TTL_S = 60.0
_GPU_BUDGET_PATH = Path("/home/tnunamak/applications/config/gpu_budget.sh")
_VRAM_ESTIMATES_PATH = Path("/home/tnunamak/applications/openai-proxy/cache/vram_estimates.json")
_DRIFT_REPORT_PATH = Path("/home/tnunamak/applications/openai-proxy/cache/drift_status.json")
_IMAGE_CACHE_DIR = Path("/home/tnunamak/applications/openai-proxy/cache/images")
_DAISY_AUDIT_PATH = Path("/home/tnunamak/applications/daisy-audit/audit.jsonl")

# Hot-spot directories the firehose should surface separately from the root partition.
_HOTSPOT_DIRS = [
    ("comfy_output_root", Path("/home/tnunamak/applications/ComfyUI/output")),
    ("comfy_output_video", Path("/home/tnunamak/applications/ComfyUI/output/video")),
    ("openai_image_cache", Path("/home/tnunamak/applications/openai-proxy/cache/images")),
    ("daisy_audit", Path("/home/tnunamak/applications/daisy-audit")),
]

# Service units we want to attribute VRAM to. Anything outside this list is
# bucketed into "desktop" (Plasma/Xorg/Chromium/etc.).
_SERVICE_UNITS = [
    "comfyui",
    "llama-turbo",
    "voxtral-int4-tts",
    "voxtral-tts",
    "parakeet-stt",
    "tabbyapi",
    "openai-proxy",
    "daisy",
    "sunshine",
]


# --- subprocess helpers (1-2s budgets, never raise) ----------------------

def _run(cmd: list[str], timeout: float = _SUBPROC_TIMEOUT) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
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


# --- backend probes -------------------------------------------------------

_ROLE_ORDER = {"required": 0, "optional": 1, "standby": 2}
_VALID_ROLES = {"required", "optional", "standby"}


async def _probe(
    client: httpx.AsyncClient,
    name: str,
    url: str,
    role: str,
    note: Optional[str],
) -> dict[str, Any]:
    base = {"name": name, "url": url, "role": role, "note": note}
    t0 = time.monotonic()
    try:
        r = await client.get(url)
        dt = int((time.monotonic() - t0) * 1000)
        if 200 <= r.status_code < 300:
            return {**base, "status": "up", "live_status": "up", "latency_ms": dt, "last_error": None}
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
        return {**base, "status": "down", "live_status": "down", "latency_ms": None, "last_error": "connect refused"}
    except httpx.TimeoutException:
        return {**base, "status": "degraded", "live_status": "degraded", "latency_ms": None, "last_error": "timeout"}
    except Exception as e:  # noqa: BLE001
        return {**base, "status": "down", "live_status": "down", "latency_ms": None, "last_error": _safe_err(e)}


def _resolve_backend_targets(config: dict) -> list[dict[str, Any]]:
    backends = config.get("backends", {}) or {}
    targets: list[dict[str, Any]] = []
    for key, cfg in backends.items():
        if not isinstance(cfg, dict):
            continue
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port")
        if port is None:
            continue
        path = cfg.get("health_endpoint", "/health")
        url = f"http://{host}:{port}{path}"
        name = cfg.get("name") or key
        role = cfg.get("role", "required")
        if role not in _VALID_ROLES:
            role = "required"
        note = cfg.get("note")
        targets.append({"key": key, "name": name, "url": url, "role": role, "note": note})
    targets.sort(key=lambda t: _ROLE_ORDER.get(t["role"], 99))
    return targets


async def _backend_grid(config: dict) -> list[dict]:
    targets = _resolve_backend_targets(config)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        results = await asyncio.gather(
            *[_probe(client, t["name"], t["url"], t["role"], t["note"]) for t in targets],
            return_exceptions=False,
        )
    return list(results)


# --- llama.cpp /props + /slots -------------------------------------------

async def _llama_props_and_slots() -> tuple[Optional[dict], Optional[list[dict]]]:
    """Fetch /props and /slots in parallel. Returns (props|None, slots|None)."""
    async def _get(client, url):
        try:
            r = await client.get(url)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            props, slots = await asyncio.gather(
                _get(client, "http://127.0.0.1:5051/props"),
                _get(client, "http://127.0.0.1:5051/slots"),
            )
            if not isinstance(slots, list):
                slots = None
            return props, slots
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
    """nvidia-smi snapshot incl. temp, power, clocks, pcie, throttle."""
    rc, out, _err = _run([
        "nvidia-smi",
        "--query-gpu="
        "index,uuid,name,memory.used,memory.total,utilization.gpu,"
        "temperature.gpu,power.draw,power.limit,clocks.gr,clocks.mem,"
        "fan.speed,clocks_throttle_reasons.active,"
        "pcie.link.gen.current,pcie.link.width.current",
        "--format=csv,noheader,nounits",
    ])
    if rc != 0 or not out.strip():
        return None
    gpus: list[dict] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 15:
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
                "clocks_gr_mhz": _safe_int(parts[9]),
                "clocks_mem_mhz": _safe_int(parts[10]),
                "fan_speed_pct": _safe_int(parts[11]),
                "throttle_reasons_hex": parts[12],
                "pcie_link_gen": _safe_int(parts[13]),
                "pcie_link_width": _safe_int(parts[14]),
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


def _vram_attribution(gpus: Optional[list[dict]]) -> dict:
    """Per-GPU per-service VRAM usage with budget comparison."""
    if not gpus:
        return {"per_gpu": [], "errors": ["gpus unavailable"]}
    apps = _get_compute_apps()
    pid_to_service = {}
    svc_pids = _service_pids()
    for svc, pids in svc_pids.items():
        for pid in pids:
            pid_to_service[pid] = svc

    # Build uuid -> index map.
    uuid_to_index = {g.get("uuid"): g.get("index") for g in gpus}
    # gpu_index -> {service: mb}
    per_gpu_svc: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    pids_seen: set[int] = set()
    for app in apps:
        idx = uuid_to_index.get(app["gpu_uuid"])
        if idx is None:
            continue
        svc = pid_to_service.get(app["pid"], "desktop")
        per_gpu_svc[idx][svc] += app["used_mb"]
        pids_seen.add(app["pid"])

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
            }
            return mapping.get(svc)
        if idx == 1:
            mapping = {"llama-turbo": raw.get("GPU1_LLM")}
            return mapping.get(svc)
        return None

    out_gpus: list[dict] = []
    for g in gpus:
        idx = g.get("index")
        svc_map = per_gpu_svc.get(idx, {})
        services = []
        for svc, mb in sorted(svc_map.items(), key=lambda x: -x[1]):
            budget_gb = budget_for(idx, svc)
            used_gb = mb / 1024.0
            over_budget = (
                budget_gb is not None and used_gb > float(budget_gb) + 0.5
            )
            pct_of_budget = (
                (used_gb / float(budget_gb) * 100.0) if (budget_gb and budget_gb > 0) else None
            )
            services.append({
                "service": svc,
                "used_mb": mb,
                "used_gb": round(used_gb, 2),
                "budget_gb": budget_gb,
                "pct_of_budget": round(pct_of_budget, 1) if pct_of_budget is not None else None,
                "over_budget": bool(over_budget),
            })
        out_gpus.append({
            "index": idx,
            "uuid": g.get("uuid"),
            "memory_used_mb": g.get("memory_used_mb"),
            "memory_total_mb": g.get("memory_total_mb"),
            "services": services,
        })
    return {
        "per_gpu": out_gpus,
        "raw_compute_apps": apps,
        "service_pids": {k: sorted(v) for k, v in svc_pids.items()},
    }


# --- budgets --------------------------------------------------------------

def _read_gpu_budget() -> Optional[dict]:
    now = time.monotonic()
    cached = _BUDGET_CACHE.get("data")
    if cached is not None and (now - _BUDGET_CACHE.get("ts", 0.0)) < _BUDGET_CACHE_TTL_S:
        return cached
    try:
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
    try:
        if not _VRAM_ESTIMATES_PATH.is_file():
            return None
        raw = json.loads(_VRAM_ESTIMATES_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        st = _VRAM_ESTIMATES_PATH.stat()
        entries = []
        for k, v in raw.items():
            if not isinstance(k, str) or not isinstance(v, (int, float)):
                continue
            entries.append({"key": k, "peak_bytes": int(v)})
        entries.sort(key=lambda e: e["peak_bytes"], reverse=True)
        return {
            "last_modified": int(st.st_mtime),
            "entries": entries,  # full list; design layer can crop.
            "total_entries": len(entries),
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
            supports_i2v = False
            supports_audio = False
            two_stage = False
            class_types: Counter = Counter()
            if wf_path.is_file():
                try:
                    wf_json = json.loads(wf_path.read_text(encoding="utf-8"))
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
            workflow_caps[wf_name] = {
                "supports_i2v": supports_i2v,
                "supports_audio": supports_audio,
                "two_stage": two_stage,
                "class_types": dict(class_types.most_common(10)),
            }
        except Exception:
            workflow_caps[wf_name] = {
                "supports_i2v": False, "supports_audio": False, "two_stage": False, "class_types": {},
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
            "class_types": caps.get("class_types") or {},
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
            if name.endswith(".ui.json"):
                continue
            if ".obsolete" in name or ".bak" in name:
                continue
            try:
                wf = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                wf = {}
            supports_edit = "-edit" in f.stem
            class_types: Counter = Counter()
            if isinstance(wf, dict):
                for v in wf.values():
                    if isinstance(v, dict) and v.get("class_type"):
                        class_types[str(v["class_type"])] += 1
            out.append({
                "name": f.stem,
                "supports_edit": supports_edit,
                "class_type_count": sum(class_types.values()),
                "top_class_types": dict(class_types.most_common(5)),
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


def _kernel_recent_events(n: int = 5) -> list[str]:
    rc, out, _ = _run(
        ["journalctl", "-k", "-n", "200", "--no-pager", "--since", "24 hours ago"],
        timeout=2.0,
    )
    if rc != 0 or not out:
        return []
    keep = []
    for line in out.splitlines():
        l = line.lower()
        if any(s in l for s in ("nvidia", "nvrm", "xid", "gpu", "amdgpu", "iommu", "mce")):
            keep.append(line[:200])
    return keep[-n:]


def _reboot_history(n: int = 5) -> list[str]:
    rc, out, _ = _run(["last", "-x", "-n", str(n + 5), "reboot"], timeout=1.0)
    if rc != 0 or not out:
        return []
    rows = []
    for line in out.splitlines():
        if line.lower().startswith("reboot"):
            rows.append(line[:160])
        if len(rows) >= n:
            break
    return rows


def _proxy_restart_count() -> Optional[int]:
    rc, out, _ = _run(
        ["systemctl", "--user", "show", "openai-proxy.service", "-p", "NRestarts"],
        timeout=1.0,
    )
    if rc != 0:
        return None
    if "=" in out:
        try:
            return int(out.strip().split("=", 1)[1])
        except Exception:
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

def _disk_partitions() -> list[dict]:
    """df for root, /home, /media/windows, /var/log."""
    out_rows: list[dict] = []
    for target in ("/", "/home", "/media/windows", "/var/log"):
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
        out_rows.append({
            "source": parts[0],
            "target": parts[5],
            "size_bytes": size,
            "used_bytes": used,
            "avail_bytes": avail,
            "pct_used": round((used / size) * 100.0, 1) if size > 0 else 0.0,
        })
    # Dedup if root and /var/log are same partition.
    seen_targets = set()
    deduped = []
    for r in out_rows:
        if r["source"] in seen_targets and r["target"] != "/":
            continue
        deduped.append(r)
        seen_targets.add(r["source"])
    return out_rows


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

    return {
        "cpu_temps": cpu_temps,
        "cpu_max_c": cpu_max,
        "cpu_thermal_state": _thermal_state(cpu_max, warn=85, crit=95),
        "dimm_temps": dimm_temps,
        "dimm_max_c": dimm_max,
        "dimm_thermal_state": _thermal_state(dimm_max, warn=70, crit=85),
        "nvme_max_c": nvme_temp,
        "amdgpu_edge_c": amdgpu_edge_temp,
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
    """Top-N processes by CPU/memory plus a load summary. ~200ms."""
    rc, out, _ = _run(["top", "-b", "-n", "1", "-w", "200"], timeout=1.0)
    info: dict[str, Any] = {"top_procs": [], "load": _load_average()}
    if rc != 0 or not out:
        return info
    lines = out.splitlines()
    # Skip the top header until the column header line, then take next 8 rows.
    in_table = False
    procs: list[dict] = []
    for line in lines:
        if line.strip().startswith("PID"):
            in_table = True
            continue
        if in_table:
            parts = line.split()
            if len(parts) < 12:
                continue
            try:
                procs.append({
                    "pid": int(parts[0]),
                    "user": parts[1],
                    "cpu_pct": float(parts[8]),
                    "mem_pct": float(parts[9]),
                    "command": " ".join(parts[11:])[:120],
                })
            except (ValueError, IndexError):
                continue
            if len(procs) >= 12:
                break
    info["top_procs"] = procs
    return info


# --- recent renders -------------------------------------------------------

_VIDEO_DIR = Path("/home/tnunamak/applications/ComfyUI/output/video")
_IMAGE_DIR = Path("/home/tnunamak/applications/ComfyUI/output")


def _recent_renders_videos(n: int = 20) -> list[dict]:
    if not _VIDEO_DIR.is_dir():
        return []
    try:
        files = [f for f in _VIDEO_DIR.iterdir() if f.suffix == ".mp4" and f.is_file()]
    except Exception:
        return []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    out: list[dict] = []
    for f in files[:n]:
        try:
            st = f.stat()
        except OSError:
            continue
        m = re.match(r"^([A-Za-z0-9.\-]+?)_\d{4,}_", f.name)
        alias = m.group(1) if m else None
        out.append({
            "filename": f.name,
            "path": str(f),
            "size_bytes": st.st_size,
            "mtime": int(st.st_mtime),
            "alias": alias,
        })
    return out


def _recent_renders_images(n: int = 10) -> list[dict]:
    if not _IMAGE_DIR.is_dir():
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
        out.append({
            "filename": f.name,
            "path": str(f),
            "size_bytes": st.st_size,
            "mtime": int(st.st_mtime),
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


async def _voices_summary() -> Optional[dict]:
    """Summary of /v1/audio/voices: count + language coverage."""
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get("http://127.0.0.1:5000/v1/audio/voices")
            if r.status_code != 200:
                return None
            j = r.json()
            voices = j.get("voices") or []
            langs: Counter = Counter()
            genders: Counter = Counter()
            for v in voices:
                if isinstance(v, dict):
                    langs[v.get("language") or "unknown"] += 1
                    genders[v.get("gender") or "unknown"] += 1
            return {
                "count": len(voices),
                "languages": dict(langs),
                "genders": dict(genders),
            }
    except Exception:
        return None


# --- daisy ---------------------------------------------------------------

def _daisy_audit_summary() -> dict:
    """Read the last few audit entries from daisy's audit log."""
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
            recent.append({
                "ts": rec.get("timestamp") or rec.get("ts"),
                "kind": rec.get("kind") or rec.get("type"),
                "summary": _truncate(str(rec.get("summary") or rec.get("prompt") or rec.get("action") or ""), 160),
            })
        info["recent_entries"] = recent
    except Exception as e:
        info["error"] = _safe_err(e)
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

async def build_status_payload(config: dict) -> dict:
    errors: dict[str, str] = {}

    async def section(name: str, coro):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            errors[name] = _safe_err(e)
            return None

    def sync_section(name: str, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors[name] = _safe_err(e)
            return None

    # Lazy import to avoid circular dep at module load.
    try:
        import proxy as proxy_module  # noqa: WPS433
    except Exception:
        proxy_module = None  # type: ignore

    try:
        import firehose_metrics
    except Exception:
        firehose_metrics = None  # type: ignore

    # Run independent probes in parallel.
    backends_task = section("backends", _backend_grid(config))
    queue_task = section("comfy_queue", _comfy_queue_and_stats())
    llama_task = section("llama_props_slots", _llama_props_and_slots())
    aux_task = section("aux_health", _enriched_aux_health())
    voices_task = section("voices_summary", _voices_summary())

    backends, queue_result, llama_result, aux_health, voices_summary = await asyncio.gather(
        backends_task, queue_task, llama_task, aux_task, voices_task,
    )
    comfy_queue, comfy_stats = (queue_result or (None, None))
    props, slots = (llama_result or (None, None))

    models = sync_section(
        "models",
        lambda: _build_models(config, props, slots),
    )

    gpus_raw = sync_section("gpus", _gpus_raw)
    try:
        gpus = await _gpu_workloads(gpus_raw, comfy_queue, slots)
    except Exception as e:  # noqa: BLE001
        errors["gpu_workloads"] = _safe_err(e)
        gpus = gpus_raw

    vram_attr = sync_section("vram_attribution", lambda: _vram_attribution(gpus_raw))
    capabilities = sync_section("capabilities", lambda: _capabilities(config)) or []
    image_capabilities = sync_section("image_capabilities", lambda: _image_capabilities(config)) or []

    driver = {
        **(sync_section("xids", _journal_xids) or {}),
        "uptime_since": sync_section("uptime", _uptime_since),
        "kernel_recent": sync_section("kernel_recent", _kernel_recent_events) or [],
        "reboot_history": sync_section("reboot_history", _reboot_history) or [],
        "proxy_restarts": sync_section("proxy_restarts", _proxy_restart_count),
    }

    disk = {
        "partitions": sync_section("disk_partitions", _disk_partitions) or [],
        "hotspots": [
            sync_section(f"hotspot_{name}", lambda n=name, p=p: _dir_summary(n, p))
            for name, p in _HOTSPOT_DIRS
        ],
        "journal_disk_usage_bytes": sync_section("journal_disk", _journal_disk_usage),
    }
    disk["hotspots"] = [h for h in disk["hotspots"] if h]

    thermal = sync_section("sensors", _read_sensors) or {}
    cpu = sync_section("cpu_usage", _cpu_usage_quick) or {}

    recent_videos = sync_section("recent_videos", lambda: _recent_renders_videos(20)) or []
    recent_images = sync_section("recent_images", lambda: _recent_renders_images(10)) or []

    budgets = {
        "gpu0": None,
        "gpu1": None,
        "images": sync_section("images_budget", lambda: _images_budget(config)) or {},
        "vram_estimates": sync_section("vram_estimates", _read_vram_estimates),
    }
    gpu_budget = sync_section("gpu_budget", _read_gpu_budget)
    if gpu_budget:
        budgets["gpu0"] = gpu_budget.get("gpu0")
        budgets["gpu1"] = gpu_budget.get("gpu1")
        budgets["raw"] = gpu_budget.get("raw")

    # Image cache snapshot.
    image_cache = sync_section("image_cache", lambda: _dir_summary("image_cache", _IMAGE_CACHE_DIR))

    # Live video jobs (in-process state).
    if proxy_module is not None and firehose_metrics is not None:
        try:
            video_meta = firehose_metrics.snapshot_video_meta()
        except Exception as e:
            errors["video_meta"] = _safe_err(e)
            video_meta = {}
    else:
        video_meta = {}
    video_jobs = sync_section(
        "video_jobs",
        lambda: _live_video_jobs(proxy_module, video_meta) if proxy_module else [],
    ) or []

    swap_state = sync_section(
        "swap_state",
        lambda: _swap_state(proxy_module) if proxy_module else {},
    )
    comfy_ws_state = sync_section(
        "comfy_ws_state",
        lambda: _comfy_ws_state(proxy_module) if proxy_module else {},
    )

    # Per-key activity from in-process counters.
    auth_keys: list[dict] = []
    known_names: list[str] = []
    if proxy_module is not None:
        try:
            registry = getattr(proxy_module, "AUTH_REGISTRY", {}) or {}
            known_names = sorted({getattr(v, "name", None) for v in registry.values() if getattr(v, "name", None)})
        except Exception:
            known_names = []
    if firehose_metrics is not None:
        try:
            auth_keys = firehose_metrics.snapshot_per_key(known_names)
        except Exception as e:
            errors["auth_keys"] = _safe_err(e)
            auth_keys = []
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

    recent_events: list[dict] = []
    if firehose_metrics is not None:
        try:
            recent_events = firehose_metrics.snapshot_recent_events(50)
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
                if isinstance(aliases_yaml, dict):
                    config_snapshot["openai_aliases_count"] = sum(
                        len(v) if isinstance(v, dict) else 0 for v in aliases_yaml.values()
                    )
            except Exception:
                pass
    except Exception:
        pass

    daisy = sync_section("daisy_audit", _daisy_audit_summary)
    drift = sync_section("workflow_drift", _workflow_drift_status)

    # Banner severity.
    banner = "ok"
    banner_reasons: list[str] = []
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
            banner = "bad"
            banner_reasons.append("required backend down")
        elif any_required_degraded or any_optional_down:
            banner = "warn"
            banner_reasons.append("backend degraded or optional down")
    else:
        banner = "bad"
        errors.setdefault("backends", "no probe results")
        banner_reasons.append("no backend probes")

    # Disk pressure → banner escalation.
    for part in disk.get("partitions") or []:
        pct = part.get("pct_used") or 0
        if pct >= 95 and banner != "bad":
            banner = "bad"
            banner_reasons.append(f"disk {part['target']} {pct}% full")
        elif pct >= 85 and banner == "ok":
            banner = "warn"
            banner_reasons.append(f"disk {part['target']} {pct}% full")

    # Thermal escalation.
    if thermal.get("cpu_thermal_state") == "critical" and banner != "bad":
        banner = "bad"
        banner_reasons.append(f"CPU temp critical ({thermal.get('cpu_max_c')}C)")
    elif thermal.get("cpu_thermal_state") == "warn" and banner == "ok":
        banner = "warn"
        banner_reasons.append(f"CPU temp warn ({thermal.get('cpu_max_c')}C)")

    # Xid surge.
    by60 = (driver or {}).get("by_code_60min") or {}
    if by60:
        total_60 = sum(by60.values())
        if total_60 >= 5 and banner != "bad":
            banner = "warn" if banner == "ok" else banner
            banner_reasons.append(f"Xid surge: {total_60} in last 60min")

    # Swap-in-progress note.
    if swap_state and (swap_state.get("tabby_swap_in_progress") or swap_state.get("kobold_swap_in_progress")):
        if banner == "ok":
            banner = "warn"
        banner_reasons.append("model swap in progress")

    return {
        "generated_at": int(time.time()),
        "cache_age_s": 0,
        "banner": banner,
        "banner_reasons": banner_reasons,
        "backends": backends or [],
        "gpus": gpus,
        "vram_attribution": vram_attr,
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
        "recent_events": recent_events,
        "daisy": daisy,
        "workflow_drift": drift,
        "config_snapshot": config_snapshot,
        "errors": errors,
    }


def _build_models(config: dict, props: Optional[dict], slots: Optional[list[dict]]) -> dict:
    """Synchronous wrapper around the async-friendly _models_section."""
    # The function above is async; we re-implement the sync portion since props/slots
    # already came in. Keep the body identical to _models_section.
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

    tts_engine: Optional[str] = None
    rc, out, _ = _run(["systemctl", "--user", "is-active", "voxtral-int4-tts.service"])
    if rc == 0 and out.strip() == "active":
        tts_engine = "voxtral-int4-tts"
    else:
        for unit in ("voxtral-tts.service", "tts.service"):
            rc2, out2, _ = _run(["systemctl", "--user", "is-active", unit])
            if rc2 == 0 and out2.strip() == "active":
                tts_engine = unit.removesuffix(".service")
                break

    loaded_name = props_model_alias or (Path(loaded_path).name if loaded_path else None)
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


async def get_cached_status(config: dict) -> dict:
    now = time.monotonic()
    if _CACHE["data"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL_S:
        data = dict(_CACHE["data"])
        data["cache_age_s"] = round(now - _CACHE["ts"], 2)
        return data
    data = await build_status_payload(config)
    _CACHE["data"] = data
    _CACHE["ts"] = now
    return data

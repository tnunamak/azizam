"""Async sampler that writes time-series snapshots into metrics.db.

One asyncio task. Fast tier every 10s, slow tier piggybacked every 60s.
Every cycle is wrapped in try/except so the loop never dies.

Reuses probe helpers from ``status_endpoint`` to avoid duplicating logic.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from typing import Any, Callable, Mapping, Optional

import metrics_store
import status_endpoint  # reuse existing probes
from gateway.metrics_writer import close_writer, get_writer, start_writer


# When the disk holding metrics.db falls below this we pause writes and warn.
_DISK_PAUSE_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB
_DISK_PAUSE_WARN_INTERVAL_S = 600  # 10 min


_state = {
    "paused_for_disk": False,
    "last_disk_warn_ts": 0,
    "last_minute_rolled": 0,
    "last_hour_rolled": 0,
    "last_prune_day": 0,
    "request_writes": 0,
    # NVKMS failure lines seen in the last journal window; None until the first tick.
    "nvkms_seen": None,
}

# The NVIDIA driver logs "Failed to allocate NVKMS memory for GEM object" when the
# desktop compositor cannot get GPU memory. Each tick re-reads this much journal and
# counts lines not seen on the previous tick, so late-arriving entries are not lost.
_NVKMS_WINDOW_S = 60
_NVKMS_GPU_ID_RE = re.compile(r"\[GPU ID (0x[0-9a-fA-F]+)\]")


def _nvkms_failure_lines() -> Optional[Counter]:
    """Kernel-journal NVKMS error lines of the last window; None if unreadable."""
    rc, out, _ = status_endpoint._run(
        ["journalctl", "-k", "--no-pager", "-o", "short-unix", "--since", f"@{int(time.time()) - _NVKMS_WINDOW_S}"],
        timeout=2.0,
    )
    if rc != 0:
        return None
    return Counter(
        line for line in out.splitlines()
        if "nvkms" in line.lower() and ("error" in line.lower() or "fail" in line.lower())
    )


def _new_nvkms_failures() -> Optional[Counter]:
    """NVKMS failure lines not seen on the previous call; None if the journal is unreadable.

    The first successful call only sets the baseline, so history is not counted.
    """
    current = _nvkms_failure_lines()
    if current is None:
        return None
    seen, _state["nvkms_seen"] = _state["nvkms_seen"], current
    return Counter() if seen is None else current - seen


def _gpu_index_by_drm_id() -> dict[int, int]:
    """nvidia-drm "GPU ID" (PCI bus << 8 | slot, domain 0 only) -> nvidia-smi index.

    Checked on this host: GPU ID 0x00000100 is the card at 01:00.0 (index 0).
    """
    rc, out, _ = status_endpoint._run(["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"])
    ids: dict[int, int] = {}
    for line in out.splitlines() if rc == 0 else []:
        idx, _, bus_id = (p.strip() for p in line.partition(","))
        m = re.fullmatch(r"0*:([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.\d", bus_id)
        if idx.isdigit() and m:
            ids[(int(m.group(1), 16) << 8) | int(m.group(2), 16)] = int(idx)
    return ids


def _emit_fast(writer, now: int) -> None:
    """GPU/CPU/RAM samples + ComfyUI queue + llama slots. Synchronous probes."""
    samples: list[tuple] = []

    # GPUs.
    gpus: list[dict] = []
    try:
        gpus = status_endpoint._gpus_raw() or []
        for g in gpus:
            idx = g.get("index")
            if idx is None:
                continue
            tags_json = f'{{"gpu":{idx}}}'
            mem_used = g.get("memory_used_mb")
            mem_total = g.get("memory_total_mb")
            if isinstance(mem_used, (int, float)):
                samples.append((now, "vram_used_mb", tags_json, float(mem_used)))
            if isinstance(mem_total, (int, float)) and isinstance(mem_used, (int, float)) and mem_total:
                samples.append((now, "vram_pct", tags_json, float(mem_used) / float(mem_total) * 100.0))
            if isinstance(mem_total, (int, float)) and isinstance(mem_used, (int, float)):
                samples.append((now, "vram_free_mb", tags_json, float(mem_total) - float(mem_used)))
            for metric_key, gpu_key in (
                ("gpu_util_pct", "instantaneous_util_pct"),
                ("gpu_temp_c", "temp_c"),
                ("gpu_power_w", "power_draw_w"),
                ("gpu_clock_gr_mhz", "clocks_gr_mhz"),
                ("gpu_clock_mem_mhz", "clocks_mem_mhz"),
                ("gpu_fan_pct", "fan_speed_pct"),
            ):
                v = g.get(gpu_key)
                if isinstance(v, (int, float)):
                    samples.append((now, metric_key, tags_json, float(v)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] gpu probe failed: {e}", flush=True)

    # Desktop (NVKMS) allocation failures this tick, per GPU. Absent when the journal
    # is unreadable: a missing row means "not measured", a 0 means "none happened".
    try:
        new = _new_nvkms_failures()
        if new is not None:
            gpu_for_id = _gpu_index_by_drm_id() if new else {}
            per_gpu: Counter = Counter({g["index"]: 0 for g in gpus if g.get("index") is not None})
            for line, n in new.items():
                m = _NVKMS_GPU_ID_RE.search(line)
                per_gpu[gpu_for_id.get(int(m.group(1), 16)) if m else None] += n
            for idx, n in per_gpu.items():
                tags = "" if idx is None else f'{{"gpu":{int(idx)}}}'
                samples.append((now, "gpu_desktop_alloc_failures", tags, float(n)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] nvkms probe failed: {e}", flush=True)

    # CPU thermal + load.
    try:
        sensors = status_endpoint._read_sensors() or {}
        cpu_max = sensors.get("cpu_max_c")
        if isinstance(cpu_max, (int, float)):
            samples.append((now, "cpu_temp_c", "", float(cpu_max)))
        nvme = sensors.get("nvme_max_c")
        if isinstance(nvme, (int, float)):
            samples.append((now, "nvme_temp_c", "", float(nvme)))
        dimm = sensors.get("dimm_max_c")
        if isinstance(dimm, (int, float)):
            samples.append((now, "dimm_temp_c", "", float(dimm)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] sensors probe failed: {e}", flush=True)

    try:
        load = status_endpoint._load_average()
        if load and len(load) >= 1 and isinstance(load[0], (int, float)):
            samples.append((now, "load_avg_1min", "", float(load[0])))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] load probe failed: {e}", flush=True)

    # RAM (free + available).
    try:
        ram = status_endpoint._ram_attribution() or {}
        for metric_key, k in (
            ("ram_total_kb", "total_kb"),
            ("ram_free_kb", "free_kb"),
            ("ram_avail_kb", "available_kb"),
        ):
            v = ram.get(k)
            if isinstance(v, (int, float)):
                samples.append((now, metric_key, "", float(v)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] ram probe failed: {e}", flush=True)

    if samples:
        try:
            writer.record_samples(samples)
        except Exception as e:  # noqa: BLE001
            print(f"[metrics_sampler] record_samples (fast) failed: {e}", flush=True)


async def _emit_fast_async(writer, now: int) -> None:
    """ComfyUI queue depth (async) + llama slots."""
    try:
        queue, _stats = await status_endpoint._comfy_queue_and_stats()
        if isinstance(queue, dict):
            running = int(queue.get("running") or 0)
            pending = int(queue.get("pending") or 0)
            writer.record_sample(now, "comfy_queue_running", float(running))
            writer.record_sample(now, "comfy_queue_pending", float(pending))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] comfy queue probe failed: {e}", flush=True)

    try:
        _props, slots = await status_endpoint._llama_props_and_slots()
        if isinstance(slots, list):
            active = sum(1 for s in slots if isinstance(s, dict) and s.get("is_processing"))
            writer.record_sample(now, "llama_slots_active", float(active))
            writer.record_sample(now, "llama_slots_total", float(len(slots)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] llama slots probe failed: {e}", flush=True)


def _emit_slow(writer, now: int, config: Optional[Mapping[str, Any]] = None) -> None:
    """Disk avail per partition + per-service RSS + per-GPU per-service VRAM.

    `config` supplies the backends that services are attributed to.
    """
    samples: list[tuple] = []

    # Disk per partition.
    try:
        parts = status_endpoint._disk_partitions() or []
        for part in parts:
            tgt = part.get("target") or "?"
            tags = f'{{"mount":{_json_str(tgt)}}}'
            avail = part.get("avail_bytes")
            if isinstance(avail, (int, float)):
                samples.append((now, "disk_avail_bytes", tags, float(avail)))
            pct = part.get("pct_used")
            if isinstance(pct, (int, float)):
                samples.append((now, "disk_pct_used", tags, float(pct)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] disk probe failed: {e}", flush=True)

    # Per-service RSS.
    try:
        ram = status_endpoint._ram_attribution(config) or {}
        for row in ram.get("rows") or []:
            svc = row.get("service")
            kb = row.get("rss_kb")
            if svc and isinstance(kb, (int, float)):
                tags = f'{{"service":{_json_str(svc)}}}'
                samples.append((now, "service_rss_kb", tags, float(kb)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] per-service RSS failed: {e}", flush=True)

    # Per-GPU per-service VRAM.
    try:
        gpus = status_endpoint._gpus_raw()
        vram_attr = status_endpoint._vram_attribution(gpus, config) or {}
        for entry in vram_attr.get("per_gpu") or []:
            idx = entry.get("index")
            if idx is None:
                continue
            for row in entry.get("services") or []:
                svc = row.get("service")
                used_mb = row.get("used_mb")
                if svc and isinstance(used_mb, (int, float)) and not str(svc).startswith("__"):
                    tags = f'{{"gpu":{int(idx)},"service":{_json_str(svc)}}}'
                    samples.append((now, "service_vram_mb", tags, float(used_mb)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] per-service VRAM failed: {e}", flush=True)

    # Xid 109 60min count (drives banner; useful as a time-series).
    try:
        xids = status_endpoint._journal_xids() or {}
        by60 = xids.get("by_code_60min") or {}
        by24 = xids.get("by_code_24h") or {}
        if isinstance(by60, dict):
            samples.append((now, "xid_109_60min", "", float(by60.get("109", 0) or 0)))
            samples.append((now, "xid_total_60min", "", float(sum(v for v in by60.values() if isinstance(v, int)))))
        if isinstance(by24, dict):
            samples.append((now, "xid_109_24h", "", float(by24.get("109", 0) or 0)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] xid probe failed: {e}", flush=True)

    if samples:
        try:
            writer.record_samples(samples)
        except Exception as e:  # noqa: BLE001
            print(f"[metrics_sampler] record_samples (slow) failed: {e}", flush=True)


def _json_str(s: str) -> str:
    """Tiny inline JSON-string encoder for tag dicts we build by hand."""
    import json as _json
    return _json.dumps(str(s))


async def sampler_loop(get_config: Optional[Callable[[], Mapping[str, Any]]] = None):
    """Main loop. Aligns to wall-clock 10s boundaries so charts look clean.

    `get_config` returns the live gateway config each cycle (it is hot-reloaded),
    so per-service attribution follows the current backends.
    """
    print("[metrics_sampler] starting", flush=True)
    try:
        # First sleep to align with the next 10s boundary.
        now = time.time()
        await asyncio.sleep(max(0.1, 10 - (now % 10)))

        while True:
            cycle_start = time.time()
            now_int = int(cycle_start)
            try:
                # Disk-pressure guard: pause writes when partition is < 1GB free.
                avail = await asyncio.to_thread(metrics_store.disk_avail_bytes, metrics_store.DB_PATH)
                if avail and avail < _DISK_PAUSE_BYTES:
                    if not _state["paused_for_disk"] or (
                        now_int - _state["last_disk_warn_ts"] >= _DISK_PAUSE_WARN_INTERVAL_S
                    ):
                        print(
                            f"[metrics_sampler] WARN: disk avail {avail} < {_DISK_PAUSE_BYTES}; "
                            f"pausing metric writes",
                            flush=True,
                        )
                        _state["last_disk_warn_ts"] = now_int
                    _state["paused_for_disk"] = True
                else:
                    if _state["paused_for_disk"]:
                        print("[metrics_sampler] disk pressure cleared; resuming writes", flush=True)
                    _state["paused_for_disk"] = False

                if not _state["paused_for_disk"]:
                    writer = get_writer()
                    # Fast tier (synchronous probes).
                    await asyncio.to_thread(_emit_fast, writer, now_int)
                    # Fast tier (async probes).
                    await _emit_fast_async(writer, now_int)

                    # Slow tier piggybacked at the top of each minute.
                    if (now_int % 60) < 10:
                        await asyncio.to_thread(_emit_slow, writer, now_int, get_config() if get_config else None)

                    # Per-minute rollup at the start of each new minute.
                    cur_min = now_int // 60
                    if cur_min != _state["last_minute_rolled"] and (now_int % 60) < 15:
                        try:
                            if writer.maintain("minute", now_int):
                                _state["last_minute_rolled"] = cur_min
                        except Exception as e:  # noqa: BLE001
                            print(f"[metrics_sampler] downsample_minute failed: {e}", flush=True)

                    # Per-hour rollup at the start of each new hour.
                    cur_hour = now_int // 3600
                    if cur_hour != _state["last_hour_rolled"] and (now_int % 3600) < 30:
                        try:
                            if writer.maintain("hour", now_int):
                                _state["last_hour_rolled"] = cur_hour
                        except Exception as e:  # noqa: BLE001
                            print(f"[metrics_sampler] downsample_hour failed: {e}", flush=True)

                    # Prune once per day at the first cycle past midnight UTC.
                    cur_day = now_int // 86400
                    if cur_day != _state["last_prune_day"] and (now_int % 86400) < 60:
                        try:
                            if writer.maintain("prune", now_int):
                                _state["last_prune_day"] = cur_day
                        except Exception as e:  # noqa: BLE001
                            print(f"[metrics_sampler] prune failed: {e}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # Top-level safety net so the loop never dies.
                print(f"[metrics_sampler] cycle failed: {e}", flush=True)

            # Sleep until next 10s boundary.
            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(0.1, 10.0 - elapsed))
    except asyncio.CancelledError:
        print("[metrics_sampler] cancelled; closing writer", flush=True)
        raise
    finally:
        await asyncio.to_thread(close_writer)


def record_request(metric: str, latency_ms: float, *, key: Optional[str], path: str, status: int) -> None:
    """Sync helper used by the request-counter middleware. Best-effort; never raises."""
    try:
        writer = get_writer()
        tags = {"path": path, "status": int(status)}
        if key:
            tags["key"] = key
        if writer.record_sample(int(time.time()), metric, float(latency_ms), tags):
            _state["request_writes"] += 1
    except Exception:
        pass

"""Async sampler that writes time-series snapshots into metrics.db.

One asyncio task. Fast tier every 10s, slow tier piggybacked every 60s.
Every cycle is wrapped in try/except so the loop never dies.

Reuses probe helpers from ``status_endpoint`` to avoid duplicating logic.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import metrics_store
import status_endpoint  # reuse existing probes


# When the disk holding metrics.db falls below this we pause writes and warn.
_DISK_PAUSE_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB
_DISK_PAUSE_WARN_INTERVAL_S = 600  # 10 min


_writer_conn = None
_state = {
    "paused_for_disk": False,
    "last_disk_warn_ts": 0,
    "last_minute_rolled": 0,
    "last_hour_rolled": 0,
    "last_prune_day": 0,
    "request_writes": 0,
}


def get_writer():
    """Module-level writer used both by the sampler loop and the request middleware."""
    global _writer_conn
    if _writer_conn is None:
        _writer_conn = metrics_store.open_writer()
    return _writer_conn


def close_writer() -> None:
    global _writer_conn
    if _writer_conn is not None:
        try:
            _writer_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
        try:
            _writer_conn.close()
        except Exception:
            pass
        _writer_conn = None


def _emit_fast(conn, now: int) -> None:
    """GPU/CPU/RAM samples + ComfyUI queue + llama slots. Synchronous probes."""
    samples: list[tuple] = []

    # GPUs.
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
            metrics_store.record_samples(conn, samples)
        except Exception as e:  # noqa: BLE001
            print(f"[metrics_sampler] record_samples (fast) failed: {e}", flush=True)


async def _emit_fast_async(conn, now: int) -> None:
    """ComfyUI queue depth (async) + llama slots."""
    try:
        queue, _stats = await status_endpoint._comfy_queue_and_stats()
        if isinstance(queue, dict):
            running = int(queue.get("running") or 0)
            pending = int(queue.get("pending") or 0)
            metrics_store.record_sample(conn, now, "comfy_queue_running", float(running))
            metrics_store.record_sample(conn, now, "comfy_queue_pending", float(pending))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] comfy queue probe failed: {e}", flush=True)

    try:
        _props, slots = await status_endpoint._llama_props_and_slots()
        if isinstance(slots, list):
            active = sum(1 for s in slots if isinstance(s, dict) and s.get("is_processing"))
            metrics_store.record_sample(conn, now, "llama_slots_active", float(active))
            metrics_store.record_sample(conn, now, "llama_slots_total", float(len(slots)))
    except Exception as e:  # noqa: BLE001
        print(f"[metrics_sampler] llama slots probe failed: {e}", flush=True)


def _emit_slow(conn, now: int) -> None:
    """Disk avail per partition + per-service RSS + per-GPU per-service VRAM."""
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
        ram = status_endpoint._ram_attribution() or {}
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
        vram_attr = status_endpoint._vram_attribution(gpus) or {}
        for entry in vram_attr.get("per_gpu") or []:
            idx = entry.get("index")
            if idx is None:
                continue
            for row in entry.get("rows") or []:
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
            metrics_store.record_samples(conn, samples)
        except Exception as e:  # noqa: BLE001
            print(f"[metrics_sampler] record_samples (slow) failed: {e}", flush=True)


def _json_str(s: str) -> str:
    """Tiny inline JSON-string encoder for tag dicts we build by hand."""
    import json as _json
    return _json.dumps(str(s))


async def sampler_loop():
    """Main loop. Aligns to wall-clock 10s boundaries so charts look clean."""
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
                avail = metrics_store.disk_avail_bytes()
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
                    conn = get_writer()
                    # Fast tier (synchronous probes).
                    _emit_fast(conn, now_int)
                    # Fast tier (async probes).
                    await _emit_fast_async(conn, now_int)

                    # Slow tier piggybacked at the top of each minute.
                    if (now_int % 60) < 10:
                        _emit_slow(conn, now_int)

                    # Per-minute rollup at the start of each new minute.
                    cur_min = now_int // 60
                    if cur_min != _state["last_minute_rolled"] and (now_int % 60) < 15:
                        try:
                            metrics_store.downsample_minute(conn)
                            _state["last_minute_rolled"] = cur_min
                        except Exception as e:  # noqa: BLE001
                            print(f"[metrics_sampler] downsample_minute failed: {e}", flush=True)

                    # Per-hour rollup at the start of each new hour.
                    cur_hour = now_int // 3600
                    if cur_hour != _state["last_hour_rolled"] and (now_int % 3600) < 30:
                        try:
                            metrics_store.downsample_hour(conn)
                            _state["last_hour_rolled"] = cur_hour
                        except Exception as e:  # noqa: BLE001
                            print(f"[metrics_sampler] downsample_hour failed: {e}", flush=True)

                    # Prune once per day at the first cycle past midnight UTC.
                    cur_day = now_int // 86400
                    if cur_day != _state["last_prune_day"] and (now_int % 86400) < 60:
                        try:
                            counts = metrics_store.prune_old(conn)
                            print(f"[metrics_sampler] prune: {counts}", flush=True)
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
        close_writer()
        raise


def record_request(metric: str, latency_ms: float, *, key: Optional[str], path: str, status: int) -> None:
    """Sync helper used by the request-counter middleware. Best-effort; never raises."""
    try:
        conn = get_writer()
        tags = {"path": path, "status": int(status)}
        if key:
            tags["key"] = key
        metrics_store.record_sample(conn, int(time.time()), metric, float(latency_ms), tags)
        _state["request_writes"] += 1
    except Exception:
        pass

"""Passive device capacity and process-cohort footprint observations.

No scheduling authority: resident memory is not a request's peak demand. History
is in-process, scoped to unchanged service configuration, device UUID and PID set.
Missing compute rows never establish a zero footprint.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from typing import Mapping

from gateway.resource_claims import CapacitySnapshot


def _mb(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def age_resource_view(view: dict, elapsed_s: float) -> dict:
    """Age a detached API projection; cached reads cannot refresh evidence."""
    result = copy.deepcopy(view)
    result["age_s"] += elapsed_s
    if result["age_s"] > result["fresh_for_s"]:
        for device in result["devices"]:
            device.update(quality="unavailable", total_mb=None, used_mb=None, free_mb=None)
        for service in result["services"]:
            for footprint in service["devices"]:
                footprint.update(quality="unavailable", resident_mb=None, reason="stale")
    return result


class ResourceObserver:
    """Learn observed high-water marks without probes, operator input or eviction.

    Called by the status builder on its event loop. Only the latest valid cohort
    per service/device is retained, so history is bounded by the current config
    and hardware. A failed, ambiguous or absent observation drops its history.
    """

    def __init__(self, *, fresh_s: float = 60.0):
        self.fresh_s = fresh_s
        self._history: dict[tuple, tuple[int, int, float]] = {}

    def observe(
        self, gpus: list[dict] | None, attribution: dict | None,
        services: Mapping[str, object], *, now: float, sampled_at: float,
        trustworthy: bool = True,
    ) -> dict:
        age = now - sampled_at
        trustworthy = trustworthy and 0 <= age <= self.fresh_s
        attribution = attribution or {}
        candidates = attribution.get("service_pid_candidates") or {}
        apps = attribution.get("raw_compute_apps") or []
        owners = Counter(pid for pids in candidates.values() for pid in set(pids))
        app_counts = Counter((app.get("pid"), app.get("gpu_uuid")) for app in apps)
        uuid_counts = Counter(gpu.get("uuid") for gpu in gpus or [])
        index_counts = Counter(gpu.get("index") for gpu in gpus or [])
        devices = []
        service_rows = {sid: {"service_id": sid, "devices": []} for sid in services}
        history: dict[tuple, tuple[int, int, float]] = {}
        for gpu in gpus or []:
            uid = gpu.get("uuid")
            total, used = gpu.get("memory_total_mb"), gpu.get("memory_used_mb")
            valid = (trustworthy and isinstance(uid, str) and bool(uid)
                     and uuid_counts[uid] == 1 and _mb(gpu.get("index"))
                     and index_counts[gpu.get("index")] == 1
                     and _mb(total) and _mb(used) and total > 0 and used <= total)
            # Reuse the resource-claim capacity contract, separately for each card.
            capacity = CapacitySnapshot(
                total_vram_mb=total if valid else None,
                used_vram_mb=used if valid else None,
                quality="measured" if valid else "unavailable",
            )
            devices.append({
                "uuid": uid, "index": gpu.get("index"),
                "total_mb": capacity.total_vram_mb, "used_mb": capacity.used_vram_mb,
                "free_mb": total - used if valid else None,
                "quality": capacity.quality,
            })
            device_apps = [app for app in apps if app.get("gpu_uuid") == uid]
            # Separate probes can race. Inconsistent process totals earn no credit.
            consistent = (valid and attribution.get("compute_apps_complete") is True
                          and all(_mb(a.get("used_mb")) for a in device_apps)
                          and sum(a["used_mb"] for a in device_apps) <= used
                          and all(app_counts[(a.get("pid"), uid)] == 1 for a in device_apps))
            for sid, config in services.items():
                pids = frozenset(candidates.get(sid) or ())
                rows = [a for a in device_apps if a.get("pid") in pids]
                collision = any(owners[pid] > 1 for pid in pids)
                reason = ("telemetry_unavailable" if not valid else "attribution_unavailable" if not pids
                          else "shared_process" if collision
                          else "inconsistent_process_sample" if not consistent
                          else "not_observed" if not rows else None)
                resident = sum(a["used_mb"] for a in rows) if reason is None else None
                peak = count = None
                if resident is not None:
                    signature = hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).digest()
                    key = (sid, signature, uid, pids)
                    old_peak, old_count, old_at = self._history.get(key, (resident, 0, sampled_at))
                    if not 0 <= sampled_at - old_at <= self.fresh_s:
                        old_peak, old_count = resident, 0
                    peak = max(old_peak, resident)
                    count = old_count + (sampled_at != old_at or old_count == 0)
                    history[key] = (peak, count, sampled_at)
                service_rows[sid]["devices"].append({
                    "uuid": uid, "resident_mb": resident,
                    "observed_peak_mb": peak, "samples": count,
                    "quality": "measured" if resident is not None else "unavailable",
                    "reason": reason,
                })
        self._history = history
        return {
            "age_s": max(0, age), "fresh_for_s": self.fresh_s,
            "devices": devices, "services": list(service_rows.values()),
            "scope": "service_process_cohort",
            "caveats": [
                "Observed resident memory and high-water marks are not request demand or guarantees.",
                "Samples are not atomic; history resets on missing evidence, configuration or PID changes and restart.",
                "No allocation, reservation, eviction or admission decision is made.",
            ],
        }

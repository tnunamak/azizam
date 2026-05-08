"""
In-process counters + event ring buffer for the firehose status page.

Single-worker assumption (the openai-proxy runs as one uvicorn process).
Everything lives in-memory; if/when we move to multi-worker we'll back this
with sqlite or redis. Until then, simplicity > durability.

Public surface:
    record_request(name, path, status, latency_ms)
    record_event(type, summary, **extra)
    snapshot_per_key()
    snapshot_recent_events(n)
    snapshot_video_meta()
    capture_video_submit(prompt_id, alias, workflow, size, seconds, key_name)

Privacy:
    - Never store request/response bodies.
    - Never store API keys (only the `name` field from AUTH_REGISTRY).
    - Truncate error messages to 160 chars.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any, Optional

# 24h window in seconds.
_WINDOW_S = 24 * 3600

_lock = threading.Lock()

# per-key activity:
#   { name: { "endpoints": { path: int }, "last_seen": float, "events": deque[(ts, status, latency_ms)] } }
_KEY_ACTIVITY: dict[str, dict[str, Any]] = defaultdict(lambda: {
    "endpoints": defaultdict(int),
    "last_seen": 0.0,
    "events": deque(maxlen=2000),  # bounded to keep memory finite
})

# Recent activity timeline (mixed event types). Bounded ring.
_RECENT_EVENTS: deque[dict[str, Any]] = deque(maxlen=200)

# Per-video metadata captured at submit time so the status page can show
# "ltx-2.3-long · 1280x720 · 8s · submitted by simon" alongside live progress.
_VIDEO_META: dict[str, dict[str, Any]] = {}


def record_request(
    key_name: Optional[str],
    path: str,
    status: int,
    latency_ms: Optional[float],
) -> None:
    """Increment counters for a finished request.

    Safe to call from async contexts (lock is brief).
    """
    if not path:
        return
    name = key_name or "anonymous"
    now = time.time()
    with _lock:
        rec = _KEY_ACTIVITY[name]
        rec["endpoints"][path] = rec["endpoints"].get(path, 0) + 1
        rec["last_seen"] = now
        rec["events"].append((now, int(status), float(latency_ms) if latency_ms is not None else None))


def record_event(event_type: str, summary: str, **extra: Any) -> None:
    """Append a free-form event to the timeline. extra is shallow-copied."""
    evt = {
        "ts": time.time(),
        "type": str(event_type),
        "summary": str(summary)[:200],
    }
    for k, v in extra.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            evt[k] = v
        else:
            evt[k] = str(v)[:200]
    with _lock:
        _RECENT_EVENTS.append(evt)


def capture_video_submit(
    prompt_id: str,
    *,
    alias: Optional[str],
    workflow: Optional[str],
    size: Optional[str],
    seconds: Optional[float],
    key_name: Optional[str],
    prompt_text: Optional[str] = None,
) -> None:
    if not prompt_id:
        return
    pt = (prompt_text or "")
    if len(pt) > 200:
        pt = pt[:200] + "..."
    with _lock:
        _VIDEO_META[prompt_id] = {
            "alias": alias,
            "workflow": workflow,
            "size": size,
            "seconds": seconds,
            "key_name": key_name,
            "prompt_truncated": pt,
            "submitted_at": time.time(),
        }


def snapshot_per_key(known_names: list[str]) -> list[dict[str, Any]]:
    """Return per-key activity, including names that have never been seen."""
    cutoff_24h = time.time() - _WINDOW_S
    out: list[dict[str, Any]] = []
    seen = set()
    with _lock:
        for name, rec in _KEY_ACTIVITY.items():
            seen.add(name)
            evts = [e for e in rec["events"] if e[0] >= cutoff_24h]
            total = len(evts)
            errs_4xx = sum(1 for _, s, _ in evts if 400 <= s < 500)
            errs_5xx = sum(1 for _, s, _ in evts if 500 <= s < 600)
            lat = sorted([l for _, _, l in evts if l is not None])
            p50 = _percentile(lat, 0.50)
            p95 = _percentile(lat, 0.95)
            top_paths = sorted(rec["endpoints"].items(), key=lambda x: -x[1])[:5]
            out.append({
                "name": name,
                "requests_24h": total,
                "errors_4xx_24h": errs_4xx,
                "errors_5xx_24h": errs_5xx,
                "last_seen": rec["last_seen"] if rec["last_seen"] else None,
                "latency_ms_p50": p50,
                "latency_ms_p95": p95,
                "top_endpoints": [{"path": p, "count": c} for p, c in top_paths],
                "active": (rec["last_seen"] or 0) >= cutoff_24h,
            })
        for name in known_names:
            if name not in seen:
                out.append({
                    "name": name,
                    "requests_24h": 0,
                    "errors_4xx_24h": 0,
                    "errors_5xx_24h": 0,
                    "last_seen": None,
                    "latency_ms_p50": None,
                    "latency_ms_p95": None,
                    "top_endpoints": [],
                    "active": False,
                })
    out.sort(key=lambda r: -(r["requests_24h"] or 0))
    return out


def snapshot_recent_events(n: int = 20) -> list[dict[str, Any]]:
    with _lock:
        items = list(_RECENT_EVENTS)
    return list(reversed(items[-n:]))


def snapshot_video_meta() -> dict[str, dict[str, Any]]:
    """Shallow copy so callers can iterate safely."""
    with _lock:
        return {k: dict(v) for k, v in _VIDEO_META.items()}


def gc_old_state() -> None:
    """Drop video meta entries older than 6h. Keep request events bounded by deque maxlen."""
    cutoff = time.time() - 6 * 3600
    with _lock:
        stale = [pid for pid, meta in _VIDEO_META.items() if meta.get("submitted_at", 0) < cutoff]
        for pid in stale:
            _VIDEO_META.pop(pid, None)


def _percentile(sorted_vals: list[float], q: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    idx = int(round((len(sorted_vals) - 1) * q))
    return sorted_vals[idx]

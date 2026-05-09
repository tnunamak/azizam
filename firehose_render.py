"""
Server-side HTML renderer for the openai.vivid.fish status dashboard.

This is the only status page now — `/` and `/firehose` both serve it.

Sections are grouped by purpose (banner & alerts → live activity → resources
→ capabilities → recent output → identity activity → diagnostics) and the
heaviest diagnostic blocks are inside collapsible <details> blocks so the
default view stays scannable while the full firehose is still one click away.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import time
from typing import Any, Optional


def _esc(s: Any) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _abs_utc(ts: Optional[float]) -> str:
    if not ts:
        return ""
    try:
        return _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return ""


def _ago(ts: Optional[float]) -> str:
    """Relative timestamp, with absolute UTC tooltip via title attr at the
    call site if needed. Returns "—" for missing/zero."""
    if not ts:
        return "—"
    diff = max(0.0, time.time() - float(ts))
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    return f"{int(diff / 86400)}d ago"


def _ago_with_abs(ts: Optional[float]) -> str:
    """Relative + absolute UTC, both visible. For diagnostics where staleness
    matters and "1d ago" alone isn't enough."""
    if not ts:
        return "—"
    rel = _ago(ts)
    abs_ = _abs_utc(ts)
    if not abs_:
        return rel
    return f'<span title="{_esc(abs_)}">{_esc(rel)}</span> <span class="muted small">({_esc(abs_)})</span>'


# clocks_throttle_reasons.active bitmask (NVML/nvidia-smi).
# Bit 0 alone == idle (CPU not driving the GPU); not a real throttle.
# Bits 3 / 5-7 (HW_SLOWDOWN family) indicate silicon-protection clamps.
_THROTTLE_BITS = {
    0: "GPU idle",
    1: "App clocks setting",
    2: "SW power cap",
    3: "HW slowdown",
    4: "Sync boost",
    5: "SW thermal",
    6: "HW thermal",
    7: "HW power brake",
    8: "Display clock setting",
}
_THROTTLE_HW_BITS = {3, 5, 6, 7}


def _decode_throttle(hex_str: Optional[str]) -> str:
    if not hex_str:
        return ""
    try:
        v = int(hex_str, 16)
    except Exception:
        return _esc(hex_str)
    if v == 0:
        return f"{hex_str} (no throttle)"
    if v == 1:
        return f"{hex_str} (idle)"
    labels: list[str] = []
    for bit in sorted(_THROTTLE_BITS.keys()):
        if v & (1 << bit):
            labels.append(_THROTTLE_BITS[bit])
    known_mask = sum(1 << bit for bit in _THROTTLE_BITS)
    leftover = v & ~known_mask
    if leftover:
        labels.append(f"unknown bits 0x{leftover:x}")
    return f"{hex_str} ({', '.join(labels)})"


def _throttle_is_real(hex_str: Optional[str]) -> bool:
    """True iff any HW slowdown bit is set."""
    if not hex_str:
        return False
    try:
        v = int(hex_str, 16)
    except Exception:
        return False
    return any(v & (1 << bit) for bit in _THROTTLE_HW_BITS)


def _render_pcie(g: dict) -> str:
    """Compact PCIe link string with idle-downclock context.

    Format: ``pcie max=gen4x16 cur=gen1x8 (idle downclock — normal)``.
    Renders cur in red if util is high but link is downgraded.
    """
    cur_g = g.get("pcie_link_gen")
    cur_w = g.get("pcie_link_width")
    max_g = g.get("pcie_link_gen_max")
    max_w = g.get("pcie_link_width_max")
    util = g.get("instantaneous_util_pct")

    if cur_g is None and cur_w is None:
        return "pcie unknown"
    cur_str = (
        f"gen{cur_g}x{cur_w}" if (cur_g is not None and cur_w is not None) else "?"
    )
    if max_g is None and max_w is None:
        return f"pcie {_esc(cur_str)}"
    max_str = (
        f"gen{max_g}x{max_w}" if (max_g is not None and max_w is not None) else "?"
    )
    downgraded = (
        (max_g is not None and cur_g is not None and cur_g < max_g)
        or (max_w is not None and cur_w is not None and cur_w < max_w)
    )
    if not downgraded:
        return f"pcie max={_esc(max_str)} cur={_esc(cur_str)}"
    if isinstance(util, int) and util >= 50:
        return (
            f"pcie max={_esc(max_str)} "
            f'<span class="bad">cur={_esc(cur_str)}</span> '
            f'<span class="muted small">(downgraded under load!)</span>'
        )
    return (
        f"pcie max={_esc(max_str)} cur={_esc(cur_str)} "
        f'<span class="muted small">(idle downclock — normal)</span>'
    )


def _render_power(g: dict) -> str:
    """Format power as ``draw=X W (limit Y W / max Z W · default D W)``."""
    draw = g.get("power_draw_w")
    limit = g.get("power_limit_w")
    default_limit = g.get("power_default_limit_w")
    max_limit = g.get("power_max_limit_w")
    if draw is None and limit is None:
        return "power=?"
    if limit is None:
        return f"power={_esc(draw)}W"
    inner = f"limit {_esc(limit)}W"
    if max_limit is not None and abs(float(max_limit) - float(limit)) >= 1.0:
        inner += f" / max {_esc(max_limit)}W"
    if (
        default_limit is not None
        and abs(float(default_limit) - float(limit)) >= 1.0
    ):
        inner += f" · default {_esc(default_limit)}W"
    return f"power={_esc(draw)}W ({inner})"


def _bytes(n: Optional[int]) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n/1024:.1f} KiB"
    if n < 1024**3:
        return f"{n/1024**2:.1f} MiB"
    if n < 1024**4:
        return f"{n/1024**3:.2f} GiB"
    return f"{n/1024**4:.2f} TiB"


def _pct_class(pct: Optional[float], warn: float = 80, crit: float = 95) -> str:
    if pct is None:
        return ""
    if pct >= crit:
        return "bad"
    if pct >= warn:
        return "warn"
    return "ok"


def _kv_table(rows: list[tuple[str, Any]]) -> str:
    out = ['<table class="kv">']
    for k, v in rows:
        if isinstance(v, (dict, list)):
            v = "<code>" + _esc(json.dumps(v, default=str)[:300]) + "</code>"
        else:
            v = _esc(v) if v is not None else "—"
        out.append(f"<tr><th>{_esc(k)}</th><td>{v}</td></tr>")
    out.append("</table>")
    return "".join(out)


def render_html(payload: dict, *, host: Optional[str] = None) -> str:
    """Render the status page from a /v1/status payload.

    ``host`` should be the request's ``Host`` header so the page header
    matches the address bar (loopback dev vs. ``openai.vivid.fish`` prod).

    Section layout:
      Top: banner + reasons + meta links.
      Group 1 — Live activity (what's happening right now).
      Group 2 — Health (backend liveness, GPUs, swap state, aux health).
      Group 3 — Resources (VRAM attribution, budgets, disk, thermal, CPU,
                image cache).
      Group 4 — Capabilities (LLM, video/image workflows, voices).
      Group 5 — Recent output (videos + images).
      Group 6 — Identity (per-key activity, recent events).
      Group 7 — Diagnostics (collapsed by default; driver, daisy, drift,
                config snapshot, vram measurements, errors).
    """
    display_host = (host or "openai.vivid.fish").strip() or "openai.vivid.fish"
    parts: list[str] = []
    # Use str.replace, not .format — the template contains CSS braces that
    # would otherwise need to be doubled.
    parts.append(_HTML_HEAD_TEMPLATE.replace(
        "__TITLE__", _esc(f"{display_host} · firehose"),
    ))
    parts.append(_render_banner(payload, host=display_host))
    parts.append(_render_critical_strip(payload))
    parts.append(_render_export_links(payload))

    def _section(title: str, body: str, *, collapsed: bool = False) -> str:
        if collapsed:
            return (
                f'<section><details><summary class="h2-summary">'
                f'<h2 style="display:inline-block; margin:0; vertical-align:middle;">{_esc(title)}</h2>'
                f'</summary><div class="details-body">{body}</div></details></section>'
            )
        return f'<section><h2>{_esc(title)}</h2>{body}</section>'

    parts.append(_render_alert_box(payload))

    parts.append('<div class="group-label">Live activity</div>')
    parts.append(_section("Live video jobs", _render_video_jobs(payload.get("video_jobs") or [])))
    parts.append(_section("ComfyUI queue", _render_comfy_queue(payload)))
    parts.append(_section("LLM slots", _render_slots((payload.get("models") or {}).get("slots") or [])))
    parts.append(_section("Swap & websocket state", _render_swap_state(payload)))

    parts.append('<div class="group-label">Health</div>')
    parts.append(_section("Backends", _render_backends(payload.get("backends") or [])))
    parts.append(_section("GPUs (live)", _render_gpus(payload.get("gpus") or [])))
    parts.append(_section("Auxiliary backend health", _render_aux_health(payload.get("aux_health") or {})))

    parts.append('<div class="group-label">Resources</div>')
    parts.append(_section("Per-service VRAM attribution", _render_vram_attribution(payload.get("vram_attribution") or {})))
    parts.append(_section("Budgets", _render_budgets(payload.get("budgets") or {})))
    parts.append(_section("Disk pressure", _render_disk(payload.get("disk") or {})))
    parts.append(_section("Image cache", _render_image_cache(payload.get("image_cache"))))
    parts.append(_section("Thermal", _render_thermal(payload.get("thermal") or {})))
    parts.append(_section("CPU & system load", _render_cpu(payload.get("cpu") or {})))

    parts.append('<div class="group-label">Capabilities</div>')
    parts.append(_section("Models / LLM", _render_models(payload.get("models") or {}, gpus=payload.get("gpus") or [])))
    parts.append(_section("Video capabilities", _render_video_caps(payload.get("capabilities") or [])))
    parts.append(_section("Image capabilities", _render_image_caps(payload.get("image_capabilities") or [])))
    parts.append(_section("Voices summary", _render_voices(payload.get("voices_summary"))))

    parts.append('<div class="group-label">Recent output</div>')
    parts.append(_section("Recent videos", _render_recent_videos(payload.get("recent_videos") or [])))
    parts.append(_section("Recent images", _render_recent_images(payload.get("recent_images") or [])))

    parts.append('<div class="group-label">Identity</div>')
    parts.append(_section(
        "Activity since last restart (or 24h, whichever shorter)",
        _render_auth_keys(payload.get("auth_keys") or [], window=payload.get("auth_keys_window") or {}),
    ))
    parts.append(_section("Recent activity timeline", _render_recent_events(payload.get("recent_events") or [])))

    parts.append('<div class="group-label">Diagnostics</div>')
    parts.append(_section("Driver & kernel events", _render_driver(payload.get("driver") or {}), collapsed=True))
    parts.append(_section("ComfyUI view (system_stats)", _render_comfy_view(payload.get("comfy_view")), collapsed=True))
    parts.append(_section("Daisy audit", _render_daisy(payload.get("daisy") or {}), collapsed=True))
    parts.append(_section("Workflow drift", _render_workflow_drift(payload.get("workflow_drift")), collapsed=True))
    parts.append(_section("VRAM measurements (image batcher)", _render_vram_estimates(payload.get("budgets") or {}), collapsed=True))
    parts.append(_section("Configuration snapshot", _render_config_snapshot(payload.get("config_snapshot") or {}), collapsed=True))
    # Show errors un-collapsed when present so misbehaving probes surface.
    # Hide the section entirely when there are no errors — an empty "none"
    # row is just visual noise.
    errs = payload.get("errors") or {}
    if errs:
        parts.append(_section("Errors during status build", _render_errors(errs)))

    commit = payload.get("commit_hash")
    version_html = (
        f' · version <code>{_esc(commit)}</code>'
        if commit else ""
    )
    parts.append(_HTML_FOOT_TEMPLATE.replace("__VERSION_HTML__", version_html))
    return "".join(parts)


# --- header ---------------------------------------------------------------

_HTML_HEAD_TEMPLATE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<meta http-equiv="refresh" content="10"/>
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark; }
  body { background:#0b0d10; color:#e3e8ee; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, system-ui, sans-serif; margin:0; padding:24px; line-height:1.45; }
  h1 { font-size:18px; margin:0 0 4px; }
  h2 { font-size:14px; letter-spacing:.04em; text-transform:uppercase; color:#9aa4b1; margin:32px 0 8px; padding-bottom:4px; border-bottom:1px solid #1c2128; }
  h3 { font-size:12px; color:#7c8794; margin:14px 0 6px; }
  section { margin-bottom:8px; }
  table { border-collapse:collapse; font-size:12px; width:100%; margin:4px 0 12px; background:#10141a; }
  th, td { border:1px solid #1c2128; padding:4px 8px; text-align:left; vertical-align:top; }
  th { color:#9aa4b1; font-weight:600; background:#11161d; }
  table.kv th { width:200px; }
  td.mono, th.mono, code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size:11.5px; color:#cdd6e0; }
  pre { white-space:pre-wrap; word-break:break-word; background:#080a0d; border:1px solid #1c2128; padding:8px; }
  .banner { padding:12px 16px; border-radius:6px; margin-bottom:16px; }
  .banner.ok { background:#0d2417; border:1px solid #1d4d2c; }
  .banner.warn { background:#2a2511; border:1px solid #5e511f; }
  .banner.bad { background:#2a1414; border:1px solid #5e2222; }
  .banner-badge.ok { background:#0d2417; border:1px solid #1d4d2c; color:#7fe6a3; }
  .banner-badge.warn { background:#2a2511; border:1px solid #5e511f; color:#f3d27f; }
  .banner-badge.bad { background:#2a1414; border:1px solid #5e2222; color:#f4a0a0; }
  .pill { display:inline-block; padding:1px 6px; border-radius:99px; font-size:10.5px; border:1px solid #2a323c; background:#161c24; color:#cbd5df; margin-right:4px; }
  .pill.ok { background:#143a25; color:#7fe6a3; border-color:#1e5e3a; }
  .pill.warn { background:#3a2f10; color:#f3d27f; border-color:#5e4d1e; }
  .pill.bad { background:#3a1414; color:#f4a0a0; border-color:#5e2222; }
  .pill.muted { color:#7c8794; }
  .bar { background:#1c2128; height:6px; border-radius:99px; overflow:hidden; margin:4px 0; }
  .bar > span { display:block; height:100%; background:#3b82f6; }
  .bar.warn > span { background:#eab308; }
  .bar.bad > span { background:#ef4444; }
  .bar.ok > span { background:#22c55e; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap:8px; }
  .card { background:#10141a; border:1px solid #1c2128; padding:8px 10px; border-radius:4px; }
  .muted { color:#7c8794; }
  .err { color:#f4a0a0; }
  a { color:#7aa6ff; }
  hr { border:none; border-top:1px solid #1c2128; margin:8px 0; }
  details { margin:4px 0; }
  summary { cursor:pointer; color:#9aa4b1; font-size:12px; list-style: revert; }
  summary.h2-summary { color:#9aa4b1; }
  summary.h2-summary h2 { display:inline-block !important; border:0 !important; padding-bottom:0 !important; margin:0 !important; }
  summary.h2-summary::after { content: " (click to expand)"; color:#5d6571; font-size:10.5px; font-weight:400; text-transform:none; letter-spacing:0; }
  details[open] summary.h2-summary::after { content: " (click to collapse)"; }
  details > .details-body { padding-top:6px; padding-left:6px; border-left:2px solid #1c2128; margin-left:4px; margin-top:6px; }
  .group-label { font-size:10.5px; letter-spacing:.12em; text-transform:uppercase; color:#5d6571; margin:24px 0 4px; padding:4px 8px; background:#0d1117; border-radius:4px; border-left:3px solid #2a3039; }
  .alert-box { margin:0 0 16px; border:1px solid #5e2222; background:#1a0e0e; border-radius:6px; padding:8px 12px; }
  .alert-row { padding:4px 0; font-size:13px; }
  .alert-row.bad { color:#f4a0a0; }
  .alert-row.warn { color:#f3d27f; }
  .alert-row + .alert-row { border-top:1px dashed #2a323c; padding-top:6px; margin-top:4px; }
  .small { font-size:10.5px; }
  .right { float:right; }
  .nowrap { white-space:nowrap; }
</style>
</head><body>
"""


_HTML_FOOT_TEMPLATE = """
<footer style="margin-top:32px; padding-top:12px; border-top:1px solid #1c2128; color:#7c8794; font-size:11px;">
  Auto-refreshing every 10s via &lt;meta http-equiv="refresh"&gt;. For programmatic access,
  poll <a href="/v1/status">/v1/status</a> directly (bearer token required).
  · <a href="https://github.com/Vivid-Fish/openai-proxy" target="_blank" rel="noopener">github.com/Vivid-Fish/openai-proxy</a>
  __VERSION_HTML__
</footer>
</body></html>
"""


# --- sections -------------------------------------------------------------

def _render_banner(p: dict, *, host: Optional[str] = None) -> str:
    banner = p.get("banner") or "ok"
    cls = banner if banner in ("ok", "warn", "bad") else "ok"
    # Primary banner is API health only. "bad" means a required backend is
    # actually down, not "the host has a full disk".
    title = {
        "ok": "API operational",
        "warn": "API Issue Detected",
        "bad": "API Issue Detected",
    }[cls]
    gen = p.get("generated_at")
    cache_age = p.get("cache_age_s")
    try:
        cache_age_str = f"{float(cache_age):.1f}s"
    except Exception:
        cache_age_str = "—"
    abs_gen = _abs_utc(gen) or "—"
    display_host = (host or "openai.vivid.fish").strip() or "openai.vivid.fish"
    reasons = p.get("banner_reasons") or []
    reasons_html = ""
    if reasons:
        reasons_html = (
            '<div style="margin-top:6px;">'
            + "".join(f'<span class="pill {cls}">{_esc(r)}</span>' for r in reasons)
            + '</div>'
        )

    # The proxy is not responsible for monitoring host machine state. The
    # underlying signals (disk fill, Xid surge, daisy audit staleness, thermal)
    # remain visible in their respective sections of the page. `system_health`
    # and `system_health_reasons` are still emitted in the JSON for clients,
    # but no top-level badge is rendered here.
    sh_html = ""
    commit = p.get("commit_hash")
    commit_html = (
        f' · version <code>{_esc(commit)}</code>' if commit else ""
    )
    refresh_html = ' · auto-refresh 10s'
    return (
        f'<div class="banner {cls}">'
        f'<h1>{_esc(title)}</h1>'
        f'<div class="muted small">'
        f'served at <code>{_esc(display_host)}</code>'
        f' · generated <span title="{_esc(abs_gen)}">{_ago(gen)}</span> '
        f'({_esc(abs_gen)}) · cache age {_esc(cache_age_str)}{commit_html}{refresh_html}'
        f'</div>'
        f'{reasons_html}'
        f'{sh_html}'
        f'</div>'
    )


def _render_export_links(p: dict) -> str:
    return (
        '<div class="muted small" style="margin-bottom:12px;">'
        '<a href="/v1/status">/v1/status (raw JSON)</a> · '
        '<a href="/health">/health</a> · '
        '<a href="/v1/models">/v1/models</a> · '
        '<a href="/v1/audio/voices">/v1/audio/voices</a>'
        '</div>'
    )


def _render_critical_strip(p: dict) -> str:
    """A thin strip just below the banner highlighting events the user almost
    certainly cares about. Pulls candidate signals from multiple sources
    (driver Xid bursts, daisy denials, swap state, model swaps, llama crashes),
    dedups by ``(type_label, summary)`` keeping the most recent timestamp,
    ranks by ``severity * log-recency``, and caps at 5 entries.

    When nothing critical has surfaced in the last 24h, renders an explicit
    "no critical events in last 24h" line (green) so the user knows the strip
    is intentional and not a render bug.
    """
    import math

    now = time.time()
    # Severity weights: tune in one place, not scattered through branches.
    SEV: dict[str, float] = {
        "xid_109": 100.0,
        "xid_other": 60.0,
        "llama_crash": 80.0,
        "daisy_denied": 40.0,
        "swap_in_progress": 25.0,
        "model_swap": 10.0,
    }

    candidates: list[dict] = []

    def _add(key: str, ts: Optional[float], label: str, summary: str, css: str,
             abs_when: str = "") -> None:
        candidates.append({
            "key": key,
            "ts": ts,
            "label": label,
            "summary": summary,
            "css": css,
            "abs_when": abs_when,
        })

    driver = p.get("driver") or {}
    by24 = driver.get("by_code_24h") or {}
    by60 = driver.get("by_code_60min") or {}
    last5_xid = driver.get("last_5_events") or []

    def _xid_latest_epoch() -> Optional[float]:
        """Best-effort epoch from the most-recent parsed Xid entry's ``ts``
        (journalctl format like ``May 08 15:37:09``)."""
        if not last5_xid:
            return None
        try:
            ts_str = last5_xid[-1].get("ts") or ""
            import datetime as _d
            year = _d.datetime.now().year
            dt = _d.datetime.strptime(f"{year} {ts_str}", "%Y %b %d %H:%M:%S")
            return dt.timestamp()
        except Exception:
            return None

    xid_latest = _xid_latest_epoch()
    if isinstance(by24, dict):
        for code, count in by24.items():
            try:
                code_int = int(code)
            except Exception:
                continue
            if not count:
                continue
            key = "xid_109" if code_int == 109 else "xid_other"
            in_60 = by60.get(code) or by60.get(str(code)) or by60.get(code_int) or 0
            window = "60min" if in_60 else "24h"
            label = f"Xid {code_int}"
            shown = in_60 if in_60 else count
            summary = f"{shown} event(s) in last {window}"
            css = "bad" if (code_int == 109 or in_60) else "warn"
            _add(key, xid_latest, label, summary, css)

    swap = p.get("swap_state") or {}
    if swap.get("tabby_swap_in_progress"):
        _add("swap_in_progress", now, "swap", "tabby model swap in progress", "warn")
    if swap.get("kobold_swap_in_progress"):
        _add("swap_in_progress", now, "swap", "kobold model swap in progress", "warn")

    daisy = p.get("daisy") or {}
    for entry in (daisy.get("recent_entries") or []):
        decision = (entry.get("decision") or "").lower()
        if decision not in ("denied", "blocked", "rejected"):
            continue
        ts_raw = entry.get("ts")
        ts_epoch: Optional[float] = None
        if ts_raw:
            try:
                import datetime as _d
                s = str(ts_raw).replace("Z", "+00:00")
                ts_epoch = _d.datetime.fromisoformat(s).timestamp()
            except Exception:
                ts_epoch = None
        summary = (entry.get("summary") or "")[:120]
        _add("daisy_denied", ts_epoch, f"daisy {decision}", summary, "bad",
             abs_when=str(ts_raw) if ts_raw else "")

    # llama-server crashes — recent_events ring, hook for future entries.
    for evt in (p.get("recent_events") or []):
        if evt.get("type") == "llama_crash":
            _add("llama_crash", evt.get("ts"), "llama crash",
                 (evt.get("summary") or "")[:120], "bad")

    # Most recent model_swap from the events ring.
    for evt in (p.get("recent_events") or []):
        if evt.get("type") == "model_swap":
            _add("model_swap", evt.get("ts"), "model swap",
                 (evt.get("summary") or "")[:120], "warn")
            break

    # Dedup by (label, summary): keep the candidate with the most recent ts.
    dedup: dict[tuple[str, str], dict] = {}
    for c in candidates:
        k = (c["label"], c["summary"])
        prev = dedup.get(k)
        if prev is None or (c.get("ts") or 0) > (prev.get("ts") or 0):
            dedup[k] = c
    deduped = list(dedup.values())

    # Strip is for *recent* signals — drop anything older than 24h.
    fresh = [
        c for c in deduped
        if (c.get("ts") is None) or (now - float(c["ts"]) <= 86400)
    ]

    if not fresh:
        return (
            '<div style="margin:0 0 12px; padding:8px 12px; border:1px solid #1d4d2c;'
            ' background:#0d2417; border-radius:4px;">'
            '<b style="color:#7ed29a; font-size:11px; letter-spacing:.08em; text-transform:uppercase;">'
            'Recent critical events</b>'
            ' <span class="muted small">no critical events in last 24h</span>'
            '</div>'
        )

    def _score(c: dict) -> float:
        """severity × log(1 + recency). Recency saturates at ~1.0 for fresh
        events and decays toward 0 over 24h."""
        sev = SEV.get(c["key"], 5.0)
        ts = c.get("ts")
        if ts is None:
            recency = 0.5
        else:
            age_h = max(0.0, (now - float(ts)) / 3600.0)
            recency = 1.0 / (1.0 + age_h)
        return sev * (math.log1p(recency * 10) + 0.1)

    fresh.sort(key=_score, reverse=True)
    capped = fresh[:5]

    rows: list[str] = []
    for c in capped:
        ts = c.get("ts")
        when_html = ""
        if ts:
            rel = _ago(ts)
            abs_w = c.get("abs_when") or _abs_utc(ts)
            when_html = (
                f' <span class="muted small" title="{_esc(abs_w)}">'
                f'({_esc(rel)})</span>'
            )
        elif c.get("abs_when"):
            when_html = f' <span class="muted small">({_esc(c["abs_when"])})</span>'
        rows.append(
            f'<span class="pill {c["css"]}">{_esc(c["label"])}: '
            f'{_esc(c["summary"])}</span>{when_html}'
        )

    return (
        '<div style="margin:0 0 12px; padding:8px 12px; border:1px solid #5e2222;'
        ' background:#1a0e0e; border-radius:4px;">'
        '<b style="color:#f4a0a0; font-size:11px; letter-spacing:.08em; text-transform:uppercase;">'
        'Recent critical events</b><div style="margin-top:6px; display:flex;'
        ' flex-wrap:wrap; gap:6px 10px; align-items:center;">'
        + " ".join(rows) +
        '</div></div>'
    )


def _render_alert_box(p: dict) -> str:
    """Show bad/warn-state items prominently if any. Currently surfaces:
    backends in down/degraded state, disk partitions over warn threshold,
    swap-in-progress, and Xid surges.
    """
    alerts: list[tuple[str, str]] = []  # (level, html)
    for b in p.get("backends") or []:
        live = b.get("live_status")
        if live in ("down", "degraded") and b.get("role") == "required":
            level = "bad" if live == "down" else "warn"
            alerts.append((
                level,
                f'<b>{_esc(b.get("name"))}</b> is {_esc(live)}'
                + (f' — last error: <code>{_esc(b.get("last_error"))}</code>' if b.get("last_error") else "")
            ))
    for part in (p.get("disk") or {}).get("partitions") or []:
        pct = part.get("pct_used") or 0
        if pct >= 95:
            alerts.append(("bad",
                f'Disk <b>{_esc(part.get("target"))}</b> is {_esc(pct)}% full '
                f'({_bytes(part.get("avail_bytes"))} free of {_bytes(part.get("size_bytes"))})'))
        elif pct >= 85:
            alerts.append(("warn",
                f'Disk <b>{_esc(part.get("target"))}</b> is {_esc(pct)}% full '
                f'({_bytes(part.get("avail_bytes"))} free of {_bytes(part.get("size_bytes"))})'))
    swap = p.get("swap_state") or {}
    if swap.get("tabby_swap_in_progress"):
        alerts.append(("warn", "TabbyAPI model swap is in progress"))
    if swap.get("kobold_swap_in_progress"):
        alerts.append(("warn", "Kobold/llama-turbo model swap is in progress"))
    by60 = ((p.get("driver") or {}).get("by_code_60min") or {})
    total60 = sum(by60.values()) if isinstance(by60, dict) else 0
    if total60 >= 5:
        alerts.append(("warn",
            f'Xid surge: {total60} kernel events in last 60 min ({_esc(by60)})'))
    thermal = p.get("thermal") or {}
    if thermal.get("cpu_thermal_state") == "critical":
        alerts.append(("bad", f'CPU temperature critical ({_esc(thermal.get("cpu_max_c"))}°C)'))

    if not alerts:
        return ""

    rows = []
    for level, body in alerts:
        rows.append(f'<div class="alert-row {level}">{body}</div>')
    return f'<div class="alert-box">{"".join(rows)}</div>'


def _render_backends(backends: list[dict]) -> str:
    if not backends:
        return '<div class="muted">no backend probes</div>'
    rows = ['<table><thead><tr><th>Name</th><th>Role</th><th>Status</th><th>Latency</th><th>URL</th><th>Last error</th><th>Note</th></tr></thead><tbody>']
    for b in backends:
        live = b.get("live_status") or b.get("status") or "?"
        cls = {"up": "ok", "degraded": "warn", "down": "bad"}.get(live, "muted")
        rows.append(
            f'<tr>'
            f'<td>{_esc(b.get("name"))}</td>'
            f'<td>{_esc(b.get("role"))}</td>'
            f'<td><span class="pill {cls}">{_esc(live)}</span></td>'
            f'<td>{_esc(b.get("latency_ms")) if b.get("latency_ms") is not None else "—"} ms</td>'
            f'<td class="mono">{_esc(b.get("url"))}</td>'
            f'<td class="mono">{_esc(b.get("last_error") or "")}</td>'
            f'<td class="muted small">{_esc(b.get("note") or "")}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_video_jobs(jobs: list[dict]) -> str:
    if not jobs:
        return '<div class="muted">no jobs in flight or recent terminal entries</div>'
    rows = [
        '<table><thead><tr>'
        '<th>Status</th><th>Progress</th><th>Phase</th><th>Step/Total</th>'
        '<th>Alias</th><th>Workflow</th><th>Size</th><th>Sec</th>'
        '<th>Elapsed</th><th>ETA</th><th>Key</th><th>Prompt (truncated)</th>'
        '<th>prompt_id</th><th>Error</th>'
        '</tr></thead><tbody>'
    ]
    def _fmt_secs(s):
        if s is None:
            return "—"
        try:
            s = int(s)
        except Exception:
            return _esc(s)
        if s < 0:
            return "—"
        if s < 60:
            return f"{s}s"
        m, sec = divmod(s, 60)
        if m < 60:
            return f"{m}m{sec:02d}s"
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"
    for j in jobs:
        status = j.get("status") or "?"
        cls = {"running": "warn", "queued": "muted", "succeeded": "ok", "failed": "bad"}.get(status, "muted")
        pct = j.get("percent") or 0
        bar_cls = "ok" if status == "succeeded" else ("bad" if status == "failed" else "warn")
        rows.append(
            f'<tr>'
            f'<td><span class="pill {cls}">{_esc(status)}</span></td>'
            f'<td><div class="bar {bar_cls}"><span style="width:{pct}%"></span></div><span class="small mono">{pct}%</span></td>'
            f'<td>{_esc(j.get("phase"))}</td>'
            f'<td class="mono">{_esc(j.get("step"))}/{_esc(j.get("total"))}</td>'
            f'<td class="mono">{_esc(j.get("alias"))}</td>'
            f'<td class="mono">{_esc(j.get("workflow"))}</td>'
            f'<td class="mono">{_esc(j.get("size"))}</td>'
            f'<td>{_esc(j.get("seconds"))}s</td>'
            f'<td class="mono">{_fmt_secs(j.get("elapsed_seconds"))}</td>'
            f'<td class="mono">{_fmt_secs(j.get("eta_seconds"))}</td>'
            f'<td>{_esc(j.get("key_name"))}</td>'
            f'<td class="mono small">{_esc(j.get("prompt_truncated") or "")}</td>'
            f'<td class="mono small">{_esc(j.get("prompt_id"))}</td>'
            f'<td class="err small">{_esc(j.get("error") or "")}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_comfy_queue(p: dict) -> str:
    q = p.get("comfy_queue")
    if not q:
        return '<div class="muted">unavailable</div>'
    return (
        f'<div>'
        f'<span class="pill ok">{_esc(q.get("running"))} running</span> '
        f'<span class="pill">{_esc(q.get("pending"))} pending</span>'
        f'</div>'
        f'<div class="mono small muted">running_ids={_esc(q.get("running_ids") or [])} '
        f'pending_ids={_esc(q.get("pending_ids") or [])}</div>'
    )


def _render_swap_state(p: dict) -> str:
    """Collapse the swap-locks + ComfyUI websocket state to a single one-line
    summary when everything is quiet. Only spell out the rows when something
    is actually in-progress or disconnected — a status page should reward the
    common-case glance.
    """
    s = p.get("swap_state") or {}
    ws = p.get("comfy_ws_state") or {}
    tabby = bool(s.get("tabby_swap_in_progress"))
    kobold = bool(s.get("kobold_swap_in_progress"))
    ws_connected = bool(ws.get("connected"))
    all_quiet = (not tabby) and (not kobold) and ws_connected

    if all_quiet:
        client_id = ws.get("client_id")
        cid_html = (
            f' <span class="muted small" title="ComfyUI client_id">'
            f'(ws client {_esc(str(client_id)[:12])}…)</span>'
            if client_id else ""
        )
        return (
            '<div><span class="pill ok">all swap locks idle, ComfyUI ws '
            f'connected</span>{cid_html}</div>'
        )

    rows: list[tuple[str, Any]] = []
    if tabby:
        rows.append(("Tabby model swap", "in progress"))
    if kobold:
        rows.append(("Kobold model swap", "in progress"))
    if not ws_connected:
        rows.append(("ComfyUI websocket", "disconnected"))
    rows.append(("ComfyUI client_id", ws.get("client_id")))
    return _kv_table(rows)


def _render_gpus(gpus: list[dict]) -> str:
    if not gpus:
        return '<div class="muted">unavailable</div>'
    parts = ['<div class="grid">']
    for g in gpus:
        used = g.get("memory_used_mb") or 0
        total = g.get("memory_total_mb") or 0
        pct = (used / total * 100.0) if total else 0
        bar_cls = "bad" if pct > 90 else ("warn" if pct > 70 else "ok")
        wl = g.get("active_workload") or {}
        throttle_hex = g.get("throttle_reasons_hex")
        throttle_cls = "bad" if _throttle_is_real(throttle_hex) else "muted"
        card_cls = "card bad" if _throttle_is_real(throttle_hex) else "card"
        parts.append(
            f'<div class="{card_cls}">'
            f'<div><b>GPU{_esc(g.get("index"))}</b> <span class="muted small">{_esc(g.get("name"))}</span></div>'
            f'<div class="mono small">{used:,} / {total:,} MiB ({pct:.1f}%)</div>'
            f'<div class="bar {bar_cls}"><span style="width:{min(100, pct):.1f}%"></span></div>'
            f'<div class="mono small">temp={_esc(g.get("temp_c"))}°C · '
            f'{_render_power(g)} · '
            f'fan={_esc(g.get("fan_speed_pct"))}%</div>'
            f'<div class="mono small">clocks gr/mem={_esc(g.get("clocks_gr_mhz"))}/{_esc(g.get("clocks_mem_mhz"))} MHz · '
            f'{_render_pcie(g)}</div>'
            f'<div class="mono small {throttle_cls}">throttle: {_decode_throttle(throttle_hex)}</div>'
            f'<div class="small muted">{_esc(wl.get("label"))}: {_esc(wl.get("detail") or "")}</div>'
            f'</div>'
        )
    parts.append("</div>")
    return "".join(parts)


def _vram_row_label(s: dict) -> str:
    """Render the Service column with synthesized-row styling and spillover note."""
    name = s.get("service") or "?"
    if name == "__unattributed":
        return (
            '<span class="muted" title="processes hold /dev/nvidia* via DRM '
            "(Xwayland, plasmashell, browsers, etc.) but don't appear in "
            'nvidia-smi --query-compute-apps because they have no CUDA '
            'context">[graphics + system, no CUDA context]</span>'
        )
    if name == "__headroom":
        cls = "bad" if s.get("headroom_short") else "muted"
        return f'<span class="{cls}" title="free VRAM (total − used)">[headroom]</span>'
    if name == "__total":
        return '<b title="GPU memory.used (authoritative)">[total]</b>'
    if s.get("spillover"):
        primary = s.get("spillover_from_gpu")
        suffix = (
            ' <span class="muted small" title="tensor-split — VRAM is '
            f'governed by GPU{primary}\'s budget for this PID">'
            '(tensor-split spillover'
            + (f' from GPU{primary}' if primary is not None else '')
            + ')</span>'
        )
        return f"{_esc(name)}{suffix}"
    return _esc(name)


def _render_desktop_contributors(top: list[dict]) -> str:
    if not top:
        return ""
    items = []
    for c in top:
        items.append(
            f'<li><span class="mono">{_esc(c.get("comm"))}</span> '
            f'(pid {_esc(c.get("pid"))}) '
            f'<span class="mono small">{_esc(c.get("used_mb"))} MiB</span></li>'
        )
    return (
        '<details class="small"><summary class="muted">'
        f'top {len(top)} desktop contributor(s)</summary>'
        f'<ul style="margin:.25em 0 .25em 1.25em">{"".join(items)}</ul>'
        '</details>'
    )


def _render_vram_attribution(va: dict) -> str:
    per_gpu = va.get("per_gpu") or []
    if not per_gpu:
        return '<div class="muted">unavailable</div>'
    parts = []
    for g in per_gpu:
        services = g.get("services") or []
        used_mb = g.get("memory_used_mb")
        total_mb = g.get("memory_total_mb")

        # Compute overall GPU utilization for the header (replaces the
        # redundant __total table row).
        try:
            util_pct = (float(used_mb) / float(total_mb) * 100.0) if total_mb else None
        except Exception:
            util_pct = None
        if util_pct is None:
            util_html = ""
        else:
            if util_pct >= 90:
                util_cls = "bad"
            elif util_pct >= 70:
                util_cls = "warn"
            else:
                util_cls = "ok"
            try:
                used_gb = float(used_mb) / 1024.0
                total_gb = float(total_mb) / 1024.0
                util_html = (
                    f' <span class="pill {util_cls}" title="overall GPU memory utilization">'
                    f'{used_gb:.2f} / {total_gb:.0f} GiB · {util_pct:.0f}%'
                    f'</span>'
                )
            except Exception:
                util_html = (
                    f' <span class="pill {util_cls}">{util_pct:.0f}%</span>'
                )

        rows = [
            f'<h3>GPU{_esc(g.get("index"))} '
            f'({_esc(used_mb)}/{_esc(total_mb)} MiB){util_html}</h3>',
            '<table><thead><tr>'
            '<th>Service</th><th>Used MiB</th><th>Used GiB</th>'
            '<th>Budget GB</th><th>% of budget</th><th>Over?</th>'
            '</tr></thead><tbody>'
        ]
        for s in services:
            name = s.get("service") or ""
            # __total is redundant with the GPU section header — skip it.
            if name == "__total":
                continue
            over = s.get("over_budget")
            pct = s.get("pct_of_budget") or 0

            row_classes: list[str] = []
            if name == "__unattributed":
                row_classes.append("muted")
            row_attr = (
                f' class="{" ".join(row_classes)}"' if row_classes else ""
            )

            # Pill class:
            #  - real services: bad if over_budget, warn if >80% of budget, else ok.
            #  - __headroom: % is meaningless against a reserve floor; suppressed.
            #  - __unattributed: muted (no budget meaning).
            if name == "__unattributed":
                pill_cls = "muted"
            elif name == "__headroom":
                pill_cls = "muted"
            else:
                pill_cls = "bad" if over else ("warn" if pct > 80 else "ok")

            # Drop the % cell entirely for __headroom — comparing free VRAM
            # to a reserve-minimum floor produces nonsense like 539%. The
            # absolute MiB/GiB columns already carry the signal.
            if name == "__headroom":
                pct_cell = '<span class="muted small">n/a</span>'
            elif s.get("pct_of_budget") is not None:
                pct_cell = (
                    f'<span class="pill {pill_cls}">{_esc(s.get("pct_of_budget"))}%</span>'
                )
            else:
                pct_cell = '<span class="pill muted">—</span>'

            over_cell = ""
            if name == "__unattributed":
                over_cell = ""
            elif name == "__headroom" and s.get("headroom_short"):
                over_cell = '<span class="bad">SHORT</span>'
            elif over:
                over_cell = '<span class="bad">YES</span>'

            # Headroom's "budget" is actually a reserve floor — relabel
            # inline so it isn't read as a ceiling.
            budget_html = _esc(s.get("budget_gb"))
            if name == "__headroom" and s.get("budget_gb") is not None:
                budget_html = (
                    f'{_esc(s.get("budget_gb"))} '
                    '<span class="muted small" title="floor, not a ceiling">'
                    '(reserved minimum)</span>'
                )

            label_html = _vram_row_label(s)
            sub = ""
            if name == "desktop":
                sub = _render_desktop_contributors(s.get("top_contributors") or [])

            rows.append(
                f"<tr{row_attr}>"
                f"<td>{label_html}{sub}</td>"
                f'<td class="mono">{_esc(s.get("used_mb"))}</td>'
                f'<td class="mono">{_esc(s.get("used_gb"))}</td>'
                f'<td class="mono">{budget_html}</td>'
                f'<td class="mono">{pct_cell}</td>'
                f"<td>{over_cell}</td>"
                f"</tr>"
            )
        rows.append("</tbody></table>")
        parts.append("".join(rows))

    # Multi-GPU PIDs (tensor-split / NVLink): only render when populated.
    # The tensor-split spillover row inside each GPU table already annotates
    # the same fact for the common single-PID case.
    multi = va.get("multi_gpu_pids") or {}
    if multi:
        parts.append(
            '<details class="small"><summary>Multi-GPU PIDs (tensor-split / NVLink)</summary>'
            '<pre>' + _esc(json.dumps(multi, indent=2)) + '</pre></details>'
        )

    pids = va.get("service_pids") or {}
    parts.append(
        '<details><summary>Service PIDs</summary><pre>'
        + _esc(json.dumps(pids, indent=2))
        + '</pre></details>'
    )
    return "".join(parts)


def _render_budgets(b: dict) -> str:
    parts = []
    g0 = b.get("gpu0") or {}
    g1 = b.get("gpu1") or {}
    img = b.get("images") or {}
    parts.append(_kv_table([
        ("GPU0 total GB", g0.get("total_gb")),
        ("GPU0 desktop GB", g0.get("desktop_gb")),
        ("GPU0 TTS GB", g0.get("tts_gb")),
        ("GPU0 ComfyUI GB", g0.get("comfyui_gb")),
        ("GPU0 headroom GB", g0.get("headroom_gb")),
        ("GPU1 total GB", g1.get("total_gb")),
        ("GPU1 LLM GB", g1.get("llm_gb")),
        ("GPU1 headroom GB", g1.get("headroom_gb")),
        ("Image max_batch_size", img.get("max_batch_size")),
        ("Image VRAM reserve", _bytes(img.get("vram_reserve_bytes"))),
        ("Image signed_url_ttl (s)", img.get("signed_url_ttl")),
        ("Image cache TTL (s)", img.get("cache_ttl")),
        ("Image cache eviction interval (s)", img.get("cache_eviction_interval")),
        ("Image cache dir", img.get("cache_dir")),
    ]))
    return "".join(parts)


def _render_vram_estimates(b: dict) -> str:
    vr = b.get("vram_estimates")
    if not vr:
        return '<div class="muted">no measurements cached</div>'
    parts = [
        f'<div class="muted small">{_esc(vr.get("total_entries"))} measurements; '
        f'cache mtime {_ago(vr.get("last_modified"))}</div>'
    ]
    reason = vr.get("unavailable_reason")
    if reason:
        # Don't show numbers we don't trust — explain why and link the fix
        # back to proxy.py.
        parts.append(
            f'<div class="bad" style="margin:.5em 0">'
            f'<b>Measurement unavailable:</b> {_esc(reason)}</div>'
        )
        parts.append(
            '<details><summary class="muted small">'
            'show raw cached values (do not trust)</summary>'
        )
    parts.append(
        '<table><thead><tr><th>Workflow:size</th><th>Peak VRAM</th>'
        '</tr></thead><tbody>'
    )
    for e in vr.get("entries") or []:
        parts.append(
            f'<tr><td class="mono">{_esc(e.get("key"))}</td>'
            f'<td class="mono">{_bytes(e.get("peak_bytes"))}</td></tr>'
        )
    parts.append("</tbody></table>")
    if reason:
        parts.append("</details>")
    return "".join(parts)


def _render_models(m: dict, gpus: Optional[list] = None) -> str:
    # Tensor-split: the raw value is a *layer count* split (e.g. "52,12" =
    # 52 layers on GPU0, 12 on GPU1). It does NOT translate directly to
    # VRAM share — KV cache, embeddings, and per-layer sizes vary. Show the
    # raw split with an explicit layer-count interpretation, then the live
    # per-GPU VRAM (if available) so the reader doesn't conflate the two.
    tensor_split_info = m.get("tensor_split_info") or {}
    raw_split = (tensor_split_info.get("raw") if tensor_split_info else None) or m.get("tensor_split")
    weights = (tensor_split_info.get("weights") if tensor_split_info else None) or []
    if raw_split:
        try:
            parts_int = [int(round(float(w))) for w in (weights or [])]
        except Exception:
            parts_int = []
        if parts_int:
            layer_breakdown = ", ".join(
                f"{n} layers on GPU{i}" for i, n in enumerate(parts_int)
            )
        else:
            layer_breakdown = ""
        # Live per-GPU VRAM (used by the LLM PID, when available). We
        # compute total memory.used per GPU as a proxy — this overstates a
        # bit if other services share a card, but it's the directionally
        # correct number for "where is the LLM weight today".
        live_bits: list[str] = []
        for g in (gpus or []):
            try:
                idx = g.get("index")
                used_mb = float(g.get("memory_used_mb") or 0)
                live_bits.append(f"GPU{idx} {used_mb / 1024.0:.1f} GiB")
            except Exception:
                continue
        live_html = (
            f'<div class="muted small">Effective VRAM (live): '
            f'{_esc(" / ".join(live_bits))}</div>'
            if live_bits else ""
        )
        layer_html = (
            f'<div class="muted small">{_esc(layer_breakdown)}</div>'
            if layer_breakdown else ""
        )
        ts_display = (
            f'<span class="mono">{_esc(raw_split)}</span>'
            f'{layer_html}'
            f'{live_html}'
            '<div class="muted small">'
            'Note: layer-count split &ne; VRAM split (KV cache, embeddings vary)'
            '</div>'
        )
    else:
        ts_display = "—"

    # --parallel: show effective integer + provenance when defaulted.
    parallel_value = m.get("parallel_arg")
    parallel_source = m.get("parallel_arg_source") or ""
    if parallel_value is not None:
        parallel_display = (
            f"{_esc(parallel_value)}"
            + (
                f' <span class="muted small">({_esc(parallel_source)})</span>'
                if parallel_source
                else ""
            )
        )
    else:
        parallel_display = "—"

    # TTS: render service unit / engine / model as labeled rows.
    tts = m.get("tts") or {}
    tts_unit = tts.get("unit") or m.get("active_tts_engine")
    tts_engine = tts.get("engine")
    tts_model = tts.get("model")
    tts_exec = tts.get("exec_start")

    out = ['<table class="kv">']
    base_rows = [
        ("Loaded LLM name", m.get("loaded_llm_name")),
        ("Loaded LLM path", m.get("loaded_llm_path")),
        ("mmproj path", m.get("mmproj_path")),
        ("Vision capable", m.get("vision_capable")),
        ("Vision source", m.get("vision_source")),
        ("Context size", m.get("ctx_size")),
        ("Context used (max n_past)", m.get("ctx_used")),
        ("Total slots", m.get("total_slots")),
        ("Active slots", m.get("active_slots")),
        ("Speculative type", m.get("speculative_type")),
        ("Cache type K", m.get("cache_type_k")),
        ("Cache type V", m.get("cache_type_v")),
    ]
    for k, v in base_rows:
        if v is None or v == "":
            v_html = "—"
        elif isinstance(v, (list, dict)):
            v_html = "<code>" + _esc(json.dumps(v, default=str)[:300]) + "</code>"
        else:
            v_html = _esc(v)
        out.append(f"<tr><th>{_esc(k)}</th><td>{v_html}</td></tr>")
    out.append(f"<tr><th>Tensor split</th><td>{ts_display}</td></tr>")
    out.append(f"<tr><th>--parallel arg (effective)</th><td>{parallel_display}</td></tr>")
    distinct = {x for x in (tts_unit, tts_engine, tts_model) if x}
    if len(distinct) <= 1 and tts_unit:
        out.append(
            f'<tr><th>Active TTS</th><td class="mono">{_esc(tts_unit)}</td></tr>'
        )
    else:
        out.append(
            f'<tr><th>TTS service unit</th>'
            f'<td class="mono">{_esc(tts_unit) if tts_unit else "—"}</td></tr>'
        )
        out.append(
            f'<tr><th>TTS engine (framework)</th>'
            f'<td class="mono">{_esc(tts_engine) if tts_engine else "—"}</td></tr>'
        )
        out.append(
            f'<tr><th>TTS model</th>'
            f'<td class="mono">{_esc(tts_model) if tts_model else "—"}</td></tr>'
        )
    if tts_exec:
        out.append(
            f'<tr><th>TTS ExecStart</th>'
            f'<td class="small mono">{_esc(tts_exec)}</td></tr>'
        )
    img_workflows = m.get("image_workflows")
    if img_workflows:
        out.append(
            "<tr><th>Image workflows</th><td><code>"
            f"{_esc(json.dumps(img_workflows)[:300])}</code></td></tr>"
        )
    vid_aliases = m.get("video_aliases")
    if vid_aliases:
        out.append(
            "<tr><th>Video aliases</th><td><code>"
            f"{_esc(json.dumps(vid_aliases)[:300])}</code></td></tr>"
        )
    out.append("</table>")
    return "".join(out)


def _render_slots(slots: list[dict]) -> str:
    if not slots:
        return '<div class="muted">no slots reported</div>'
    rows = ['<table><thead><tr><th>id</th><th>state</th><th>n_ctx</th><th>n_past</th><th>n_decoded</th><th>id_task</th><th>prompt %</th><th>speculative</th></tr></thead><tbody>']
    for s in slots:
        cls = "warn" if s.get("is_processing") else "muted"
        rows.append(
            f'<tr><td>{_esc(s.get("id"))}</td>'
            f'<td><span class="pill {cls}">{_esc(s.get("state"))}</span></td>'
            f'<td class="mono">{_esc(s.get("n_ctx"))}</td>'
            f'<td class="mono">{_esc(s.get("n_past"))}</td>'
            f'<td class="mono">{_esc(s.get("n_decoded"))}</td>'
            f'<td>{_esc(s.get("id_task"))}</td>'
            f'<td>{_esc(s.get("prompt_processing_progress"))}</td>'
            f'<td>{_esc(s.get("speculative"))}</td></tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_aux_health(aux: dict) -> str:
    """Render Voxtral (TTS) and Parakeet (STT) as small cards with labeled
    rows instead of dumping raw JSON. Falls back to a JSON pre-block when
    a payload doesn't fit the expected schema (forward-compatibility).
    """

    def _status_pill(status: Any) -> str:
        s = str(status or "").lower()
        cls = {"ok": "ok", "healthy": "ok", "degraded": "warn",
               "down": "bad", "error": "bad"}.get(s, "muted")
        return f'<span class="pill {cls}">{_esc(status or "—")}</span>'

    def _card(title: str, body: str) -> str:
        return (
            f'<div class="card" style="display:inline-block; vertical-align:top; '
            f'min-width:280px; margin-right:8px; margin-bottom:8px;">'
            f'<h3 style="margin-top:0">{_esc(title)}</h3>{body}</div>'
        )

    parts: list[str] = []

    vox = aux.get("voxtral")
    if isinstance(vox, dict):
        rows = [
            f'<tr><th>Status</th><td>{_status_pill(vox.get("status"))}</td></tr>',
            f'<tr><th>Model</th><td class="mono small">{_esc(vox.get("model") or "—")}</td></tr>',
        ]
        vram = vox.get("vram_gb")
        if vram is not None:
            rows.append(
                f'<tr><th>VRAM</th><td class="mono small">{_esc(vram)} GB</td></tr>'
            )
        for k, v in vox.items():
            if k in ("status", "model", "vram_gb"):
                continue
            rows.append(
                f'<tr><th>{_esc(k)}</th>'
                f'<td class="mono small">{_esc(v)}</td></tr>'
            )
        parts.append(_card("Voxtral (TTS)",
                          f'<table class="kv">{"".join(rows)}</table>'))
    elif vox is not None:
        parts.append(_card("Voxtral (TTS)",
                          f'<pre class="small">{_esc(json.dumps(vox, indent=2))}</pre>'))

    pk = aux.get("parakeet")
    if isinstance(pk, dict):
        rows = [
            f'<tr><th>Status</th><td>{_status_pill(pk.get("status"))}</td></tr>',
            f'<tr><th>Default model</th>'
            f'<td class="mono small">{_esc(pk.get("default_model") or "—")}</td></tr>',
        ]
        if pk.get("speedup") is not None:
            rows.append(
                f'<tr><th>Speedup</th>'
                f'<td class="mono small">{_esc(pk.get("speedup"))}</td></tr>'
            )
        models = pk.get("models") or []
        if isinstance(models, list) and models:
            rows.append(
                f'<tr><th>Available models</th><td>'
                f'<details><summary class="muted small">'
                f'{len(models)} model(s)</summary>'
                f'<ul style="margin:.25em 0 .25em 1em">'
                + "".join(
                    f'<li class="mono small">{_esc(m)}</li>' for m in models
                )
                + '</ul></details></td></tr>'
            )
        for k, v in pk.items():
            if k in ("status", "default_model", "speedup", "models"):
                continue
            rows.append(
                f'<tr><th>{_esc(k)}</th>'
                f'<td class="mono small">{_esc(v)}</td></tr>'
            )
        parts.append(_card("Parakeet (STT)",
                          f'<table class="kv">{"".join(rows)}</table>'))
    elif pk is not None:
        parts.append(_card("Parakeet (STT)",
                          f'<pre class="small">{_esc(json.dumps(pk, indent=2))}</pre>'))

    if not parts:
        return '<div class="muted">unavailable</div>'
    return '<div>' + "".join(parts) + '</div>'


def _render_voices(v: Optional[dict]) -> str:
    if not v:
        return '<div class="muted">unavailable</div>'
    sources = v.get("sources") or {}
    openai_map = v.get("openai_alias_map") or {}
    native = v.get("native_voices") or []
    parts: list[str] = []
    parts.append(_kv_table([
        ("Voice count", v.get("count")),
        ("Languages", v.get("languages")),
        ("Genders", v.get("genders")),
        ("Source breakdown",
            f"{sources.get('openai_alias', 0)} OpenAI alias / "
            f"{sources.get('voxtral_native', 0)} Voxtral native"
            if sources else None),
        ("List endpoint", v.get("list_endpoint")),
    ]))
    if openai_map:
        rows = [
            '<details><summary class="muted small">'
            f'OpenAI alias → Voxtral mapping ({len(openai_map)})'
            '</summary>'
            '<table><thead><tr><th>OpenAI alias</th><th>→</th><th>Voxtral voice</th>'
            '</tr></thead><tbody>'
        ]
        for alias, mapped in list(openai_map.items())[:20]:
            rows.append(
                f'<tr><td class="mono">{_esc(alias)}</td>'
                f'<td class="muted">→</td>'
                f'<td class="mono">{_esc(mapped)}</td></tr>'
            )
        rows.append('</tbody></table></details>')
        parts.append("".join(rows))
    if native:
        parts.append(
            '<details><summary class="muted small">'
            f'Native Voxtral voices ({len(native)})</summary>'
            f'<pre class="small">{_esc(", ".join(native))}</pre>'
            '</details>'
        )
    curl = v.get("example_curl")
    if curl:
        parts.append(
            '<details><summary class="muted small">Example curl (TTS)</summary>'
            f'<pre class="small">{_esc(curl)}</pre>'
            '</details>'
        )
    return "".join(parts)


def _format_top_classes(top: dict) -> str:
    """Render a top-3 class-type dict as compact pills (e.g. ``CLIPTextEncode×2``)."""
    if not top:
        return '<span class="muted">—</span>'
    parts = []
    for k, v in list(top.items())[:3]:
        parts.append(
            f'<span class="pill small">{_esc(k)}<span class="muted">×{_esc(v)}</span></span>'
        )
    return " ".join(parts)


def _render_video_caps(caps: list[dict]) -> str:
    if not caps:
        return '<div class="muted">no aliases</div>'
    rows = [
        '<table><thead><tr>'
        '<th>Alias</th><th>Workflow</th><th>Min/Max sec</th><th>Max size</th>'
        '<th>FPS</th><th>I2V</th><th>Audio</th><th>Two-stage</th>'
        '<th>Nodes</th><th>Top class types</th><th>Full</th>'
        '</tr></thead><tbody>'
    ]
    for c in caps:
        # Prefer structured top_class_types (3) and node_count; fall back to
        # the legacy class_types dict.
        top = c.get("top_class_types") or c.get("class_types") or {}
        all_ct = c.get("all_class_types") or c.get("class_types") or {}
        node_count = c.get("node_count")
        if node_count is None and isinstance(c.get("class_types"), dict):
            node_count = sum(c["class_types"].values())
        details_html = (
            '<details><summary class="muted small">show</summary>'
            f'<pre class="small">{_esc(json.dumps(all_ct, indent=2))}</pre>'
            '</details>'
        ) if all_ct else '<span class="muted">—</span>'
        rows.append(
            f'<tr>'
            f'<td class="mono">{_esc(c.get("alias"))}</td>'
            f'<td class="mono">{_esc(c.get("workflow"))}</td>'
            f'<td>{_esc(c.get("min_seconds"))} / {_esc(c.get("max_seconds"))}</td>'
            f'<td class="mono">{_esc(c.get("max_size"))}</td>'
            f'<td>{_esc(c.get("frame_rate"))}</td>'
            f'<td>{_esc("yes" if c.get("supports_i2v") else "no")}</td>'
            f'<td>{_esc("yes" if c.get("supports_audio") else "no")}</td>'
            f'<td>{_esc("yes" if c.get("two_stage") else "no")}</td>'
            f'<td class="mono">{_esc(node_count) if node_count is not None else "—"}</td>'
            f'<td>{_format_top_classes(top)}</td>'
            f'<td>{details_html}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_image_caps(caps: list[dict]) -> str:
    if not caps:
        return '<div class="muted">no image workflows</div>'
    rows = [
        '<table><thead><tr>'
        '<th>Workflow</th><th>Edit?</th><th>Nodes</th>'
        '<th>Top class types</th><th>Full</th>'
        '</tr></thead><tbody>'
    ]
    for c in caps:
        top = c.get("top_class_types") or {}
        all_ct = c.get("all_class_types") or top
        details_html = (
            '<details><summary class="muted small">show</summary>'
            f'<pre class="small">{_esc(json.dumps(all_ct, indent=2))}</pre>'
            '</details>'
        ) if all_ct else '<span class="muted">—</span>'
        rows.append(
            f'<tr>'
            f'<td class="mono">{_esc(c.get("name"))}</td>'
            f'<td>{_esc("yes" if c.get("supports_edit") else "no")}</td>'
            f'<td class="mono">{_esc(c.get("class_type_count"))}</td>'
            f'<td>{_format_top_classes(top)}</td>'
            f'<td>{details_html}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_prompt_cell(text: Optional[str], *, max_len: int = 80) -> str:
    """Render a prompt cell: truncated display, full prompt on hover.

    Empty/None renders as a muted em-dash so the column stays aligned.
    """
    if not text or not isinstance(text, str):
        return '<span class="muted">—</span>'
    text = text.strip()
    if not text:
        return '<span class="muted">—</span>'
    short = text if len(text) <= max_len else (text[: max_len - 1] + "…")
    return f'<span class="small" title="{_esc(text)}">{_esc(short)}</span>'


def _render_recent_videos(v: list[dict]) -> str:
    if not v:
        return '<div class="muted">no videos</div>'
    rows = [
        '<table><thead><tr>'
        '<th>Filename</th><th>Alias</th><th>Duration</th><th>Dimensions</th>'
        '<th>Audio</th><th>Size</th><th>Age</th><th>Prompt</th>'
        '</tr></thead><tbody>'
    ]
    for r in v:
        dur = r.get("duration_s")
        dur_html = f'{_esc(dur)}s' if dur is not None else '<span class="muted">—</span>'
        w, h = r.get("width"), r.get("height")
        dims_html = (
            f'{_esc(w)}×{_esc(h)}' if (w and h) else '<span class="muted">—</span>'
        )
        audio = r.get("has_audio")
        if audio is True:
            audio_html = "yes"
        elif audio is False:
            audio_html = "no"
        else:
            audio_html = '<span class="muted">—</span>'
        err = r.get("probe_error")
        err_html = (
            f' <span class="muted small" title="{_esc(err)}">probe-err</span>'
            if err else ""
        )
        path_attr = _esc(r.get("path") or "")
        prompt_html = _render_prompt_cell(r.get("prompt"))
        rows.append(
            f'<tr>'
            f'<td class="mono" title="{path_attr}">{_esc(r.get("filename"))}{err_html}</td>'
            f'<td class="mono">{_esc(r.get("alias") or "—")}</td>'
            f'<td class="mono">{dur_html}</td>'
            f'<td class="mono">{dims_html}</td>'
            f'<td>{audio_html}</td>'
            f'<td class="mono">{_bytes(r.get("size_bytes"))}</td>'
            f'<td title="{_esc(_abs_utc(r.get("mtime")))}">{_ago(r.get("mtime"))}</td>'
            f'<td>{prompt_html}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_recent_images(v: list[dict]) -> str:
    if not v:
        return '<div class="muted">no images</div>'
    rows = [
        '<table><thead><tr>'
        '<th>Filename</th><th>Dimensions</th><th>Size</th><th>Age</th><th>Prompt</th>'
        '</tr></thead><tbody>'
    ]
    for r in v:
        w, h = r.get("width"), r.get("height")
        dims_html = (
            f'{_esc(w)}×{_esc(h)}' if (w and h) else '<span class="muted">—</span>'
        )
        err = r.get("probe_error")
        err_html = (
            f' <span class="muted small" title="{_esc(err)}">probe-err</span>'
            if err else ""
        )
        path_attr = _esc(r.get("path") or "")
        prompt_html = _render_prompt_cell(r.get("prompt"))
        rows.append(
            f'<tr>'
            f'<td class="mono" title="{path_attr}">{_esc(r.get("filename"))}{err_html}</td>'
            f'<td class="mono">{dims_html}</td>'
            f'<td class="mono">{_bytes(r.get("size_bytes"))}</td>'
            f'<td title="{_esc(_abs_utc(r.get("mtime")))}">{_ago(r.get("mtime"))}</td>'
            f'<td>{prompt_html}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_auth_keys(keys: list[dict], *, window: Optional[dict] = None) -> str:
    if not keys:
        return '<div class="muted">no keys configured</div>'
    header = ""
    if window:
        ws = window.get("window_started_at")
        ps = window.get("process_started_at")
        ws_h = _ago(ws) if ws else "—"
        ps_h = _ago(ps) if ps else "—"
        header = (
            '<div class="small muted" style="margin-bottom:6px;">'
            f'window started {_esc(ws_h)} · process started {_esc(ps_h)} '
            f'· max window {(window.get("window_max_s") or 0) // 3600}h'
            '</div>'
        )
    rows = [
        header,
        '<table><thead><tr><th>Key name</th><th>Active?</th><th>Last seen</th>'
        '<th>Reqs</th><th>4xx</th><th>5xx</th><th>Latency p50</th><th>Latency p95</th>'
        '<th>Top endpoints</th></tr></thead><tbody>'
    ]
    for k in keys:
        active = k.get("active")
        cls = "ok" if active else "muted"
        rows.append(
            f'<tr>'
            f'<td>{_esc(k.get("name"))}</td>'
            f'<td><span class="pill {cls}">{_esc("yes" if active else "no")}</span></td>'
            f'<td>{_ago(k.get("last_seen"))}</td>'
            f'<td class="mono">{_esc(k.get("requests_24h"))}</td>'
            f'<td class="mono">{_esc(k.get("errors_4xx_24h"))}</td>'
            f'<td class="mono">{_esc(k.get("errors_5xx_24h"))}</td>'
            f'<td class="mono">{_esc(k.get("latency_ms_p50"))}</td>'
            f'<td class="mono">{_esc(k.get("latency_ms_p95"))}</td>'
            f'<td class="small mono">{_esc(json.dumps(k.get("top_endpoints") or []))}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_recent_events(events: list[dict]) -> str:
    if not events:
        return '<div class="muted">no events captured yet</div>'
    # Sort by ts desc (snapshot_recent_events already returns descending, but
    # be defensive: replayed events may have arrived out of order).
    events_sorted = sorted(events, key=lambda e: -(e.get("ts") or 0))
    rows = ['<table><thead><tr><th>Time</th><th>Type</th><th>Summary</th><th>Detail</th></tr></thead><tbody>']
    for e in events_sorted:
        ts = e.get("ts")
        details = {k: v for k, v in e.items() if k not in ("ts", "type", "summary")}
        rows.append(
            f'<tr>'
            f'<td title="{_esc(_abs_utc(ts))}">{_ago(ts)}</td>'
            f'<td><span class="pill">{_esc(e.get("type"))}</span></td>'
            f'<td>{_esc(e.get("summary"))}</td>'
            f'<td class="small mono">{_esc(json.dumps(details, default=str))}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_disk(disk: dict) -> str:
    parts = ['<h3>Partitions</h3>']
    parts.append('<table><thead><tr><th>Mount</th><th>Source</th><th>Used</th><th>Avail</th><th>Size</th><th>%</th></tr></thead><tbody>')
    for p in disk.get("partitions") or []:
        cls = _pct_class(p.get("pct_used"), warn=85, crit=95)
        parts.append(
            f'<tr>'
            f'<td class="mono">{_esc(p.get("target"))}</td>'
            f'<td class="mono">{_esc(p.get("source"))}</td>'
            f'<td class="mono">{_bytes(p.get("used_bytes"))}</td>'
            f'<td class="mono">{_bytes(p.get("avail_bytes"))}</td>'
            f'<td class="mono">{_bytes(p.get("size_bytes"))}</td>'
            f'<td><span class="pill {cls}">{_esc(p.get("pct_used"))}%</span></td>'
            f'</tr>'
        )
    parts.append('</tbody></table>')
    parts.append('<h3>Hot-spot directories <span class="muted small">(comfy_output_root contains comfy_output_video; sizes overlap)</span></h3>')
    parts.append('<table><thead><tr><th>Name</th><th>Path</th><th>Files</th><th>Total size</th><th>Oldest</th><th>Newest</th><th>Biggest file</th></tr></thead><tbody>')
    for h in disk.get("hotspots") or []:
        parts.append(
            f'<tr>'
            f'<td>{_esc(h.get("name"))}</td>'
            f'<td class="mono small">{_esc(h.get("path"))}</td>'
            f'<td class="mono">{_esc(h.get("file_count"))}</td>'
            f'<td class="mono">{_bytes(h.get("size_bytes"))}</td>'
            f'<td title="{_esc(_abs_utc(h.get("oldest_mtime")))}">{_ago(h.get("oldest_mtime"))}</td>'
            f'<td title="{_esc(_abs_utc(h.get("newest_mtime")))}">{_ago(h.get("newest_mtime"))}</td>'
            f'<td class="small mono">{_esc(h.get("biggest_file") or "—")} <span class="muted">({_bytes(h.get("biggest_file_size_bytes"))})</span></td>'
            f'</tr>'
        )
    parts.append('</tbody></table>')
    non_overlap = disk.get("hotspots_total_non_overlapping_bytes")
    if non_overlap is not None:
        parts.append(
            f'<div class="small muted">Total non-overlapping: <b>{_bytes(non_overlap)}</b> '
            f'(unique on-disk bytes across hot-spot paths above)</div>'
        )
    parts.append(
        f'<div class="small muted">journal disk usage: {_bytes(disk.get("journal_disk_usage_bytes"))}</div>'
    )
    return "".join(parts)


def _render_image_cache(c: Optional[dict]) -> str:
    if not c:
        return '<div class="muted">unavailable</div>'
    return (
        '<table class="kv">'
        f'<tr><th>Path</th><td class="mono">{_esc(c.get("path"))}</td></tr>'
        f'<tr><th>Files</th><td class="mono">{_esc(c.get("file_count"))}</td></tr>'
        f'<tr><th>Total size</th><td class="mono">{_bytes(c.get("size_bytes"))}</td></tr>'
        f'<tr><th>Oldest</th><td>{_ago_with_abs(c.get("oldest_mtime"))}</td></tr>'
        f'<tr><th>Newest</th><td>{_ago_with_abs(c.get("newest_mtime"))}</td></tr>'
        f'<tr><th>Biggest file</th><td class="small mono">{_esc(c.get("biggest_file") or "—")}</td></tr>'
        f'<tr><th>Biggest file size</th><td class="mono">{_bytes(c.get("biggest_file_size_bytes"))}</td></tr>'
        '</table>'
    )


def _render_cpu(cpu: dict) -> str:
    load = cpu.get("load") or []
    parts = [f'<div>load avg: <span class="mono">{_esc(load)}</span></div>']
    procs = cpu.get("top_procs") or []
    if procs:
        parts.append('<table><thead><tr><th>PID</th><th>User</th><th>CPU%</th><th>Mem%</th><th>Command</th></tr></thead><tbody>')
        for p in procs:
            parts.append(
                f'<tr>'
                f'<td class="mono">{_esc(p.get("pid"))}</td>'
                f'<td>{_esc(p.get("user"))}</td>'
                f'<td class="mono">{_esc(p.get("cpu_pct"))}</td>'
                f'<td class="mono">{_esc(p.get("mem_pct"))}</td>'
                f'<td class="small mono">{_esc(p.get("command"))}</td>'
                f'</tr>'
            )
        parts.append('</tbody></table>')
    return "".join(parts)


def _render_thermal(t: dict) -> str:
    return _kv_table([
        ("CPU max temp °C", t.get("cpu_max_c")),
        ("CPU thermal state", t.get("cpu_thermal_state")),
        ("DIMM max temp °C", t.get("dimm_max_c")),
        ("DIMM thermal state", t.get("dimm_thermal_state")),
        ("NVMe max temp °C", t.get("nvme_max_c")),
        ("AMDGPU edge temp °C", t.get("amdgpu_edge_c")),
        ("CPU temps detail", t.get("cpu_temps")),
        ("DIMM temps detail", t.get("dimm_temps")),
        ("Unavailable reason", t.get("unavailable_reason")),
    ])


def _render_comfy_view(c: Optional[dict]) -> str:
    if not c:
        return '<div class="muted">unavailable</div>'
    dev = c.get("device") or {}
    return _kv_table([
        ("ComfyUI version", c.get("comfyui_version")),
        ("Python", c.get("python_version")),
        ("PyTorch", c.get("pytorch_version")),
        ("RAM total", _bytes(c.get("ram_total_bytes"))),
        ("RAM free", _bytes(c.get("ram_free_bytes"))),
        ("argv", c.get("argv")),
        ("device[0] name", dev.get("name")),
        ("device[0] vram_total", _bytes(dev.get("vram_total"))),
        ("device[0] vram_free (Comfy view)", _bytes(dev.get("vram_free"))),
        ("device[0] torch_vram_total", _bytes(dev.get("torch_vram_total"))),
        ("device[0] torch_vram_free", _bytes(dev.get("torch_vram_free"))),
    ])


def _render_driver(d: dict) -> str:
    by24 = d.get("by_code_24h") or {}
    by60 = d.get("by_code_60min") or {}
    by24_pill = " ".join(f'<span class="pill">Xid {_esc(c)}: {_esc(n)}</span>' for c, n in by24.items()) or '<span class="muted">none</span>'
    by60_pill = " ".join(f'<span class="pill warn">Xid {_esc(c)}: {_esc(n)}</span>' for c, n in by60.items()) or '<span class="muted">none</span>'
    parts = [
        '<table class="kv">'
        f'<tr><th>Host uptime since</th><td class="mono">{_esc(d.get("uptime_since"))}</td></tr>'
        f'<tr><th>Proxy NRestarts (since unit start)</th><td class="mono">{_esc(d.get("proxy_restarts"))}</td></tr>'
        f'<tr><th>Xid events 24h</th><td>{by24_pill}</td></tr>'
        f'<tr><th>Xid events 60min</th><td>{by60_pill}</td></tr>'
        '</table>'
    ]
    last_evts = d.get("last_5_events") or []
    if last_evts:
        parts.append('<h3>Last 5 Xid events (parsed)</h3>')
        parts.append('<table><thead><tr><th>When</th><th>Code</th><th>Line</th></tr></thead><tbody>')
        for e in last_evts:
            parts.append(
                f'<tr>'
                f'<td class="mono">{_esc(e.get("ts"))}</td>'
                f'<td><span class="pill warn">{_esc(e.get("code"))}</span></td>'
                f'<td class="small mono">{_esc(e.get("line"))}</td>'
                f'</tr>'
            )
        parts.append('</tbody></table>')
    # Non-Xid kernel events only — Xid bursts surface above. This complements
    # with MCE / IOMMU / PCIe-AER / amdgpu lines + the lead WARNING of any
    # actual oops trace.
    parts.append('<h3>Recent kernel events (non-Xid: MCE / IOMMU / AER / amdgpu)</h3>')
    kr = d.get("kernel_recent") or []
    if not kr:
        parts.append('<div class="muted">none in 24h</div>')
    elif isinstance(kr[0], str):
        # Legacy shape preserved for backward compatibility.
        parts.append('<pre>' + _esc("\n".join(kr)) + '</pre>')
    else:
        # Structured shape: list[dict] with ``ts``, ``summary`` and optional
        # ``context`` (full register dump from an oops, hidden behind details).
        rows_html = [
            '<table><thead><tr>'
            '<th>When</th><th>Summary</th><th></th>'
            '</tr></thead><tbody>'
        ]
        for evt in kr:
            ts = evt.get("ts") or ""
            summ = evt.get("summary") or ""
            ctx = evt.get("context") or ""
            ctx_html = ""
            if ctx:
                ctx_html = (
                    '<details><summary class="muted small">full kernel '
                    'context</summary>'
                    f'<pre class="small">{_esc(ctx)}</pre></details>'
                )
            rows_html.append(
                f'<tr>'
                f'<td class="mono small">{_esc(ts)}</td>'
                f'<td class="small mono">{_esc(summ)}</td>'
                f'<td>{ctx_html}</td>'
                f'</tr>'
            )
        rows_html.append('</tbody></table>')
        parts.append("".join(rows_html))
    parts.append('<h3>Reboot history</h3>')
    parts.append(_render_reboot_history(d.get("reboot_history")))
    return "".join(parts)


def _render_reboot_history(history: Any) -> str:
    """Render structured reboot history (list of dicts) gracefully.

    Each row from ``status_endpoint._reboot_history`` carries:
      - ``index`` (0 = current boot, -1 = previous, ...)
      - ``boot_id`` (uuid; tooltipped, not displayed inline)
      - ``first_msg_ts`` / ``last_msg_ts`` (human strings)
      - ``first_msg_epoch`` / ``last_msg_epoch`` / ``duration_s``
      - ``is_current``
    Falls back to a ``<pre>`` dump when given the legacy list-of-strings.
    """
    if not history:
        return '<div class="muted">unavailable</div>'
    if isinstance(history, list) and history and isinstance(history[0], str):
        return '<pre>' + _esc("\n".join(history)) + '</pre>'

    def _fmt_dur(seconds: Optional[int]) -> str:
        if seconds is None:
            return ""
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60} min"
        if seconds < 86400:
            return f"{seconds / 3600:.1f}h"
        return f"~{seconds / 86400:.1f} days"

    rows: list[str] = []
    current = next(
        (r for r in history if isinstance(r, dict) and r.get("is_current")),
        None,
    )
    # Filter out boot-loop / recovery-cycle entries: any boot that lasted
    # less than 60s isn't normal operation. We keep a count of suppressed
    # entries so the user knows the table isn't lying.
    BOOT_LOOP_THRESHOLD_S = 60
    raw_others = [
        r for r in history
        if isinstance(r, dict) and not r.get("is_current")
    ]
    others = [
        r for r in raw_others
        if (r.get("duration_s") is None) or r.get("duration_s") >= BOOT_LOOP_THRESHOLD_S
    ]
    suppressed = len(raw_others) - len(others)

    if current:
        first = current.get("first_msg_ts") or "?"
        rel = (
            _ago(current.get("first_msg_epoch"))
            if current.get("first_msg_epoch") else ""
        )
        rows.append(
            '<table class="kv">'
            f'<tr><th>Current boot</th>'
            f'<td title="boot_id={_esc(current.get("boot_id"))}">'
            f'Started <span class="mono">{_esc(first)}</span>'
            + (f' <span class="muted">({_esc(rel)})</span>' if rel else '')
            + '</td></tr>'
            '</table>'
        )

    if others:
        rows.append(
            '<table><thead><tr>'
            '<th>Boot</th><th>Started</th><th>Ended</th><th>Duration</th>'
            '</tr></thead><tbody>'
        )
        for r in others:
            dur_html = (
                _esc(_fmt_dur(r.get("duration_s")))
                if r.get("duration_s") is not None else "—"
            )
            rows.append(
                f'<tr title="boot_id={_esc(r.get("boot_id"))}">'
                f'<td class="mono">{_esc(r.get("index"))}</td>'
                f'<td class="mono">{_esc(r.get("first_msg_ts") or "?")}</td>'
                f'<td class="mono">{_esc(r.get("last_msg_ts") or "?")}</td>'
                f'<td class="mono">{dur_html}</td>'
                f'</tr>'
            )
        rows.append('</tbody></table>')

    if suppressed:
        rows.append(
            f'<div class="muted small">'
            f'{suppressed} short boot(s) (&lt; {BOOT_LOOP_THRESHOLD_S}s) hidden — '
            f'likely boot loop / recovery cycles.'
            f'</div>'
        )

    return "".join(rows) if rows else '<div class="muted">unavailable</div>'


def _render_daisy(d: dict) -> str:
    last_mod = d.get("last_modified")
    # The audit log only writes on approval prompts. Absence isn't a problem
    # — it just means daisy hasn't asked to do anything risky in 24h. Surface
    # that as informational ("no approvals in 24h"), not as a "stale" warning.
    no_recent = bool(last_mod and (time.time() - float(last_mod)) > 86400)
    parts = [
        '<table class="kv">'
        f'<tr><th>Audit path</th><td class="mono">{_esc(d.get("path"))}</td></tr>'
        f'<tr><th>Exists</th><td>{_esc(d.get("exists"))}</td></tr>'
        f'<tr><th>Last modified</th><td>{_ago_with_abs(last_mod)}'
        + (' <span class="pill muted">no approvals in 24h</span>' if no_recent else '')
        + '</td></tr>'
        f'<tr><th>Size</th><td class="mono">{_bytes(d.get("size_bytes"))}</td></tr>'
        f'<tr><th>Tail line count</th><td class="mono">{_esc(d.get("line_count_tail"))}</td></tr>'
        '</table>'
    ]
    pi_ctx = d.get("pi_context")
    if pi_ctx:
        parts.append('<h3>Pi context utilization</h3>')
        ctx_w = pi_ctx.get("context_window")
        cur = pi_ctx.get("current_estimated")
        pct = pi_ctx.get("percent_used")
        parts.append(_kv_table([
            ("Context window", ctx_w),
            ("Current estimated", cur),
            ("Percent used", f"{pct:.1f}%" if isinstance(pct, (int, float)) else None),
            ("Status line", pi_ctx.get("status_line")),
            ("Last compaction at", pi_ctx.get("last_compaction_at")),
            ("models.json unavailable", pi_ctx.get("unavailable_reason")),
            ("tmux unavailable", pi_ctx.get("tmux_unavailable_reason")),
        ]))
    bridge = d.get("telegram_bridge")
    if bridge:
        state = bridge.get("state") or "unknown"
        state_cls = {"connected": "ok", "disconnected": "bad"}.get(state, "muted")
        parts.append('<h3>Telegram bridge</h3>')
        parts.append(
            '<table class="kv">'
            f'<tr><th>State</th><td><span class="pill {state_cls}">{_esc(state)}</span></td></tr>'
            f'<tr><th>Last seen</th><td>{_esc(bridge.get("last_seen") or "—")}</td></tr>'
            f'<tr><th>Last line</th><td class="small mono">{_esc(bridge.get("last_line") or "—")}</td></tr>'
            f'<tr><th>Unavailable reason</th><td>{_esc(bridge.get("unavailable_reason") or "—")}</td></tr>'
            '</table>'
        )
    entries = d.get("recent_entries") or []
    if entries:
        parts.append('<h3>Recent entries</h3>')
        parts.append('<table><thead><tr><th>Time</th><th>Kind</th><th>Decision</th><th>Summary</th></tr></thead><tbody>')
        for e in entries:
            parts.append(
                f'<tr>'
                f'<td class="small mono">{_esc(e.get("ts") or "")}</td>'
                f'<td class="mono">{_esc(e.get("kind") or "")}</td>'
                f'<td>{_esc(e.get("decision") or "")}</td>'
                f'<td class="small mono">{_esc(e.get("summary") or "")}</td>'
                f'</tr>'
            )
        parts.append('</tbody></table>')
    if d.get("error"):
        parts.append(f'<div class="err small">{_esc(d.get("error"))}</div>')
    return "".join(parts)


def _render_workflow_drift(d: Optional[dict]) -> str:
    if not d:
        return '<div class="muted">unavailable</div>'
    if d.get("unavailable_reason"):
        return _kv_table([
            ("Last modified", _ago(d.get("last_modified")) if d.get("last_modified") else None),
            ("Unavailable reason", d.get("unavailable_reason")),
        ])
    report = d.get("report") or {}
    status = report.get("status") if isinstance(report, dict) else None
    cls = {"ok": "ok", "drift": "warn", "error": "bad"}.get(status, "muted")
    parts = [
        '<table class="kv">'
        f'<tr><th>Last refreshed</th><td>{_ago_with_abs(d.get("last_modified"))}</td></tr>'
        f'<tr><th>Status</th><td><span class="pill {cls}">{_esc(status or "—")}</span></td></tr>'
        f'<tr><th>Exit code</th><td class="mono">{_esc(report.get("exit_code") if isinstance(report, dict) else "—")}</td></tr>'
        f'<tr><th>Reason</th><td>{_esc(report.get("reason") if isinstance(report, dict) else "")}</td></tr>'
        '</table>'
    ]
    if isinstance(report, dict):
        if report.get("stdout_tail"):
            parts.append('<h3>stdout tail</h3>')
            parts.append(f'<pre>{_esc(report.get("stdout_tail"))}</pre>')
        if report.get("stderr_tail"):
            parts.append('<h3>stderr tail</h3>')
            parts.append(f'<pre>{_esc(report.get("stderr_tail"))}</pre>')
    return "".join(parts)


def _render_config_snapshot(c: dict) -> str:
    parts: list[str] = []
    parts.append(_kv_table([
        ("LLM config", c.get("llm")),
        ("Image config", c.get("images")),
        ("Default video workflow", c.get("videos_default_workflow")),
        ("Routing", c.get("routing")),
        ("Auth enabled", c.get("auth_enabled")),
        ("Auth public paths", c.get("auth_public_paths")),
        ("Model compatibility", c.get("model_compatibility")),
        ("OpenAI aliases file", c.get("openai_aliases_file")),
        ("OpenAI aliases file size", _bytes(c.get("openai_aliases_file_size_bytes"))),
        ("OpenAI aliases file mtime", _ago(c.get("openai_aliases_file_mtime"))),
        ("OpenAI aliases count", c.get("openai_aliases_count")),
        ("OpenAI aliases by category", c.get("openai_aliases_by_category")),
    ]))
    sample = c.get("openai_aliases_sample") or []
    if sample:
        rows = [
            '<details><summary class="muted small">'
            f'OpenAI alias sample (first {len(sample)} alphabetical)</summary>'
            '<table><thead><tr><th>Model id</th><th>Inferred category</th>'
            '</tr></thead><tbody>'
        ]
        for item in sample:
            if not isinstance(item, dict):
                continue
            rows.append(
                f'<tr><td class="mono">{_esc(item.get("id"))}</td>'
                f'<td class="mono">{_esc(item.get("category"))}</td></tr>'
            )
        rows.append('</tbody></table></details>')
        parts.append("".join(rows))
    return "".join(parts)


def _render_errors(errs: dict) -> str:
    if not errs:
        return '<div class="muted">none</div>'
    rows = ['<table><thead><tr><th>Section</th><th>Error</th></tr></thead><tbody>']
    for k, v in errs.items():
        rows.append(f'<tr><td>{_esc(k)}</td><td class="err mono">{_esc(v)}</td></tr>')
    rows.append("</tbody></table>")
    return "".join(rows)

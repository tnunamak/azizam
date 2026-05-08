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


_THROTTLE_BITS = {
    0: "GPU idle",
    1: "App clocks",
    2: "SW power cap",
    3: "HW slowdown",
    4: "Sync boost",
    5: "SW thermal",
    6: "HW thermal",
    7: "HW power brake",
    8: "Display clock setting",
}


def _decode_throttle(hex_str: Optional[str]) -> str:
    if not hex_str:
        return ""
    try:
        v = int(hex_str, 16)
    except Exception:
        return _esc(hex_str)
    if v == 0:
        return f"{hex_str} (none)"
    labels = [name for bit, name in _THROTTLE_BITS.items() if v & (1 << bit)]
    if not labels:
        return f"{hex_str} (unknown bits)"
    return f"{hex_str} ({', '.join(labels)})"


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


def render_html(payload: dict) -> str:
    """Render the status page from a /v1/status payload.

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
    parts: list[str] = []
    parts.append(_HTML_HEAD)
    parts.append(_render_banner(payload))
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
    parts.append(_section("Models / LLM", _render_models(payload.get("models") or {})))
    parts.append(_section("Video capabilities", _render_video_caps(payload.get("capabilities") or [])))
    parts.append(_section("Image capabilities", _render_image_caps(payload.get("image_capabilities") or [])))
    parts.append(_section("Voices summary", _render_voices(payload.get("voices_summary"))))

    parts.append('<div class="group-label">Recent output</div>')
    parts.append(_section("Recent videos", _render_recent_videos(payload.get("recent_videos") or [])))
    parts.append(_section("Recent images", _render_recent_images(payload.get("recent_images") or [])))

    parts.append('<div class="group-label">Identity</div>')
    parts.append(_section("Per-API-key activity (24h)", _render_auth_keys(payload.get("auth_keys") or [])))
    parts.append(_section("Recent activity timeline", _render_recent_events(payload.get("recent_events") or [])))

    parts.append('<div class="group-label">Diagnostics</div>')
    parts.append(_section("Driver & kernel events", _render_driver(payload.get("driver") or {}), collapsed=True))
    parts.append(_section("ComfyUI view (system_stats)", _render_comfy_view(payload.get("comfy_view")), collapsed=True))
    parts.append(_section("Daisy audit", _render_daisy(payload.get("daisy") or {}), collapsed=True))
    parts.append(_section("Workflow drift", _render_workflow_drift(payload.get("workflow_drift")), collapsed=True))
    parts.append(_section("VRAM measurements (image batcher)", _render_vram_estimates(payload.get("budgets") or {}), collapsed=True))
    parts.append(_section("Configuration snapshot", _render_config_snapshot(payload.get("config_snapshot") or {}), collapsed=True))
    # Always show errors un-collapsed when present so misbehaving probes surface.
    errs = payload.get("errors") or {}
    parts.append(_section("Errors during status build", _render_errors(errs), collapsed=not errs))

    parts.append(_HTML_FOOT)
    return "".join(parts)


# --- header ---------------------------------------------------------------

_HTML_HEAD = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>openai.vivid.fish · firehose</title>
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


_HTML_FOOT = """
<footer style="margin-top:32px; padding-top:12px; border-top:1px solid #1c2128; color:#7c8794; font-size:11px;">
  Auto-refresh disabled (server-rendered). Reload to update. Latency-sensitive
  consumers should poll <a href="/v1/status">/v1/status</a> directly.
</footer>
</body></html>
"""


# --- sections -------------------------------------------------------------

def _render_banner(p: dict) -> str:
    banner = p.get("banner") or "ok"
    cls = banner if banner in ("ok", "warn", "bad") else "ok"
    title = {"ok": "All systems operational", "warn": "Degraded", "bad": "Outage"}[cls]
    gen = p.get("generated_at")
    cache_age = p.get("cache_age_s")
    try:
        cache_age_str = f"{float(cache_age):.1f}s"
    except Exception:
        cache_age_str = "—"
    abs_gen = _abs_utc(gen) or "—"
    reasons = p.get("banner_reasons") or []
    reasons_html = ""
    if reasons:
        reasons_html = (
            '<div style="margin-top:6px;">'
            + "".join(f'<span class="pill {cls}">{_esc(r)}</span>' for r in reasons)
            + '</div>'
        )
    return (
        f'<div class="banner {cls}">'
        f'<h1>{_esc(title)}</h1>'
        f'<div class="muted small">generated <span title="{_esc(abs_gen)}">{_ago(gen)}</span> '
        f'({_esc(abs_gen)}) · cache age {_esc(cache_age_str)} · openai.vivid.fish</div>'
        f'{reasons_html}'
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
    s = p.get("swap_state") or {}
    ws = p.get("comfy_ws_state") or {}
    return _kv_table([
        ("Tabby model swap in progress", s.get("tabby_swap_in_progress")),
        ("Kobold model swap in progress", s.get("kobold_swap_in_progress")),
        ("ComfyUI websocket connected", ws.get("connected")),
        ("ComfyUI client_id", ws.get("client_id")),
    ])


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
        parts.append(
            f'<div class="card">'
            f'<div><b>GPU{_esc(g.get("index"))}</b> <span class="muted small">{_esc(g.get("name"))}</span></div>'
            f'<div class="mono small">{used:,} / {total:,} MiB ({pct:.1f}%)</div>'
            f'<div class="bar {bar_cls}"><span style="width:{min(100, pct):.1f}%"></span></div>'
            f'<div class="mono small">temp={_esc(g.get("temp_c"))}°C · '
            f'power={_esc(g.get("power_draw_w"))}/{_esc(g.get("power_limit_w"))}W · '
            f'fan={_esc(g.get("fan_speed_pct"))}%</div>'
            f'<div class="mono small">clocks gr/mem={_esc(g.get("clocks_gr_mhz"))}/{_esc(g.get("clocks_mem_mhz"))} MHz · '
            f'pcie gen{_esc(g.get("pcie_link_gen"))}x{_esc(g.get("pcie_link_width"))}</div>'
            f'<div class="mono small muted">throttle: {_decode_throttle(g.get("throttle_reasons_hex"))}</div>'
            f'<div class="small muted">{_esc(wl.get("label"))}: {_esc(wl.get("detail") or "")}</div>'
            f'</div>'
        )
    parts.append("</div>")
    return "".join(parts)


def _render_vram_attribution(va: dict) -> str:
    per_gpu = va.get("per_gpu") or []
    if not per_gpu:
        return '<div class="muted">unavailable</div>'
    parts = []
    for g in per_gpu:
        services = g.get("services") or []
        rows = [
            f'<h3>GPU{_esc(g.get("index"))} ({_esc(g.get("memory_used_mb"))}/{_esc(g.get("memory_total_mb"))} MiB)</h3>',
            '<table><thead><tr><th>Service</th><th>Used MiB</th><th>Used GiB</th><th>Budget GB</th><th>% of budget</th><th>Over?</th></tr></thead><tbody>'
        ]
        for s in services:
            over = s.get("over_budget")
            cls = "bad" if over else ("warn" if (s.get("pct_of_budget") or 0) > 80 else "ok")
            rows.append(
                f'<tr>'
                f'<td>{_esc(s.get("service"))}</td>'
                f'<td class="mono">{_esc(s.get("used_mb"))}</td>'
                f'<td class="mono">{_esc(s.get("used_gb"))}</td>'
                f'<td class="mono">{_esc(s.get("budget_gb"))}</td>'
                f'<td class="mono"><span class="pill {cls}">{_esc(s.get("pct_of_budget"))}%</span></td>'
                f'<td>{_esc("YES" if over else "")}</td>'
                f'</tr>'
            )
        rows.append("</tbody></table>")
        parts.append("".join(rows))
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
    parts = [f'<div class="muted small">{_esc(vr.get("total_entries"))} measurements; cache mtime {_ago(vr.get("last_modified"))}</div>']
    parts.append('<table><thead><tr><th>Workflow:size</th><th>Peak VRAM</th></tr></thead><tbody>')
    for e in vr.get("entries") or []:
        parts.append(f'<tr><td class="mono">{_esc(e.get("key"))}</td><td class="mono">{_bytes(e.get("peak_bytes"))}</td></tr>')
    parts.append("</tbody></table>")
    return "".join(parts)


def _render_models(m: dict) -> str:
    return _kv_table([
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
        ("Tensor split", m.get("tensor_split")),
        ("--parallel arg", m.get("parallel_arg")),
        ("Active TTS engine", m.get("active_tts_engine")),
        ("Image workflows", m.get("image_workflows")),
        ("Video aliases", m.get("video_aliases")),
    ])


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
    return (
        '<h3>Voxtral (TTS)</h3>'
        + f'<pre>{_esc(json.dumps(aux.get("voxtral"), indent=2))}</pre>'
        + '<h3>Parakeet (STT)</h3>'
        + f'<pre>{_esc(json.dumps(aux.get("parakeet"), indent=2))}</pre>'
    )


def _render_voices(v: Optional[dict]) -> str:
    if not v:
        return '<div class="muted">unavailable</div>'
    return _kv_table([
        ("Voice count", v.get("count")),
        ("Languages", v.get("languages")),
        ("Genders", v.get("genders")),
    ])


def _render_video_caps(caps: list[dict]) -> str:
    if not caps:
        return '<div class="muted">no aliases</div>'
    rows = [
        '<table><thead><tr><th>Alias</th><th>Workflow</th><th>Min/Max sec</th><th>Max size</th><th>FPS</th><th>I2V</th><th>Audio</th><th>Two-stage</th><th>Class types</th></tr></thead><tbody>'
    ]
    for c in caps:
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
            f'<td class="small mono">{_esc(json.dumps(c.get("class_types") or {}))}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_image_caps(caps: list[dict]) -> str:
    if not caps:
        return '<div class="muted">no image workflows</div>'
    rows = ['<table><thead><tr><th>Workflow</th><th>Edit?</th><th>Class type count</th><th>Top class types</th></tr></thead><tbody>']
    for c in caps:
        rows.append(
            f'<tr>'
            f'<td class="mono">{_esc(c.get("name"))}</td>'
            f'<td>{_esc("yes" if c.get("supports_edit") else "no")}</td>'
            f'<td>{_esc(c.get("class_type_count"))}</td>'
            f'<td class="small mono">{_esc(json.dumps(c.get("top_class_types") or {}))}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_recent_videos(v: list[dict]) -> str:
    if not v:
        return '<div class="muted">no videos</div>'
    rows = ['<table><thead><tr><th>Filename</th><th>Alias</th><th>Size</th><th>Age</th></tr></thead><tbody>']
    for r in v:
        rows.append(
            f'<tr>'
            f'<td class="mono">{_esc(r.get("filename"))}</td>'
            f'<td class="mono">{_esc(r.get("alias") or "—")}</td>'
            f'<td class="mono">{_bytes(r.get("size_bytes"))}</td>'
            f'<td title="{_esc(_abs_utc(r.get("mtime")))}">{_ago(r.get("mtime"))}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_recent_images(v: list[dict]) -> str:
    if not v:
        return '<div class="muted">no images</div>'
    rows = ['<table><thead><tr><th>Filename</th><th>Size</th><th>Age</th></tr></thead><tbody>']
    for r in v:
        rows.append(
            f'<tr>'
            f'<td class="mono">{_esc(r.get("filename"))}</td>'
            f'<td class="mono">{_bytes(r.get("size_bytes"))}</td>'
            f'<td title="{_esc(_abs_utc(r.get("mtime")))}">{_ago(r.get("mtime"))}</td>'
            f'</tr>'
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_auth_keys(keys: list[dict]) -> str:
    if not keys:
        return '<div class="muted">no keys configured</div>'
    rows = [
        '<table><thead><tr><th>Key name</th><th>Active 24h?</th><th>Last seen</th>'
        '<th>Reqs 24h</th><th>4xx</th><th>5xx</th><th>Latency p50</th><th>Latency p95</th>'
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
    rows = ['<table><thead><tr><th>Time</th><th>Type</th><th>Summary</th><th>Detail</th></tr></thead><tbody>']
    for e in events:
        ts = e.get("ts")
        details = {k: v for k, v in e.items() if k not in ("ts", "type", "summary")}
        rows.append(
            f'<tr>'
            f'<td>{_ago(ts)}</td>'
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
    parts.append('<h3>Recent kernel events (NV/GPU/IOMMU/MCE)</h3>')
    if d.get("kernel_recent"):
        parts.append('<pre>' + _esc("\n".join(d.get("kernel_recent") or [])) + '</pre>')
    else:
        parts.append('<div class="muted">none in 24h</div>')
    parts.append('<h3>Reboot history</h3>')
    if d.get("reboot_history"):
        parts.append('<pre>' + _esc("\n".join(d.get("reboot_history") or [])) + '</pre>')
    else:
        parts.append('<div class="muted">unavailable</div>')
    return "".join(parts)


def _render_daisy(d: dict) -> str:
    last_mod = d.get("last_modified")
    stale = bool(last_mod and (time.time() - float(last_mod)) > 86400)
    parts = [
        '<table class="kv">'
        f'<tr><th>Audit path</th><td class="mono">{_esc(d.get("path"))}</td></tr>'
        f'<tr><th>Exists</th><td>{_esc(d.get("exists"))}</td></tr>'
        f'<tr><th>Last modified</th><td>{_ago_with_abs(last_mod)}'
        + (' <span class="pill warn">stale &gt;24h</span>' if stale else '')
        + '</td></tr>'
        f'<tr><th>Size</th><td class="mono">{_bytes(d.get("size_bytes"))}</td></tr>'
        f'<tr><th>Tail line count</th><td class="mono">{_esc(d.get("line_count_tail"))}</td></tr>'
        '</table>'
    ]
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
    return _kv_table([
        ("Last modified", _ago(d.get("last_modified")) if d.get("last_modified") else None),
        ("Unavailable reason", d.get("unavailable_reason")),
        ("Report", d.get("report")),
    ])


def _render_config_snapshot(c: dict) -> str:
    return _kv_table([
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
    ])


def _render_errors(errs: dict) -> str:
    if not errs:
        return '<div class="muted">none</div>'
    rows = ['<table><thead><tr><th>Section</th><th>Error</th></tr></thead><tbody>']
    for k, v in errs.items():
        rows.append(f'<tr><td>{_esc(k)}</td><td class="err mono">{_esc(v)}</td></tr>')
    rows.append("</tbody></table>")
    return "".join(rows)

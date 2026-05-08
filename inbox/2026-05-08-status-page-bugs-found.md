# Status page bugs found — 2026-05-08

Investigation phase before edits. Cross-referenced rendered HTML, `/v1/status`
JSON, source code (`status_endpoint.py`, `firehose_metrics.py`,
`firehose_render.py`, `proxy.py`), and ground truth (`df`, `nvidia-smi`,
`journalctl`, ComfyUI APIs, llama-server APIs, sensors, audit log).

## P0 — wrong data displayed

### P0-1. Disk `pct_used` calc misreports 99% as 93.7%

- **Section:** Disk pressure / banner.
- **Evidence:**
  - `df -h /` → `1.4T total, 1.3T used, 17G avail, 99% used` (the standard `df`
    formula `used / (used + avail)` excluding reserved blocks).
  - JSON `disk.partitions[/].pct_used = 93.7` (`status_endpoint.py:976`,
    formula `used / size`).
  - The 5% reserved blocks are not user-writable space; the page should match
    `df`'s notion of full.
  - Banner threshold is `>= 95` for `bad`; the bug keeps the banner at `warn`
    instead of `bad`, hiding an imminent disk-full condition.
- **Fix:** `pct_used = used / (used + avail)` (matches `df`).

### P0-2. `voices_summary` returns null because the fetch is unauthenticated

- **Section:** Voices summary (page shows "unavailable").
- **Evidence:**
  - Direct authed call to `/v1/audio/voices` returns 37 voices.
  - `status_endpoint.py:1255-1259` calls `httpx.get('http://127.0.0.1:5000/v1/audio/voices')`
    with no `Authorization` header. `/v1/audio/voices` is not in `auth.public_paths`,
    so it returns 401 → `_voices_summary()` returns `None`.
- **Fix:** Either add `/v1/audio/voices` to `auth.public_paths` (it's a static
  list of voice names, no secrets) — but the cleaner fix is reading the voice
  registry directly from the proxy module (no HTTP at all, no auth concern).

### P0-3. Anonymous "key" is filled by the dashboard's own polling

- **Section:** Per-API-key activity (24h).
- **Evidence:**
  - `auth_keys[0] = {name: anonymous, requests_24h: 16, top_endpoints: [/v1/audio/voices x11, / x5]}`.
  - `_FIREHOSE_NO_RECORD_PREFIXES` excludes `/health`, `/v1/status`,
    `/firehose`, etc. — but **not `/`**, **not `/_next/`** (it's there but as a
    prefix that excludes), and `_voices_summary` calls
    `127.0.0.1:5000/v1/audio/voices` (above) with no key, so each status build
    increments the "anonymous" counter.
  - The "anonymous" row is therefore a self-poll artifact, not real user
    traffic. It also obscures whether real un-authed calls are happening.
- **Fix:** (a) Stop the unauthenticated self-call (P0-2 fix). (b) Add `/`
  to no-record list. (c) Continue counting truly anonymous external calls
  separately if useful, but don't display rows whose top endpoints are 100%
  internal probe paths.

### P0-4. `_kernel_recent_events()` returns `[]` despite Xid storms in last 24h

- **Section:** Driver & kernel events / "Recent kernel events".
- **Evidence:**
  - 9 Xid 109 events + 1 Xid 31 are present in the journal in the last
    24h — confirmed via direct `journalctl -k --since "24 hours ago"` filter
    that matches `nvrm|nvidia|xid`.
  - `_kernel_recent_events()` calls `journalctl -k -n 200 --since "24 hours ago"`.
    The `-n 200` cap returns the *last 200 lines* of any kernel output;
    on a chatty kernel (USB, nf_conntrack, scheduler, etc.) the Xids fall
    off the tail. The display ends up empty.
- **Fix:** Drop `-n 200` and grep first; or grep with `-g`/`-G` (journalctl
  match), or pipe `journalctl -k --since "24 hours ago" | grep ...` and tail.

### P0-5. `last` command not present → `reboot_history` always empty

- **Section:** Driver & kernel events / "Reboot history".
- **Evidence:** `/usr/bin/last` does not exist on this host (`util-linux`
  variant). `_run(["last", ...])` returns rc=127 and `_reboot_history` returns
  `[]`. Section silently shows empty.
- **Fix:** Use `journalctl --list-boots` (always present where journald is)
  or `who -b`. Surface `unavailable_reason` when both fail.

### P0-6. `openai_aliases_count` is 0 despite 134 generated aliases

- **Section:** Configuration snapshot.
- **Evidence:**
  - `openai_model_aliases.generated.yml` has top-level `models: [...]` with
    134 items.
  - `status_endpoint.py:1635` does
    `sum(len(v) if isinstance(v, dict) else 0 for v in aliases_yaml.values())`
    — `models` is a list, not a dict, so it sums to 0.
- **Fix:** Treat list values as their `len`, dict values as their `len`,
  scalars as 1; or directly read `models` and `len()` it.

### P0-7. `cache_age_s` displayed without unit & rounded inconsistently

- **Section:** Banner.
- **Evidence:** Banner shows `cache_age=0.74s · ...` (renderer adds "s") but
  in the response with `cache_age_s` in seconds. After fix to disk it should
  surface anyway. Soft-P0 for cosmetic confusion.
- **Fix:** Show `cache_age_s=0.7s` (single decimal); always display unit.

## P1 — missing data the page should surface

### P1-1. Banner fails to escalate to `bad` when disk is actually 99%

- **Section:** Banner.
- **Evidence:** Disk fix (P0-1) → real pct = 98.7% → escalates from `warn`
  to `bad`. Without P0-1, banner reasons say `93.7% full` which is wrong.

### P1-2. Daisy audit "recent_entries" don't decode anything useful

- **Section:** Daisy audit.
- **Evidence:**
  - Audit JSON shape: `{"ts","tool","cwd","command","risk_rule","approval_id","decision","decision_ts",...}`.
  - Parser at `status_endpoint.py:1304-1308` looks for keys
    `kind|type|summary|prompt|action`, none of which exist → all entries are
    `{"kind": null, "summary": ""}`.
- **Fix:** Map to actual fields: `tool`, `risk_rule` for kind; `command`
  truncated for summary; `decision`.

### P1-3. Daisy "Last modified" is ~4 days old; not flagged as stale

- **Section:** Daisy audit.
- **Evidence:** `last_modified: 1777928046` (May 4) vs now (May 8). Section
  shows it relative ("4d ago") but no warning. Probably out of scope, soft
  flag.
- **Fix (light):** Color the "last modified" pill warn if >24h.

### P1-4. `recent_renders` is a strict subset of `recent_videos` — duplicate render

- **Section:** Recent videos vs Recent renders.
- **Evidence:** `recent_renders = recent_videos[:5]` per `status_endpoint.py:1723`,
  preserved for "backwards-compatible alias for the existing Next.js page".
  Now that the Next.js page is being removed, this duplicate becomes pure
  noise on the firehose render (which renders both sections).
- **Fix:** Render either Recent videos OR Recent renders (not both).

### P1-5. `vision_capable` source field rendered as bare `"props"` w/ no explanation

- **Section:** Models / LLM.
- **Evidence:** Renderer shows "Vision source: props" with no context. The
  user can't tell whether that means "trusted upstream answer" or "fallback".
- **Fix (light):** Render as "props (live `/props` modalities.vision)" or
  similar tooltip.

### P1-6. ComfyUI `vram_free` (10.5 GiB) ≠ nvidia-smi free (10.9 GiB) but no
discrepancy badge.

- **Section:** GPU strip vs ComfyUI view.
- **Evidence:**
  - GPU0: 13425 / 24576 MiB used → 11151 MiB free (~10.89 GiB).
  - ComfyUI device.vram_free = 11276517376 bytes = 10.50 GiB.
  - Difference = `--reserve-vram 9` (hold-back) less Comfy's own reservation.
  - The audit doc explicitly called this out as worth surfacing.
- **Fix:** Show the delta as a small "headroom mismatch" line.

### P1-7. No live banner for "image gen produced output" (but it's there)

- **Section:** Recent images was added but the page doesn't promote it to
  the high-confidence "is image gen working" position.
- **Fix:** Section ordering improvement (covered in restructure).

### P1-8. `proxy_restarts` always 0 (no restart since last service-config edit)

- Not a bug per se — `NRestarts` is correctly read. But there's no surface
  for "uptime of the proxy itself" (we have host uptime). Useful when chasing
  503 cascades.
- **Fix:** Add proxy-process-uptime alongside host-uptime.

### P1-9. Skill doc claims `X-Vision-Routed` and `X-Duration-Adjusted` headers exist
— not surfaced

- **Section:** Capabilities.
- **Evidence:** SKILL.md mentions both but the dashboard never reflects
  whether they're being emitted recently.
- **Fix:** Defer (would require capturing recent response headers; not in
  current data model).

### P1-10. `recent_events` only has video submits, but the doc says it should
include 4xx/5xx samples

- **Section:** Recent activity timeline.
- **Evidence:** `firehose_metrics.record_event` is only called from video
  submit. The audit report explicitly recommended last-N 4xx/5xx samples,
  not landed.
- **Fix:** In the firehose middleware, emit `record_event("error_4xx", ...)`
  for non-2xx statuses with truncated path/key.

### P1-11. Hotspot "comfy_output_root" total includes the `video/` subdir
counted again as a separate hotspot.

- **Section:** Disk hotspots.
- **Evidence:** `comfy_output_root` (3.1 GB, 2071 files) walks the full tree,
  which already includes `comfy_output_video` (135 MB, 99 files) shown
  separately. The separate-line is fine, but the parent total is misleading
  (looks like more disk pressure than is actually attributable).
- **Fix (light):** Annotate the parent row, or list the `video/` hotspot as
  a child entry.

## P2 — cosmetic / structural

### P2-1. `live_status` duplicates `status` 1:1 — wastes JSON & confuses viewers
Already called out in the audit doc; can be hidden in render.

### P2-2. Recent-render alias regex silently empties on non-conforming names
E.g., a hypothetical `wan-2.2-ti2v-5b_00006_.mp4` works; an `outputs/foo.mp4`
without the `_NNNN_` suffix gets `alias: null`. Render this as "—" rather
than empty string for clarity.

### P2-3. Page is two endpoints (`/` and `/firehose`) — the user wants ONE.
Resolution: serve maximalist HTML at `/`, redirect `/firehose` → `/`, drop
the Next.js page. Restructure into a clearer hierarchy (banner & actionable
warnings → live activity → static config → diagnostics).

### P2-4. Dense data shown without collapsibility
Many sections (config snapshot, vram estimates, capability matrix) are large
tables shown by default. Use `<details>` for low-priority sections.

### P2-5. Numeric values often miss units
Latencies show "X" then a separate "ms" cell, but VRAM "used_mb" is bare.
Render with units everywhere.

### P2-6. Timestamps relative-only, no absolute UTC alongside.
"2m ago" is good for live data; bad for "is this stale?". Show
`2m ago (17:01 UTC)`.

### P2-7. Throttle-reasons hex `0x0000000000000001` is unparsed.
Bit 0 = "GPU IDLE". Decode to a human label.

### P2-8. NRestarts label says "Proxy NRestarts" — should be "since last config reload" or "since unit start". Clarify.

## Out of scope / deferred

- Workflow drift cached report doesn't exist (`unavailable_reason` set
  correctly). The skill doc said this is fine to defer to a daily timer.
- TabbyAPI cold-start in-flight indicator: deferred (audit notes it's nice
  to have; the swap_state currently only reflects the lock — adequate for
  now).
- `/v1/status` `cache_age_s` rounding: cosmetic.

## Restructuring summary (collapse to /)

- Drop the Next.js `/` page.
- Mount maximalist HTML at `/` (loopback bypass + bearer auth).
- `/firehose` → 301 to `/`.
- New section ordering:
  1. **Banner + reasons** (with click-to-action).
  2. **Anything currently bad/warn** (red/yellow boxes promoted).
  3. **Live activity** — video jobs, ComfyUI queue, LLM slots.
  4. **Health snapshot** — backends, GPUs, swap state, ws state, aux health.
  5. **Resources** — VRAM attribution, budgets, disk pressure, image cache,
     thermal, CPU.
  6. **Capabilities** — models/LLM, video & image workflows, voices.
  7. **Recent renders** — videos + images.
  8. **Per-key activity** + **Recent events**.
  9. **Diagnostics (collapsed by default)** — driver/kernel events, ComfyUI
     view, daisy audit, workflow drift, config snapshot, VRAM measurements,
     errors, raw service-PIDs map.
- Always-show units. Add absolute UTC alongside relative timestamps.
- Color-code: red bad / yellow warn / green ok / grey muted.

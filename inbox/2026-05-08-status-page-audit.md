# Status page content audit — 2026-05-08

## Executive summary

Three highest-impact gaps the page should address before anything else:

1. **Root disk is at 99% (18 GB free of 1.4 TB) and the dashboard cannot show it.** The disk card only probes `/media/windows`. A user looking at a "green banner" page right now has no way to see that ComfyUI output, the image cache, journals, models, or any other write to `/` will start failing in a few image-gen runs. This is the single most actionable missing signal on the page.
2. **No live-job visibility for `/v1/videos`.** The proxy keeps a fully populated `_video_progress` dict (status_endpoint never reads it) with `{percent, step, total, node, phase, error}` per `prompt_id`. A 5-15 minute LTX-long render is currently invisible to anyone who didn't kick it off — they can only see "ComfyUI: 1 running" in the queue card, with no way to know which alias, which prompt, how far in, or expected ETA.
3. **No request/error/identity activity panel.** AUTH_REGISTRY has 6 named keys (`simon`, `daisy`, `librechat`, `cap`, `open-webui`, ...). The proxy cannot tell anyone whether daisy has hit it in the last hour, whether anything is 4xx-ing, or whether the recent `/v1/chat/completions` rate is flat or spiking. After the Xid-109 → 503 cascade pattern documented in the inbox, the dashboard's main job is "is the service healthy *for callers*", and right now it can only answer "are the backends up".

The rest of this report is ranked by similar-impact gaps and a small set of removals/restructures.

## Method

Read `proxy.py` (4 974 lines), `status_endpoint.py` (730 lines), `proxy_config.yml`, the SKILL doc, and three inbox docs (final-state, honesty-audit, video-proposal). Probed live: `/v1/status` (cache-warm), llama.cpp `/slots` and `/props` on :5051, ComfyUI `/system_stats`, `/queue`, `/history`, `/prompt`, voxtral `/health`, parakeet `/health`. Inspected disk, dmesg, `nvidia-smi --query-compute-apps`, `sensors`, the file system layout under `/home/tnunamak/applications/{openai-proxy/cache,ComfyUI/output,daisy-audit}`. No code or config changes.

## What's currently on the page

(Verified from `/v1/status` JSON @ generated_at=1778279187.)

- Banner (`ok`/`warn`/`bad`) keyed off backend `role` × `live_status`.
- Backend grid: 6 backends with live_status, role, latency_ms, last_error, note.
- GPU strip: `index`, `memory_used_mb`, `memory_total_mb`, `instantaneous_util_pct`, plus `active_workload` annotation (ComfyUI queue depth on GPU0, llama.cpp slot count on GPU1).
- `comfy_queue`: `running`, `pending` counts.
- `models`: `loaded_llm_path`, `loaded_llm_name`, `mmproj_path`, `vision_capable`, `ctx_size`, `ctx_used` (max `n_past` across slots), `active_tts_engine`, `image_workflows`, `video_aliases`.
- `capabilities` matrix: 13 video aliases with `workflow`, `max_seconds`, `max_size`, `frame_rate`, `supports_i2v`, `supports_audio`.
- `recent_renders`: 5 most recent `*.mp4` from `/home/tnunamak/applications/ComfyUI/output/video/` with filename, size, mtime, alias.
- `driver`: last 5 Xid 109 timestamps (24 h window), `uptime_since`, `disk_avail_models` (only `/media/windows`).
- `budgets` (in-flight from sister agent): GPU0/GPU1 budget breakdown, image batch reserves, top-5 measured peak-VRAM per (workflow,size).
- Footer: links to `/health`, `/v1/models`.

## Recommended additions (ranked by impact)

### High value

#### Root disk fill warning
- **What**: Disk-pressure card surfacing `/`, `/home`, and `/media/windows` together. Color-warn when any partition crosses 85%, banner-warn at 95%.
- **Why**: Right now `df -h /` reports `/dev/nvme0n1p5  1.4T  1.3T  18G  99% /`. Nothing on the page surfaces this. ComfyUI writes to `~/applications/ComfyUI/output` (3 GB and growing — 84 files in the last 24 h), the image cache writes to `~/applications/openai-proxy/cache/images/`, daisy's audit log writes to `~/applications/daisy-audit/`, and journalctl/systemd write to `/var/log`. All of those land on the 18 GB. A single LTX-long-15-minute render at 1280×720 has been measured at ~2 MB output but several GB of intermediate disk pressure (see model-loading dumps under `/var/log/journal`). One thoughtful video session can fill the disk.
- **Source**: `df -h --output=source,size,used,avail,pcent,target /` plus `/home` and `/media/windows`. Already wired pattern: `_disk_avail_models` in `status_endpoint.py:582`. ~5 ms.
- **Cost**: One subprocess call (already budgeted for at 1 s in `_run`). Add ~3 ms to status build.
- **Caveat**: None. Banner-warn rules need a config knob so a user with predictably-tight `/` doesn't see a permanent yellow.

#### Live video-job panel (replaces or augments "ComfyUI Queue")
- **What**: For each active video job, show: alias, requested seconds, requested size, percent, step/total, phase (`encoding`/`sampling`/`decoding`), node id, age, ETA. When idle: show `0 jobs` and the most recent terminal status (success/fail timestamp).
- **Why**: The proxy already has all this in `_video_progress` (proxy.py:2835), populated live by the websocket listener. A user who didn't initiate the job currently sees only the bare queue depth. With a 15 min `ltx-2.3-long` 1280×720 wall time, that's 15 minutes of "ComfyUI: 1 running" with no way to gauge whether to wait, kill, or reroute. The inbox doc explicitly calls block-swap serialization a fragility — the dashboard is exactly where that should be visible. Daisy specifically benefits: she hands the user back `video_<id>` and currently has no reason to ever look at the dashboard.
- **Source**: Add a getter that returns a list-of-snapshots from `_video_progress` filtered to `status in {queued,running}` (or, for "recent", the last N entries). The current `/v1/status` is a separate module that imports `proxy.py`-internal state — surface this via a small shim function in `proxy.py` exported into `build_status_payload`. Polling every 8 s is plenty (the websocket pushes every step internally).
- **Cost**: Trivial — read in-process dict. No probe.
- **Caveat**: Prompt text is not in `_video_progress` today (it's in the request body which is discarded post-submit). To show prompt text would require capturing it at submit time. Suggest *not* showing prompt text by default — privacy concern across multiple keys. Show alias + size + seconds + age as the identifying triple instead.

#### Per-key request activity (last 60 minutes)
- **What**: Per-named-key counters: `requests_total`, `errors_4xx`, `errors_5xx`, `last_seen_ts`, optionally `top_endpoint` for the window. Sorted by traffic.
- **Why**: After the Xid-109 → llama-turbo restart cascade pattern, "is daisy still working" is the question this dashboard exists to answer. With six named keys (simon, daisy, librechat, cap, open-webui, +1), surface alone tells you which integration broke. Today the page can show "all backends up" and daisy could be 100% 503-ing because of an in-flight model swap — invisible.
- **Source**: Wrap the auth middleware to increment counters in a `collections.Counter` keyed by `identity.name` × bucket × status_class. AUTH_REGISTRY identity is already resolved at `proxy.py:883`. The current endpoint `prepare_downstream_headers` is the central choke point. Wrapped into a small async-safe `_REQUEST_STATS` dict with a 60-minute rolling window (1-min buckets so memory is bounded).
- **Cost**: ~50 lines of code in `proxy.py`. O(1) per request. No persistence, no Prometheus, just in-process.
- **Caveat**: This adds in-process state that didn't exist; calling out so it's a deliberate decision. If durability matters later, switch to writing 1-min summaries to a JSONL file. Keep request-body visibility *out* — only metadata.

#### Loaded LLM details: speculative decoder, total slots, slot saturation, spec hit rate
- **What**: Extend the models card with: `total_slots` (4), `active_slots`, `speculative` flag (and target model when on), per-slot `n_decoded`, the model alias from `/props` (`Qwen3.6-27B-UD-Q4_K_XL.gguf`).
- **Why**: `ctx_used` today is only `max(n_past)` — a single user filling slot 0 looks identical to four concurrent users on slots 0-3. Slot saturation is the actual scarce resource: a 5th caller's request blocks. Speculative-decoding state is also useful for "why is this slow" debugging — the current build has `speculative.type=none` per `/slots`, but a rebuild could flip that. The `/props` endpoint also reports `modalities.vision: true` directly, which is a more honest source of `vision_capable` than "mmproj path was on the cmdline".
- **Source**: `/props` (already partially used by the slot-counter heuristic in `_gpu_workloads`). One additional GET to `:5051/props`, ~10 ms.
- **Cost**: Already paying for `/slots`; add a parallel `/props` read in the same gather. Trivial.
- **Caveat**: `/props` exposes detailed sampler defaults — fine to display the few load-bearing fields, don't dump the whole payload.

#### Recent renders include images, not just videos
- **What**: Merge `~/applications/ComfyUI/output/*.png` (most recent 5) into the "recent renders" view; show video and image rows distinguished by icon. Show prompt text *only* if it can be cheaply extracted from PNGInfo (it's there — workflow embedded since 2026-05-08).
- **Why**: Image gen is the higher-volume endpoint per the eviction-loop logs (12+ restarts of the cache-evictor in 6 hours, all under 4 minutes apart). Today the recent-renders panel shows only mp4s, so the user can never confirm image gen is producing output through the dashboard. After an eviction-loop bug or a workflow change, this panel is the fastest "is text-to-image still working" check.
- **Source**: Same `Path.iterdir` pattern at `/home/tnunamak/applications/ComfyUI/output/`; filter to `*.png` and `*.mp4` excluding the `video/` subdir vs including it. Decode PNGInfo via PIL only if cheap (skip if PIL not on the path).
- **Cost**: Trivial directory listing. PNGInfo decode is the only added cost; safe to skip.
- **Caveat**: 3 GB output dir. iterdir scans all entries; cap with mtime sort. Don't render thumbnails — just filename + size + mtime + workflow stem.

#### Tabby cold-start in flight
- **What**: When TabbyAPI is being cold-started by `ensure_target()`, show a transient "starting…" pill with the target model and elapsed time; on success flip to "loaded".
- **Why**: Tabby is `standby` (correctly down at idle). But when a vision request flips it on, there's a 10–60 s window where the request hangs and the dashboard shows "tabby: down". A user troubleshooting that hang sees "down + last_error: connect refused", not "swap in progress". The proxy already serializes via `tabby_swap_lock` at proxy.py:482 — a `swap_in_progress` boolean exists in process state.
- **Source**: Expose the lock status (`tabby_swap_lock.locked()`) and the most recent `ensure_target()` target/start time. Add a small in-process state container.
- **Cost**: Few lines.
- **Caveat**: Same for kobold via `kobold_swap_lock`. For now `pin_loaded_model: false` in config means kobold may swap on role change too.

### Medium value

#### Per-process VRAM breakdown
- **What**: For each GPU, show the top-3 processes by VRAM use with PID, name, MiB. (E.g. on GPU1 right now: `llama-server 17666 MiB`; on GPU0: `voxtral python 4070 MiB, sunshine 262 MiB`.)
- **Why**: GPU memory bars are accurate in MB total but say nothing about *what* is using the memory. Useful when budget cards say "expected 13 GB ComfyUI" and reality is 4 GB ComfyUI + 9 GB orphaned llama-turbo from a botched swap.
- **Source**: `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader`. Already a 1 s subprocess budget pattern.
- **Cost**: One extra subprocess.
- **Caveat**: PIDs leak nothing useful; cmd lines may. Truncate process_name to the binary basename.

#### Image-cache footprint
- **What**: Show count of cached images, total bytes, age of oldest entry. Only meaningful since 2026-05-08 honesty-audit fix landed (`response_format: "url"` cache).
- **Why**: Currently the cache is 1.3 MB / 2 entries. Once daisy or another client flips to `response_format: "url"` in production, this becomes the disk-pressure component most likely to grow silently. The eviction loop runs every hour but only honors a 24 h TTL — heavy use over a single day can swell the cache to GB scale. Tied directly to the root-disk warning.
- **Source**: `Path.iterdir` over `cache_dir`, sum sizes, oldest mtime.
- **Cost**: Trivial.
- **Caveat**: None.

#### Last-N-minutes 4xx/5xx samples
- **What**: A small "recent failures" panel: timestamp, endpoint, status code, key-name, error class. Capped at 5 entries, last 60 min.
- **Why**: A 503 cascade during a kobold swap currently has no first-class surface on the dashboard. The proxy logs land in `journalctl --user -u openai-proxy`, but the dashboard is what humans look at. Five recent failures is a much faster signal than chasing journals.
- **Source**: Same auth/middleware wrapper as the per-key counter; ring-buffer of (ts, identity_name, path, status, short_error).
- **Cost**: ~30 lines.
- **Caveat**: Don't include request body. Truncate `short_error` to 80 chars. Drop entries on key rotation.

#### CPU thermal / RAM-DIMM thermal
- **What**: One number: `cpu_tctl` from `k10temp` and the highest RAM module temp. Color-warn at >85 °C.
- **Why**: Inbox `final-state.md` line 153 says "CPU sat at 95 °C during heavy concurrent ComfyUI + daisy" and explicitly names it as a deferred fragility. Right now it's at 76.8 °C — fine. But during a long-form video render plus chat traffic, it climbs into thermal-throttling territory and the dashboard can't see it. The DIMMs are at 51 °C with their `high` threshold at 55 °C — also worth a quiet line.
- **Source**: `sensors` parse, or `cat /sys/class/hwmon/*/temp*_input`. Subprocess, ~5 ms.
- **Cost**: One subprocess call.
- **Caveat**: `sensors` output is not stable across distros. Pre-resolve the hwmon paths once at module load.

#### ComfyUI-side memory headroom
- **What**: ComfyUI's own view of `vram_free` from `/system_stats` (shows `11418140672` right now = 10.9 GiB). Show alongside the nvidia-smi number — if they disagree by >2 GB, surface a discrepancy badge.
- **Why**: ComfyUI's `vram_free` accounts for `--reserve-vram 9` (9 GB it deliberately won't touch). nvidia-smi shows total free. The two together let a user understand "image gen has 10.9 GB to work with right now even though nvidia-smi says 11.2 GB free" — important for the budgets card and for understanding why a request 503'd.
- **Source**: `GET http://127.0.0.1:7890/system_stats` (already probed for backend health). Pull `devices[0].vram_free`.
- **Cost**: Already paying for the request; just extract more fields.
- **Caveat**: System_stats `devices[]` is single-card on this host (only the GPU ComfyUI binds to). That's fine — call it out as "ComfyUI's view" so users know it's not GPU1.

#### Workflow drift indicator
- **What**: A small dot per workflow showing whether the `*.ui.json` twin is in sync with the API workflow. Red = stale. Run `scripts/check_workflow_drift.py` lazily and cache for an hour.
- **Why**: Outputs embed the UI workflow in metadata so dragging into ComfyUI rebuilds the graph. Stale UI twins → lying about reproducibility. The drift checker exists already (`scripts/check_workflow_drift.py`) but produces no surface a human checks.
- **Source**: Run the script subprocess, parse exit code, cache for 60 min. Or read the last cached run if the script writes status to a file.
- **Cost**: Subprocess takes ~10 s (drives Playwright). MUST be backgrounded with a long TTL — not in the 12 s status cache.
- **Caveat**: If Playwright isn't installed in the proxy venv, this needs a separate worker. Punt to a daily systemd timer that writes a JSON file the dashboard reads.

#### Voxtral / Parakeet richer health
- **What**: Voxtral health includes `vram_gb` (3.78 currently). Parakeet health includes `speedup` (20.7×) and the loaded model. Surface those.
- **Why**: TTS quality is a frequent question — the model name change between voxtral-int4 and voxtral-tts is something the dashboard could display directly. Parakeet's speedup metric is a useful "is the GPU still snappy" signal.
- **Source**: Already probed (`/health` calls). Parse the JSON and show 1-2 fields.
- **Cost**: Zero added probe; parse what's already fetched.
- **Caveat**: None.

#### Last-N-minutes Xid event window
- **What**: Currently shows the last 5 Xid 109 events from the past 24 h. Add: count of events in the last 60 min (high-frequency = imminent reboot needed), count in 24 h, and *also* surface Xid codes other than 109 (Xid 31 also appears in dmesg right now).
- **Why**: The inbox doc explicitly says "84+ llama-turbo crashes today" — 24 h is the wrong window for "is the host healthy *now*". A 1-hour window with a count gives a glanceable freshness signal. Xid 31 (MMU fault) is also in journals at 15:29:11.
- **Source**: Same `journalctl -k` parse, just two windows + multi-code regex.
- **Cost**: Same subprocess (already budgeted). Add ~5 lines.
- **Caveat**: None.

### Low value / nice to have

- **TabbyAPI loaded model name** — when up, surface the EXL3 model id from `/v1/models`. Today the catch-all says "Qwen3-VL" in the backend label but the dashboard never confirms which model is actually resident.
- **Image-edit vs image-gen workflow split** — distinguish `flux2-klein-4b` from `flux2-klein-4b-edit` in the workflows list (they're sibling rows now indistinguishable except by the `-edit` suffix).
- **VRAM-estimate cache age** — the budgets card shows top-5 estimates; add the count of total estimates and last-modified date for the file, so users know whether the adaptive batcher is operating on stale data.
- **Process restart counter** — show how many times the proxy systemd unit has restarted since boot. Recovery-from-Xid pattern would be visible.
- **OpenAI alias coverage hint** — surface the count of generated aliases in `openai_model_aliases.generated.yml` (size 11.2 K). Validates that the alias generator ran.
- **Comfy custom-node warnings** — `GET /extensions` and `GET /embeddings` can flag missing custom nodes; show count of embeddings and a warning banner if `/extensions` returns a load-error list. Useful after a ComfyUI upgrade.
- **Skill-doc drift hint** — note when `~/code/dotfiles/ai/skills/local/openai-vivid-fish/SKILL.md` was last updated. Helps users spot stale docs without leaving the page.
- **Streaming connection count** — number of active SSE connections through the proxy (per ASGI lifespan it's tracked indirectly).
- **Daisy audit-log freshness** — `~/applications/daisy-audit/audit.jsonl` mtime + line count. Daisy's most recent activity in one number.

## Recommended removals

- **`instantaneous_util_pct`** in the GPU strip. The status_endpoint.py:284 comment already calls it "misleading for LLM/diffusion workloads (memory-bandwidth-bound)". The in-flight Budgets agent is reportedly removing it from the UI; if so, drop the field from the JSON payload too. Otherwise: keep emitting but never show it on the public page.
- **The "recent_renders" alias regex when the file follows a different convention.** The current regex `^([A-Za-z0-9.\-]+?)_\d{4,}_` works for `ltx-2.3-distilled_00017_.mp4` but silently empties `alias` on anything off-pattern. Either drop the field when it's empty (instead of `""`) or compute alias from the parent dir / `image_workflow` JSON metadata instead of the filename.
- **Redundant `live_status` field in the backend grid.** Today it duplicates `status` 1-for-1 (both are computed identically per status_endpoint.py:88-110). The intent was that `status` could differ from `live_status` ("show as up because role is standby even though we couldn't reach it") — but the actual implementation never diverges. Either implement the divergence (e.g. role-aware `status`) or drop `live_status`. Right now it adds JSON size and reader confusion with no signal.

## Recommended restructuring

- **Banner section should answer a single question: "what should I do *right now*?"** Today it summarizes backend health. Make it a true incident-summary line that combines: any required-down (red), root-disk >95% (red), recent Xid surge (yellow), in-flight swap (yellow), nothing (green). Each red/yellow gets one click-to-detail.
- **Collapse "Models" + "Capabilities" into a single "What can I ask for?" card.** The matrix today lists 13 video aliases, 5 image workflows, 1 LLM, 1 TTS, 1 STT — all in three different cards. A single 2-column layout (left: chat/audio; right: image/video) would cut the page height by 40% and put related signals side-by-side. The capability matrix's wide table is overkill for the 5 unique workflows behind 13 aliases — group by workflow, list aliases as pills under each.
- **GPU strip + Budgets card should fuse.** Both describe the same scarce resource. Stack the budgets bar under the live VRAM bar so the user sees "2.3 GB remaining of 4 GB ComfyUI budget" instead of mentally subtracting.
- **Move "Recent Renders" up.** It's a high-confidence "this thing actually still works" signal. Today it sits below driver/system, which is the right home for kernel-level chatter but the wrong place for a sample of working output.
- **Footer should expose the OpenAPI surface link**, not just `/health` and `/v1/models`. Links to `/v1/audio/voices`, the SKILL.md (could be served as `/SKILL.md`), and `/v1/status` (raw) help anyone landing on the page without context.

## Out of scope / open questions

- Per-key counters require new in-process state. Acceptable for the current bare-VPS deployment, but if the proxy ever runs >1 worker, counters need to move to a shared store (sqlite, redis). Decide before implementing.
- Recent-failures buffer leaks user identity and endpoints into a public-rendered page. Status page is bearer-protected (`/v1/status` requires auth) but the rendered HTML at `/` doesn't display sensitive data today. Decide whether the page itself should require auth before shipping per-key panels.
- "Recent renders" thumbnails would massively improve the visual confirmation, but require either signed image URLs in the page or a separate gallery endpoint with its own caching. Worth discussing whether the image cache (already content-addressed) should serve as the source.
- Workflow-drift checker depends on Playwright. Decide whether to run it from a daily systemd timer (writing a JSON status file) or to pre-bake it into a lightweight static-comparison checker.
- The skill doc claims things the dashboard could verify — e.g., "vision routing emits `X-Vision-Routed` header on every chat-completions response". A "self-test pass count" panel that pings `/v1/chat/completions` with a known payload nightly would surface regressions, but is meaningfully more code than the rest of this report.

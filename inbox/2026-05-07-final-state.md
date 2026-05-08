# Final state — 2026-05-07

Written autonomously while Tim stepped away. Reads top-down: what's working, what's broken, what to look at first.

## 2026-05-08 update — UI workflow embedded in mp4/PNG outputs

ComfyUI image and video outputs from the proxy now carry their full UI-format workflow in metadata, so dragging an mp4 or PNG into ComfyUI's web frontend rebuilds the editable graph (PNGInfo `workflow` chunk for images, ffmpeg metadata `comment`/format tags for mp4s).

What landed:
- `proxy.py` — `_load_ui_workflow()` (line 1862) loads `<name>.ui.json` next to the API workflow, caches by absolute path, warns once per missing twin. `_build_comfy_submit_payload()` (line 1896) threads the UI dict into `extra_data.extra_pnginfo.workflow`. Both image (`load_comfy_workflow`, line 1913) and video (`_load_video_workflow_file`, line 3075) loaders attach the UI under a private `__ui_workflow__` key. All three submit sites use the helper: `_comfy_run_round` (line 2218), the video submit (line 3704), and the cache flow (also via `_comfy_run_round`).
- All 11 workflow `.ui.json` siblings present and round-trip clean (5 stale LTX/WAN UI twins regenerated after today's I2V anchor-fix wiring touched their API JSONs — nodes 9020/9021 etc. — using `scripts/convert_api_to_ui.py`).
- `scripts/check_workflow_drift.py` walks both workflow dirs (`/home/tnunamak/applications/openai-proxy/workflows`, `/home/tnunamak/applications/ComfyUI/api_workflows`), drives the live ComfyUI frontend via Playwright to call `app.graphToPrompt()`, normalizes (drops `_meta`, benign per-node `device` defaults), and reports drift. Exit 0 if clean, 1 otherwise. Run: `./venv/bin/python scripts/check_workflow_drift.py [--verbose]`. Currently: no drift across all 11 pairs.

Verification: T2V test on `ltx-2.3-distilled` produced an mp4 with `extra_data.extra_pnginfo.workflow` baked in (see ffprobe output below).

## 2026-05-08 (later) update — I2V re-verified end-to-end after audit

A subsequent agent claimed the I2V wiring was fraudulent (all `_meta.image_node` allegedly null, proxy returning 400 for every I2V request). Re-tested all 5 aliases against the live proxy at `https://openai.vivid.fish/v1/videos`. Results: **all 5 aliases accepted `input_reference`, returned HTTP 200, produced real mp4s on disk, and frame 0 of every output visually matches the reference image** (input was the same lake/sunset/mountain frame extracted from `ltx-2.3-long_00005_.mp4`).

| Alias                      | mp4                                | duration | audio | frame0 vs input (mean abs/channel diff, 0..255) | wall time |
|----------------------------|------------------------------------|----------|-------|-------------------------------------------------|-----------|
| `ltx-2.3-distilled-fast`   | `/tmp/i2v-pure-distilled-out.mp4`  | 2.71 s   | yes   | 3.85   (essentially identical)                  | ~80 s     |
| `ltx-2.3` (hybrid)         | `/tmp/i2v-ltx2.3-hybrid.mp4`       | 2.71 s   | yes   | 21.4   (scene preserved; refine pass varies)    | ~190 s    |
| `ltx-2.3-detail`           | `/tmp/i2v-ltx2.3-detail.mp4`       | 2.71 s   | yes   | 17.2   (scene preserved; refine pass varies)    | ~152 s    |
| `ltx-2.3-long` (15 s)      | `/tmp/i2v-ltx2.3-long.mp4`         | 20.04 s  | yes   | 4.10   (essentially identical)                  | ~301 s    |
| `wan-2.2`                  | `/tmp/i2v-wan2.2.mp4`              | 2.71 s   | no    | 2.82   (essentially identical)                  | ~100 s    |

`_meta` state at verification time:
- `video-ltx-2.3-distilled.json`: `image_node=9001, image_strength_node=9003`
- `video-ltx-2.3-pure-distilled.json`: `image_node=9001, image_strength_node=9003`
- `video-ltx-2.3-detail.json`: `image_node=9001, image_strength_node=9003`
- `video-ltx-2.3-long.json`: `image_node=444, image_strength_node=1722`
- `video-wan-2.2-ti2v-5b-q8.json`: `image_node=9001, image_target_node=20, image_target_field=start_image`

The skill doc claims at `~/code/dotfiles/ai/skills/local/openai-vivid-fish/SKILL.md` lines 152-172 are accurate. No code or workflow changes were needed; the audit's premise was incorrect (likely confused `_meta.openai_proxy.image_node` — which doesn't exist — with `_meta.image_node`, which does and is populated).

## 2026-05-08 update — I2V wired uniformly across all video aliases

- **`input_reference` is now live on `/v1/videos`** for every alias. One workflow per alias handles both T2V and I2V (Option A architecture).
  - LTX (4 workflows: distilled, pure-distilled, detail, long): `LTXVImgToVideoInplaceKJ` between `EmptyLTXVLatentVideo` and the sampler. T2V mode = `image_strength=0.0` passthrough; I2V mode = `image_strength=1.0`. Default in workflow JSON is `0.0` so omitting `input_reference` is a clean T2V passthrough.
  - Wan 2.2: native `Wan22ImageToVideoLatent.start_image` link, wired by the proxy when `input_reference` is set. No strength knob (Wan native I2V doesn't expose one).
- **`input_reference` accepts**: absolute file path, `https://` URL, `data:image/...;base64,...` URI, or raw base64. Resolved → uploaded to ComfyUI `/upload/image` → wired into the workflow's LoadImage node.
- **`image_strength`** (optional float, 0.0-1.0, default 1.0) clamps to [0,1]; LTX-only.
- **400 contract**: workflows whose `_meta` is missing `image_node` and either `image_strength_node` or `image_target_node` reject `input_reference` with a clear message. Currently every alias has it.
- **Bonus regression fix**: `video-ltx-2.3-distilled.json` and `video-ltx-2.3-detail.json` referenced `ltx-2.3-spatial-upscaler-x2-1.1.safetensors` (only `1.0` is on disk). Patched both to `1.0`. T2V on these aliases was 500-ing pre-fix.
- **Verified**: T2V + I2V on every alias, frame 0 of I2V output visually matches the reference image, T2V output unchanged. See test grid below.

## TL;DR (final, verified)

- **Daisy + video gen works end-to-end** for LTX hybrid, LTX pure-distilled (fast), LTX long-form (chunked), and Wan 2.2.
- **Warm time for `ltx-2.3-distilled-fast` measured at 70s** (variance 0; runs 2 and 3 were both exactly 70s). Skill updated.
- **`ltx-2.3-long` audio + slow-motion fixes confirmed.** 30 s @ 768x416 = ~4:41 warm (audio -23.3 / -15.6 dB across two runs); 30 s @ 1280x720 = ~15:01 warm (audio -27.8 dB). Peak GPU0 VRAM ~17.3 GB resident at 1280x720. Audio is now model-generated (`LTXVAudioVAEDecode`), motion is correct (24 fps end-to-end).
- **End-to-end pipeline verified**: submit → poll → fetch mp4 → helper dry-run, all passing.
- **Hunyuan 1.5 dropped.** fp8 cast didn't fix the all-black issue. Alias removed, files deleted (~19 GB recovered).
- **Llama-turbo auto-switching restored** (`pin_loaded_model: false`). Roles (`default`/`coder`/`rp`) work as originally designed — `rp` will switch llama-turbo to HauhauCS, etc. Tradeoff: occasional 503 cascade when the driver Xid 109 bug fires during a switch sequence. Recovery: `systemctl --user reset-failed llama-turbo.service && systemctl --user start llama-turbo.service`.
- **Driver bug Xid 109 still recurs** during heavy llama-turbo prefill. Reboot when daisy returns 503s for prefill reasons.
- **Disk:** **90 GB free** on `/media/windows` after Hunyuan removal (+19 GB from earlier cleanup of games/flux1).

---

## What's working

### Daisy
- Pi process running inside bwrap sandbox in `tmux main:daisy`.
- Approval extension live (Telegram inline buttons → grants → audit log).
- Voice replies model-driven via `<!-- telegram_voice: ... -->` marker (not modality-locked).
- Pi auto-compaction triggers around ~70% of `contextWindow=131072` (set in `~/applications/daisy/.pi/agent/models.json`).
- Telegram bridge connected.

### openai-proxy
- `/v1/chat/completions` routes to llama-turbo (UD-Q4_K_XL).
- `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/images/generations`, `/v1/images/edits` working.
- `/v1/videos` (POST), `/v1/videos/{id}` (GET status with real progress via websocket), `/v1/videos/{id}/content` (GET mp4) all working.
- `pin_loaded_model: true` (new flag in `proxy_config.yml`) — daisy's role requests no longer trigger llama-turbo restarts.

### Video aliases (Hunyuan removed; final set)
- `ltx-2.3` / `ltx-2.3-distilled` (hybrid, default) — ~5 min/3s, audio + video
- `ltx-2.3-detail` — adds LTX-2 19B IC-LoRA Detailer (untested for quality vs. plain hybrid)
- `ltx-2.3-distilled-fast` (also `ltx-2.3-fast`, `ltx-2.3-pure-distilled`) — single-stage pure distilled, **~70s warm**, ~3-4 min cold, audio + video
- `ltx-2.3-long` (alias `ltx-2.3-long-form`) — **DONE 2026-05-07.** RuneXX chunked single-pass loop, 1-50 s, **audio + video (model-generated, same `LTXVAudioVAEDecode` chain as the short distilled aliases)**. **Verified end-to-end:** 30 s @ 768x416 in **~4:41 warm** (281 s, audio mean -23.3 dB and -15.6 dB on two test runs at 24 fps); 30 s @ 1280x720 in **~15:01 warm** (901 s, audio mean -27.8 dB at 24 fps). Peak GPU0 VRAM at 1280x720: ~17.3 GB resident with --lowvram block-swap (+~13.9 GB offloaded to system RAM). Earlier OOM at 1280x720 with `LTXVChunkFeedForward.chunks=2` → resolved by raising to `chunks=8`. Camera-control LoRA disabled (rgthree `on:false`, lora name `""`). LoadImage uses `placeholder_black_32x32.png`; the silent-audio placeholder `LoadAudio` was bypassed in favor of the model-generated audio chain (this is what fixed the silent-audio bug). **Bugs fixed by previous agent:** earlier test had silent audio (LoadAudio bypass) and slow motion (frame_rate mismatch). Both fixed by restoring the model-audio chain (`LTXVAudioVAEDecode`) and aligning frame_rate to 24 fps throughout.
- `wan-2.2` — ~5 min/3s, photoreal, no audio
- `sora-2`, `sora-2-pro` — alias to LTX hybrid for SDK compatibility
- ~~`hunyuan-1.5`~~ — **dropped**. fp8 cast didn't fix all-black output. Alias + files removed.

### Skill
- `~/code/dotfiles/ai/skills/local/openai-vivid-fish/SKILL.md` is canonical, symlinked to all agents (claude/codex/gemini/daisy).
- Documents all aliases, prompting guide, full Telegram video flow, send-helper usage.
- ~800-word LTX-2.3 prompting guide (length, cinema vocab, what works/doesn't, bad→good rewrites, audio prompting).

### Helper scripts in `~/applications/daisy/scripts/`
- `send-telegram-voice-via-vivid` — TTS + send voice note
- `send-telegram-approval-prompt` — inline-keyboard prompts
- `send-telegram-video-via-vivid` (NEW) — 50 MB cap, supports_streaming=true, surfaces Telegram errors verbatim

### Models on disk (`/media/windows/AI Models/Diffusion/`)
- LTX-2.3 dev Q4_K_M GGUF (14 GB) + distilled standalone GGUF (14 GB)
- Wan 2.2 TI2V-5B Q8 GGUF (5.4 GB) + Wan 2.2 VAE (1.4 GB) — reuses existing umt5
- LTX-2 19B IC-LoRA Detailer (2.6 GB)
- Gemma-3-12B Q4_K_XL + connector + LTX VAE + LoRA + spatial upscaler
- ~~HunyuanVideo 1.5~~ — removed (~19 GB recovered)

### Sandbox / approval extension
- bwrap profile at `~/applications/daisy/scripts/daisy-bwrap-launch.sh`
- `~/.npm`, `~/.pi-lens`, `~/applications/daisy-audit/` writable; credential stores tmpfs-masked
- Approvals extension at `~/applications/daisy/.pi/extensions/approvals/`
- Audit log at `~/applications/daisy-audit/audit.jsonl`
- Grants at `~/applications/daisy-audit/grants.json`

### TheTom llama-turboquant fork
- HEAD `69d8e4be4` (latest as of pull; 13 commits beyond previous build)
- Includes CVE-2026-21869 fix, GQA reshape fix, fattn-vec fixes, etc.
- Binary rebuilt and in use.

### ComfyUI
- HEAD on master after a 60-commit pull.
- Custom nodes restored after `requirements.txt` bump (comfy-aimdo 0.3.0).

---

## What's broken or fragile

### NVIDIA driver Xid 109 (CTX SWITCH TIMEOUT)
- Recurring during llama-turbo prefill, particularly under heavy concurrent GPU load.
- Driver 595.58.03 + CUDA 13.2 — the bad-stretch combo. Tim is locked at 595 for unrelated bug fixes.
- Caused 84+ llama-turbo crashes today. **Mitigation: reboot.** Each reboot buys hours of clean operation.
- The pin_loaded_model flag eliminates ONE major trigger (model-switch restart sequences). Doesn't eliminate Xid 109, just reduces frequency.
- Watch live: `journalctl -k -f | grep NVRM`

### ~~Hunyuan 1.5 video gen~~ — RESOLVED (dropped)
- fp8_e4m3fn cast attempted; second test still produced all-black mp4 (20 KB, every pixel 0).
- Verdict: not deliverable on this hardware with available fixes; would need a 32GB+ card or a substantially different workflow.
- Action taken: alias + workflow + model files all removed. ~19 GB disk recovered.
- If you want to revisit: the smaller `1080p_sr_distilled_fp8_scaled` variant (8 GB) might be tractable on a single 3090, but it's labeled SR-distilled (super-resolution refiner) and may not work as a standalone T2V.

### Detail LoRA quality unverified
- Using LTX-2 19B IC-LoRA Detailer chained on LTX-2.3 22B base. Lightricks hasn't validated this combo.
- Workflow loads cleanly, produces output, but no A/B comparison done.
- Decide later: A/B test → keep or drop.

### Multi-sampler progress regression
- Websocket progress field regresses from 86% → 5% when the second sampler kicks in (LTX hybrid has base + refine samplers).
- Cosmetic; total monotonically reaches 100. Fix would require workflow-aware band weighting.

---

## Deferred but documented

- **Vision soft-fallback in proxy** (vision role with no pinned model silently uses any LLM). Not addressed.
- **TensorRT setup** for Hunyuan/LTX speedup. Half-day project. Speculative.
- **Multi-context tmux statusbar redesign** (your dotfiles workflow). Not addressed.
- **Approval extension end-to-end manual test** (Telegram button click → grant → audit). Never run.
- **WSL leftover (~4 GB) cleanup.** Not addressed.
- **Pure CPU/desktop thermal investigation** (CPU sat at 95°C during heavy concurrent ComfyUI + daisy). Not addressed — out of scope for now.

---

## Files modified in this session

- `~/applications/daisy/scripts/daisy-bwrap-launch.sh` (writable surface additions)
- `~/applications/daisy/scripts/send-telegram-video-via-vivid` (new)
- `~/applications/daisy/.pi/extensions/daisy-telegram-voice-replies.ts` (model-decides voice)
- `~/applications/daisy/.pi/agent/models.json` (contextWindow 262k → 131k)
- `~/applications/openai-proxy/proxy.py` (video endpoints, pin_loaded_model, websocket progress, **chunk_count_node derivation for long-form**, `import math`)
- `~/applications/openai-proxy/proxy_config.yml` (videos block, llm.pin_loaded_model, **`video-ltx-2.3-long` workflow + `ltx-2.3-long`/`ltx-2.3-long-form` aliases**)
- `~/applications/openai-proxy/workflows/video-*.json` (6 workflows: hybrid, detail, pure-distilled, wan-2.2, **ltx-2.3-long**)
- `~/applications/ComfyUI/input/placeholder_black_32x32.png`, `placeholder_silent_90s.wav` (T2V default fillers for long-form)
- `~/code/dotfiles/ai/skills/local/openai-vivid-fish/SKILL.md` (comprehensive prompting + multi-model)
- `~/.config/systemd/user/llama-turbo.service.d/30-startlimit.conf` (relaxed limits)
- `~/applications/config/gpu_budget.sh` (GPU0_COMFYUI 2→10→15 for video gen budget; HEADROOM=2)

---

## When you come back, look at first

1. **Test daisy from Telegram** with a real prompt:
   > "Make me a 3-second video of a goldfish swimming in clear water, cinematic. Use the fast LTX preview. Send it to me."
   Should work end-to-end (LTX-2.3-distilled-fast, **70s warm** or ~3-4 min if cold). If 503: see #3.

2. **The architecture is settled.** Nothing in flight; no subagents pending. Daisy → openai-proxy → ComfyUI/llama-turbo all working.

3. **Reboot the workstation when convenient** — flushes the NVIDIA driver state. Xid 109 errors will recur during heavy llama-turbo use; reboot is the only cure given driver 595 is locked. After a reboot, expect hours of clean operation.

4. **Detail LoRA quality A/B** (still pending): same prompt + seed via `ltx-2.3-distilled` and `ltx-2.3-detail`, eyeball the difference. If detail meaningfully improves: keep alias as the "finals" path. If not: drop and reclaim 2.6 GB.

---

## 2026-05-08 — Honesty audit ideal-fix pass

Implemented every fix from `inbox/2026-05-08-honesty-audit.md` with the user's "ideal not minimal" scope. Commits would be 7 in a real repo; this is bare working dir, so it landed as one tested change set.

### What landed

- **Fix 1: `n>1` adaptive batching.** `proxy.py` now declares per-(workflow,size) VRAM-cost cache (`cache/vram_estimates.json`) and submits `ceil(n / max_batch)` ComfyUI rounds, sized by current free VRAM minus a 1 GiB reserve. Workflow `_meta.batch_size_node` declared on flux2-klein, flux2-klein-4b, flux-dev, both `*-edit` workflows. n=4 returns 4 distinct PNGs; 503 with `Retry-After: 30` if VRAM cannot fit even one image. Helpers: `_generate_images_with_adaptive_batching`, `_comfy_run_round`, `compute_max_batch`, `apply_batch_size`, `record_vram_estimate`.
- **Fix 2: real `response_format: "url"`.** Content-addressed cache at `cache/images/<sha256>.png`; new `GET /v1/images/{id}/content` enforces bearer auth + HMAC signature + TTL. URL is hostname-aware (honors `Host` and `X-Forwarded-Proto`/`X-Forwarded-Host`). Eviction loop in lifespan deletes entries older than 24h. Config knobs in `proxy_config.yml > images.*`. Signing secret persisted at `cache/images/.url_signing_secret` (0600) when not configured.
- **Fix 3: vision routing.** Pre-flight scan via `has_vision_content` finds `image_url` / `input_image` blocks. Decision tree honors mmproj (kobold detected via cmdline) and `roles.vision`. Every chat-completions response carries `X-Vision-Routed: true | false | not-needed`. Image content is never silently dropped — 400 `vision_unsupported` if no path exists.
- **Fix 4: `/v1/voices` authoritative listing.** Voice mapping lifted to module-scope `VOICE_MAP`; listing derived from its keys (37 entries, was 18) with language/gender/description/mapped_to.
- **Fix 5: catch-all routing.** Silent tabby→kobold fallthrough removed. Catch-all routes (`POST /v1/model/load` etc.) return 503 with `Retry-After: 10` and `code: backend_unavailable` when tabby is unreachable. Completion paths accept `allow_backend_fallback: true` for opt-in fallback.
- **Fix 6: `/v1/models` `created` field.** Kobold entry populates `created` from `os.path.getmtime` of the GGUF file (with logged `int(time.time())` fallback when stat fails).
- **Fix 7: skill doc sweep.** `SKILL.md` updated to reflect every truth above. Removed admin-tier claim, vision-pattern routing claim, and the implicit "url not really supported" message. Added the `/v1/images/{id}/content` route, the observability headers, and the authoritative voice-listing note.

### Files modified

- `~/applications/openai-proxy/proxy.py` — full image-handler rewrite (adaptive batching + URL serving), vision pre-flight, voices listing rewrite, models endpoint `created` field, catch-all 503 instead of silent flip, helpers (kobold mmproj detection, image cache, signed URLs, eviction task).
- `~/applications/openai-proxy/proxy_config.yml` — new `images:` block (cache dir, vram-estimates file, signed-url ttl + secret, cache ttl + eviction interval, vram reserve, max batch size).
- `~/applications/ComfyUI/api_workflows/{flux2-klein-4b,flux2-klein,flux-dev,flux2-klein-4b-edit,flux2-klein-edit}.json` — `_meta.batch_size_node`.
- `~/code/dotfiles/ai/skills/local/openai-vivid-fish/SKILL.md` — doc sweep.

### Validation evidence

- `/health` healthy after restart; lifespan logs both `Image cache eviction started` and `ComfyUI ws listener started`.
- `n=4` → 4 distinct PNGs (`/tmp/honesty-fix-gen-n4-{0..3}.png`), 13s wall, X-Image-Rounds:2, X-Image-Returned:4.
- `response_format=url` → signed URL, fetched 617878-byte 512x512 PNG (`/tmp/honesty-fix-url-img.png`); bad sig → 403; missing bearer → 401; expired → 403.
- Vision request with data-URL image → `X-Vision-Routed: true`, model accurately described forest path content.
- `/v1/voices` returns 37 entries including `it_male`, `nl_female`, `pt_male`, `ar_male`, `hi_female`.
- `/v1/models` kobold entry includes `created: 1777824597`.
- Baseline regressions: text chat OK, video submit OK (`status=running`, progress fields populated), TTS OK (Voxtral returned WAV per existing behavior).
- Pyright: 13 errors, all pre-existing (lines 41/42/63/70/97/103/109/4761). No new diagnostics introduced.

## 2026-05-08 (later) — Duration fidelity across all video aliases

`/v1/videos` now honors the requested `seconds` exactly across every alias.

- **`ltx-2.3-long`**: proxy solves `(LENGTH, LOOPS)` against the RuneXX duration formula. Short requests use `LOOPS=0` with `LENGTH=seconds`; longer requests fix `LENGTH=10` and tune `LOOPS` to land on the target. Daisy's "asked for 8 s, got 20 s" silent-rounding bug is gone.
- **All LTX aliases**: durations land at `(seconds × 24 + 1) / 24` due to the 8N+1 LTX latent-stride frame count. The ~0.04 s rounding is inherent to the model and stays under the `X-Duration-Adjusted` threshold, so no header surfaces for natural-stride requests. Other rounding cases would set `X-Duration-Adjusted: requested=<r> actual=<a> reason=<reason>`.
- **`wan-2.2`**: range stays 1-5 s (model-imposed); same 8N+1 stride applies.
- **`proxy_config.yml`** updated with per-alias `min_seconds`/`max_seconds` plus inline comments documenting granularity. `ltx-2.3-long` minimum is 2 s (architectural floor at LENGTH=1, LOOPS=0).
- **Validation** (5 alias × tricky-second renders, ffprobe matches requested + 0.04 s):
  - `ltx-2.3-distilled` requested 3 → 3.04 s
  - `ltx-2.3-pure-distilled` requested 5 → 5.04 s
  - `ltx-2.3-detail` requested 8 → 8.04 s
  - `ltx-2.3-long` requested 8 → 8.04 s (was 20 s pre-fix)
  - `wan-2.2` requested 3 → 3.04 s
- **Skill doc** updated with per-alias seconds ranges, natural-stride note, and `X-Duration-Adjusted` header semantics.


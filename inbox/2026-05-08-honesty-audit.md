# OpenAI proxy honesty audit — 2026-05-08

## TL;DR

- `n` parameter on `/v1/images/generations` and `/v1/images/edits` is silently capped at 1: the workflow only generates one image, then the proxy slices `[:n]`. Doc lists `n` as a supported field, OpenAI clients expecting `n=4` get `n=1`. **P0**.
- `response_format: "url"` on image gen is silently downgraded to `b64_json`: the doc explicitly lists `url` as a valid value, but the code returns `b64_json` regardless and the comment even admits it's "not implemented". **P1**.
- `/v1/models` doc claim that it "needs an admin-tier key" is wrong: any valid proxy key works; the proxy uses its own server-side `ADMIN_KEY` to talk to Tabby. **P2**.
- Doc claim "vision messages auto-route to TabbyAPI when model name matches a vision pattern (`vl`, `vision`, …)" is misleading: those patterns only set Tabby's `vision: true` flag. Routing is by filesystem detection (`detect_model_backend`) for unknown names, by role for role names, and by alias→default-chat for OpenAI aliases — never by the listed patterns. With kobold hot and an OpenAI alias request carrying images, the request goes to kobold and the image content silently disappears. **P1**.
- `/v1/voices` lists fewer voices than `/v1/audio/speech` accepts (no `it_*`, `nl_*`, `pt_*`, `ar_male`, `hi_*`). The voice mapper accepts those, the listing endpoint hides them. **P2**.

## Methodology

Read the SKILL.md cover to cover (470 lines), then read all 4031 lines of `proxy.py` plus `proxy_config.yml` and the workflow `_meta` blocks. For each documented operation I located its handler, traced the request/response transformation, and noted parameters that are read vs ignored. I verified flat `_meta.<field>` schema by reading the consuming code (lines 2782–3012), confirming I2V wiring is real for all configured workflows. I did not run live HTTP probes — all findings are from code inspection. No edits were made.

## Confirmed honesty gaps

### `n` parameter on image endpoints is silently capped at 1
- **Claim**: SKILL.md line 79 example shows `"n":1`, line 110 lists "`n` (optional, int, default 1)" as a documented multipart field for `/v1/images/edits`, line 79/110 doc both imply OpenAI's standard `n` semantics ("how many images to generate").
- **Reality**: `proxy.py:1809` and `proxy.py:1987` read `n` but the ComfyUI workflows are submitted once and produce one image; the only use of `n` is `images_data[:n]` (`proxy.py:1938`, `proxy.py:2174`) — slicing a 1-element list. `n=4` returns 1 image with no warning.
- **Severity**: **P0** — this is exactly the "endpoint accepts a parameter but ignores it / returns 200 instead of warning" pattern.
- **Suggested fix**: Either (a) loop the workflow submission `n` times in the handler (preferred — matches OpenAI semantics), or (b) reject `n>1` with a 400 and stop documenting it.

### `response_format: "url"` is silently downgraded to `b64_json`
- **Claim**: SKILL.md line 111 lists "`response_format` (optional): `b64_json` (default) or `url`" as a documented multipart field for `/v1/images/edits`. Image-gen recipe (line 79) uses `b64_json` but the doc still implies `url` is valid.
- **Reality**: `proxy.py:1810` reads `response_format`, but the only place it's consulted is `proxy.py:1919-1924`, where the `else` branch (anything other than `b64_json`) base64-encodes the image anyway with the comment `# For URL format, we'd need to serve the image - not implemented`. The image-edit handler (`proxy.py:2159-2160`) doesn't even branch on `response_format` — always returns `b64_json`. So a client that asks for `url` gets a `b64_json`-shaped response (the `data[].url` field never appears).
- **Severity**: **P1** — wrong response shape for OpenAI clients that branch on `data[0].url` vs `data[0].b64_json`.
- **Suggested fix**: Either implement `url` (proxy serves the file) or 400 on `response_format=url` with a clear "url not implemented" message and remove `url` from the doc.

### `/v1/models` does not require an admin-tier key
- **Claim**: SKILL.md line 44: "Everything except `/health` requires the bearer token. `/v1/models` and `/v1/model/list` need an admin-tier key."
- **Reality**: The proxy's auth (`authenticate_request`, `proxy.py:825-843`) has no notion of admin tiers — any key in `AUTH_REGISTRY` passes. `apply_tabby_auth` (`proxy.py:907-918`) injects the *server's* `ADMIN_KEY` (or downstream secret from the matched identity) on the way to Tabby. So the user-facing key just needs to be valid; "admin-tier" is a fiction the doc invented.
- **Severity**: **P2** — misleading but defensive (doc claims more restriction than reality).
- **Suggested fix**: Doc edit — remove the admin-tier claim or implement a tier system.

### "Vision auto-routes to TabbyAPI when model name matches a vision pattern" is misleading
- **Claim**: SKILL.md line 60: "Vision: send a chat message with `image_url` content parts; auto-routed to TabbyAPI when the model name matches a vision pattern (`vl`, `vision`, `pixtral`, `llava`, `cogvlm`, `internvl`)."
- **Reality**: `VISION_MODEL_PATTERNS` (`proxy.py:161-168`) and `is_vision_model` are used in exactly one place: `proxy.py:3912` sets `body_json["vision"] = is_vision_model(...)` for Tabby. They do *not* drive routing. Routing logic (`proxy.py:3732-3823`) prioritizes:
  1. Literal GGUF path → kobold
  2. Role name → role's backend
  3. OpenAI alias (`infer_openai_model_category`) → soft path; honor preferred backend if hot, else fall back to "whichever backend is hot"
  4. Else → `ensure_backend_for_model` (`detect_model_backend` filesystem scan)
  None of these paths look at `VISION_MODEL_PATTERNS`. With kobold hot and a request `model: "qwen3-vl-30b"`, path 4 may flip to tabby via filesystem detection — but the doc's stated mechanism (pattern match) isn't what actually fires.
  Worse: with kobold hot and an OpenAI alias like `gpt-4o` carrying image content, path 3 keeps the request on kobold (`proxy.py:3805-3813` — soft path, no auto-swap), and the `image_url` content is silently dropped or ignored.
- **Severity**: **P1** — the doc tells users vision routing is automatic via name patterns; the actual contract is "filesystem detection or role-based, and OpenAI vision aliases stay on whatever's hot."
- **Suggested fix**: Doc rewrite — describe the real routing rules and warn that OpenAI vision aliases (`gpt-4o`, `gpt-4-vision-*`) won't auto-switch backends.

### `/v1/voices` lists a strict subset of voices the speech endpoint accepts
- **Claim**: SKILL.md line 31 lists `/v1/voices` as "List TTS voices". Lines 414-429 lists the *speech endpoint's* full mapping including `it_female/male`, `nl_female/male`, `pt_female/male`, `ar_male`, `hi_female/male`.
- **Reality**: `/v1/voices` handler (`proxy.py:3563-3593`) hard-codes 18 voices: 6 OpenAI aliases + 5 native English + only `fr_*`, `de_*`, `es_*`. The `VOICE_MAP` (`proxy.py:1575-1596`) accepts all the `ar_male`, `hi_*`, `it_*`, `nl_*`, `pt_*` IDs as native pass-through. So a client that lists voices and uses one of those listed cannot discover `it_male`, but if they happen to send it, it works.
- **Severity**: **P2** — incomplete listing, not silently broken; just inconsistent.
- **Suggested fix**: Code edit — add the missing native voices to `list_voices` so the listing matches `VOICE_MAP`.

### Catch-all "proxy to TabbyAPI" can fall through to kobold
- **Claim**: SKILL.md line 42: "`* /<any other>` | tabby | Catch-all proxy to TabbyAPI (admin/model load endpoints, etc.)".
- **Reality**: `proxy_request` (`proxy.py:3713-4000`) defaults to `TABBY_HOST` but `proxy.py:3855-3866` flips to `KOBOLD_HOST` if Tabby is unhealthy and kobold is the active backend. So a `POST /v1/model/load` issued while llama-turbo is hot would silently hit kobold (which has no such endpoint, so a 404 would result — not a 200 lie, but the doc's "Catch-all proxy to TabbyAPI" implies tabby always).
- **Severity**: **P2** — error path is honest (404), but the doc oversells the routing guarantee.
- **Suggested fix**: Doc edit — note that the catch-all may route to kobold when tabby isn't running.

### Models response shape: kobold-loaded GGUF entry lacks `created`
- **Claim**: SKILL.md line 30: "Returns the active backend's catalog plus the currently-loaded kobold GGUF if any."
- **Reality**: `proxy.py:3699-3703` appends `{"id": loaded_gguf, "object": "model", "owned_by": "kobold"}` — no `created` timestamp. OpenAI's models endpoint always includes `created` (Unix ts) on every model object. SDKs that destructure `created` get `undefined`/None for the kobold entry only.
- **Severity**: **P2** — minor schema gap; SDKs typically tolerate missing `created` but it can fail strict validators.
- **Suggested fix**: Code edit — include `"created": int(time.time())` in the appended entry.

## False positives investigated

- **I2V wiring across all video aliases** — confirmed real. `_meta.image_node` and either `_meta.image_strength_node` (LTX) or `_meta.image_target_node`+`_meta.image_target_field` (Wan) are present in `video-ltx-2.3-distilled.json`, `video-ltx-2.3-pure-distilled.json`, `video-ltx-2.3-detail.json`, `video-ltx-2.3-long.json`, `video-wan-2.2-ti2v-5b-q8.json`. The proxy consumes them at `proxy.py:2982-3012` via flat schema — matches the workflow JSONs.
- **`_meta` schema flat vs nested** — code uses flat `meta.get("image_node")` etc. (`proxy.py:2784-2792`, `2910`, `2982`); workflow JSONs match. No nested `_meta.openai_proxy.*` indirection in this codebase.
- **`seed` parameter on `/v1/videos`** — honored (`proxy.py:2857-2868`), threads through to all `seed_nodes` listed in `_meta`, including the `value` key for INTConstant pattern noted in the comment.
- **`negative_prompt` on `/v1/videos`** — honored (`proxy.py:2800-2802`).
- **`image_strength` clamp + default 1.0** — honored (`proxy.py:2997-3000`).
- **Wan has no strength knob** — confirmed: Wan workflow has `image_target_node` but not `image_strength_node`, so the strength code-path no-ops, matching SKILL.md line 158, 172.
- **`status=failed` includes truncated `error`** — confirmed `proxy.py:3168-3176`, truncates JSON-stringified message to 500 chars.
- **`progress`, `step`, `total`, `node`, `phase` fields during a run** — confirmed populated from the live websocket listener (`proxy.py:2286-2375`) with history fallback (`proxy.py:3115-3150`).
- **Voice mapping defaults to `neutral_female`** — confirmed `proxy.py:1597`, `1612`.
- **TTS forwards `speed`/`response_format` to Voxtral** — confirmed; the body is forwarded after voice/model swap (`proxy.py:1611-1624`). Whether Voxtral honors `opus`/`speed` is a backend question, not a proxy honesty gap.
- **Streaming chat completions** — works: `proxy.py:3934-3950` detects `text/event-stream` and streams. Doc doesn't claim or deny streaming.
- **`/health` is public** — confirmed (`proxy.py:652`, `805`).

## Areas not audited

- **TabbyAPI auto-load and idle-unload behavior** — read but not deeply checked for honesty gaps; doc doesn't make user-facing claims about idle behavior.
- **Voxtral and Parakeet response shapes** — backend-internal; the proxy's job is to forward bodies and shape responses match upstream. Not in scope for proxy honesty.
- **`/v1/embeddings` with OpenAI aliases** — `default_model_for_operation("embeddings", …)` returns `""`, which raises `ModelCompatibilityError` ("No local default model is configured for embeddings requests"). Doc shows embeddings as supported but doesn't promise OpenAI-alias rewrite for embeddings, so I called this acceptable. Worth reviewing if you start documenting embedding aliases.
- **The full `/v1/chat/completions` SDK response shape** (usage tokens, finish_reason, tool calls) — these are produced by the LLM backend, not the proxy. Not a proxy honesty surface unless the doc claims specific fields (it doesn't).
- **Auth race conditions, rate limits, IPv6, TLS** — out of scope.
- **`generate-image-via-vivid` and other daisy helpers** — not read in this pass.

## Open questions for the user

1. Should `n>1` on image gen/edit loop the workflow (slower but truthful) or 400 (reject)? Looping is the OpenAI contract; rejecting is cheaper.
2. Is the `/v1/models` admin-tier claim a leftover from a planned tier system, or just incorrect doc text? If you want admin-only model listing, that needs code, not just doc.
3. For the vision-routing doc claim: do you want vision auto-switching to actually happen on OpenAI aliases (`gpt-4o` → switch to tabby when image content present), or do you want the doc to honestly say "use the `vision` role"?
4. The `created` field missing from the appended kobold model entry is trivial to fix in code — fix or leave?

## Status (2026-05-08, ideal-fix pass)

- **`n>1` silently capped (P0)** — **FIXED**. `proxy.py:proxy_comfy_request` and `proxy_comfy_edit_request` now invoke `_generate_images_with_adaptive_batching`. Workflow `_meta.batch_size_node` declared on every image workflow. Per-(workflow,size) VRAM cost measured on first request and persisted to `cache/vram_estimates.json`. Probe (n=4, 512x512) returns 4 distinct PNGs in ~13s; 503 with `Retry-After: 30` if VRAM cannot fit even one image. Validation: `/tmp/honesty-fix-gen-n4-{0..3}.png`.
- **`response_format: "url"` silently downgraded (P1)** — **FIXED**. `_build_image_response_payload` writes content-addressed PNGs to `cache/images/<sha256>.png`, returns hostname-aware HMAC-signed URLs (`?expires=&sig=`). New `GET /v1/images/{id}/content` enforces bearer auth + signature + TTL; 401 (no auth), 403 (bad/expired sig), 404 (evicted), 200 returns the actual PNG. Eviction loop runs in lifespan, deletes entries older than 24h. Validation: `/tmp/honesty-fix-url-img.png` is a valid 512x512 PNG fetched via signed URL.
- **`/v1/models` admin-tier doc claim (P2)** — **FIXED**. Skill doc updated to "no auth tier system; any valid proxy key passes for every endpoint."
- **Vision routing misleading (P1)** — **FIXED**. Pre-flight scan in `proxy_request` (`has_vision_content` checks `image_url` / `input_image` blocks). Decisions: (a) kobold w/ mmproj loaded → no switch; (b) tabby active → no switch; (c) text-only kobold + `roles.vision` configured → ensure_target switch; (d) no path → 400 `vision_unsupported`. Every chat-completions response carries `X-Vision-Routed: true | false | not-needed`. Validation: data-URL image to `model: default` returned `x-vision-routed: true` and a correct visual description.
- **`/v1/voices` strict subset (P2)** — **FIXED**. Listing now derived from module-scope `VOICE_MAP` (single source of truth). 37 entries (was 18) with `language`, `gender`, `description`, `mapped_to` metadata. All `it_*`, `nl_*`, `pt_*`, `ar_*`, `hi_*` IDs present.
- **Catch-all routing flip (P2)** — **FIXED**. The silent tabby→kobold fallthrough is gone. Catch-all routes that target tabby return 503 with `Retry-After: 10` and `code: backend_unavailable` when tabby is unreachable. Completion paths additionally accept `allow_backend_fallback: true` in the body to opt into kobold fallback when applicable.
- **`/v1/models` missing `created` (P2)** — **FIXED**. Kobold entry now populates `created` from `os.path.getmtime` of the GGUF file (with `int(time.time())` fallback when stat fails). Validation: response shows `created: 1777824597` (Sun May 3 11:09:57 2026).
- **Skill doc (Fix 7)** — **FIXED**. `~/code/dotfiles/ai/skills/local/openai-vivid-fish/SKILL.md` updated: vision routing rewritten, admin-tier claim removed, `/v1/images/{id}/content` documented, `n` and `response_format=url` truthful, observability headers (`X-Image-Rounds`, `X-Image-Returned`, `X-Vision-Routed`, `X-Proxy-Warning`) listed.

Pyright: no new diagnostics introduced. Pre-existing errors (lines 41, 42 fastapi imports; 63, 70, 97, 103, 109 env-var int casts; 4761 uvicorn) untouched.

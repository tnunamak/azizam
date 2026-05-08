# openai-proxy

Self-hosted, OpenAI-compatible HTTP gateway that fronts a stack of local AI backends:

- **TabbyAPI** (Qwen3-VL EXL3) — chat / completions / vision
- **llama.cpp / koboldcpp** — GGUF chat/completions (mutually exclusive with TabbyAPI on the same GPU; swap via systemd)
- **Voxtral-4B** (vLLM) — text-to-speech
- **Parakeet TDT 0.6B** — speech-to-text
- **ComfyUI** — image generation, image edit, and video generation (LTX-2.3, Wan 2.2)

The proxy presents a single `/v1` surface that matches the OpenAI API closely enough that off-the-shelf clients (LibreChat, OpenWebUI, OpenClaw, the OpenAI SDKs, etc.) point at it without modification.

In production it's served at `https://openai.vivid.fish` via a Traefik front door.

## Endpoint surface

| Method + Path | Backend | Purpose |
|---|---|---|
| `GET  /health` | proxy | Liveness; public, no auth |
| `GET  /v1/models` | active LLM backend | List models; augments with currently loaded GGUF |
| `GET  /v1/voices`, `GET /v1/audio/voices` | proxy (static) | List TTS voices |
| `POST /v1/chat/completions` | tabby (default) or kobold (auto-routed) | Chat completion (vision-capable on tabby) |
| `POST /v1/completions` | tabby/kobold | Text completion |
| `POST /v1/embeddings` | tabby | Embeddings |
| `POST /v1/audio/speech` | Voxtral TTS | OpenAI-compatible TTS, voice-mapped |
| `POST /v1/audio/transcriptions` | Parakeet STT | Multipart audio → transcript |
| `POST /v1/images/generations` | ComfyUI | Text-to-image (default `flux2-klein-4b`) |
| `POST /v1/images/edits` | ComfyUI | Image edit; multipart with input image |
| `GET  /v1/images/{id}/content` | proxy cache | Fetch a cached image referenced by a `response_format: "url"` result. Auth-gated; URL is HMAC-signed with a TTL. |
| `POST /v1/videos` | ComfyUI | Submit video job (Sora-shaped, async) |
| `GET  /v1/videos/{id}` | ComfyUI | Poll job status (`queued` → `running` → `succeeded`/`failed`) |
| `GET  /v1/videos/{id}/content` | ComfyUI | Fetch rendered MP4 once `status=succeeded` |
| `* /<any other>` | tabby | Catch-all proxy to TabbyAPI (admin/model-load endpoints) |

Notable behaviors documented in detail under [`AUTH_GUIDE.md`](AUTH_GUIDE.md), [`MODEL_COMPATIBILITY.md`](MODEL_COMPATIBILITY.md), and [`OPENCLAW_SETUP.md`](OPENCLAW_SETUP.md):

- **Vision routing** — chat-completions requests carrying `image_url` / `input_image` content blocks auto-switch to the configured `roles.vision` backend; text-only backends with no vision role return HTTP 400 (`code: vision_unsupported`) rather than silently dropping image content. Every chat-completions response carries an `X-Vision-Routed` header for observability.
- **Honest `n` for images** — adaptive ComfyUI batching backs the request, returning all `n` images. If even one image won't fit in free VRAM, the proxy returns HTTP 503 with `Retry-After: 30` rather than silently truncating.
- **Signed image URLs** — `response_format: "url"` returns hostname-aware, HMAC-signed URLs into a content-addressed cache. URLs are bearer-auth-gated and expire (default 1 h), and the cache evicts older entries (default 24 h).
- **Async video generation** — Sora-2-shaped submit/poll/fetch flow with multiple model aliases (`ltx-2.3`, `ltx-2.3-detail`, `ltx-2.3-long`, `wan-2.2`, etc.). Generated MP4s and PNGs embed the full UI-format ComfyUI workflow in metadata, so dropping an output back into ComfyUI's frontend rebuilds the editable graph.

## Auth

All `/v1` traffic requires a bearer token. `/health` is public.

Proxy keys are stored as sha256 hashes in `proxy_api_keys.yml` (gitignored). Generate or rotate one with:

```bash
./venv/bin/python proxy.py generate-key <name>
```

The printed `opk_...` value is the bearer key clients send as `Authorization: Bearer opk_...`. The yaml only ever holds the hash.

Per-key downstream overrides are supported — any one proxy key can map to different upstream credentials per backend (TabbyAPI key, TTS auth header, etc.). See [`proxy_api_keys.example.yml`](proxy_api_keys.example.yml) for the structure.

See [`AUTH_GUIDE.md`](AUTH_GUIDE.md) for the agent-facing key generation and rotation workflow.

## Repository layout

```
proxy.py                       # Main FastAPI app: routing, auth, ComfyUI orchestration
status_endpoint.py             # /v1/status — backend health, GPU/disk/queue rollup, image/video capability matrix
firehose_metrics.py            # /v1/metrics firehose — recent renders, jobs, events
proxy_config.yml               # All backend wiring, video aliases, cache config, role mappings
proxy_api_keys.example.yml     # Hash-only key store template (commit)
proxy_api_keys.yml             # Real keys — gitignored
openai_model_aliases.generated.yml   # OpenAI-name → local-model rewrite table
requirements.txt               # Python deps (FastAPI, httpx, PyYAML, ...)
start.sh                       # Service starter (systemd ExecStart)
openai-proxy.service           # Reference systemd unit
test_e2e.sh                    # End-to-end smoke tests against a running proxy
workflows/                     # ComfyUI workflows (committed; canonical alongside proxy code)
  images/                      #   Flux + Flux2-Klein image gen / edit graphs
  video-*.json                 #   LTX-2.3 / Wan 2.2 video graphs (paired with .ui.json twins)
  video-*.ui.json              #   UI-format twins (regeneratable but expensive — committed for reproducibility)
scripts/
  check_workflow_drift.py      # Verifies every .ui.json's API-format extraction still matches the committed *.json
  convert_api_to_ui.py         # Round-trips API-format graph back into UI-format
status-page/                   # Next.js status dashboard (uses /v1/status + /v1/metrics)
inbox/                         # Operational notes / audits / proposals (kept as history)
```

### Image and video workflows

Both API-format (`*.json`) and UI-format (`*.ui.json`) workflows live in this repo and are committed. The UI twins are regeneratable from the API graph + a running ComfyUI instance via `scripts/convert_api_to_ui.py`, but regenerating them is expensive (requires Playwright + ComfyUI), so they're treated like a lockfile: tracked, with a drift detector to keep them in sync.

`scripts/check_workflow_drift.py` is the invariant: it walks every `.ui.json`, extracts the API-format graph, and compares against the committed `.json` sibling. Run it whenever a workflow changes.

## Setup

### 1. Python deps

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. Configure backends

Edit `proxy_config.yml`. The shipped config assumes:

- TabbyAPI on `127.0.0.1:5050`
- llama.cpp/koboldcpp on `127.0.0.1:5051`
- Voxtral TTS on `127.0.0.1:8001`
- Parakeet STT on `127.0.0.1:5092`
- ComfyUI on `127.0.0.1:7890`

Adjust hosts, ports, model paths, and role mappings as needed.

### 3. Create your first proxy key

```bash
cp proxy_api_keys.example.yml proxy_api_keys.yml
./venv/bin/python proxy.py generate-key first-client
```

That command rewrites `proxy_api_keys.yml` to add a hashed entry and prints the `opk_...` bearer token once. The token is never recoverable from disk.

### 4. Start the proxy

For development:

```bash
./start.sh
```

For production, install the included systemd unit. The reference unit at `openai-proxy.service` expects the proxy at `/home/tnunamak/applications/openai-proxy` — adjust `WorkingDirectory` and `ExecStart` for your install path:

```bash
cp openai-proxy.service ~/.config/systemd/user/openai-proxy.service
# Edit paths if your checkout is elsewhere
systemctl --user daemon-reload
systemctl --user enable --now openai-proxy.service
```

### 5. Smoke test

```bash
curl -s http://127.0.0.1:5000/health
curl -s http://127.0.0.1:5000/v1/models -H "Authorization: Bearer opk_..."
./test_e2e.sh
```

The status page (Next.js, in `status-page/`) is a separate static build and is served independently.

## Status page

A static Next.js dashboard lives under `status-page/` and renders backend health, disk/GPU rollup, recent renders, and queue depth from `/v1/status` and `/v1/metrics`. Build with:

```bash
cd status-page
npm install
npm run build  # outputs to ./out
```

Serve `status-page/out/` from any static host or behind the proxy.

## Operational notes

`inbox/` holds dated audits, proposals, and "final state" snapshots from incremental work on the proxy — these are running notes rather than canonical docs. Treat the README and the top-level `*.md` files as the authoritative reference; the inbox is operational history.

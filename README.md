# Vivid Fish AI Gateway

> Project directory and Python package are still named `openai-proxy`. The product, public discovery surfaces, and self-hoster docs are now branded **Vivid Fish AI Gateway** (or **AI Gateway** for short). Existing app configs that point at `openai.vivid.fish` keep working unchanged.

Self-hosted, OpenAI-compatible HTTP gateway and control plane. The repo ships
seed records and adapters for a local AI stack, but live service/provider
availability is resolved from the DB/UI registry:

- **TabbyAPI** (Qwen3-VL EXL3) — chat / completions / vision
- **llama.cpp / koboldcpp** — GGUF chat/completions (mutually exclusive with TabbyAPI on the same GPU; swap via systemd)
- **Voxtral-4B** (vLLM) — text-to-speech
- **Parakeet TDT 0.6B** — speech-to-text
- **ComfyUI** — image generation, image edit, and video generation (LTX-2.3, Wan 2.2)

The gateway presents canonical protocol aliases for OpenAI, Anthropic, and Ollama-compatible clients:

- `/openai/v1` — OpenAI-compatible surface
- `/anthropic/v1` — Anthropic Messages subset
- `/ollama/api` — Ollama-compatible subset

For existing tools, the convenience routes `/v1` and `/api` remain mounted and map to the OpenAI and Ollama-compatible surfaces. Off-the-shelf apps (LibreChat, Open WebUI, OpenClaw, Continue, Cline, the OpenAI SDKs, etc.) can keep using those convenience routes without modification.

The canonical public host is **`https://ai.vivid.fish`**. The original **`https://openai.vivid.fish`** host is preserved as a compatibility alias — every route, discovery document, and signed URL resolves identically on both hosts, so existing apps do not need to be reconfigured.

## Agent discovery

Agents and apps can start without local repo context by fetching the public discovery surface (no auth required):

- `GET /` — browser account portal
- `GET /setup` — web-first owner claim and setup handoff
- `POST /setup/claim` — accept an existing `oak_...` admin key and issue a
  signed browser admin session
- `POST /setup/first-app` — signed-admin setup action that creates or reuses
  the operator's first App and shows a one-time `opk_...` runtime key
- `GET /health`
- `GET /login` — browser OIDC login entry point when configured
- `GET /me` — legacy browser alias; redirects to `/`
- `GET /me/api/session` — signed-OIDC account view for Apps and non-secret
  API-key records owned by the current user
- `POST /me/api/apps` — signed-OIDC self-service App creation; the App
  inherits the user's default permissions
- `DELETE /me/api/apps/{app_id}` — delete a signed-in user's App and revoke
  its active keys
- `POST /me/api/apps/{app_id}/keys` — create an API key for an App the
  signed-in user owns
- `GET /me/api/credentials/{credential_id}/access` — inspect capabilities for
  a key owned by the signed-in user without pasting the secret
- `DELETE /me/api/credentials/{credential_id}` — delete a key the signed-in
  user owns
- `GET /.well-known/ai-gateway` — canonical gateway metadata document
- `GET /.well-known/openai-proxy` — compatibility alias (identical body)
- `GET /llms.txt`
- `GET /.well-known/skills`
- `GET /.well-known/skills/openai-proxy-gateway/SKILL.md`
- `GET /gateway/v1/extensions`
- `GET /gateway/v1/models/{model}/capabilities`
- `GET /gateway/v1/providers` — provider-native lanes visible to the current
  App profile
- `GET /gateway/v1/providers/{provider}` — metadata for one provider-native
  lane
- `* /gateway/v1/providers/{provider}/{path...}` — provider-native namespace
  for policy-gated native passthrough; requires an App profile that allows the
  `provider-native` protocol and native passthrough for the target provider and
  operation.

The discovery document advertises:

- `service.product = "Vivid Fish AI Gateway"`
- `service.canonical_host = "https://ai.vivid.fish"`
- `service.compatibility_aliases` includes `https://openai.vivid.fish`
- `protocols[].canonical_base_path` for `/openai/v1`, `/anthropic/v1`, and
  `/ollama/api`
- `protocols[].convenience_base_path` where `/v1` or `/api` is also accepted

The bundled skill is also committed at [`skills/openai-proxy-gateway/SKILL.md`](skills/openai-proxy-gateway/SKILL.md). The skill ID is preserved as `openai-proxy-gateway` so existing skill catalogs and tests keep resolving it. The skill itself tells agents to discover capabilities before assuming a backend, keep bearer tokens out of chat, use `/openai/v1` for new OpenAI-compatible setup, and inspect `/gateway/v1/receipts` when workflow controls are rejected or ignored.

## Endpoint surface

| Method + Path | Resolved lane | Purpose |
|---|---|---|
| `GET  /` | proxy | Browser account portal |
| `GET  /setup` | proxy | First-run setup and owner/admin browser-session handoff |
| `POST /setup/claim` | proxy | Claim a browser admin session with an existing `oak_...` admin key |
| `POST /setup/first-app` | proxy | Signed-admin first App/key creation from setup; raw key is shown once |
| `GET  /login` | proxy + OIDC | Browser sign-in entry point; redirects back to `/` or `/admin` |
| `GET  /me` | proxy | Legacy browser alias; redirects to `/` |
| `GET  /admin` | proxy | Admin dashboard; browser sessions redirect through `/login` |
| `GET  /status` | proxy | Operator status dashboard; requires admin OIDC session or `oak_...` admin key |
| `GET  /health` | proxy | Liveness; public, no auth |
| `GET  /v1/models` | LiteLLM + gateway catalog | List LiteLLM LLM model groups plus gateway-native workflow model IDs |
| `POST /anthropic/v1/messages` | chat pipeline | Anthropic Messages-compatible request/response and SSE streaming; `/v1/messages` remains a convenience route |
| `GET  /ollama/api/tags` | proxy catalog | Ollama-compatible model list; `/api/tags` remains a convenience route |
| `POST /ollama/api/chat`, `POST /ollama/api/generate` | chat pipeline | Ollama-compatible chat/generate with NDJSON streaming; `/api/chat` and `/api/generate` remain convenience routes |
| `POST /ollama/api/embed`, `POST /ollama/api/embeddings` | selected embeddings provider | Ollama-compatible current and legacy embeddings; `/api/embed`, `/api/embeddings` remain convenience routes |
| `POST /ollama/api/show`, `GET /ollama/api/ps`, `GET /ollama/api/version` | proxy catalog/status | Ollama-compatible read-only model metadata/status; `/api/show`, `/api/ps`, `/api/version` remain convenience routes |
| `GET  /v1/voices`, `GET /v1/audio/voices` | proxy (static) | List TTS voices |
| `POST /v1/chat/completions` | LiteLLM execution substrate | Chat completion; gateway activates local vision targets before execution when needed |
| `POST /v1/completions` | LiteLLM execution substrate | Text completion |
| `POST /v1/embeddings` | LiteLLM execution substrate | OpenAI-compatible embeddings; no GPU budget claim |
| `POST /v1/audio/speech` | Profile-selected TTS provider | OpenAI-compatible TTS, voice-mapped; `response_format` supports `wav` and `mp3` |
| `POST /v1/audio/transcriptions` | Profile-selected STT provider | Multipart audio → transcript |
| `POST /v1/audio/translations` | Profile-selected STT provider | OpenAI-compatible route; returns `unsupported_audio_translation` unless the selected STT provider declares translation support |
| `POST /v1/images/generations` | Profile-selected image provider/workflow | Text-to-image (default seed workflow `flux2-klein-4b`) |
| `POST /v1/images/edits` | Profile-selected image provider/workflow | Image edit; multipart with input image and optional top-level OpenAI `mask` |
| `GET  /v1/images/{id}/content` | proxy cache | Fetch a cached image referenced by a `response_format: "url"` result. Signed capability URL; no bearer required until expiry. |
| `POST /gateway/v1/media/jobs` | proxy + selected media provider/workflow | Durable media-job submit. For images, send `{"intent":"image.generate","request":{...}}`. |
| `GET  /gateway/v1/media/jobs/{id}/content` | proxy cache | Fetch the OpenAI-shaped image payload for a completed durable image job. |
| `POST /v1/videos` | Profile-selected video provider/workflow | Submit video job (Sora-shaped, async) |
| `GET  /v1/videos/{id}` | selected video provider/workflow | Poll job status (`queued` → `running` → `succeeded`/`failed`) |
| `GET  /v1/videos/{id}/content` | selected video provider/workflow | Fetch rendered MP4 once `status=succeeded` |
| `GET  /gateway/v1/extensions` | proxy | Live extension namespace and route discovery |
| `GET  /gateway/v1/models/{model}/capabilities` | proxy | Live model-specific capability and workflow-control discovery |
| `GET  /gateway/v1/providers` | proxy | List provider-native lanes projected through the caller's App profile policy. |
| `GET  /gateway/v1/providers/{provider}` | proxy | Metadata for one provider-native lane. |
| `* /gateway/v1/providers/{provider}/{path...}` | proxy + selected provider | Policy-gated provider-native passthrough for explicit backend operations. App policy must allow `provider-native`, native passthrough, the provider, and the operation. |
| `GET  /gateway/v1/assets/{asset_id}` | proxy | Gateway-native metadata for generated image/audio/video asset handles. |
| `GET  /gateway/v1/assets/{asset_id}/content` | proxy | Signed generated-asset content URL; no bearer required until expiry. |
| `* /<any other>` | proxy | Unknown routes return 404 instead of falling through to a backend. There is no legacy root Tabby shim or root catch-all backend passthrough. |

Notable behaviors documented in detail under [`AUTH_GUIDE.md`](AUTH_GUIDE.md), [`MODEL_COMPATIBILITY.md`](MODEL_COMPATIBILITY.md), and [`OPENCLAW_SETUP.md`](OPENCLAW_SETUP.md):

- **Vision routing** — chat-completions requests carrying `image_url` / `input_image` content blocks activate the configured `roles.vision` target before LiteLLM execution; text-only backends with no vision role return HTTP 400 (`code: vision_unsupported`) rather than silently dropping image content.
- **Honest `n` for images** — adaptive ComfyUI batching backs the request, returning all `n` images. If even one image won't fit in free VRAM, the proxy returns HTTP 503 with `Retry-After: 30` rather than silently truncating.
- **Durable image jobs** — long/batch image work can use `POST /gateway/v1/media/jobs` with `intent: "image.generate"` and an OpenAI image request under `request`. The proxy queues the work server-side, retries transient VRAM/ComfyUI/tile-composition failures, persists redacted job state, and returns reusable asset handles via polling/content endpoints. The private request payload is kept on disk only while the job is pending/running so the proxy can recover from client disconnects and restarts.
- **Generated asset handles** — generated image/audio/video outputs can be inspected at `GET /gateway/v1/assets/{asset_id}` with the minting API key and fetched through signed bearerless content URLs until expiry. Asset handles and input reuse remain scoped to the API key that minted them.
- **Signed image URLs** — `response_format: "url"` returns hostname-aware, HMAC-signed URLs into a content-addressed cache. URLs are bearerless until expiry (default 1 h), and the cache evicts older entries (default 24 h).
- **Async video generation** — Sora-2-shaped submit/poll/fetch flow with multiple model aliases (`ltx-2.3`, `ltx-2.3-detail`, `ltx-2.3-long`, `wan-2.2`, etc.). Generated MP4s and PNGs embed the full UI-format ComfyUI workflow in metadata, so dropping an output back into ComfyUI's frontend rebuilds the editable graph.

## Control-plane nouns

The gateway uses a small, stable set of nouns across docs, code, and receipts. Two planes — keep them straight:

**Admin plane** (who can operate this gateway):

- **User** — a human principal, typically authenticated via OIDC (e.g. the self-hoster, a family member, a friend).
- **ServiceAccount** — a non-human principal that owns one or more apps and the credentials they use.
- **Credential** — a bearer-token secret (an `opk_...` App API key, an `oak_...` admin API key, an OIDC session, or a downstream provider key); always stored as a hash where possible.
- **AdminRole** — a named admin permission bundle. The current control plane treats `owner`/`admin`/`superadmin` as admin-capable; finer-grained endpoint permissions are a future extension.
- **AuditEvent** — an immutable record of an admin-plane action (key minted, profile changed, role assigned, etc.).

**Runtime plane** (what gets executed for an end user):

- **App** — a registered consumer of the gateway (e.g. LibreChat, Open WebUI, Continue, a personal chat client). Apps are what mint and present API keys at request time.
- **Profile** — the policy bundle attached to an App: allowed protocols, models, workflows, backends, fallback rules, native passthrough scopes, and receipt visibility tier.
- **Credential** (runtime) — the App API key the App presents, plus any per-key downstream-credential overrides.
- **Capability** — a runtime fact about what a backend/model can actually do right now (intent, modality, context window, streaming, health, resource estimate).
- **Provider** — a configured backend instance (TabbyAPI, llama.cpp, ComfyUI, TTS, STT, hosted-LLM fallback).
- **Workflow** — a typed manifest that maps portable inputs (prompt, denoise, seed) to a backend-specific binding.
- **Job** — a durable, asynchronous execution record for media work (image, video, long audio).
- **Receipt** — a body-free record of every request: who, what intent, what policy decision, what workflow/manifest, what was accepted/rejected/ignored.
- **UsageRecord** — per-App tokens/seconds/bytes/GPU-time, used for status and metrics.

The product invariant is:

> **A request without a resolved App + Profile + policy decision is never executed. Every executed request produces exactly one Receipt.**

The legacy term **"client"** is preserved only in compatibility contexts (OpenAI client SDKs, "client receipts" audience tier, protocol-level client/server wording). In product docs the user-facing entity is an **App**.

## Auth

All `/v1`, `/api`, and `/gateway/v1` traffic requires a bearer token. `/health`, `/`, `/setup`, `/me`, `/me/api/session`, and the `/.well-known/*` discovery surfaces are public. `/me` redirects to `/`; the account portal and setup page do not expose private data by themselves. The browser uses either the signed admin/OIDC session cookie or a pasted `opk_...` key to check effective access. Pasted keys are kept in browser session storage, not long-lived local storage.

The normal first-run path is web-first:

1. Start the service. The gateway defaults to `data/admin.db` and creates a
   persistent `data/admin_session_key` if no explicit browser-session secret
   is configured.
2. Open `/setup`.
3. Claim or create the first local owner — sign in with OIDC if already
   configured, or create the local owner account in the browser.
4. The app seeds default Profiles, identity/Profile mappings, and the
   signed-in user's first App, then mints a one-time `opk_...` runtime key.
5. Configure your OpenAI-compatible client against
   `https://ai.vivid.fish/openai/v1` (or `http://127.0.0.1:5000/openai/v1`
   locally).
6. Manage the service/provider registry, OIDC, users, Apps, Profiles, keys,
   admission limits, and service availability from `/admin`. Restart is only
   required for irreducible deployment bootstrap such as listener/TLS settings
   and secret/env changes.

App keys (`opk_...`) are for runtime clients and SDKs. Admin keys (`oak_...`) are for the control plane and can also bootstrap a browser admin session through `/setup/claim`. See [`docs/FIRST_RUN_SETUP.md`](docs/FIRST_RUN_SETUP.md) for the full first-run contract.

App API keys created from `/setup`, `/`, or `/admin` are stored as sha256
hashes in `admin.db`. The bearer key apps present is
`Authorization: Bearer opk_...`; the raw value is shown once and cannot be
recovered from disk.

Service/provider records live in the DB/UI service registry. `proxy_config.yml`
is seed/provisioning/bootstrap input, not the normal live operator surface.
Env and Docker secrets remain for irreducible bootstrap and provider secret
references. Provisioned services are visible and testable in `/admin`, but
read-only unless detached or copied to UI-managed ownership. Per-App
downstream credential overrides are not part of the current DB runtime path;
use Profiles to decide what an App may access.

Admin/control-plane credentials are separate from App keys. The `oak_...` admin key administers `/admin/api/*`; it is not an OpenAI-compatible `/v1` App key. Browser login starts at `/login` when `admin.oidc` is configured, and the admin dashboard is served at `/admin`. Admins are Users with an admin-capable role, so signed-in admins also see an Admin link from the account portal. The dashboard can create Apps, Profiles, and API keys, revoke credentials, and simulate policy decisions before handing someone access.

OIDC can grant admin from configured IdP groups or roles without hardcoding a
deployment's policy into the proxy. Set `admin.oidc.admin_groups` /
`admin.oidc.admin_roles`, or point `admin_groups_env` / `admin_roles_env` at
comma-separated environment variables. Group matching accepts common Keycloak
forms such as `/Trusted`, `trusted`, `trusted-users`, and `trusted users`.
Claim-based grants create `admin` users only; `owner` remains a bootstrap or
explicit manual grant.

### Give a family member a key

The shortest "share my gateway with another human" flow:

1. **OIDC User.** The family member signs in once so the admin store has their **User** row. They do not need a global admin role.
2. **Personal App.** The account portal lets them create one or more self-service **Personal Apps**. An App is the access boundary for a tool, workflow, or script; it is not the person.
3. **Inherited Profile.** The App inherits the user's effective Profile from
   dashboard-managed identity/Profile mappings. `admin.user_defaults` can seed
   those mappings on first startup, but ongoing changes belong in `/admin`.
   Admins can still attach an explicit App/Profile override when a narrower or
   broader boundary is needed.
4. **App API key.** The user creates their own `opk_...` key from the account portal and can delete it later. They can also inspect any key they own, or paste an external key locally in the browser, to confirm the base URL, SDK settings, and effective capabilities before configuring their chat app.

Rotation is per-App, not per-human: delete the old Credential and create a new key while preserving the App's Profile and downstream mapping. Dashboard-minted keys inherit the App's current Profile unless an explicit per-key override is requested.

See [`AUTH_GUIDE.md`](AUTH_GUIDE.md) for the full key-generation and rotation workflow.

## Repository layout

```
proxy.py                       # Main FastAPI app: routing, auth, ComfyUI orchestration
status_endpoint.py             # /v1/status — admin-only backend health, GPU/disk/queue rollup
firehose_metrics.py            # /v1/metrics firehose — recent renders, jobs, events
proxy_config.yml               # Bootstrap wiring, service/provider seed/provisioning, optional one-time defaults
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
  doctor.py                    # Operator/agent smoke checks for health, auth, manifests, and workflow bindings
  check_workflow_drift.py      # Verifies every .ui.json's API-format extraction still matches the committed *.json
  convert_api_to_ui.py         # Round-trips API-format graph back into UI-format
status-page/                   # Legacy Next.js status dashboard source; not served by the proxy
inbox/                         # Operational notes / audits / proposals (kept as history)
```

### Image and video workflows

Both API-format (`*.json`) and UI-format (`*.ui.json`) workflows live in this repo and are committed. The UI twins are regeneratable from the API graph + a running ComfyUI instance via `scripts/convert_api_to_ui.py`, but regenerating them is expensive (requires Playwright + ComfyUI), so they're treated like a lockfile: tracked, with a drift detector to keep them in sync.

`scripts/check_workflow_drift.py` is the invariant: it walks every `.ui.json`, extracts the API-format graph, and compares against the committed `.json` sibling. Run it whenever a workflow changes.

## Setup

The full first-run story lives in [`docs/FIRST_RUN_SETUP.md`](docs/FIRST_RUN_SETUP.md). The short version:

### 1. Python deps

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### 2. Bootstrap config (minimal)

A fresh deployment can boot with near-zero local config. If no
`admin.session_signing_key` or `AI_GATEWAY_ADMIN_SESSION_KEY` is configured,
the gateway creates `data/admin_session_key` and reuses it for browser admin
sessions. Set an explicit secret only when your deployment already has a
secret-management story.

If no `admin.db_path` is configured, the gateway creates and uses
`data/admin.db` next to `proxy.py`. That file is durable host-local state and
holds every runtime decision the dashboard manages (owner, Apps, Profiles,
keys, identity mappings, admission caps). Back it up; mount its directory on a
volume in container deployments.

Anything else in `proxy_config.yml` is optional seed/provisioning/bootstrap
detail:

- Listener host/port and TLS terminator.
- Service/provider seed records and downstream provider secret references.
- Optional bootstrap `admin.oidc` if you want IdP login before creating a
  local owner. Otherwise create the local owner first and configure OIDC from
  `/admin`.

Bundled seed service records target these local development defaults:

- TabbyAPI on `127.0.0.1:5050`
- llama.cpp/koboldcpp on `127.0.0.1:5051`
- Voxtral TTS on `127.0.0.1:8001`
- Parakeet STT on `127.0.0.1:5092`
- ComfyUI on `127.0.0.1:7890`

After boot, provider/service availability lives in the DB/UI registry with an
explicit ownership mode: `ui`, `provisioned`, `builtin`, or `discovered`.
Provisioned services are visible and testable but read-only unless detached or
copied to UI-managed ownership. Runtime access policy (Profiles, Apps,
role/model bindings, admission caps, identity mappings, service availability)
lives in `admin.db` and is edited from the dashboard. Provisioning / as-code
config export is deferred; default Profiles are seeded into `admin.db` on
first startup, not YAML-managed. See
[`docs/CONFIG_SOURCE_OF_TRUTH.md`](docs/CONFIG_SOURCE_OF_TRUTH.md) for the
full policy.

### 3. Start the proxy

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

### 4. Claim the first owner in the browser

Open `/setup`. There are two ways to claim or create the first owner, no host
shell required after the service is running:

- **OIDC configured** — sign in with the IdP account that should own the
  deployment. The first allowed login is promoted to `owner` when no owner
  exists yet.
- **No OIDC, fresh machine** — create the local owner account directly on
  `/setup`, then configure OIDC later from `/admin` if you want IdP login.

Once the browser holds an admin session, `/setup` mints the first one-time
`opk_...` runtime key for your client. Everything else — Profiles, identity
mappings, shared Apps, role/model bindings, admission policy, and the
service/provider registry — is dashboard work at `/admin` and does not need a
restart for UI-managed records.

### 5. Smoke test

```bash
curl -s http://127.0.0.1:5000/health
curl -s http://127.0.0.1:5000/v1/models -H "Authorization: Bearer opk_..."
./test_e2e.sh
```

The operator status page is served by the proxy at `/status`. The product root
`/` serves the account portal; `/me` is a legacy browser redirect to `/`.

### Recovery: admin key generation (not the normal path)

The normal way to administer the gateway is `/setup` and `/admin` in a
browser. The CLI below is **only** for recovery — bootstrapping an `oak_...`
key for headless automation, or regaining control-plane access when
browser/local-owner/OIDC admin access is broken.

```bash
# Recovery / headless admin key. Initializes the admin store if needed.
./venv/bin/python proxy.py generate-admin-key owner-cli
```

The command prints one `oak_...` secret value once and stores only a hash. It
is not recoverable from disk. Treat it as the back door, not the front door.
Paste it into `/setup` to claim a browser admin session, then do everything
else from the dashboard.

## Status page

The server-rendered `/status` dashboard shows backend health, disk/GPU rollup,
recent renders, and queue depth from the same cached status data used by
`/v1/status`. Status access requires an admin OIDC session or `oak_...` admin
key, including from loopback. Client
liveness probes should use the public `/health` route, and clients should use
normal authenticated compatibility APIs such as `/v1/models` for runtime
capability checks.

## Metrics persistence

Time-series metrics are written into `data/metrics.db` (SQLite, WAL). The DB is host-local runtime state and is gitignored; the schema lives entirely in `metrics_store.py`.

Layout:

| Table | Retention | Sample rate |
|---|---|---|
| `samples` | 7 days | every 10 s (fast tier) / 60 s (slow tier) |
| `samples_1min` | 30 days | rolled up at the top of each minute |
| `samples_1hour` | 365 days | rolled up at the top of each hour |

Each row is `(ts, metric, tags_json, value)` plus rollup columns `(avg_value, min_value, max_value, count)` on the aggregated tables. Tag dicts (e.g. `{"gpu":0}`, `{"key":"alpha","path":"/v1/chat/completions","status":200}`) serialize deterministically with sorted keys.

Sampling cadence is driven by a single `asyncio` task in `metrics_sampler.py`:

- **Fast tier (10 s):** GPU util / VRAM / temp / clocks / fan, CPU/NVMe/DIMM temps, load avg, RAM totals, ComfyUI queue depth, llama-server slots active count.
- **Slow tier (60 s):** disk avail+pct per mount, per-service RSS, per-GPU per-service VRAM attribution, Xid-109 / total Xid 60-min and 24-h counts.
- **Per-request samples:** the auth middleware writes one `request` sample per non-skip path with `{key, path, status}` tags so per-key request-rate sparklines survive proxy restarts.

A WAL checkpoint is performed on shutdown. On startup the file is `PRAGMA integrity_check`-ed; a corrupt DB is renamed to `metrics.db.broken-{ts}` and a fresh one is created.

The `/v1/status` payload exposes a `metrics` block with windowed aggregates (5 min / 1 h / 24 h) and 24-hour series suitable for sparklines, and `/status` renders a "Trends" section using inline-SVG sparklines (no JavaScript).

## Operational notes

`inbox/` holds dated audits, proposals, and "final state" snapshots from incremental work on the proxy — these are running notes rather than canonical docs. Treat the README and the top-level `*.md` files as the authoritative reference; the inbox is operational history.

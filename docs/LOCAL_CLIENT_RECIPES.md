# Local Client Recipes

Date: 2026-05-15
Status: operational, scoped to the first real local instance.

This document is the practical client-side companion to `AUTH_GUIDE.md`,
`docs/API_TARGETS.md`, and `docs/CORE_CONTRACTS.md`. It tells someone setting
up a chat UI, coding agent, or SDK what URL to point at, what protocol surface
is actually live today, and which features will *not* work yet.

All examples assume:

```text
Local base host:   http://127.0.0.1:5000
Public base host:  https://ai.vivid.fish         # optional, when exposed
Client API key:    opk_...                       # never commit, never log
```

Generate a key once per agent or app, as documented in `AUTH_GUIDE.md`:

```bash
./venv/bin/python proxy.py generate-key <agent-or-app-name>
```

Then export it in the shell you run smoke commands from:

```bash
export OPENAI_PROXY_API_KEY='opk_...'
```

> The `$OPENAI_PROXY_API_KEY` placeholder is used throughout. Do not paste
> real `opk_...` values into config files, prompts, or commit messages.

## Live Surfaces Today

The proxy currently exposes (live, mounted in `proxy.py`):

| Path | Protocol | Notes |
|---|---|---|
| `GET /health` | — | Public, no auth. |
| `POST /openai/v1/chat/completions` | OpenAI Chat | Streaming + non-streaming; `/v1/chat/completions` remains a convenience route. |
| `POST /openai/v1/completions` | OpenAI Completions | Legacy, supported. |
| `POST /openai/v1/embeddings` | OpenAI Embeddings | CPU `nomic-embed-text-v1.5` backend; no GPU budget claim. |
| `GET /openai/v1/models` | OpenAI Models | Aliases + backend models. |
| `POST /openai/v1/responses` | OpenAI Responses | Translator over chat completions; durable store. |
| `GET /openai/v1/responses/{id}` | OpenAI Responses | Read back. |
| `DELETE /openai/v1/responses/{id}` | OpenAI Responses | |
| `POST /openai/v1/images/generations` | OpenAI Images | Synchronous image generation; emits observational gateway receipts. |
| `POST /openai/v1/images/edits` | OpenAI Images | Emits observational gateway receipts. |
| `GET /openai/v1/images/{id}/content` | OpenAI Images | Asset retrieval. |
| `GET /v1/assets/{asset_id}/content` | Native | Signed generated-asset retrieval. |
| `GET /gateway/v1/assets/{asset_id}` | Gateway native | Generated-asset metadata and fresh signed content URL. |
| `GET /gateway/v1/assets/{asset_id}/content` | Gateway native | Signed generated-asset retrieval through the native surface. |
| `POST /gateway/v1/media/jobs` | Gateway media jobs | Durable async image generation when `intent` is `image.generate` and `request` carries the OpenAI image body. |
| `POST /openai/v1/audio/speech` | OpenAI Audio | TTS. |
| `POST /openai/v1/audio/transcriptions` | OpenAI Audio | STT. |
| `GET /openai/v1/voices`, `GET /openai/v1/audio/voices` | OpenAI Audio | Voice list. |
| `POST /openai/v1/audio/generate`, `GET /openai/v1/audio/models` | Native | Music/SFX/voice models. |
| `POST /openai/v1/videos`, `GET /openai/v1/videos/{id}`, `GET /openai/v1/videos/{id}/content` | OpenAI-shaped video | |
| `GET /v1/status` | Admin | Operational status; requires admin auth. |
| `POST /anthropic/v1/messages` | Anthropic Messages | Translator over chat completions with SSE streaming; `/v1/messages` remains a legacy convenience route. |
| `GET /ollama/api/tags` | Ollama-compatible | Model list from configured catalog; `/api/tags` remains a convenience route. |
| `POST /ollama/api/chat`, `POST /ollama/api/generate` | Ollama-compatible | Translators over chat completions with NDJSON streaming; `/api/*` remains a convenience route. |
| `POST /ollama/api/embed`, `POST /ollama/api/embeddings` | Ollama-compatible | Current and legacy embedding routes over `/v1/embeddings`; `/api/*` remains a convenience route. |
| `POST /ollama/api/show`, `GET /ollama/api/ps`, `GET /ollama/api/version` | Ollama-compatible | Read-only model metadata/status projected from the gateway catalog/runtime. |

Gateway-native discovery, receipts, and media-job state (`gateway/api.py`) are
mounted into the live app:

```text
GET /gateway/v1/workflows
GET /gateway/v1/workflows/{id}
GET /gateway/v1/capabilities
GET /gateway/v1/extensions
GET /gateway/v1/models/{model}/capabilities
GET /gateway/v1/providers
GET /gateway/v1/providers/{provider}
GET /gateway/v1/receipts
GET /gateway/v1/receipts/{request_id}
POST /gateway/v1/media/jobs
GET /gateway/v1/media/jobs
GET /gateway/v1/media/jobs/{job_id}
GET /gateway/v1/media/jobs/{job_id}/content
GET /gateway/v1/media/jobs/{job_id}/events
POST /gateway/v1/media/jobs/{job_id}/events
POST /gateway/v1/media/jobs/{job_id}/cancel
```

Capability and receipt providers are wired conditionally. These endpoints may
return empty catalogs when no matching data exists, but the router itself is
live and authenticated.

## Agent Testing Path

Use the discovery surfaces before guessing workflow controls:

```bash
curl -s http://127.0.0.1:5000/gateway/v1/extensions \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"

curl -s http://127.0.0.1:5000/gateway/v1/models/flux2-klein-4b-edit/capabilities \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"

./venv/bin/python scripts/doctor.py \
  --base-url http://127.0.0.1:5000 \
  --comfy-url http://127.0.0.1:7890 \
  --format text
```

`scripts/doctor.py` checks public health, public discovery metadata, optional
ComfyUI health, manifest registry shape, and workflow bindings. Auth
diagnostics are limited to redacted environment-variable presence; do not
inspect or print `proxy_api_keys.yml`.

## Workflow Control Examples

Workflow knobs live under `extensions.com.vividfish.workflow`; standard API
fields such as `model`, `prompt`, `size`, `duration`, `seconds`, and
image-edit `mask` stay top-level. Route-native `seed` stays top-level on
audio/video; image workflow seeds are manifest-backed extensions.

Image generation seamless tile, recommended Flux2 offset+inpaint path:

```bash
curl -s http://127.0.0.1:5000/v1/images/generations \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "flux2-klein-4b-seamless-tile",
    "prompt": "hand-painted underwater basalt floor texture, game asset, tileable",
    "size": "1024x1024",
    "n": 1,
    "response_format": "b64_json",
    "extensions": {
      "com.vividfish.workflow": {
        "steps": 5,
        "guidance": 3.5,
        "seed": 4242,
        "seam_mask_width": 96,
        "seam_mask_feather": 32,
        "seam_denoise": 0.58,
        "seam_steps": 8
      }
    }
  }'
```

This is a gateway-composed route: Flux2 generation, 50%/50% circular offset,
center seam-cross mask, Flux2 inpaint repair, then offset back. Response
headers include `X-Image-Seamless-Tile-Strategy: offset-inpaint`.

Direct SDXL postprocess tile workflow:

```bash
curl -s http://127.0.0.1:5000/v1/images/generations \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "sdxl-seamless-tile",
    "prompt": "hand-painted underwater basalt floor texture, game asset, tileable",
    "size": "1024x1024",
    "n": 1,
    "response_format": "b64_json",
    "extensions": {
      "com.vividfish.workflow": {
        "steps": 28,
        "guidance": 7.0,
        "seed": 4242,
        "negative_prompt": "text, logo, watermark, frame, border, visible seam",
        "sampler": "dpmpp_2m",
        "scheduler": "karras",
        "seam_blending": 0.4
      }
    }
  }'
```

`seam_blending` is bounded to `0..0.5` by the installed WAS/img2texture
implementation. Values above `0.5` are rejected by the proxy instead of being
submitted to ComfyUI and timing out. This direct SDXL workflow is postprocess
only; it is not the full circular SDXL stack.

Every image result includes gateway-native reuse metadata in addition to the
OpenAI-compatible field the client asked for:

```json
{
  "asset_id": "img_<sha256>",
  "asset_url": "http://127.0.0.1:5000/v1/assets/img_<sha256>/content?...",
  "media_type": "image/png"
}
```

`asset_url` is signed and auth-gated like `/v1/images/{id}/content`; fetch it
with the same bearer token. `asset_id` can be passed back to gateway-aware
routes that explicitly accept an image input reference. Asset handles are scoped
to the API key that minted them, so they are convenient local references rather
than global public content hashes.

Gateway-aware clients can refresh metadata and a new signed URL with:

```bash
curl "http://127.0.0.1:5000/gateway/v1/assets/$ASSET_ID" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Durable image generation job, preferred for unattended batches or clients that
cannot safely keep a long HTTP request open:

```bash
JOB_JSON=$(curl -s http://127.0.0.1:5000/gateway/v1/media/jobs \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: tile-rock-4242" \
  -d '{
    "intent": "image.generate",
    "request": {
      "model": "flux2-klein-4b-seamless-tile",
      "prompt": "hand-painted underwater basalt floor texture, game asset, tileable",
      "size": "1024x1024",
      "n": 1,
      "extensions": {
        "com.vividfish.workflow": {
          "seed": 4242,
          "seam_mask_width": 96,
          "seam_denoise": 0.58
        }
      }
    }
  }')
JOB_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<<"$JOB_JSON")
curl -s "http://127.0.0.1:5000/gateway/v1/media/jobs/$JOB_ID" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
curl -s "http://127.0.0.1:5000/gateway/v1/media/jobs/$JOB_ID/content" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

The job executor forces `response_format: "url"` so durable records point at
signed, auth-gated cached assets instead of storing base64 response bodies.
Seeded image requests are idempotent by request hash; `Idempotency-Key` is the
stronger option for unseeded prompts and client retries. The media-job record
stores only redacted metadata; the private request payload lives in
`data/image_media_jobs/` while the job is pending/running and is removed after
success.

Image edit denoise:

```bash
curl -s http://127.0.0.1:5000/v1/images/edits \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F model=flux2-klein-4b-edit \
  -F prompt='remove the background clutter' \
  -F image=@input.png \
  -F 'extensions={"com.vividfish.workflow":{"denoise":0.42,"steps":24,"guidance":3.5}}'
```

Image edit mask:

```bash
curl -s http://127.0.0.1:5000/v1/images/edits \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F model=flux2-klein-4b-edit \
  -F prompt='replace only the masked area with coral texture' \
  -F image=@input.png \
  -F mask=@mask.png
```

`mask` is the standard OpenAI multipart field, not a workflow extension. Alpha
masks use OpenAI polarity: transparent edits, opaque preserves. Grayscale masks
use white to edit and black to preserve. The current Flux2 edit workflow has no
native inpaint node, so the proxy preserves unmasked pixels with a documented
post-composite fallback unless a native mask-bound workflow is selected.

ACE-Step music generation:

```bash
curl -s http://127.0.0.1:5000/v1/audio/generate \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ace-step-1.5-xl-turbo",
    "tags": "dream pop, warm piano, soft drums",
    "lyrics": "first line of the song\nsecond line of the song",
    "duration": 20,
    "seed": 1234
  }' \
  -o /tmp/music.wav
```

For ACE-Step workflows, `prompt` is still accepted and maps to the model's
`tags` input. Use top-level `tags` when an agent wants to be explicit about the
music-description field, and top-level `lyrics` for sung lyrics. If `tags` and
`prompt` are both present on ACE-Step, `tags` wins.

TangoFlux audio generation:

```bash
curl -s http://127.0.0.1:5000/v1/audio/generate \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tangoflux",
    "prompt": "short analog synth riser",
    "duration": 6,
    "seed": 1234,
    "extensions": {
      "com.vividfish.workflow": {
        "steps": 30,
        "guidance": 4.5,
        "batch_size": 1
      }
    }
  }'
```

LTX video generation:

```bash
curl -s http://127.0.0.1:5000/v1/videos \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ltx-2.3",
    "prompt": "slow dolly shot across a workbench",
    "size": "1280x720",
    "seconds": 5,
    "seed": 1234,
    "extensions": {
      "com.vividfish.workflow": {
        "steps": 20,
        "stage1_cfg": 1.0,
        "stage2_cfg": 1.0
      }
    }
  }'
```

ControlNet is not ready on the current local system. The audit exists, but
operators should not advertise or send ControlNet controls until a real
ControlNet workflow, model, manifest, and readiness check are present.

When a future workflow does advertise conditioning controls, keep them under
`extensions.com.vividfish.workflow`. For multipart image edits, an asset-valued
control can point at an extra form field without becoming a top-level API
parameter:

```bash
curl -s http://127.0.0.1:5000/v1/images/edits \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F model=flux2-klein-4b-edit-controlnet \
  -F prompt='preserve the geometry, paint it as underwater rock' \
  -F image=@input.png \
  -F control_image=@edge-map.png \
  -F 'extensions={"com.vividfish.workflow":{"control_image":{"multipart_field":"control_image"},"controlnet_strength":0.75}}'
```

Fetch `/gateway/v1/workflows` first. If `control_image` or
`controlnet_strength` is absent for the selected workflow, the request should
be treated as unsupported.

## Chaining And Variants

Generated image outputs can be reused without inventing new top-level fields:

- For OpenAI-compatible clients, request `response_format: "url"` and fetch
  `data[].url` or `data[].asset_url` with the same bearer token.
- For gateway-aware clients, keep `data[].asset_id` and pass it where a route
  advertises an asset input. Video `input_reference` accepts `{"asset_id":
  "img_<sha256>"}` or `asset:img_<sha256>` when the same API key minted the
  asset.
- For future conditioning workflows, asset-valued extension inputs such as
  `control_image` may use `{"asset_id":"img_<sha256>"}` instead of uploading a
  second multipart file, but only when discovery shows the selected workflow
  supports that asset input.

Variants use existing route semantics:

- Image generation and edits: use top-level `n` for count and manifest-backed
  `extensions.com.vividfish.workflow.seed` when the selected image workflow
  exposes `seed`.
- Video: repeat requests with different top-level `seed`; model aliases are
  quality/speed choices, not batch-variant controls.
- Do not send ad hoc fields such as `variant_count`, `variations`, or
  `controlnet_strength` unless a gateway-native route or workflow manifest
  explicitly advertises them.

### Live with documented limits

The Anthropic and Ollama frontends are live, but intentionally narrow:

- Streaming is supported on `/anthropic/v1/messages`, `/ollama/api/chat`, and
  `/ollama/api/generate`.
- Anthropic image/document/thinking blocks are rejected by the translator
  instead of silently dropped. Anthropic-native prompt caching markers and beta
  headers are not represented.
- Ollama read-only model routes are implemented: `/api/tags`, `/api/show`,
  `/api/ps`, and `/api/version`. Mutating routes (`/api/pull`, `/api/push`,
  `/api/copy`, `/api/delete`, `/api/create`) return an Ollama-shaped
  unsupported-operation error because the gateway does not mutate local model
  files through this protocol surface.
- Ollama's common generation options such as `temperature`, `top_p`,
  `num_predict`, `seed`, `stop`, `presence_penalty`, and
  `frequency_penalty` are mapped. Backend-native options such as `top_k`,
  `num_ctx`, GPU placement, and memory flags are rejected because they do not
  have a portable canonical meaning yet.

For maximum compatibility, point general clients at the OpenAI-compatible
surface and use the Anthropic/Ollama frontends only when a client specifically
requires those protocols.

Canonical protocol aliases:

| Protocol | Canonical base | Convenience base |
|---|---|---|
| OpenAI-compatible | `http://127.0.0.1:5000/openai/v1` | `http://127.0.0.1:5000/v1` |
| Anthropic Messages | `http://127.0.0.1:5000/anthropic` for SDK base URL; concrete endpoint is `/anthropic/v1/messages` | none |
| Ollama-compatible | `http://127.0.0.1:5000/ollama` for clients that append `/api`; concrete endpoints are under `/ollama/api/*` | `http://127.0.0.1:5000` for clients hard-coded to `/api/*` |

Provider-native discovery lives at `/gateway/v1/providers` and
`/gateway/v1/providers/{provider}`; results are projected through the caller's
App profile policy. Raw provider-native passthrough, when enabled for a key, is
reserved for `/gateway/v1/providers/{provider}/{path...}`. It is not exposed by
root paths, and there is no legacy root Tabby shim. Passthrough preserves
streamed response bodies such as SSE, NDJSON, and generated media rather than
buffering the whole upstream response first.

Collision strategy is intentionally simple: canonical protocol frontends own
`/openai/v1`, `/anthropic/v1`, `/ollama/api`, plus the legacy convenience
aliases `/v1` and `/api`. Provider-native passthrough never claims those roots.
If a backend has its own `/v1/models`, `/api/tags`, `/queue`, or similar native
path, call it through `/gateway/v1/providers/{provider}/{path...}` after
discovering the provider id and confirming the App profile allows
`native_passthrough`.

Auth semantics are also uniform. `/health`, `/.well-known/ai-gateway`,
`/.well-known/openai-proxy`, `/.well-known/skills`, `/llms.txt`, and
`/openapi.json` are public discovery. Protocol frontends, gateway-native
runtime APIs, and provider-native passthrough require
`Authorization: Bearer $OPENAI_PROXY_API_KEY`. Some SDKs name that field
`api_key`, `apiKey`, or `x-api-key`; the value is still the gateway `opk_...`
token, not an upstream OpenAI, Anthropic, or Ollama credential.

## Recipe: Open WebUI

Open WebUI talks to any OpenAI-compatible server. The OpenAI provider is the
best-supported path. Open WebUI's Ollama provider can use `/api/tags`,
`/api/show`, `/api/ps`, `/api/version`, and streaming `/api/chat` through the
convenience routes. Mutating model-management panels still fail clearly because
the gateway does not pull, push, create, copy, or delete local model files.

**Settings → Connections → OpenAI:**

```text
Base URL:  http://127.0.0.1:5000/openai/v1
API Key:   $OPENAI_PROXY_API_KEY
```

Then refresh models; Open WebUI will call the configured models endpoint and
populate the selector with whatever aliases and backend models the proxy
advertises.

Smoke test from the same host:

```bash
curl -s http://127.0.0.1:5000/openai/v1/models \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  | head -c 400
```

Expected unsupported features in Open WebUI against this proxy today:

- Ollama-specific mutation panels (pull, push, create, copy, delete) return
  unsupported-operation; manage model files on the backend that actually hosts
  them.
- Web search / RAG features that depend on Open WebUI's own pipelines work
  normally; they are client-side.
- Image generation works only if the configured image backend is reachable.

## Recipe: LibreChat

LibreChat reads its provider list from `librechat.yaml`. Add a `custom`
endpoint pointing at the proxy:

```yaml
endpoints:
  custom:
    - name: "openai-proxy"
      apiKey: "${OPENAI_PROXY_API_KEY}"
      baseURL: "http://127.0.0.1:5000/openai/v1"
      models:
        default: ["auto"]
        fetch: true
      titleConvo: true
      modelDisplayLabel: "Local Proxy"
```

Set `OPENAI_PROXY_API_KEY` in the LibreChat `.env`. Do not paste the literal
`opk_...` into `librechat.yaml`.

Expected unsupported features:

- LibreChat's Anthropic provider block can target `POST /anthropic/v1/messages`,
  including streaming. The OpenAI Chat surface remains the safer default for
  LibreChat features that assume OpenAI-specific endpoints.
- Plugin/Tools that hit OpenAI-only endpoints (e.g. assistant beta) — the
  proxy does not implement Assistants; the Responses surface is the closest
  live equivalent.

## Recipe: Continue (VS Code / JetBrains)

`~/.continue/config.json`:

```jsonc
{
  "models": [
    {
      "title": "Local Proxy",
      "provider": "openai",
      "model": "auto",
      "apiBase": "http://127.0.0.1:5000/openai/v1",
      "apiKey": "$OPENAI_PROXY_API_KEY"
    }
  ],
  "tabAutocompleteModel": {
    "title": "Local Proxy (autocomplete)",
    "provider": "openai",
    "model": "auto",
    "apiBase": "http://127.0.0.1:5000/openai/v1",
    "apiKey": "$OPENAI_PROXY_API_KEY"
  },
  "embeddingsProvider": {
    "provider": "openai",
    "model": "auto",
    "apiBase": "http://127.0.0.1:5000/openai/v1",
    "apiKey": "$OPENAI_PROXY_API_KEY"
  }
}
```

Replace `auto` with whichever alias your `proxy_config.yml` defines. Continue
substitutes `$OPENAI_PROXY_API_KEY` from environment at load time.

Expected unsupported features:

- Continue's "Anthropic" provider can target `POST /anthropic/v1/messages`,
  including streaming. The OpenAI provider remains the safer default when
  Continue expects OpenAI-specific endpoints.
- Continue's Ollama provider can use streaming `/ollama/api/chat`,
  `/ollama/api/generate`, read-only model routes, and tags. Native backend
  options that do not map to the canonical gateway are still rejected.
- Reranking provider — only works if a rerank-capable backend is configured.

## Recipe: Cline-style agents (Cline, Roo Code, Kilo, etc.)

These agents accept any OpenAI-compatible endpoint.

```text
API Provider:   OpenAI Compatible
Base URL:       http://127.0.0.1:5000/openai/v1
API Key:        $OPENAI_PROXY_API_KEY
Model ID:       auto   (or whichever alias proxy_config.yml defines)
```

Important caveats:

- Many Cline-style agents assume Anthropic-native tool-use semantics when
  pointed at Claude models. Through the OpenAI Chat surface, tools follow
  OpenAI's `tool_calls` schema. If the agent has a toggle for
  "OpenAI-style tools", enable it.
- Long-context features (prompt caching, beta headers) that depend on
  Anthropic-specific request headers are silently dropped at this surface.

## Recipe: Ollama-compatible clients

The proxy exposes a narrow Ollama-compatible surface backed by the canonical
chat-completions and embeddings pipelines:

| Path | Status |
|---|---|
| `GET /ollama/api/tags` | Live; returns the proxy's configured model catalog. |
| `POST /ollama/api/chat` | Live; supports `stream: true` NDJSON. |
| `POST /ollama/api/generate` | Live; supports `stream: true` NDJSON. |
| `POST /ollama/api/embed` | Live; current Ollama embeddings shape. |
| `POST /ollama/api/embeddings` | Live; legacy Ollama embeddings shape. |
| `POST /ollama/api/show` | Live; returns visible gateway catalog metadata. |
| `GET /ollama/api/ps` | Live; returns the visible currently loaded LLM ids. |
| `GET /ollama/api/version` | Live; returns the gateway compatibility version. |
| `/ollama/api/pull`, `/ollama/api/push`, `/ollama/api/copy`, `/ollama/api/delete`, `/ollama/api/create` | Registered, but returns unsupported-operation. |

Point the Ollama base URL at `http://127.0.0.1:5000/ollama` when the client
supports a pathful base URL. For clients hard-coded to `/api`, use
`http://127.0.0.1:5000` and the convenience `/api` routes. Send
`Authorization: Bearer $OPENAI_PROXY_API_KEY`; this is not a vanilla
unauthenticated Ollama daemon.

```bash
curl -fsS http://127.0.0.1:5000/ollama/api/tags \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" | head -c 400
```

Use the OpenAI-compatible surface for general-purpose clients. The Ollama
frontend is a protocol adapter over gateway-owned model state, not a model
lifecycle manager. These calls are explicitly unsupported and return an
Ollama-shaped unsupported-operation error without touching local files:
`/ollama/api/create`, `/ollama/api/pull`, `/ollama/api/push`,
`/ollama/api/copy`, and `/ollama/api/delete` (and the matching `/api/*`
convenience aliases). Manage downloads, Modelfiles, copies, and deletes in the
backend service that actually owns the model directory.

## Recipe: Anthropic SDK clients

The Anthropic Messages surface (`POST /anthropic/v1/messages`) is live as a
translator over the canonical chat-completions pipeline, including
Anthropic-shaped SSE streaming:

```python
import os
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:5000/anthropic",
    api_key=os.environ["OPENAI_PROXY_API_KEY"],
)

msg = client.messages.create(
    model="auto",
    max_tokens=256,
    messages=[{"role": "user", "content": "ping"}],
)
print(msg.content[0].text)
```

Streaming calls such as `client.messages.stream(...)` are supported. Do not put
real Anthropic keys into `proxy_api_keys.yml`.

## Recipe: OpenAI SDK clients

This is the best-supported path.

Python:

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:5000/openai/v1",
    api_key=os.environ["OPENAI_PROXY_API_KEY"],
)

resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "ping"}],
)
print(resp.choices[0].message.content)
```

Node:

```js
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://127.0.0.1:5000/openai/v1",
  apiKey: process.env.OPENAI_PROXY_API_KEY,
});

const r = await client.chat.completions.create({
  model: "auto",
  messages: [{ role: "user", content: "ping" }],
});
console.log(r.choices[0].message.content);
```

Smoke test (no SDK):

```bash
curl -s http://127.0.0.1:5000/openai/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"ping"}]}' \
  | head -c 400
```

Responses API:

```python
r = client.responses.create(
    model="auto",
    input="Summarize the README of this repo in one sentence.",
)
print(r.output_text)
```

The Responses surface is implemented as a translator over
`/v1/chat/completions` with a durable store, so behavior tracks chat
completions plus the Responses object shape.

Expected unsupported features:

- Assistants API — not implemented; use Responses.
- Fine-tuning, files, batches — not implemented.
- Realtime API — not implemented.

## Discovery and Debugging: `/gateway/v1/*`

The read-only gateway router (`gateway/api.py`) is mounted into the live app.
Power users and agents can use it for runtime discovery and post-hoc debugging
without scraping logs.

The audience returned to a client is determined server-side; default is
`client`, which redacts backend paths and policy sources.

### Workflows

```bash
curl -s http://127.0.0.1:5000/gateway/v1/workflows \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Returns the workflows the caller's profile is allowed to see, with portable
inputs and outputs (no backend node IDs at `client` audience).

### Capabilities

```bash
curl -s http://127.0.0.1:5000/gateway/v1/capabilities \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Use this from an agent at startup to decide what intents are actually
available right now. Treat absence of an intent as a hard "not supported"
signal rather than retrying blindly.

### Receipts

After making a request, capture the `request_id` (header or response body
depending on protocol) and:

```bash
curl -s "http://127.0.0.1:5000/gateway/v1/receipts/$REQUEST_ID" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Or list recent:

```bash
curl -s "http://127.0.0.1:5000/gateway/v1/receipts?limit=20" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

A receipt records: canonical intent, accepted/rejected/ignored inputs,
resolved model or workflow, policy decision summary, manifest ID/version/hash
when one participated, warnings, status, and latency.

At `client` audience, backend identity, raw policy sources, and gateway
filesystem details are redacted. Today, observational receipts are emitted
for `/v1/images/generations` and `/v1/images/edits` only; coverage will
expand as more handlers route through `gateway/receipts.py`.

Agents debugging a "why did this fail?" should:

1. Pull the receipt for the failing request.
2. Inspect `rejected_inputs` and `ignored_inputs` for reasons.
3. Inspect `warnings` for fallback notes.
4. Re-check `capabilities` if the intent appears unsupported.

## Migrating to Profile-Scoped Keys

Generated `opk_...` keys map to downstream credentials and may carry a live
`profile:` block (see `AUTH_GUIDE.md` § "Downstream Credential Mapping"). The
`AppProfile` concept (`gateway/profiles.py`, `gateway/policy.py`) layers a
named policy bundle on top of each client key:

- which protocols a client can use
- which model aliases it sees
- whether cloud fallback is allowed
- which credentials may be used downstream
- how much status and diagnostics it may see
- which native passthrough lanes are allowed

The migration path is:

1. **Do not rotate keys preemptively.** The existing `opk_...` values keep
   working; profile binding is additive in `proxy_api_keys.yml`.
2. **Add a `profile:` field** to each agent entry in `proxy_api_keys.yml`
   referencing a profile ID (e.g. `continue-local`, `image-agent`,
   `experimental-cloud`). The proxy reloads the file without restart.
3. **Use the profile fields in `proxy_api_keys.yml`.** Profiles never contain
   raw secrets; they reference downstream credential slots by name, exactly
   like the existing `downstream:` block.
4. **Verify with `/gateway/v1/capabilities`.** After binding a profile, the
   capabilities listed for that key should narrow or widen to match. If the
   set looks wrong, the profile is misconfigured; do not rotate the key.
5. **Only rotate the `opk_...` key itself** if it was exposed (committed,
   pasted, sent to a third party). Profile changes do not require key
   rotation. Use `proxy.py generate-key <name>` to rotate; the printed
   `opk_...` is shown once and never stored in plaintext.

What this document deliberately does *not* do: print any real key, real
hash, real env name from a downstream service block, or any value from a
live `proxy_api_keys.yml`. The placeholders `opk_...`, `sha256:...`, and
`$OPENAI_PROXY_API_KEY` are the only forms that should appear in shared
docs and commit history.

## Operational Notes

- `/health` is the only unauthenticated endpoint. Use it for liveness in
  Docker/systemd; do not use authenticated endpoints for health checks.
- The proxy reloads `proxy_api_keys.yml` on change. Adding, rotating, or
  revoking a key does not require a restart.
- Streaming clients must respect server-side idle timeouts on
  `/v1/chat/completions` and `/v1/completions`. A long stall in an
  intermediate proxy/CDN will surface as a dropped stream to the SDK.
- When a client misbehaves, use an admin session/key to check `/v1/status`,
  then inspect `/gateway/v1/receipts` for the specific `request_id` before
  opening logs.

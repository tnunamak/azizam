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

For new installs, create a key from `/setup`, the account portal (`/`), or
`/admin` so it is stored in `admin.db`, as documented in `AUTH_GUIDE.md`. Then
export it in the shell you run smoke commands from:

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
| `POST /openai/v1/images/generations` | OpenAI Images | Synchronous image generation; emits observational gateway receipts. Honors `output_format`/`output_compression`/`background`/`quality`; see "Image output shape" below. |
| `POST /openai/v1/images/edits` | OpenAI Images | Emits observational gateway receipts. Accepts up to as many `image[]` reference images as the resolved workflow supports (every shipped workflow today: 1; more get a clear 400, not a silent drop). Same `output_format`/`output_compression`/`background`/`quality` handling as generations. |
| `POST /openai/v1/images/variations` | OpenAI Images | Routed through a configurable edit-capable workflow (`images.variations.default_workflow` in `proxy_config.yml`) with a fixed generic prompt -- there is no local model dedicated to variations. See "Image variations" below. |
| `GET /openai/v1/images/{id}/content` | OpenAI Images | Asset retrieval. |
| `GET /v1/assets/{asset_id}/content` | Native | Signed generated-asset retrieval. |
| `GET /gateway/v1/assets/{asset_id}` | Gateway native | Generated-asset metadata and fresh signed content URL. |
| `GET /gateway/v1/assets/{asset_id}/content` | Gateway native | Signed generated-asset retrieval through the native surface. |
| `POST /gateway/v1/media/jobs` | Gateway media jobs | Durable async image generation when `intent` is `image.generate` and `request` carries the OpenAI image body. Variations are not available through this durable-job surface; use `POST /v1/images/variations` directly. |
| `POST /openai/v1/audio/speech` | OpenAI Audio | TTS. |
| `POST /openai/v1/audio/transcriptions` | OpenAI Audio | STT. |
| `POST /openai/v1/audio/translations` | OpenAI Audio | Registered compatibility route; returns `unsupported_audio_translation` unless the local STT backend declares translation support. |
| `GET /openai/v1/voices`, `GET /openai/v1/audio/voices` | OpenAI Audio | Voice list. |
| `POST /openai/v1/audio/generate`, `GET /openai/v1/audio/models` | Gateway native | Music/SFX/voice generation; not an OpenAI SDK-compatible audio endpoint. |
| `GET/POST /openai/v1/assistants`, `/files`, `/batches`, `/fine_tuning/*`, `/realtime/*` | OpenAI compatibility stubs | Registered compatibility routes; return `unsupported_operation`. |
| `POST /openai/v1/videos`, `GET /openai/v1/videos/{id}`, `GET /openai/v1/videos/{id}/content` | OpenAI-shaped video | Submit/poll/download. |
| `GET /openai/v1/videos` | OpenAI-shaped video | List video jobs *submitted by the calling API key* via this route since the gateway process last restarted (`limit`, `after`, `order`). No durable store -- same statelessness as retrieve/content. |
| `DELETE /openai/v1/videos/{id}` | OpenAI-shaped video | Cancels a queued/running job (ComfyUI `/api/jobs/{id}/cancel`) before tombstoning it; a `409` means cancellation couldn't be confirmed, not that the job survived. Scoped to the owning API key -- all four of status/content/list/delete are. |
| `POST /openai/v1/videos/{id}/remix` | OpenAI-shaped video | Stub: always a stable `501` (`video_remix_not_configured`, or `video_remix_not_implemented` if an operator sets `videos.remix_workflow`) -- no remix backend exists on this gateway. |
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
inspect or print local key stores.

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

`asset_url` and `/v1/images/{id}/content` URLs are signed capability URLs. Fetch
the bytes directly; SDKs and browsers do not need to attach the original bearer
token. `asset_id` can be passed back to gateway-aware routes that explicitly
accept an image input reference. Asset IDs are scoped to the API key that minted
them for reuse as inputs, while signed content URLs are shareable until they
expire.

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
signed, bearerless cached assets instead of storing base64 response bodies.
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
use white to edit and black to preserve. The default Qwen editor routes masked
requests to `qwen-image-2.1-inpaint`, which binds the mask through source-image
alpha. Explicit Flux2 edit workflows keep their documented post-composite
fallback because they have no native inpaint node.

Image edit with multiple reference images (gpt-image-1 shape, repeated
`image[]`):

```bash
curl -s http://127.0.0.1:5000/v1/images/edits \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F model=flux2-klein-4b-edit \
  -F prompt='combine these references' \
  -F 'image[]=@reference-a.png' \
  -F 'image[]=@reference-b.png'
```

The default Qwen edit route selects `qwen-image-2.1-edit-multiref` for two or
more uploaded images. That workflow accepts exactly two references. Other
single-image workflows reject extra uploads with a clear `400` rather than
silently dropping them. Workflows declare image capacity with
`image_nodes: ["<node_a>", "<node_b>"]` in their raw workflow JSON's `_meta`
block; uploads route to those nodes in submission order.

### Image output shape

`output_format` (`png`/`jpeg`/`webp`), `output_compression` (0-100, jpeg/webp
only, integers only -- `100.9` is rejected, not silently truncated), and
`quality` (`low`/`medium`/`high`/`auto`, plus the legacy dall-e-2/dall-e-3
values `standard`/`hd` mapped onto `medium`/`high`) work on both
`/v1/images/generations` and `/v1/images/edits`. A jpeg request's
`background` never resolves to removal even when left at the default
`"auto"` -- jpeg has no alpha channel, so there is nothing for an "auto"
decision to remove:

```bash
curl -s http://127.0.0.1:5000/v1/images/generations \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "flux2-klein-4b",
    "prompt": "a lighthouse at dusk",
    "size": "1024x1024",
    "background": "transparent",
    "output_format": "webp",
    "output_compression": 80
  }'
```

`output_format`/`output_compression` drive a real Pillow re-encode of the
workflow's native PNG output; the response's `data[].media_type` and (for
`response_format: "url"`) the signed URL's content type reflect the requested
format. `background: "transparent"` combined with `output_format: "jpeg"` is
rejected with a `400` (jpeg has no alpha channel), matching OpenAI itself.
`quality` maps to a per-workflow sampler-step count only when the resolved
workflow's manifest declares a `quality_steps` table (none of the shipped
Flux2 Klein manifests do yet — this distilled model is tuned around a fixed
step count, and scaling its steps without empirically verifying output
quality first risked a false "high quality" being worse than the default, so
this was deliberately left as a data-only hook rather than guessed at). When
no `quality_steps` table applies, `quality` is still accepted (no error) and
recorded as accepted-but-not-applied on the request's gateway receipt — never
silently dropped with no trace. The response always echoes the resolved
`output_format`/`background`/`quality`/`size` at the top level, matching the
gpt-image-1 response shape (`background`/`quality` echo the value actually
applied, never the literal `"auto"` OpenAI itself never returns).

### Image variations

`/v1/images/variations` has no prompt in the request (unlike edits), so there
is no local "true" variations model to route to — it is routed through a
configurable edit-capable workflow instead, with a fixed generic prompt:

```bash
curl -s http://127.0.0.1:5000/v1/images/variations \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F image=@input.png \
  -F n=2 \
  -F size=1024x1024
```

The backing workflow is `images.variations.default_workflow` in
`proxy_config.yml` (default: `qwen-image-2.1-edit`, matching the edit route's
own default). `model` is accepted: an OpenAI-named model (`dall-e-2`,
`dall-e-3`, `gpt-image-1`, ...) resolves to the configured default; a
concrete local workflow id (e.g. `flux2-klein-4b`) is honored literally
(with `-edit`/`-inpaint` suffixed the same way the edit route does). When the
resolved workflow's manifest declares a `denoise` input (both shipped
`flux2-klein-4b-edit` and `flux2-klein-4b-inpaint` do), a configurable
partial-denoise strength (`images.variations.denoise`, default `0.75`) is
applied so the output resembles but is not identical to the source image.
Fields `/v1/images/variations` does *not* accept (because OpenAI's own
variations request doesn't have them): `prompt`, `mask`, `background`,
`output_format`, `output_compression`, `quality`.

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

List video jobs submitted by the calling API key via this route since the
gateway process last restarted (no durable store, same statelessness as
retrieve/content -- and the same ownership scoping status/content/delete
already have: a different API key's jobs never appear here):

```bash
curl -s "http://127.0.0.1:5000/v1/videos?limit=10&order=desc" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

A pagination `after` cursor that no longer resolves (evicted, deleted, or
belongs to a different key) returns a `400 cursor_not_found` rather than
silently restarting from page one.

Delete a video job. A still-queued/running job is actively cancelled first
(ComfyUI's `/api/jobs/{id}/cancel`) before being tombstoned; if the cancel
can't be confirmed, this returns `409` instead of claiming `deleted: true`
for a job that may keep rendering:

```bash
curl -s -X DELETE "http://127.0.0.1:5000/v1/videos/$VIDEO_ID" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Remix is a stub — no backend exists for it on this gateway yet. It always
returns a stable `501`:

```bash
curl -s -X POST "http://127.0.0.1:5000/v1/videos/$VIDEO_ID/remix" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
# {"error": {"message": "...", "type": "invalid_request_error", "code": "video_remix_not_configured"}}
```

Set `videos.remix_workflow` in `proxy_config.yml` to change the error code to
`video_remix_not_implemented` — this does not enable remix, it only moves the
config surface a future implementation would land on.

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
- Anthropic image blocks are translated into OpenAI-compatible multimodal chat
  content and use the gateway's normal vision routing. Document/thinking blocks
  are rejected instead of silently dropped. Anthropic-native prompt caching
  markers and beta headers are not represented.
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

Each provider discovery row includes policy facts scoped to the authenticated
caller: whether `native_passthrough` is enabled or denied, the caller-visible
allowed backend and operation set, the gateway auth requirement, non-secret
downstream auth classes such as `api_key`, `admin_key`, or configured headers
when known, and the provider's live enabled state. Discovery never returns
credential values or environment variable contents.

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

The chat and generate translators map common portable options such as
`temperature`, `top_p`, `num_predict`, `seed`, `stop`, and OpenAI-compatible
penalties into the canonical chat request. Backend-native controls such as
`num_ctx`, `top_k`, and `repeat_penalty` stay explicit and fail rather than
being silently ignored. Ollama `images` are translated to OpenAI-style
multimodal content blocks and use the gateway's normal vision routing; if no
vision backend is configured or permitted, the chat path returns a clear vision
error. `raw: true` remains unsupported because Ollama raw prompting bypasses
model templating in a way the canonical chat-completions pipeline cannot
preserve exactly across backends.

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

Streaming calls such as `client.messages.stream(...)` are supported. The
Anthropic model list lives at `GET /anthropic/v1/models`, and token counting
uses `POST /anthropic/v1/messages/count_tokens`. Do not put real Anthropic keys
into source files, prompts, or shared docs.

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

- Assistants API — registered compatibility routes return `unsupported_operation`; use Responses.
- Fine-tuning, files, batches — registered compatibility routes return `unsupported_operation`.
- Realtime API — registered compatibility routes return `unsupported_operation`.

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
for `/v1/images/generations`, `/v1/images/edits`, and `/v1/images/variations`
(which reuses the edit route's receipt end-to-end) only; coverage will
expand as more handlers route through `gateway/receipts.py`.

Agents debugging a "why did this fail?" should:

1. Pull the receipt for the failing request.
2. Inspect `rejected_inputs` and `ignored_inputs` for reasons.
3. Inspect `warnings` for fallback notes.
4. Re-check `capabilities` if the intent appears unsupported.

## Profile-Scoped Keys

Dashboard-generated and imported `opk_...` keys authenticate through
`admin.db`. Each key belongs to an App, and the App's stored Profile layers a
named policy bundle on top of the runtime key:

- which protocols a client can use
- which model aliases it sees
- whether cloud fallback is allowed
- how much status and diagnostics it may see
- which native passthrough lanes are allowed

The profile-binding path is:

1. **Do not rotate keys preemptively.** Existing imported `opk_...` values keep
   working after their hashes are in `admin.db`.
2. **Create or edit a Profile** in `/admin` referencing the protocols, models,
   workflows, and provider-native surfaces the App should use.
3. **Attach the Profile to the App** in `/admin`, or let the App inherit from
   the owning user's dashboard-managed identity/Profile mapping.
4. **Verify with `/gateway/v1/capabilities`.** After binding a Profile, the
   capabilities listed for that key should narrow or widen to match. If the
   set looks wrong, the Profile is misconfigured; do not rotate the key.
5. **Only rotate the `opk_...` key itself** if it was exposed (committed,
   pasted, sent to a third party). Profile changes do not require key rotation.

What this document deliberately does *not* do: print any real key, real hash,
real env name from a downstream service block, or any value from a live key
store. The placeholders `opk_...`, `sha256:...`, and `$OPENAI_PROXY_API_KEY`
are the only forms that should appear in shared docs and commit history.

## Operational Notes

### SeedVR2 image upscale

The image edit route executes the `seedvr2-upscale` workflow. It uses a
multipart upload and returns the usual image JSON response:

```bash
curl -s http://127.0.0.1:5000/v1/images/edits \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F 'model=seedvr2-upscale' \
  -F 'prompt=Upscale and restore this image faithfully' \
  -F 'image=@input.png' \
  -F 'extensions={"com.vividfish.workflow":{"scale":2,"color_correction_method":"lab"}}'
```

`scale` accepts `2` or `4`; `color_correction_method` accepts `none`, `lab`,
`wavelet`, or `adain`. The prompt is required by the shared image-edit route
but is not consumed by this graph.

### RIFE video interpolation

Upload-driven video-edit manifests run synchronously through the gateway's
manifest executor. `workflow_id` can be `rife-interpolate` or the full
manifest id `video.edit.rife-interpolate`. The response body is the rendered
video file:

```bash
curl -sS http://127.0.0.1:5000/gateway/v1/workflows/rife-interpolate/run \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F 'source_video=@input.mp4;type=video/mp4' \
  -F 'multiplier=2' \
  -o interpolated.mp4
```

The workflow manifest validates the multiplier. `2` is the verified setting;
the RIFE node also declares `3` and `4` as supported values.

- `/health` is the only unauthenticated endpoint. Use it for liveness in
  Docker/systemd; do not use authenticated endpoints for health checks.
- Dashboard Apps, Profiles, and keys live in `admin.db` and apply live through
  the admin store.
- Streaming clients must respect server-side idle timeouts on
  `/v1/chat/completions` and `/v1/completions`. A long stall in an
  intermediate proxy/CDN will surface as a dropped stream to the SDK.
- When a client misbehaves, use an admin session/key to check `/v1/status`,
  then inspect `/gateway/v1/receipts` for the specific `request_id` before
  opening logs.

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
Public base host:  https://ai.example.com        # optional, when exposed
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

## Supported interfaces

A facade route either works with its official client or returns an explicit,
vendor-shaped "not supported" error. It never reports success it did not
achieve. "Proof" names the offline test that checks the behavior against a
pinned vendor schema. "Live" means an official client run against the gateway
on 2026-10-02; those scripts are not part of the test suite.

| Interface | Feature | Status | Proof |
|---|---|---|---|
| **Anthropic Messages** (`/anthropic/v1`, `/v1/messages`) | `messages.create`, `messages.stream`, system prompts, multi-turn | Works | `tests/test_anthropic_schema_conformance.py` (schemas from `anthropic` SDK 1.11.0); Live: Anthropic Python SDK, Claude Code 2.1.286 |
| | Tools (`tool_use`/`tool_result`, `tool_choice`), streamed tool calls | Works | same |
| | Images (base64/URL), `thinking` blocks | Works | same |
| | `count_tokens` | Estimate only (about 4 characters per token) | same |
| | `models.list`, `models.retrieve` | Works | same |
| | Errors | Anthropic shape, typed by status, `request-id` header | same |
| | `stop_sequence` field, prompt-cache usage fields | Not reported (`stop_sequences` still stop generation) | documented gap |
| | `document` blocks, server tools (web search, code execution, computer use), batches, files | Unsupported: 400 `invalid_request_error` or route absent | `tests/test_protocol_frontends.py` |
| **Ollama** (`/ollama/api`, `/api`) | `chat`, `generate`, streaming (default) and non-streaming | Works | `tests/test_ollama_api_conformance.py` (schemas from `docs/api.md`, ollama/ollama `b0c1ca4`); Live: `ollama` Python client 0.6.3 |
| | Tools, images, `format` (JSON and schema), `think` | Works | same |
| | `embed`, `embeddings`, `tags`, `show`, `ps`, `version` | Works | same |
| | Empty-prompt load/unload | Answers like Ollama but does not load or free anything | same |
| | Sampling options; runtime options (`num_ctx`, `num_gpu`, ...) | Sampling forwarded; runtime options ignored | same |
| | `suffix`, `template`, `raw` | Unsupported: 400 Ollama-shaped error | same |
| | `pull`, `push`, `create`, `copy`, `delete`, `blobs` | Unsupported: 501 `{"error": "unsupported operation: ..."}`. The gateway owns no model files. | same |
| **OpenAI Responses** (`/v1/responses`, `/openai/v1/responses`) | `responses.create`, streaming events, function tools, `previous_response_id`, retrieve, delete, `input_items`, images, reasoning items, `text.format` | Works | `tests/test_openai_responses_conformance.py` (schemas from openai/openai-openapi `145fd3b`); Live: `openai` Python SDK 3.23.0 |
| | Stored responses | Private to the creating key; unknown or foreign `previous_response_id` is a 404 | same |
| | Hosted tools (web search, file search, code interpreter, computer use), `background`, `conversation`, `prompt`, `cancel`, `input_tokens` count | Unsupported: 400 with a specific `code` | same |
| | Reasoning summaries (`reasoning.summary`) | Not produced; raw reasoning returns as a `reasoning` item | documented gap |
| **OpenAI** chat, completions, embeddings, models, images, audio, video | Response shapes | Works | `tests/test_openai_schema_conformance.py` (openai/openai-openapi `145fd3b`); Live: `openai` Python SDK |
| **Rerank** (`/v1/rerank`, `/openai/v1/rerank`, `/rerank`, `/v2/rerank`) | Cohere and Jina body (`query`, `documents` as strings or `{"text"}` objects, `top_n`, `return_documents`); Voyage `top_k`; TEI `texts` (answered as TEI's bare list) | Works | `tests/test_rerank.py`; Live: `cohere` Python SDK 7.2.0 (`Client` and `ClientV2` with `base_url`) |
| | Response | `results[]` and `data[]` (same items: `index`, `relevance_score` in 0..1, optional `document.text`), `id`, `usage`, `meta` | same |
| | Errors | 400 with `message` (Cohere), `detail` (Jina, Voyage) and `error{}` (gateway) in one body; TEI bodies get TEI's `error` and `error_type` | same |
| | `max_tokens_per_doc`, `max_chunks_per_doc`, `rank_fields` | Unsupported: 400 `unsupported_parameter` (they would change what is scored) | same |
| **MCP** (`/mcp`, Streamable HTTP) | Three tools, `capabilities`, `run` and `job`, over every image, audio and video workflow, the stable aliases, and `speech` | Works | `tests/test_mcp_server.py`; Live: Claude Code 2.1.286, `pi-mcp-adapter` 4.0.0 (legacy, auto and 2026-07-28), MCP Python SDK 1.26.0 |
| | Protocol revisions | 2026-07-28 (stateless) and 2025-03-26 to 2025-11-25 (no sessions) | same |
| | Auth | Bearer app key. A 401 carries an RFC 9728 `resource_metadata` challenge. There is no OAuth server. | same |
| | Results | `resource_link` blocks that point at signed asset URLs; no base64 | same |
| | Resources, prompts, sampling, elicitation, tasks, `subscriptions/listen`, SSE streams, `GET /mcp`, session ids | Unsupported: `-32601` or 405 | same |
| | External image URLs as tool inputs | Unsupported: tool error. Use a data URI or an asset id from an earlier result. | same |
| **OpenAI Realtime** (`wss://.../v1/realtime`, `/openai/v1/realtime`) | WebSocket session: `session.update` (instructions, voice, tools, server VAD, PCM16 24 kHz), `input_audio_buffer.append`, `speech_started`/`speech_stopped`, input transcription, `response.create`/`cancel`, audio and transcript deltas, function calls, `response.done` | Works | `tests/test_realtime_proxy.py` (relay and auth); Live: `openai` Python 2.28.0 `realtime.connect` and `@openai/agents` 0.14.3 `RealtimeSession` (voice turns, barge-in, function call) |
| | Turn handling, tools, barge-in | Done by the sibling Hugging Face `speech-to-speech` service, not the gateway. About 1.5 to 2 s from end of speech to first audio. | same |
| | WebRTC (`/v1/realtime/calls`), `client_secrets`, SIP, `conversation.item.truncate` (accepted, no effect), server-side audio formats other than PCM16 24 kHz, `semantic_vad` | Not supported, or not tested | documented gap |
| **A1111 / SD WebUI** (`/sdapi/v1`, host root) | `txt2img`, `options`, `sd-models`, `samplers`, `schedulers`, `upscalers`, `latent-upscale-modes`, `sd-vae`, `progress`, `interrupt` | Works | `tests/test_a1111_api.py`; Live: SillyTavern 1.18.0 server code against a temporary gateway |
| | Hires fix, `restore_faces`, ADetailer and other scripts, LoRA tags, styles, variation seeds | Unsupported: 422 that names the field | same |
| | Negative prompt, sampler, scheduler, `cfg_scale`, `clip_skip` on a model that does not use them | Dropped; the names are in the `x-gateway-ignored-params` header | same |
| | `img2img`, extras upscaling, `interrogate`, `png-info` | Unsupported: 501 | same |

Per-client recipes follow in this document.

## Live Surfaces Today

The proxy currently exposes (live, mounted in `proxy.py`):

| Path | Protocol | Notes |
|---|---|---|
| `GET /health` | — | Public, no auth. |
| `POST /openai/v1/chat/completions` | OpenAI Chat | Streaming + non-streaming; `/v1/chat/completions` remains a convenience route. |
| `POST /openai/v1/completions` | OpenAI Completions | Legacy, supported. |
| `POST /openai/v1/embeddings` | OpenAI Embeddings | CPU `nomic-embed-text-v1.5` backend; no GPU budget claim. |
| `GET /openai/v1/models` | OpenAI Models | Aliases + backend models. Each entry carries `type`, `architecture`, and when known `tags`/`max_tokens`/`context_window`: see [MODEL_METADATA.md](api/MODEL_METADATA.md). |
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
| `GET /openai/v1/voices`, `GET /openai/v1/audio/voices` | OpenAI Audio | Built-in voice list, plus the caller's custom voices under `data` (OpenAI list shape). |
| `POST /openai/v1/audio/voices`, `GET/POST/DELETE /openai/v1/audio/voices/{id}`, `GET .../voices/{id}/sample`, `POST/GET /openai/v1/audio/voice_consents`, `GET/POST/DELETE .../voice_consents/{id}` | OpenAI Audio | Custom voices and consent recordings; see "Custom voices" below. |
| `POST /openai/v1/audio/generate`, `GET /openai/v1/audio/models` | Gateway native | Music/SFX/voice generation; not an OpenAI SDK-compatible audio endpoint. |
| `GET/POST /openai/v1/assistants`, `/files`, `/batches`, `/fine_tuning/*`, `/realtime/*` | OpenAI compatibility stubs | Registered compatibility routes; return `unsupported_operation`. |
| `POST /openai/v1/videos`, `GET /openai/v1/videos/{id}`, `GET /openai/v1/videos/{id}/content` | OpenAI-shaped video | Submit/poll/download. |
| `GET /openai/v1/videos` | OpenAI-shaped video | List video jobs *submitted by the calling API key* via this route (`limit`, `after`, `order`). Job records are persisted for 14 days and survive a restart. |
| `DELETE /openai/v1/videos/{id}` | OpenAI-shaped video | Cancels a queued/running job (ComfyUI `/api/jobs/{id}/cancel`) before tombstoning it; a `409` means cancellation couldn't be confirmed, not that the job survived. Scoped to the owning API key -- all four of status/content/list/delete are. |
| `POST /openai/v1/videos/extensions` | OpenAI-shaped video (Sora's extend) | Add footage to a completed video; the result is the source plus the new segment. `501 video_extend_not_configured` if `videos.extend_workflow` is unset. See "Long video". |
| `POST /openai/v1/videos/{id}/remix`, `POST /openai/v1/videos/edits` | OpenAI-shaped video | Remix a completed or uploaded video with a new prompt (Bernini-R). `501 video_remix_not_configured` if `videos.remix_workflow` is unset. See "Remix and character replace". |
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

YuE2 song generation (songs with vocals; license CC BY-NC, non-commercial):

```bash
curl -s http://127.0.0.1:5000/v1/audio/generate \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "yue2-song",
    "tags": "indie folk pop, warm female vocals, fingerpicked acoustic guitar",
    "lyrics": "[Verse 1]\nMorning light on the window sill\n\n[Chorus]\nRun with me into the sunlight",
    "duration": 120,
    "seed": 42
  }' -o song.mp3
```

`tags` is the style prompt (`prompt` also works). `lyrics` uses `[Verse]`/`[Chorus]` section
markers. `duration` is a MAXIMUM in seconds (10-240, default 120), not an exact length: the
model decides the real length and it is usually shorter. Output is MP3. A 2-minute song takes
about 1 minute. Aliases: `yue2`, `song`.

Stable Audio 3 small-sfx (default for `model: "sfx"` and `"ambient"`):

```bash
curl -s http://127.0.0.1:5000/v1/audio/generate \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "sa3-small-sfx", "prompt": "glass bottle breaking on concrete", "duration": 4}' \
  -o sfx.flac
```

`duration` is 1-120 s (default 10); output is stereo 44.1 kHz FLAC, about 1 s per clip when warm.
The gateway peak-normalizes it to -1 dBFS (the raw model output is quiet; set
`audio.workflows.<id>.normalize_peak_db` to do the same for another workflow). Use
`"model": "tangoflux"` for the previous SFX model.

Video generation. The default model is LTX-2.5 (`ltx-2.5`, also `sora-2`, `sora-2-pro`,
and any request without `model`). MiniMax H3 is `minimax-h3`. Clips longer than 10 s, storyboards
(`shots`) and extensions use the long-video chain of the model (`ltx-2.5-long`, `video-long`, `h3-long`; see "Long video"). LTX-2.3 is retired: every `ltx-2.3*` name still works and
points at the LTX-2.5 workflow for its role (see the table below).

```bash
# Text to video (LTX-2.5, default)
curl -s http://127.0.0.1:5000/v1/videos \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a lighthouse at dusk, waves crashing", "seconds": 5, "size": "960x512"}'
# Image to video: add input_reference (path, URL, data URI, base64, or multipart file).
# The gateway picks the i2v graph (video-ltx-2.5-i2v / video-minimax-h3-i2v) itself.
```

Request fields map as follows: `prompt` to the text encoder; `seconds` to frame count at 24 fps;
`size` to width x height; `input_reference` to the first frame; `seed` and `negative_prompt`
(LTX only); `image_strength` (LTX i2v, 0-1, default 0.7). `GET /v1/videos/{id}` polls and
`GET /v1/videos/{id}/content` returns the MP4 (with audio). The create response reports the
`size` and `actual_seconds` that were really used.

| Model | seconds | size | Frames | Measured (3090) |
|---|---|---|---|---|
| `ltx-2.5` / `-i2v` | 1-10 | multiples of 64, max 1280x704, default 960x512 | 8k+1 | 960x512 5 s: 45 s warm, 107 s cold; 1280x704 5 s: 140 s; i2v 85 s |
| `minimax-h3` / `-i2v` | 2-10 | multiples of 32, max 1344x768, default 960x544 | 17k+5 | 960x544 5 s: 120-250 s; i2v 185 s |

LTX-2.5 needs width and height to be multiples of 64, so the gateway floors any other size
(for example `1280x720` becomes `1280x704`, `1000x600` becomes `960x576`) instead of rejecting it.
Seconds snap to the nearest valid frame count; `X-Duration-Adjusted` appears when the change is over
0.05 s. MiniMax H3 reads sound cues from an `Audio:` paragraph in the prompt. H3 license: the
MiniMax H3 Community License excludes use in the US, EU, UK and South Korea. LTX-2.x Community
License applies to LTX. An `*-i2v` model ID without `input_reference` returns 400.

Retired LTX-2.3 names (kept so old clients work):

| Old name | Now runs | Notes |
|---|---|---|
| `ltx-2.3`, `ltx-2.3-distilled`, `ltx-2.3-22b`, `video-ltx-2.3-distilled` | `video-ltx-2.5-t2v` | `input_reference` still switches to image-to-video |
| `ltx-2.3-fast`, `ltx-2.3-distilled-fast`, `ltx-2.3-pure-distilled`, `video-ltx-2.3-pure-distilled` | `video-ltx-2.5-t2v` | `video-fast` is the stable name |
| `ltx-2.3-detail`, `video-ltx-2.3-detail` | `video-ltx-2.5-t2v` | request `size: 1280x704` for the highest detail |
| `ltx-2.3-long`, `ltx-2.3-long-form`, `video-ltx-2.3-long` | `video-ltx-2.5-long` | long-video chain (see "Long video"); `video-long` is the stable name |

Differences from LTX-2.3: width and height snap to multiples of 64 (`ltx-2.5-long`: 32);
the LTX-2.3 `steps`, `stage1_cfg`, and `stage2_cfg` workflow controls no longer exist (the
LTX-2.5 distilled model has a fixed 8-step schedule at CFG 1), so sending them returns
400 `invalid_workflow_extension`. `ltx-2.5-long` output size follows the 16:9 placeholder
frame (default about 960x512) and is single-stage, with no x2 upscale.

List video jobs submitted by the calling API key via this route. Records are
stored in `media_jobs.db` (table `video_job_records`) and kept 14 days, so ids
and their owner survive a gateway restart. Status, content, list, delete, remix,
edit and extend are all scoped to the owning key: another key's id, and any id
the gateway has no record of (never made here, expired, or made before records
were stored), returns `404 video_id not found` (unless auth is disabled). Set `videos.allow_untracked_ids: true` to keep videos made before records were stored readable for a while. A
different API key's jobs never appear in the list:

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

### Long video

LTX-2.5 makes about 10 s in one pass. Longer video is built in segments: each new
segment is conditioned on the last second of the video so far (a latent guide on
`context_seconds`, so motion carries across the join, not one frame), with its own
prompt. All segments of one request run as one ComfyUI job, so the video id,
status, progress, content and delete work as for any video. Output is single-stage
(about 960x512 by default, no x2 upscale), 24 fps, with audio. Total length (source
included) is capped by `videos.max_total_seconds` (default 40): past it you get
`400 video_total_length_exceeded`, because quality drops with every added segment.
Measured: a 24 s, three-prompt chain took 310 s on the 3090 (about 100 s per 8 s).

Three ways in, from most to least control:

```python
# 1. Extend a finished video (Sora's shape). The result is source + new footage.
v = client.videos.create_and_poll(model="ltx-2.5", prompt="a cyclist on a coast road", seconds=8)
# openai-python 3.x sends `video` as a file part only (`video={"id": ...}` raises client-side), so
# name a zero-byte upload after the video id. The gateway reads it as that id.
v2 = client.videos.extend(video=(f"{v.id}.mp4", b"", "video/mp4"), prompt="she climbs into the pines",
                          seconds="8", extra_body={"context_seconds": 1.0})
v2 = client.videos.poll(v2.id)       # v2.seconds is the stitched total, v2.remixed_from_video_id == v.id

# 2. Storyboard: one request, one prompt and length per shot (gateway extension on /v1/videos).
client.videos.create(model="video-long", prompt="", extra_body={"shots": [
    {"prompt": "a lone cyclist on a coast road, golden hour", "seconds": 8},
    {"prompt": "the road climbs into pine forest", "seconds": 8},
    {"prompt": "she coasts down into a white village", "seconds": 8}]})

# 3. Sugar: a plain long `seconds`. The prompt is reused for every segment.
client.videos.create(model="ltx-2.5", prompt="a slow aerial over a glacier", seconds=24)
```

`POST /v1/videos/extensions` takes `{"video": {"id": ...}, "prompt": ..., "seconds": N}` as JSON or
multipart (`video[id]`, an uploaded file in `video`, or a zero-byte `video` file named `<video id>.mp4`, which is the form openai-python can send). `seconds` is the length of the NEW footage
(the prompt describes only the new action); the resource's `seconds` is the stitched total, and
`total_seconds` repeats it as a number. `remixed_from_video_id` is the source id (the OpenAI
spec has no extension field), and `extended_from_video_id` repeats it. Fields: `model`, `seed`,
`size` (default: the source's size), `negative_prompt`, `context_seconds` (0.34-4, default 1; the
overlap taken from the end of the source), and under
`extensions.com.vividfish.workflow`: `state_ledger`, `context_strength` (0-1, default 0.9).
`input_reference` and `shots` are not accepted on extensions. A segment is at most 8 s
(`videos.chain_segment_seconds`); a longer request is split into near-equal segments with
the same prompt. Frame counts are multiples of 8, so the result can differ from the request
by a fraction of a second (`X-Duration-Adjusted`). The source needs at least
`8 * ceil(context_seconds * fps / 8) + 1` frames (33 at the default).

`shots` on `POST /v1/videos` is a list of 1-12 `{prompt, seconds}` objects (each at least 1 s).
Omit top-level `seconds`, or make it match the sum. The first shot is a normal text-to-video
(or image-to-video with `input_reference`) segment. `shots` works with `ltx-2.5`, `video-long`,
`ltx-2.5-long` and the old `ltx-2.3-long*` names; other models return `400 long_video_not_supported`.
`seconds` above a model's native length does the same on `ltx-2.5`.

Failure: the job fails as a whole and no partial video is kept (a truncated clip is
never returned as the result). The error says where it stopped:
`{"code": "video_generation_failed", "message": "segment 3 of 4 failed (2 finished); ...",
"failed_segment": 3, "segments_completed": 2, "segments_total": 4, "completed_seconds": 14.04}`.
While running, `segments_completed` and `segments_total` are on the resource and `progress`
spans all segments. To keep finished work, send shorter chains and extend (each is its own video).

Strategy that works with these limits:

- Use native length (`ltx-2.5`, up to 10 s) when one shot is enough; it is the best quality.
- Extend with the overlap for one continuous action; keep `context_seconds` at 1-2 s.
- Use `shots` when the content changes (new place, new action): give every shot its own prompt.
  A single prompt over 24 s makes the model hold the first scene.
- Split at rest beats: end a shot where the subject slows or pauses, so the next prompt
  starts from a natural rest state. A join in mid-motion is where drift shows.
- Experimental `state_ledger` (text, up to 8000 chars) is put in front of every segment prompt.
  Use it to restate what must not change (character, clothing, place, light). Its effect is not
  measured; treat it as a prompt aid, not a guarantee.
- Past about 40 s every system drifts (backgrounds first). Finish and cut, or start a new video.

#### Long video with MiniMax H3

The same three routes work with `model: minimax-h3` (also `h3`, `minimax-h3-i2v`, `h3-long`,
`minimax-h3-long`): `shots[]`, `seconds` above 10 s, and `POST /v1/videos/extensions`. The
resource, the cap, the failure error and the progress are the same as for LTX. What differs:

- Native audio. Each segment makes its own sound; the sound of the last second is pinned to the
  next segment, so the soundtrack continues instead of restarting. Put sound cues after an
  `Audio:` paragraph in each shot prompt, as for a single H3 clip.
- Output is 960x544 by default (up to 1344x768, multiples of 32), 24 fps. A segment is at most
  10 s (`chain_segment_seconds` on the workflow). Frames are on the `17k + 5` grid (first
  segment) and grow in steps of 17 (added segments), so lengths can differ from the request by
  up to about 0.4 s (`X-Duration-Adjusted`). A shot is at least 2 s.
- Continuity inside one request is lossless: the tail of the previous segment's latent (22
  frames and the last second of sound) is pinned to the head of the next one, with no decode and
  no re-encode. When you extend an existing video there is no latent, so the decoded last
  frames and audio are encoded once; that one join is lossy, the joins after it are not.
- `context_seconds` snaps down to 5, 22, 39 or 56 frames (0.2, 0.9, 1.6 or 2.3 s). The default
  of 1 s is 22 frames. The pinned head is cut from the result, so it costs render time but no
  length. `context_strength` does not exist for H3 (the pinned rows are never denoised); sending
  it returns `400 invalid_workflow_extension`. `negative_prompt` is ignored, as for single H3 clips.
- Extending: a video made by H3 is extended by H3 when you send no `model`; a video made by
  another model keeps the default extender (`ltx-2.5-long`). The source must be 24 fps
  (`400 video_fps_unsupported` otherwise).
- Write each shot so it opens by holding the closing framing of the shot before it for about
  two seconds (give the held subject a small motion, a breath or a glance), then makes the
  change. A prompt that opens on a different arrangement of people renders as a sum of both.
  The model has no memory beyond the pinned second: restate what must not change in the prompt
  or in `state_ledger`.
- Measured on the 3090 (2026-10-02, 960x544): 5 s + 5 s direct 430 s (cold load); a two-shot
  20 s storyboard through the API 965 s (about 7 min per 10 s segment, the second segment is
  longer because 22 context frames are sampled too); a 10 s extension 572 s. Peak GPU0 22.4 GB
  of 24.5; host RAM is tight (about 108 of 124 GB used while rendering). No frame-difference jump
  at the joins; the extension join (the lossy path) shows a small bump. Run under `bg_run`.
- License: the MiniMax H3 Community License excludes use in the US, EU, UK and South Korea
  (accepted for this personal stack).
- Needs the `ComfyUI-H3-Motion-Context` custom node (pinned commit in `docs/long-video-design.md`).

### Remix and character replace

Two workflows take a source video as input. Both need the Wan models and
ComfyUI nodes on this host (`video-bernini-r-remix`, `video-scail-2-replace`).

**Remix** follows OpenAI's API. `POST /v1/videos/{video_id}/remix` takes
`{"prompt": ...}` and returns a new video job with `remixed_from_video_id` set
to the source. The source must be a completed video of the same API key. It runs
Bernini-R (Wan 2.2 14B, Q4_K_M). The edit is approximate, not frame-exact: use
prompts like "Replace the beach with a snowy mountain. Keep the dog and its
motion unchanged."

```bash
curl -s -X POST "http://127.0.0.1:5000/v1/videos/$VIDEO_ID/remix" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "Replace the beach with a snowy mountain at golden hour. Keep the dog unchanged."}'
```

```python
video = client.videos.remix(video_id, prompt="Make it snowy. Keep the dog unchanged.")
video = client.videos.poll(video.id)
client.videos.download_content(video.id).write_to_file("remix.mp4")
```

To remix a clip that is not a gateway video, use OpenAI's edit route,
`POST /v1/videos/edits`: `client.videos.edit(prompt=..., video=open("clip.mp4", "rb"))`.
Over JSON, `video` can also be `{"id": "video_..."}` (this sets
`remixed_from_video_id`). An upload gives `remixed_from_video_id: null`. The same
upload also works on `POST /v1/videos` with `model: "video-remix"` and
`extensions.com.vividfish.workflow.source_video` set to a video id, an http(s) URL
or a `data:video/mp4;base64,...` URI.

Optional gateway fields on remix and edit: `seed`, `seconds`, `size`, `negative_prompt`,
and `model`. Without `model` the resource reports `"model": "video-remix"` (OpenAI reports
the source's model); `vivid_fish_resolved_model` is always `video-bernini-r-remix`.
Output keeps the source frame rate and audio. `seconds` defaults to the source length, up to
3.4 s, and may not exceed the source length; the maximum is 4 s, 832x480 (4k+1 frames).
Measured on the 3090: 81 frames at 832x480 = 462 s warm.

**Character replace** has no OpenAI equivalent, so it is a gateway workflow on
`POST /v1/videos` with `model: "character-replace"` (also `scail-2`). A reference
character image takes over the person in a driving video, with the driving
video's motion and scene (SCAIL-2, Q4_K_M, SAM 3.1 tracking). Inputs go in
`extensions.com.vividfish.workflow`:

| Input | Required | Meaning |
|---|---|---|
| `source_video` | yes | The driving video: `{"video_id": "video_..."}` (or `"video_..."`), an http(s) URL, or a `data:video/*` URI. |
| `reference_image` | yes | The character: a data URI, URL, base64, or `{"asset_id": "img_..."}`. |
| `source_subject` | no | SAM 3 text prompt for the person to replace in the video. Default `person`. |
| `reference_subject` | no | SAM 3 text prompt for the character in the image. Default `person`. |
| `subject_index` | no | Which tracked person to replace. SAM 3 tracks every match (background people too); they are ordered by size in the first frame they appear: `0` (default) is the largest, `1` the next. A list such as `0,2` replaces several only if you also give a multi-person reference; leave it at one index. |
| `source_point_x`, `source_point_y` | no | A point on the person to replace in the FIRST frame, as 0-1 fractions from the top left. SAM 3 segments that point and tracks only that person. Most reliable when several people are in the clip; it overrides `subject_index`. |
| `reference_fit` | no | Default `true`. Cuts the character out of the full reference image, scales it to the height of the replaced person in the first frame and places it at that person's position on a grey canvas, then uses that as the reference. Set `false` to send the image as is (it is then centre-cropped to 832x448). |

`prompt` describes the character in the scene. One 81-frame segment is rendered
(832x448, about 3.4 s at 24 fps); longer driving videos use their first 81 frames and a
source with fewer frames gives a shorter clip. Measured on the 3090: 303 s warm.
Only one person is replaced. With several people in the clip, name the person by position
(`source_point_x`/`source_point_y`) or by order (`subject_index`); text names such as "man in gray hoodie"
still track every match and the largest one wins. When more than one person was tracked and you did
not choose, the finished video has `vivid_fish_warnings` saying how many were tracked and how to choose.
Measured on a clip with two men and a crowd: the old graph (reference centre-cropped, all tracked people
masked) drew a large extra woman and left the man; a point on the man plus `reference_fit` replaced him
and kept the other man. A reference with the whole character in frame (head to feet) works best.

```python
import base64
ref = "data:image/png;base64," + base64.b64encode(open("character.png", "rb").read()).decode()
video = client.videos.create_and_poll(
    model="character-replace",
    prompt="A young woman in a bright red raincoat walks toward the camera, waving.",
    extra_body={"extensions": {"com.vividfish.workflow": {
        "source_video": {"video_id": driving_video.id},
        "reference_image": ref,
        "source_subject": "man in gray hoodie",
        "reference_subject": "woman",
    }}},
)
```

The OpenAI Python SDK sends `extra_body` on `videos.create` as bracketed multipart
fields (`extensions[com.vividfish.workflow][source_video][video_id]`); the gateway
rebuilds them, and converts numeric strings to the manifest's types.

Both jobs use the normal video resource: `queued` to `in_progress` to `completed`
or `failed`, `GET /v1/videos/{id}/content` returns 404 `video_not_ready` until done.
`model` echoes what you sent. Errors: 400 for a missing or unreadable source, a missing
reference image, or `seconds` longer than the source; 404 for a video id that is not yours;
501 `video_remix_not_configured` if `videos.remix_workflow` is unset.

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
  content and use the gateway's normal vision routing. `document` blocks and
  server tools (web search, code execution, computer use) are rejected with a
  400 instead of being faked. Prompt-caching markers (`cache_control`) and
  `anthropic-beta` headers are accepted and ignored.
- Ollama read-only model routes are implemented: `/api/tags`, `/api/show`,
  `/api/ps`, and `/api/version`. Mutating routes (`/api/pull`, `/api/push`,
  `/api/copy`, `/api/delete`, `/api/create`) return an Ollama-shaped
  unsupported-operation error (501) because the gateway does not mutate local
  model files through this protocol surface.
- Ollama sampling options (`temperature`, `top_p`, `top_k`, `min_p`,
  `typical_p`, `repeat_penalty`, `repeat_last_n`, `mirostat*`, `num_predict`,
  `seed`, `stop`, `presence_penalty`, `frequency_penalty`) reach the backend.
  Options that size a local runtime (`num_ctx`, `num_gpu`, `num_thread`,
  `use_mmap`, and similar) and unknown options are accepted and ignored, as
  Ollama ignores options it cannot apply. The context size is fixed by the
  backend; read it from `/api/show`.

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

## Recipe: Rerank (Open WebUI, LlamaIndex, langchain-cohere, Dify, Continue)

`POST /v1/rerank` takes the Cohere and Jina body and returns the same results list
every one of those clients reads. `/rerank` and `/v2/rerank` are the same engine under
the other common paths; `/v2/rerank` does not echo documents unless you ask.

```bash
curl -s http://127.0.0.1:5000/v1/rerank \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"rerank","query":"capital of France","top_n":2,
       "documents":["Paris is the capital of France.","Bananas are yellow.","Lyon is a city in France."]}'
# {"results":[{"index":0,"relevance_score":0.99,...},{"index":2,...}], "data":[...same...], ...}
```

Cohere's own SDK works with a base URL override (checked with `cohere` 7.2.0):

```python
import cohere
co = cohere.ClientV2(api_key=KEY, base_url="http://127.0.0.1:5000")   # posts to /v2/rerank
r = co.rerank(model="rerank", query=q, documents=docs, top_n=3)
# v1 client: cohere.Client(api_key=KEY, base_url="http://127.0.0.1:5000") posts to /v1/rerank
```

Settings in the other clients (field names from each client's rerank settings; these clients were not run
against the gateway here, the request shape they send is the one above):

- **Open WebUI:** Admin Settings, Documents, Reranking Engine `External`; URL
  `http://127.0.0.1:5000/v1/rerank`; key `$OPENAI_PROXY_API_KEY`; model `rerank`.
- **LlamaIndex / langchain-cohere:** the Cohere rerank class with `base_url` set to
  `http://127.0.0.1:5000` and any `rerank*` model name.
- **Dify, Continue:** a Cohere-style or Jina-style rerank provider; endpoint
  `http://127.0.0.1:5000/v1/rerank`.

Notes:

- The model name does not choose a model. Stable name `rerank` and every name that contains `rerank`
  (`rerank-v3.5`, `rerank-english-v3.0`, `jina-reranker-v2-base-multilingual`, `bge-reranker-v2-m3`) reach the one engine;
  any other name is a 404 `model_not_found`. See `model_aliases.rerank` in `proxy_config.yml`.
- The engine is a CPU `llama-server --rerank` unit (`rerank-cpu.service`, see `docs/ops/rerank-cpu.service`).
  It is not running when idle. The first request after a pause starts it and waits for it (a few seconds);
  it exits after 10 minutes without a request.
- Limits: 1000 documents, 10 000 characters per document. Longer input is a 400, not silently cut.
- Scores are model scores in 0..1. They compare documents within one request. Do not reuse a threshold from
  another reranker.
- A key may rerank when its profile allows `text.embeddings`.

## Recipe: OpenAI Realtime (voice agents)

`/v1/realtime` is the OpenAI Realtime WebSocket. The gateway does not run the protocol. It checks the key and
relays frames to a sibling service, Hugging Face `speech-to-speech` (voice activity detection, turn detection,
barge-in, tools), which in turn calls this gateway's own routes:

| Slot | Gateway call | Setting |
|---|---|---|
| Hear | `POST /v1/audio/transcriptions` | model `stt-fast` |
| Think | `POST /v1/chat/completions` | model `current`, `reasoning_effort: none` (thinking off), streamed |
| Speak | `POST /v1/audio/speech` | model `tts-fast` (Supertonic), `response_format=pcm`; `tts-1` (Chatterbox) is a drop-in option |

Silero VAD and Smart Turn run on the CPU. The service uses no GPU and loads no model of its own.

```python
from openai import OpenAI
client = OpenAI(api_key=KEY, base_url="http://127.0.0.1:5000/v1", websocket_base_url="ws://127.0.0.1:5000/v1")
with client.realtime.connect(model="gpt-realtime") as conn:     # the model name is ignored
    conn.session.update(session={"type": "realtime", "instructions": "Answer in one short sentence.",
        "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": {"type": "server_vad"}},
                  "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": "alloy"}}})
    # send 24 kHz mono PCM16 with conn.input_audio_buffer.append(audio=<base64>) in 20 ms chunks;
    # read response.output_audio.delta events. Send a little silence after speech so the VAD can end the turn.
```

`@openai/agents` works too: `new RealtimeSession(agent, {transport: new OpenAIRealtimeWebSocket(), model: "gpt-realtime"})`
and `session.connect({apiKey: KEY, url: "ws://127.0.0.1:5000/v1/realtime?model=gpt-realtime"})`. Function tools
run in the SDK as usual.

Notes:

- Auth is the one of the other routes: a trusted LAN client needs no key; otherwise send `Authorization: Bearer`,
  `?api_key=`, or (browsers) the `openai-insecure-api-key.<key>` subprotocol. A refused upgrade is a plain HTTP 401
  or 403. The key's profile must allow transcription, chat and speech, because a voice session uses all three.
- Real numbers, 5 voice turns (2026-10-03): end of speech to first audio 1.5 to 2.0 s (median 1.6 s). That is
  speech-to-text 0.2 to 0.8 s, then the model's first sentence 0.7 to 1.5 s (time to first token about 0.33 s), then
  Supertonic's first audio 0.26 to 0.30 s.
- The language model has one slot, shared with every other client. A voice session takes it for each turn.
  Measured effect on a 95 000-token prefix held by another client: the next request from that client still reused
  the whole prefix and paid about 0.8 s extra to swap it back in (no re-processing).
- The service holds 2 sessions at once (`REALTIME_POOL_SIZE`). A third gets an `error` event.
- The first connection after the service was down can take longer than 10 s (it warms its three slots, about 16 s),
  and the Python SDK gives up at 10 s. Keep the unit enabled so it is already running.
- Not provided: WebRTC (`/v1/realtime/calls`), `client_secrets`, SIP. `conversation.item.truncate` is accepted and has
  no effect.

Deployment: `docs/ops/speech-to-speech.service` (install in `~/.config/systemd/user/`, then
`systemctl --user enable --now speech-to-speech`). The service lives in `~/applications/speech-to-speech`
(`run.sh` holds all flags; the package is pinned to release tag `v1.0.0` in a uv venv with CPU torch). After the
first deploy, add the backend to the DB-owned registry:
`./venv/bin/python scripts/local/register_service_from_config.py realtime --apply`.

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
  Anthropic-specific request headers are ignored at this surface.

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
| `/ollama/api/pull`, `/ollama/api/push`, `/ollama/api/copy`, `/ollama/api/delete`, `/ollama/api/create` | Registered, but returns unsupported-operation (501). |

Point the Ollama base URL at `http://127.0.0.1:5000/ollama` when the client
supports a pathful base URL. For clients hard-coded to `/api`, use
`http://127.0.0.1:5000` and the convenience `/api` routes. Send
`Authorization: Bearer $AI_GATEWAY_API_KEY` (`x-api-key` also works, but the
Ollama clients only send a bearer header); this is not a vanilla
unauthenticated Ollama daemon. Clients on the LAN bypass list need no key.
The official `ollama` Python client takes the key as
`ollama.Client(host=..., headers={"Authorization": "Bearer ..."})`.

What is verified with the official `ollama` Python client (0.6.3) and the
`docs/api.md` schemas pinned in `tests/fixtures/ollama-api-schemas.json`:

| Feature | Status |
|---|---|
| `chat`, `generate`, streaming and non-streaming | Works. `stream` defaults to true, as in Ollama. |
| Tools (`tools`, assistant `tool_calls`, `role: tool` with `tool_name`) | Works. A streamed tool call arrives whole in one object. |
| `images` (base64 or raw bytes) | Works; every local chat model has vision. |
| `format: "json"` and JSON-schema `format` | Works. |
| `think: true` or `"low"/"medium"/"high"` | Works; reasoning returns in `message.thinking` (`thinking` for generate). Without `think` it is not returned. |
| Final-object stats | `prompt_eval_count`, `eval_count`, and nanosecond durations are filled from backend timings. `load_duration` is always 0. |
| Empty `messages` / empty `prompt` | Loads or, with `keep_alive: 0`, unloads. The gateway only checks that the model exists; it does not warm or free anything. |
| `embed`, legacy `embeddings` | Works. `truncate`, `keep_alive`, `options` are ignored; `dimensions` is forwarded. |
| `list` / `tags`, `show`, `ps`, `version` | Works. `show` reports `completion`, `vision`, `tools`, `thinking` (`embedding` for embedding models). `version` is a semver with a `+vivid-fish` suffix. |
| Unknown model | 404 `model "x" not found, try pulling it first`. `name:latest` is the same model as `name`. |
| `suffix` (fill-in-the-middle), `template`, `raw: true` | Rejected with 400. The chat-completions pipeline cannot honor them. |
| `context` (deprecated), `keep_alive`, `logprobs` | Ignored. |
| `pull`, `push`, `create`, `copy`, `delete` | 501. Manage models in the backend. |
| Digests, sizes, `modified_at`, `families` | Placeholders (`""`, `0`, a fixed date); the gateway does not own model files. |

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

The Anthropic Messages surface (`POST /anthropic/v1/messages`) is a translator
over the canonical chat-completions pipeline, including Anthropic-shaped SSE
streaming. It is verified with the official `anthropic` Python SDK (1.11.0) and
with Claude Code 2.1.286, and its responses and stream events are checked
against schemas generated from the SDK
(`tests/fixtures/anthropic-sdk-schemas.json`).

```python
import os
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:5000/anthropic",
    api_key=os.environ["AI_GATEWAY_API_KEY"],  # sent as x-api-key
)

msg = client.messages.create(
    model="default",
    max_tokens=256,
    messages=[{"role": "user", "content": "ping"}],
)
print(msg.content[0].text)
```

Auth: the SDK's `api_key` is sent as `x-api-key`; `auth_token=` (bearer) works
too. Use either. Clients on the LAN bypass list need no key.

Claude Code: set `ANTHROPIC_BASE_URL=http://127.0.0.1:5000`,
`ANTHROPIC_AUTH_TOKEN=$AI_GATEWAY_API_KEY`, `ANTHROPIC_MODEL=default` and
`ANTHROPIC_SMALL_FAST_MODEL=default`. Claude Code then runs its tool loop
(Read, Bash, and so on) against the local model. Claude Code prints
`unrecognized_model` for local model names; that is only a cost-table warning.
With the base URL at the gateway root, `/v1/models` answers in Anthropic shape
when the request carries the `anthropic-version` header, and in OpenAI shape
otherwise.

| Feature | Status |
|---|---|
| `messages.create`, `messages.stream` (event accumulation) | Works. |
| System prompt as string or text blocks; multi-turn | Works. `system`-role entries inside `messages` (sent by Claude Code) are merged into the system prompt. |
| Tools: `tool_use` / `tool_result` round trip, `tool_choice` `auto`/`any`/`tool`/`none`, `disable_parallel_tool_use` | Works, including streamed `input_json_delta`. |
| Images (base64 and URL blocks) | Works. |
| `thinking` `enabled` / `adaptive` | Works. Local reasoning returns as `thinking` blocks (signature is empty); `display: "omitted"` returns none. `budget_tokens` and `output_config.effort` choose a `reasoning_effort`. Without `thinking`, reasoning is never returned as text. Prior `thinking` blocks in history are dropped. |
| `stop_reason` | `end_turn`, `max_tokens`, `tool_use`. `stop_sequence` is never reported: the backend does not say which sequence matched, so `stop_sequences` stop generation but the reason is `end_turn` and `stop_sequence` is null. A stop sequence can also match inside the model's reasoning. |
| `usage` | `input_tokens` and `output_tokens` from the backend. Cache fields are absent. Streams report usage in `message_delta`. |
| `count_tokens` | Estimate (about 4 characters per token); header `X-AI-Gateway-Token-Counting: estimated`. |
| `models.list`, `models.retrieve` | Works at `/anthropic/v1/models[/{id}]`. `display_name` is the model description or a title-cased id. |
| Errors | Anthropic shape with a `request-id` header: 400 `invalid_request_error`, 401 `authentication_error`, 403 `permission_error`, 404 `not_found_error` (unknown model: `model: <name>`), 429 `rate_limit_error`, 5xx `api_error`. |
| `top_k` | Forwarded to the backend. `temperature`, `top_p`, `metadata.user_id` map as usual. In SDK 1.11 `temperature` is no longer a typed argument; pass it with `extra_body`. |
| `document` blocks, server tools, `web_search`, computer use, citations, batches, files, `container`, `service_tier`, `cache_control` effects | Unsupported. The first two are rejected with 400; the rest are ignored or absent. |

Known gap: the `request-id` response header is generated by the gateway and is
not linked to a receipt.

## Recipe: OpenAI Responses API

`POST /v1/responses` (also `/openai/v1/responses`) translates onto the chat
pipeline. It is verified with the official `openai` Python SDK (3.23.0), and
its objects and every streamed event are checked against the `Response` and
`ResponseStreamEvent` schemas of openai/openai-openapi commit `145fd3b`
(`tests/test_openai_responses_conformance.py`).

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:5000/v1", api_key=KEY)
first = client.responses.create(model="default", input="Remember the word mango.")
second = client.responses.create(
    model="default",
    input="What word did I ask you to remember?",
    previous_response_id=first.id,
)
print(second.output_text)
```

- Works: `create` (string or item-list `input`, `instructions`), streaming,
  function tools and `function_call_output` items, `previous_response_id`
  chains, `retrieve`, `delete`, `input_items.list`, `input_image` content,
  `text.format` (JSON schema), `max_output_tokens` (status `incomplete`), and
  `reasoning.effort`.
- Reasoning: local reasoning returns as a `reasoning` output item (and as
  `response.reasoning_text.*` events). `reasoning.summary` is accepted but no
  summary is produced.
- Stored responses (`store` defaults to true) last 7 days and belong to the
  key that created them. Another key gets 404 on retrieve, delete, or as a
  `previous_response_id`. An unknown or expired `previous_response_id` is a
  404 `previous_response_not_found`; the gateway does not continue without the
  context.
- Unsupported, with an explicit 400: hosted tools (`web_search`,
  `file_search`, `code_interpreter`, `computer_use`, ...), `background`,
  `conversation`, `prompt`, `POST /v1/responses/{id}/cancel`, and
  `POST /v1/responses/input_tokens`.
- An unknown model is a 404 `model_not_found`.

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

## Recipe: MCP clients (Claude Code, Pi, Cursor, Codex)

The gateway is an MCP server at `/mcp` (Streamable HTTP). It has three tools, so an
agent pays about 600 tokens once and gets every workflow:

| Tool | Use |
|---|---|
| `capabilities` | List workflows, aliases (`image`, `video`, `music`, `sfx`, ...) and `speech`. With `target`, return the typed inputs, outputs and expected run time. |
| `run` | Start a `target` with `inputs`. Fast work returns the result. Slow work returns a `job_id` after `wait_seconds` (default 30, at most 120). |
| `job` | Poll a `job_id`, wait for it (`wait_seconds`), or `cancel` it. |

Media comes back as MCP `resource_link` blocks that point at signed asset URLs, and
as `structuredContent.assets[]` with the `asset_id`. Pass an `asset_id` or a data URI
as an image input to chain an edit. The adapter does not fetch external URLs.

The tools are generated from `x-vividfish-workflows` and the stable aliases, so a
new workflow appears with no code change. Every action is dispatched to the same
core route a plain HTTP client would call, with your key, so profile policy and
receipts apply. A policy denial comes back as a tool error that carries the HTTP status.

Auth is the same bearer key. A request without a key gets `401` with
`WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/mcp"`.
The gateway has no OAuth server, so give the client the key as a header.

Protocol: revision 2026-07-28 (stateless, `server/discover`) and the handshake
revisions 2025-03-26 to 2025-11-25. The server never issues `Mcp-Session-Id`;
`GET` and `DELETE` on `/mcp` return 405.

**Claude Code:**

```bash
claude mcp add gateway https://ai.example.com/mcp --transport http \
  -H 'Authorization: Bearer ${AI_GATEWAY_API_KEY}'
```

**Pi (`pi-mcp-adapter`)**, in `.mcp.json` or `~/.config/mcp/mcp.json`:

```json
{ "mcpServers": { "gateway": { "url": "https://ai.example.com/mcp",
    "headers": { "Authorization": "Bearer ${AI_GATEWAY_API_KEY}" },
    "protocolVersion": "auto" } } }
```

`protocolVersion` may be `legacy` (the adapter default), `auto` or `2026-07-28`; all three work.

**Smoke test without a client:**

```bash
curl -s https://ai.example.com/mcp -H "Authorization: Bearer $AI_GATEWAY_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"run","arguments":{"target":"tts-fast","inputs":{"input":"Hello."}}}}'
```

## Recipe: SillyTavern image generation (A1111 source)

SillyTavern's `openai` image source is hard-coded to api.openai.com. Its "SD WebUI
(AUTOMATIC1111)" source works, because the gateway serves the `/sdapi/v1` routes
SillyTavern calls at host root.

**Extensions → Image Generation → Source: Stable Diffusion Web UI (AUTOMATIC1111):**

```text
SD WebUI URL:    http://127.0.0.1:5000        (host root, no path)
Authentication:  gateway:$AI_GATEWAY_API_KEY  (user:password; the password is the key)
```

Click Connect, then pick a model. The model list holds the stable image aliases
(`image`, `image-fast`, `image-uncensored`, `image-anime`) first, then each image workflow. The chosen model is
remembered per API key until the gateway restarts; the default is `image`.

What maps and what does not:

- `prompt`, `width`, `height`, `steps`, `seed`, `batch_size` and `n_iter` map onto the
  chosen workflow. `cfg_scale`, `negative_prompt`, `sampler_name` and `scheduler` map
  only when that workflow has the input (for example `flux2-klein-4b` takes
  `guidance`; `krea2-turbo` takes none of them). Otherwise the response header
  `x-gateway-ignored-params` names what was dropped. `steps` and `cfg_scale` clamp
  to the workflow's range and say so in the same header.
- A size outside the workflow's range is a 422, not a silent resize.
- Hires fix (`enable_hr`), face restoration, ADetailer and other scripts, styles,
  variation seeds, VAE choice, and LoRA tags in the prompt are rejected with a 422
  that names the field. Leave "Hires fix", "Restore faces" and "ADetailer" off in
  SillyTavern.
- If you send no seed, the gateway picks one and reports it in `info.seed`.
- `img2img`, extras upscaling, `interrogate` and `png-info` return 501.
- `interrupt` cancels the request in flight for that key.

SillyTavern's generate call waits for the whole image, so a workflow with a long
`typical_seconds` (see `capabilities`) makes it wait that long.

## Model aliases (stable names)

Clients never need internal model names. Two layers, both defined in the
`model_aliases:` section of `proxy_config.yml` (the one place to re-point
anything; code in `gateway/stable_aliases.py`):

1. **Stable names** (ours; never change meaning): the names to put in new code.
2. **OpenAI names** (`gpt-4o`, `tts-1`, `gpt-image-1`, ...): ordered globs that
   point at a stable name, so they follow it when it is re-pointed.

Concrete names (roles such as `coder-q5`, workflow ids such as `flux2-klein-4b`,
engine ids such as `omnivoice`) always keep working, so a client can still pin
an exact model or settings. Unknown names pass through unchanged.

| API | Stable name | Resolves to today | OpenAI names that map to it |
|---|---|---|---|
| Chat / completions | `default` | Qwen3.8-27B, reasoning on (also does vision) | `gpt-5*`, `gpt-4.1`, `gpt-4o`, `gpt-4`, `o1*`, `o3*`, `o4*`, `chatgpt-*`, `claude-*` |
| | `fast` | `default-fast` (same weights, reasoning off) | `gpt-*-mini*`, `gpt-*-nano*`, `o*-mini*`, `gpt-3.5*`, `claude-*haiku*` |
| | `vision` | `default` | |
| | `coder` | `coder` (coding profile) | `gpt-*-codex*`, `codex-*` |
| | `rp` | `rp` (roleplay) | |
| Embeddings | `embed` | `nomic-embed-text-v1.5` (768-dim; not vector-compatible with OpenAI's) | `text-embedding-*` |
| Image generate | `image` | `krea2-turbo` | `gpt-image-*`, `chatgpt-image-*`, `dall-e-*` |
| | `image-fast` | `flux2-klein-4b` | |
| | `image-uncensored` | `krea2-turbo-snofs` (Krea 2 Turbo + SNOFS v1.4 LoRA; explicit output for adults, opt-in) | none |
| | `image-anime` | `anima-turbo` (Anima Turbo v1.1, anime, uncensored) | none |
| Image edit / variations | `image-edit` (also `image`) | `qwen-image-2.1-edit`; masks and 2+ images switch to inpaint / multiref automatically | `gpt-image-*`, `dall-e-*` |
| | `image-edit-fast` (also `image-fast`) | `flux2-klein-4b-edit` | |
| Video | `video` | `video-ltx-2.5-t2v`; `input_reference` switches to image-to-video | `sora-2`, `sora-2-pro`, `sora*` |
| | `video-fast` | `video-ltx-2.5-t2v` (same graph as `video`) | |
| | `video-long` | `video-ltx-2.5-long` (code-built chain, up to `videos.max_total_seconds`, default 40 s) | |
| | `h3-long`, `minimax-h3-long` | `video-minimax-h3-long` (code-built H3 chain, same cap; `minimax-h3` itself also chains past 10 s) | |
| | `video-remix` | `video-bernini-r-remix`; needs `extensions...source_video`; also what `/v1/videos/{id}/remix` runs | |
| | `character-replace` | `video-scail-2-replace`; needs `extensions...source_video` and `reference_image` | |
| Rerank | `rerank` | Qwen3-Reranker-0.6B Q5_K_M (CPU, on demand) | any name containing `rerank` |
| TTS | `tts` | Chatterbox Turbo (GPU0, on demand), then OmniVoice. The 13 OpenAI voice names are fixed cloned identities, and `voice: {"id": "voice_..."}` selects a user-created voice | `tts-1`, `gpt-4o-mini-tts`, `gpt-4o-tts`, `tts-*` |
| | `tts-fast` | Supertonic (audio.cpp, GPU0; preset voices), then Chatterbox, then OmniVoice | none (point `tts-1` here to make it the fast tier) |
| | `tts-quality` | Chatterbox Turbo, then OmniVoice, then Kokoro | `tts-1-hd` |
| STT | `stt` | Parakeet TDT 0.6B v3 on GPU0 (audio.cpp, on demand), then the CPU Parakeet if GPU0 is full | `whisper*`, `parakeet-*` |
| | `stt-fast` | Nemotron streaming (audio.cpp, GPU0), then Parakeet | `gpt-4o-mini-transcribe*` |
| | `stt-quality` | Parakeet on GPU0, then CPU Parakeet, then Canary 180M Flash | `gpt-4o-transcribe*` |
| | `stt-diarize` | Nemotron 3 Diarization (CPU) for speakers + Parakeet for words; no fallback engine | `gpt-4o-transcribe-diarize*`, `*transcribe-diarize*` |
| Translations | -- | not supported by the local STT backend (clear 400) | |
| Music / SFX | `music` | `ace-step-1.5-xl-turbo` | |
| | `song` | `yue2-song` (vocals; license CC BY-NC) | |
| | `sfx` | `sa3-small-sfx` | |

TTS and STT aliases resolve through **engines**: each tier lists engines in
order and the first one whose service is deployed wins. Engine ids
(`chatterbox`, `omnivoice`, `supertonic`, `kokoro`, `parakeet-gpu`, `parakeet`, `nemotron-streaming`,
`canary`, `nemotron-diarize`) are valid `model` values too; an engine that is not deployed returns
`400 model_not_deployed`. Chatterbox Turbo and OmniVoice clone the 13 OpenAI voice
names from the same fixed reference clips (the nine public OpenAI sample clips for
alloy, ash, coral, echo, fable, nova, onyx, sage and shimmer; designed clips for ballad,
cedar, marin and verse, which OpenAI publishes no sample for). Supertonic and Kokoro
map OpenAI voice names to the closest preset voice (`voice_map`; an engine-native
voice name is passed through). The tier order is a policy choice: Chatterbox won the
blind listening test 7/7, so it leads `tts` and `tts-quality`, and OmniVoice is the fallback.

**Fallback.** STT and TTS tiers try the tier's next engine (`whisper-1` -> Parakeet on GPU, then the
always-on CPU Parakeet) when the first engine cannot serve the request:

| Reason in the header | Trigger |
| --- | --- |
| `start_failed` | the unit would not start (GPU0 full, unit failed) |
| `connection_refused` | the engine refused the connection (unit stopped after the health check) |
| `busy` | the engine answered `503` (still loading, GPU0 busy) |
| `gpu_oom` | the engine answered `500`/`502`/`507` with an allocation or out-of-memory message |

A `4xx`, bad audio, a timeout and any other `5xx` never fall back, and a name that is an engine id
(`model=parakeet-gpu`) never falls back. Each fallback is visible: the response carries
`x-vivid-fallback: parakeet-gpu->parakeet (gpu_oom)` (comma-joined when there are several), the gateway logs
it, and for STT the serving receipt lists it in `fallback_reasons`. When every engine fails, the last
engine's error is returned with the header. A TTS fallback engine applies its own voice map, so the voice
can differ; a custom voice only falls back to engines that accept one. `GET /gateway/v1/aliases/resolve`
lists `fallback_engines`.

**Starting engines.** An engine whose service has a `systemd` lifecycle is
started on demand (one health check, then `systemctl --user start` and a health
poll). `chatterbox-tts`, `omnivoice-tts`, `supertonic-tts`, `kokoro-tts`, `nemotron-stt`, `canary-stt` and `diarize-stt` are
operator-owned user units; the gateway only starts them. Chatterbox frees its weights after 10 min idle and exits after
1 h; OmniVoice exits after 5 min idle. If GPU0 is full (ComfyUI is rendering), Chatterbox answers
`503` with `Retry-After` instead of failing mid-request.

**Streaming speech.** For engines marked `streaming: true`,
`response_format: "pcm"` streams chunked 24 kHz s16le audio while the engine is
still speaking; add `stream_format: "audio"` to stream `mp3`/`opus`/`aac`/`flac`/`wav`
too, or `stream_format: "sse"` for `speech.audio.delta` events. Other engines
answer once (an SSE request gets one delta). `GET /v1/audio/voices` lists each
deployed engine's voices under `engines`.

### Custom voices

Users can create their own voices and use them with the speech route. The request
and response shapes follow OpenAI's public spec (`POST /v1/audio/voices`,
`/v1/audio/voice_consents`, and `voice: {"id": "voice_..."}` on `/v1/audio/speech`),
so the stock `openai` SDK works. OpenAI's spec has no list, get or delete for voices;
those and the rename and sample routes are our additions.

```bash
# 1. Optional: record a consent statement (required when voices.consent_mode is "required")
curl $BASE/audio/voice_consents -H "Authorization: Bearer $KEY" \
  -F name="Jo Doe" -F language=en-US -F recording=@consent.wav          # -> {"id": "cons_...", ...}
# 2. Create the voice from a 5-60 s sample (best 8-15 s of clear speech)
curl $BASE/audio/voices -H "Authorization: Bearer $KEY" \
  -F name="My narrator" -F audio_sample=@me.wav -F consent=cons_...      # -> {"object": "audio.voice", "id": "voice_...", ...}
# 3. Speak with it
curl $BASE/audio/speech -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"tts","voice":{"id":"voice_..."},"input":"Hello there."}' -o out.wav
```

| Route | Notes |
|---|---|
| `POST /v1/audio/voices` | multipart: `name`, `audio_sample` (file, <= 10 MiB; wav, mp3, flac, ogg, aac, webm, m4a), `consent` (id), `type` (`audio_sample`, default). Extras: `description`, `labels` (JSON object, <= 10), `remove_background_noise` (default false). |
| `POST /v1/audio/voices` with `type: "prompt"` | JSON or multipart: `name`, `prompt` (1-2000 chars), `script_hint`, `model` (`auto`), extra `seed`. OmniVoice designs from a fixed vocabulary, not free text: the gateway reads the description for gender (male, female), age (child, teenager, young adult, middle-aged, elderly), pitch (very low, low, moderate, high, very high), `whisper` and an accent (american, british, australian, canadian, indian, chinese, japanese, korean, portuguese, russian), lists the words it could not use in `warnings` (`design_terms_ignored: ...`), and returns the attributes it applied as `design_instruct`. A description with none of them is `400 invalid_prompt`. OmniVoice renders the script once and the gateway keeps that clip, so the voice then works on every cloning engine (OpenAI limits prompt voices to its Live API). |
| `GET /v1/audio/voices` | Built-in list as before, plus `object: "list"`, `data` (your custom voices, newest first), `first_id`, `last_id`, `has_more`; `after` and `limit` (1-100). |
| `GET/POST/DELETE /v1/audio/voices/{id}` | Get; rename or relabel (JSON `name`, `description`, `labels`); delete (`{"id", "object": "audio.voice", "deleted": true}`). |
| `GET /v1/audio/voices/{id}/sample` | The stored reference clip (`audio/wav`), to hear what the clone is built from. |
| `/v1/audio/voice_consents[/{id}]` | OpenAI's consent CRUD: multipart `name`, `recording`, `language` (BCP 47); `POST` with JSON `{name}` renames. The gateway stores the recording; it does not verify the spoken phrase. |

The voice object has OpenAI's fields (`object`, `id`, `type`, `name`, `created_at`) and these additions:
`description`, `labels`, `duration_seconds`, `consent`, `seed`, `prompt`, `warnings`,
`compatible_engines`, and `quality` (`speaker_similarity`, `encoder`, `engine`, `status`).

What the gateway does with a sample: decodes it with ffmpeg, rejects it when it is under 5 s,
over 60 s, has under 3.5 s of speech, or is not audio; trims silence; keeps the best 12 s window
of a longer recording; levels it to -20 dBFS; and stores a 24 kHz mono WAV. It warns on clipping,
a low signal-to-noise ratio and long silences. Denoising is off unless you ask for it, because it can
make clean audio worse. After creation the clone engine speaks a test sentence from the clip and the
gateway reports the ECAPA speaker similarity: `status` is `ok` at 0.45 or higher and `low` below it
(the 13 built-in voices score 0.64-0.79); `unchecked` means the engine was unavailable (`quality.reason` says why: `gpu_busy` after three tries, `engine_would_not_start`, ...). It is a warning only.

Rules. Every voice and consent belongs to the API key that created it; another key gets `404`.
A voice is used only through `{"id": ...}` or a bare `voice_<24 hex>` string, never by its name, and a
voice cannot be named like a built-in voice (`400 voice_name_reserved`), so built-in names cannot be
shadowed. Chatterbox and OmniVoice accept custom voices (`compatible_engines`); Supertonic and Kokoro answer
`400 custom_voice_engine_unsupported`. An unknown id is `404 voice_not_found`, never a silent fallback to
another voice. Limits per key, in `proxy_config.yml` `voices:`: 20 voices, 50 consents, 200 MB. Revoking an
API key deletes its voices and consents. Files live under `data/voices/<owner>/`.

**Streaming transcription.** Send `stream=true` on
`POST /v1/audio/transcriptions` to get `transcript.text.delta` events followed by
`transcript.text.done`. Streaming engines (Nemotron) send deltas while decoding;
other engines send the whole text in one delta. The gateway reads the `model`
field before the file to choose the service, so send `model` first (OpenAI SDKs
do); a request whose `model` names an engine on another service but arrives after
more than 8 MiB of audio gets `400 stt_model_field_order`.

**Speaker diarization.** `model: "gpt-4o-transcribe-diarize"` (or `stt-diarize`) on
`POST /v1/audio/transcriptions` follows OpenAI's request and response shape, so
the OpenAI SDK works unchanged:

```python
r = client.audio.transcriptions.create(
    model="gpt-4o-transcribe-diarize", file=open("meeting.wav", "rb"),
    response_format="diarized_json", chunking_strategy="auto")
for s in r.segments:            # s.speaker is "A", "B", ... in order of first appearance
    print(s.speaker, s.start, s.end, s.text)
```

- `response_format`: `diarized_json` (speaker segments; `usage` is the audio duration in seconds), `json` or `text` (plain
  text). `srt`, `vtt` and `verbose_json` are a 400, as at OpenAI. `prompt`, `include[]` and `timestamp_granularities[]` are
  a 400 `unsupported_parameter`, as at OpenAI. `language` and `temperature` are accepted and ignored.
- `chunking_strategy`: `"auto"` or `{"type": "server_vad"}` is accepted, and not required (OpenAI needs it above 30 s).
  Any length up to 4 h works: the service cuts the audio at quiet points itself and ignores VAD tuning values.
- `stream=true` with `diarized_json` sends, per segment, `transcript.text.delta` (with `segment_id`) then
  `transcript.text.segment`, and ends with `transcript.text.done` and `[DONE]`. The first event arrives after the first
  window (up to 52 s of audio) is transcribed.
- `known_speaker_names[]` + `known_speaker_references[]` (1 to 4, same count, base64 data URLs of 2 to 10 s each) are
  supported. The service puts each sample in front of the audio so the diarizer has met the voice, then labels that
  speaker with the name. Other speakers keep the letters `A`, `B`, ... (skipping a letter a caller used as a name). A
  wrong count, too many, a bad data URL or a sample outside 2 to 10 s is a 400 `invalid_known_speakers` /
  `invalid_known_speaker_reference`.
- Speakers per file: up to 8. Overlapping speech is attributed to one speaker per word (the strongest one).
- If the engine is not deployed the answer is `400 model_not_deployed`, never plain text without speakers.
- Cost: CPU only (about 1.5 GB RAM, unit exits after 15 min idle) plus the always-on Parakeet service for the words.
  About 20 s of wall time per 5 min of audio and 80 s per 25 min, measured through the gateway.

Discovery and dry run (no model is loaded or run):

```bash
curl -s localhost:5000/v1/models -H "Authorization: Bearer $KEY"      # alias entries carry vivid_fish_resolves_to; every entry a type (docs/api/MODEL_METADATA.md)
curl -s localhost:5000/gateway/v1/aliases -H "Authorization: Bearer $KEY"   # stable + OpenAI rules + engines (deployed or not)
curl -s "localhost:5000/gateway/v1/aliases/resolve?operation=chat&model=gpt-4o" -H "Authorization: Bearer $KEY"
```

`operation` is one of `chat`, `embeddings`, `images`, `image_edits`, `videos`,
`speech`, `transcriptions`, `audio_generate`. `/gateway/v1/capabilities` lists
each stable alias as a capability tagged `alias`, `resolves_to:<target>` and
`engine:<id>`.

## Showcase page: `/showcase`

`GET /showcase` serves a static demo page (source in `showcase/`, no build step,
no CDN). It builds one card per capability at load time from
`/gateway/v1/workflows`, `/gateway/v1/capabilities`, `/gateway/v1/aliases`,
`/v1/voices` and `/openapi.json`, so a new workflow manifest appears without a
code change. Section order and matching rules are data in
`showcase/sections.json`; an unmatched workflow lands in "More workflows".

Each card shows a recorded example (input, output, wall time, alias and what it
resolves to), a Try it form built from the OpenAPI input schema, and copy-paste
curl, Python and JavaScript. The Try it panels call the gateway from the browser
on the same origin, so LAN bypass works; an optional API key is kept only in
`localStorage`. The top strip shows engine status, the ComfyUI queue and whether
the LLM is generating (`GET /showcase/api/status`, normal auth).

The page, its assets and the example files are public. Examples live in
`data/showcase/examples/<id>/` (gitignored; override with
`OPENAI_PROXY_SHOWCASE_EXAMPLES_DIR`). Record them with
`scripts/build-showcase-examples`. It is idempotent, waits for an idle GPU before
every job, and runs cheap jobs first and video last. `--list` shows what is pending.

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

## Media Metadata And Provenance

The gateway always stores generated media with full metadata. What a client
receives is a delivery choice.

**What is embedded (stored copy, always).**

| Media | Where | Content |
| --- | --- | --- |
| PNG | text chunks | ComfyUI `prompt` (API graph), `workflow` (UI graph; drag the file into ComfyUI to reopen it), `vividfish_provenance` |
| JPEG, WebP | EXIF | `Make` = `workflow:<json>`, `Model` = `prompt:<json>`, `ImageDescription` = `vividfish_provenance:<json>` (ComfyUI's WebP convention) |
| MP4, WebM, FLAC, MP3 | container tags | `prompt`, `workflow`, `vividfish_provenance` |
| WAV | `comment` tag | `vividfish_provenance=<json>` (WAV has no free-form tag fields) |

The `workflow` graph is derived from the graph actually submitted to ComfyUI,
so it shows this request's prompt, seed and size. Speech (`/v1/audio/speech`)
comes from TTS engines, not ComfyUI: it has no workflow, and `full` returns
the engine bytes unchanged.

The provenance record is JSON: `schema`, `kind`, `created_at`, `request_id`,
`receipt` (`/gateway/v1/receipts/<id>`), `requested_model`, `model_alias`
(set when the client used an alias), `resolved_model`, `workflow_id`,
`workflow_version`, `workflow_hash`, `backend`, `seed`, `parameters`, `index`.
It is built from an allow-list. API keys, auth headers, signed-URL signatures
and uploaded media never enter it (values that look like credentials or inline
media are replaced before embedding).

**Delivery control.** Per request, on every route that returns generated media:

```bash
curl -s http://127.0.0.1:5000/v1/images/generations \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"flux2-klein-4b","prompt":"a lighthouse","extensions":{"com.vividfish.workflow":{"metadata":"none"}}}'
```

- `"full"`: deliver the stored bytes, metadata included.
- `"none"`: deliver a lossless rewrite with all metadata removed. PNG text
  chunks, JPEG/WebP EXIF/XMP/comments, and MP4/WebM/audio tags go; pixels and
  samples are unchanged (no re-encode; video and audio use ffmpeg
  `-map_metadata -1 -c copy`). The stored copy is never modified.
- Not set: the profile default applies (below).

Where to put it:

| Route | How |
| --- | --- |
| `/v1/images/generations`, `/v1/audio/generate`, `/v1/audio/speech`, `/v1/videos` | JSON `extensions` |
| `/v1/images/edits`, `/v1/images/variations`, `/v1/videos` (multipart) | form field `extensions[com.vividfish.workflow][metadata]` (edits also accept the JSON `extensions` field) |
| `/gateway/v1/images/identity` | form field `extensions[com.vividfish.workflow][metadata]` |
| `/gateway/v1/workflows/{id}/run` | form field `extensions[com.vividfish.workflow][metadata]` |
| `GET /v1/videos/{id}/content`, `GET /gateway/v1/media/jobs/{id}/content`, `GET /gateway/v1/assets/{id}` | query `?extensions[com.vividfish.workflow][metadata]=none` or the short `?metadata=none` |

For `response_format: "url"`, media jobs and assets, the mode is fixed when the
signed URL is minted: the URL carries `md=full|none` and the signature covers
it, so a client cannot flip `none` to `full`. A signed URL minted before this
control existed (no `md`) is delivered with `none`. A video job remembers the
choice made when it was submitted; a later GET override wins over it. Streaming
speech (`stream_format`) is delivered as the engine produced it, unbuffered.
An invalid value is a `400` with `param: extensions.com.vividfish.workflow.metadata`.

**Profile default.** Order: request control, then profile
`media_metadata.default`, then `none`.

```yaml
# profile document (admin Profile `profile_json`, or an entry's `profile:` block)
media_metadata:
  default: full      # full | none; omitted = none
```

- Seeded `Owner (local)` profile (`owner-local`): `full`.
- Trusted LAN/localhost clients (`auth.lan_bypass_*`): `full`.
- Auth disabled (a personal local run): `full`.
- Any other profile, path-token client, or profile without the block: `none`.

To change an existing profile, edit its `profile_json` (admin Profile API, or
the DB row) and add the block. Seeding never overwrites profiles, so existing
databases need this edit once: `PATCH /admin/api/profiles/{profile_id}` with
`{"profile": {...the full profile document..., "media_metadata": {"default": "full"}}}`
(the body replaces the document, so send all of it). The `/admin` Profile form
rebuilds the document from its own fields and does not carry `media_metadata`
yet: re-saving a profile from the form drops the block.

Prior art, in short: ComfyUI embeds `prompt` and `workflow` in every saved file
and offers `--disable-metadata` as a server-wide off switch (no per-request
choice). Automatic1111 writes one `parameters` text chunk (human-readable
settings, no graph). Hosted image services such as OpenAI attach a C2PA
manifest and return no workflow or generation parameters. C2PA signs a
manifest into the asset with a certificate. It proves who made a file and does
not carry a re-runnable workflow. The gateway does not implement C2PA; the
provenance record is unsigned and meant for the owner's own tooling.

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

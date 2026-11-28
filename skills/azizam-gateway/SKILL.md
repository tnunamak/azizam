---
name: azizam-gateway
description: Use Azizam (the local OpenAI-compatible AI gateway) for chat, embeddings, image generation/editing, TTS, STT, audio generation, video jobs, Anthropic Messages, Ollama-compatible chat/generate/model routes, and gateway-native capabilities/receipts. Use when a task mentions Azizam, azizam, /gateway/v1, opk_ keys, local AI backends, or apps that need one local AI endpoint.
---

# Azizam

Use this skill when you need to call the user's local AI gateway. Treat the gateway as local infrastructure owned by the user. Discover capabilities first, use the narrowest working protocol, and never expose secrets.

Set `BASE` to the operator's chosen gateway host, such as `http://127.0.0.1:5000`
for a local install or `https://ai.example.com` for a public deployment. Keep a
working app's base URL when it uses an operator-supported compatibility alias.

## Discover First

Start with unauthenticated metadata:

```bash
curl -fsS "$BASE/health"
curl -fsS "$BASE/.well-known/azizam"
```

Then use authenticated gateway discovery before assuming a modality:

```bash
curl -fsS "$BASE/gateway/v1/capabilities" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
curl -fsS "$BASE/gateway/v1/workflows" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
```

Set `AZIZAM_API_KEY` to the App bearer token for the calls below.

Typed inputs, aliases, typical times, and the sync and async route of every native workflow are in `GET /openapi.json`, root list `x-azizam-workflows` (each entry points at a `WorkflowInputs_<id>` schema). Slow work uses an async route: submit, poll, fetch the content. Pick the entry, then call its route with curl or an OpenAI SDK: `execution.fields` says whether each input is a body field, an `extensions["com.azizam.workflow"]` key, or a file part.

If a needed capability is absent or unavailable, stop and report that honestly.

## Choose The Surface

- Use `/openai/v1` OpenAI-compatible endpoints by default for new setup. `/v1` remains a convenience route for existing clients. This is the best-supported path and supports streaming chat.
- Use `POST /anthropic/v1/messages` only for Anthropic-shaped clients. The legacy convenience route is `POST /v1/messages`. Streaming is supported with Anthropic Messages SSE.
- Use `GET /ollama/api/tags`, `POST /ollama/api/chat`, `POST /ollama/api/generate`, `POST /ollama/api/show`, `GET /ollama/api/ps`, and `GET /ollama/api/version` only for Ollama-shaped clients. `/api/*` remains a convenience route for clients hard-coded to an Ollama root. Chat/generate support Ollama NDJSON streaming.
- Use `/gateway/v1/media/jobs` for durable long-running media jobs and `/gateway/v1/receipts` for diagnostics. For durable image generation, submit `{"intent":"image.generate","request":{...OpenAI image body...}}`, then poll `/gateway/v1/media/jobs/{job_id}` and fetch `/gateway/v1/media/jobs/{job_id}/content`.
- Use `GET /gateway/v1/providers` and `GET /gateway/v1/providers/{provider}` to discover provider-native lanes. Use `/gateway/v1/providers/{provider}/{path...}` only when the App profile explicitly allows `provider-native` and `native_passthrough` for that provider and operation.
- Use `data[].asset_id` and signed `/v1/assets/{asset_id}/content` URLs when image outputs need to be reused by later gateway-aware calls.

## Auth Rules

- Use `Authorization: Bearer $AZIZAM_API_KEY`.
- Bearer tokens start with `opk_` regardless of which env-var name your client uses.
- Never ask the user to paste a raw `opk_...` token into chat.
- Never read, print, or echo local key stores.
- Do not put real OpenAI, Anthropic, or vendor API keys into source files,
  prompts, or shared docs.
- Prefer environment variables, local secret stores, or existing app config.

## Debugging

When a request is rejected, an extension is ignored, or output does not match the expected workflow control, fetch receipts:

```bash
curl -fsS "$BASE/gateway/v1/receipts?limit=20" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
curl -fsS "$BASE/gateway/v1/receipts/$REQUEST_ID" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
```

Receipts record field names and reasons, not raw prompts or media bytes. Use them to understand policy denials, accepted inputs, rejected inputs, ignored namespaced workflow controls, and manifest provenance.

## Workflow Controls

Do not send backend workflow knobs as random top-level OpenAI fields. First fetch `GET /gateway/v1/workflows`, then send only manifest-listed controls under `extensions.com.azizam.workflow`.

Current examples:

- `/v1/images/edits` with `model: qwen-image-2.1-edit` supports `steps`, `cfg`, and `seed` as namespaced workflow controls. Check discovery for workflows that advertise ControlNet inputs.
- `/v1/images/edits` accepts OpenAI's standard top-level multipart `mask` field. Do not put `mask` under `extensions`. Alpha masks use OpenAI polarity: transparent edits, opaque preserves. Grayscale masks use white to edit and black to preserve. If the resolved workflow has no native mask binding, the proxy preserves unmasked pixels with a documented post-composite fallback and returns `X-Image-Mask: post-composite`.
- Future ControlNet/conditioning controls are manifest-backed workflow controls, not OpenAI top-level fields. If discovery shows an asset input such as `control_image`, multipart edits can upload it as an extra form field and reference it from the extension value: `{"control_image":{"multipart_field":"control_image"},"controlnet_strength":0.75}`.
- Asset-valued workflow controls may also use `{"asset_id":"img_<sha256>"}` when the same App API key minted that prior image response and discovery shows the selected workflow supports the asset input.
- `/v1/audio/generate` with ACE-Step accepts top-level `tags` and `lyrics`. It uses `prompt` for tags when `tags` is absent. Keep `duration` and `seed` top-level.
- `/v1/videos` with the bundled LTX-2.5 workflows uses a fixed 8-step schedule at CFG 1. Keep `size`, `seconds`, `seed`, `prompt`, and `input_reference` top-level. Video chaining requires an operator-configured workflow.

## Chaining And Variants

- Image generation/edit responses include `asset_id`, `asset_url`, and `media_type` alongside `b64_json` or `url`.
- Durable image jobs always produce URL-backed assets, even if the submitted request asked for `b64_json`; this keeps job records small and reusable.
- Video `input_reference` accepts prior image handles as `{"asset_id":"img_<sha256>"}` or `asset:img_<sha256>` when the same App API key minted the asset.
- For image variants, use top-level `n`; for repeatability, use manifest-backed `extensions.com.azizam.workflow.seed` only when discovery lists `seed`.
- For video variants, repeat calls with different top-level `seed`. Do not invent `variant_count` or other unadvertised controls.

Example JSON body shape:

```json
{
  "model": "qwen-image-2.1-edit",
  "prompt": "preserve this structure, paint it as underwater rock",
  "extensions": {
    "com.azizam.workflow": {
      "cfg": 3.5
    }
  }
}
```

## Common Calls

- Chat: `POST /openai/v1/chat/completions`
- Embeddings: `POST /openai/v1/embeddings`
- TTS: `POST /openai/v1/audio/speech`
- STT: `POST /openai/v1/audio/transcriptions`
- Image generation: `POST /openai/v1/images/generations`
- Durable image generation: `POST /gateway/v1/media/jobs` with `intent: "image.generate"`
- Image edit: `POST /openai/v1/images/edits`
- Audio/music/SFX generation: `POST /openai/v1/audio/generate`
- Video job submit/poll/fetch: `POST /openai/v1/videos`, `GET /openai/v1/videos/{id}`, `GET /openai/v1/videos/{id}/content`
- Native discovery: `GET /gateway/v1/capabilities`, `GET /gateway/v1/workflows`, `GET /gateway/v1/providers`

## Anti-Patterns

- Assuming a model or modality exists because OpenAI has it.
- Assuming Ollama model mutation routes pull/create/delete local files; they return unsupported-operation by design. Manage model files on the backend that owns them.
- Sending ComfyUI-specific knobs as random top-level OpenAI fields. Put gateway workflow controls under `extensions.com.azizam.workflow` when a manifest supports them.
- Rewriting a working app's hostname purely for cosmetic reasons when the operator still supports its compatibility alias.
- Reading secret files to "check" credentials.

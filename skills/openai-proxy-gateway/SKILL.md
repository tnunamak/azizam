---
name: openai-proxy-gateway
description: Use openai-proxy, the local OpenAI-compatible AI gateway, for chat, embeddings, image generation/editing, TTS, STT, audio generation, video jobs, Anthropic Messages, Ollama-compatible chat/generate, and gateway-native capabilities/receipts. Use when a task mentions openai-proxy, openai.vivid.fish, /gateway/v1, opk_ keys, local AI backends, or agent clients that need one local AI endpoint.
---

# openai-proxy gateway

Use this skill when you need to call the user's local AI gateway. Treat the gateway as local infrastructure owned by the user. Discover capabilities first, use the narrowest working protocol, and never expose secrets.

## Discover First

Start with unauthenticated metadata:

```bash
curl -fsS "$BASE/health"
curl -fsS "$BASE/.well-known/ai-gateway" || curl -fsS "$BASE/.well-known/openai-proxy"
```

Then use authenticated gateway discovery before assuming a modality:

```bash
curl -fsS "$BASE/gateway/v1/capabilities" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
curl -fsS "$BASE/gateway/v1/workflows" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

If a needed capability is absent or unavailable, stop and report that honestly.

## Choose The Surface

- Use `/v1` OpenAI-compatible endpoints by default. This is the best-supported path and supports streaming chat.
- Use `POST /v1/messages` only for Anthropic-shaped clients. It is non-streaming unless discovery metadata says otherwise.
- Use `GET /api/tags`, `POST /api/chat`, or `POST /api/generate` only for Ollama-shaped clients. Chat/generate are non-streaming unless discovery metadata says otherwise.
- Use `/gateway/v1/media/jobs` for durable long-running media jobs and `/gateway/v1/receipts` for diagnostics.

## Auth Rules

- Use `Authorization: Bearer $OPENAI_PROXY_API_KEY`.
- Never ask the user to paste a raw `opk_...` token into chat.
- Never read, print, or echo `proxy_api_keys.yml`.
- Do not put real OpenAI, Anthropic, or vendor API keys into `proxy_api_keys.yml`.
- Prefer environment variables, local secret stores, or existing client config.

## Debugging

When a request is rejected, an extension is ignored, or output does not match the expected workflow control, fetch receipts:

```bash
curl -fsS "$BASE/gateway/v1/receipts?limit=20" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
curl -fsS "$BASE/gateway/v1/receipts/$REQUEST_ID" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Receipts record field names and reasons, not raw prompts or media bytes. Use them to understand policy denials, accepted inputs, rejected inputs, ignored namespaced workflow controls, and manifest provenance.

## Workflow Controls

Do not send backend workflow knobs as random top-level OpenAI fields. First fetch `GET /gateway/v1/workflows`, then send only manifest-listed controls under `extensions.com.vividfish.workflow`.

Current examples:

- `/v1/images/edits` with `model: flux2-klein-4b-edit` supports `denoise`, `steps`, and `guidance` as namespaced workflow controls. ControlNet is not currently exposed unless discovery shows a ControlNet-capable workflow.
- `/v1/audio/generate` with `model: tangoflux` supports `steps`, `guidance`, and `batch_size` as namespaced workflow controls. Keep `duration` and `seed` top-level.
- `/v1/videos` with `model: video-ltx-2.3-distilled` supports `steps`, `stage1_cfg`, and `stage2_cfg` as namespaced workflow controls. Keep `size`, `seconds`, `seed`, prompt, and image-reference fields top-level.

Example JSON body shape:

```json
{
  "model": "flux2-klein-4b-edit",
  "prompt": "preserve this structure, paint it as underwater rock",
  "extensions": {
    "com.vividfish.workflow": {
      "denoise": 0.35
    }
  }
}
```

## Common Calls

- Chat: `POST /v1/chat/completions`
- Embeddings: `POST /v1/embeddings`
- TTS: `POST /v1/audio/speech`
- STT: `POST /v1/audio/transcriptions`
- Image generation: `POST /v1/images/generations`
- Image edit: `POST /v1/images/edits`
- Audio/music/SFX generation: `POST /v1/audio/generate`
- Video job submit/poll/fetch: `POST /v1/videos`, `GET /v1/videos/{id}`, `GET /v1/videos/{id}/content`
- Native discovery: `GET /gateway/v1/capabilities`, `GET /gateway/v1/workflows`

## Anti-Patterns

- Assuming a model or modality exists because OpenAI has it.
- Streaming over `/v1/messages`, `/api/chat`, or `/api/generate` without discovery metadata saying it is supported.
- Sending ComfyUI-specific knobs as random top-level OpenAI fields. Put gateway workflow controls under `extensions.com.vividfish.workflow` when a manifest supports them.
- Reading secret files to "check" credentials.

# API And SDK Target Inventory

Date: 2026-05-15

This file tracks the API surfaces this gateway should care about. It separates public client-facing compatibility from backend adapter support.

The product goal is broad backend coverage without pretending every API is equally supported. A second user with a different pile of engines should be able to add adapters and workflow manifests without changing the core protocol/router/scheduler model.

## Compatibility Tiers

```text
Tier A: SDK-compatible
Real SDK/client tests pass for the documented subset.

Tier B: useful subset
Common requests work, and unsupported features return protocol-shaped errors.

Tier C: explicit native/passthrough
Useful backend-native operations are exposed without broad compatibility claims.

Adapter-only
The gateway calls this API behind the scenes, but does not expose it as a public client protocol.

Deferred
Known surface, not worth first-class support yet.
```

## Client-Facing Protocols

| Priority | Surface | Initial Tier | Why It Matters |
|---|---|---:|---|
| P0 | OpenAI Chat Completions | A/B | Local tooling lingua franca for Open WebUI, LibreChat, Continue, Cline-like agents, vLLM, llama.cpp, LM Studio, TabbyAPI, TGI, OpenRouter-style clients. |
| P0 | OpenAI Responses | B | Strategic OpenAI-native agent surface; this repo already has a translator/store. |
| P0 | OpenAI Images | B | Familiar app API for image generation/editing while hiding ComfyUI/A1111 workflow details. |
| P0 | OpenAI Audio | B | Familiar API for TTS/STT while routing to local or cloud voice engines. |
| P0 | OpenAI Embeddings | B | Common vector/search integration surface. |
| P0 | Anthropic Messages | A/B | Important for Claude-shaped coding agents and tool-use semantics. |
| P0 | Ollama native API | B/C | Self-hosters treat Ollama as a first-class local protocol, especially for model management. |
| P1 | Gateway job/assets API | A | Needed for video, music, sound effects, long image jobs, durable polling, cancellation, and assets. |
| P1 | ElevenLabs-compatible speech | B | Useful if voice agents and TTS clients become a real wedge. |
| P1 | Gemini GenerateContent | B | Useful for Gemini-native apps and cloud fallback, but not first wedge. |
| P1 | KoboldAI-compatible API | B/C | Relevant for SillyTavern/roleplay users where native sampler controls matter. |
| P2 | OpenRouter-specific extensions | C | Mostly OpenAI-compatible; support provider metadata/routing when needed. |
| P2 | A1111/Forge-compatible image API | C | Useful for SD clients, but more valuable initially as backend adapter. |
| P2 | ComfyUI native API passthrough | C | Useful for power users, but raw graph/queue API should not be the primary public contract. |
| Defer | Bedrock/Vertex full native APIs | Deferred | Hosted enterprise surfaces; too broad for the local-first wedge. |
| Defer | Fine-tuning/training/admin APIs | Deferred | Not part of the gateway's first job. |

## Backend Adapter Targets

| Priority | Backend API | Adapter Role |
|---|---|---|
| P0 | Ollama | Chat, generate, embeddings, model list/show, load/unload/ps semantics where possible. |
| P0 | llama.cpp server | OpenAI-compatible chat/completions/embeddings, multimodal/tool support where available, local model metadata. |
| P0 | vLLM OpenAI server | High-throughput chat/completions/embeddings, streaming, tool support, model metadata. |
| P0 | LM Studio local server | Desktop local-AI endpoint, OpenAI-compatible and any documented Anthropic-compatible behavior. |
| P0 | TabbyAPI | EXL2/GPTQ/FP16 local backend, model load/unload/admin API, OpenAI-compatible chat. |
| P0 | KoboldCpp/KoboldAI | OpenAI-compatible plus native Kobold parameters for SillyTavern-style use. |
| P0 | ComfyUI | Image/video/audio workflow execution, queue/history/websocket progress, upload/download/assets. |
| P0 | local TTS engines | Speech synthesis behind OpenAI/ElevenLabs/gateway voice APIs. |
| P0 | local STT engines | Transcription behind OpenAI/gateway audio APIs. |
| P0 | music/SFX engines | Music/sound generation behind gateway job API. |
| P1 | A1111/Forge SD API | Image generation/edit/upscale/inpaint backend. |
| P1 | InvokeAI/SwarmUI | Image workflow backend if users ask. |
| P1 | Hugging Face TGI | Text generation backend, preferably through OpenAI-compatible route first. |
| P1 | Hugging Face TEI | Embeddings/rerank backend. |
| P1 | OpenRouter | Hosted fallback/provider pool through OpenAI-compatible surface. |
| P1 | OpenAI/Anthropic/Gemini hosted APIs | Optional cloud fallback backends with local-only policy controls. |
| P1 | ElevenLabs | TTS/SFX backend. |
| P2 | Stability/Replicate/Fal/etc. | Optional hosted media fallback backends. |
| P2 | Stable Horde / AI Horde | Optional community compute backend. |

## Backend Adapter Pattern

Use two layers for local LLM backends:

1. OpenAI-compatible baseline adapter.
   This covers the common request path for vLLM, llama.cpp, LM Studio, TabbyAPI, text-generation-webui, TGI, Aphrodite, LocalAI, and parts of KoboldCpp.

2. Backend-native management adapter.
   This covers lifecycle, discovery, health, and extra controls that OpenAI-compatible APIs do not expose well.

Examples:

- Ollama native: `/api/tags`, `/api/show`, `/api/ps`, `/api/pull`, `/api/chat`, `/api/generate`, `keep_alive`.
- LM Studio native: model list/load/unload/download APIs.
- llama.cpp native: `/health`, `/props`, `/slots`, `/completion`, `/reranking`.
- TabbyAPI admin: model load/unload and admin-key-protected operations.
- Kobold native: `/api/v1/generate`, streaming extras, legacy sampler controls.
- TEI: embedding-only backend.

The OpenAI-compatible baseline gets requests running quickly. The native adapter makes the gateway good for self-hosters.

## Adapter Capability Metadata

Every backend adapter should report:

- supported intents
- supported protocol-native passthrough operations
- model list
- model residency if known
- load/unload support
- streaming support
- tool-call support
- structured-output support
- vision/audio/input modalities
- embeddings/rerank support
- context limits
- max concurrency
- cancellation support
- resource estimates
- health and readiness probes
- known compatibility caveats

## Canonical Intents Must Stay Broad

The intent set should cover the first user's full machine and a second user's different machine:

- `chat.generate`
- `text.complete`
- `text.embed`
- `text.rerank`
- `speech.synthesize`
- `speech.transcribe`
- `audio.generate`
- `sound.generate`
- `music.generate`
- `image.generate`
- `image.edit`
- `image.upscale`
- `image.inpaint`
- `video.generate`
- `video.edit`
- `audio.translate`
- `model.list`
- `model.load`
- `model.unload`
- `model.pull`
- `model.show`
- `model.running`
- `job.create`
- `job.get`
- `job.cancel`
- `asset.get`

Not every protocol needs to expose every intent. The router should return honest unsupported-capability errors when a client API asks for something no configured backend can do.

## Media API Shape

Media and multimodal work should expose three layers:

1. OpenAI-compatible layer.
   Images, image edits, audio speech, audio transcriptions, audio translations, embeddings, and eventually Responses image/audio behavior.

2. Provider-native passthrough layer.
   Explicit paths for backend-specific controls such as ComfyUI graph/queue operations, A1111 script args, ElevenLabs voice settings, or backend-specific audio/music parameters.

3. Generic workflow/job layer.
   Durable jobs for video, music, sound effects, long-running image workflows, upscaling batches, and hosted queue-style media APIs.

Implemented generic job surface:

```text
POST /gateway/v1/media/jobs
GET  /gateway/v1/media/jobs
GET  /gateway/v1/media/jobs/{id}
POST /gateway/v1/media/jobs/{id}/events
POST /gateway/v1/media/jobs/{id}/cancel
```

Gateway-native discovery and extension schemas should also live outside
vendor-compatible namespaces. `gateway/api.py` is mounted by `proxy.py` for
workflow discovery, capabilities, receipts, and media jobs. The currently live
discovery/receipt surface is:

```text
GET /gateway/v1/capabilities
GET /gateway/v1/extensions
GET /gateway/v1/models/{model}/capabilities
GET /gateway/v1/workflows
GET /gateway/v1/workflows/{workflow}
GET /gateway/v1/receipts
GET /gateway/v1/receipts/{request_id}
```

Additional discovery can layer onto this surface as more adapters and manifests
land.

These are not OpenAI/Ollama/Anthropic standards. They are the gateway's own
introspection contract.

Workflow-specific conditioning controls such as ControlNet, IP-Adapter, and
style-reference images belong in this gateway extension/job layer unless a
vendor-compatible protocol already owns the field. See
`docs/CONDITIONING_CONTROLS.md`.

The job request should carry:

- intent
- provider/backend or route policy
- workflow alias
- typed inputs
- input assets
- idempotency key
- requested output formats

This is the right abstraction for ComfyUI, video generation, music generation, SFX, long audio, and queued hosted media APIs.

## Reference Links

- OpenAI API reference: https://platform.openai.com/docs/api-reference
- OpenAI Python SDK: https://github.com/openai/openai-python
- OpenAI Node SDK: https://github.com/openai/openai-node
- Anthropic Python SDK: https://github.com/anthropics/anthropic-sdk-python
- Anthropic TypeScript SDK: https://github.com/anthropics/anthropic-sdk-typescript
- Ollama API: https://docs.ollama.com/api
- Ollama Python SDK: https://github.com/ollama/ollama-python
- Ollama JavaScript SDK: https://github.com/ollama/ollama-js
- vLLM OpenAI-compatible server: https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html
- llama.cpp server: https://www.mintlify.com/ggml-org/llama.cpp/inference/server
- LM Studio server docs: https://lmstudio.ai/docs/app/api
- TabbyAPI: https://github.com/theroyallab/tabbyapi
- KoboldCpp: https://koboldai.com/KoboldCpp/
- Text Generation WebUI API docs: https://github.com/oobabooga/text-generation-webui/wiki
- Hugging Face TGI API: https://huggingface.co/docs/text-generation-inference/reference/api_reference
- Hugging Face TEI: https://huggingface.co/docs/text-embeddings-inference
- ComfyUI server routes: https://docs.comfy.org/development/comfyui-server/comms_routes
- Automatic1111 API: https://github.com/AUTOMATIC1111/stable-diffusion-webui/wiki/API
- InvokeAI API docs: https://invoke-ai.github.io/InvokeAI/
- ElevenLabs API: https://elevenlabs.io/docs/api-reference
- Gemini OpenAI compatibility: https://ai.google.dev/gemini-api/docs/openai
- Gemini API docs: https://ai.google.dev/gemini-api/docs
- OpenRouter API: https://openrouter.ai/docs/api-reference/overview
- LibreChat endpoints: https://www.librechat.ai/docs/configuration/librechat_yaml/ai_endpoints
- Open WebUI OpenAI-compatible provider docs: https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible
- Open WebUI Ollama provider docs: https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-ollama/
- SillyTavern API connections: https://docs.sillytavern.app/usage/api-connections/
- Continue model providers: https://docs.continue.dev/customize/model-providers
- Cline docs: https://docs.cline.bot/

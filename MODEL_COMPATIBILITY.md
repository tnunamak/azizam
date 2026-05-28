# Model Compatibility Guide

The proxy accepts OpenAI-compatible model names from clients and resolves them
at the protocol boundary. Commodity LLM execution (`chat`, `completions`, and
`embeddings`) is delegated to LiteLLM model groups. Gateway-native media
operations still map OpenAI presets such as `tts-1`, `whisper-1`, or `dall-e-3`
to local TTS, STT, or ComfyUI workflows.

## Runtime Contract

The endpoint defines the operation. For LLM-shaped OpenAI routes, `model` is a
required LiteLLM model group or gateway-declared local role. The gateway may
activate a configured local target before calling LiteLLM, but it does not
forward these routes directly to Tabby, Kobold, or an embeddings server.

Examples:

```text
POST /v1/chat/completions      model omitted      -> 400 missing_required_parameter
POST /v1/chat/completions      model=default      -> LiteLLM model group, with optional local target activation
POST /v1/completions           model=default      -> LiteLLM model group, with optional local target activation
POST /v1/embeddings            model=embed-default -> LiteLLM embedding model group
POST /v1/audio/speech          model=tts-1        -> local TTS model
POST /v1/audio/transcriptions  model=whisper-1    -> local STT model
POST /v1/images/generations    model=dall-e-3     -> local image model
POST /v1/images/edits          model=gpt-image-1  -> local image-edit model
```

Speech synthesis accepts `response_format: "wav"` and `response_format: "mp3"`.
The local TTS backend is requested as WAV; MP3 responses are transcoded at the
gateway boundary with ffmpeg. Other OpenAI speech formats are rejected with
`unsupported_response_format`.

Known OpenAI model families used on the wrong endpoint fail with a clear `400`
instead of routing silently:

```text
POST /v1/chat/completions model=dall-e-3 -> 400 invalid_request_error
POST /v1/audio/speech     model=gpt-4o   -> 400 invalid_request_error
```

Local LLM model names do not bypass LiteLLM. Operators should expose them as
LiteLLM model groups or gateway local roles. Unknown LLM model names are left to
LiteLLM so non-local LiteLLM providers can still work; unknown media model names
continue to be resolved by the gateway workflow catalog for the relevant media
endpoint.

## Defaults

Defaults live in `proxy_config.yml`:

```yaml
model_compatibility:
  enabled: true
  generated_aliases_file: openai_model_aliases.generated.yml
  defaults:
    speech: mistralai/Voxtral-4B-TTS-2603
    transcriptions: parakeet-tdt-0.6b-v3
    images: flux2-klein-4b
    image_edits: flux2-klein-4b-edit
```

For chat, completions, and embeddings, LiteLLM is the execution substrate and
its catalog is the source of truth for provider/model-group execution. Gateway
roles may still declare local targets for VRAM-aware activation before LiteLLM
is called, but startup/load is never a hidden fallback after LiteLLM is
unconfigured or disabled.

These routes are mounted as explicit Gateway handlers:

```text
POST /openai/v1/chat/completions
POST /openai/v1/completions
POST /openai/v1/embeddings
POST /v1/chat/completions
POST /v1/completions
POST /v1/embeddings
```

They do not depend on the generic backend-native catch-all. If LiteLLM is not
configured, disabled, or unavailable, they return a protocol-shaped error rather
than falling through to Tabby, Kobold, or the embedding server.

`GET /v1/models` is intentionally stricter than secondary discovery projections:
if the LiteLLM model catalog cannot be reached, `/v1/models` returns `503
backend_unavailable`. Anthropic and Ollama model-list projections return valid
empty protocol-shaped lists in that situation so client discovery does not crash
the gateway.

## Dynamic OpenAI Model Discovery

OpenAI's official `GET /v1/models` endpoint returns currently available model
IDs for an API key. The proxy can cache those IDs locally:

```bash
cd /home/tnunamak/applications/openai-proxy
OPENAI_MODEL_DISCOVERY_API_KEY='sk-...' ./venv/bin/python proxy.py refresh-openai-models
```

`OPENAI_API_KEY` is also accepted if `OPENAI_MODEL_DISCOVERY_API_KEY` is not set.

The command writes:

```text
openai_model_aliases.generated.yml
```

The generated file stores model IDs and inferred categories. Runtime request
handling reads the local file; it does not call OpenAI or scrape docs while
serving requests.

By default the refresh command excludes private and fine-tuned model IDs from
the cache. Set `OPENAI_MODEL_DISCOVERY_INCLUDE_PRIVATE=true` only if you
intentionally want account-specific models in the generated alias file.

This is intentional:

- Request behavior stays deterministic and offline.
- A transient OpenAI/docs outage cannot break local inference.
- Operators can review generated aliases before relying on them.

## Classification

The proxy combines three sources:

1. Built-in stable OpenAI naming patterns.
2. Optional manual aliases in `proxy_config.yml`.
3. Optional generated aliases from `refresh-openai-models`.

Manual aliases can be added when a new model name appears before the heuristic
knows how to categorize it:

```yaml
model_compatibility:
  aliases:
    gpt-example-next:
      category: chat
    image-example-next:
      category: images
```

Supported categories:

```text
chat
images
speech
transcriptions
embeddings
unknown
```

`unknown` aliases are accepted for the endpoint that received them and mapped to
that operation's local default. Explicit image/audio/embedding names used on the
wrong endpoint are rejected.

## Embeddings

OpenAI embedding model aliases are served through LiteLLM model groups. The
current local default group points at `nomic-embed-text-v1.5`, served by a
dedicated CPU-only llama.cpp embedding backend so it does not claim GPU VRAM
from the LLM, TTS, or ComfyUI budgets.

Use `/v1/embeddings` or `/openai/v1/embeddings` for OpenAI-compatible clients.
Ollama-compatible clients may use `/api/embed`, `/ollama/api/embed`,
`/api/embeddings`, or `/ollama/api/embeddings`.

## Logs And Responses

When the gateway activates a local target before LiteLLM execution, it logs the
requested model and target information:

```text
[proxy] LiteLLM local target activation default -> kobold /models/default.gguf
```

For non-streaming JSON chat responses, LiteLLM owns the returned `model` field.
The gateway adds route receipts and response headers for execution observability
without re-implementing provider-specific response semantics.

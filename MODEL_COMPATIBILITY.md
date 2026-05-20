# Model Compatibility Guide

The proxy accepts OpenAI-compatible model names from clients and translates them
to local backend models for the requested operation. This lets agents keep using
ordinary OpenAI presets such as `gpt-4o`, `tts-1`, `whisper-1`, or `dall-e-3`
without knowing the private Tabby, Kobold, TTS, STT, or ComfyUI model names.

## Runtime Contract

The endpoint defines the operation. The `model` field is treated as a public
compatibility hint.

Examples:

```text
POST /v1/chat/completions      model omitted      -> active/default local LLM
POST /v1/chat/completions      model=gpt-4o       -> active/default local LLM
POST /v1/audio/speech          model=tts-1        -> local TTS model
POST /v1/audio/transcriptions  model=whisper-1    -> local STT model
POST /v1/images/generations    model=dall-e-3     -> local image model
POST /v1/images/edits          model=gpt-image-1  -> local image-edit model
```

Known OpenAI model families used on the wrong endpoint fail with a clear `400`
instead of routing silently:

```text
POST /v1/chat/completions model=dall-e-3 -> 400 invalid_request_error
POST /v1/audio/speech     model=gpt-4o   -> 400 invalid_request_error
```

Local model names still pass through. If a client asks for a model that is not
recognized as an OpenAI model alias, the proxy treats it as a local/backend model
name and keeps the existing routing behavior.

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

For chat and completions, OpenAI aliases use the currently active LLM backend
when one is healthy. The proxy then rewrites the request to that backend's loaded
model ID. If no LLM backend is active, the normal default Tabby startup/load path
is used.

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

OpenAI embedding model aliases are recognized and mapped to the configured
local embedding default. The current default is `nomic-embed-text-v1.5`,
served by a dedicated CPU-only llama.cpp embedding backend so it does not claim
GPU VRAM from the LLM, TTS, or ComfyUI budgets.

Use `/v1/embeddings` or `/openai/v1/embeddings` for OpenAI-compatible clients.
Ollama-compatible clients may use `/api/embed`, `/ollama/api/embed`,
`/api/embeddings`, or `/ollama/api/embeddings`.

## Logs And Responses

When the proxy rewrites a model alias, it logs the mapping:

```text
[proxy] Model alias gpt-4o -> Qwen3.6-27B for /v1/chat/completions
```

For non-streaming JSON chat responses, the proxy returns the client-requested
OpenAI model name in the `model` field for client compatibility while the logs
show the effective local model.

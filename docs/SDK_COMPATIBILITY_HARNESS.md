# SDK Compatibility Harness

Date: 2026-05-15

The gateway should prove compatibility with real SDKs and apps as it evolves. Curl tests are not enough: SDKs validate response envelopes, streaming event names, multipart uploads, tool-call shapes, pagination, and error types.

## Harness Principles

- Pin SDK versions in test fixtures.
- Run SDK tests against fake backend adapters first.
- Run optional smoke tests against real local backends when present.
- Publish a generated compatibility matrix.
- Test unsupported features as carefully as supported ones.
- Do not claim broad compatibility unless SDK tests pass for that endpoint.

## P0 SDKs

| SDK | Package | Protocols To Test |
|---|---|---|
| OpenAI Python | `openai` | Responses, Chat Completions, Images, Audio, Embeddings, Files where needed |
| OpenAI Node | `openai` | Responses, Chat Completions, Images, Audio, Embeddings |
| Anthropic Python | `anthropic` | Messages, streaming, tool use, image blocks |
| Anthropic TypeScript | `@anthropic-ai/sdk` | Messages, streaming, tool use, image blocks |
| Ollama Python | `ollama` | chat, generate, list/tags, show, ps, embeddings |
| Ollama JavaScript | `ollama` | chat, generate, list/tags, show, embeddings |

## P1 SDKs And Consumers

| SDK/App | Why It Matters |
|---|---|
| Vercel AI SDK | Common abstraction used by apps and agents. Tests OpenAI-compatible and Anthropic-compatible behavior from a different client layer. |
| Google Gen AI SDKs | Gemini-native compatibility and cloud fallback. |
| ElevenLabs SDKs | Voice clients and TTS/SFX workflows. |
| Open WebUI | High-value self-hosted frontend smoke target. |
| LibreChat | Multi-provider chat frontend smoke target. |
| SillyTavern | Roleplay/local-backend compatibility and Kobold/OpenAI quirks. |
| Continue | Coding assistant compatibility target. |
| Cline/OpenCode-style agents | Tool-call, streaming, Anthropic/OpenAI behavior, and long-context smoke target. |
| Claude Code | Anthropic-compatible API, `ANTHROPIC_BASE_URL`, streaming, tool use, gateway model discovery. |
| n8n | Automation workflows for image/audio/video APIs. |
| Home Assistant custom OpenAI-compatible integrations | Voice assistant/entity-control smoke target; built-in HA OpenAI integration is official-OpenAI-only. |

## Test Cases By Protocol

### OpenAI-Compatible

- client construction with custom `base_url`
- bearer auth with proxy key
- `GET /v1/models`
- chat non-streaming
- chat streaming
- function/tool calls
- JSON/schema response format
- image input content blocks
- Responses non-streaming
- Responses streaming where supported
- stored Responses get/delete
- embeddings
- image generation
- image edit multipart upload
- audio speech
- audio transcription multipart upload
- protocol-shaped validation error
- backend unavailable error
- unsupported hosted-tool error

### Anthropic Messages

- client construction with custom `base_url`
- bearer or `x-api-key` auth mapping
- message non-streaming
- message streaming event names
- system prompt
- tools and tool results
- image content blocks
- max token validation
- unsupported feature error
- backend unavailable error

### Ollama Native

- client construction with custom host
- model list/tags
- show model metadata
- chat non-streaming
- chat streaming
- generate non-streaming
- generate streaming
- embeddings
- running model/process list where supported
- pull/load/unload semantics if mapped
- unsupported operation error

### Gateway Job/Asset API

- create image/video/audio/music job
- idempotency key handling
- queue position
- progress events
- cancellation
- success asset URL
- signed/local asset fetch
- failure with actionable diagnostic

## App And Agent Smoke Tests

These are not unit tests for the gateway protocol contract. They are high-signal integration tests that prove real self-hosted clients can connect.

| App/Agent | Smoke Test |
|---|---|
| Open WebUI | Configure OpenAI-compatible URL ending in `/v1`; test `/v1/models`, non-stream chat, streaming chat, embeddings, images, STT, and TTS where enabled. |
| LibreChat | Use `librechat.yaml` custom endpoint with `baseURL`; verify generated requests hit `/v1/chat/completions`; test streaming and tool calls. |
| SillyTavern | Configure `Custom (OpenAI-compatible)` with `/v1`; run long character/system prompt, streaming chat, and multimodal content if model supports it. |
| Continue | Configure `provider: openai`, `apiBase`; test chat, autocomplete via `/completions`, streaming, and behavior when `/responses` is disabled. |
| Cline | Configure OpenAI-compatible provider; run a minimal task, a tool-using task, streaming response, and image input if the model declares vision. |
| OpenCode | Configure custom provider with `@ai-sdk/openai-compatible`; run text and tool-use prompts, verify headers and model metadata. |
| Claude Code | Set `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY`; test `/v1/messages`, streaming event sequence, tool use, and `/v1/models` discovery if enabled. |
| n8n | Validate OpenAI credential, run AI Agent with an OpenAI Chat Model and a simple tool, test streaming trigger where supported, and raw HTTP Request fallback. |
| Home Assistant custom integration | Test one conversation turn, one entity-control tool call, image generation action if supported, and timeout/error handling. |

## Compatibility Matrix Output

Generate a machine-readable report, for example:

```json
{
  "protocol": "openai",
  "sdk": "openai-python",
  "sdk_version": "x.y.z",
  "gateway_version": "local",
  "results": {
    "chat.non_streaming": "pass",
    "chat.streaming": "pass",
    "chat.tools": "partial",
    "responses.streaming": "unsupported",
    "images.edits": "pass"
  },
  "notes": {
    "responses.streaming": "Gateway returns protocol-shaped unsupported error."
  }
}
```

The README should eventually render this into a human table.

## Running The Scaffold

A first pass of this harness lives under `tests/compat/` and
`scripts/compat/`. It is intentionally minimal:

- `tests/compat/harness/` is the shared, stdlib-only library (env
  validation, matrix builder, fake response shapes).
- `tests/test_compat_harness.py` covers the harness library and is run
  by the repo's default unittest discovery.
- `tests/compat/python/probe_*.py` are optional live probes that
  auto-skip when their SDK or the env is missing.
- `tests/compat/node/` is an isolated Node project for JS SDK probes —
  it does not touch the Python install.
- `scripts/compat/run_matrix.py` aggregates whatever probes are
  available and emits a JSON document matching
  `tests/compat/matrix/compatibility.schema.json`.

Quick commands:

```bash
PROXY_PYTHON=${PROXY_PYTHON:-venv/bin/python}
PROXY_PIP=${PROXY_PIP:-venv/bin/pip}

# Core unit tests (no SDKs, no network):
$PROXY_PYTHON -m unittest discover -s tests -p 'test_*.py' -v

# Compatibility matrix dry-run:
$PROXY_PYTHON scripts/compat/run_matrix.py --dry-run

# Optional SDK probe (Python):
$PROXY_PIP install -r tests/compat/python/requirements-openai.txt
export OPENAI_PROXY_BASE_URL=http://127.0.0.1:8080
export OPENAI_PROXY_API_KEY=sk-your-proxy-key
$PROXY_PYTHON -m unittest tests.compat.python.probe_openai -v
```

Full setup, env vars, and instructions for adding new probes live in
[`tests/compat/README.md`](../tests/compat/README.md).

## Suggested Repo Layout

```text
tests/compat/
  README.md
  conftest.py
  fake_backends/
    openai_chat.py
    media.py
    ollama.py
  python/
    test_openai_sdk.py
    test_anthropic_sdk.py
    test_ollama_sdk.py
  node/
    package.json
    openai.test.ts
    anthropic.test.ts
    ollama.test.ts
  fixtures/
    requests/
    expected/
  matrix/
    compatibility.schema.json
```

## Reference Links

- OpenAI Python SDK: https://github.com/openai/openai-python
- OpenAI Node SDK: https://github.com/openai/openai-node
- OpenAI API reference: https://platform.openai.com/docs/api-reference
- Anthropic Python SDK: https://github.com/anthropics/anthropic-sdk-python
- Anthropic TypeScript SDK: https://github.com/anthropics/anthropic-sdk-typescript
- Anthropic Messages API: https://docs.anthropic.com/en/api/messages
- Ollama Python SDK: https://github.com/ollama/ollama-python
- Ollama JavaScript SDK: https://github.com/ollama/ollama-js
- Ollama API: https://docs.ollama.com/api
- Vercel AI SDK: https://ai-sdk.dev/docs
- Google Gen AI SDK docs: https://ai.google.dev/gemini-api/docs/sdks
- ElevenLabs SDK/API docs: https://elevenlabs.io/docs

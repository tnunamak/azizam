# Vendor Facades

Status: implementation snapshot, 2026-10-03.

This document describes the vendor-shaped front doors that exist in the gateway today. It is not a roadmap. Check `proxy.py` before changing it: most facades are registered there and delegate into pure translators under `gateway/`.

The common rule is the same for all facades: they translate vendor protocol shapes into existing gateway routes. Auth, app profile policy, route receipts and backend selection stay in the core gateway path. When a vendor feature would require owning model files, hosted vendor tools, or a protocol engine the gateway does not have, the facade returns an explicit error.

## Anthropic Messages

Routes:

```text
GET  /anthropic/v1/models
GET  /anthropic/v1/models/{model_id}
POST /anthropic/v1/messages
POST /anthropic/v1/messages/count_tokens
GET  /v1/models
GET  /v1/models/{model_id}
POST /v1/messages
POST /v1/messages/count_tokens
```

The `/v1/models` and `/v1/models/{model_id}` routes return Anthropic-shaped model objects when the request has an `anthropic-version` header. The `/v1/messages` routes are compatibility aliases for Anthropic SDKs.

The Messages facade translates Anthropic requests to the gateway's canonical chat-completions body in `gateway/protocols/anthropic.py`, then forwards through the normal chat path. It supports text messages, system prompts, image content blocks through OpenAI-compatible multimodal chat content, tools, tool choice, streaming SSE, visible thinking blocks, model listing and a best-effort `count_tokens` estimator.

Auth uses the normal gateway API key handling. Runtime policy is evaluated with the `anthropic.messages` protocol and the route intent before any chat request is forwarded.

Known limits:

- `max_tokens` is required because Anthropic Messages requires it.
- `document` content blocks are rejected.
- Prior `thinking` and `redacted_thinking` blocks are dropped because local backends cannot use signed Anthropic thinking blocks.
- Token counting is an estimate based on request shape, not a backend tokenizer.
- Anthropic batch, files and server-tool surfaces are not implemented here.

Evidence:

- Route registration and auth/policy: `proxy.py` around `anthropic_models`, `anthropic_count_tokens` and `anthropic_messages`.
- Translation rules: `gateway/protocols/anthropic.py`.
- Schema and stream conformance: `tests/test_anthropic_schema_conformance.py`.
- SDK probes: `tests/compat/python/probe_anthropic.py` and `tests/compat/node/anthropic.test.mjs`.

## Ollama API

Routes:

```text
POST /api/chat                 POST /ollama/api/chat
POST /api/generate             POST /ollama/api/generate
POST /api/embed                POST /ollama/api/embed
POST /api/embeddings           POST /ollama/api/embeddings
GET  /api/tags                 GET  /ollama/api/tags
POST /api/show                 POST /ollama/api/show
GET  /api/ps                   GET  /ollama/api/ps
GET  /api/version              GET  /ollama/api/version
POST /api/create               POST /ollama/api/create
POST /api/copy                 POST /ollama/api/copy
POST /api/pull                 POST /ollama/api/pull
POST /api/push                 POST /ollama/api/push
HEAD /api/blobs/{digest}       HEAD /ollama/api/blobs/{digest}
POST /api/blobs/{digest}       POST /ollama/api/blobs/{digest}
DELETE /api/delete             DELETE /ollama/api/delete
```

The read and inference routes translate Ollama request and response bodies in `gateway/protocols/ollama.py`. Chat and generate use the gateway chat path. Embed and embeddings use the gateway embeddings path. Streaming returns Ollama NDJSON. Empty chat or generate requests act as Ollama load or unload probes and do not call the chat backend.

Auth uses the normal gateway API key handling. The discovery routes can be made bearer-free only when `auth.ollama_compat_discovery` or `OLLAMA_COMPAT_DISCOVERY` enables that behavior. Runtime policy is evaluated with Ollama-specific protocol names such as `ollama.chat`, `ollama.generate`, `ollama.embeddings` and `ollama.models`.

Known limits:

- Model file mutation is deliberately unsupported. `create`, `copy`, `pull`, `push`, `delete` and `blobs` return `501` with an Ollama-shaped `{"error": ...}` body after auth and policy checks.
- Raw prompting is rejected.
- Native `options` keys are mapped only when they have a canonical chat equivalent. Unmapped native options are rejected on the route before forwarding.
- `suffix` and `template` on `/api/generate` are rejected because the gateway does not expose fill-in-the-middle or template override behavior through this frontend.

Evidence:

- Route registration: `proxy.py` from `ollama_chat` through `ollama_delete`.
- Translation rules: `gateway/protocols/ollama.py`.
- Offline API conformance: `tests/test_ollama_api_conformance.py`.
- SDK probes: `tests/compat/python/probe_ollama.py` and `tests/compat/node/ollama.test.mjs`.

## OpenAI Responses

Routes:

```text
POST   /v1/responses
POST   /openai/v1/responses
GET    /v1/responses/{response_id}
GET    /v1/responses/{response_id}/input_items
POST   /v1/responses/input_tokens
POST   /v1/responses/{response_id}/cancel
DELETE /v1/responses/{response_id}
```

Every listed `/v1/responses` route also has an `/openai/v1/responses` alias.

The Responses facade translates to chat completions in `responses_translator.py`. It supports string and item-array input, developer messages mapped to system messages, multimodal image inputs mapped to chat content parts, function tools, tool choice, JSON text formats, reasoning effort, streaming Responses SSE, stored responses, `previous_response_id`, retrieval, input-item listing and deletion.

Stored responses are private to the API key that created them. The route stores requests and translated responses in `responses_store.py` when `store` is true or omitted.

Known limits:

- Hosted tools such as web search, file search, code interpreter, computer-use, MCP, shell and image generation are rejected with `hosted_tool_not_supported`.
- Background mode and other unsupported Responses operations are rejected with `unsupported_operation`.
- `POST /v1/responses/input_tokens` returns an unsupported-operation error because the gateway has no Responses tokenizer.
- `POST /v1/responses/{response_id}/cancel` returns an unsupported-operation error for locally translated Responses.
- The gateway does not run a server-side tool loop. If the model emits a function call, the client must send the function result in the next request.

Evidence:

- Route registration and storage ownership checks: `proxy.py` around `create_response`, `retrieve_response`, `response_input_items`, `responses_input_tokens`, `cancel_response` and `delete_response`.
- Translation rules: `responses_translator.py`.
- Store: `responses_store.py`.
- Conformance tests: `tests/test_openai_responses_conformance.py` and `tests/test_responses_translator.py`.

## A1111 `/sdapi/v1`

Routes:

```text
GET  /sdapi/v1/options
POST /sdapi/v1/options
GET  /sdapi/v1/sd-models
GET  /sdapi/v1/samplers
GET  /sdapi/v1/schedulers
GET  /sdapi/v1/upscalers
GET  /sdapi/v1/latent-upscale-modes
GET  /sdapi/v1/sd-vae
GET  /sdapi/v1/progress
POST /sdapi/v1/txt2img
POST /sdapi/v1/interrupt
POST /sdapi/v1/img2img
POST /sdapi/v1/extra-single-image
POST /sdapi/v1/extra-batch-images
POST /sdapi/v1/interrogate
POST /sdapi/v1/png-info
```

The last five POST routes return `501` with a reason.

The A1111 facade is a thin subset for clients such as SillyTavern that expect Stable Diffusion WebUI routes at the host root. `txt2img` maps the active A1111 checkpoint to a synchronous `image.generate` workflow and calls `/v1/images/generations` in process. The response returns base64 images and an A1111-style `info` string. Per-key session state remembers the selected checkpoint.

Auth accepts the normal gateway `Authorization: Bearer` and `x-api-key` forms. It also accepts SillyTavern-style Basic auth and treats the password, or the whole decoded string when no colon is present, as the gateway API key. The core image request is forwarded with that key as a bearer token, so normal app profile policy and receipts still apply.

Known limits:

- Only synchronous `image.generate` workflows appear as A1111 checkpoints.
- Output-changing fields that the gateway cannot honor are rejected with `422`. This includes hires fix, face restoration, ADetailer and scripts, styles, refiner checkpoint, tiling, variation seeds, seed resizing, LoRA/LyCORIS and hypernetwork prompt tags.
- Model-specific knobs with no effect on the selected workflow are ignored and named in `x-azizam-ignored-params`.
- Upscaling and img2img are not implemented in this facade. Use the gateway's native image edit or workflow routes for those operations.

Evidence:

- Registration in the real app: `proxy.py` around `a1111_api.register_a1111_routes`.
- Route implementation and auth shim: `gateway/a1111_api.py`.
- Unit tests: `tests/test_a1111_api.py`.
- Real-app wiring tests: `tests/test_mcp_a1111_wiring.py`.

## OpenAI Realtime WebSocket

Routes:

```text
WS /v1/realtime
WS /openai/v1/realtime
```

The gateway does not implement the Realtime protocol itself. It authenticates and authorizes the client, starts or checks the configured sibling Realtime service, opens an upstream WebSocket to that service, and relays frames both ways. Query parameters are forwarded except `api_key`, which is deliberately not sent upstream.

Auth uses the normal gateway key handling. Browser clients can pass the key in an OpenAI-compatible subprotocol named `openai-insecure-api-key.<key>`. If the client offers the `realtime` subprotocol, the gateway echoes `realtime` and does not echo the key subprotocol.

Runtime policy is checked for all routes the sibling service needs: `/v1/audio/transcriptions`, `/v1/chat/completions` and `/v1/audio/speech`.

Known limits:

- A configured `realtime` backend service with a `port` is required. Without it, the route refuses the upgrade with `503 realtime_not_configured`.
- If the sibling service cannot start or refuses the connection, the route refuses the upgrade with `503`.
- The gateway relays protocol frames. It does not validate or synthesize Realtime event semantics.

Evidence:

- WebSocket route and policy checks: `proxy.py` around `realtime_v1`.
- Relay helpers: `gateway/realtime_proxy.py`.
- Route tests: `tests/test_realtime_proxy.py`.

## MCP Server

Routes:

```text
POST   /mcp
GET    /.well-known/oauth-protected-resource/mcp
GET    /.well-known/oauth-protected-resource
GET    /mcp
DELETE /mcp
```

`GET /mcp` and `DELETE /mcp` return `405`.

The MCP facade is a Streamable HTTP JSON-RPC endpoint. It supports the 2026-07-28 stateless revision and legacy handshake revisions 2025-11-25, 2025-06-18 and 2025-03-26. It exposes three generated meta-tools: `capabilities`, `run` and `job`.

Tool calls dispatch in process to existing gateway routes with the caller's credentials. `capabilities` reads the workflow catalog and stable aliases. `run` starts a workflow or the speech route. `job` polls, waits for or cancels a media job. Media results are returned as MCP `resource_link` blocks with signed asset URLs.

Auth uses bearer API keys in headers. Missing credentials return a `WWW-Authenticate` challenge pointing at the protected-resource metadata route. Browser origins must be same-origin unless configured in `mcp.allowed_origins`.

Known limits:

- There is no server-sent event stream on `GET /mcp`; all responses are plain JSON.
- The server does not mint an `Mcp-Session-Id`.
- Only the three meta-tools are exposed. It does not generate one tool per workflow.
- Feature limits come from the core route that a tool calls, and are returned as tool errors.

Evidence:

- Registration in the real app: `proxy.py` around `mcp_server.register_mcp_routes`.
- MCP transport and tools: `gateway/mcp_server.py`.
- Unit tests: `tests/test_mcp_server.py`.
- Real-app wiring tests: `tests/test_mcp_a1111_wiring.py`.

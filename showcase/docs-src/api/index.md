---
title: API overview
summary: Choose a protocol and base URL, authenticate with an app key, and handle errors.
---

# API overview

The gateway speaks several protocols at one address. Use the protocol your app or SDK already knows.

The full list of the gateway's own routes is in its [OpenAPI document](/openapi.json). Each gateway serves its own copy at `/openapi.json`. Some compatibility routes are not in it; their pages say so.

## Set up your environment

The examples in this section read three environment variables:

```sh
export AZIZAM_BASE_URL='http://127.0.0.1:5000'
read -r -s -p 'App key: ' AZIZAM_API_KEY; printf '\n'; export AZIZAM_API_KEY
export AZIZAM_MODEL='my-chat'
```

- `AZIZAM_BASE_URL` is the gateway address, with no trailing slash. Use the address the owner gave you.
- `AZIZAM_API_KEY` is your app key. Do not use an admin key or a provider's key.
- `AZIZAM_MODEL` is a model name that your key can use. `my-chat` is an example. Find real names with the request below.

Keep keys out of source control, shared logs and screenshots.

## Choose a base URL

| Client | Base URL |
| --- | --- |
| OpenAI SDKs and apps | `$AZIZAM_BASE_URL/openai/v1` (`/v1` also works) |
| Anthropic SDKs | `$AZIZAM_BASE_URL/anthropic` |
| Ollama clients that add `/api` | `$AZIZAM_BASE_URL/ollama` |
| A1111 clients | `$AZIZAM_BASE_URL` (routes under `/sdapi/v1`) |
| MCP clients | `$AZIZAM_BASE_URL/mcp`, with Streamable HTTP |

## Check access and find models

```sh
curl --fail-with-body -sS "$AZIZAM_BASE_URL/openai/v1/models" \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
```

The response lists the models your key can use. Model names depend on each gateway's settings.

For the gateway's own discovery, use `/gateway/v1/capabilities`. If a model or protocol is missing, the app's [access plan](../concepts/access-plans.md) may not allow it. Changing the plan does not need a new key.

## Handle errors

Check both the HTTP status and the `code` in the body. [Error codes](../reference/errors.md) lists the shapes and codes. Compatibility routes keep their protocol's error format.

| Status | Meaning | What to do |
| --- | --- | --- |
| 400, 422, 501 | The feature or input is not supported. | Check the protocol page. Do not retry unchanged. |
| 401 | The key is missing or not valid. | Check the key. |
| 413 | The request is too large. | See [request limits](../reference/configuration.md#request-limits). |
| 429, 503 | The gateway, an engine or a provider is busy, or the gateway is draining. | Wait for `Retry-After`, then retry. |

Follow `Retry-After` when it is present. If you lose the response to a media request, retry with the same idempotency key and the same request. See [Media jobs](media-jobs.md#retry-safely).

## Protocols

- [OpenAI-compatible API](openai.md): chat, Responses, embeddings, images, audio and video.
- [Anthropic-compatible API](anthropic.md): Messages.
- [Ollama-compatible API](ollama.md): chat, generate and embeddings.
- [A1111-compatible API](a1111.md): text to image for SD WebUI clients.
- [Media jobs](media-jobs.md): picture work that survives a lost connection.
- [MCP for agents](mcp.md): media tools for agents.
- [Client setup examples](clients.md): settings for common chat apps and editors.

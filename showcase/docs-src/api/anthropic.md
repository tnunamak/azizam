---
title: Anthropic-compatible API
summary: Send Messages requests with Anthropic SDKs, and know what is translated.
---

# Anthropic-compatible API

Set your SDK's base URL to `$AZIZAM_BASE_URL/anthropic`. The Messages route is `/anthropic/v1/messages`, and `/v1/messages` also works.

Send your app key as `x-api-key` or as a bearer token. Set the environment variables in the [API overview](index.md#set-up-your-environment) first.

## Messages with curl

```sh
curl --fail-with-body -sS "$AZIZAM_BASE_URL/anthropic/v1/messages" \
  -H "x-api-key: $AZIZAM_API_KEY" \
  -H 'Content-Type: application/json' \
  --data @- <<EOF
{"model": "$AZIZAM_MODEL", "max_tokens": 256,
 "messages": [{"role": "user", "content": "Say hello in one sentence."}]}
EOF
```

## Messages with Python

Install the `anthropic` package first.

```python
import os
from anthropic import Anthropic

client = Anthropic(
    base_url=os.environ["AZIZAM_BASE_URL"].rstrip("/") + "/anthropic",
    api_key=os.environ["AZIZAM_API_KEY"],
)
message = client.messages.create(
    model=os.environ["AZIZAM_MODEL"],
    max_tokens=256,
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
)
for block in message.content:
    if block.type == "text":
        print(block.text)
```

## What works

The gateway translates Messages requests into its chat pipeline. Streaming, system prompts, multi-turn history, image blocks and tool calls work, including tool results, tool choice and streamed tool arguments.

Models are listed at `/anthropic/v1/models`.

Token counting at `/anthropic/v1/messages/count_tokens` is an estimate: about four characters per token.

## Limits

- Document blocks and server tools return HTTP 400.
- Web search, computer use, citations, batches and files are not supported.
- `cache_control` markers and `anthropic-beta` headers do not turn on prompt caching or beta features. Cache usage fields are absent.
- A stop sequence can stop the reply, but the response does not say which one matched.
- Thinking output has an empty signature. Earlier thinking blocks in the history are dropped.

Errors use the Anthropic shape and include a `request-id` header. The request ID does not link to a gateway receipt.

| Status | Error type |
| --- | --- |
| 400 | `invalid_request_error` |
| 401 | `authentication_error` |
| 402 | `billing_error` |
| 403 | `permission_error` |
| 404 | `not_found_error` |
| 413 | `request_too_large` |
| 429 | `rate_limit_error` |
| 529 | `overloaded_error` |
| Other 5xx | `api_error` |

These routes are in the gateway's [OpenAPI document](/openapi.json).

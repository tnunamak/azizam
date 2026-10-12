---
title: Ollama-compatible API
summary: Use Ollama clients for chat, generate and embeddings, without model file management.
---

# Ollama-compatible API

For a client that adds `/api` to its base URL, use `$AZIZAM_BASE_URL/ollama`. For a client that always calls `/api` at the root of the host, use `$AZIZAM_BASE_URL`.

Send your app key as a bearer token or as `x-api-key`. The gateway needs a key, unless the owner allows anonymous model discovery for `tags` and `version`. Set the environment variables in the [API overview](index.md#set-up-your-environment) first.

## Chat with curl

This example turns streaming off, so the response is one JSON object.

```sh
curl --fail-with-body -sS "$AZIZAM_BASE_URL/ollama/api/chat" \
  -H "Authorization: Bearer $AZIZAM_API_KEY" \
  -H 'Content-Type: application/json' \
  --data @- <<EOF
{"model": "$AZIZAM_MODEL", "stream": false,
 "messages": [{"role": "user", "content": "Say hello in one sentence."}]}
EOF
```

## Chat with Python

Install the `ollama` package first.

```python
import os
from ollama import Client

client = Client(
    host=os.environ["AZIZAM_BASE_URL"].rstrip("/") + "/ollama",
    headers={"Authorization": "Bearer " + os.environ["AZIZAM_API_KEY"]},
)
response = client.chat(
    model=os.environ["AZIZAM_MODEL"],
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
    stream=False,
)
print(response.message.content)
```

## What works

Chat and generate stream by default, as newline-delimited JSON. Tools, images, JSON and schema output, and thinking work. The routes also include `embed`, `embeddings`, `tags`, `show`, `ps` and `version`.

Sampling options go to the engine. Read a model's details with `/ollama/api/show`.

## Limits

- `pull`, `push`, `create`, `copy`, `delete` and blobs return HTTP 501. The gateway does not manage model files.
- Runtime options such as `num_ctx`, `num_gpu` and `num_thread` are accepted and ignored. Set them on the engine.
- `suffix`, `template` and `raw` return HTTP 400.
- A load or unload request with an empty prompt gets a normal response, but does not load or free a model.

These routes are in the gateway's [OpenAPI document](/openapi.json). See [Client setup examples](clients.md) for client settings.

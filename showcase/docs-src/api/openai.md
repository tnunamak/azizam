---
title: OpenAI-compatible API
summary: Use Chat Completions, Responses and the media routes with OpenAI SDKs.
---

# OpenAI-compatible API

Set your SDK's base URL to `$AZIZAM_BASE_URL/openai/v1` and its API key to your app key. `/v1` also works as a base URL.

The routes cover chat completions, legacy completions, embeddings, models, images, audio and video. Which models and media tasks work depends on the gateway's engines and workflows.

Set the environment variables in the [API overview](index.md#set-up-your-environment) first.

## Chat with curl

```sh
python3 - <<'PY' | curl --fail-with-body -sS "$AZIZAM_BASE_URL/openai/v1/chat/completions" -H "Authorization: Bearer $AZIZAM_API_KEY" -H 'Content-Type: application/json' --data-binary @-
import json, os
print(json.dumps({"model": os.environ["AZIZAM_MODEL"], "messages": [{"role": "user", "content": "Say hello in one sentence."}]}))
PY
```

Add `"stream": true` to get the reply as a stream of events.

## Chat with Python

Install the `openai` package first.

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url=os.environ["AZIZAM_BASE_URL"].rstrip("/") + "/openai/v1",
    api_key=os.environ["AZIZAM_API_KEY"],
)
response = client.chat.completions.create(
    model=os.environ["AZIZAM_MODEL"],
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
)
print(response.choices[0].message.content)
```

## Responses

The Responses API runs through the same chat pipeline. It supports streaming, function tools, image inputs, reasoning items and structured text output.

```python
response = client.responses.create(
    model=os.environ["AZIZAM_MODEL"],
    input="Say hello in one sentence.",
)
print(response.output_text)
```

You can retrieve or delete a stored response, and continue from it with `previous_response_id`. A stored response belongs to the app and person that created it. Any key of that app can read it. For anyone else, `previous_response_id` returns HTTP 404 with `previous_response_not_found`.

## Limits

- Assistants, files, batches and fine-tuning are not available. Their routes return HTTP 400 with `unsupported_operation`.
- Responses does not support `input_file`, `background`, `conversation`, `prompt`, cancel or input token counting.
- Hosted tools are left out, and the `x-azizam-omitted-tools` header names them. Forcing a hosted tool returns HTTP 400.
- Reasoning summaries are not produced.
- Realtime over WebSocket needs a speech engine. Realtime over HTTP, client secrets, SIP and WebRTC are not supported.
- Music and sound effects at `/v1/audio/generate` are a gateway extension, not part of the OpenAI SDK.

For picture work that should survive a lost connection, use [Media jobs](media-jobs.md). For every route and field, see the gateway's [OpenAPI document](/openapi.json).

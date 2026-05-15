# Local Client Recipes

Date: 2026-05-15
Status: operational, scoped to the first real local instance.

This document is the practical client-side companion to `AUTH_GUIDE.md`,
`docs/API_TARGETS.md`, and `docs/CORE_CONTRACTS.md`. It tells someone setting
up a chat UI, coding agent, or SDK what URL to point at, what protocol surface
is actually live today, and which features will *not* work yet.

All examples assume:

```text
Local base host:   http://127.0.0.1:5000
Public base host:  https://openai.vivid.fish     # optional, when exposed
Client API key:    opk_...                       # never commit, never log
```

Generate a key once per agent or app, as documented in `AUTH_GUIDE.md`:

```bash
./venv/bin/python proxy.py generate-key <agent-or-app-name>
```

Then export it in the shell you run smoke commands from:

```bash
export OPENAI_PROXY_API_KEY='opk_...'
```

> The `$OPENAI_PROXY_API_KEY` placeholder is used throughout. Do not paste
> real `opk_...` values into config files, prompts, or commit messages.

## Live Surfaces Today

The proxy currently exposes (live, mounted in `proxy.py`):

| Path | Protocol | Notes |
|---|---|---|
| `GET /health` | — | Public, no auth. |
| `POST /v1/chat/completions` | OpenAI Chat | Streaming + non-streaming. |
| `POST /v1/completions` | OpenAI Completions | Legacy, supported. |
| `POST /v1/embeddings` | OpenAI Embeddings | |
| `GET /v1/models` | OpenAI Models | Aliases + backend models. |
| `POST /v1/responses` | OpenAI Responses | Translator over chat completions; durable store. |
| `GET /v1/responses/{id}` | OpenAI Responses | Read back. |
| `DELETE /v1/responses/{id}` | OpenAI Responses | |
| `POST /v1/images/generations` | OpenAI Images | Emits observational gateway receipts. |
| `POST /v1/images/edits` | OpenAI Images | Emits observational gateway receipts. |
| `GET /v1/images/{id}/content` | OpenAI Images | Asset retrieval. |
| `POST /v1/audio/speech` | OpenAI Audio | TTS. |
| `POST /v1/audio/transcriptions` | OpenAI Audio | STT. |
| `GET /v1/voices`, `GET /v1/audio/voices` | OpenAI Audio | Voice list. |
| `POST /v1/audio/generate`, `GET /v1/audio/models` | Native | Music/SFX/voice models. |
| `POST /v1/videos`, `GET /v1/videos/{id}`, `GET /v1/videos/{id}/content` | OpenAI-shaped video | |
| `GET /v1/status` | Native | Operational status. |
| `POST /v1/messages` | Anthropic Messages | Non-streaming translator over chat completions. |
| `GET /api/tags` | Ollama-compatible | Model list from configured catalog. |
| `POST /api/chat`, `POST /api/generate` | Ollama-compatible | Non-streaming translators over chat completions. |

Gateway-native discovery, receipts, and media-job state (`gateway/api.py`) are
mounted into the live app:

```text
GET /gateway/v1/workflows
GET /gateway/v1/workflows/{id}
GET /gateway/v1/capabilities
GET /gateway/v1/receipts
GET /gateway/v1/receipts/{request_id}
POST /gateway/v1/media/jobs
GET /gateway/v1/media/jobs
GET /gateway/v1/media/jobs/{job_id}
POST /gateway/v1/media/jobs/{job_id}/events
POST /gateway/v1/media/jobs/{job_id}/cancel
```

Capability and receipt providers are wired conditionally. These endpoints may
return empty catalogs when no matching data exists, but the router itself is
live and authenticated.

### Live with documented limits

The Anthropic and Ollama frontends are live, but intentionally narrow:

- Streaming is rejected on `/v1/messages`, `/api/chat`, and `/api/generate`.
  Use `stream: false` or omit streaming on these protocol surfaces.
- Anthropic image/document/thinking blocks are rejected by the translator
  instead of silently dropped. Anthropic-native prompt caching markers and beta
  headers are not represented.
- Ollama native model management is not implemented: `/api/show`, `/api/ps`,
  `/api/pull`, `/api/push`, `/api/copy`, `/api/delete`, and `/api/create`.
- Ollama's common generation options such as `temperature`, `top_p`,
  `num_predict`, `seed`, `stop`, `presence_penalty`, and
  `frequency_penalty` are mapped. Backend-native options such as `top_k`,
  `num_ctx`, GPU placement, and memory flags are rejected because they do not
  have a portable canonical meaning yet.

For maximum compatibility, point general clients at the OpenAI-compatible
surface and use the Anthropic/Ollama frontends only when a client specifically
requires those protocols.

## Recipe: Open WebUI

Open WebUI talks to any OpenAI-compatible server. The OpenAI provider is the
best-supported path. Open WebUI's Ollama provider can use `/api/tags` and
non-streaming `/api/chat`, but its model-management panels will fail because
those routes are deliberately not implemented.

**Settings → Connections → OpenAI:**

```text
Base URL:  http://127.0.0.1:5000/v1
API Key:   $OPENAI_PROXY_API_KEY
```

Then refresh models; Open WebUI will call `GET /v1/models` and populate the
selector with whatever aliases and backend models the proxy advertises.

Smoke test from the same host:

```bash
curl -s http://127.0.0.1:5000/v1/models \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  | head -c 400
```

Expected unsupported features in Open WebUI against this proxy today:

- Ollama-specific UI panels (pull, push, model management) are not implemented;
  manage models on the backend that actually hosts them.
- Streaming chat through the Ollama provider is not supported. Use Open WebUI's
  OpenAI provider for streaming.
- Web search / RAG features that depend on Open WebUI's own pipelines work
  normally; they are client-side.
- Image generation works only if the configured image backend is reachable.

## Recipe: LibreChat

LibreChat reads its provider list from `librechat.yaml`. Add a `custom`
endpoint pointing at the proxy:

```yaml
endpoints:
  custom:
    - name: "openai-proxy"
      apiKey: "${OPENAI_PROXY_API_KEY}"
      baseURL: "http://127.0.0.1:5000/v1"
      models:
        default: ["auto"]
        fetch: true
      titleConvo: true
      modelDisplayLabel: "Local Proxy"
```

Set `OPENAI_PROXY_API_KEY` in the LibreChat `.env`. Do not paste the literal
`opk_...` into `librechat.yaml`.

Expected unsupported features:

- LibreChat's Anthropic provider block can target `POST /v1/messages` if that
  provider can run non-streaming. If LibreChat forces streaming for the flow,
  route Claude-shaped traffic through the OpenAI Chat surface instead.
- Plugin/Tools that hit OpenAI-only endpoints (e.g. assistant beta) — the
  proxy does not implement Assistants; the Responses surface is the closest
  live equivalent.

## Recipe: Continue (VS Code / JetBrains)

`~/.continue/config.json`:

```jsonc
{
  "models": [
    {
      "title": "Local Proxy",
      "provider": "openai",
      "model": "auto",
      "apiBase": "http://127.0.0.1:5000/v1",
      "apiKey": "$OPENAI_PROXY_API_KEY"
    }
  ],
  "tabAutocompleteModel": {
    "title": "Local Proxy (autocomplete)",
    "provider": "openai",
    "model": "auto",
    "apiBase": "http://127.0.0.1:5000/v1",
    "apiKey": "$OPENAI_PROXY_API_KEY"
  },
  "embeddingsProvider": {
    "provider": "openai",
    "model": "auto",
    "apiBase": "http://127.0.0.1:5000/v1",
    "apiKey": "$OPENAI_PROXY_API_KEY"
  }
}
```

Replace `auto` with whichever alias your `proxy_config.yml` defines. Continue
substitutes `$OPENAI_PROXY_API_KEY` from environment at load time.

Expected unsupported features:

- Continue's "Anthropic" provider can target `POST /v1/messages` only for
  non-streaming calls. Continue commonly streams, so the OpenAI provider remains
  the safer default.
- Continue's Ollama provider works only for non-streaming `/api/chat` and
  `/api/generate`. Model management and native backend options are not
  implemented.
- Reranking provider — only works if a rerank-capable backend is configured.

## Recipe: Cline-style agents (Cline, Roo Code, Kilo, etc.)

These agents accept any OpenAI-compatible endpoint.

```text
API Provider:   OpenAI Compatible
Base URL:       http://127.0.0.1:5000/v1
API Key:        $OPENAI_PROXY_API_KEY
Model ID:       auto   (or whichever alias proxy_config.yml defines)
```

Important caveats:

- Many Cline-style agents assume Anthropic-native tool-use semantics when
  pointed at Claude models. Through the OpenAI Chat surface, tools follow
  OpenAI's `tool_calls` schema. If the agent has a toggle for
  "OpenAI-style tools", enable it.
- Long-context features (prompt caching, beta headers) that depend on
  Anthropic-specific request headers are silently dropped at this surface.

## Recipe: Ollama-compatible clients

The proxy exposes a narrow Ollama-compatible surface backed by the canonical
chat-completions pipeline:

| Path | Status |
|---|---|
| `GET /api/tags` | Live; returns the proxy's configured model catalog. |
| `POST /api/chat` | Live, non-streaming only. |
| `POST /api/generate` | Live, non-streaming only. |
| `/api/show`, `/api/ps`, `/api/pull`, `/api/push`, `/api/copy`, `/api/delete`, `/api/create`, `/api/embeddings` | Not implemented. |

Point the Ollama base URL at `http://127.0.0.1:5000` with no `/v1` suffix.
Send `Authorization: Bearer $OPENAI_PROXY_API_KEY`; this is not a vanilla
unauthenticated Ollama daemon.

```bash
curl -fsS http://127.0.0.1:5000/api/tags \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" | head -c 400
```

Use the OpenAI-compatible surface for streaming, model management, or clients
that depend on backend-native Ollama options.

## Recipe: Anthropic SDK clients

The Anthropic Messages surface (`POST /v1/messages`) is live as a non-streaming
translator over the canonical chat-completions pipeline:

```python
import os
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:5000",
    api_key=os.environ["OPENAI_PROXY_API_KEY"],
)

msg = client.messages.create(
    model="auto",
    max_tokens=256,
    messages=[{"role": "user", "content": "ping"}],
)
print(msg.content[0].text)
```

Do not use streaming calls such as `client.messages.stream(...)`; the proxy
rejects `stream: true` on this surface today. Do not put real Anthropic keys
into `proxy_api_keys.yml`.

## Recipe: OpenAI SDK clients

This is the best-supported path.

Python:

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:5000/v1",
    api_key=os.environ["OPENAI_PROXY_API_KEY"],
)

resp = client.chat.completions.create(
    model="auto",
    messages=[{"role": "user", "content": "ping"}],
)
print(resp.choices[0].message.content)
```

Node:

```js
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://127.0.0.1:5000/v1",
  apiKey: process.env.OPENAI_PROXY_API_KEY,
});

const r = await client.chat.completions.create({
  model: "auto",
  messages: [{ role: "user", content: "ping" }],
});
console.log(r.choices[0].message.content);
```

Smoke test (no SDK):

```bash
curl -s http://127.0.0.1:5000/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"ping"}]}' \
  | head -c 400
```

Responses API:

```python
r = client.responses.create(
    model="auto",
    input="Summarize the README of this repo in one sentence.",
)
print(r.output_text)
```

The Responses surface is implemented as a translator over
`/v1/chat/completions` with a durable store, so behavior tracks chat
completions plus the Responses object shape.

Expected unsupported features:

- Assistants API — not implemented; use Responses.
- Fine-tuning, files, batches — not implemented.
- Realtime API — not implemented.

## Discovery and Debugging: `/gateway/v1/*`

The read-only gateway router (`gateway/api.py`) is mounted into the live app.
Power users and agents can use it for runtime discovery and post-hoc debugging
without scraping logs.

The audience returned to a client is determined server-side; default is
`client`, which redacts backend paths and policy sources.

### Workflows

```bash
curl -s http://127.0.0.1:5000/gateway/v1/workflows \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Returns the workflows the caller's profile is allowed to see, with portable
inputs and outputs (no backend node IDs at `client` audience).

### Capabilities

```bash
curl -s http://127.0.0.1:5000/gateway/v1/capabilities \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Use this from an agent at startup to decide what intents are actually
available right now. Treat absence of an intent as a hard "not supported"
signal rather than retrying blindly.

### Receipts

After making a request, capture the `request_id` (header or response body
depending on protocol) and:

```bash
curl -s "http://127.0.0.1:5000/gateway/v1/receipts/$REQUEST_ID" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Or list recent:

```bash
curl -s "http://127.0.0.1:5000/gateway/v1/receipts?limit=20" \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

A receipt records: canonical intent, accepted/rejected/ignored inputs,
resolved model or workflow, policy decision summary, manifest ID/version/hash
when one participated, warnings, status, and latency.

At `client` audience, backend identity, raw policy sources, and gateway
filesystem details are redacted. Today, observational receipts are emitted
for `/v1/images/generations` and `/v1/images/edits` only; coverage will
expand as more handlers route through `gateway/receipts.py`.

Agents debugging a "why did this fail?" should:

1. Pull the receipt for the failing request.
2. Inspect `rejected_inputs` and `ignored_inputs` for reasons.
3. Inspect `warnings` for fallback notes.
4. Re-check `capabilities` if the intent appears unsupported.

## Migrating to Profile-Scoped Keys

Generated `opk_...` keys map to downstream credentials and may carry a live
`profile:` block (see `AUTH_GUIDE.md` § "Downstream Credential Mapping"). The
`AppProfile` concept (`gateway/profiles.py`, `gateway/policy.py`) layers a
named policy bundle on top of each client key:

- which protocols a client can use
- which model aliases it sees
- whether cloud fallback is allowed
- which credentials may be used downstream
- how much status and diagnostics it may see
- which native passthrough lanes are allowed

The migration path is:

1. **Do not rotate keys preemptively.** The existing `opk_...` values keep
   working; profile binding is additive in `proxy_api_keys.yml`.
2. **Add a `profile:` field** to each agent entry in `proxy_api_keys.yml`
   referencing a profile ID (e.g. `continue-local`, `image-agent`,
   `experimental-cloud`). The proxy reloads the file without restart.
3. **Use the profile fields in `proxy_api_keys.yml`.** Profiles never contain
   raw secrets; they reference downstream credential slots by name, exactly
   like the existing `downstream:` block.
4. **Verify with `/gateway/v1/capabilities`.** After binding a profile, the
   capabilities listed for that key should narrow or widen to match. If the
   set looks wrong, the profile is misconfigured; do not rotate the key.
5. **Only rotate the `opk_...` key itself** if it was exposed (committed,
   pasted, sent to a third party). Profile changes do not require key
   rotation. Use `proxy.py generate-key <name>` to rotate; the printed
   `opk_...` is shown once and never stored in plaintext.

What this document deliberately does *not* do: print any real key, real
hash, real env name from a downstream service block, or any value from a
live `proxy_api_keys.yml`. The placeholders `opk_...`, `sha256:...`, and
`$OPENAI_PROXY_API_KEY` are the only forms that should appear in shared
docs and commit history.

## Operational Notes

- `/health` is the only unauthenticated endpoint. Use it for liveness in
  Docker/systemd; do not use authenticated endpoints for health checks.
- The proxy reloads `proxy_api_keys.yml` on change. Adding, rotating, or
  revoking a key does not require a restart.
- Streaming clients must respect server-side idle timeouts on
  `/v1/chat/completions` and `/v1/completions`. A long stall in an
  intermediate proxy/CDN will surface as a dropped stream to the SDK.
- When a client misbehaves, check `/v1/status` first, then `/gateway/v1/receipts`
  for the specific `request_id`, before opening logs.

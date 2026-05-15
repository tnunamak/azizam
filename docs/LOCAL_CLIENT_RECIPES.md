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

Read-only gateway-native discovery (`gateway/api.py`, not yet mounted into the
live app) provides:

```text
GET /gateway/v1/workflows
GET /gateway/v1/workflows/{id}
GET /gateway/v1/capabilities
GET /gateway/v1/receipts
GET /gateway/v1/receipts/{request_id}
```

Capability and receipt providers are wired conditionally. When the live app
mounts the router with a receipt store path and capability provider, these
endpoints return data; otherwise they return empty catalogs / protocol-shaped
404s.

### Not yet live as client-facing protocols

These surfaces have pure-Python translators or design docs but are **not**
mounted in `proxy.py` today. Do not configure clients against them yet:

- Anthropic Messages (`POST /v1/messages`) — translator exists in
  `gateway/protocols/anthropic.py`.
- Ollama native (`/api/tags`, `/api/show`, `/api/chat`, `/api/generate`,
  `/api/ps`, `/api/pull`) — translator exists in `gateway/protocols/ollama.py`.
- Generic gateway media-job API (`POST /gateway/v1/media/jobs` and friends) —
  target shape per `docs/API_TARGETS.md`.

Until these are mounted, point Anthropic-shaped and Ollama-shaped clients at
the OpenAI Chat surface instead, or use a client that supports an
OpenAI-compatible endpoint.

## Recipe: Open WebUI

Open WebUI talks to any OpenAI-compatible server. Use the OpenAI provider, not
the Ollama provider (Ollama native is not mounted yet).

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

- Ollama-specific UI panels (pull, push, model management) — not wired.
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

- LibreChat's Anthropic provider block — leave unset until `/v1/messages` is
  mounted. Routing Claude-shaped traffic via OpenAI Chat works for most chat
  use, but loses Anthropic-native tool-use shape.
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

- Continue's "Anthropic" provider — point at OpenAI instead until `/v1/messages`
  is mounted.
- Continue's Ollama provider option — same; `/api/*` is not mounted yet.
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

Ollama native (`/api/*`) is **not mounted** in `proxy.py` today. The
translator in `gateway/protocols/ollama.py` is pure and not wired to a route.

For now:

- If the client supports an OpenAI-compatible endpoint alongside its Ollama
  setting, use the OpenAI mode pointed at `http://127.0.0.1:5000/v1`.
- If the client is Ollama-only, run a real Ollama next to the proxy and treat
  the proxy as a separate provider, or wait for the native surface to be
  mounted.

When `/api/*` is mounted, this section will document the exact path mapping.

## Recipe: Anthropic SDK clients

The Anthropic Messages surface (`POST /v1/messages`) is **not mounted** in
`proxy.py` today. The translator in `gateway/protocols/anthropic.py` is pure.

Two options today:

1. Use the proxy's OpenAI Chat surface and the OpenAI SDK instead. Most
   Claude-shaped behavior works at the chat level (system prompts, multi-turn,
   streaming, OpenAI-shaped tool calls). You lose Anthropic-specific features:
   prompt caching markers, vision-specific content blocks beyond what OpenAI
   accepts, and Anthropic-native tool-use ergonomics.
2. Continue pointing the official Anthropic SDK at Anthropic's hosted API for
   Claude work, and at this proxy only for local-routed OpenAI-shaped
   traffic. Configure the SDK per-call; do not put real Anthropic keys into
   this proxy's key file.

When `/v1/messages` is mounted, the SDK config will be:

```python
# anthropic-sdk-python — when /v1/messages is live
import os
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:5000",
    api_key=os.environ["OPENAI_PROXY_API_KEY"],
)
```

Until then, treat this as a future recipe.

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

Once the read-only gateway router is mounted into the live app (see
`gateway/api.py`), power users and agents can use it for runtime discovery
and post-hoc debugging without scraping logs.

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

Today, generated `opk_...` keys map to the global downstream credentials
configured for the proxy (see `AUTH_GUIDE.md` § "Downstream Credential
Mapping"). The forthcoming `AppProfile` concept (`gateway/profiles.py`,
`gateway/policy.py`) layers a named policy bundle on top of each client key:

- which protocols a client can use
- which model aliases it sees
- whether cloud fallback is allowed
- which credentials may be used downstream
- how much status and diagnostics it may see
- which native passthrough lanes are allowed

When profile-scoped keys land, the migration is:

1. **Do not rotate keys preemptively.** The existing `opk_...` values keep
   working; profile binding is additive in `proxy_api_keys.yml`.
2. **Add a `profile:` field** to each agent entry in `proxy_api_keys.yml`
   referencing a profile ID (e.g. `continue-local`, `image-agent`,
   `experimental-cloud`). The proxy reloads the file without restart.
3. **Define profiles** in the gateway profile catalog (location TBD; tracked
   in `gateway/profiles.py`). Profiles never contain raw secrets — they
   reference downstream credential slots by name, exactly like the existing
   `downstream:` block.
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

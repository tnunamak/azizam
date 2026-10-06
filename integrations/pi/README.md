# pi-provider-azizam

A [Pi](https://pi.dev) package that adds an **Azizam** provider. It reads the gateway's `GET /v1/models` and lists the **chat models** in Pi (and in T3 Code, which drives Pi over RPC), with context window, image input and reasoning taken from the gateway's own metadata. No hand-written model list.

Azizam is the working name of the `openai-proxy` gateway. Any OpenAI-compatible gateway works; metadata-rich ones work best (see [the contract](#the-model-metadata-contract)).

## Install

```bash
pi install npm:pi-provider-azizam          # once published
pi install /path/to/openai-proxy/integrations/pi   # from a checkout
pi -e /path/to/openai-proxy/integrations/pi        # try for one run, no install
```

## Configure

| Variable | Default | Meaning |
|---|---|---|
| `AZIZAM_BASE_URL` | `http://127.0.0.1:5000/v1` | Gateway API root, **including** the `/v1` prefix. Read at every refresh. |
| `AZIZAM_API_KEY` | none | Bearer key. |
| `AZIZAM_MODELS_TIMEOUT_MS` | `10000` | Timeout of the model-list request. |
| `AZIZAM_DEBUG` | unset | Set to `1` to print the filter mode and model count to stderr on each refresh. |

The key can also come from the usual Pi sources: `/login azizam` (stored in `auth.json`), `--api-key`, or `apiKey` in `models.json` (`"$MY_VAR"`, `"${MY_VAR}"`, or `"!command"`):

```json
{ "providers": { "azizam": { "apiKey": "!cat ~/.secrets/azizam-key" } } }
```

The key is never logged and never appears in error messages. A keyless gateway still needs any non-empty key, because Pi hides providers without credentials.

### Override model metadata (`models.json`)

Defaults sent to the gateway for every model (verified against the gateway):

```json
{ "supportsDeveloperRole": false, "supportsReasoningEffort": true, "supportsStrictMode": false,
  "maxTokensField": "max_tokens", "thinkingFormat": "openai" }
```

Override any of them, or any other model field, per model with Pi's normal `~/.pi/agent/models.json`:

```json
{
  "providers": {
    "azizam": {
      "modelOverrides": {
        "coder": { "maxTokens": 4000, "compat": { "supportsReasoningEffort": false } }
      }
    }
  }
}
```

### How models are discovered

1. `GET {AZIZAM_BASE_URL}/models` with `Authorization: Bearer <key>`.
2. Keep the chat models (rules below), map them to Pi models, and save the catalog in Pi's `models-store.json`.
3. On the next start Pi restores the saved catalog first, so models are available offline, then refreshes in the background.
4. If a refresh fails (gateway down, 401, bad URL), Pi shows the error and **keeps the last good catalog**.
5. `--offline` / `PI_OFFLINE=1` skips all network calls and uses the saved catalog.

Pi refreshes in the background, so on a brand-new agent dir the extension also does one 3-second prefetch at load (only when `AZIZAM_API_KEY` is set and Pi is not offline). That makes `--provider azizam --model <id>` work on the very first run. If you supply the key only through `/login` or `models.json`, the first run has no models until the background refresh finishes. Start Pi once, then use `--provider azizam --model <id>`.

Mapping rules (from the contract's client rules):

| Pi field | Source | If missing |
|---|---|---|
| list membership | `type == "language"` | no entry has `type`: entries whose `architecture.output_modalities` is exactly `["text"]`; no metadata at all: **all** entries |
| `input` | `architecture.input_modalities` ∩ {`text`,`image`} | `["text"]` |
| `reasoning` | `tags` contains `reasoning` | `false` |
| `contextWindow` | `context_window` | `128000` |
| `maxTokens` | `max_tokens`, capped at the context window | `8192` (conservative; many backends reject larger values) |
| `cost` | per-token `pricing` (`input`/`output`/`input_cache_read`/`input_cache_write`, or OpenRouter `prompt`/`completion`) × 1,000,000 | `0` |
| `name` | the model `id` | |

Entries that are not objects with a string `id` are ignored. Duplicate ids keep the first one.

## T3 Code

T3 Code starts Pi as an RPC subprocess. Point it at Pi with the provider and model flags in its Pi launch arguments:

```
--provider azizam --model <model-id>
```

The same environment (`AZIZAM_BASE_URL`, `AZIZAM_API_KEY`) must reach that process. The model list T3 shows is Pi's `get_available_models`, so it is the live, filtered Azizam list.

## The model metadata contract

An Azizam gateway makes `/v1/models` self-describing. Every entry keeps the OpenAI fields and adds:

| Field | Meaning |
|---|---|
| `type` | `language`, `embedding`, `reranking`, `image`, `video`, `audio`, `decision` |
| `architecture.input_modalities` / `output_modalities` | OpenRouter vocabulary: `text`, `image`, `audio`, `video`, `file` (output also `embedding`) |
| `context_window` | total tokens |
| `max_tokens` | maximum output tokens per request |
| `tags` | subset of `tool-use`, `reasoning`, `vision`, `file-input` |

`type: "language"` means "callable through `POST /v1/chat/completions` with text in, text out". OpenAI clients ignore the extra keys. The full contract lives in the gateway repository. Fields use Vercel AI Gateway and OpenRouter names, so other clients can use them without a Azizam-specific adapter.

Example entry:

```json
{ "id": "coder", "object": "model", "type": "language",
  "architecture": { "input_modalities": ["text", "image"], "output_modalities": ["text"] },
  "context_window": 200000, "max_tokens": 16000, "tags": ["tool-use", "reasoning", "vision"] }
```

### Other clients

- **Any script or OpenAI SDK**: `client.models.list()` returns the extra keys on each item (`model.model_extra` in the Python SDK). Filter with `type == "language"`.
- **opencode** (custom provider): declare the models under the provider in `opencode.json` (see opencode's custom-provider docs), copying `context_window`, `max_tokens` and the image input modality from the list. Generate the values with `curl -H "Authorization: Bearer $KEY" $BASE/models | jq '.data[] | select(.type=="language")'`.
- **Chat UIs** (Open WebUI, LibreChat and similar): filter the list by `type == "language"` to hide image, audio and embedding models. Use `architecture.input_modalities` to show a vision badge.
- **Gateways without the contract**: apply the same fallback this package uses (output modalities exactly `["text"]`, else show everything).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| No `azizam` models in `/model` | No key: set `AZIZAM_API_KEY`. Or the first refresh has not finished or failed: run with `AZIZAM_DEBUG=1`. |
| `Could not reach the Azizam gateway` | Wrong `AZIZAM_BASE_URL`, or the gateway is down. The last saved catalog stays in use. |
| `rejected the API key (HTTP 401)` | Wrong or expired key. |
| `returned 404 … include the API prefix` | `AZIZAM_BASE_URL` is missing `/v1`. |
| `did not return JSON` | The URL points at a web page, not the API. |
| Every model of the gateway is listed, including image/audio | The gateway does not emit contract metadata yet. Run with `AZIZAM_DEBUG=1` to see `filter: unfiltered`. |
| Requests fail with `max_tokens` or `developer` role errors | Override `compat` / `maxTokens` per model in `models.json` (see above). |
| Models disappear after changing the gateway | Pi restores the old catalog until the next successful refresh. Restart Pi with the new `AZIZAM_BASE_URL`. |

## Development

No build step: Pi loads the TypeScript source directly.

```bash
npm test          # unit + real-Pi RPC tests (+ optional live smoke test)
```

Requires Node ≥ 22.18 and an installed `pi` (`PI_BIN` overrides its location). Tests start a mock gateway on a random port and run `pi --mode rpc --no-session` in a temporary agent dir. The live smoke test (`AZIZAM_LIVE_BASE_URL`, default `http://127.0.0.1:5000/v1`) skips itself when the gateway or a key is unavailable.

Before publishing, replace the `OWNER/REPO` placeholders in `package.json` (`repository`, `homepage`, `bugs`).

## License

Apache-2.0.

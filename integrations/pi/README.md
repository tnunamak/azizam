# pi-provider-azizam

A [Pi](https://pi.dev) package that adds an **Azizam** provider. It reads the gateway's `GET /v1/models` and lists the **chat models** in Pi (and in T3 Code, which drives Pi over RPC), with context window, image input and reasoning taken from the gateway's own metadata. No hand-written model list.

Azizam is the working name of the `azizam` gateway. Any OpenAI-compatible gateway works; metadata-rich ones work best (see [the contract](#the-model-metadata-contract)).

## Install

```bash
pi install npm:pi-provider-azizam          # once published
pi install /path/to/azizam/integrations/pi   # from a checkout
pi -e /path/to/azizam/integrations/pi        # try for one run, no install
```

## Configure

| Variable | Default | Meaning |
|---|---|---|
| `AZIZAM_BASE_URL` | `http://127.0.0.1:5000/v1` | Gateway API root, **including** the `/v1` prefix. Read at every refresh. User info (`user:pass@`), query strings and fragments are rejected, and error messages never repeat the value you set. |
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

**Cold start.** Pi refreshes in the background, but `--provider azizam --model <id>` is resolved before that refresh finishes. So at load the extension builds a short-lived Pi `ModelRuntime` on the same agent dir. It restores the saved catalog and, only if no saved catalog exists for the configured `AZIZAM_BASE_URL`, resolves the key exactly as Pi does (`auth.json` from `/login`, `apiKey` in `models.json` including `!command`, or `AZIZAM_API_KEY`) and fetches once. A usable saved catalog means no extra request at start, so a gateway that is down adds no delay. Pi's normal background refresh still runs afterwards. Nothing is fetched when Pi is offline or no key resolves.

- **Model storage is optional.** An unreadable, locked or corrupt `models-store.json` counts as "no saved catalog". If Pi cannot save the catalog, the fetched models are still served from memory for that run. An empty list from the gateway stays empty.
- **Credential storage is required for prompting.** If Pi cannot read `auth.json` (for example a read-only agent dir), the early fetch falls back to `AZIZAM_API_KEY`, so the models are still discovered and `--provider azizam --model <id>` resolves. That is all this guarantees: Pi reads the same store again to list available models and to authenticate each request, so prompting fails (`Credential store read failed`) until the store is readable.
- **Time limit.** Restore, key lookup and fetch share one 3 second budget. Restore and key lookup get a quarter each, so a stalled store cannot starve the fetch, and the `AZIZAM_API_KEY` fallback always has at least half of the budget. When the budget runs out, the remaining steps are skipped and Pi starts without waiting. One step has no limit we can set: Pi runs a `models.json` `!command` key with a blocking call (Pi caps it at 10 seconds), and nothing can interrupt that. A slow password-manager command can therefore delay the first start by up to 10 seconds. Use `auth.json` (`/login azizam`) or `AZIZAM_API_KEY` to avoid it.
- **Other providers are not touched.** Registering a provider in Pi's `ModelRuntime` normally starts a refresh that checks credentials for every provider, including `!command` keys of unrelated providers. The extension suppresses that one call, so only Azizam's key is resolved. This relies on Pi 1.0.x calling it synchronously inside `registerNativeProvider`; Pi has no scoped registration API.
- **Cost.** On the very first run (no saved catalog), a `models.json` `!command` key for Azizam runs up to twice: once here and once in Pi's own refresh. Later starts use the saved catalog and run it once.

Limits: a key passed only with `--api-key` on the command line is not visible to this early step (the background refresh uses it, so the first run needs the key in env, `/login` or `models.json`). If the gateway is unreachable on the very first run, no models exist until Pi's background refresh succeeds.

Mapping rules (from the contract's client rules):

| Pi field | Source | If missing |
|---|---|---|
| list membership | `type == "language"` | no entry has `type`: entries whose `architecture.output_modalities` is exactly `["text"]`; no metadata at all: **all** entries |
| `input` | `architecture.input_modalities` ∩ {`text`,`image`} | `["text"]` only when the list has neither (`["image"]` stays `["image"]`) |
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

`type: "language"` means "callable through `POST /v1/chat/completions` with text in, text out". OpenAI clients ignore the extra keys. The full contract lives in the gateway repository. Fields use Vercel Azizam and OpenRouter names, so other clients can use them without a Azizam-specific adapter.

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
| No `azizam` models in `/model` | No key: set `AZIZAM_API_KEY`, run `/login azizam`, or set `apiKey` in `models.json`. Or the first refresh has not finished or failed: run with `AZIZAM_DEBUG=1`. |
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
npm test          # unit + real-Pi RPC tests (+ opt-in live smoke test)
```

Requires Node ≥ 22.18 and an installed `pi` (`PI_BIN` overrides its location). Tests start a mock gateway on a random port and run `pi --mode rpc --no-session` in a temporary agent dir. The live smoke test is opt-in: it runs only with `AZIZAM_LIVE_TEST=1` (gateway from `AZIZAM_LIVE_BASE_URL`, default `http://127.0.0.1:5000/v1`; key from `AZIZAM_API_KEY`) and skips itself when the gateway or key is unavailable. Without the variable it contacts nothing.

See [Before publishing](#before-publishing) for the release checklist.

## Compatibility

Tested with Pi 1.0.4. The peer range is `>=1.0.4 <2` for `@earendil-works/pi-ai` and `@earendil-works/pi-coding-agent`. The cold start uses Pi's `ModelRuntime` and relies on `registerNativeProvider` calling `refresh()` synchronously. The real-runtime control test (`test/runtime-isolation.test.ts`) is the upgrade gate: run `npm test` against a new Pi version before you widen the range.

## Before publishing

- [x] `package.json` points at `tnunamak/azizam` (`repository.url`, `homepage`, `bugs.url`).
- [ ] Confirm that you own the npm name `pi-provider-azizam` (or choose another and update the install commands above).
- [ ] Run `npm test` against the newest Pi in the supported range.

## License

Apache-2.0.

# Model metadata in `GET /v1/models`

The OpenAI-shaped model list describes itself. A client can tell chat models from image, video, audio,
embedding, rerank and decision models without matching on names. This page is the contract (v1) as
this gateway implements it. The prior-art reasoning is in the Azizam contract notes: reuse the Vercel AI
Gateway `type`/`tags`/`context_window`/`max_tokens` names and OpenRouter's `architecture.*_modalities`;
invent only `audio` and `decision`.

Code: `gateway/model_metadata.py` (pure derivation), wired in `proxy.py` (`list_models`,
`_model_metadata_sources`, `_declared_model_metadata`, `_catalog_model_metadata`).

## Fields

Every entry keeps `id`, `object: "model"`, `created`, `owned_by` and every existing `vivid_fish_*` field.
The new fields are additive; OpenAI clients ignore unknown keys (checked against the `openai` Python SDK
`Model` type in `tests/test_model_metadata_endpoints.py`).

| Field | Type | Values | When present |
|---|---|---|---|
| `type` | string | `language`, `embedding`, `reranking`, `image`, `video`, `audio`, `decision` | Every entry the gateway can classify. |
| `architecture.input_modalities` | string[] | subset of `text`, `image`, `audio`, `video`, `file` | Whenever `type` is present. |
| `architecture.output_modalities` | string[] | subset of `text`, `image`, `audio`, `video`, `embedding` | Whenever `type` is present. |
| `context_window` | int | total tokens | Already emitted before this contract; unchanged. Omitted if unknown. |
| `max_tokens` | int | max output tokens per request | Only when an operator declares it. Never guessed. |
| `tags` | string[] | subset of `tool-use`, `reasoning`, `vision`, `file-input` | Only declared or derivable facts. Omitted when empty. |

Semantics:

- `type: "language"`: callable through `POST /v1/chat/completions` (and `/v1/responses`), text in, text
  out. A chat client lists exactly these.
- `vision` tag <=> `image` in `input_modalities`. The gateway keeps the two consistent on every entry,
  including image-edit and image-to-video models.
- `reasoning`: the model thinks and honours `reasoning_effort` (the gateway maps it to engine budgets,
  see `reasoning:` in `proxy_config.yml`).
- `tool-use`: OpenAI `tools` / tool calls work through the gateway.
- An alias (`coder`, `image`, `tts`, ...) is typed by what it resolves to. An alias with several
  operations (`image` -> `images` + `image_edits`) takes the type they share and the union of their
  modalities and tags.
- No new field uses a `vivid_fish` name.

Example:

```json
{"id": "coder", "object": "model", "created": 1759700000, "owned_by": "openai",
 "context_window": 202752, "type": "language",
 "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
 "tags": ["tool-use", "reasoning", "vision"]}
```

## How values are derived

Derivation reads only gateway vocabulary, never engine names. The first matching source wins; an
operator declaration then overrides it field by field.

| Entry source | `type` | input -> output |
|---|---|---|
| Workflow intent `image.generate` | `image` | text -> image |
| Workflow intent `image.edit` | `image` | text, image -> image |
| Workflow intent `image.analyze` | `image` | image -> the manifest's output media type (JSON report -> text) |
| Workflow intent `video.generate` | `video` | text (+ image when the manifest has an `asset` input) -> video |
| Workflow intent `video.edit` | `video` | text, video -> video |
| Workflow intent `audio.generate` | `audio` | text -> audio |
| Alias op `chat` | `language` | text -> text (+ image only when the target declares it) |
| Alias op `embeddings` | `embedding` | text -> embedding |
| Alias ops `images` / `image_edits` | `image` | as the intents above |
| Alias op `videos` | `video` | as the target workflow, else text -> video |
| Alias ops `speech` / `audio_generate` | `audio` | text -> audio |
| Alias op `transcriptions` | `audio` | audio -> text |
| Alias op `rerank` | `reranking` | text -> text |
| Decision models | `decision` | text -> text |
| Registry service capability `chat`/`completions` | `language` | text -> text (+ image when the service lists `vision`) |
| Registry service capability `embeddings` / `rerank` / `decisions` | `embedding` / `reranking` / `decision` | |
| LLM catalog (LiteLLM-backed) entries, `current`, `current-fast` | `language` unless the serving registry service says otherwise | text -> text |

Notes:

- A manifest `asset` input may declare `media_type` (for example `media_type: audio/wav`); without it, an
  asset input of a `video.generate` workflow counts as an image.
- Output modalities come from the manifest's `outputs.*.media_type` when present.
- Alias targets resolve against the whole catalog before profile filtering, so a caller's profile never
  changes what an alias reports.
- Context windows keep their existing lookup (`_annotate_model_context_windows`); this contract only
  passes them through.

## Operator declarations (`model_metadata`)

Declared values win over derived values. Declare them as data on any of these existing surfaces; when
one id has several, the most specific wins per field:

1. Service-registry model record: `backends.<service>.models[].model_metadata` in `proxy_config.yml`
   (seed) and the same key on the record in `admin.db` (live). The record matches by `id`,
   `model_group` or `public_name`.
2. A `models:` entry in `proxy_config.yml` with the same `id`. An entry with only `id` and
   `model_metadata` is valid: it annotates any listed id (a workflow, an alias, a decision model) and is
   not added to the GGUF catalog.
3. A `roles.<id>` entry in `proxy_config.yml`.

Keys (all optional):

```yaml
model_metadata:
  type: language                     # one of the seven types
  input_modalities: [text, image]
  output_modalities: [text]
  max_tokens: 8192                   # only if the deployment really caps output
  tags: [reasoning, tool-use, vision]
```

Values outside the vocabulary are ignored. Declaring `tags: [vision]` adds `image` input and declaring
`image` input adds the `vision` tag. Declaring `tags: []` on an alias removes the tags it would inherit.

### Applying a seed change to a running instance

After the first boot, `admin.db` owns each service's `models` list; edits under `backends:` in
`proxy_config.yml` do not reach it. Copy `model_metadata` (and only that key) into the registry with:

```bash
./venv/bin/python scripts/local/sync_model_metadata_from_config.py            # dry run: prints the plan
./venv/bin/python scripts/local/sync_model_metadata_from_config.py --apply    # backs up admin.db, then writes
```

The gateway reads the change on its next start or service hot reload. The script does not restart
anything. `roles:` and `models:` declarations are read from `proxy_config.yml` at start.

### This machine's declarations

`backends.kobold.models` declares, for the local Qwen-family chat models:

| ids | `input_modalities` | `tags` | why |
|---|---|---|---|
| `default`, `coder`, `coder-q5`, `coder-q8`, `coder-unchained`, `rp`, `current` | text, image | reasoning, tool-use, vision | Every record loads the mmproj (image input); llama-bee runs with `--jinja` (native tool calls) and `--reasoning on`, and the gateway maps `reasoning_effort` to a thinking budget. |
| `default-fast`, `current-fast` | text, image | tool-use, vision | Same weights; the record's `reasoning_effort: none` overlay turns thinking off, so clients should not send an effort. |

No `max_tokens` is declared: the `max_tokens: 16384` on `default` is a `default-params` default that
callers override, not a cap. The `vision` and `fast` aliases inherit from `default` and `default-fast`.

## Endpoints that agree

| Endpoint | Behaviour |
|---|---|
| `GET /v1/models`, `GET /openai/v1/models` | Annotated as above. |
| `GET /v1/models/{id}` | Returns the same entry as the list. |
| `GET /me/api/models/catalog` (member Models page) | Alias and model rows carry the same `type`, `architecture`, `max_tokens` and `tags` as `/v1/models` for the same id. |
| `GET /anthropic/v1/models[/{id}]`, or `/v1/models` with `anthropic-version` | Anthropic shape, unchanged; none of these fields are added. |
| Ollama `GET /api/tags` | Ollama shape, unchanged. |
| `GET /v1/models` with a LiteLLM virtual key (delegated) | **Gap:** returns LiteLLM's raw payload unchanged, without these fields. Delegated keys are scoped by LiteLLM itself, and LiteLLM's `/v1/models` does not emit `model_info`, so the gateway does not rewrite that payload. Clients that need the metadata should use a gateway key. |

## Client rules

- Chat model list = entries with `type == "language"`.
- Fallback for gateways without this contract: if no entry has `type`, treat entries whose
  `architecture.output_modalities` is exactly `["text"]` as language models; with no metadata at all,
  list every entry and say so in logs.
- Input modalities for a chat client = `architecture.input_modalities` ∩ the client's supported set,
  default `["text"]`.
- `reasoning` = `tags` contains `reasoning`. Context window = `context_window`. Max output =
  `max_tokens`, else the client's documented default.

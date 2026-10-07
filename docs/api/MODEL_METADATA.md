# Model metadata in `GET /v1/models`

The OpenAI-shaped model list describes itself. A client can tell chat models from image, video, audio,
embedding, rerank and decision models without matching on names. This page is the contract (v1) as
this gateway implements it. The prior-art reasoning is in the Azizam contract notes: reuse the Vercel AI
Gateway `type`/`tags`/`context_window`/`max_tokens` names and OpenRouter's `architecture.*_modalities`;
invent only `audio` and `decision`.

Code: `gateway/model_metadata.py` (pure derivation), wired in `proxy.py` (`list_models`,
`_model_metadata_sources`, `_service_model_index`, `_declared_model_metadata_index`, `_with_loaded_model_inheritance`,
`_catalog_model_metadata`).

## Fields

Every entry keeps `id`, `object: "model"`, `created`, `owned_by` and every existing `azizam_*` field.
The new fields are additive; OpenAI clients ignore unknown keys (checked against the `openai` Python SDK
`Model` type in `tests/test_model_metadata_endpoints.py`).

| Field | Type | Values | When present |
|---|---|---|---|
| `type` | string | `language`, `embedding`, `reranking`, `image`, `video`, `audio`, `decision` | Every entry the gateway can classify. |
| `architecture.input_modalities` | string[] | subset of `text`, `image`, `audio`, `video`, `file` | Whenever `type` is present. |
| `architecture.output_modalities` | string[] | subset of `text`, `image`, `audio`, `video`, `embedding` | Whenever `type` is present. |
| `context_window` | int | total tokens | Already emitted before this contract; unchanged. Omitted if unknown. |
| `max_tokens` | int | max output tokens per request | Only when an operator declares it. Never guessed. |
| `tags` | string[] | subset of `tool-use`, `reasoning`, `vision`, `file-input` | `language` entries only, and only declared or derivable facts. Omitted when empty. |

Semantics:

- `type: "language"`: callable through `POST /v1/chat/completions` (and `/v1/responses`), text in, text
  out. A chat client lists exactly these.
- `tags` are chat-model capability flags and appear only on `type: "language"` entries. For those,
  `vision` tag <=> `image` in `input_modalities`, and the gateway keeps the two consistent. Media
  models (image edit, image-to-video, ...) state image input in `architecture.input_modalities` only;
  the gateway drops any tags declared on them.
- `reasoning`: the model thinks and honours `reasoning_effort` (the gateway maps it to engine budgets,
  see `reasoning:` in `proxy_config.yml`).
- `tool-use`: OpenAI `tools` / tool calls work through the gateway.
- An alias (`coder`, `image`, `tts`, ...) is typed by what it resolves to. An alias with several
  operations (`image` -> `images` + `image_edits`) takes the type they share and the union of their
  modalities and tags.
- A dynamic alias (`current`, `current-fast`: whatever local LLM is loaded) inherits the metadata of
  the loaded model. See "Dynamic aliases".
- All gateway-owned fields use an `azizam` name.

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
| Workflow intent `video.edit` | `video` | text, video (+ each `asset` input's declared media type) -> video |
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
| LLM catalog (LiteLLM-backed) entries | `language` unless the serving registry service says otherwise | text -> text |
| `current`, `current-fast` | the loaded model's, see "Dynamic aliases" | the loaded model's |

Notes:

- Each manifest `asset` input adds its medium to `input_modalities`. An input may declare it with
  `media_type` (for example `media_type: image/*`). Without it, an asset input counts as an image for
  `image.*` and `video.generate` workflows, a video for `video.edit`, and audio for `audio.generate`.
  A manifest that takes assets of different media (for example `video-scail-2-replace`: a reference
  image and a source video) must declare `media_type` on each one.
- Output modalities come from the manifest's `outputs.*.media_type` when present.
- Alias targets resolve against the whole catalog before profile filtering, so visibility does not
  change how static alias targets resolve. Dynamic aliases inherit only when this request already
  observes a loaded identity for filtering.
- Context windows keep their existing lookup (`_annotate_model_context_windows`); this contract only
  passes them through.

## Dynamic aliases (`current`, `current-fast`)

These aliases send requests to whichever local LLM is loaded. When profile filtering already
observes the loaded identity in this model-list request, metadata uses that same observation.
Declared service models match through chat execution's activation resolver. Metadata adds no
health or loaded-state probes and no cache. Profiles that do not need loaded-state filtering,
and catalog consumers without that observation, use the conservative fallback.

- The aliases inherit `type`, `architecture`, `tags` and `max_tokens` from the `model_metadata`
  declarations of those ids. Only declared ids count. When several are declared, the aliases claim
  only what all of them claim. A record whose own request policy turns thinking off (it defaults
  `reasoning_effort` to `none`, for example with a `default-params` policy overlay) does not count
  against the `reasoning` tag.
- An alias whose own request policy turns thinking off drops the `reasoning` tag. On this machine that
  is `current-fast`, through its `reasoning_effort: none` overlay. The gateway reads this from the
  policy data, not from the alias name.
- `context_window`: a value observed from the running engine wins. Without one, the aliases take the
  smallest `context_window` configured on the loaded model's records, ahead of static alias values.
- When nothing is loaded, or no id of the loaded model is declared (for example `cydonia-24b`), the
  aliases keep their own declaration: text in, text out, no tags.
- The inherited values win over the aliases' own `model_metadata`, which is only the fallback.
- Clients may cache `/v1/models`. After a model swap, a client can see the previous model's values
  until it lists the models again.

List and retrieve endpoints use the same request policy. The member catalog has no loaded-state
filtering observation, so its dynamic aliases use the conservative fallback. Model-ID lists in
response metadata and alias target maps include only IDs visible in `data` under the caller's profile.

## Operator declarations (`model_metadata`)

Declared values win over derived values. Declare them as data on any of these existing surfaces; when
one id has several, the most specific wins per field:

1. Service-registry model record: `backends.<service>.models[].model_metadata` in `proxy_config.yml`
   (seed) and the same key on the record in `admin.db` (live). The record matches the public name
   LiteLLM serves for it (for a remote provider, the provider-prefixed name such as
   `openai/text-embedding-3-small`, never the raw record id). Records of other services match by
   `id`, `model_group` or `public_name`.
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

Values outside the vocabulary are ignored. On a `language` entry, declaring `tags: [vision]` adds
`image` input and declaring `image` input adds the `vision` tag. Declaring `tags: []` on an alias
removes the tags it would inherit. Tags declared on any other type are dropped.

### Applying a seed change to a running instance

After the first boot, `admin.db` owns each service's `models` list; edits under `backends:` in
`proxy_config.yml` do not reach it. Copy `model_metadata` (and only that key) into the registry with:

```bash
./venv/bin/python scripts/local/sync_model_metadata_from_config.py            # dry run: prints the plan
./venv/bin/python scripts/local/sync_model_metadata_from_config.py --apply    # backs up admin.db, then writes
```

The script writes one registry revision and one audit row through the audited compare-and-swap writer,
and nothing else. It does not regenerate LiteLLM's config file and does not trigger a hot reload, so it
is safe against a copied database whose registry points `config_path` at a live file. A second run
reports nothing to change. If the registry changed between the plan and the write, the write fails
with a generation conflict; run the script again.

A running gateway reads the change on its next start. `model_metadata` is not part of LiteLLM's
compiled config, so the restart rewrites LiteLLM's config with the same content and the LiteLLM
watcher does not restart LiteLLM. The safe sequence, from the main checkout after merging:

1. `./venv/bin/python scripts/local/sync_model_metadata_from_config.py` and read the plan: it must list
   only `model_metadata` changes on the records you expect.
2. `./venv/bin/python scripts/local/sync_model_metadata_from_config.py --apply`.
3. `scripts/safe-restart` (gateway only; not `--llama`).
4. `curl -s localhost:5000/v1/models -H "Authorization: Bearer $KEY"` and check the entries.

To undo, stop the gateway, copy the `admin.db.bak.pre-model-metadata-*` backup over `admin.db`, and
start it again. `roles:` and `models:` declarations are read from `proxy_config.yml` at start.

### This machine's declarations

`backends.kobold.models` declares, for the local Qwen-family chat models:

| ids | `input_modalities` | `tags` | why |
|---|---|---|---|
| `default`, `coder`, `coder-q5`, `coder-q8`, `coder-unchained`, `rp` | text, image | reasoning, tool-use, vision | Every record loads the mmproj (image input); llama-bee runs with `--jinja` (native tool calls) and `--reasoning on`, and the gateway maps `reasoning_effort` to a thinking budget. |
| `default-fast` | text, image | tool-use, vision | Same weights; the record's `reasoning_effort: none` overlay turns thinking off, so clients should not send an effort. |
| `current`, `current-fast` | text | none | The fallback only (see "Dynamic aliases"): the values when nothing declared is loaded, for example the text-only `cydonia-24b` (`models:`, no mmproj, no declaration). With a Qwen record loaded they inherit text, image and tool-use, vision, plus reasoning for `current`. |

No `max_tokens` is declared: the `max_tokens: 16384` on `default` is a `default-params` default that
callers override, not a cap. The `vision` and `fast` aliases inherit from `default` and `default-fast`.

## Endpoints that agree

| Endpoint | Behaviour |
|---|---|
| `GET /v1/models`, `GET /openai/v1/models` | Annotated as above. |
| `GET /v1/models/{id}` | Returns the same entry as the list. |
| `GET /me/api/models/catalog` (member Models page) | Alias and model rows carry the same metadata contract. Dynamic aliases use the conservative fallback because this request has no loaded identity. The row's `capability`, `icon_key` and `io` come from those fields, see "Models page classification". |
| `GET /anthropic/v1/models[/{id}]`, or `/v1/models` with `anthropic-version` | Anthropic shape, unchanged; none of these fields are added. |
| Ollama `GET /api/tags` | Ollama shape, unchanged. |
| `GET /v1/models` with a LiteLLM virtual key (delegated) | **Gap:** returns LiteLLM's raw payload unchanged, without these fields. Delegated keys are scoped by LiteLLM itself, and LiteLLM's `/v1/models` does not emit `model_info`, so the gateway does not rewrite that payload. Clients that need the metadata should use a gateway key. |

## Models page classification

The member Models page (`/me/api/models/catalog`) groups rows by a product capability and shows one
icon per row. One classifier, `product_capability` in `gateway/model_presentation.py`, derives both from
this contract: the row's `/v1/models` fields when the list has the id, else the serving capability's
intent and modalities read through `model_metadata.from_capability`.

| `type` | `capability` (and `icon_key`) |
|---|---|
| `language` | `chat` |
| `image` / `video` | `image` / `video` |
| `embedding` / `reranking` / `decision` | `embed` / `rerank` / `decide` |
| `audio`, audio in and no audio out | `transcribe` |
| `audio`, other | the declared subtype (`capability: music` or `sfx` in the workflow manifest, or `speech`), else the speech intent gives `speech`, else `audio` (icon `sfx`) |

A declared `capability` only splits audio. It cannot move a row to another type. The row's `io` is its
`architecture` when the list has the id. The page shows `architecture` as an "inputs → outputs" chip and
each tag as a chip. `tests/test_model_metadata_endpoints.py` fails if a row's capability and its
`/v1/models` `type` disagree.

## Client rules

- Chat model list = entries with `type == "language"`.
- Fallback for gateways without this contract: if no entry has `type`, treat entries whose
  `architecture.output_modalities` is exactly `["text"]` as language models; with no metadata at all,
  list every entry and say so in logs.
- Input modalities for a chat client = `architecture.input_modalities` ∩ the client's supported set,
  default `["text"]`.
- `reasoning` = `tags` contains `reasoning`. Context window = `context_window`. Max output =
  `max_tokens`, else the client's documented default.

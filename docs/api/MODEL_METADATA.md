# Model metadata in `GET /v1/models`

The OpenAI-shaped model list describes itself. A client can tell chat models from image, video, audio,
embedding, rerank and decision models without matching on names. This page is the contract (v1) as
this gateway implements it. The prior-art reasoning is in the Azizam contract notes: reuse the Vercel AI
Gateway `type`/`tags`/`context_window`/`max_tokens` names and OpenRouter's `architecture.*_modalities`;
invent only `audio` and `decision`.

Code: `gateway/model_metadata.py` (pure derivation), wired in `proxy.py` (`list_models`,
`_model_metadata_sources`, `_service_model_index`, `_declared_model_metadata_index`, `_with_loaded_model_inheritance`,
`_catalog_model_metadata`).

At startup, the gateway reads the type, input modality, output modality and tag vocabularies from
the `model_metadata` enums in `registry/defaults/models.schema.json:19-28`, preserving their order.
A missing or unreadable file, invalid JSON, or a missing or malformed metadata enum stops startup
with an error naming the schema file; there is no fallback vocabulary. The loader and startup read
are in `gateway/product_contracts.py:15-60`; metadata derivation loads the same vocabularies in
`gateway/model_metadata.py:79-86` and `gateway/model_metadata.py:184-189`.

## Fields

Every entry keeps `id`, `object: "model"`, `created`, `owned_by` and every existing `azizam_*` field.
The new fields are additive; OpenAI clients ignore unknown keys (checked against the `openai` Python SDK
`Model` type in `tests/test_model_metadata_endpoints.py`).

| Field | Type | Values | When present |
|---|---|---|---|
| `type` | string | <!-- model-metadata:types:start --> `language`, `embedding`, `reranking`, `image`, `video`, `audio`, `decision` <!-- model-metadata:types:end --> | Every entry the gateway can classify. |
| `architecture.input_modalities` | string[] | subset of <!-- model-metadata:input_modalities:start --> `text`, `image`, `audio`, `video`, `file` <!-- model-metadata:input_modalities:end --> | Whenever `type` is present. |
| `architecture.output_modalities` | string[] | subset of <!-- model-metadata:output_modalities:start --> `text`, `image`, `audio`, `video`, `embedding` <!-- model-metadata:output_modalities:end --> | Whenever `type` is present. |
| `context_window` | int | total tokens | Already emitted before this contract; unchanged. Omitted if unknown. |
| `max_tokens` | int | max output tokens per request | Only when an operator declares it. Never guessed. |
| `tags` | string[] | subset of <!-- model-metadata:tags:start --> `tool-use`, `reasoning`, `vision`, `file-input` <!-- model-metadata:tags:end --> | `language` entries only, and only declared or derivable facts. Omitted when empty. |
| `azizam_availability` | object | `{"state": "available" \| "unavailable" \| "unknown", "reason"?: string}` | Every entry the gateway can map to the service or services that serve it. `reason` only when `unavailable`. |

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

## Availability

`azizam_availability` is a hint about the engine behind a name. It is not a promise: the list still means
"names you may use", and every listed name stays callable. The state comes from the cached status snapshot and
the request receipts, the same evidence the capability and Models pages use. The availability pass reads cached
evidence only: it starts no probe and waits for no status build. (The context-window annotation in the same
request can still read an engine when its answer is not cached; that is separate.) A missing or old snapshot reads
as `unknown`. Each list request keeps the snapshot refreshing, so after a quiet spell the first list can read
`unknown` and the lists after it carry states.

- A name maps to its services by the model name a service serves, by the workflow it is, or through the names an
  alias resolves to. Each backend that can run a workflow is one way to run it: its engine plus every service that
  backend's binding requires, all needed at once. The workflow is `available` if any way is, and `unavailable`
  only if every way is. A model on gateway lifecycle routing maps to the backend that routing sends it to, not to
  the service that declares it.
  A service that is turned off, or that is a remote provider, still counts for this mapping (a turned-off service
  reads as `unavailable`). A turned-off service marks a name only when no enabled service serves that name.
  The names the gateway's own `current` route answers (`current`, `current-fast`) resolve to a backend at request
  time, so they are judged by the LLM backends that route tries, never by a record that lists the same model id.
  An alias target that maps to no service counts as `unknown`; an entry where nothing maps has no field.
- An engine-operation alias (speech, transcriptions, rerank, decisions) resolves to an ordered list of engines,
  each served by one registry service. It maps to the services of its deployed candidates. A candidate that is not
  deployed cannot answer a request. If the alias has known candidates and none is deployed, it reads `unavailable`,
  whatever the health of their services, because a request to it is refused with `model_not_deployed`. If no
  candidate is known at all, the alias has no field. The several-services rule below then decides among the
  deployed candidates. A decision, speech, transcription or reranking model entry (by its `type`) maps the same
  way: an engine id, or the model an engine serves, or a name that points at an engine, maps to that engine's
  service. The match stays inside one operation. An engine id wins; the served-model names are searched only when no
  engine id matched. An entry that can belong to several operations (audio with no input modality: speech and
  transcriptions) resolves the name in each operation on its own, then reads `available` if any operation does. An entry of any other type (language, image and so on) is never mapped through an engine's
  names. A decision provider's catalog names map to the provider's service, also when the provider is turned off
  (it then reads `unavailable`).
- Several services: `available` if any is available (ready, busy, degraded, or startable on demand),
  `unavailable` only if all are unavailable, otherwise `unknown`. A workflow that needs several services is
  `unavailable` if any is, even when another requirement cannot be resolved; it is `available` only when all are.
- `GET /v1/models?available=true` leaves out entries whose state is `unavailable`. `unknown` and unmapped
  entries stay (fail open). Any other value, or no parameter, changes nothing. Per-caller profile filtering runs
  first. Routing is unchanged: a left-out name still works if called.

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
- Alias targets resolve against the whole catalog before access plan filtering, so visibility does not
  change how static alias targets resolve. Dynamic aliases inherit from the backend `current` would
  serve (see "Dynamic aliases").
- Context windows keep their existing lookup (`_annotate_model_context_windows`); this contract only
  passes them through.

## Dynamic aliases (`current`, `current-fast`)

These aliases send requests to whichever local LLM is loaded. Inheritance follows the backend
`current` would serve at that moment. A listing that includes either alias makes one invocation of
the selector `current` routes with: it walks the LLM backends in priority order and skips a backend
that is unhealthy, refused by an operator override, or has no model loaded. A backend the selector
skips lends no metadata. The loaded model maps to declarations of that backend only:

- A backend with `loaded_model_identity: path` matches on the loaded file path, through chat
  execution's activation resolver. Its served name is never used, because a name does not prove
  which weights are loaded. If the loaded path is gone when the listing maps it, nothing matches.
- Any other backend reports the id it serves. That id matches the records whose deployment asks the
  engine for it: `upstream_model`, else `deployment`, else `model`, else `id`, the same rule that
  builds the deployment. Ids compare without surrounding spaces and ignoring case, as model ids do
  elsewhere in the gateway.
  The engine may also name other ids that serve the same loaded model, for example an old id kept as an
  alias after the weights changed. The loaded entry's own `aliases`, and the id of every listed entry
  whose `alias_of` is the loaded id, match too. They come from the same model-list read that found the
  loaded id, are cached with it, and cost no request. Both fields are read only when the engine sends
  them; without them, matching is exactly as above. An id the engine does not list never matches.

A listing that includes neither alias makes no observation.

Probe cost: the selector may health-check candidates, in priority order, up to the first servable
one, and read the loaded model of that backend. Health answers are cached for about 10 seconds. The
listing then reads the loaded path for declaration matching on a path-identity backend. That read is
normally cached, but it is not an atomic snapshot with the selector and may differ after cache expiry.
The member catalog makes no observation and uses the conservative fallback.

- The aliases inherit `type`, `architecture`, `tags` and `max_tokens` from the `model_metadata`
  declarations of those ids. Only declared ids count. When several are declared, the aliases claim
  only what all of them claim. A record whose own request policy turns thinking off (it defaults
  `reasoning_effort` to `none`, for example with a `default-params` policy overlay) does not count
  against the `reasoning` tag.
- An alias whose own request policy turns thinking off drops the `reasoning` tag. On this machine that
  is `current-fast`, through its `reasoning_effort: none` overlay. The gateway reads this from the
  policy data, not from the alias name.
- `context_window`: a value observed from the running engine wins. Without one, the aliases take the
  smallest window the loaded model's own rows publish. Each id gets its window by the lookup its own row
  uses: its record, else its service's configured window, else the service's cached probe. These come ahead
  of static alias values. The aliases read the rows already in the listing and start no extra probe.
- `max_tokens` rides the same merge as the tags. An id that declares none counts as not agreeing, so a
  sibling record of the same weights with no declared cap keeps the aliases from publishing one.
- When no backend is servable, no id of the served model is declared (for example `cydonia-24b`), a
  path backend's loaded path is gone or matches no record, or the declaration match is ambiguous, the
  aliases keep their own declaration: text in, text out, no tags.
- The inherited values win over the aliases' own `model_metadata`, which is only the fallback.
- Clients may cache `/v1/models`. After a model swap, a client can see the previous model's values
  until it lists the models again.

List and retrieve endpoints use the same request policy. The member catalog has no selector
observation, so its dynamic aliases use the conservative fallback. Model-ID lists in
response metadata and alias target maps include only IDs visible in `data` under the caller's access plan.

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
| `GET /me/api/models/catalog` (member Models page) | Alias and model rows carry the same metadata contract. Dynamic aliases use the conservative fallback because the member catalog makes no selector observation. The row's `capability`, `icon_key` and `io` come from those fields, see "Models page classification". |
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

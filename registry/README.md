# Registry Seed Catalogs

This directory contains bundled seed catalogs for the gateway registry. Core
code loads these files generically; it must not hardcode particular providers,
services, workflows, targets, or protocol frontends.

At startup, these catalogs may seed service-registry records. The catalog files
remain immutable package data, but their service rows live in `admin.db` after
the one-time seed. A service row's `source` is provenance, not permission:
every non-managed service row remains editable, enablable/disableable, and
removable from `/admin`, regardless of whether it was bundled, provisioned,
builtin, discovered, or created in the UI.

The managed LiteLLM substrate is the sole exception. Its gateway-derived
endpoint/protocol fields are locked and it cannot be removed; its name, enabled
state, and note remain editable.

`capability_words.yml` defines ordered `capabilities` rows (`id`, `label`, optional `policy_protocol`, and the presentation fields below), followed by protocol words and extra route intents referring to those ids. Row order is display/access-plan order. Home defaults an omitted policy protocol to `openai.chat`, including decide and rerank; embed explicitly keeps `openai.embeddings`. `gateway/capability_ids.py` validates schema version 1 and the rows before the word maps, naming the file and row for malformed rows, duplicate ids, unknown row keys, empty/non-string labels or non-string protocols. It loads read-only ordered tables once at import. Add a row and any word mappings to extend the catalog without a code table edit; this does not add a registered model kind. Restart after editing it.
- `icon`: optional non-empty capability id, including a later row; defaults to this row’s `id` (audio declares `sfx`).
- `recommended_alias`: optional non-empty stable caller name; omission adds no recommended alias.
- `summary`: optional non-empty presentation sentence; omission adds no summary. Presentation maps follow row order and are read-only.
- `hidden_by`: optional list of capability ids, including later rows. A service hides this label when any listed capability is also present; audio names speech, transcribe, music and sfx.

Top-level `type_capabilities` maps parsed metadata types to declared capability
ids. `audio_rule` declares the special type, required input modality, absent
output modality, result capability, allowed subtypes and fallback capability.
The loader rejects unknown fields and undeclared capability references with
file/row diagnostics. Classification tables and nested lists are immutable and
load once. Existing word/presentation loader return shapes remain supported;
older files may omit the two new sections, which then yield empty tables.
Parsing and normalization remain code. Listed contracts precede inferred
contracts, then declared capability; the audio direction rule precedes metadata
subtype, capability subtype, tags, intent mapping and fallback. Adding a mapping
does not expand the upstream metadata parser's accepted type vocabulary.

`presentation_words.yml` (schema version 1) supplies ordered label-cleanup
vocabulary for `gateway/model_presentation.py`: provider suffixes, engine prefixes
and tails, capability-specific suffixes, acronym replacements, parenthesized
hardware words, hardware label suffixes, bare titles, technical-summary regexes
and the unclassified presentation fallback. All word lists/maps are non-empty.
Only `technical_patterns` contains regex syntax; other words are matched
literally. Prefix matching stays case-sensitive, while engine tails and
capability suffixes are case-insensitive. The loader names the file and key for
missing/unknown fields, wrong types, blank words, empty lists/maps or invalid
patterns. Data is loaded once at import into read-only maps/tuples and patterns
are compiled then. Restart after edits. Cleanup order, case rules, punctuation,
sentence/eight-word filtering and URL/IP logic stay in code. Capability taxonomy
and classification remain separate from this vocabulary.

`models_catalog_operations.yml` holds the catalog's operation synonyms, ordered
intent prefixes, and exact example/path templates. `gateway/models_catalog.py`
validates schema version 1 and loads deeply read-only maps once at import. Each
operation row has an `id`, `words`, `intent_prefixes`, and optional `examples`;
example rows declare `intent`, `kind`, mapping `body`, `path`, and optional `files`.
The one top-level `row_id_placeholder` replaces matching whole body values with
the requested model ID. Synonyms normalize strip/lower/underscore-to-dot; intent
matching stays exact. Duplicate synonyms/IDs/intents and malformed rows fail with
file/row diagnostics. Add data rows for new operations; restart after edits.

## Protocol descriptions

`registry/protocols/*.yml` hold protocol descriptions: data that says how
Azizam talks to a kind of engine (requests, body mappings, framing, auth,
async jobs). `protocol.schema.json` is their closed schema; the loader is
`gateway/engine_protocol/description.py`. The file name must equal the `id`.
A description names no engine and no code. See
`docs/design/ENGINE_PROTOCOL_EXECUTOR.md`.

Three descriptions say `executor: adapter:<id>`: `node-graph`, `inline-tts` and
`chunked-stt`. They list operation names only (the reviewed adapter owns the
wire) and carry `serves` (capabilities the adapter answers for when an engine
record names no protocol), `label` (the engine's name in caller-facing
messages) and, for `node-graph`, `binding` (the key under a workflow manifest's
`bindings`). An engine record selects an adapter by naming its protocol, or by
the capability of the route it serves.

## STT sample recordings

`stt_samples.yml` lists long recordings with human reference transcripts
(AMI, ICSI, Earnings-21) for measuring long-audio transcription settings.
It is data only: each sample names its licence, every file to download
(https URL, sha256, size) and a recipe (`ami-words`, `icsi-words` or
`earnings21`) that says how to read the reference words. Azizam ships no
audio.

`gateway/stt_samples.py` reads the file (`load_manifest`) and prepares one
sample on demand (`fetch_sample`). It downloads each source into a cache
named by sha256, so an archive that several samples share downloads once.
It refuses a file that is larger than declared or does not match its
checksum, and deletes it. It then writes a 16 kHz mono PCM16 WAV with
ffmpeg and builds the word list `[(word, t0, t1), ...]`. To add a sample
from a corpus that a recipe already reads, add a row; a new corpus format
needs a new recipe function. Each source requires `audio_rights` and
`transcript_rights` objects, with `status` (`stated`, `not-stated`, or
`not-applicable`) and a nonempty `note`. The recipe's audio source cannot use
`not-applicable` for audio rights. A `not-stated` status for either kind of
rights raises the sample caveat. Sources also require nonempty `provenance`
and `human_transcribed_evidence` strings. These fields record source claims
and evidence; they do not independently verify rights or authorship.

Read the source notes before using a sample. Earnings-21 states CC BY-SA
rights for transcripts and alignment files, but no separate audio licence.
Public hosting does not establish reuse rights. Its audio is excluded from
the default measurement selection and requires an explicit sample choice.

## Engine record defaults

`engine_record_defaults.yml` holds the values an engine's service record may leave out: request timeouts, the default
model, the health path and the workflow directories, by service id. `engine_record_defaults.schema.json` is its closed
schema, and `gateway/engine_record_defaults.py` reads it. A value in the record wins. The file never holds an address:
a record that names no host and port is an engine that is not configured (docs/design/CODE_TO_DATA.md, S8).

## Retention

`retention.yml` holds the bundled defaults of the request-retention contract:
the shared retention choices and their lengths, the kill-switch default, one
member switch per group of kinds, and each kind's purpose, parts and readers.
`retention.schema.json` is its closed schema and `gateway/retention.py` adds the
cross-row rules. The gateway does not start without a valid file, and Python and
the web read these values through the settings API
(`docs/design/REQUEST_RETENTION.md` section 3.2).
Restart the gateway after editing `retention.yml`. A stored period keeps its
duration while its ID remains in `retention_choices`, even if it is no longer
offered for new selections; a stored `never` stays no-expiry. If its ID is
deleted, the shortest finite offered period applies. A file that offers only
`never` is valid until a stored period ID is deleted and no finite offered
period can replace it; then the gateway stops at startup and names the ID.
Startup validates the file and attempts to shorten saved prompt deadlines
before serving. If that repair fails, the owner's prompt status shows
`deadline_repair_pending` and the recurring maintenance pass retries it until
it succeeds. A transient database lock defers the stored-ID check to repair;
it does not by itself invalidate the retention file.
Slice 1 refuses `household.never_keep: true` in this file; the owner setting
performs the required erasure when that switch is enabled.

## Operation model metadata

Each stable-alias operation is declared once on the `registered_routes` row in
`protocol_frontends.yml` that serves it. `operation` is an exact member of
`gateway.stable_aliases.OPERATIONS`; `model_metadata` holds `type`,
`input_modalities` and `output_modalities`. The type must be in
`gateway.model_metadata.MODEL_TYPES`; both modality fields are lists using the
corresponding `INPUT_MODALITIES` and `OUTPUT_MODALITIES` vocabularies.
Routes that do not declare an operation omit both keys. An operation declaration
without metadata, or metadata without an operation, is invalid.

The loader rejects duplicates, unknown operation/type/modality values and a
missing roster operation at module load. It builds a read-only operation index
once from release data, in stable-alias roster order. Metadata is not inferred
from route intent: the rerank row declares `type: reranking` even though its
runtime policy shares the embeddings intent. Changing these rows requires a
process restart; they are not owner-config metadata overrides.

## Service capability model metadata

`service_capability_metadata.yml` maps service-registry capability names to
operation metadata from `protocol_frontends.yml`. Its `schema_version` is 1.
`capabilities` is a nonempty ordered list of `capability` / `operation` rows:
the first matching row wins, independently of the caller's capability order.
Operation references must be keys of `gateway.model_metadata.OPERATION_METADATA`.

The single `augmentation` declares a capability name, a `match_type` from
`MODEL_TYPES`, and a nonempty `input_modalities` list from `INPUT_MODALITIES`.
It adds inputs only after a row matches that model type. The shipped rule makes
vision add image input to language; vision alone gives no metadata and does not
change an embeddings match. This is first-match selection, not a shared-type or
intersection rule.

`gateway.model_metadata.load_service_capability_metadata(path)` rejects unknown
keys/operations, empty or malformed files, non-string or noncanonical names,
duplicates after normalization, and augmentation names also used by matching
rows. Names in the file must already be stripped and lowercase; caller names
retain their existing strip/lower normalization. Errors name the file and row.
The index and its nested values are read-only and loaded once at import.
Changes require a process restart; the file is release data, not owner config.

## Capability intent model metadata

`capability_intent_metadata.yml` supplies gateway capability defaults with
`schema_version: 1`, an `exact` list of `intent` rows and an ordered
`families` list of `prefix` rows. Each row gives exactly one of an
`operation` name from `OPERATION_METADATA`, or literal `model_metadata`
with `type`, `input_modalities` and `output_modalities` from the shared
vocabularies. Exact intent matches win; otherwise the first matching prefix
wins in file order. Declared capability modalities still refine those defaults.

`gateway.model_metadata.load_capability_intent_metadata(path)` rejects unknown
keys, duplicate intents or prefixes, unknown operation/type/modality values,
and both/neither metadata representations with file-and-row errors. Names are
nonempty strings and matching remains case-sensitive. Its maps and metadata
are read-only, loaded once at import from release data, not owner config.
Changing the file requires a process restart; new families need only rows.

## Extension catalog route facts

Serving `registered_routes` rows in `protocol_frontends.yml` may declare:

- `extensions`: a nonempty list of known namespaces: `com.azizam.prompts`,
  `com.azizam.workflow`, `com.azizam.chat`, `com.azizam.routing`.
- `extension_order`: a globally unique integer that fixes public catalog order
  independently of YAML row order. A row shared by namespaces has one position.
- `route_inputs`: string input names owned by the route, excluded from workflow
  extension controls. Required with the workflow extension.
- `workflow_targets`: required with the workflow extension; `intent_prefixes`
  is a nonempty string list and `route_intent` is a nonempty string. These are
  discovery facts, not changes to runtime route policy.

Rows without extension facts omit these keys. The loader rejects unknown
namespaces, non-string inputs, missing route intents and duplicate order values
(including residual positions), as well as malformed containers. Workflow-only
keys require the workflow namespace. Audio/video inputs remain equal to their
binder constants, enforced by `tests/test_extension_catalog_rows.py`.

`gateway.extension_catalog.load_extension_routes(path)` reads only shipped
`records[].registered_routes`, not `frontends` or owner configuration. The module
builds its index once at import; the mapping, row tuples and row fields are
read-only. Changes require a process restart.

Two routes have no registered row: the dynamic workflow-video route and the
media-jobs image-generations frontend. They remain in the named
`_RESIDUAL_EXTENSION_ROUTES` tuple at orders 50 and 90 respectively; no registry
rows are invented for them. Every other extension route fact belongs on its
serving registered row.
## Local capability rows

`local_capability_rows.yml` is schema-version-1 package data for
`gateway/local_capabilities.py`: ordered `alias_operations` name intent,
input/output modalities and sync/job execution, with an optional `capability` product subtype that a stable alias row without its own `capability` inherits (a `backend_rows` row may also carry `capability`); ordered `workflow_families`
name the config section, enabled flag, intent, modalities and resource tier.
Ordered `backend_rows` map declared capability triggers to flags, intents,
modalities, streaming, sync/job execution, GPU needs and optional display facts.
Groups run in endpoints, speech, recognition order; within each group, backend
order comes first and row order second. A row is emitted once if any trigger
matches. Unknown triggers are skipped. Optional `vision` names a trigger and
input override; `resources_from: backend` delegates resource parsing to the
existing backend resource parser. Other rows use only their declared GPU need.
Alias names and workflow section names must be unique within their list;
backend triggers must be unique across rows (case-insensitive).
The loader rejects unknown/missing fields and invalid values with the file and
logical row. `image_defaults` declares the image section, enabled flag, section
requirement, backend-presence rule, fallback backend, output modalities, execution,
streaming, tier and GPU requirement. Ordered `image_rows` name intent, inputs and
model sources such as `backend.default_model` or
`config.model_compatibility.defaults.images`; only `backend` and `config` roots
are accepted. Selection uses the first truthy source, then requires a nonempty
string, preserving the existing non-string suppression behavior.
`workflow_defaults` holds section/backend requirements and shared execution,
streaming and GPU defaults; each workflow family still owns its resource tier.
The whole file loads once into deeply read-only tables at import. Builders never
reload it or inspect the environment. Restart after editing the file. The
original two-table loader API and files without `backend_rows` remain supported;
an omitted list is empty, with no hard-coded backend fallback.
This file does not probe workflow files or enable a backend by itself.

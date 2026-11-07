# Workflow execution contract (code-to-data S2)

Status: design, revision 4, 2026-11-05. Independent reviews shaped it: revision 1 (4 blockers, 6 majors,
1 minor), revision 2 (1 blocker, 4 majors), all taken. Revision 4 adds the terminal row to D3 and splits S2.1 into the
store (S2.1) and its wiring (S2.1b), after the review of the S2.1 build.

The lead verified the two facts the decisions rest on:
- The execution spool exists: `_spool_media_job_payload` in proxy.py, and docs/CORE_CONTRACTS.md, "Normal asynchronous
  jobs retain restart durability".
- The job metadata guard is key-only (gateway/redaction.py).

## Problem

A workflow run today has four steps:
1. The route reads its own fields.
2. The graph adapter compiles the manifest map.
3. `jobs.run` submits and polls.
4. `fetch_assets` returns every file under the fixed image, video and audio history keys.

This causes four gaps:
- A JSON result, or a set of named results, has no path.
- Nothing checks the results against the manifest.
- The run is not pinned to what was validated. The graph is read again from its current path at submit, and the
  manifest hash does not cover the template.
- A synchronous engine has no place in the job lifecycle.

## Decisions

### D1. The envelope holds definitions, never caller content

The envelope is a typed record. It is stored in its own field on the job, not in diagnostic metadata, so the redaction
guard does not change. The envelope holds:

```yaml
envelope_version: 1
source: {kind: workflow|offering, id, revision}
definitions:                    # content-addressed, retained blobs (D2)
  contract: sha256:...          # input and output schemas, after defaults are normalized
  binding: sha256:...           # the selected binding: key, mapping and output selectors
  template: sha256:...          # optional: the graph or request template bytes the adapter compiles
  protocol: {id, revision}      # the engine description revision
executor: {adapter_id, interface_version}   # for example node-graph v1, http-v1 v1
upstream: {service_id, origin_digest}       # pinned destination; credentials are resolved at use
generated: {<input name>: {generator: <reviewed id>, value}}   # values the gateway made, e.g. a random integer
results: {bundle: <result ref> | null}
```

- The envelope holds no caller-supplied value. Caller inputs, uploads and URLs stay in the **execution spool**. Prompt
  memory, forget, member-off, the kill switch and erasure already govern the spool. Restart durability does not change:
  a durable spool resumes, and a memory-only spool cannot.
- A value that the gateway generated is not caller content. It is saved with the id of the reviewed generator that made
  it (the `{generate: random_integer}` tag from S3). No core code names `seed`. Each reviewed generator declares how its
  value persists: `value` (a random integer), `digest` or `none`. A manifest cannot change that. Erasure and forget
  clear the `generated` block together with the spool, to be safe.
- The envelope is built once, after idempotency resolution and before the first effect.
- Each attempt resolves the current credentials and checks revocation again. A pin never revives a revoked App,
  credential or asset grant.

### D2. Pinned definitions are retained blobs, with a reference-counted lifecycle

A small content-addressed store keeps the contract, binding and template bytes that an envelope names.

- **What they are.** Definitions are owner-operational data (manifests, bindings, graph templates), not caller content.
  A person's erasure does not need to delete them. It deletes the job's envelope and spool, which releases the
  references.
- **References.** One table, `definition_ref(blob_sha, job_id)`. It is written in the same transaction as the envelope,
  so a blob is never referenced before it exists. The registry's current revisions hold references too (`job_id` =
  `registry:<revision>`).
- **Release.** A job's references are deleted when its envelope is deleted: by job retention, job deletion, erasure, or
  the kill switch. That is the same transaction that removes the envelope.
- **Garbage collection.** A sweep, under the store's write lock, deletes blobs with no reference that are older than a
  grace period (default 1 hour). The grace period covers a blob written just before its envelope commit. A blob is
  written before the envelope transaction and is idempotent by hash.
- **Use.** The adapter compiles from the pinned template, in both `validate` and `submit`. It never reads a live path.
  If a pinned blob is missing, the job fails with `definition_unavailable`. It never falls back to the current file.

### D3. One start outcome for synchronous and asynchronous engines, composed with today's marker

Adapter interface v2 adds `start(conn, op, request) -> Pending(JobRef) | Completed(ResultBundle)`. A v1 adapter keeps
`submit`. The runner calls `start` only on an adapter that declares interface v2, so v1 and v2 coexist. The attempt
marker **is** today's `protocol_submitting`, written just before the call. There is one durable state machine for both
outcomes:

| Saved state on the job | After restart |
|---|---|
| A terminal status (succeeded, failed, canceled, or any other end state) | No recovery action. A replay is a read of the stored result. This row is checked first, so a terminal job that still carries `protocol_submitting` or `protocol_job` is not reclassified or polled again. |
| No `protocol_submitting` | No effect happened. If the spool is present, resume from the envelope plus the spool. If the spool was memory-only (forget, member-off, kill switch), the job ends as `interrupted_before_submit`. |
| `protocol_submitting`, no `protocol_job`, no committed bundle | `submission_unknown`. Never a blind retry (today's rule). |
| `protocol_job` saved | Poll the pinned upstream (today's rule). |
| A non-terminal job with a committed result bundle | Not reachable: D4 publishes the bundle and ends the job in one transaction. If it is seen, it is a broken invariant to repair, not a state to recover. The D4 startup receipt-link scan keys on "bundle published, no receipt", not on this table. |

A synchronous `Completed` reply is staged and committed (D4) before the marker is cleared. If the process crashes
between send and commit, the state is row 2: `submission_unknown`.

**Retry of a terminal job.** S2 adds no reactivation. A same-key POST returns the same terminal job, as `media_jobs`
does today. The caller retries with a new key. The error says so, and names the envelope revision it would pin.

The adapter's declared capability chooses the lifecycle, never an engine name.

### D4. Named results: a tagged bundle with private, durable delivery

```yaml
bundle_version: 1
results:
  <name>: {kind: json, value_ref: <private ref>, bytes, sha256}
  <name>: {kind: assets, items: [{asset_ref, media_type, bytes}]}
```

- **Staging.** Results are staged privately, in a durable table `result_stage(job_id, attempt, name, ref, state:
  staged | published | aborted)`.
- **Publication.** One SQLite transaction does three things, and only after every check in D6 passes and any cancel has
  settled:
  1. sets the job's bundle reference;
  2. marks the job's stage rows `published`;
  3. moves the job to its terminal state.
- **Receipt.** The completion receipt is linked after that commit, idempotently by job id.
- **Startup scan.** On startup, a scan does two things:
  - It deletes the stage rows and blobs of any attempt that is not running and not published.
  - It links the receipt of a published bundle that has none. This covers a crash between publication and receipt
    linkage.
- **JSON values.** They go to a private result store, with owner and maker scope, retention, erasure and an
  authenticated read that answers `application/json`. Values never go into job metadata, events or receipts.
- **Receipts.** A receipt carries the envelope identity and each result's name, kind, count and size. Each job gets one
  completion receipt, linked idempotently.
- **Replay.** Replay returns the same committed bundle.
- **Status.** The client job view gains `results: {name: {kind, count, href}}`. Today's `assets` list stays for the
  existing routes.

### D5. Output selectors are adapter data, and new contracts must declare them

The binding declares where each named result comes from. The selector form belongs to the adapter:
- A graph result: `{node, key, from: output_dir, decode: none|json, take: all|first|single}`.
- An http-v1 result: a declared response selection or binary source.

Core receives named values only, never node keys or JSON pointers.

- A new contract with no selectors is refused at save.
- The bundled workflows get explicit selectors, made from their current graphs and checked against the golden outputs.
- Until each bundled workflow is migrated, a legacy fallback stays inside the graph adapter, behind a version flag. It
  reads only files in the output directory, never source echoes or previews. It refuses an ambiguous result rather than
  delivering more.
- The adapter defines how it handles missing versus null, order, duplicates and lists. Each rule has a test.

### D6. Result limits are enforced during fetch, not after

The limits are trusted deployment limits, not manifest values. They cover:
- history or response bytes;
- the number of selectors;
- JSON depth, item count, string length and total bytes;
- the number of assets, bytes per asset and total bytes;
- fetch time and concurrent fetches.

Enforcement:
- JSON with non-finite numbers or duplicate keys is refused.
- Asset bytes stream into staging, and the limit is enforced while reading. `AssetStore.save` already does this; the
  graph adapter switches to its streaming reader.
- The reviewed sniffer checks each asset's type against the declared media type. A media type that the sniffer does not
  know is refused at save (D8).
- "Delivers nothing" means that no result can be read. It cannot undo the inference that produced the result.

### D7. Media inputs: resolve, authorize, disclose, upload

- **Resolver.** A trusted resolver takes the authenticated context and an asset id or an upload. It ignores the type,
  size and name that the caller claims. It checks read access, and it checks that the selected upstream may receive the
  bytes. It returns an immutable stored reference with a lease.
- **URLs.** URLs go only through the existing media ingestion and its origin rules, including redirects.
- **Upload.** Engine upload becomes a declared adapter capability, with the media types it supports. The graph adapter
  declares what its upload endpoint accepts and nothing more.
- **Effects.** Ingestion and upload are effects. Each has its own marker and orphan cleanup. "Zero upstream effects" is
  stated per boundary: a denied authorization or an invalid input reaches neither upload nor generation.

### D8. A minimum closed grammar now, so unsupported forms fail at save

S2 ships the subset of the grammar that it can run:
- input references (asset, by media type);
- result kinds and counts (`json`, or `assets` with `min` and `max`);
- selectors;
- decode modes;
- adapter capabilities: completion mode, upload types and result kinds.

Rules:
- Each selected binding is checked against its adapter's descriptor, at load and at save. A malformed descriptor never
  becomes an offering.
- Legacy defaults are explicit. An output with no count keeps its current behaviour; for example, `flux2-klein` still
  batches up to 8.
- A legacy form that the generic path cannot run keeps its reviewed route, or is marked "needs configuration" with a
  precise error.
- S3 widens this grammar. It does not replace it.

### D9. Names

- The protocol description revision, the adapter id and interface version, and the binding key are separate fields.
- `graph_digest`, node selectors and media-family logic stay in the graph adapter's definitions.

## Build order inside S2

Every step is additive and keeps the current route path as the default. Each step has the same gate:
- the audio route goldens and the image route wire goldens are unchanged;
- one real `/v1/audio/generate` job and one real image job succeed on the live gateway after deploy.

| Step | Content | Default behaviour after the step |
|---|---|---|
| S2.1 | The envelope record and the definition store: references, garbage collection, erasure release, as functions on the jobs database. The recovery table, read only. No route calls them yet. | Unchanged. |
| S2.1b | Record the envelope alongside today's flow. Every path that deletes a `media_jobs` row (retention, delete, erasure, kill switch) calls `release_in` in the same transaction, with a test that enumerates those paths. Schedule the garbage-collection sweep. | Unchanged. The envelope is recorded, but not yet used to compile. |
| S2.2 | Compile from the pinned template, per route, behind a config flag. The audio route goes first. | The flag is off until the audio goldens, plus a crash test and a pin test, pass with it on. Then it is turned on for audio. |
| S2.3 | The result bundle, staging and publication, the private JSON store, receipts, limits. The routes keep `assets`. | Existing routes publish a bundle that mirrors their assets. Client output is unchanged. |
| S2.4 | Selectors and adapter descriptors; selector migration of the bundled workflows (golden-checked); the legacy fallback flag; refusal at save. | The legacy fallback stays on until every bundled workflow has selectors. |
| S2.5a | The v2 `start` outcome in the runner, an http-v1 selector descriptor, the pinned runtime, and the data-only and pin tests (acceptance 1 and 4). | v1 adapters are unchanged. No route calls the runner yet. |
| S2.5b | The image and video routes collect through `adapter.collect`; the `results` job-view field and the JSON read route; the legacy collection goes off. | Route goldens move on purpose. S2.5b did the first two; the legacy flag stays on (four bundled workflows have no selectors; see "S2.5b as built"). |
| S2.6 | The asset resolver, the disclosure check, the upload capability. | The image routes keep their current ingestion until their goldens pass on the resolver. |

### S2.2 as built

- The setting is `workflow_execution.compile_from_pin.<capability>` in the runtime config, read on each job
  (`gateway/pinned_definitions.py`, `compile_from_pin_enabled`). The key is the route's graph capability. It is on for
  `audio.generate`, which covers the audio, music and sound-effect job intents.
- The job runner reads the pin before it starts (`pinned_definitions.load`). The audio route then takes its manifest
  and graph from the pin and never reads the registry or the workflow file. A job with no envelope, a missing or
  damaged blob, or a pin that is not a manifest fails with `definition_unavailable`. So does a job whose model now
  resolves to another workflow than the one it pinned (an alias or `audio.workflows` edit): the run never mixes a pinned
  graph with another workflow's limits.
- A route with the flag on refuses to accept a job that could not pin: a pin that cannot be built, or one the store
  refuses to write, is a 503 and no job row (`media_jobs.create(require_envelope=True)`). With the flag off, writing
  the envelope stays best effort.
- A job pinned before S2.2 (a contract without `schema_version`, `id` and `intent`) fails `definition_unavailable`.
  There is no reader for the old shape: the product is unreleased.
- The deployment limits (`audio.workflows.<name>`) are not pinned. They narrow the pinned contract at run time.
- The synchronous `/v1/audio/generate` route has no job, so it still reads the live files.
- The pinned contract now carries the manifest header (`schema_version`, `id`, `intent`) next to the inputs and outputs,
  because the compile validates it. The job goldens moved only by that blob.

### S2.3 as built

The code is `gateway/result_bundle.py`, with three additive tables on the jobs database (`result_stage`, `result_json`,
`result_bundle`). The schema version does not change.

- **Staging.** `result_stage(job_id, attempt, name, ref, state, kind, count, bytes, sha256, items_json, created)`. A
  result is `json` (the value is in `result_json`, keyed by `ref`) or `assets` (a list of
  `{asset_ref, media_type, size_bytes}`, no bytes). Staging a name that an earlier attempt already published is refused.
  Staging for a job that erasure has already covered is refused, so a late worker cannot write a value after erasure.
- **Publication.** `publish_in` runs inside `media_jobs.update_status`, through a new `before_commit(conn, job)` hook. The
  hook runs after the transition is checked and written, and before the commit. A refused transition never runs it. In
  that one transaction it checks the bundle limits, sets `results.bundle` in the envelope
  (`definition_store.set_bundle_in`), writes the `result_bundle` row, marks the attempt's rows `published`, and marks
  every other attempt's staged rows `aborted`. A pending cancel raises before the hook, so nothing is published while a
  cancel has not settled. Any failure aborts the staged rows and fails the job with `result_refused`.
- **Receipt.** The receipt lives in another database, so the link cannot join the transaction. `link_receipt` merges the
  summary into the `metadata` of the job's work receipt (`receipt_store.merge_job_metadata`) and then sets
  `receipt_linked_at`. It is idempotent by job id. The summary is `{bundle_version, bundle, envelope identity, results:
  [{name, kind, count, size_bytes}]}`. It never holds a value. It sits in internal-tier receipt metadata, so the client
  and admin receipt views do not change. No new receipt row is made.
- **Startup scan.** `startup_scan` runs first in `_recover_media_jobs`. It deletes the stage rows and JSON values of
  attempts that are neither running nor published, and links each published bundle that has no receipt.
- **JSON store.** Values sit in `result_json` with the owner scope and the maker stamp of the job. `read_json` answers
  `application/json` only to the maker (person, then credential, then connection) under the same owner scope; every
  other case returns nothing. Retention is 30 days (`sweep`, called from the existing garbage-collection pass). Erasure
  deletes values and unpublished rows. A published JSON row keeps its facts and loses its hash. No route produces JSON
  yet; unit tests prove the store.
- **Limits.** `workflow_execution.result_limits` in the runtime config (not in `proxy_config.yml`, so the config-homes
  goldens do not move). Defaults: 64 assets, 256 MiB per asset, 1 GiB total, 4 MiB JSON, depth 32, 100 000 items, 1 000 000
  characters per string. `LimitedAssetStore` wraps `AssetStore.save` and enforces count, per-asset and total bytes while
  the stream is read; a lower cap from the caller still applies. JSON is checked for size, depth (before parsing, outside
  strings), item count, string length, non-finite numbers and duplicate keys.
- **Release.** Delete and the retention sweep release through `media_jobs._release_envelopes_in`, which now also calls
  `result_bundle.release_in` in the same transaction as the job row. Person erasure, the kill switch and the cutoff all go
  through `media_jobs.erase_prompt_derivatives`, which calls `result_bundle.erase_in`. `tests/test_result_bundle_release.py`
  enumerates these paths from the code and fails if a new deleter appears.
- **Audio route.** `_run_audio_media_job` checks the artifact sizes against the limits before it saves a file, then
  finishes through `result_bundle.mirror_assets`. The bundle has one result, `assets`. The job view, the `assets` list and
  the audio route goldens are unchanged. The bundle is published whether or not the flag is on; a job with no envelope
  publishes a bundle and its summary carries no envelope identity.

Goldens: `tests/fixtures/adapter_differential/audio_generate_job.json` moved on purpose, and only inside
`$.media_jobs`, the dump of the internal jobs database (the envelope's `results.bundle` and the new `result_bundle`
and `result_stage` rows). The client wire trace, the job view and the receipt goldens did not move.

Decisions the design did not settle:
- **No `results` field in the job view.** D4 adds `results: {name: {kind, count, href}}`. That field waits for S2.4,
  when a route can produce a result that the client cannot already see as an asset. Nothing client-visible changed.
- **The summary goes into the existing receipt.** The doc says the receipt carries envelope identity and each result's
  name, kind, count and size. S2.3 adds that to the job's work receipt and does not add a receipt. The receipt goldens
  did not move.
- **The result name `assets`** is the default for a mirrored asset list. It is a name, not a vocabulary: no engine,
  workflow, intent or modality name is in the module.
- **A cancel that has not settled** aborts the attempt's stage at once and lets the cancel proceed. A crash before the
  abort leaves `staged` rows; the startup scan deletes them.
- **Link window.** A bundle whose receipt never appears is given up after 14 days (`media_jobs.REPLAY_WINDOW_S`), so the
  scan does not retry it forever.
- **Limits are optional config with code defaults.** Putting them in `proxy_config.yml` would move the config-homes
  goldens; that is S2.4's call, with the selector fields.

Left for S2.4 and later:
- The image and video job routes. Their completion sites differ (several `update_status` calls with `add_assets` and
  derived-asset paths), so a generic change was not small. They call `mirror_assets` in the next step, with their own
  goldens.
- The `results` field in the job view and the authenticated JSON read route (the read function exists).
- The selectors that make a route produce a JSON result, and the streaming reader in the graph adapter that goes through
  `LimitedAssetStore`.
- Limits for history bytes, selector count, fetch time and concurrent fetches (D6). (Done in S2.4.)

### S2.4 as built

The code is `gateway/output_selectors.py` (the grammar, the checks, the legacy flag reader and the fetch gate), the
selection functions in `gateway/engine_protocol/node_graph.py`, and the audio route in `proxy.py` (three small hunks and
one helper). The module names no workflow, engine, model, intent or modality.

- **Adapter descriptor, as data.** The adapter declares the selector form it accepts in a `result_selectors` block of its
  protocol description (`registry/protocols/node-graph.yml`; the schema is `protocol.schema.json`). The block lists
  `fields` (each `text` or `enum`, required or with a default), `identity` (the fields that say where a result is),
  `template_node` (the field that names a node of the template) and `kind_field` (an enum field whose values map to a
  result kind, `json` or `assets`). The compiler refuses the block unless the description names a binding (S2.5a relaxed an earlier
  rule that also required an adapter executor, so an http-v1 description can carry one). A malformed descriptor raises `DescriptorError`, so the description does not load.
- **Selectors in the binding.** `selectors: {<result name>: {node, key, from: output_dir, decode: none|json,
  take: all|first|single}}`. `validate_manifest` checks the shape against the descriptor at load: declared fields only,
  the right types, required fields present, defaults filled, every named result a declared output, every output covered,
  at most `max_selectors` selectors. `check_for_save` adds the contract rules: a binding of an adapter that takes
  selectors must have them, a selector must not name an echo of a source or an input, two selectors must not have the
  same identity, and two outputs of the same kind that cannot be told apart are refused. Each refusal has a code
  (`selectors_missing`, `selector_echoes_source`, `selector_duplicate`, `results_ambiguous`, `selector_malformed`,
  `selector_unknown_result`, `selector_missing_result`, `selector_unknown_node`, `selector_count`).
  There is no workflow-save path yet, so `check_for_save` has no caller outside tests. The S4 registration route calls it.
- **Selection.** `NodeGraphAdapter.collect(job, block, outputs, limits=..., legacy_allowed=...)` returns the files of
  each named result. It reads only history entries of `type: output` (an entry with no type counts as output). A missing,
  null or empty list for the selected node and key is one refusal, `result_missing`. The order follows the history.
  A file that two entries name is kept once. `take: first` keeps the first file, `single` refuses any count but one, `all`
  keeps every file. An entry that is not a plain file name, or that leaves the output directory, is refused with
  `result_malformed`. A refused run delivers nothing.
- **Legacy fallback.** `workflow_execution.legacy_output_collection` in the runtime config. It is on unless the value is
  the literal `false`. When a binding has no selectors and the flag is on, `legacy_select` reads the output directory
  only (never a source echo or a preview) and refuses what it would have to guess (`result_ambiguous`): a contract of
  more than one result, or files that come from more than one place in the answer. With the flag off, a binding with no selectors
  is refused with `selectors_missing`. Turn the flag off after every workflow in use carries selectors; the bundled
  ones all do.
- **Bundled workflows.** The 34 manifests that bind the graph engine now carry explicit selectors made from their graphs.
  `tests/test_bundled_selectors.py` builds, for each one, the history its graph's save node would write (the node id and
  the history key follow the node class), and shows that the selectors read the same files the old per-family collection
  read. It builds a second history with a source echo and a preview ahead of the result, and shows the selectors and the
  legacy path still read only the result, where the old image collection returned the echo too. Every bundled manifest
  also passes `check_for_save` with its real template. Gateway-compose and the 3D service bindings take no selectors (see
  below).
- **Audio route.** `proxy_audio_generate` now reads its result through `adapter.collect`. A history with no file for the
  selector is the same 502 "No audio output found" as before. Any other refusal is a 502 with a fixed message and the
  refusal code in `error.code`; it names no value. The download runs inside `output_selectors.FETCH_GATE.slot(limits)`;
  a fetch that waits or runs past the time limit is a 504 with `fetch_timeout`. The route sets
  `response.selected_result` to the output name, and `_run_audio_media_job` passes it to `mirror_assets`, so the job's
  bundle result is named by the contract's output (`audio` for the bundled audio workflows) and no longer `assets`.
- **Limits (D6).** `ResultLimits` gains `max_selectors` (16), `max_history_bytes` (8 MiB), `fetch_timeout_s` (300) and
  `max_concurrent_fetches` (8). They are trusted deployment config under `workflow_execution.result_limits` with code
  defaults, never manifest values. `collect` refuses a history answer over `max_history_bytes` (`history_too_large`)
  before it reads it and a binding over `max_selectors`. `FetchGate` holds at most `max_concurrent_fetches` fetches per
  event loop, and the time limit covers the wait for a slot.

Goldens: in `tests/fixtures/adapter_differential/` 21 traces moved on purpose, and only inside the internal stores: the
receipt's `manifest_hash` (the manifests now hold selectors), and, for the four job scenarios that pin a definition,
`$.media_jobs` (the binding blob, its digest and the envelope's source revision). `audio_generate_job.json` also shows
the bundle result named `audio`. The client wire trace, the engine requests, the job view and every response did not move
(checked path by path against the old goldens).

Test changes: the audio fakes (`history_done` in the differential rig, the tangoflux, sa3, yue2 and ace-step route
fakes, and the media-job end-to-end fake) used a stub node id and key. They now give the node and key of the real graph's
save node, because a selector names them. `tests/test_result_bundle_audio.py` expects the result name `audio`.

Decisions the design did not settle:
- **The descriptor lives in the protocol description**, next to the other adapter data, so a new adapter ships its selector
  form with its description and core reads it without a branch on the adapter.
- **Grammar and field names** (`node`, `key`, `from`, `decode`, `take`) are the node-graph descriptor's. Core knows only
  `identity`, `template_node` and `kind_field`.
- **Limits are in the runtime config, not in `proxy_config.yml`.** Putting them in the file moves the config-homes goldens,
  and the S2.3 limits already live in the runtime config. One home for all result limits, so the decision is the same.
- **The legacy flag** is a runtime config key, on by default, and only a literal `false` turns it off.
- **Missing, null and empty are one refusal** (`result_missing`), and a duplicate file is kept once.
- **`take` per workflow type:** workflows that write several images use `all`; video and audio workflows use `first`.
- **The bundle result name** is the contract's output name, not `assets`. This changes the S2.3 default for the audio route
  only. `mirror_assets` keeps `assets` as the default for a route that names none.
- **No `results` field in the job view.** It would be additive, but no route yet produces a result that the client cannot
  see as an asset, so the field and the JSON read route stay for a later step. Nothing client-visible changed.
- **The image and video routes keep the old collection.** Their completion sites differ (several status updates, derived
  assets, batch rounds), so a generic change is not small. They are the next step.
- **Gateway-compose and 3D service bindings take no selectors.** Those engines have no history to read; their descriptor
  belongs with the http-v1 adapter.
- **`check_for_save` has no caller yet** (see above).

Left for S2.5b and later:
- The image and video job routes: collect through `adapter.collect`, name their bundle results by output, and refresh
  their goldens on purpose.
- The `results` field in the job view and the authenticated JSON read route.
- Turn the legacy flag off by default once no workflow in use lacks selectors, and delete the legacy collection.
- The call to `check_for_save` from the registration route (S4).

(S2.5a did the selector descriptor for the http-v1 adapter, the JSON read through the fetch gate, and the data-only tests.)

### S2.5a as built

The code is `gateway/engine_protocol/jobs.py` (the runner), `gateway/pinned_runtime.py` (new), the v2 methods of
`http_adapter.py` and `node_graph.py`, the result types in `gateway/engine_protocol/types.py`, and small hunks in
`description.py`, `output_selectors.py`, `executor.py`, `manifests.py`, `pinned_definitions.py`, `job_envelope.py` and
`proxy.py`. None of them names a workflow, engine, model, intent or modality; a test reads the tokens of `jobs.py`,
`pinned_runtime.py` and `http_adapter.py` and fails on a modality or engine word.

- **Interface version 2.** `SUPPORTED_INTERFACE_VERSIONS = (1, 2)`. `start(conn, op, request)` answers `Pending(ref)` or
  `Completed(ResultBundle)`. An optional `results(conn, ref, status, request, context)` reads the named results of a job
  that has succeeded. The http-v1 and node-graph adapters declare version 2. The reviewed inline-speech and chunked-speech
  adapters stay on version 1 and still use `submit`, `poll` and `fetch_assets`.
- **The runner decides by the declared number, never by a name.** A version 2 adapter without `start` is refused as
  `adapter_version_mismatch` before anything is resolved or sent. A version 2 job that is already finished returns
  `None` before its connection is resolved (D3, first row). The marker is written before `start`, as before. A
  `Completed` bundle is staged and published in the one transaction of D4, and that commit clears
  `protocol_submitting`. A crash before the commit leaves the marker, so a restart reads `submission_unknown` and sends
  nothing. A `Pending` outcome follows today's loop (`protocol_job` saved, then poll). When the poll succeeds and the
  adapter has `results`, the runner commits that bundle and leaves the marker alone (the job id is saved, so the marker
  no longer decides a restart); an adapter without `results` goes through `fetch_assets` as before.
- **What `run` returns.** The adapter's files for a version 1 adapter. The published `ResultBundle` for a version 2
  adapter that returned results. `None` for a finished version 2 job. Tests that drove a graph workflow through `run`
  read the bundle now.
- **A refused result publishes nothing.** `ResultRefused` (a limit) or `SelectionRefused` (a result that is missing,
  null, ambiguous or of the wrong type) leaves the job running, the marker set, and every staged result aborted.
- **Result limits reach the adapter through the runtime.** The runner asks its runtime for `result_context()` (the limits
  and the legacy setting). A runtime that does not have one gets the code defaults.
- **The http-v1 selector form** is the descriptor in the description, as for the graph. The test fixture uses
  `identity: [source]`, `kind_field: decode` and `fields.source` (text, the name of a `select` entry of the response) and
  `fields.decode` (`json`, `base64` or `binary`, mapped to the kinds `json`, `assets` and `assets`). Names, fields and
  values are data. Core reads only `identity`, `kind_field` and the kind map.
  - `json`: the selected value, written as canonical JSON, is the result bytes.
  - `base64`: the selected text is an encoded file that the adapter decodes and saves. This is how one synchronous
    reply holds a JSON value and a file at once (a mixed contract).
  - `binary`: the whole binary body is one file result. A reply has one body, so at most one result may read it.
  - A selected value that is missing or null is `result_missing`. A text that is not valid base64 is `result_malformed`.
    An operation with a `job` pattern is `Pending` and takes the usual loop; an operation with neither a `job` nor a
    binding that has selectors is refused (`selectors_missing`), because a synchronous answer has no legacy reading. A
    streamed reply has no selectors and is refused (`UnsupportedByAdapter`).
- **Result against contract** (`output_selectors.wrong_type_reason`, used by both adapters). A `json` result fits an
  output that declares `application/json`. A file result fits an output that declares the same top-level type for every
  file, as the reviewed sniffer names it, and never fits a JSON output. An output that declares no type accepts either.
  The reason never holds a value.
- **A pinned run** (`pinned_runtime.PinnedRuntime`) asks its envelope, not a file or a registry. The request carries the
  pinned manifest (pinned contract, one pinned binding) and the pinned template, so an edit or a delete after accept
  changes nothing. The connection is the live one, checked against the pin first: protocol id and revision, adapter id
  and interface version, and the digest of the engine address. A difference raises `DefinitionUnavailable` and nothing
  is sent. The address is stored only as a digest, so a moved engine cannot be followed; the job needs a new
  acceptance. The check runs on every call, so a runtime built after a restart meets the same pin.
  - **The adapter id** is the named adapter, or the built-in executor (`http-v1`) when the description names none. The
    envelope needs a non-empty value.
  - **The protocol revision** is the SHA-256 of the canonical JSON of the parsed description. Comments and spacing do not
    change it; any other edit does. `proxy.py` pins it for graph jobs in place of the `unrevisioned` marker, so a job
    accepted before this change fails the identity check (the product is unreleased, so there is no compatibility path).
  - **The pinned manifest is checked against the pinned protocol's own selector descriptor** (`validate_manifest` and
    `PinnedDefinitions.manifest` take the descriptors), not the shipped registry. `compile_workflow` no longer re-checks
    selectors in its view of one binding (they say how results are read, not how inputs are bound, and were checked at
    load).
- **The JSON read goes through the fetch gate.** `NodeGraphAdapter.fetch_json` took no part in `FETCH_GATE`. Called alone
  it now takes a slot under the deployment limits (a refused wait is `SelectionRefused('fetch_timeout')`). `results` holds
  one slot for its whole read (history, JSON and files) and passes its session, so the gate is taken once. Tests cover
  both: a held slot times a standalone read and a result read out.
- **A JSON file on a graph engine is read through a `key`.** The descriptor's `key` enum lists the history lists that
  hold files (`images`, `videos`, `gifs`, `audios`, `audio`, `wav`, `mp3`), so a JSON file must be listed under one of
  them. A node that writes JSON under another key needs the enum widened in the adapter's description (data). Not done:
  no bundled engine does this.

Goldens that moved (path by path): `image_job_succeeds`, `image_job_upstream_failure`, `image_job_oom_then_complete`,
`video_job_succeeds` and `audio_generate_job` (shared by the compiled-from-pin run). All changes are in the pinned
envelope and the copies of it: `$.media_jobs.job_envelope[0].envelope_json.definitions.protocol.revision`
(`unrevisioned` becomes the description digest) and `...executor.interface_version` (1 becomes 2). `audio_generate_job`
also moves `$.media_jobs.result_bundle[0].summary_json.envelope.interface_version` and
`$.receipts.receipts[0].receipt_json.metadata.result_bundle.envelope.interface_version` (1 becomes 2), because the
bundle summary and the receipt copy the envelope. No other path moved. The audio route wire goldens, the OpenAPI snapshots
and the job view did not move.

Tests are in `tests/test_start_outcome_v2.py`. Every name a contract or description uses is made at random when a test
builds its rig. The same checks run on a synchronous http-v1 fixture and a graph fixture, for a JSON-only and a mixed
contract, and for one offering envelope. Positive controls put back an identity branch and a modality branch in the
commit step; each must fail the check. Revert-checks (each mutation made the named tests fail, and was reverted):
`start` not chosen for version 2 (26 tests), the contract type check removed from the http adapter (the wrong-type
case), the pin identity check removed (the seven pin cases), the gate removed from standalone `fetch_json` and from
`results` (one test each).

Decisions the design did not settle:
- **No route calls the runner or `PinnedRuntime` yet.** S2.5b moves the image and video routes; until then the new path is
  exercised only by tests, as the runner was before.
- **Both adapters moved to version 2 now**, not only http-v1, so one pin and one runner path cover them. This is why the
  goldens above moved.
- **`run` returns the bundle**, not a list of files, for a version 2 job that published one.
- **An answer with no declared selectors is refused on a synchronous operation.** The legacy collection reads a graph
  history only, so it has nothing to read here.
- **Left for S2.5b:** the items listed above, plus choosing the graph engine's JSON `key` values when a bundled engine
  needs one.

### S2.5b as built

What changed:
- **Image and video routes collect by selector.** The graph round takes the binding and the contract outputs of the
  workflow's manifest. For an image job the poll carries the history entry it already read (`JobStatus.record`, a
  `FinishedRecord` with the entry and its size), and `NodeGraphAdapter.fetch_selected` collects from it with the binding's
  selectors, so no second history call is made. The file reads go through `output_selectors.FETCH_GATE` and are bounded
  while they are read: the number of files, the bytes of one file and the total are the deployment's result limits, and a
  file is refused at the first byte past a limit. The video content route collects with `adapter.collect` on the finished
  entry (`_select_video_file`) and reads the one file inside the fetch gate. The old `_first_video_output` alias is gone.
- **A bundle is named by output.** `response.selected_result` carries the output name the selector chose, and the three
  `mirror_assets` call sites (image, video, and the audio route from S2.4) pass it as the bundle result name, so an image
  job publishes a result `image`, a video job `video`. A workflow with no selector keeps the default name. The job's
  `assets` entries are unchanged: `mirror_assets(stored=)` takes the size and media type of a saved asset from a separate
  map (`_stored_asset_facts`) when the entry does not carry them, so the client view and the OpenAPI schema do not move.
- **The streaming JSON reader.** A JSON result file is read as it arrives and refused at the first byte over the limit
  (`json_too_large`), not after the file is held.
- **`results` in the job view.** A published result is listed by name with its kind and count; a JSON result also carries
  `href`, the route that reads it. An asset result has no `href`: its files are already in `assets` and fetched by their
  own route. No value is ever in the view. The field is added at every audience (the gateway's own owner view reads at the
  internal audience), for a job, the job list and the batch read, from `result_bundle.results_view`. A job that published
  nothing, or a failed job whose results are only staged, has no `results` field.
- **`GET /gateway/v1/media/jobs/{job_id}/results/{name}`.** It reads one published JSON result through
  `result_bundle.read_json` (the streaming reader with the result limits, the owner check and erasure handling that S2.3
  built). The reader (`owner_scope`, person, credential, connection) is built from the authenticated identity by
  `_gateway_result_reader`, the way the job's own authorization is. The record authorizer runs first on the job. Every
  refusal is the same 404 body (`media_job_result_not_found`): no such job or result, another maker or another App's
  credential, a staged result of a failed run, an erased or expired value, an asset result, no identity. A caller cannot
  tell them apart. The answer is the stored bytes with `Cache-Control: private, no-store` and `X-Content-Type-Options:
  nosniff`.

Tests and evidence:
- `tests/test_media_job_results.py` (16 cases, the real router on a temporary jobs database) and
  `tests/test_image_video_route_selectors.py` (13 cases on the adapter-differential rig: a selector beats a temp preview,
  an input echo and a saved file of a side branch; the history, asset and fetch-gate limits refuse before or while the file
  is read, for images and for videos; the legacy video collection skips a picture and takes the first clip; the bundle is
  named by output and listed in the view).
- Revert-checks (each mutation made the named tests fail, and was reverted): `fetch_selected` given an empty binding (four
  image cases), `_select_video_file` given an empty binding (three video cases), `with_results` returning no results (view,
  list and asset-result cases), and the record authorizer removed from the read route (the authorizer case).
- Moved goldens, on purpose (`tests/fixtures/adapter_differential/`): `image_job_succeeds.json`, `video_job_succeeds.json`,
  `image_job_oom_then_complete.json` and `audio_generate_job.json`. Paths that moved: the `media_jobs.job_envelope` row's
  `results.bundle` (null to a bundle id) and the new `result_bundle` and `result_stage` tables for image and video; the
  `result_bundle` summary in the receipt and job `metadata` for image and video; `results` (and `content-length`) in the
  client job view of all four. The audio view gained the field only; its bundle was S2.4's. The wire request to the client
  and the engine requests did not change (the goldens' request lines are identical).
- Route fixtures regenerated with the repo's tool (`scripts/generate_headless_fixtures.py` through the hermetic runner):
  `headless_openapi.json` gained the one new path and nothing else (a structural diff confirmed it), then
  `route_graph_frozen.json`, `headless_non_control_routes.json`, `route_policy_known_gaps.txt` (the new route is a 404 for the
  probe, as its sibling `.../events` is, in both sections) and `content_access_inventory.json` (the route row, and the new
  `source_sha256` of the event writers whose bodies changed). Two audio writers (`_run_audio_media_job`, `completed` and
  `started`) already had stale hashes on the S2.5a base; they were refreshed with the rest.
- Test fakes corrected to match each workflow's own selector, since the old collection took any file: video fakes use the
  key `images` and the save node of their manifest (node 60 for the ltx workflows, 18 for minimax, 6 for rife), image fakes
  use node 9, 10 or 13 as the workflow saves. These fakes were wrong about the engine, not the gateway.

Review fixes (round 2):
- **A result name is one path segment.** The read route takes `{name:path}`, so a path-shaped name never falls out of the
  handler: it gets the same 404 body as any missing name. A published name could not be read back if it held a `/`, so the
  store refuses such a name when it is staged (`bad_name`), and a manifest whose output has one is refused when it is
  checked (`RESULT_NAME_RULE` in `gateway/manifests.py`): a letter or digit first, then letters, digits, `.`, `_` and `-`,
  at most 128. Every bundled output name already fits.
- **The legacy video collection keeps its old reading** while the flag is on (`legacy_select(..., kind="video")`): only
  video files count, so a preview picture ahead of the clip is skipped, and the first clip in node order is the result, so
  several output nodes are not `result_ambiguous`. It still reads the output directory only. The legacy image collection is
  unchanged.
- **Video reads are bounded.** `NodeGraphAdapter.fetch_file` reads one chosen file inside the fetch gate and refuses at the
  first byte past `max_asset_bytes` (`asset_too_large`), as the image read does. Both video routes use it. The source-video
  helper for workflow inputs now takes the fetch gate too; it keeps its own byte bound (`media_fetch`).

Decisions the design did not settle:
- **The legacy flag stays on.** Four bundled workflows have no selectors: `face-likeness`, `flux2-klein-4b-seamless-tile`,
  `image-identity` (a gateway binding; it runs a graph) and `mesh.generate.trellis-2`. Turning it off would fail them. The
  legacy code is not deleted. The flag goes off when those four declare selectors.
- **The view carries a summary, never a value;** a JSON value is read only on its own route, so a list of jobs can never
  carry a canary.
- **One 404 for every refusal** on the read route, in line with the job route's uniform "not found" for other makers.
- **No history read per round.** The poll's entry is reused, so an image job makes the same engine calls as before.

Left:
- Selectors for the four workflows above, then the flag off by default and the legacy code deleted.
- The `check_for_save` call from the registration route (S4).
- S2.6 (the asset resolver, disclosure check and upload capability).
- Base failures seen, not chased here: `tests/test_store_write_locks.py::test_prompt_erasure_locks_each_batch_and_rolls_a_failed_batch_back`,
  `tests/test_config_keys_are_classified.py` (`asset_ref`), and
  `tests/test_no_upstream_text_in_client_errors.py::test_no_error_response_takes_text_an_upstream_wrote`
  (`NodeGraphAdapter.results`, which this slice did not change); all fail on the S2.5a base.

## Done when: the S2 acceptance matrix

1. **Data only.**
   - Random workflow, result and input names are made after the runner starts.
   - A JSON-only contract and a mixed JSON-and-audio contract run through a graph fixture and a synchronous http-v1
     fixture. One non-workflow offering envelope runs too.
   - Positive control: put back an identity or modality branch; the test must fail.
2. **Privacy.**
   - Canary values go in neutral string, enum, integer, number, boolean, nested, filename and URL inputs.
   - Check the envelope, the job, events, receipts, and the client and admin views.
   - Check forget, member-off, the kill switch, erasure and a stale worker rewrite.
   - The normal spool keeps working.
3. **Crash and replay.**
   - Crash before the envelope commit, after it, around each upload, around the attempt marker, after send but before
     the id or result is saved, during fetch and during the final commit. Count every effect exactly.
   - Concurrent same-key retries; a changed input, owner or person; erased fingerprints; both sides of the 14-day replay
     window.
4. **Pins and revocation.**
   - Change or delete the manifest, graph, selectors, protocol description and engine address between validate and
     submit, and across a restart.
   - The run uses its pins, or fails with `definition_unavailable`.
   - A revoked grant never revives.
5. **Results.**
   - Results that are missing, extra, null, an empty list, or the wrong count or type.
   - JSON that is a scalar or an array, malformed, too deep or too large.
   - Too many small files, and total bytes over the limit.
   - Source echoes, duplicate selectors, and two named outputs of the same type.
   - After any failure, no staged result can be read. After a restart, the same result is returned. Another person is
     refused. Exactly one completion receipt.
6. **Input effects.**
   - Assets that are unauthorized, forged, missing or expired.
   - URLs that are not allowed, or that redirect to a place that is not allowed.
   - A path that escapes its folder, a wrong engine destination, an audio upload and an unsupported type.
   - Each effect is counted at its own boundary.
7. **Compatibility.**
   - Wire goldens, error paths and batches do not change on the audio and image routes.
   - Unsupported combinations are refused at save.
   - The runtime still checks hostile engine replies.

The full public oracle (owner registration, hot edit, the generic route) stays with S4 to S6. S2 does not claim it.

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
| S2.5 | The v2 `start` outcome, with an http-v1 synchronous fixture. | v1 adapters are unchanged. |
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
- Limits for history bytes, selector count, fetch time and concurrent fetches (D6).

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

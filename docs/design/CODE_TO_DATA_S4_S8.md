# Code to data, slices S4 to S8: one design

Status: draft 2, 2026-11-08 (answers the freeze review; section 13). Owner: the Azizam thread. This document replaces the per-slice S4 to S8 rows of
[CODE_TO_DATA.md](CODE_TO_DATA.md) with one design, so that the five slices can be built in parallel against frozen
interfaces and land as one train.

Read first: [CODE_TO_DATA.md](CODE_TO_DATA.md) (the rule and the slice list), [CODE_TO_DATA_S2.md](CODE_TO_DATA_S2.md)
(execution envelope, typed results, D1 to D3), [CODE_TO_DATA_S3.md](CODE_TO_DATA_S3.md) (schema v2, `check_contract`,
policy facts as record data).

## 1. Goal and the oracle

**The rule.** Core never selects behaviour by workflow id, model, engine identity or intent spelling. A workflow is
data: the owner adds one, and it runs, lists and documents itself with no code change.

**Done means all of these hold on one build** (the acceptance matrix, section 11):

1. An owner saves a new workflow whose intent no code names (for example `poster.generate`) through the owner API. It
   passes the save checks, it appears in the catalog, in OpenAPI and in the MCP `run` tool once it is published, and
   `POST /gateway/v1/workflows/{id}/run` runs it to a typed result.
2. An edit to a workflow while a job of it is queued does not change what that job runs. Disable and delete while
   queued stop the job before any effect. A revoked caller stops the job before any effect.
3. A gateway restart resumes every queued workflow job from its pin, for every capability.
4. The image, video, audio and mesh routes are frontends over the same run core. Their goldens are byte-identical
   before and after.
5. The domain-literal ratchet (`[ratchets] domain_literals`, 802 today) falls to the reviewed protocol-edge sites only,
   each listed with its reason (target below 120; section 8 lists the remaining sites by kind).
6. No graph lives in Python: chain, subject-select and identity graphs are templates with declared node roles.

## 2. What exists today (main at f80a79508)

The full map with file and line references is in the lead's notes (`code-to-data/S4-S6-code-map-2026-11-08.md`). The
facts this design depends on:

- **One registry document.** Stored workflows are the `workflows` section of the service-registry JSON in
  `operator_settings` (`service_registry`). Every write is compare-and-swap on one generation (`os-N`, the ETag of
  `GET /admin/api/operator/service-registry`; `PUT` needs `If-Match`). Bundled manifests (`manifests/workflows/`, 38
  files) load into `GATEWAY_WORKFLOW_REGISTRY`; a stored record with a bundled id replaces it only with
  `overrides: <id>` (`refuse_unclaimed_bundled_workflows`).
- **Reload.** A registry write fires `reload_runtime_config`, which builds a new `WorkflowRegistry` strictly and then
  `adopt()`s it in place. A failed build keeps the old one.
- **Pins exist but are mostly unused.** The envelope (`gateway/workflow_envelope.py`) records content digests of the
  contract, binding and template, stored in `definition_store` blobs. Only `audio.generate` can compile from its pin,
  and only when `workflow_execution.compile_from_pin.audio.generate` is true; the release default is `{}`. So every
  queued job reads the live registry at run time. Registry holders (`registry:<revision>`) are designed and unused.
- **Run-by-id is video.edit only.** `POST /gateway/v1/workflows/{id}/run` is synchronous multipart, makes no job and no
  envelope, and hard-codes intent `video.edit`, protocol `openai.video`, the `videos` adapter and a video file part.
- **Each modality route wires the same calls itself.** Caller context, runtime route policy, workflow target policy,
  admission, receipt, delivery, input validation, binding, adapter, envelope, job create, schedule.
- **Hand-kept intent tables** (S5 replaces them, section 6.3): `_MEDIA_JOB_RESOURCE_CLASS_BY_INTENT`,
  `_AUDIO_MEDIA_JOB_INTENTS`, `_VIDEO_MEDIA_JOB_INTENTS`, `_IMAGE_ROUTE_INTENTS`, `_DISCOVERY_OPERATION_INTENTS`,
  `_MODEL_LISTED_MEDIA_INTENTS`, `_IDENTITY_JOB_INTENT`, `gateway/audio_media_jobs._AUDIO_INTENTS`,
  `gateway/model_metadata._INTENTS`/`_INTENT_ASSET_MODALITY`/`_CAPABILITY_INTENTS`/`_CAPABILITY_INTENT_FAMILIES`,
  `gateway/local_capabilities._ALIAS_INTENTS`, `gateway/workflow_openapi._ALIAS_OPERATION_INTENTS` and the OpenAPI
  filter on `video.edit`, `gateway/frontend_profiles` (registered by intent).
- **Known defects this design fixes.** The OpenAPI cache key (`_OPENAPI_INPUTS`) holds aliases and video limits, not
  the workflow registry, so a workflow edit does not rebuild the spec. No id, alias or OpenAPI-name clash check runs
  at save. `check_for_save` has no production caller. `classify_recovery` (the S2 D3 restart table) has no production
  caller; production recovery resumes only image, audio, video and make jobs.
- **The schema v2 contract** (S3) has `intent`, `capability` (a media class: image, video, sfx, music, mesh), `inputs`,
  `outputs` and `bindings` (keyed by adapter: `comfyui`, `http-multipart`, `gateway`). Policy facts (`exposure`,
  `trust_tier`, `resource_tier`, `tags`) are record data in `registry/workflow_policy.yml`, never in the document.

## 3. The frozen interfaces

The contracts every builder codes against are code on the design branch, frozen before any builder starts:

- `gateway/workflow_runtime_types.py`: types and protocols only (I1 to I5). The file is authoritative; this section
  states the rules that go with it.
- `gateway/workflow_kernel.py`: the shared pure rules that more than one builder must apply the same way, written once
  (section 3.0).
- `tests/workflow_runtime_fakes.py`: in-memory fakes that keep the same contracts (fail-closed facts, state kept across
  edits, annotation-free execution digests, read-only views, listener isolation, replay by key), with
  `tests/test_workflow_runtime_types.py` proving that they do.

A builder that needs a change stops that part and asks the design owner; the change is made in those files and
announced to every builder. Draft 2 (this one) answers the dot's freeze review `azizam-c2d-s4s8-design-review-1108`
(six P1 and four P2 findings); section 13 lists each finding and where it is answered.

### 3.0 The kernel: rules that must not be written twice

| Function | Rule | Used by |
|---|---|---|
| `canonical_token(name)` | lower case, every run of characters other than `a-z0-9` becomes `_`, trimmed. `A.b-C`, `a b_c` and `a.b.c` are one name. | A (clash check), B (name view, OpenAPI), D (model and target resolution) |
| `generated_names(workflow_id)` | the OpenAPI operation id `run_<token>` and the component name `<token>`. The OpenAPI builder emits no other derived name. | A, B |
| `route_key(method, path)` | `POST /v1/images`: upper method, lower segments, no trailing slash. | A, D |
| `name_clashes(pairs)` | over the whole prospective revision. Workflow ids and aliases, model ids and model aliases, and frontend ids share one token space (a request can name any of them as a target); operation names, component names and routes each have their own. One owner may hold a token many times; two owners of one token are a clash (`id_clash`, naming both). | A (every write of the registry document) |
| `execution_digest(contract)` | the digest of the contract without `annotations` at the root, in input specs (with their nested `properties` and `items`) and in output specs. S3: annotations never execute, so they never change what a job pins. | A, B, C |
| `selector_problems`, `selector_matches` | the closed selector schema: `{ids: [...]}` or `{match: {inputs: {<name>: {type} or {media_type}}, outputs: {kind, media_type}}}`; `media_type` may be a family wildcard (`image/*`). No expression language, no intent word. | A (save check), D (row validation and routing) |

Short ids (`short_id`, the id without its intent prefix) stop being addresses: the generic route and the MCP target
use full ids and declared aliases only. Two workflows of different intents with the same suffix no longer collide.

### I1. Snapshot, names and the snapshot provider

- A `RegistrySnapshot` is one immutable view of the service-registry document at generation `revision` (the N of
  `os-N`) plus the bundled release. It holds `workflows`, `frontends` and `names`. Its mappings are read-only views.
- `names` (`NameView`) is every name a request can address at that revision, built from the same document: workflow
  ids and aliases, model ids and model aliases from the services section and the dynamic aliases, frontend ids, route
  keys, generated operation and component names. Engine offerings stay in the services section (they are not folded
  into `WorkflowEntry`); the name view is the small read-only part of them that the clash check and target resolution
  need.
- `digest` covers the workflows (by `record_digest`), the frontend rows and the name view. A frontend-only or
  alias-only edit changes it.
- `WorkflowEntry.contract_digest` is the execution digest (kernel); `record_digest` is the digest of the whole stored
  record (contract with annotations, facts, state, published, overrides). `incarnation` is set when an id is created
  and kept by every later change; delete and create again gives a new one (section 5.2).
- `needs_configuration` means the facts are missing. Whether an adapter exists for a binding is a separate projection
  (`entry_problems`, I3). An entry with any problem never runs.
- **One snapshot per request.** `SnapshotProvider.current()` is the only way code reaches the registry. A request-scope
  hook takes it once and stores it on `request.state`; every read in that request uses it. No module keeps a registry
  in a closure or a global: `gateway/api.py` today captures the registry in closures (around lines 339 and 481-492);
  builder B moves those reads to the provider. A reload builds a new snapshot strictly and swaps the provider's
  reference in one assignment; a failed build keeps the old one.

### I2. The store (S4, builder A)

- Every write is one compare-and-swap on the `os-N` generation, in the existing `write_service_registry_with_audit`
  transaction. `put_workflow(if_match=None)` creates only (409 `workflow_exists`); every other write needs `if_match`
  (412 `generation_mismatch`, with the current revision).
- **The name check runs on every write of the registry document**, not only on workflow saves: this store, the
  whole-document `PUT /admin/api/operator/service-registry`, and engine, model and alias edits all build the
  prospective revision's names and refuse a write that `name_clashes` refuses. A model alias added after a workflow
  is refused the same way as a workflow added after a model.
- `check(doc, facts=..., overrides=...)` runs the whole existing save pipeline, not a reduced one: `enforce_contract`
  (validation, `check_contract`, the binding-closure and output-selector checks of `manifests.py`), the frontend
  shape profiles for every row whose selector would select the workflow, the bundled-id rule (`overrides`), the name
  check, the bounds of section 5.7 and `check_for_save`. It returns every problem with its path. `check_contract` gets
  no new mode in this train: the save and pin calls are the existing ones.
- `facts`: `KEEP` (the default) keeps the stored facts, `None` clears them (the workflow needs configuration), a
  `WorkflowFacts` sets them. `overrides` is explicit on the call and in the owner body.
- **History is by record revision.** Every accepted change writes the whole record as a blob in `definition_store`,
  held by the `registry:<workflow id>` holder for the last `history_depth` (20) changes; `HistoryEntry` carries
  `record_revision`, `record_digest`, `contract_digest`, the actor and the kind of change. `restore(id,
  record_revision)` writes that record back (contract with annotations, facts, published, overrides) as a new
  revision; the current state and incarnation stay. Two saves that differ only in annotations are two history rows
  with two restore addresses.
- `subscribe(listener)` returns an unsubscribe callable. Listeners run after the commit is live with (old, new); one
  that raises is logged and stops neither the swap nor the others.

### I3. Projections (S5, builder B)

- `Projections` is pure: it reads only its arguments. Viewer-specific and live data (authorization, availability,
  recent activity, base URL) are applied by the caller after the static projection.
- `execution_profile(entry, binding_key)`: `adapter_id`, `adapter_facet` (declared by the binding, never derived from
  an intent or a capability word), `resource_class` (a policy fact), `accepts` (per media input: types, size,
  `min_items`/`max_items`) and `produces` (per output: `kind` json or assets from the media type, the media type, and
  the S3 D5 count as `min_items`/`max_items`).
- `entry_problems(entry, adapter_ids)`: stable reason codes (`needs_policy`, `adapter_missing`, ...). The owner view,
  the catalog and the run core all use it.
- `catalog_static`, `openapi_paths` and `input_form` are the static projections; the OpenAPI builder uses only the
  kernel's generated names. **No projection cache in this train**: projections are measured uncached first (the
  catalog is already built per request). The one cache that exists, the OpenAPI document, gains `snapshot.digest` in
  its key, which fixes today's stale-spec defect.

### I4. The run core and the job service (S6, builder C)

- `submit_run(req, caller, snapshot) -> RunJobView | RunRefused`. The core never builds an HTTP response; a refusal
  is structured (`http_status`, `code`, `message`, `problems`, `retry_after_s`) and the frontend renders it.
- `RunJobView` is the one job shape every frontend renders: `job_id`, `status` (the job store's spelling:
  `canceled`), `disposition` (`new` or `replay`), `workflow_id`, `record_revision`, `binding_key`, `created_at_s`,
  `updated_at_s`, `failure` (`code`, `category`, `message`, `retryable`, `upstream_status`, `problems`) and `results`
  (one `ResultItem` per published output item).
- Media inputs are `MediaRef`s, a tuple per input name (multiplicity). Each holds `digest`, `media_type`, `size`,
  `filename`, and exactly one of `asset_id` (a library asset the caller may read) or `spool_key` (an upload staged in
  the request spool). See section 7.1 for ownership.
- `JobService` (`get`, `wait`, `cancel`, `open_result`) reads and controls one job for its caller under the existing
  media-jobs rules. Frontends and the media-jobs routes use it and never read the job store.
- `recover_runs(now_s) -> RecoveryReport`: counts per outcome of the S2 D3 classifier (section 5.4), and the ids it
  failed.
- How the core reaches the main module (caller rebuild, admission, generated values, adapters, scheduling) is C's
  own port, injected at startup; no other builder calls it, so it is not frozen here.

### I5. Frontend rows and shapes (S7, builder D)

- A route is a row (`FrontendRow`) over a reviewed wire shape (`FrontendShape`, code). A new route on an existing
  shape is data; a new shape is code.
- `selects` is the kernel's closed selector schema. Compatibility-API membership (for example the OpenAI images list)
  is a match or an id list, never the intent word.
- `profile(contract)` returns the reasons a shape cannot serve a contract; the store calls it at save for every row
  whose selector selects the workflow.
- `to_run_request` stages uploads through the request spool and returns a `RunRequest` (or its own error response for a
  malformed wire request). `to_response(outcome, row, jobs=..., caller=...)` is async: for `mode: sync` it waits through
  `JobService.wait`, reads results through `open_result`, and renders success, failure, refusal, wait timeout and
  replay in its own vocabulary.
- Route aliases (`RouteSpec.aliases`) are path aliases. Model aliases are names in the name view; resolving a model
  field to a workflow is the name view's job, not a route alias.

### I6. Owner API (S4, with the S8 form over it)

| Method and path | Body | Answer |
|---|---|---|
| `GET /admin/api/workflows` | | every entry: id, state, published, record_revision, contract_digest, record_digest, provenance, `entry_problems`; `ETag: "os-N"` |
| `GET /admin/api/workflows/{id}` | | the entry, its contract, facts, problems and the last 20 history entries; `ETag` |
| `POST /admin/api/workflows/check` | `{contract, facts?, overrides?}` | the report (every problem with its path, including name clashes against the current revision); writes nothing |
| `POST /admin/api/workflows` | `{contract, facts?, overrides?}` | 201 and the `Commit`; 409 `workflow_exists`; 422 with problems |
| `PUT /admin/api/workflows/{id}` | `{contract, facts?, overrides?}`, `If-Match` | the `Commit`; 412; 422. An absent `facts` keeps them; `facts: null` clears them |
| `POST /admin/api/workflows/{id}/state` | `{state}`, `If-Match` | the `Commit` |
| `POST /admin/api/workflows/{id}/publish` | `{published}`, `If-Match` | the `Commit` |
| `POST /admin/api/workflows/{id}/restore` | `{record_revision}`, `If-Match` | the `Commit` |
| `DELETE /admin/api/workflows/{id}` | `If-Match` | the `Commit`; 409 `id_is_bundled` for a bundled id |

The web owner view (E) is the one client this train ships. The MCP owner tools and a CLI over the same routes follow
in a later train (cut, section 12): they add no rule and the oracle does not need them.

### I7. Defaults (S8)

Only the defaults S4 to S7 actually read move in this train, each to a `registry/defaults/<section>.yml` file with its
schema, read through the config-homes reader: `workflow_execution.max_wait_s` (60), `workflow_execution.history_depth`
(20), `workflow_bounds.{max_template_bytes, max_nodes, max_depth}` (section 5.7). `workflow_execution.compile_from_pin`
is deleted. Other literal defaults stay where they are.

## 4. Prior art

The registry questions were compared across 12 systems (Replicate, fal, Modal, AWS Lambda, Temporal, Argo Workflows,
GitHub Actions, n8n, Windmill, Kubernetes, Envoy xDS, Comfy Registry) in the divan entry
`system-design/live-versioned-registries-separate-definition-identity-routing-and-execution-control-and-disagree-on-what-happens-to-queued-and-running-work.md`
(2026-11-08, a documentation comparison, not an execution test). What this design takes from it:

- **Identity, route and run control are separate objects** (Lambda versions and aliases, Replicate versions and
  deployments, Windmill hashes and path heads). Here: the content digest is the identity (5.1), the workflow id is the
  route, and `state`/`published` are run control (5.2, 5.9).
- **Disable has no common meaning.** Replicate's deployment disable cancels in-flight work; GitHub's workflow disable
  leaves existing runs alone; a Kubernetes Job suspend deletes active Pods. So 5.2 states its own rule and the reason.
- **Retry is a new decision point.** n8n offers the original or the current definition; Windmill lets a restart pick a
  version; Temporal recovers pinned runs separately from routing rollback. 5.4 names which definition each restart uses.
- **CAS protects authors, not executions** (Lambda RevisionId, GitHub replacement SHA, Kubernetes resourceVersion). 5.3
  uses CAS for edits only; the run-time state check of 5.2 is a separate rule.
- **Rollback is another change** (Modal new deployment, Git revert, n8n restore then publish). 5.5 restore writes a new
  revision.
- **Argo Secure mode errors a running workflow when its template changes.** This design does not copy it: a running job
  finishes on its pin (5.2).
- **Bounds evidence:** GitHub workflow files at most 500 KB, reusable workflows 10 levels deep and 50 unique; Argo
  objects under 1 MB including status, recursion 100 by default. The 512 KiB / 2,000 nodes / depth 32 bounds of 5.7 sit
  inside these.
- **Publication is not always separate from save** (Lambda `$LATEST` is invocable unpublished; n8n settings edits
  republish). 5.9 makes save and publish separate and says what unpublished means.
- **No system has an atomic revocation check before every external effect.** 5.2 checks once, before the first effect;
  it claims no more than that.
- **Cache invalidation guarantees are mostly undocumented**; Replicate keeps the schema per version. 5.8 keys every
  derived view by the snapshot digest, so no invalidation step exists to forget.

## 5. S4: the live versioned registry

### 5.1 What a job pins

A job pins content, not a revision. The envelope's definitions are content digests in `definition_store` (the
contract by its execution digest, the binding, the template, the protocol revision), plus the pinned executor and
upstream identity (adapter, interface version, service id, origin digest). The job's `job:<id>` references hold those
blobs **until the envelope itself is deleted**, in the same transaction as that delete (S2), not when the job
finishes: a retained envelope must always find its definitions. `source.record_revision` and `source.incarnation`
are recorded for audit, the receipt and the stop rule (5.2); execution never reads the registry at a past revision.
Precedent: a Replicate prediction names a version id; a GitHub Actions run reads the workflow at its triggering SHA;
a Lambda durable execution stays on its version when the alias moves (section 4).

### 5.2 Update, disable and delete while queued or running

**The first-effect boundary.** A job is *running* from the moment the worker commits status `running` in the job
store. The worker does, in this order: commit `running`; then the dispatch checks below, against the provider's
current snapshot and the current caller; then the first effect (adapter submit). A change committed after the checks
pass is a change *while running*. A change committed before them is a change *while queued*. There is no window in
which a job is neither.

**The dispatch checks** (once, in this order; a failure fails the job with its code and makes no engine call):
1. Rebuild the caller from the spool (`_auth_context_from_image_job_payload`): a revoked key, App, person or MCP
   connection fails the job.
2. The workflow still exists as the same incarnation: `snapshot.get(id)` is present and its `incarnation` equals the
   envelope's. Absent, or a different incarnation (deleted, or deleted and created again), fails with
   `workflow_deleted`.
3. Its `state` is `active`; `disabled` fails with `workflow_disabled`.
4. Its current facts and the current caller pass the workflow target policy (exposure, plan), as revocation does; no
   facts fails closed (`needs_policy`). The current `published` flag is **not** re-checked: unpublish controls new
   submissions and visibility, not admitted work.
5. `entry_problems` is empty for the pinned binding's adapter.
6. Re-admission (no queue count).

The pinned contract, binding and template are what runs; the live entry supplies only these checks.

| Owner action | Queued job | Running job |
|---|---|---|
| Update (contract, binding, facts) | Runs its pinned definitions; current facts are re-checked (check 4). | Finishes on its pin. |
| Disable | Fails at dispatch with `workflow_disabled` if the workflow is still disabled then. Disable then enable before dispatch: the job runs. | Finishes. The owner can cancel it. |
| Delete | Fails at dispatch with `workflow_deleted`, also when the id was created again (new incarnation). | Finishes. Its pinned blobs stay while its envelope exists. |
| Unpublish | Runs (it was admitted while published); the caller still passes check 4. | Finishes. |
| Policy fact change | Re-checked at dispatch (check 4). | Finishes. |

Why the check is at dispatch, not a seal at disable time: the registry is in `admin.db` and the jobs are in the media
jobs database, so no one transaction covers both; a dispatch check with the incarnation gives the same result for
delete and an honest, stated result for disable then enable (the owner's latest decision wins). This is the GitHub
rule (disable leaves existing runs), applied at the queue boundary; Replicate's cancel-on-disable is the rejected
alternative, because an owner who wants that has the cancel route. No system in the research has an atomic
revocation check before every external effect; this design checks once, at the boundary, and claims no more.

### 5.3 Compare-and-swap

Every write carries `If-Match: "os-N"` (create carries none). The store compares it with the current generation inside
the write transaction (`write_service_registry_with_audit`, which already does this) and refuses with 412
`generation_mismatch` when they differ. The answer body names the current revision so the editor can re-read. CAS
protects authors, not executions: the dispatch checks of 5.2 are a separate rule.

### 5.4 Restart

At startup, `recover_runs` runs the S2 D3 classifier (`workflow_envelope.classify_recovery`, first match wins) for
every unfinished workflow job of every capability, and acts per row:

| Classifier row | Meaning | Action |
|---|---|---|
| `terminal` | succeeded, failed or canceled | nothing |
| `replay` | a result bundle is committed but the job is not terminal | finish the job from the committed bundle (publish, receipt). This follows the code; S2 prose calls it invariant repair, and the action is the same: no engine call. |
| `poll` | a protocol job id is recorded (the engine accepted it) | poll that engine job through the pinned upstream identity, without the dispatch checks (it is already running, 5.2) |
| `submission_unknown` | the submit started and no engine job id was recorded | fail with `submission_unknown`; never resubmit blindly |
| `resume` | not submitted, spool present | run `execute_run`: the dispatch checks of 5.2 apply, then the first effect |
| `interrupted_before_submit` | not submitted, spool gone (forget, member off, kill switch) | fail with `spool_missing` |

An unfinished job with no envelope at all (queued before this train by a route that did not record one) cannot run
from a pin; it fails with `pin_not_recorded` (a new failure code; the existing 503 for an unrecorded pin carries none) and is counted. The deploy lists such jobs
first; there are usually none, because routes that generate values already require an envelope.

A job whose envelope does not parse (an unknown `envelope_version`) fails with `envelope_version_unsupported` and is
counted. Restart after a crash uses the pin. "Run again" (an owner or App asking for a finished job to be repeated) is
a new `submit_run` against the current definition, a new job with a new pin, and says so in its receipt. There is no
"rerun on the old definition" in this train: an owner who wants that restores the old version first (5.5).

**Envelope versions.** Envelope v2 adds `source.incarnation` and `source.binding_key` (section 7.4). The new build
reads v1 and v2 and writes v2. A v1 job (queued before the train) has no incarnation and no binding key: dispatch check
2 compares only presence, and the receipt names the binding as unknown. Nothing rewrites an old pin from the live
registry.

### 5.5 Rollback

- **Owner rollback of one workflow:** `restore(id, record_revision)` writes that record back as a new revision.
- **Gateway code rollback (a train rollback):** one gateway instance runs at a time (the single-instance lock), and
  `scripts/safe-restart` drains running async media jobs before it restarts, so there is no mixed fleet and no
  running v2 engine job is left to an older build. Queued v2 jobs are the exposure: the previous build reads only v1
  and fails them as `definition_unavailable` (its existing parse failure), with no engine call. The train's rollback
  note says so and lists the queued workflow jobs at rollback time. Stored workflow records stay readable by the
  previous build only if their schema step is one it knows; the schema step of section I2 (published and state) is
  additive, so the previous build ignores the new keys.

### 5.6 Clash refusal

The name rule is the kernel's `name_clashes` over the whole prospective revision, run by every write of the registry
document (I2). It covers generated names (two ids whose tokens collide, such as `a-b` and `a.b`, are refused), model
ids and model aliases in both directions (an alias added after a workflow is refused as a workflow added after the
alias is), frontend ids, and routes by `route_key`. A self-update is not a clash (one owner). A stored workflow with
a bundled id still needs `overrides: <bundled id>`; the overridden bundled entry leaves the name view. Ambiguity is
refused, never resolved by a heuristic winner. MCP alias resolution that today picks between a generate and an edit
workflow by inputs is a frontend selection over declared ids, not a name clash; it stays as is (section 12).

### 5.7 Inline templates and structural bounds

A stored workflow may carry its template inline (`bindings.<key>.template: {inline: {...}}`). Bounds, checked at save
and again at pin: template bytes (canonical JSON) at most 512 KiB, nodes at most 2,000, nesting depth at most 32. The
limits are deployment data (`workflow_bounds`, I7). A bundled workflow keeps its template path; the loader reads it
into a blob at load, so the job pins the same digest either way.

### 5.8 Cache invalidation

Projections are pure and uncached in this train (cut: the review found the catalog folds viewer and live data, so a
digest-keyed cache of the whole catalog would leak across viewers). The one cache that exists stays: the OpenAPI
document is keyed by `snapshot.digest` together with today's `_OPENAPI_INPUTS`, which fixes the stale-spec defect. A
projection cache can come later, keyed by the digest, once uncached cost is measured.

### 5.9 Publish is a separate decision

`published` defaults to false for a stored workflow and comes from the release policy for a bundled one (all 38 are
published today). An unpublished workflow is visible to the owner (owner API, owner catalog view) and runs only for the
owner; it is absent from App catalogs, App OpenAPI and the MCP tools.

## 6. S5: one effective registry and its projections

### 6.1 One snapshot over workflows and engine offerings

`RegistrySnapshot` holds workflows, frontends and the name view. Engine offerings (models that engines serve) stay in
the service registry's `services` and are projected by the existing model catalog; the snapshot carries only their
names (ids and aliases, I1), built from the same document, so the clash check (5.6) and target resolution read the
same revision. A full unification of offerings into the snapshot is not needed for the oracle and is out of scope.

### 6.2 Every listing reads one revision

Catalog (`/me/api/workflows`, `/gateway/v1/workflows`), OpenAPI, forms, policy decisions and receipts read the same
`RegistrySnapshot` reference for the life of a request. The receipt records `ReceiptIdentity` (I3), so a receipt says
which revision ran.

### 6.3 The intent tables and where their data goes

| Table today | Replacement |
|---|---|
| `_MEDIA_JOB_RESOURCE_CLASS_BY_INTENT` | `PolicyFacts.resource_class` (record data; migration writes today's values) |
| `_AUDIO_MEDIA_JOB_INTENTS`, `audio_media_jobs._AUDIO_INTENTS`, `_VIDEO_MEDIA_JOB_INTENTS`, `_IDENTITY_JOB_INTENT` | the frontend row's `selects` (I5) |
| `_IMAGE_ROUTE_INTENTS`, `_DISCOVERY_OPERATION_INTENTS`, `workflow_openapi._ALIAS_OPERATION_INTENTS`, the `video.edit` OpenAPI filter | frontend rows and the kernel's `generated_names` |
| `_MODEL_LISTED_MEDIA_INTENTS` | a frontend row flag `lists_as_model: true` |
| `model_metadata._INTENTS`, `_INTENT_ASSET_MODALITY`, `_CAPABILITY_INTENTS`, `_CAPABILITY_INTENT_FAMILIES` | computed from the contract's inputs and outputs (`ExecutionProfile.accepts`/`produces`); OpenAI-style metadata is a lossy edge projection and never gates execution |
| `local_capabilities._ALIAS_INTENTS` | model aliases in the name view (it resolves a model target, not a route) |
| `frontend_profiles` (keyed by intent) | `FrontendShape.profile` (I5), run for the rows whose selector selects the workflow |

A builder that finds another table keyed by an intent, capability word, model or engine adds it here in the same
commit. The final review greps for dict literals keyed by intent strings.

## 7. S6: the generic run route

### 7.1 Request and media ownership

`POST /gateway/v1/workflows/{id}/run` (full id only), JSON body `{"inputs": {...}, "binding": "<key>"?}`, or
multipart with a JSON part `inputs` and file parts named by input (repeat a name for a multi-item input). A media
input may instead be a library asset reference `{"asset_id": "..."}` in `inputs` (the shared library input, per the
Library pick rule). The `Idempotency-Key` header is the idempotency key.

Uploads: the frontend shape streams each file part into the **request spool** (sniffed type, size, digest, original
filename) and passes `MediaRef(spool_key=...)`. `submit_run` adopts the staged files into the new job's spool in the
step that creates the job, so a queued job owns its inputs before it is visible. Staged files no job adopted (a
refusal, a replay, an error) are deleted when the request ends. Library assets are checked for the caller at submit
and again at dispatch (check 1 of 5.2 covers the caller; the asset read is the existing ownership rule). Peak memory
stays bounded by streaming to the spool, as the audio job route does today.

### 7.2 Submit order: replay first

`submit_run` does, in this order:
1. **Replay.** With an idempotency key, compute the request identity (owner scope, maker, workflow id, binding key,
   inputs, media digests) and look the key up (`media_jobs`' existing replay rule: same owner scope, maker and input
   hash, within the replay window). A match returns that job's view with `disposition: replay` and adopts nothing,
   draws nothing, admits nothing, validates nothing against the current contract. A key with a different identity is
   `idempotency_conflict` (409).
2. Resolve the entry in the request's snapshot; the workflow target policy (exposure, `published`, plan);
   `entry_problems` empty.
3. Validate inputs against the current contract (`validate_inputs`) and media against `ExecutionProfile.accepts`
   (types, size, item counts); every problem at once (422).
4. Admission (`resource_class` from the profile).
5. Generated values, once (S2).
6. Build the v2 envelope and `media_jobs.create(..., envelope=..., require_envelope=True)` with the adopted spool. The
   create keeps its own replay check under `BEGIN IMMEDIATE`: if a concurrent request with the same key won, create
   returns that job, this request returns it as a replay, and its own generated values are discarded unseen. So a
   replay never shows a second draw, and the race is safe without a separate unchecked read.
7. Schedule. Answer `RunJobView` with `disposition: new`.

### 7.3 Always a job, with a bounded wait

Every run is a job. The generic route answers the job view. `Prefer: wait=N` (N at most `max_wait_s`, 60) holds the
answer until the job finishes or N passes, then answers the view with its results, or the view with status `queued`
or `running` and `Location` of the job. This is the Replicate `Prefer: wait` pattern. A synchronous frontend (`mode:
sync`) is the same submit plus `JobService.wait` in the shape.

### 7.4 Envelope version 2

`envelope_version` 2 adds, under `source`, `incarnation` (text) and `binding_key` (text), both required in v2, and
renames nothing. `source.revision` keeps its S2 meaning (the digest of contract and binding); the entry's
`record_revision` is added as `source.record_revision` (integer, audit only). The parser accepts 1 and 2 (5.4); the
writer writes 2.

### 7.5 Results, events, cancel

The existing media job routes serve every workflow job through `JobService`: `GET /gateway/v1/media/jobs/{id}`,
`.../results`, `.../events`, `POST .../cancel`. Cancel keeps today's rules: the job's caller or the owner may cancel;
cancel of a terminal job is a no-op that answers the job; cancel racing completion ends in whichever state commits
first (the job store's `cancel_requested_at` is already checked at each poll); a canceled job's spool is released by
the existing cleanup. The job's `operation` is `workflow.run`; its `resource_class` comes from the profile.

### 7.6 The old video.edit run route

It is replaced by the generic route on the same path, with no compatibility branch (the product is unreleased). The
video-edit multipart client shape becomes a row with a multipart shape over `submit_run` if a named client needs it;
the goldens decide (S7).

## 8. S7: modality routes become frontends; graphs leave Python

- **Method:** goldens first, as for the audio route: record each route's answer for a fixed request set on main, then
  move the route onto a frontend row over `submit_run` and prove the same bytes (images, audio, video) or the same
  structure with masked ids and times (job objects).
- **Routes:** OpenAI images (generations, edits, variations), the image jobs route, audio generate (sync and job),
  OpenAI video create and edit, mesh. Mesh follows the decided plan: `/v1/3d/generations` in LocalAI's shape plus the
  raw-GLB multipart row; `/v1/meshes/generate` goes.
- **Graphs:** the chain, subject-select and identity graphs become templates with declared node roles, loaded like any
  bundled template; `_meta` leaves image and video.
- **What stays code:** the wire shapes, their error vocabularies, the adapters' internals, the security limits. Each
  remaining domain literal is listed in the ratchet's reviewed-site list with its reason.

## 9. S8: defaults, config and the owner UI

- Literal defaults leave with the slice that replaces their owner (I7).
- Owner web UI: the Workflows page gains an owner view: the list from `GET /admin/api/workflows` with state, published
  and needs-configuration; a detail sheet with history and restore; an editor for a workflow document (a code editor
  with the check route's problems shown at their paths); publish and disable toggles with the ETag. No free-text
  identifiers: the binding key, adapter, engine and model are pick-lists from the snapshot and the service registry.
- The owner API is the agent interface in this train; the MCP owner tools and a CLI over it are cut to a later train (I6, section 12).

## 10. Build plan

### 10.1 Children and file ownership

| Child | Slice | Owns (writes) | Reads | Route |
|---|---|---|---|---|
| A | S4 store | `gateway/workflow_registry_store.py` (new), `gateway/service_registry.py` workflow section and the name check on every document write, `gateway/admin_store.py` holders, `gateway/definition_store.py` holders, the owner routes in `gateway/admin_api.py` (and the whole-document PUT's name check) | I1, I2, I6, kernel | Sonnet child |
| B | S5 snapshot and projections | `gateway/registry_snapshot.py` (new: the snapshot and name-view builder, the `SnapshotProvider`, the request-scope hook), `gateway/workflow_projections.py` (new), `gateway/workflow_policy.py` (the `resource_class` fact: parser, schema and data migration in one commit), `registry/workflow_policy.yml`, `gateway/workflow_openapi.py`, `gateway/workflows_catalog.py`, `gateway/model_metadata.py`, `gateway/local_capabilities.py`, `gateway/api.py` (registry reads move to the provider) | I1, I3, kernel | Sonnet child |
| C | S6 run core | `gateway/workflow_run.py` (new: `RunCore`, `JobService`, its port to the main module), the generic route row, `gateway/workflow_envelope.py` v2 (read 1 and 2), `gateway/job_envelope.py`, the request spool and adoption, recovery wiring | I1, I3, I4, kernel | Sonnet child |
| D | S7 frontends | `gateway/frontend_shapes/` (new), `registry/protocol_frontends.yml`, the modality route handlers in the main module, templates for chain, subject-select and identity | I4, I5, kernel | Sonnet child; golden test design on the dot |
| E | S8 | `registry/defaults/*` (the I7 keys only), the web Workflows owner view | I6, I7 | the dot |

The snapshot builder's signature, which A calls and B writes: `registry_snapshot.build(document, bundled, *,
generation) -> RegistrySnapshot`, where `document` is the service-registry document at generation `generation` and
`bundled` is the bundled release. Until B lands, A uses `tests/workflow_runtime_fakes.FakeSnapshot`.

The main module is edited by A (owner route registration only), B (the request-scope hook registration), C (route row
wiring, worker and recovery calls) and D (handler bodies). Each child keeps its main-module edits to the functions its
brief names and lists them in its report.

### 10.2 What forces an order

1. **The interfaces and the kernel, first.** `gateway/workflow_runtime_types.py`, `gateway/workflow_kernel.py`, the
   fakes and this document are frozen (draft 2 plus its re-review) before any child starts. The kernel removes the
   semantic dependencies the review found between A, B and D (names, generated names, selectors, digests): each of them
   calls the same code instead of matching a description.
2. **Fakes, not waits.** Each child builds against the fakes of the interfaces it reads. The fakes keep the contracts,
   so a child cannot learn a wrong rule from them.
3. **Atomic transitions inside one child.** B's policy parser, schema and data migration land in one commit; A's
   record schema step (state, published, incarnation) lands in one commit with its readers; C's envelope reader
   accepts v1 and v2 in the same commit that makes the writer write v2.
4. **End-to-end proof waits on integration.** D's goldens pass end to end only with C's core; the acceptance matrix
   runs only on the integration branch. The goldens cover failure, replay, wait timeout and cancel as well as success.
5. **Merge order on `release/c2d-int`:** A, then B, then C, then D, then E. A later child rebases on the branch, not on
   main. This is a merge order, not a design order.

### 10.3 Landing

One train: the integration branch behind the full suite (hermetic pytest, vitest, Playwright, the gate), the
acceptance matrix, a dot review of the whole diff, and the train rule (one-command rollback, post-deploy checks).
Rollback note: queued workflow jobs with envelope v2 fail as `definition_unavailable` on the previous build (5.5); the
deploy lists them.

## 11. The acceptance matrix

| # | Case | Proves |
|---|---|---|
| 1 | An owner saves `poster.generate.demo` (an intent no code names) through the owner API, publishes it and runs it through the generic route to a result bundle. | data-only workflows |
| 2 | The same workflow appears in `/gateway/v1/workflows`, `/me/api/workflows`, OpenAPI and the MCP `run` tool only after publish. | publish is separate; one revision |
| 3 | Edit while a job is queued: the job's result comes from the pinned graph (distinct output marker). | pin by content |
| 4 | Disable while queued: the job fails with `workflow_disabled` and no engine call is made (fake adapter counter at zero). | owner stop before effects |
| 5 | Delete while queued: `workflow_deleted`, no engine call. | |
| 6 | Revoke the caller's key while queued: the job fails, no engine call. | revocation at run |
| 7 | Two edits with the same `If-Match`: the second gets 412. | CAS |
| 8 | Save with an id whose token equals a listed model alias, a frontend id, or another workflow's generated name: each 422 `id_clash`. | clash refusal |
| 9 | Save an inline template over each bound (bytes, nodes, depth): 422 with the bound. | bounds |
| 10 | Restart with a queued job of each capability: each resumes from its pin; an unfinished job without an envelope fails `pin_not_recorded`. | restart |
| 11 | Restore an old record revision: a new revision with the old record digest. | rollback |
| 12 | OpenAPI before and after a workflow edit differ in that workflow's schema. | cache key |
| 13 | Every modality golden is byte-identical (or structure-identical where it holds ids and times). | frontends preserve behaviour |
| 14 | The core module has zero domain literals; the ratchet is under the target with every remaining site listed. | the rule |
| 15 | A schema miss, a policy denial and an admission refusal through the generic route each give their code. | errors |
| 16 | Queue a job, delete the workflow, create the same id again, dispatch: `workflow_deleted`, no engine call. | incarnation |
| 17 | Queue, disable, enable, dispatch: the job runs. Queue, disable, dispatch: `workflow_disabled`. | the stated disable rule |
| 18 | Submit with a key, add a required input to the workflow, submit the same request with the same key: the first job, `disposition: replay`, no second generated value. The same key with other inputs: 409 `idempotency_conflict`. | replay first |
| 19 | Two concurrent submits with one key: one job; both answers name it. | race-safe create |
| 20 | Edit only a frontend row or only a model alias: the snapshot digest changes and OpenAPI is rebuilt. | snapshot identity |
| 21 | Two saves that differ only in annotations: two history rows, the same `contract_digest`; restore of the first gives the first record's annotations. | history identity |
| 22 | Add a model alias whose token equals a workflow id (whole-document PUT): 422 `id_clash`. Save workflows `x.a-b` and `x.a.b`: the second is refused. | names in both directions |
| 23 | Restart with one job in each classifier row: each takes its 5.4 action; the `poll` job is not stopped by a disable made before the restart. | restart table |
| 24 | A v1 envelope queued before the train runs on the new build. | v1 read |
| 25 | A refused multipart submit leaves no staged file; an accepted one moves its files into the job spool. | media ownership |
| 26 | Cancel a queued job, cancel a finished job, cancel racing completion: each ends in one committed state and the job service answers it. | cancel |

## 12. Open questions and cuts

1. Unpublish while queued (5.2): the draft runs it, because the caller was admitted. The research found no precedent at
   the queue boundary (n8n unpublish stops triggers; its effect on queued executions is unverified), so this stays a
   judgement call.
2. The history depth (20) is a first guess, set as data.
3. Whether a synchronous OpenAI images call through a job adds latency the goldens notice; measured on the
   integration branch before the train.
4. "Run again on the original definition" (5.4) is not offered. n8n offers both; add it later only if owners ask.
5. MCP alias resolution that picks between a generate and an edit workflow by the caller's inputs (5.6) stays as is;
   whether it should become a declared frontend selection is a later decision.
6. **Cut from this train** (the oracle does not need them): the projection cache (5.8), the MCP owner tools and the
   owner CLI (I6), defaults outside the I7 keys, an expression language for selectors.

## 13. Draft 2: the freeze review and where each finding is answered

| Review finding (dot, `azizam-c2d-s4s8-design-review-1108`) | Answer |
|---|---|
| P1 snapshot identity omits frontends and names; caching folds viewer data; captured registries in `api.py` | I1 (`names`, digest over workflows, frontends and names; `SnapshotProvider`; B migrates `api.py`), I3 and 5.8 (no projection cache) |
| P1 idempotency resolved after validation, admission and generation | 7.2 (replay first; create's own race check; a loser's draws discarded unseen); matrix 18-19 |
| P1 C to D media ownership and the result, error and cancel exchange | I4 (`MediaRef` tuples, `RunJobView`, `RunFailure`, `RunRefused`, `JobService`, `canceled`), I5 (async `to_response`), 7.1, 7.5; matrix 25-26 |
| P1 delete and create again bypasses the stop; no first-effect boundary; policy re-check; recovery through the state gate | 5.2 (incarnation, the boundary, checks 1-6, disable then enable stated), 5.4 (`poll` skips the dispatch checks); matrix 16-17, 23 |
| P1 the D3 table and the envelope version transition | 5.4 (all six rows and their actions; v1 and v2 read, v2 written), 5.5 (single instance, drain, queued v2 on rollback), 7.4; matrix 24 |
| P1 clash rules miss generated names and later writes | 3.0 (kernel), 5.6, I2 (every document write); short ids stop being addresses; matrix 22 |
| P2 save validation pipeline, facts in check, `resource_class` parser, availability versus policy | I2 (whole existing pipeline, no new mode; `KEEP`/`None`), 10.1 and 10.2 (B's parser and migration in one commit), I3 (`entry_problems`) |
| P2 execution digest versus history identity; refs released too early | I1 (`record_digest`), I2 (restore by record revision), 3.0 (`execution_digest`), 5.1 (refs until envelope deletion); matrix 21 |
| P2 fakes teach wrong semantics | the fakes rewritten; `tests/test_workflow_runtime_types.py` proves missing facts, kept state, incarnation, annotation-only restore, read-only views, listener isolation, replay |
| Cuts | section 12 item 6 |

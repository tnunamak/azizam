# Code to data, slices S4 to S8: one design

Status: draft 1, 2026-11-08. Owner: the Azizam thread. This document replaces the per-slice S4 to S8 rows of
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

These are the contracts every child builds against. They are frozen in code in `gateway/workflow_runtime_types.py`
(types and protocols only, no behaviour) on the design branch before any builder starts. Where the sketches below and
that file differ, the file is right. A child that needs to change
one stops and asks the design owner; the change is made in that file and announced to every child.

### I1. `RegistrySnapshot`: one immutable view of everything that can run

```python
@dataclass(frozen=True)
class WorkflowEntry:
    id: str                          # unique across workflows, offerings and frontend operation names (I2 clash rules)
    contract: Mapping[str, Any]      # the validated schema-v2 document, read-only
    contract_digest: str             # sha256:<hex> of the canonical contract (S2 D1); excludes annotations
    bindings: Mapping[str, BindingDef]   # binding key -> its definition and digest
    facts: WorkflowFacts | None      # exposure, trust_tier, resource_tier, resource_class, tags; None = needs configuration
    provenance: Provenance           # bundled | stored | package; who saved it; overrides: <bundled id> | None
    state: Literal["active", "disabled"]
    published: bool                  # visible to Apps (catalog, OpenAPI, MCP, run); saving never publishes
    record_revision: int             # the registry revision at which this entry last changed

@dataclass(frozen=True)
class BindingDef:
    adapter_id: str                  # the reviewed adapter the binding key names
    binding: Mapping[str, Any]       # the binding body, closed by the adapter descriptor
    binding_digest: str
    template: TemplateRef | None     # inline body (S4) or a bundled path; always with its digest

@dataclass(frozen=True)
class RegistrySnapshot:
    revision: int                    # the registry document generation (the N of os-N)
    digest: str                      # sha256 over every entry's (id, record_revision, contract_digest, state, published)
    workflows: Mapping[str, WorkflowEntry]
    frontends: Mapping[str, FrontendRow]     # I5
    def get(self, workflow_id: str) -> WorkflowEntry | None: ...
    def by_intent(self, intent: str) -> tuple[WorkflowEntry, ...]: ...   # an index for listings, never a switch
    def runnable(self, workflow_id: str) -> WorkflowEntry | None: ...    # active, facts present, not needs-configuration
```

Rules:
- A snapshot is built once per registry revision and never mutated. Readers hold a reference for the life of one
  request; a reload swaps the module-level reference atomically (one assignment), it does not `adopt()` in place.
- `revision` is the existing `os-N` generation. There is no second counter: every registry write already bumps it in
  the same transaction as the audit row.
- `digest` is the cache key for every projection (I3). Two snapshots with the same digest project the same output.

### I2. `RegistryStore`: edits, checks and history (S4)

```python
class RegistryStore(Protocol):
    def snapshot(self) -> RegistrySnapshot: ...
    def check(self, doc: Mapping[str, Any], *, workflow_id: str | None, snapshot: RegistrySnapshot) -> CheckReport: ...
    def put_workflow(self, doc: Mapping[str, Any], *, if_match: int | None, actor: Actor, facts: WorkflowFacts | None = None) -> Commit: ...
    def set_state(self, workflow_id: str, state: Literal["active", "disabled"], *, if_match: int, actor: Actor) -> Commit: ...
    def set_published(self, workflow_id: str, published: bool, *, if_match: int, actor: Actor) -> Commit: ...
    def delete_workflow(self, workflow_id: str, *, if_match: int, actor: Actor) -> Commit: ...
    def history(self, workflow_id: str, *, limit: int = 20) -> tuple[HistoryEntry, ...]: ...
    def restore(self, workflow_id: str, contract_digest: str, *, if_match: int, actor: Actor) -> Commit: ...
    def subscribe(self, listener: Callable[[RegistrySnapshot, RegistrySnapshot], None]) -> None: ...

@dataclass(frozen=True)
class Commit:
    revision: int                    # the new os-N
    workflow_id: str
    record_revision: int
    contract_digest: str | None      # None after a delete

class RegistryConflict(Exception): ...       # if_match is not the current revision -> 412 generation_mismatch
class RegistryRefused(Exception):            # a save check failed -> 422 with every problem
    problems: tuple[Problem, ...]            # Problem(path, code, message), the S3 Report shape
```

Rules:
- `if_match` is the snapshot revision the editor read (the existing ETag). `put_workflow` with `if_match=None` creates
  only, and fails with `RegistryConflict` when the id exists (create-if-absent, as Kubernetes `create`).
- `check` is `check_contract(doc, context(mode="save"))` plus the save-only checks of section 5.6 and 5.7. `put_workflow`
  runs `check` inside the write transaction against the snapshot at `if_match`.
- Every committed contract, binding and template body is written to `definition_store` as a blob and held by a
  `registry:<workflow id>` holder for the last 20 versions (`history`). `restore` re-puts an old version by digest as a
  new revision. The whole-document `PUT /admin/api/operator/service-registry` stays for engines and frontends; its
  `workflows` section goes through the same `check`.
- `subscribe` listeners run after the commit, with the old and new snapshot, on the reload path. They must not raise;
  a listener error is logged and the snapshot swap still happens.

### I3. Projections: pure functions of a snapshot (S5)

```python
def execution_profile(entry: WorkflowEntry, binding_key: str) -> ExecutionProfile: ...
def catalog(snapshot: RegistrySnapshot, viewer: Viewer) -> CatalogView: ...
def openapi_paths(snapshot: RegistrySnapshot) -> Mapping[str, Any]: ...          # cached by snapshot.digest
def input_form(entry: WorkflowEntry) -> FormSpec: ...
def receipt_identity(entry: WorkflowEntry, binding_key: str) -> ReceiptIdentity: ...

@dataclass(frozen=True)
class ExecutionProfile:
    adapter_id: str
    adapter_facet: str               # what the graph backend's record calls this work (today "images", "videos", "audio")
    resource_class: str              # the admission and queue class (today "image", "video", "audio")
    admission: Literal["job"]        # every workflow run is a job (section 7.2)
    accepts: Mapping[str, MediaAccept]   # input name -> accepted media types and size, from the contract
    produces: Mapping[str, OutputKind]   # output name -> json | asset | list, from the contract

@dataclass(frozen=True)
class ReceiptIdentity:
    workflow_id: str
    record_revision: int
    contract_digest: str
    intent: str                      # copied for display and filters; never used to choose behaviour
```

Rules:
- A projection reads only its arguments. No projection reads a global, the live config or the clock.
- `resource_class` is a policy fact (I1 `WorkflowFacts.resource_class`; `gateway/workflow_policy.PolicyFacts` gains the field). The migration writes today's value for every
  bundled workflow from `_MEDIA_JOB_RESOURCE_CLASS_BY_INTENT`; a workflow without it needs configuration (fails closed,
  as S3 D2 does for the other facts).
- `adapter_facet` is declared by the adapter descriptor for the binding key, from the binding body. For the graph
  adapter the binding names it (`comfyui: {facet: images, ...}`); the migration writes today's value for every bundled
  workflow. No projection maps an intent or a capability word to a facet.
- Intent stays an attribute and an index. `by_intent` serves listings that a frontend declares (I5); core never
  branches on its value.

### I4. The run core (S6)

```python
@dataclass(frozen=True)
class RunRequest:
    workflow_id: str
    inputs: Mapping[str, Any]            # JSON inputs keyed by the contract's input names
    media: Mapping[str, MediaInput]      # input name -> an uploaded part or a library asset reference
    binding_key: str | None = None       # None: the entry's first binding the caller may use
    idempotency_key: str | None = None
    wait_s: float = 0.0                  # bounded by registry/defaults (workflow_execution.max_wait_s, 60 s)
    frontend_id: str = "workflows"       # who called; the receipt records it

@dataclass(frozen=True)
class RunAccepted:
    job_id: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    result: ResultBundle | None          # set when the job finished within wait_s

async def submit_run(req: RunRequest, ctx: AuthContext, *, snapshot: RegistrySnapshot) -> RunAccepted | Response: ...
async def execute_run(job_id: str) -> None: ...                  # the worker; also the restart resume path
def recover_runs(now_s: int) -> RecoveryReport: ...             # startup; uses classify_recovery (S2 D3)
```

Rules:
- `submit_run` does, in this order: resolve the entry in the given snapshot; the workflow target policy (exposure,
  `published`, plan); input validation (`validate_inputs` on the contract, media checks from `ExecutionProfile.accepts`);
  admission (`_media_job_admission_response` with `resource_class`); generated values once (S2); the envelope with
  `source_revision = entry.record_revision`; `media_jobs.create(..., envelope=..., require_envelope=True)`; schedule.
  The caller has already passed authentication and the runtime route policy; the frontend does those (I5).
- `execute_run` does, in this order, before any effect: rebuild the caller (`_auth_context_from_image_job_payload`; a
  revoked App, person, key or connection fails the job); re-admission (no queue count); the entry state check (section
  5.2); load the pin (`DefinitionUnavailable` fails the job, never falls back to the live file); compile; run the
  adapter; publish the result bundle; finish the receipt.
- Every workflow job compiles from its pin. The `compile_from_pin` setting is removed (the product is unreleased; no
  compatibility key).
- The core names no intent, capability word, adapter, engine, model or media type. A test reads the module and fails on
  any such literal (the ratchet has a zero-literal module list; this module joins it).

### I5. `FrontendRow`: a route is a registry row over a reviewed wire shape (S7)

```python
@dataclass(frozen=True)
class FrontendRow:
    id: str
    shape: str                       # a reviewed wire-shape id, registered in code (FRONTEND_SHAPES)
    routes: tuple[RouteSpec, ...]    # method, canonical path, aliases, runtime_policy
    selects: WorkflowSelector        # which workflows it serves: explicit ids, or a declared contract match
    mode: Literal["sync", "job"]     # sync waits on the job up to the shape's limit; job returns the job object
    receipt_protocol: str            # the receipt vocabulary word (openai.images, azizam.workflows, ...)
    strictness: Literal["exact", "lenient"]

class FrontendShape(Protocol):
    shape_id: str
    def profile(self, contract: Mapping[str, Any]) -> list[str]: ...          # reasons it cannot serve the contract
    async def to_run_request(self, request: Request, row: FrontendRow, snapshot: RegistrySnapshot) -> RunRequest | Response: ...
    def to_response(self, accepted: RunAccepted, row: FrontendRow) -> Response: ...
```

Rules:
- A new route on an existing shape is data (a row). A new shape is code (a reviewed `FrontendShape`).
- `selects` is either `{ids: [...]}` or `{match: {...}}`, where a match is a validated predicate over the contract
  (inputs present and typed, output kinds). Compatibility-API membership (for example the OpenAI images list) is a
  match, never inferred from the intent word.
- `profile` replaces `frontend_profiles.register(intent, ...)`. At save, the store runs `profile` for every row whose
  `selects` would include the workflow, and refuses the save with the row id when one refuses.
- The rows live in `registry/protocol_frontends.yml` (the existing file): its `registered_routes` grow a `shape`,
  `selects` and `mode`. The generic run route is the row `workflows` with shape `azizam.workflow-run`.

### I6. Owner API (S4, with the S8 form over it)

| Method and path | Body | Answer |
|---|---|---|
| `GET /admin/api/workflows` | | every entry: id, state, published, record_revision, contract_digest, provenance, needs_configuration; `ETag: "os-N"` |
| `GET /admin/api/workflows/{id}` | | the entry, its contract, facts and the last 20 history entries; `ETag` |
| `POST /admin/api/workflows/check` | `{contract, facts?}` | the `CheckReport` (every problem with its path); writes nothing |
| `POST /admin/api/workflows` | `{contract, facts?}` | 201 and the `Commit`; 409 `workflow_exists`; 422 with problems |
| `PUT /admin/api/workflows/{id}` | `{contract, facts?}`, `If-Match` required | the `Commit`; 412 `generation_mismatch`; 422 |
| `POST /admin/api/workflows/{id}/state` | `{state}`, `If-Match` | the `Commit` |
| `POST /admin/api/workflows/{id}/publish` | `{published}`, `If-Match` | the `Commit` |
| `POST /admin/api/workflows/{id}/restore` | `{contract_digest}`, `If-Match` | the `Commit` |
| `DELETE /admin/api/workflows/{id}` | `If-Match` | the `Commit`; 409 `id_is_bundled` for a bundled id |

The MCP server gets owner tools with the same names and bodies (`workflow_check`, `workflow_put`, `workflow_state`,
`workflow_publish`, `workflow_restore`, `workflow_delete`), and `scripts/` gets one CLI over the same routes. All three
are thin over `RegistryStore`; none adds a rule.

### I7. Defaults (S8)

Every literal default that S4 to S7 still read moves to a `registry/defaults/<section>.yml` file with its schema, read
through the config-homes reader. New keys: `workflow_execution.max_wait_s` (60), `workflow_execution.history_depth`
(20), `workflow_bounds.{max_template_bytes, max_nodes, max_depth}` (section 5.7). `workflow_execution.compile_from_pin`
is deleted.

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

A job pins content, not a revision. The envelope's definitions are content digests in `definition_store`; the job holds
them with a `job:<id>` reference until it finishes. `source_revision` records `entry.record_revision` for audit and for
the receipt, but execution never reads the registry at that revision. Why: a pin by content survives registry rollback,
restore and restart with no history lookup, and the blob store already exists (S2). Precedent: a Replicate prediction names a version id; a
GitHub Actions run reads the workflow at its triggering SHA; a Lambda durable execution stays on its version when the
alias moves (section 4).

### 5.2 Update, disable and delete while queued or running

| Owner action | Queued job | Running job |
|---|---|---|
| Update (new contract or binding) | Runs its pinned definitions. | Finishes on its pinned definitions. |
| Disable | Fails before any effect with `workflow_disabled`. | Finishes. The owner can cancel it (`cancel` route). |
| Delete | Fails before any effect with `workflow_deleted`. | Finishes. Its blobs stay until the job's reference is released. |
| Unpublish | Runs (it was admitted while published; its caller passes the re-check only if the plan still allows the workflow). | Finishes. |
| Policy fact change (exposure, plan) | Re-checked at run with the target policy, like revocation. | Finishes. |

Why disable and delete stop a queued job: they are the owner's stop, the same kind of decision as a revoked key, and S2
already checks revocation at run before effects. Why a running job finishes: there is no safe preemption point in a
graph engine run; cancel is the explicit tool. This is the GitHub rule (disable leaves existing runs), applied at the
queue boundary: a job that has not started any effect is not yet a run. Replicate's cancel-on-disable is the rejected
alternative, because an owner who wants that has the cancel route. The state check reads the current snapshot (`snapshot().get(id)`), the
only live registry read at run time.

### 5.3 Compare-and-swap

Every write carries `If-Match: "os-N"` (create carries none). The store compares it with the current generation inside
the write transaction (`write_service_registry_with_audit`, which already does this) and refuses with 412
`generation_mismatch` when they differ. The answer body names the current revision so the editor can re-read. This is
the existing mechanism, now per workflow route. It is the Kubernetes `resourceVersion` rule.

### 5.4 Restart and old envelopes

At startup, `recover_runs` calls `classify_recovery` (S2 D3) for every unfinished workflow job:
- queued with a recorded envelope: resume from the pin (all capabilities, not only image, audio, video and make);
- queued without an envelope (written before this train): fail with `restart_no_pin`;
- running with an engine `submission_unknown` marker: the S2 D3 row (poll the engine, else fail);
- an envelope whose `envelope_version` the build does not know: fail with `envelope_version_unsupported`.

Resume runs `execute_run`, so the disable, delete and revocation checks of 5.2 apply after a restart too.

Restart after a crash uses the pin. "Run again" (an owner or App asking for a finished or failed job to be repeated) is a
new `submit_run` against the current definition; it is a new job with a new pin and says so in its receipt. There is no
"rerun on the old definition" in this train: an owner who wants that restores the old version first (5.5). This is the
n8n choice reduced to one default; the other side is listed in section 12.

### 5.5 Rollback

- **Owner rollback of one workflow:** `restore(id, contract_digest)` puts an old version back as a new revision.
- **Gateway code rollback (a train rollback):** stored workflow records stay schema v2; the previous build reads them.
  Jobs pinned by the newer build carry `envelope_version` 2 (section 7.3); a previous build that sees one fails it with
  `envelope_version_unsupported`, never runs it differently. The train's rollback note says so.

### 5.6 Clash refusal at save

A save is refused (422, code `id_clash`, the clashing name in the body) when, after the canonical normalization
(`strip().lower()`), the workflow id equals any of: another workflow id; a model id or alias that `/v1/models` lists
(engine offerings and dynamic aliases); a frontend row id; an OpenAPI operation id that another workflow or route
already produces (`operation_id(entry)` is one function, used by the OpenAPI builder and by this check). A stored
workflow with a bundled id still needs `overrides: <bundled id>` (the existing rule).

### 5.7 Inline templates and structural bounds

A stored workflow may carry its template inline (`bindings.<key>.template: {inline: {...}}`). Bounds, checked at save
and again at pin: template bytes (canonical JSON) at most 512 KiB, nodes at most 2,000, nesting depth at most 32. The
limits are deployment data (`workflow_bounds`, I7). A bundled workflow keeps its template path; the loader reads it
into a blob at load, so the job pins the same digest either way.

### 5.8 Cache invalidation

Every derived view is keyed by `snapshot.digest`: the OpenAPI document (the `_OPENAPI_INPUTS` key gains it, which fixes
today's stale-spec defect), the catalog (built per request today; it becomes cached per digest), input forms and
frontend selections. A store listener (I2 `subscribe`) clears nothing: a new digest is a new key, and the old entries
age out of a bounded cache (16 entries).

### 5.9 Publish is a separate decision

`published` defaults to false for a stored workflow and comes from the release policy for a bundled one (all 38 are
published today). An unpublished workflow is visible to the owner (owner API, owner catalog view) and runs only for the
owner; it is absent from App catalogs, App OpenAPI and the MCP tools.

## 6. S5: one effective registry and its projections

### 6.1 One snapshot over workflows and engine offerings

`RegistrySnapshot` holds workflows and frontends. Engine offerings (models that engines serve) stay in the service
registry's `services` and are projected by the existing model catalog; S5 only makes the clash check (5.6) and the
catalog read them from the same revision. A full unification of offerings into the snapshot is not needed for the
oracle and is out of scope.

### 6.2 Every listing reads one revision

Catalog (`/me/api/workflows`, `/gateway/v1/workflows`), OpenAPI, forms, policy decisions and receipts read the same
`RegistrySnapshot` reference for the life of a request. The receipt records `ReceiptIdentity` (I3), so a receipt says
which revision ran.

### 6.3 The intent tables and where their data goes

| Table today | Replacement |
|---|---|
| `_MEDIA_JOB_RESOURCE_CLASS_BY_INTENT` | `PolicyFacts.resource_class` (record data; migration writes today's values) |
| `_AUDIO_MEDIA_JOB_INTENTS`, `audio_media_jobs._AUDIO_INTENTS`, `_VIDEO_MEDIA_JOB_INTENTS`, `_IDENTITY_JOB_INTENT` | the frontend row's `selects` (I5) |
| `_IMAGE_ROUTE_INTENTS`, `_DISCOVERY_OPERATION_INTENTS`, `workflow_openapi._ALIAS_OPERATION_INTENTS`, the `video.edit` OpenAPI filter | frontend rows and `operation_id(entry)` |
| `_MODEL_LISTED_MEDIA_INTENTS` | a frontend row flag `lists_as_model: true` |
| `model_metadata._INTENTS`, `_INTENT_ASSET_MODALITY`, `_CAPABILITY_INTENTS`, `_CAPABILITY_INTENT_FAMILIES` | computed from the contract's inputs and outputs (`ExecutionProfile.accepts`/`produces`); OpenAI-style metadata is a lossy edge projection and never gates execution |
| `local_capabilities._ALIAS_INTENTS` | frontend rows (the alias is a route alias) |
| `frontend_profiles` (keyed by intent) | `FrontendShape.profile` (I5) |

A builder that finds another table keyed by an intent, capability word, model or engine adds it here in the same
commit. The final review greps for dict literals keyed by intent strings.

## 7. S6: the generic run route

### 7.1 Request

`POST /gateway/v1/workflows/{id}/run`, JSON body `{"inputs": {...}, "binding": "<key>"?}`, or multipart with a JSON
part `inputs` and one file part per media input, named by the input. A media input may instead be a library asset
reference `{"asset_id": "..."}` in `inputs` (the shared library input, as the Library pick rule requires). Inputs are
validated by `validate_inputs(contract, inputs)`; a miss answers 422 with every problem and its input path.

### 7.2 Always a job, with a bounded wait

Every run is a job. The response is the job object `{id, status, workflow_id, record_revision, created_at, result?}`.
The header `Prefer: wait=N` (N at most `max_wait_s`, 60) holds the answer until the job finishes or N passes; the
answer then carries the result bundle, or the job object with status `queued` or `running` and `Location` of the job.
This is the Replicate `Prefer: wait` pattern; fal splits sync and queue into two endpoints (to be confirmed by the
research entry). One route keeps one admission path and one receipt path. A synchronous frontend (I5 `mode: sync`) is
the same submit plus a wait in the shape.

### 7.3 Envelope version 2

The envelope gains nothing structural: `source_revision` becomes `entry.record_revision` (an int), and `binding_key` is
added. Because the field's meaning changes, `ENVELOPE_VERSION` becomes 2 and the parser accepts exactly 2 (5.4, 5.5).

### 7.4 Results, events, cancel

The existing media job routes serve every workflow job: `GET /gateway/v1/media/jobs/{id}`, `.../results`,
`.../events`, `POST .../cancel`. The job's `operation` is `workflow.run`; its `resource_class` comes from the profile.

### 7.5 The old video.edit run route

It is replaced by the generic route on the same path, with no compatibility branch (the product is unreleased). The
video-edit multipart client shape becomes the row `video-edit` with a multipart shape over `submit_run`, if a named
client needs it; the goldens decide (S7).

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
- The MCP owner tools and the CLI (I6) are the agent interfaces; the web UI is one client of the same routes.

## 10. Build plan

### 10.1 Children and file ownership

| Child | Slice | Owns (writes) | Reads | Route |
|---|---|---|---|---|
| A | S4 store | `gateway/workflow_registry_store.py` (new), `gateway/service_registry.py` workflow section, `gateway/admin_store.py` holders, `gateway/definition_store.py` holders, owner routes in `gateway/admin_api.py` | I1, I2, I6 | Sonnet child |
| B | S5 projections | `gateway/registry_snapshot.py` (new; builds I1), `gateway/workflow_projections.py` (new), `gateway/workflow_openapi.py`, `gateway/workflows_catalog.py`, `gateway/model_metadata.py`, `gateway/local_capabilities.py`, `registry/workflow_policy.yml` migration | I1, I3 | Sonnet child |
| C | S6 run core | `gateway/workflow_run.py` (new; I4), the generic route row, `gateway/workflow_envelope.py` v2, `gateway/job_envelope.py`, recovery wiring | I1, I3, I4 | Sonnet child |
| D | S7 frontends | `gateway/frontend_shapes/` (new), `registry/protocol_frontends.yml`, the modality route handlers in the main module, templates for chain, subject-select and identity | I4, I5 | Sonnet child; goldens on the dot |
| E | S8 | `registry/defaults/*`, the web Workflows owner view, MCP owner tools, CLI | I6, I7 | the dot (web, defaults); MCP and CLI with A |

The main module is edited by A (owner route registration only), C (route row wiring, recovery call) and D (handler
bodies). Each child keeps its main-module edits to the named functions and lists them in its report.

### 10.2 What forces an order

1. **The interfaces, first.** `gateway/workflow_runtime_types.py` and this document are frozen before any child starts.
2. **Fakes, not waits.** Each child builds against an in-memory fake of the interfaces it reads (a `FakeStore` that
   returns a fixed `RegistrySnapshot`, a fake `submit_run`), shipped in `tests/workflow_runtime_fakes.py` with the
   interface file.
3. **End-to-end proof waits on integration.** D's goldens pass end to end only with C's core; the acceptance matrix
   runs only on the integration branch.
4. **Merge order on `release/c2d-int`:** A, then B, then C, then D, then E. A later child rebases on the branch, not on
   main. This is a merge order, not a design order.

### 10.3 Landing

One train: the integration branch behind the full suite (hermetic pytest, vitest, Playwright, the gate), the
acceptance matrix, a dot review of the whole diff, and the train rule (one-command rollback, post-deploy checks).
Rollback note: queued jobs pinned with envelope v2 fail on the previous build (5.5).

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
| 8 | Save with an id equal to a listed model alias, a frontend id, another workflow's OpenAPI operation id: each 422 `id_clash`. | clash refusal |
| 9 | Save an inline template over each bound (bytes, nodes, depth): 422 with the bound. | bounds |
| 10 | Restart with a queued job of each capability: each resumes from its pin; a job without an envelope fails `restart_no_pin`. | restart |
| 11 | Restore an old version: a new revision, the old digest. | rollback |
| 12 | OpenAPI before and after a workflow edit differ in that workflow's schema. | cache key |
| 13 | Every modality golden is byte-identical (or structure-identical where it holds ids and times). | frontends preserve behaviour |
| 14 | The core module has zero domain literals; the ratchet is under the target with every remaining site listed. | the rule |
| 15 | A schema miss, a policy denial and an admission refusal through the generic route each give their code. | errors |

## 12. Open questions

1. Unpublish while queued (5.2): run or stop? The draft runs it, because the caller was admitted. The research entry
   found no precedent for unpublish at the queue boundary (n8n unpublish stops triggers; its effect on queued
   executions is unverified), so this stays a judgement call.
5. "Run again on the original definition" (5.4) is not offered. n8n offers both; add it later only if owners ask.
2. The history depth (20) and the bounded cache size (16) are first guesses, set as data.
3. Whether a synchronous OpenAI images call through a job adds latency the goldens notice; measured on the integration
   branch before the train.
4. The S6 dot draft (in flight) may propose a different request shape; it is reconciled into section 7 before freeze.

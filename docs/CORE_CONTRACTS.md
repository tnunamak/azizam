# Core Contracts

Date: 2026-05-15
Status: live gateway integration in progress
Product: **Azizam** (project directory still named
`azizam`; public examples use `https://ai.example.com`).

This document defines the smallest set of stable contracts the gateway needs
before it grows more endpoints, adapters, or scheduler behavior.

The goal is not to freeze implementation details. The goal is to make every
important boundary explicit enough that compatibility, routing, native power
use, and diagnostics can evolve without turning into one large special case.

## Product Model And Internal Facts

The operator-facing model should stay small. The dashboard and docs should
mostly speak this language:

```text
User -> owns App -> has Profile -> has Keys -> produces Activity
```

| Product noun | Meaning |
|--------------|---------|
| `User`       | A human principal, typically authenticated via OIDC. |
| `App`        | An access boundary for a tool, device, agent, or family member. |
| `Profile`    | A reusable permission bundle attached to an App. |
| `Key`        | A rotatable bearer secret for an App or admin automation. |
| `Activity`   | Usage, audit events, receipts, jobs, and errors rendered as facts. |

The code can and should store more precise facts. They are stable contracts,
but they do not all need to be first-class dashboard nouns.

### Admin Plane

| Noun           | Meaning                                                                 |
|----------------|-------------------------------------------------------------------------|
| `User`         | A human principal, typically authenticated via OIDC.                    |
| `ServiceAccount` | A non-human principal that owns Apps and Credentials.                 |
| `Credential`   | A hashed bearer token (App API key, admin key, OIDC session, downstream provider). |
| `AdminRole`    | A named bundle of admin-plane permissions.                              |
| `AuditEvent`   | An immutable record of an admin-plane action.                           |

### Runtime Plane

| Noun           | Meaning                                                                 |
|----------------|-------------------------------------------------------------------------|
| `App`          | A registered consumer of the gateway (e.g. LibreChat, Continue, an agent). |
| `Profile`      | The policy bundle attached to an App.                                   |
| `Credential`   | The App API key (and any per-key downstream-credential overrides).      |
| `Capability`   | A runtime fact about what a Provider/model can do right now.            |
| `Provider`     | A configured backend instance (TabbyAPI, llama.cpp, ComfyUI, TTS, ...). |
| `Workflow`     | A typed manifest mapping portable inputs to a backend binding.          |
| `Job`          | A durable, asynchronous execution record for media work.                |
| `Receipt`      | A body-free record of one request: identity, decision, accepted/rejected/ignored inputs. |
| `UsageRecord`  | Per-App tokens/seconds/bytes/GPU-time aggregations.                     |

Rule for adding a new product-facing noun: it must have an independent
lifecycle and the target user must deliberately create, grant, rotate, inspect,
or delete it. Otherwise keep it as fields or events under App/Profile/Activity.
This keeps the system Rich-Hickey-simple: separate facts that change
independently, without inventing abstractions that merely rename code paths.

The product invariant — load-bearing across every contract below:

> A request without a resolved **App** + **Profile** + policy decision is
> never executed. Every executed request produces exactly one **Receipt**.

The word "client" is preserved only inside compatibility contexts: SDK names
("OpenAI client"), audience tiers ("client receipts"), and protocol-level
client/server wording. In product docs the consumer entity is an **App**.

## Implementation Status

The contracts below are now partially in the live request path. The core pure
Python building blocks currently exist on `main`, and `proxy.py` wires the
auth/profile bridge, gateway discovery router, image manifest extensions,
durable receipts, media-job store initialization, and non-streaming
Anthropic/Ollama protocol frontends:

- `gateway/policy.py` — `AppProfile`, `PolicyRequest`, `PolicyDecision`,
  `evaluate(...)`. Consulted by the live gateway/image/protocol handlers
  through the runtime-auth bridge; pure module remains independently testable.
- `gateway/profiles.py` — pure loader from profile dicts into `AppProfile`,
  without reading files or resolving secrets. It remains useful for imports,
  tests, and DB-stored Profile validation.
- `gateway/runtime_auth.py` — pure bridge from stored App credentials to
  `RuntimeIdentity` (name, key hash, downstream config, loaded `AppProfile`).
  Supplies `evaluate_and_apply` and a
  `discovery_audience_for_profile` helper that maps
  `status_visibility.level` → audience tier.
- `gateway/manifests.py` — workflow manifest load/validate plus
  `apply_backend_binding` and receipt-ready `bind_backend_request`. Image
  generation/edit handlers use this binding layer for supported workflow
  extension inputs.
- `gateway/extensions.py` — typed namespaced request-extension validation with
  accepted/rejected/ignored outcomes. The canonical namespace for
  gateway-owned controls is `com.azizam.workflow`.
- `gateway/image_extensions.py` — image-route extension/manifest binding layer
  consumed by the image handlers when mounted.
- `gateway/capabilities.py` — pure capability and resource-estimate records
  with audience-aware summaries.
- `gateway/discovery.py` — workflow registry with audience-aware summaries
  (`internal`, `admin`, `client`).
- `gateway/api.py` — `/gateway/v1/workflows`, `/gateway/v1/capabilities`,
  `/gateway/v1/receipts`, and `/gateway/v1/media/jobs` router; mounted into
  the live proxy behind proxy-key auth.
- `gateway/receipts.py` — `RouteReceipt` data model with policy-decision and
  validation hooks. Image routes persist durable receipts and emit body-free
  firehose events with safe field-name outcomes only.
- `gateway/receipt_store.py` — durable SQLite receipt store with audience
  filtering and payload/secret shape guards.
- `gateway/media_jobs.py` — gateway media-job model and durable store backing
  the `/gateway/v1/media/jobs` surface. The live proxy now executes
  `image.generate`, `audio.generate`, `music.generate`, `sfx.generate`, and
  `video.generate` jobs from this surface and returns completed image/audio/video
  asset payloads at `/gateway/v1/media/jobs/{job_id}/content`.
- `gateway/admin_store.py` — durable SQLite control-plane store for Users,
  ServiceAccounts, Apps, Profiles, Credentials, AdminRoles, and AuditEvents.
- `gateway/admin_api.py` / `gateway/admin_runtime.py` — `/admin/api/*`
  router and runtime adapter for creating Apps, minting one-time App keys,
  revoking Credentials, listing audit events, and simulating profile policy.
- `gateway/admin_oidc.py` — pure OIDC/session helpers for the browser admin
  login path; `proxy.py` provides the network adapter and signed-cookie wiring.
- `gateway/admin_render.py` — compact server-rendered admin dashboard at
  `/admin`, intentionally backed by the same admin API/store nouns.

Backend adapter extraction, a richer live capability inventory, resource
claims, generic gateway assets, and broader media-job adapters described below
are still in progress.

## Contract Rule

Every request should cross these boundaries:

```text
protocol request
  -> canonical intent
  -> app/profile policy
  -> capability match
  -> resource claim
  -> backend binding
  -> route receipt and events
```

Compatibility endpoints can hide this from clients. Internally, the gateway
should not skip these decisions once a request affects routing, native controls,
credentials, resource use, or durable assets. This is a processing pipeline,
not a mandate to expose every step as a product object.

## Authority Boundary

The most important gateway decision is not "which backend can do this?" It is:

```text
who is allowed to cause or observe which action?
```

Every privileged request should resolve:

- caller identity
- app profile
- requested protocol and intent
- requested model, workflow, backend, or native operation
- allowed credentials
- allowed fallback policy
- allowed status/receipt visibility
- allowed resource/lifecycle actions

Keep the authority graph minimal until behavior forces a new fact. While only
global owner/admin users attach Profiles, there is no separate grant object in
the core model. If non-admin App owners are allowed to choose or switch
Profiles, then the missing fact becomes concrete:

```text
User may attach this Profile to this App
```

Until then, "who may attach a Profile?" is just an admin permission check, not
a new product noun.

The output is a policy decision:

```yaml
decision: allowed
sources:
  - profile:image-agent
  - policy:local-only
reasons:
  - profile permits image edit workflows
```

This decision belongs in the route receipt. Without it, receipts become logs
instead of an auditable authority boundary.

## Versioning

Contracts are versioned independently from implementation modules.

Minimum fields:

```yaml
schema_version: 1
min_gateway_version: "0.1"
```

Rules:

- Unknown top-level manifest fields are tolerated for forward compatibility.
- Unknown request inputs are never silently applied.
- Unsupported known inputs are either rejected or recorded as ignored with a
  reason.
- Backend-specific bindings can change without changing the portable manifest
  contract when the portable inputs and outputs stay the same.

## Canonical Intent

Canonical intent is the internal request shape after protocol parsing.

Example:

```yaml
schema_version: 1
intent: image.edit
source_protocol: openai.images
requirements:
  input_modalities: ["text", "image"]
  output_modalities: ["image"]
  execution: async
  stream: false
  output_contract:
    media_type: image/png
    count: 1
  constraints:
    max_width: 1024
    max_height: 1024
    max_latency_s: 120
  policy:
    local_only: true
    allow_fallback: false
```

The intent name is not enough. Routing needs structured requirements:

- modalities
- execution mode
- stream or non-stream behavior
- output contract
- context or size constraints
- policy constraints
- optional native extension requirements

This is how the gateway avoids pretending a chat model, ComfyUI workflow, TTS
server, and video queue all expose the same kind of capability.

## Workflow Manifest

A workflow manifest has two layers:

1. A portable contract that clients and profiles can reason about.
2. Backend-specific bindings that turn validated inputs into engine requests.

Example:

```yaml
schema_version: 1
version: "0.1.0"
id: image.edit.controlnet.example
intent: image.edit
source: local
trusted: true
inputs:
  prompt:
    type: string
    required: true
  denoise:
    type: number
    minimum: 0
    maximum: 1
  controlnet_strength:
    type: number
    minimum: 0
    maximum: 2
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: workflows/images/example-controlnet.json
    map:
      denoise:
        - node: "31"
          input: denoise
      controlnet_strength:
        - node: "44"
          input: strength
```

The portable contract does not mention ComfyUI nodes. The binding does.
Committed ComfyUI audio and video workflows are expected to have matching
manifests under `manifests/workflows/` so `/gateway/v1/workflows` and
`/gateway/v1/capabilities` expose ACE-Step, TangoFlux, LTX, and WAN capability
truth consistently with `proxy_config.yml`.

If a future backend works nothing like ComfyUI, it gets a different binding
under the same portable inputs when the semantics match. If the semantics do not
match, it needs a different workflow manifest rather than a strained adapter.

Manifest identity must include enough provenance for receipts and debugging:

- manifest ID
- manifest version
- content hash
- source
- trusted or untrusted state
- backend workflow path or package reference
- drift status when bound to a mutable workflow file

## Adapter Manifest

An adapter manifest describes one backend family or service instance.

Minimum concepts:

```yaml
schema_version: 1
id: comfyui.local
backend_type: comfyui
base_url: http://127.0.0.1:7890
protocols: ["comfyui.http", "comfyui.websocket"]
capabilities:
  - intent: image.generate
  - intent: image.edit
lifecycle:
  managed_by: external
  expected_down: false
resources:
  gpu:
    devices: [0]
    vram_gb_estimate: 18
health:
  kind: http
  path: /
```

Adapters should expose:

- capabilities
- health
- runtime config
- lifecycle state
- resource claims
- native passthrough permissions

They should not expose raw engine behavior directly to ordinary compatibility
clients.

## Capability

Capabilities are runtime facts merged from manifest configuration, backend
probing, policy, and resource state.

Example:

```yaml
intent: chat.completions
model: qwen3-coder
backend: llama.cpp.gpu1
input_modalities: ["text"]
output_modalities: ["text"]
context_window: 65536
streaming: true
tools: partial
state: warm
available: true
exposure: default
trust_tier: stable
resource_tier: interactive
tags: ["coder"]
resources:
  max_parallel: 1
policy:
  local_only: true
```

The same model name can map to different capabilities under different app
profiles. Capability discovery must be policy-aware.

Minimum eligibility fields for routing:

- intent
- input and output modalities
- execution mode
- exposure class (`default`, `opt_in`, `admin`, `disabled`)
- trust tier (`stable`, `experimental`, `private`, `admin`)
- resource tier (`interactive`, `batch`, `expensive`)
- tags for Profile/template selection
- streaming support
- lifecycle state
- health availability
- policy availability
- resource estimate
- caveats that affect correctness

These fields keep profile management from becoming a per-model chore. The
default rule is:

```text
new capability metadata + existing Profile selectors -> availability decision
```

Operators should edit every App/Profile only when the new capability introduces
a new category of risk. Otherwise the capability's own metadata controls
whether matching Profiles can see it.

## Policy Profile

A Profile is a named policy bundle tied to an App. In the durable admin store,
Apps point at stored Profiles and App keys inherit that Profile at runtime.
Missing Profile bindings fall back to `DEFAULT_PROFILE` (local-only, basic
visibility) so simple Apps keep working unchanged.

Example:

```yaml
id: continue-local
protocols: ["openai.chat", "openai.responses"]
fallback:
  cloud_allowed: false
native_passthrough:
  enabled: false
status_visibility:
  level: basic
```

The mature Profile shape should prefer selectors over giant allow-lists:

```yaml
id: family-chat
allow:
  intents: ["chat.completions", "speech.synthesis"]
  trust_tiers: ["stable"]
  resource_tiers: ["interactive"]
  exposure: ["default"]
fallback:
  cloud_allowed: false
native_passthrough:
  enabled: false
```

Current implementations may still support direct `protocols`, `workflows`,
`models`, and `backends` allow-lists. Those remain useful for narrow Apps and
break-glass overrides, but they should not be the normal way to keep a family
member, agent, or UI app updated as new models and workflows are installed.
Operational limits such as `max_parallel` belong to the admission/controller
contract, not the identity/Profile selector itself.

Protocol strings are concrete (e.g. `openai.chat`, `openai.images`,
`openai.audio`, `openai.responses`, `anthropic.messages`, `ollama.chat`,
`ollama.generate`). Allow-list semantics are conservative: an omitted list
means "no opinion" for that dimension; an empty list means "deny all".

Profiles are where the gateway maps:

- which protocols an App can use
- which workflows, models, and Providers it may target
- whether cloud fallback is allowed
- which Credentials may be used downstream
- how much status, discovery, and Receipt detail it may see
  (`status_visibility.level`: `none` / `basic` / `standard` / `full` →
  client / client / admin / internal audience tiers; "client" here is the
  audience-tier name, not a product noun)
- which native passthrough lanes are allowed

Backend-native power knobs (denoise, controlnet strength, model-specific
controls) belong under namespaced request extensions
(`extensions.com.azizam.workflow.*`), not as global OpenAI fields. Native
passthrough remains opt-in, scoped per profile, and is not a blanket ComfyUI
DSL.

OpenAI-native media fields remain top-level even when the local backend needs
adapter work. For example, `/v1/images/edits` receives `image` and `mask` as
standard multipart fields. The route may normalize and bind those assets to a
workflow or apply a documented preservation fallback, but `mask` is not a
workflow extension.

Non-standard conditioning assets remain workflow extensions. For multipart
image edits, an extension value such as
`extensions.com.azizam.workflow.control_image={"multipart_field":"control_image"}`
may point at an extra uploaded form part. That form part is transport only; the
selected manifest must still declare and bind `control_image` before the route
will apply it.

Generated media may also surface a gateway asset handle. For image outputs,
`data[].asset_id` is a content-addressed `img_<sha256>` handle and
`data[].asset_url` is a signed bearerless content URL under
`/v1/assets/{asset_id}/content`. These handles are gateway-native metadata:
OpenAI-compatible fields such as `b64_json` and `url` remain intact, while
agents can reuse `asset_id` where a workflow manifest declares an asset input
or where a route explicitly accepts an input reference, such as video
`input_reference`. Asset IDs are scoped to the authenticated API key that
minted them; signed content URLs require a valid HMAC signature and expiration,
but not the original bearer token.

This is the bridge between authentication, routing, credential provisioning,
and user-facing compatibility.

## Resource Claim

Resource claims make scheduling explicit before execution.

Example:

```yaml
request_id: req_123
backend: comfyui.local
kind: gpu
devices: [0]
vram_gb_estimate: 12
duration_class: medium
exclusive: false
```

Early versions can be estimates. The important rule is that media and model
lifecycle requests should not be opaque to the gateway.

Resource policy is also where operator modes and maintenance states belong.
Examples: `llm-only` mode can reserve VRAM for a large reasoning model and
disable image/video queues; `maintenance` can drain a Provider before restart;
per-App/Profile budgets can cap queue wait, concurrency, GPU seconds, or
fallback use. These constraints must be visible to policy, receipts, and
capability discovery instead of being hidden in backend-specific scripts.

The admission boundary is:

```text
auth/quota -> capability health -> mode policy -> resource lease -> adapter
```

Standard outcomes should be boring and machine-readable: `202 queued`,
`429 rate_limited`, `503 unavailable_or_maintenance`, and `409 mode_conflict`.
Capability health uses `available`, `degraded`, `draining`, `maintenance`, and
`unavailable`; resource leases use `requested`, `granted`, `renewed`,
`released`, `denied`, and `expired`.

## Route Receipt

A route receipt records what the gateway accepted, rejected, routed, ignored,
and executed.

Minimum fields:

- request ID
- protocol and path
- canonical intent
- app/profile identity
- requested model or workflow
- resolved model or workflow
- backend
- policy decision, sources, and reasons
- manifest ID, version, and hash when a manifest participates
- job ID and parent request ID when durable work participates
- accepted inputs
- rejected inputs with reasons
- ignored inputs with reasons
- fallback reasons
- warnings
- status and latency

Receipts are not full request logs. They should avoid raw prompts, raw files,
secrets, downstream credentials, and large payloads by default.

Generated-media prompts have a separate store keyed by asset and maker person.
A request counts as a person only when it is authenticated as that person
(session/OIDC), or uses a personal App key created by that App's own owner.
A key minted by anyone on a service App, or by the owner on someone else's
App, has no person: prompts are not remembered and maker-only metadata is
unavailable. Generated items still belong to the App for Library visibility
and receipts. Library item visibility does not depend on the key's creator.

At read time, a pre-upgrade key with no recorded creator (`created_by IS NULL`)
on a personal App is treated as created by that App's owner. This preserves
existing keys and applies the owner's prompt-memory setting. Service App keys
remain no-person. New keys minted by non-human administrators record an empty
creator, which explicitly excludes them from the legacy fallback. Sharing your
own personal App key delegates your person identity, including access to your
saved prompts and full workflow downloads.

A retained prompt has an optional expiry. Receipts and media-job metadata
reject raw prompt fields. New cache writes strip embedded prompts and workflows.
Full metadata downloads require the authenticated maker and an unexpired saved
prompt; signed URLs default to `metadata=none`. Another key on the same App
can read permitted clean media, but cannot read another person's prompt or full
workflow. The owner role grants no prompt access through these APIs. An owner
can read server databases or issue an account reset link and sign in as a
member; this system does not protect content against that server authority.

`extensions.com.azizam.prompts.mode="forget"`, a member's off setting,
Responses `store:false`, and the household kill switch prevent prompt storage.
Member preferences are saved at rollout and user creation. The household
default affects new members only. The kill switch overrides every preference.
Responses conversation rows are scoped by person and immutable App ID, or by
credential hash when there is no App. Display names grant no access. Older
rows without trustworthy scope are hidden. No-person requests default to
`store:false`; explicit `store:true` works only with a stable authenticated App
or credential scope. Keyless LAN requests have no private conversation scope.

Normal asynchronous jobs retain restart durability. For service or keyless
requests with no person, the prompt is required to resume inference, so the
execution spool keeps it until completion, failure or cancellation, with an
orphan pruner as a fallback. This temporary operational copy is not personal
prompt memory. Redacting it would make the job unable to resume. Explicit
forget, member-off and the kill switch use a memory-only execution spool.
Those jobs can finish in the current process but cannot resume after restart.
Delete-all and the kill switch move active spools to memory and remove their
durable copies. The submit-time forget decision stays in effect during retries.
Normal seeded no-person jobs also retain their existing prompt-derived body
HMAC for automatic deduplication after the execution spool is deleted. Explicit
forget, member-off and kill-switch jobs use random request hashes instead;
caller-supplied Idempotency-Key semantics remain unchanged.

Deleting media, evicting the cache, deleting a video, failing a video, and
removing a member remove associated prompt rows. Cancellation removes the
execution spool and current placeholder, but does not abort an in-flight
backend generation; its output can still finish. Unfetched video rows expire
after 14 days. A separate maintenance task removes expired and orphaned rows
and retries pending erasure. Media asset prompt writes run off the event loop
and fail closed: if a write fails, the finished generation still succeeds with
clean cache bytes and no saved prompt.

Upgrade runs a throttled, resumable background migration that strips legacy
cache metadata in place. Asset IDs remain opaque handles after this rewrite;
no download path rechecks them against a content hash. Delete-all and the kill
switch strip affected legacy files before returning success. A partial purge
returns HTTP 202 with pending counts and schedules retries; it does not claim
completed erasure. Unsupported or corrupt files remain reported as pending.
SQLite secure deletion reduces recoverable database content, but snapshots,
backups, backend histories and previous downloads are outside this contract.
See `docs/design/PROMPT_MEMORY.md`.

Deletion records a per-person erasure cutoff before cleanup. Final prompt and
Responses writes reject requests started at or before that cutoff, including
async image retries. Whole-second timestamps can also suppress storage for a
new request in the same second. Media generation still completes. A deletion
whose intent cannot be saved returns 503 with retry_required; it does not claim
that a background retry was scheduled. Persisted off settings and retention
limits are reconciled on startup and every minute.

Receipts have audiences:

- internal receipts can include backend, policy source, manifest hash, and
  diagnostic metadata
- admin receipts can include most routing facts but should omit arbitrary
  metadata by default
- client receipts should include request/job correlation, accepted/rejected
  inputs, warnings, status, and latency, but not backend internals or policy
  source details

## Job And Event

Durable media and long-running work should use jobs.

Minimum job states:

```text
queued -> running -> succeeded
queued -> running -> failed
queued -> canceled
```

Events are append-only facts that jobs, receipts, metrics, status pages, and
progress streams can consume.

Early versions can keep events simple, but they must not be confused with
metrics samples. Metrics are aggregations. Events are facts.

## Native Passthrough Permission

Native passthrough is necessary for sophisticated self-hosters, but it is a
privileged lane.

Minimum policy:

```yaml
native_passthrough:
  enabled: true
  allowed_backends: ["comfyui.local"]
  allowed_operations: ["queue_prompt", "history"]
  audit: true
```

Rules:

- disabled by default
- scoped by app profile
- visible in route receipts
- never used as a substitute for tested compatibility behavior

## First Implementation Target

The first implementation target is deliberately narrow:

```text
Validate typed workflow inputs, apply a backend binding to a fake ComfyUI-style
request, and record accepted/rejected inputs in a route receipt.
```

That proves the core semantic move without changing live proxy behavior.

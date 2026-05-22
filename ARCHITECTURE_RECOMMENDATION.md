# Vivid Fish AI Gateway — Architecture Recommendation

Date: 2026-05-15
Status: recommendation plus live vertical-slice checkpoint
Product: **Vivid Fish AI Gateway** (the "AI Gateway"). Canonical host
`https://ai.vivid.fish`. The historical `https://openai.vivid.fish` host is a
preserved compatibility alias and resolves to the same routes.

This document is the product and architecture recommendation for evolving
the codebase still known internally as `openai-proxy` into the local-first
**Vivid Fish AI Gateway** — a generalized control plane and operator
experience for self-hosted AI.

Detailed inventories live in:

- `docs/API_TARGETS.md`
- `docs/CORE_CONTRACTS.md`
- `docs/SDK_COMPATIBILITY_HARNESS.md`
- `docs/SELF_HOSTED_APP_QUIRKS_AUDIT.md`
- `docs/SLVP_SCOPE.md`

## Executive Decision

Build a local-first compatibility, routing, scheduling, and diagnostics layer
for people running real AI systems on their own hardware.

Do not position this as another generic AI gateway. LiteLLM, Bifrost, LocalAI,
Portkey, Helicone, Cloudflare, Kong, and Envoy already cover large parts of that
category. The gap is narrower and stronger:

```text
One reliable local endpoint for all your local AI backends, frontends, agents,
and media workflows.
```

The product should be broad in backend coverage but strict in compatibility
claims.

That means:

- first-class support for messy local machines
- high-fidelity public protocols where clients actually depend on compatibility
- broad backend adapters for LLM, image, video, STT, TTS, music, SFX, and hosted fallbacks
- explicit capability metadata instead of pretending every backend is OpenAI
- scheduler-visible resource use instead of backend-specific hidden behavior
- real SDK/app compatibility tests instead of curl confidence
- private/local operation by default

The current repo is not a throwaway prototype. It is a working vertical slice
with OpenAI-compatible chat, responses, images, audio, video, auth, status,
metrics, ComfyUI workflows, TTS/STT, and local GPU awareness. It should become
the extraction source for the architecture.

## SLVP Bar

For this project, SLVP means:

- **Small:** one or two excellent public compatibility surfaces first
- **Lovable:** solves the local operator's actual pain, not abstract gateway pain
- **Valuable:** immediately improves existing tools and backends
- **Provable:** compatibility and routing claims are tested against real SDKs/apps

The SLVP is not "support every vendor API."

The SLVP is:

```text
OpenAI + Anthropic + Ollama-compatible front doors,
backed by broad local backend adapters,
with route receipts, diagnostics, and resource-aware media jobs.
```

Scope discipline lives in `docs/SLVP_SCOPE.md`. The short rule: keep broad
local needs expressible through adapters, workflow manifests, and native lanes;
promote only repeated, testable concepts into core protocol semantics.

## Simplicity Revision

The gateway should not become a noun museum. The public/operator model stays
small:

```text
User -> owns App -> has Profile -> has Keys -> produces Activity
```

These are the concepts a self-hoster should operate in the dashboard:

- **Users**: people authenticated by OIDC.
- **Apps**: access boundaries for tools, devices, agents, or family members.
- **Profiles**: reusable permission bundles attached to Apps.
- **Keys**: rotatable bearer secrets for Apps or admin automation.
- **Activity**: usage, audit, jobs, receipts, and errors rendered as facts.

Everything else is internal data unless it earns its way into the UI:

- **Capability** is a fact about what can run now.
- **Workflow** is a typed binding for media/native power.
- **Provider** is an operator-facing backend only on status/config screens.
- **Job** is the durable shape for long work.
- **Receipt** is a body-free fact for debugging and audit.
- **ResourceClaim** is an admission decision input, not a product noun.
- **UsageRecord** is an accounting fact, not a thing users manage directly.

Introduce a new user-facing noun only when it has an independent lifecycle and
the target user must deliberately create, grant, rotate, inspect, or delete it.
Otherwise keep it as fields or events under App/Profile/Activity. This is the
Rich-Hickey-simple line for the project: separate facts that change
independently; avoid abstractions that only rename code paths.

## Product Thesis

Target the self-hoster who already has more than one engine:

- llama.cpp or KoboldCpp
- vLLM, TabbyAPI, Ollama, LM Studio, or LocalAI
- ComfyUI image/video/audio workflows
- local STT and TTS
- music/SFX generation
- Open WebUI, LibreChat, SillyTavern, Continue, Cline, OpenCode, Claude Code, n8n
- maybe cloud fallback, but only when policy allows it

These users do not need another chat UI. They need a compatibility layer that
lets existing apps and engines work together without each tool learning every
backend's quirks.

The user-facing pitch should be:

```text
Keep your favorite frontend. Keep your favorite backend. Put this in the middle
so they speak clearly.
```

## What Exists Today

`openai-proxy` already provides a serious local gateway:

- OpenAI-compatible `/v1` API on FastAPI
- chat/completions and embeddings passthrough to local LLM backends
- OpenAI Responses translation over chat completions with SQLite persistence
- OpenAI Audio speech/transcription over local TTS/STT services
- OpenAI Images generation/editing over ComfyUI workflows
- Sora-shaped video job API over ComfyUI
- gateway-specific audio generation for ACE-Step/TangoFlux-style workflows
- signed cached image URLs
- model aliasing and operation-specific model compatibility
- hashed App API keys in the admin store
- Profile-scoped runtime authorization
- per-App usage/status metrics
- ComfyUI websocket progress
- VRAM estimates and adaptive image batching
- workflow drift detection
- rich `/v1/status` operational surface

The current weakness is architectural shape, not lack of substance. The first
gateway slice now exists, but much of the behavior is still concentrated in
`proxy.py`, and the boundary between protocol, routing, backend execution,
resource policy, diagnostics, and passthrough is not yet stable enough for
external contributors or a second operator's machine.

## Implementation Status

This section anchors the rest of the document, which is largely aspirational.
The detailed slice-by-slice tracker lives in `docs/OWNER_EXECUTION_PLAN.md`.

Implemented (lives on `main`):

- `gateway/policy.py`: pure app-profile policy module with `AppProfile`,
  `PolicyRequest`, `PolicyDecision`, fallback/native-passthrough/visibility
  policies, and an `evaluate` entry point. It is used by the live gateway
  discovery view, image route receipts, and protocol frontends.
- `gateway/profiles.py`: pure loader from stored profile/config dicts into
  `AppProfile`, keeping credential lookup and file I/O outside the policy
  layer.
- `gateway/runtime_auth.py`: bridge between stored App credentials and
  profile-aware gateway identities. Runtime authorization is admin-db backed;
  imported legacy hashes keep old `opk_...` values working without a YAML
  registry.
- `gateway/discovery.py`: pure workflow registry with audience-aware summaries
  (`internal`, `admin`, `client`).
- `gateway/manifests.py`: workflow manifest load/validate plus
  `apply_backend_binding` and receipt-ready `bind_backend_request`.
- `gateway/extensions.py`: pure typed request-extension validator for
  namespaced gateway-native controls. It records accepted/rejected/ignored
  field names without logging values.
- `gateway/capabilities.py`: pure capability record, resource estimate, and
  audience-aware summaries for future discovery and router pre-filtering.
- `gateway/local_capabilities.py`: pure builder that converts
  `proxy_config.yml`-shaped data into local capability records for LLM, image,
  video, audio, speech, transcription, and embeddings.
- `gateway/api.py`: `/gateway/v1` router for workflows, capabilities,
  receipts, and durable media-job state. It is mounted in `proxy.py` behind the
  proxy key layer.
- `gateway/receipts.py`: `RouteReceipt` data model with policy-decision and
  validation hooks.
- `gateway/receipt_store.py`: SQLite receipt persistence with audience
  filtering and structural guards against raw prompts, media bytes, request
  bodies, and secrets.
- `gateway/media_jobs.py`: SQLite-backed durable media-job model and store,
  wired to `/gateway/v1/media/jobs`.
- Durable image media-job execution: `POST /gateway/v1/media/jobs` accepts
  `intent: image.generate` with an OpenAI image request under `request`, queues
  it server-side, retries transient ComfyUI/VRAM/tile-composition failures, and
  exposes completed image assets through
  `/gateway/v1/media/jobs/{job_id}/content`.
- `gateway/protocols/anthropic.py` and `gateway/protocols/ollama.py`:
  pure translators for Anthropic Messages and Ollama chat/generate/tags/show/ps
  frontends, including stream-envelope translation.
- `manifests/workflows/flux2-klein-4b*.yml` and
  `manifests/workflows/tangoflux.yml`: initial portable workflow manifests for
  image generation/editing and TangoFlux audio generation.
- Observational image-route receipts: `/v1/images/generations` and
  `/v1/images/edits` create a receipt after auth, attach a
  profile-derived policy decision, record safe field-name validation outcomes
  for standard image inputs and namespaced workflow extensions, attach manifest
  identity when a binding participates, persist to SQLite, and emit a body-free
  `image_route_receipt` event through the firehose metrics path. The receipt is
  not returned inline to clients; it is discoverable through `/gateway/v1`.
- Live protocol frontends: `POST /v1/messages`, `POST /api/chat`,
  `POST /api/generate`, `GET /api/tags`, `POST /api/show`, `GET /api/ps`, and
  `GET /api/version`. Anthropic Messages streams as Anthropic SSE; Ollama
  chat/generate stream as NDJSON. Mutating Ollama model-management routes are
  registered but return unsupported-operation instead of pretending to mutate
  local model files.
- `scripts/compat/` and `tests/compat/` SDK compatibility harness (see
  `docs/SDK_COMPATIBILITY_HARNESS.md`). The matrix runner executes registered
  unittest probes when SDK and environment preconditions are present; published
  compatibility claims are still separate from this infrastructure.
- `scripts/local/upgrade_profiles.py`, `scripts/local/smoke_client.sh`, and
  `scripts/compat/smoke_protocol_frontends.py`: local operator tooling for
  additive profile migration and smoke checks without printing secrets.

Not implemented:

- A clean protocol frontend / canonical intent / router / scheduler split is
  not fully extracted yet; request handling still largely lives in `proxy.py`.
- No backend adapter contract has been extracted.
- Ollama embeddings are implemented as a protocol frontend over the
  OpenAI-compatible `/v1/embeddings` surface. The current local provider is a
  CPU-only `nomic-embed-text-v1.5` llama.cpp server so embeddings do not claim
  GPU budget.
- Provider-native passthrough is implemented only under
  `/gateway/v1/providers/{provider}/{path...}` and remains policy-gated by App
  profile. Provider-native discovery is implemented at `/gateway/v1/providers`
  and `/gateway/v1/providers/{provider}`. There is no root catch-all or legacy
  root Tabby shim.
- Gateway media assets are exposed through `/gateway/v1/assets/{asset_id}` for
  metadata and `/gateway/v1/assets/{asset_id}/content` for signed,
  bearer-auth-gated content. The content-addressed cache still has a TTL; it is
  a reusable local handle, not a permanent object store.
- ACE-Step music and configured video workflows have committed manifests and a
  registry guard against config/manifests drift.
- Capability matching, manifest validation, and workflow bindings are not yet
  the source of truth for every live request route.

## Target Users

### 1. Local Agent And Coding Tool Power User

They use local models through Continue, Cline, OpenCode, Claude Code, Codex-like
tools, LibreChat, Open WebUI, or custom agents.

They value:

- one stable `base_url`
- OpenAI and Anthropic compatibility
- streaming that works
- reliable tool calls
- model aliases such as `coder`, `fast`, `reasoning`, `vision`
- long-context behavior that fails honestly
- optional cloud fallback before first output
- route receipts explaining what actually ran

Architecture implication:

- Anthropic Messages is a P0 public protocol, not a later novelty.
- Tool-call normalization belongs in `chat.completions`, not as OpenAI-only glue.
- Dashboard wording should stay in App/Profile/Key language; agent/coding users
  should not need to learn scheduler or workflow internals to connect a tool.

### 2. Multi-Backend GPU Tinkerer

They run multiple local engines because each is good at something different.

They value:

- routing by model, capability, context, speed, or GPU availability
- model lifecycle visibility
- hot/cold backend state
- native controls when OpenAI-compatible APIs are too narrow
- diagnostics for "why did this client fail?"
- performance telemetry and GPU/VRAM state

Architecture implication:

- every backend adapter needs capability, health, runtime config, and resource
  metadata
- OpenAI-compatible inference adapters are not enough; native management
  adapters matter
- Provider/resource detail belongs in diagnostics and status, not the ordinary
  "give someone access" flow.

### 3. Local Multimodal Workflow Builder

They use ComfyUI, image/video workflows, TTS/STT, music, sound effects, and
automation tools.

They value:

- simple public APIs for image/audio/video jobs
- workflow aliases instead of raw graph JSON
- queue status, progress, cancellation, and durable assets
- dependency checks for models/custom nodes
- local-only guarantees
- reproducible outputs

Architecture implication:

- ComfyUI is a workflow runtime, not a public protocol
- workflow manifests and media jobs are core gateway concepts
- Workflow manifests should feel like named tools with typed controls, not a
  second programming language users must understand before they can generate
  an image, sound, or video.

## Community Fit Decision

The shared ideal across r/selfhosted, r/LocalLLaMA, r/StableDiffusion, and an
SLVP advisor is not "more settings." It is safe defaults, good escape hatches,
and almost no repeated permission editing.

| Audience | Ideal experience | Design pressure |
|---|---|---|
| r/selfhosted operators | OIDC admin login, App keys, revocation, audit, obvious access previews | normal self-hosted app UX; avoid policy mazes |
| r/LocalLLaMA users | one OpenAI/Anthropic/Ollama-compatible endpoint, model discovery, routing, honest failures | preserve model/backend visibility and native escape hatches |
| r/StableDiffusion users | named workflows with typed controls, dependency health, reproducibility, LoRA/ControlNet/seed/denoise control | do not flatten ComfyUI power into a toy image API |
| SLVP advisor | prove 2-3 flows end-to-end with metadata defaults before building full RBAC/quota/scheduler UI | keep the product model small and implementation facts testable |

The design decision is:

```text
Profiles grant categories by metadata.
Manifests/adapters declare metadata.
Explicit per-model/per-workflow lists are overrides, not the default.
```

Adding a new model, workflow, or service should normally require editing only
that thing's manifest/adapter metadata. It should not require touching every
App or Profile. A new capability becomes visible automatically when its
metadata matches a Profile; risky capabilities stay opt-in.

Default exposure classes:

```text
default      Safe for any matching Profile
opt_in       Requires an explicit Profile selector or allow-list
admin        Admin/native-power only
disabled     Not exposed through the gateway
```

Common selectors:

```text
intent          chat.completions, image.edit, speech.synthesis, video.generate
trust_tier      stable, experimental, private, admin
resource_tier   interactive, batch, expensive
exposure        default, opt_in, admin, disabled
backend         comfyui.local, llama.cpp.gpu0, tabby.local
tags            family-safe, native-power, controlnet, inpaint, music
```

The dashboard should explain outcomes in human terms:

```text
This App can use stable interactive chat and speech.
It cannot use image/video, cloud fallback, native passthrough, or expensive batch jobs.
```

That explanation is the main UX. The underlying selectors are how the gateway
avoids micromanagement.

## Design Principles

1. **Simple over easy.**
   Do not let every frontend know every backend. Add one canonical intent layer.

2. **Fidelity at the edge, breadth at the back.**
   Public protocols need conformance tests. Backend adapters need capability
   metadata and honest limitations.

3. **No fake compatibility.**
   Unsupported features return protocol-shaped errors. They are not silently
   dropped.

4. **Runtime facts beat static config.**
   Probed context length, model state, flags, GPU placement, queue state, and
   workflow drift should override stale assumptions.

5. **Local-first is a security model.**
   Cloud fallback, native passthrough, status visibility, and backend lifecycle
   actions must be explicit policy decisions.

6. **Authority before observability.**
   Route receipts should carry policy decisions and visibility boundaries, not
   just request facts.

7. **Events before dashboards.**
   Dashboards, metrics, audit logs, progress streams, and route receipts should
   all derive from normalized events.

8. **Externally managed services are normal.**
   The gateway should integrate with systemd, Docker, processes, and remote URLs.
   It should not assume it owns every backend.

9. **Workflow engines need manifests.**
   Raw ComfyUI/A1111/Invoke graphs are implementation details. API clients need
   typed inputs, output contracts, dependencies, resources, and drift status.

## Architecture

Recommended shape:

```text
Client SDK / App / Agent
        |
        v
Protocol Frontend
        |
        v
Canonical Intent
        |
        v
Router / Policy
        |
        v
Scheduler / Resource Claims
        |
        v
Backend Adapter
        |
        v
Local or Hosted Backend
```

Side planes:

```text
Access Plane       Users, Apps, Profiles, Keys
Runtime Facts      Capabilities, Workflows, Providers, Jobs, Receipts, Usage
Operator Tools     Diagnostics, Compatibility Harness, Assets, Lifecycle
```

Only the Access Plane should dominate the dashboard. Runtime Facts and Operator
Tools exist so sophisticated users can debug and extend the gateway, but they
should be reached from concrete tasks such as "why was this request denied?",
"what is down?", or "what controls does this workflow expose?"

## Protocol Frontends

Protocol frontends own client compatibility.

They parse vendor/client-shaped requests, validate them, translate them into
canonical intents, and render protocol-shaped responses, streams, and errors.

P0 frontends:

- OpenAI-compatible `/v1`
- Anthropic Messages
- Ollama native API

P1 frontends:

- Gateway media job/assets API
- ElevenLabs-compatible speech, if voice clients require it
- Gemini GenerateContent, if a concrete workflow requires Gemini-native clients

Do not implement public protocols as backend special cases. An Anthropic route
should not know that TabbyAPI exists. It should know how to translate Anthropic
Messages into `chat.completions` and how to render Anthropic event streams/errors.

## Backend Adapters

Backend adapters own execution.

They translate canonical intents into backend-native calls, report capability
metadata, expose health/capacity/runtime config, and emit normalized events.

P0 backend families:

- Ollama
- llama.cpp server
- vLLM OpenAI-compatible server
- LM Studio
- TabbyAPI
- KoboldCpp/KoboldAI
- ComfyUI
- local TTS
- local STT
- local music/SFX engines

Adapter pattern:

1. **OpenAI-compatible baseline adapter**
   Gets many LLM backends working quickly.

2. **Backend-native management adapter**
   Handles model list/show/load/unload, slots, LoRA, pull/download, health,
   runtime flags, queue state, and backend-specific controls.

Examples:

- Ollama native for `/api/tags`, `/api/show`, `/api/ps`, `keep_alive`
- llama.cpp native for `/props`, `/slots`, `/health`, `/reranking`
- TabbyAPI admin for model load/unload
- LM Studio native for model download/load/unload
- ComfyUI native for `/prompt`, `/queue`, `/history`, `/ws`, uploads

The public protocol target set can remain opinionated while backend coverage
stays broad.

## Canonical Intents

Use a small but broad discriminated union. Do not build a giant vague
`AIRequest`.

Initial intent families:

```text
chat.completions
text.complete
text.embed
text.rerank
speech.synthesis
speech.transcribe
audio.translate
audio.generate
sound.generate
music.generate
image.generate
image.edit
image.upscale
image.inpaint
video.generate
video.edit
model.list
model.show
model.running
model.pull
model.load
model.unload
job.create
job.get
job.cancel
asset.get
```

Not every protocol exposes every intent. Not every backend implements every
intent. That is the point: capabilities are explicit instead of implicit.

## Router And Policy

The router selects a backend candidate and fallback chain from data:

- intent type
- requested model/workflow/voice
- required capabilities
- protocol frontend
- auth identity
- app profile
- backend health and readiness
- runtime capability probes
- route policy
- local-only/cloud policy
- estimated resources

Router output:

- selected backend
- fallback chain
- timeout budget
- retry/fallback rule
- scheduler class
- route receipt skeleton

Fallback rule:

```text
Fallback is allowed only before the first externally visible output event.
After that, failures are surfaced as stream/job failures unless a protocol
explicitly supports replay or resume.
```

## Scheduler And Resource Claims

Local hardware makes scheduling part of correctness.

The scheduler must know:

- GPU identity
- VRAM reservation and runtime use
- RAM and disk pressure
- model residency
- workflow resource estimates
- queue state
- cancellation signal
- timeout budget
- backend lifecycle state
- operational safety signals
- operator mode constraints (`normal`, `llm-only`, `media-only`,
  `maintenance`, custom)
- per-App/profile concurrency and usage budgets
- degraded-mode rules when a Provider is down, warming, draining, or queued

Resource claims should look like data:

```yaml
claims:
  - kind: gpu_vram
    gpu: gpu0
    estimate_mb: 9000
  - kind: model
    id: flux2-klein-4b
    mode: shared
  - kind: process
    service: comfyui
    mode: shared
  - kind: disk
    path: /media/windows
    estimate_mb: 5000
```

Scheduling policy should also be data, not hidden conditionals:

```yaml
mode: llm-only
constraints:
  - kind: provider_state
    provider: comfyui.local
    state: disabled
    reason: reserving_vram_for_llm
  - kind: queue_budget
    profile: family-chat
    max_wait_seconds: 15
  - kind: concurrency
    profile: image-agent
    max_running: 1
  - kind: usage_budget
    app: brother-laptop
    window: day
    max_gpu_seconds: 600
```

Admission happens before an expensive backend is touched:

```text
request
  -> auth/quota
  -> capability health
  -> mode policy
  -> resource lease decision
  -> backend adapter
```

The outcome should be explicit HTTP behavior:

- `202` when accepted into a durable queue
- `429` when the App/Profile has exceeded a quota or concurrency budget
- `503` when a capability is unavailable or under maintenance
- `409` when the request conflicts with the current operator mode

Capability health is per capability and provider, not a single global boolean:

```yaml
capability: image.generate
provider: comfyui.local
state: maintenance # available | degraded | draining | maintenance | unavailable
reason: reserving_vram_for_llm
observed_at: 2026-05-17T12:00:00Z
retry_after_seconds: 900
```

Resource leases should have a tiny lifecycle: `requested -> granted ->
renewed -> released`, with `denied` and `expired` as terminal outcomes. A
lease can represent GPU VRAM, a model slot, a long-running media queue slot, or
an exclusive mode such as "smart LLM owns gpu0".

The SLVP scheduler can be simple:

- per-backend semaphores
- per-GPU queues
- no preemption
- cancellation propagation
- timeout budgets
- advisory resource warnings
- explicit maintenance/drain states that fail or queue before execution
- operator-selectable mode profiles that can disable whole capability classes
  without editing every App Profile

But its boundary must exist early, because hidden backend-level restarts and
silent model swaps do not scale.

## Lifecycle States

Backend state is not binary.

Use explicit lifecycle/readiness states:

```text
up
down
cold
standby
starting
stopping
degraded
failed
externally_managed
unsupported
```

The current stack already needs this: TabbyAPI can be down at idle and still be
a valid standby backend.

Lifecycle providers should be pluggable:

- none
- systemd user
- Docker
- compose
- process command
- remote URL

The router should not assume it may start or stop a backend. Policy and the
lifecycle provider decide that.

## Capability Registry

Every backend adapter should publish an effective capability document.

It should include:

- supported intents
- exposure class (`default`, `opt_in`, `admin`, `disabled`)
- trust tier (`stable`, `experimental`, `private`, `admin`)
- resource tier (`interactive`, `batch`, `expensive`)
- tags used by Profiles and templates
- supported public protocol passthroughs
- native management operations
- model list
- loaded/running models
- context limits
- tool-call support
- structured-output support
- vision/audio modalities
- embeddings/rerank support
- streaming support
- cancellation support
- max concurrency
- resource estimates
- runtime flags
- version/build metadata
- known compatibility caveats

Effective capability is the merge of:

- declared config
- runtime probes
- manual operator overrides
- compatibility test results

When runtime probes and static config disagree, the gateway should surface the
disagreement and avoid overclaiming.

Capability metadata is the anti-micromanagement contract. A new stable chat
model marked `intent=chat.completions`, `trust_tier=stable`,
`resource_tier=interactive`, `exposure=default` should become available to
matching chat Profiles automatically. A new experimental ControlNet workflow
marked `intent=image.edit`, `trust_tier=experimental`,
`resource_tier=batch`, `exposure=opt_in`, `tags=[controlnet]` should not appear
for family/basic Apps until an operator opts that Profile into the tag or ID.

## Access Plane

Preserve and generalize what the current implementation already does well, but
keep the operator-facing surface small so admin- and runtime-plane concerns
stop blurring together.

The public model is:

```text
User      Human identity from OIDC
App       Access boundary for a tool, device, agent, or family member
Profile   Permission bundle attached to an App
Key       Rotatable secret for an App or admin automation
Activity  Usage, audit, receipts, jobs, and errors as facts
```

Implementation still has durable facts such as ServiceAccount, Credential,
AdminRole, AuditEvent, Capability, Provider, Workflow, Job, Receipt, and
UsageRecord. Those are real data. They should not all become top-level product
concepts unless the operator must manage them directly.

The legacy entity names (`AuthIdentity`, `CredentialMapping`, `Policy`) survive
inside the implementation, but product docs, discovery surfaces, and operator UI
should stay centered on Users, Apps, Profiles, Keys, and Activity. The word
"client" remains valid only for compatibility uses (SDK names, audience tiers,
protocol-level wording); the consumer entity in product docs is an **App**.

The load-bearing invariant is the same one stated in `docs/CORE_CONTRACTS.md`:
a request without a resolved App + Profile + policy decision is never
executed, and every executed request produces exactly one body-free Activity
fact/Receipt.

Required key behavior:

```text
generate -> store hash -> use -> rotate -> revoke -> audit
```

Required behavior:

- generate proxy keys without storing raw key material
- hash stored proxy keys
- hot-reload key changes
- rotate and revoke keys without restart
- map one proxy key to different downstream backend credentials
- support environment-backed and file-backed secrets
- strip sensitive client headers before forwarding
- fail closed when auth is enabled but no keys are configured
- scope keys by protocol, intent, backend, model, passthrough, and cloud fallback
- record per-key usage, errors, route receipts, tokens, media jobs, bytes, and GPU time

There are two separate token concerns:

1. Credential tokens: API keys, bearer tokens, backend secrets.
2. Usage tokens: prompt/output tokens, audio/video seconds, asset bytes, GPU time.

Do not conflate them.

## App Templates And Profiles

Do not overload "profile." There are two related but different things:

- **Policy Profile**: authorization for an App. This is the durable
  `Profile` attached to an App.
- **App Template**: setup help for Open WebUI, LibreChat, Continue, Daisy,
  etc. This is documentation/tests/defaults, not an authorization grant by
  itself.

Named keys such as `open-webui`, `librechat`, `claude-test`, and `daisy` imply
app-specific behavior, but the dashboard should expose them as templates that
create an App with a recommended Policy Profile and compatibility checks.

Add App Templates for:

- Open WebUI
- LibreChat
- SillyTavern
- Continue
- Cline
- OpenCode
- Claude Code
- n8n
- Home Assistant custom OpenAI-compatible integrations

Profiles should define:

- expected protocol
- base URL shape
- required endpoints
- auth header behavior
- streaming expectation
- tool-call expectation
- model discovery behavior
- compatibility smoke tests
- recommended Profile fields

Templates are not hardcoded special-case routing. They are setup recipes,
compatibility tests, and defaults. The attached Policy Profile remains the
actual authorization boundary.

Policy Profiles should prefer selectors over enumerating every model/workflow.
Use explicit allow/deny lists only for narrow Apps, fragile workflows, or
temporary exceptions.

Example:

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

This Profile should automatically pick up a new stable interactive chat model.
It should not pick up experimental image/video workflows, expensive batch
rendering, native provider passthrough, or cloud fallback.

### Concrete flow: give a family member a key

The self-hoster-friendly flow that this architecture must support end-to-end:

1. **OIDC User.** The family member signs in once so the gateway has their
   `User` row. Browser login is `/admin/login` when `admin.oidc` is
   configured; service-account admin keys exist for CLI and automation.
2. **Personal App.** An owner/admin creates one `App` owned by that User —
   `aunt-mary-chat`. The App is the access boundary, not the person.
3. **Policy Profile.** A narrow Profile is attached:
   `protocols: [openai.chat, openai.audio]`, `cloud_allowed: false`,
   `native_passthrough: { enabled: false }`, `status_visibility.level: basic`.
4. **App API key.** The owner mints an App Credential through `/admin/api`
   (`POST /admin/api/apps/{app_id}/keys`). The returned `opk_...` value is
   shown once and pasted into the App's config with base URL
   `https://ai.vivid.fish/openai/v1` (`/v1` remains a convenience route).

Rotation is per-App; the User and Profile survive key rotation. Dashboard
key-minting should inherit the App's current Profile by default. Per-key
Profile overrides are exceptional and should be used only for narrower or
temporary access.

## Normalized Events And Route Receipts

Use one normalized event model for:

- request logs
- route receipts
- streaming
- job progress
- metrics
- audit
- status page

Important event types:

```text
request.started
auth.accepted
intent.created
route.selected
scheduler.queued
scheduler.started
resource.claimed
model.load.started
model.load.completed
backend.request.started
token.delta
audio.chunk
image.progress
job.progress
asset.created
backend.warning
request.completed
request.failed
```

Every request/job should produce a route receipt:

```yaml
protocol: openai
intent: image.generate
auth_identity: daisy
requested_model: gpt-image-1
resolved_model: flux2-klein-4b
backend: comfyui
workflow: flux2-klein-4b
scheduler_queue: gpu0-interactive
resource_claims:
  - gpu0 vram estimate 3600mb
fallbacks_considered: []
policy:
  cloud_fallback: false
```

Receipts should be available to admins and optionally to clients through a debug
header or response expansion. They should be redacted by audience.

## Event Storage

The current metrics layer is useful but assumes one process and uses a mix of
in-memory state, JSONL replay, and SQLite.

SLVP storage can still be simple:

- SQLite for durable events, jobs, responses, and usage
- in-memory caches for current progress and hot metrics
- JSONL only as optional debug/export, not the primary state model

Declare multi-worker unsupported until event/progress state is centralized.

## Workflow Manifests

ComfyUI-style backends need workflow manifests.

Raw graph JSON is not a stable public contract. A manifest should define:

```yaml
id: product-photo
intent: image.generate
backend: comfyui
workflow: workflows/images/product-photo.json
ui_workflow: workflows/images/product-photo.ui.json
inputs:
  prompt:
    type: string
    required: true
  background:
    type: enum
    values: [auto, transparent, opaque]
    default: auto
outputs:
  image:
    media_type: image/png
resources:
  vram_estimate_mb: 9000
dependencies:
  models:
    - flux2-klein-4b
  custom_nodes: []
concurrency:
  safe: true
```

Manifest responsibilities:

- typed inputs
- defaults and validation
- output contracts
- portable input/output semantics separate from backend-specific bindings
- explicit mapping from workflow inputs to backend request fields, nodes, or commands
- dependency checks
- resource estimates
- warm/cold behavior
- drift hash between API and UI workflow
- caller-controlled versus operator-controlled fields
- reproducibility metadata

Workflow drift should degrade or block the affected workflow.

Workflow manifests are the main escape hatch for advanced media controls. For
example, image-edit settings such as denoise or ControlNet strength should start
as typed manifest inputs with backend-specific bindings, not as global OpenAI
Images fields. A ComfyUI binding may map an input to node IDs; an A1111 binding
may map the same input to `/sdapi/v1/img2img` fields; a future purpose-built
image-edit backend may map it to ordinary JSON. If those controls become common
across users/backends, they can graduate into shared manifest conventions or
canonical intent fields.

## Media Jobs And Assets

Images, video, music, sound effects, and long audio are not simple chat
requests.

Use three layers:

1. OpenAI-compatible Images/Audio where it fits.
2. Explicit provider-native passthrough for power users.
3. Generic media job/assets API for long-running work.

Recommended job surface (image.generate first slice and generic generated
asset serving are wired in `proxy.py`; audio/video job adapters remain next):

```text
POST /gateway/v1/media/jobs
GET  /gateway/v1/media/jobs/{id}
GET  /gateway/v1/media/jobs/{id}/content
GET  /gateway/v1/media/jobs/{id}/events
POST /gateway/v1/media/jobs/{id}/cancel
GET  /gateway/v1/assets/{id}
GET  /gateway/v1/assets/{id}/content
```

Keep OpenAI-compatible `/v1/*` clean. Gateway-owned discovery, jobs,
diagnostics, native passthrough, and extension schemas belong under
`/gateway/v1/*` so clients never confuse gateway features for vendor-standard
OpenAI behavior.

The canonical protocol aliases are `/openai/v1`, `/anthropic/v1`, and
`/ollama/api`. The `/v1` and `/api` routes are convenience mounts for clients
that expect OpenAI or Ollama-style roots.

Media job records should persist:

- job ID
- auth identity
- intent
- workflow alias
- backend prompt/job ID
- inputs hash
- route receipt
- resource claims
- status
- progress
- output asset IDs
- errors
- timestamps

Synchronous OpenAI-shaped responses can be implemented as a thin wrapper over
internal jobs when the operation is short enough.

## Native Passthrough Lanes

Local users need escape hatches, but they must be explicit.

Use namespaced passthrough paths:

```text
/gateway/v1/providers
/gateway/v1/providers/{provider}
/gateway/v1/providers/{provider}/{path...}
```

The collection and provider routes are discovery-only and should project lanes
through the current App profile. Raw passthrough is reserved for the deeper
`{path...}` route.

Passthrough requirements:

- disabled by default for non-admin keys
- scoped per key
- allowed only when the App profile includes the `provider-native` protocol
- logged in route receipts
- never accidentally exposed by a root catch-all
- no legacy root Tabby shim
- redacts credentials
- clearly labeled as native/non-portable

The existing catch-all behavior is valuable for local power use, but it should
not become the public security boundary.

Unknown public-protocol parameters must not be silently accepted when they do
nothing. A protocol frontend should accept protocol fields and documented gateway
extension containers, reject unknown extension inputs, and include accepted,
ignored, or rejected inputs in the route receipt.

## Status, Diagnostics, And Security

Split health/status into visibility tiers:

```text
/healthz                  public liveness only
/v1/diagnostics           authenticated user/app diagnostics
/v1/admin/status          admin/operator diagnostics
```

Public diagnostics must not expose:

- local model paths
- GPU UUIDs
- process IDs
- service names where sensitive
- key names
- recent request metadata
- local filesystem paths
- backend credentials or headers

Diagnostics should test:

- model listing
- streaming chat
- tool calls
- context limits
- embeddings
- image generation
- image edits
- TTS/STT
- media jobs
- workflow dependencies
- backend health
- route selection
- auth scopes

The gateway should know its exposure mode:

```text
localhost
LAN
public
```

Public exposure should warn loudly when admin diagnostics are enabled.

## API Targets

The detailed target inventory is in `docs/API_TARGETS.md`.

Summary:

P0 public protocols:

- OpenAI Chat Completions
- OpenAI Responses
- OpenAI Images
- OpenAI Audio
- OpenAI Embeddings
- Anthropic Messages
- Ollama native API

P1 public protocols:

- gateway media job/assets API
- ElevenLabs-compatible speech if voice clients require it
- Gemini GenerateContent if a concrete workflow requires it
- KoboldAI-compatible API if SillyTavern/roleplay users require native behavior

Backend coverage should remain much broader than public protocol coverage.

Gateway-native discovery lives outside vendor-compatible namespaces. The live
first slice is:

```text
GET /gateway/v1/capabilities
GET /gateway/v1/workflows
GET /gateway/v1/workflows/{workflow}
GET /gateway/v1/receipts
GET /gateway/v1/receipts/{request_id}
POST /gateway/v1/media/jobs
GET /gateway/v1/media/jobs
GET /gateway/v1/media/jobs/{id}
POST /gateway/v1/media/jobs/{id}/events
POST /gateway/v1/media/jobs/{id}/cancel
```

The remaining target discovery extensions are:

```text
GET /gateway/v1/models/{model}/capabilities
GET /gateway/v1/extensions
```

This is not a standard API. It is the gateway's contract for introspection,
schemas, and local capability discovery.

## Compatibility Harness

The detailed harness plan is in `docs/SDK_COMPATIBILITY_HARNESS.md`.

P0 SDKs:

- `openai` Python
- `openai` Node
- `anthropic` Python
- `@anthropic-ai/sdk`
- `ollama` Python
- `ollama` JavaScript

P1 app/consumer smokes:

- Vercel AI SDK
- Open WebUI
- LibreChat
- SillyTavern
- Continue
- Cline
- OpenCode
- Claude Code
- n8n
- Home Assistant custom OpenAI-compatible integrations

Publish compatibility as a matrix by SDK/app version, endpoint, feature, and
backend. Do not claim broad compatibility without passing tests.

## Migration Plan

### Phase 0: Freeze And Inventory

- Record current endpoint inventory.
- Add smoke tests for current behavior.
- Publish first compatibility matrix for existing OpenAI-shaped surface.
- Capture current status/config/workflow facts in fixtures.
- Add route receipt skeletons without changing behavior.

### Phase 1: Stabilize Events, Receipts, And Auth

- Introduce normalized event types.
- Add durable SQLite event/job/usage tables.
- Add route receipts for current handlers.
- Split status into public/user/admin tiers.
- Promote per-key scopes and stored Policy Profiles.

### Phase 2: Define Core Contracts

- Add `gateway/intents.py`.
- Add `gateway/events.py`.
- Add `gateway/capabilities.py`.
- Add `gateway/adapters/base.py`.
- Add workflow manifest schema.
- Add lifecycle provider interface.

Keep existing routes working. Move one handler at a time behind the contracts.

### Phase 3: Extract Backend Adapters

- llama.cpp/OpenAI-compatible LLM adapter
- TabbyAPI inference/admin adapter
- TTS adapter
- STT adapter
- ComfyUI workflow adapter
- media asset/job store

Then add Ollama, LM Studio, vLLM, Kobold, A1111/Forge, TEI/TGI as separate
adapters.

### Phase 4: Add Public Protocol Frontends

- Solidify OpenAI frontend from current behavior.
- Solidify the live Anthropic Messages frontend, including SSE streaming.
- Solidify the live Ollama chat/generate/tags/show/ps/version frontend,
  including NDJSON chat/generate streaming and read-only model metadata.
- Extend gateway media jobs beyond the live image-generation executor to
  generic assets, video, audio/music, and provider-native adapters.

### Phase 5: Introduce Scheduler Enforcement

- Start with per-backend and per-GPU queues.
- Add resource claims.
- Add disk/thermal/Xid advisory policies.
- Add cold-start policy.
- Add cancellation and timeout propagation.

### Phase 6: Plugin Contract

- Keep plugins in-tree until the contract survives several real adapters.
- Then allow configured module or entry-point loading.
- Treat plugin API compatibility as a product commitment only after the core
  contract is proven.

## Non-Goals

Do not make the gateway:

- a training platform
- a model hub
- a mandatory UI
- an inference engine
- a managed SaaS control plane
- a universal clone of every vendor API
- a hidden router that silently drops unsupported fields

## Key Open Decisions

1. Should Ollama native be shipped as P0 public compatibility or P0.5 after
   OpenAI/Anthropic?
2. Should all media requests become internal jobs, even when the public API is
   synchronous?
3. Should route receipts be client-visible by opt-in header?
4. Which health signals block scheduling by default: disk, thermal, Xid, VRAM
   over-budget, workflow drift?
5. Should backend-native passthrough be disabled by default except for admin keys?
6. Should App Templates ship as dashboard setup recipes or compatibility
   harness fixtures first?
7. Is SQLite sufficient as the durable event/job store for the self-hosted SLVP?

## Success Criteria

The architecture is working when:

- Open WebUI, LibreChat, Continue, Cline/OpenCode, Claude Code, and n8n can each
  connect through dedicated app keys.
- OpenAI Python/Node, Anthropic Python/TS, and Ollama Python/JS compatibility
  tests produce a published matrix.
- A chat request, image request, TTS request, STT request, audio-generation job,
  and video job each produce route receipts.
- A ComfyUI workflow cannot be exposed unless its manifest, dependencies, and
  drift checks pass.
- A cold standby backend can be represented without marking the gateway failed.
- A local-only key cannot use cloud fallback or native admin passthrough.
- Operator diagnostics can show the full machine state, while public health
  exposes only liveness.
- A workflow-specific extension such as image-edit denoise/controlnet can be
  added through a manifest and proven with a fake backend test, without changing
  public protocol semantics.

## Bottom Line

The simple architecture is:

```text
Protocol Frontend -> Intent -> Router -> Scheduler -> Backend Adapter
                              |
             Auth / Capabilities / Events / Workflows / Diagnostics
```

The lovable product is:

```text
One reliable local endpoint that makes a messy self-hosted AI stack feel
coherent without hiding what actually happened.
```

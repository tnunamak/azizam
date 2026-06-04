# OpenAI Proxy / Vivid Fish AI Gateway UX Research Packet

Generated: 2026-05-18T17:08:07-05:00

Repo: /home/tnunamak/applications/openai-proxy

This packet concatenates UX-relevant product, architecture, access-management, status-page, compatibility, and modality-control research materials found on disk. Each source is included verbatim under a source header.

## Included sources

- `PRD.md`
- `ARCHITECTURE_RECOMMENDATION.md`
- `docs/ADMIN_ACCESS_UX_RESEARCH.md`
- `AUTH_GUIDE.md`
- `docs/CORE_CONTRACTS.md`
- `docs/SLVP_SCOPE.md`
- `docs/SELF_HOSTED_APP_QUIRKS_AUDIT.md`
- `docs/OWNER_EXECUTION_PLAN.md`
- `docs/API_TARGETS.md`
- `docs/EXTENSION_TARGETS.md`
- `docs/SDK_COMPATIBILITY_HARNESS.md`
- `docs/CONDITIONING_CONTROLS.md`
- `inbox/2026-05-08-status-page-audit.md`
- `inbox/2026-05-08-status-page-bugs-found.md`
- `inbox/2026-05-08-honesty-audit.md`
- `inbox/2026-05-09-deferred-status-features.md`
- `inbox/2026-05-07-video-generation-proposal.md`


---

# Source: PRD.md

# AI Gateway: Problem Statement

## Context

Local AI inference has reached a quality threshold where it's a credible substitute for hosted APIs across many tasks: chat, embeddings, TTS, STT, image generation, video generation, music generation, and sound effects. The local-inference toolchain is rich — ComfyUI, vLLM, llama.cpp, TabbyAPI, Kobold, ExLlamaV3, Voxtral, Parakeet, and many model-specific servers — and each speaks its own protocol.

At the same time, application developers have settled into using a small number of widely-adopted SDKs from hosted-AI vendors: OpenAI, Anthropic, ElevenLabs, and others. These SDKs are well-documented, type-safe, idiomatic in their target languages, and serve as the integration point for a growing ecosystem of agent frameworks, IDE extensions, and end-user products.

The gap between "I run my own models" and "I use a popular SDK against my own models" is currently bridged ad-hoc — per deployment, per stack, with one-off shims.

## The Core Problem

There is no widely-used way to expose a local inference stack such that:

- Multiple hosted-AI SDKs can target it as a drop-in `base_url`, simultaneously.
- Each capability (chat, TTS, music, etc.) is served by whichever local backend is best suited to it.
- Adding a new SDK or a new local backend doesn't require modifying core code.

Existing gateways tend to be opinionated in one of two directions:

- **Single-protocol, multi-backend.** Speak OpenAI's API; route to many local backends. Works well for chat-shaped work; awkward or impossible for capabilities OpenAI doesn't have endpoints for (music, video, voice design, conversational agents).
- **Per-product proxies.** One project mimics ElevenLabs; another mimics Anthropic; another mimics OpenAI. Users running multiple workloads have to run multiple proxies, each with its own auth, config, and observability surface.

Neither shape lets a user say *"I have local models for these capabilities; expose them through whichever SDK my application happens to already use."*

## Why This Is Hard

- **Protocol surfaces are large and inconsistent.** Each vendor's SDK has its own request shape, response shape, streaming conventions, error envelope, and auth header. SDKs validate responses against generated types, so partial compatibility fails fast.
- **Capabilities don't map 1:1 across protocols.** OpenAI has no music endpoint. ElevenLabs has limited tool-use semantics. Anthropic has no image generation. A federated gateway must handle gaps gracefully — return appropriate errors for unsupported routes, route to compatible backends when capabilities overlap.
- **Local backends vary in shape.** ComfyUI is graph-based and asynchronous. Voxtral is a streaming HTTP service. llama.cpp is already OpenAI-compatible. TabbyAPI and Kobold differ again. Each has its own idea of "model" and its own resource cost. The gateway cannot assume a uniform backend interface.
- **Resource orchestration is unavoidable.** Local hardware has a fixed VRAM budget. Models must be loaded, unloaded, and swapped. Some workloads compete; some compose. The gateway sits at the natural choke point for these decisions, but doing so without leaking hardware concerns into the protocol layer is non-trivial.
- **Operational concerns scale with surface area.** Auth, multi-tenancy, observability, rate-limiting, request retries, and audit logging are needed even for a single-user deployment, and become essential as more SDKs and backends are added.
- **Protocols evolve.** Vendors ship new endpoints and deprecate old ones. The gateway has to track those changes without rewrites.

## Who This Is For

- **Self-hosters** running local models who want to use applications written against hosted APIs without rewriting them.
- **Developers** building products that should work against both hosted and local inference, transparently.
- **Researchers and tinkerers** experimenting with new local models, who want familiar SDKs as their interaction layer.
- **Small teams** running shared local inference infrastructure, who need multi-tenant auth and observability without standing up a full ops platform.

## What Success Looks Like

A user can:

1. Install the gateway.
2. Point it at the local inference services they already run.
3. Configure which SDK protocols they want to expose.
4. Hand any compatible application an SDK `base_url` and an API key.
5. Have the application work — for every capability the gateway has a backend for — without modification.

A maintainer or contributor can:

1. Add support for a new SDK by writing one focused module, without touching unrelated protocols.
2. Add support for a new local backend by writing one focused module, without touching protocol code.
3. Reason about routing, auth, and resource policy in one place.

## Non-Goals

- Replacing the local inference engines themselves. The gateway is *only* the protocol/routing layer.
- Becoming an inference framework, training platform, or model hub.
- Providing a UI. The gateway is API-only; UIs are a separate concern.
- Reimplementing every feature of every SDK. Some capabilities (e.g. ElevenLabs voice cloning, OpenAI Assistants threads) may have no local equivalent and are out of scope by definition.
- Hosted deployment as a managed service. The gateway is designed to be self-hosted; a managed offering is a downstream decision, not a project goal.

## Open Questions

- What is the right level of abstraction between protocols and backends? A canonical "intent" type? Direct protocol-to-backend wiring? A hybrid?
- How are conflicts resolved when two SDKs use the same path (`/v1/models`) with different response shapes? Path namespacing, header-based dispatch, union responses, something else?
- How explicit should the routing config be? Fully declarative YAML? Code-level registration? Both?
- How do we handle capability gaps gracefully — return SDK-shaped 404s, route to a fallback, or surface the gap to the user?
- What does the plugin contract look like such that an out-of-tree plugin can ship without forking the project?
- How does the gateway represent resource orchestration (VRAM budgets, model swaps) in a way that's useful without being prescriptive?
- What's the right shape for observability — protocol-shaped logs, backend-shaped logs, or a normalized event stream?
- How do we test multi-protocol compatibility durably as SDKs evolve?



---

# Source: ARCHITECTURE_RECOMMENDATION.md

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
- hashed proxy API keys
- per-key downstream credential mapping
- per-key usage/status metrics
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
  discovery view, image route receipts, and non-streaming protocol frontends.
- `gateway/profiles.py`: pure loader from existing proxy key-entry dicts into
  `AppProfile`, keeping credential lookup and file I/O outside the policy
  layer.
- `gateway/runtime_auth.py`: bridge between the existing proxy key registry and
  profile-aware gateway identities. Real `proxy_api_keys.yml` entries can now
  carry additive `profile:` blocks without rotating keys.
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
  pure translators for non-streaming Anthropic Messages and Ollama
  chat/generate/tags frontends.
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
- Live non-streaming protocol frontends: `POST /v1/messages`, `POST /api/chat`,
  `POST /api/generate`, and `GET /api/tags`. Streaming and backend-native
  Ollama management remain out of scope for this slice.
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
- Streaming Anthropic/Ollama frontends, Ollama model-management routes, and
  native passthrough provider namespaces are not implemented.
- Gateway media assets are not yet served by a generic durable
  `/gateway/v1/assets/*` surface; image media jobs use the existing signed
  `/v1/assets/{asset_id}/content` cache plus a job content endpoint.
- ACE-Step music and video workflow manifests are not yet backfilled.
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
   `https://ai.vivid.fish/v1`.

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

Recommended job surface (image.generate first slice is wired in `proxy.py`;
generic assets remain a target):

```text
POST /gateway/v1/media/jobs
GET  /gateway/v1/media/jobs/{id}
GET  /gateway/v1/media/jobs/{id}/content
GET  /gateway/v1/media/jobs/{id}/events
POST /gateway/v1/media/jobs/{id}/cancel
GET  /gateway/v1/assets/{id}
```

Keep OpenAI-compatible `/v1/*` clean. Gateway-owned discovery, jobs,
diagnostics, native passthrough, and extension schemas belong under
`/gateway/v1/*` so clients never confuse gateway features for vendor-standard
OpenAI behavior.

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
/gateway/v1/providers/ollama/*
/gateway/v1/providers/tabby/*
/gateway/v1/providers/comfyui/*
/gateway/v1/providers/kobold/*
/gateway/v1/providers/a1111/*
```

Passthrough requirements:

- disabled by default for non-admin keys
- scoped per key
- logged in route receipts
- never accidentally exposed by a root catch-all
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
- Solidify the live non-streaming Anthropic Messages frontend.
- Solidify the live non-streaming Ollama chat/generate/tags frontend.
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



---

# Source: docs/ADMIN_ACCESS_UX_RESEARCH.md

# Admin Access UX Research

Date: 2026-05-18

This note records the design research behind the admin access-management UI for
Vivid Fish AI Gateway. It is intentionally product-facing: the durable backend
model can stay explicit, but the UI should help operators complete access tasks
without hand-authoring policy documents.

## Durable Model

Keep the existing model:

```text
User or service account -> App -> Profile -> API keys
```

Use these nouns consistently:

- **App**: the thing using the gateway, such as LibreChat, Open WebUI,
  Continue, Home Assistant, a Telegram bot, a cron job, or a family member's
  device.
- **Profile**: a reusable access policy and runtime envelope. A Profile says
  what an App may use.
- **App API key**: an `opk_...` runtime credential issued to one App. Apps can
  have multiple keys for rotation.
- **Admin automation key**: an `oak_...` control-plane credential for scripts
  that manage the gateway itself. It is not an OpenAI-compatible runtime key.
- **Service**: a backend or workflow host, such as llama.cpp, ComfyUI, STT,
  TTS, or a custom HTTP tool.

The admin mental model should be:

```text
This App uses this App API key, and the key runs under this Profile.
```

Do not add another stored abstraction called Access, Plan, Template, or Grant
unless the implementation proves it needs one. Presets/templates are UI helpers
for generating existing objects.

Keep the plane split explicit:

```text
opk_... -> runtime App API key -> /v1, /gateway/v1, model/workflow execution
oak_... -> admin automation key -> /admin/api, /status, gateway management
```

`Full runtime access` and `Gateway admin automation` are different products.
The former can spend GPU/storage/time on runtime capabilities. The latter can
change gateway configuration, Apps, Profiles, and keys. Never blur them in the
same creation flow.

## Prior Art

The useful pattern across mature developer products is: expose the operator's
intent first, then push raw permissions into advanced/custom views.

- Stripe restricted API keys expose key type, name, per-resource permissions,
  one-time reveal, and rotation/restriction actions. The raw security mechanics
  are not the first screen.
- Cloudflare API token templates start from a known task and let the user
  modify permissions/resources afterward.
- GitHub fine-grained personal access tokens ask for token name, expiration,
  resource owner/repository scope, and permission levels, while the endpoint
  mapping stays in documentation/reference material.
- Tailscale OAuth clients expose client identity, scopes, and tags. OAuth
  protocol details and token exchange mechanics stay out of the main admin
  decision path.

Reference docs:

- https://docs.stripe.com/keys
- https://developers.cloudflare.com/fundamentals/api/reference/template/
- https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens
- https://tailscale.com/kb/1215/oauth-clients

## UX Principles

1. **Start from a task, not a schema.**
   The primary flow is "Create an API key for an app" or "Create a family
   chat policy", not "create service account, profile id, app name, protocol,
   intent, tags, JSON".

2. **Keep Apps and Profiles separate.**
   Many Apps can share one Profile, and an App can move Profiles without
   rotating all of its keys.

3. **Keep Keys separate from Apps.**
   A single App may need current, staging, and retiring keys. Revoking a key
   should preserve App history.

4. **Separate runtime access from gateway administration.**
   Runtime App keys are for apps and SDK tools. Admin automation keys are for
   trusted scripts that mutate gateway state. A broad runtime Profile must not
   imply control-plane permission.

5. **Make Profiles understandable as reusable policies.**
   A Profile should read like "Family chat: chat only, local stable routes,
   no cloud fallback" before it reads like JSON.

6. **Progressively disclose implementation details.**
   Profile ids, service account owner names, protocol selectors, exact
   intents, status visibility, native passthrough, and raw JSON belong in
   advanced sections.

7. **Treat media workflows as first-class risk/cost surfaces.**
   Image, audio, and video workflows have different latency, VRAM, storage,
   and safety implications from chat.

8. **Never grant newly discovered services implicitly.**
   A service can be available without being allowed by any Profile.

9. **Make denials and routing explainable.**
   Self-hosted operators need to know whether a failure was authentication,
   profile denial, upstream unavailable, queue/resource pressure, timeout, or
   invalid request shape.

## Holistic Admin IA

The admin UI should be organized around operator jobs, not storage tables:

- **Apps & Keys**: create App API keys, see active Apps, rotate/revoke runtime
  keys.
- **Profiles**: reusable runtime access policies, created from named templates
  and summarized in plain language.
- **Workflows**: media workflow catalog, dependency/status checks, manifest
  controls.
- **Activity**: receipts, jobs, denials, and audit events.
- **Status**: backends, queues, GPU/VRAM, and maintenance modes.
- **Admin**: admin users, admin automation keys, OIDC/config diagnostics.

The current thin dashboard may render several of these on one page, but the
mental model should follow this IA.

## SLVP Policy Choices

The best first product surface is a short set of named choices:

- **Chat only**
  - Stable local chat.
  - No embeddings, media workflows, cloud fallback, native passthrough, or
    admin access.
  - Use for family chat, LibreChat, and basic Open WebUI.
- **Chat + embeddings**
  - Chat plus retrieval/search embeddings.
  - No media workflows, backend operation, or admin access.
  - Use for RAG apps, Continue, local search, and document ingestion.
- **Media workflows**
  - Approved image/audio/video workflows with typed manifest controls.
  - Queue, VRAM, duration, and dependency constraints apply.
  - No native passthrough or admin access by default.
- **Full runtime access**
  - Broad runtime access to local models and workflows.
  - May include stable, opt-in, experimental, batch, and expensive runtime
    capabilities, depending on the gateway policy.
  - Does not grant permission to manage Apps, Profiles, keys, users, or
    gateway settings.
- **Gateway admin automation**
  - Creates an `oak_...` admin automation key, not an `opk_...` App API key.
  - Can manage gateway control-plane resources according to its role.
  - Does not imply model/media runtime access.
- **Advanced native passthrough**
  - A dangerous modifier or advanced template, not a default option.
  - Must scope explicit backends and operations and keep audit on by default.

Do not use vague labels like "All local and stable" in the SLVP UI. They
encode implementation metadata, not an operator decision.

## Main Workflows

### Create an API key for an App

Use when the admin wants to connect LibreChat, Open WebUI, Continue, a bot, an
agent, or another shared service.

Visible fields:

- App or service name
- Runtime access policy:
  - Chat only
  - Chat + embeddings
  - Media workflows
  - Full runtime access

The primary flow should always create the key. Do not ask "create API key
now?" in a form whose purpose is key creation. Do not ask for a key label in
the SLVP flow; derive it from the App name.

Derived fields:

- service account owner
- App name
- Profile id
- Profile label
- protocol list
- intent list
- tags

Do not put generated ids, owner overrides, raw policy JSON, or service-account
language in this first flow. Those controls belong in separate advanced
maintenance tools, not the primary path.

### Create reusable Profile

Use when the admin wants to define a policy that several Apps can share, such
as Chat only, Chat + embeddings, Media workflows, Full runtime access, or a
custom advanced policy.

Visible fields:

- Policy name
- Start from Profile template

Advanced fields:

- generated policy id
- status detail level
- protocol selectors
- tags
- cloud fallback
- native passthrough
- raw JSON override

### Create admin automation key

Use when the admin wants a script to manage Apps, Profiles, keys, status, or
audit data.

Visible fields:

- Automation name
- Admin role: admin or owner

Rules:

- The generated key is an `oak_...` key and should be shown once.
- It is for `/admin/api` control-plane calls, not for OpenAI-compatible
  runtime calls.
- The UI must warn: "Do not paste this into chat apps, SDK tools, browser
  code, prompts, logs, or issue trackers."
- Creating owner automation keys should require an existing owner actor.

### Rotate or revoke keys

Use when a key leaks, a tool is retired, or access needs to be reduced.

The UI should show active keys by App, support instant revoke, preserve history,
and eventually support rotation windows.

### Debug access

The policy simulator is valuable, but it is a diagnostics tool. It should be
named as testing/debugging access, not part of the everyday creation path.

It should help answer:

- Which App/key made the request?
- Which Profile applied?
- What model/workflow/backend was requested?
- Was the request allowed, denied, queued, or failed upstream?
- What changed would allow it?

## Anti-Patterns

Avoid:

- one giant permission matrix as the first experience
- per-key policy sprawl
- OAuth-first UX for self-hosted tools that mostly need API keys
- SaaS billing nouns such as seats, plans, or workspace limits unless they are
  truly part of the product
- hiding routing decisions from operators
- deleting Apps as the main revocation path
- exposing raw ids and JSON in common flows
- making "Access" a durable product noun when it only means a UI preset
- implying that Full runtime access can manage the gateway
- putting admin automation keys into runtime App-key flows
- creating an `oak_...` key without strong one-time-secret and danger copy

## SLVP Bar

The SLVP ideal is not fewer backend concepts. It is fewer concepts in the
operator's immediate path.

Ship:

- task-shaped App API key creation
- separate admin automation key creation
- plain-language reusable Profile creation
- key creation/revocation with one-time key handling
- readable summaries of what each App/Profile can use
- advanced escape hatches for the operator who needs exact control

Do not ship:

- Profile creation that requires handtyping ids, protocols, intents, resource
  tiers, tags, and JSON for normal cases
- service access flows that show duplicated generated names as separate
  required fields
- UI-only abstractions that become a second policy model
- vague labels like "All local and stable"
- any UI that makes `opk_...` and `oak_...` keys feel interchangeable



---

# Source: AUTH_GUIDE.md

# Vivid Fish AI Gateway — Auth Guide

This document explains how Vivid Fish AI Gateway (the project formerly known as
`openai-proxy`) handles authentication for the apps that call it.

The gateway uses **App API keys** that are separate from downstream provider
keys. Each app — LibreChat, Open WebUI, Continue, Cline, a personal chat
client, an agent — authenticates to the gateway with its own `opk_...` key.
The gateway then maps that key to the provider credentials needed for Tabby,
Kobold, TTS, STT, ComfyUI, or any other configured backend.

The legacy term "client" is preserved only inside protocol/SDK compatibility
contexts (the OpenAI Python "client", "client receipts" as an audience tier).
The product entity is an **App**.

## Access model at a glance

```text
User -> owns App -> has Profile -> has Keys -> produces Activity
```

- **User** — a human, typically authenticated via OIDC.
- **App** — an access boundary for a tool, device, agent, or family member.
- **Profile** — the policy bundle attached to an App.
- **Key** — a rotatable bearer secret, usually an App API key (`opk_...`) or
  admin automation key (`oak_...`).
- **Activity** — usage, audit events, receipts, jobs, and errors rendered as
  facts.

Service accounts, credentials, admin roles, audit events, capabilities,
providers, workflows, jobs, receipts, and usage records are still precise
internal facts. Most operators should meet them through App/Profile/Key screens
or Activity views, not as a pile of top-level concepts.

The invariant: every executed request resolves an App + Profile + policy
decision before it runs, and produces exactly one body-free **Receipt**.

## Quick Start For Apps

Run this from the gateway directory:

```bash
cd /home/tnunamak/applications/openai-proxy
./venv/bin/python proxy.py generate-key <app-name>
```

Example:

```bash
./venv/bin/python proxy.py generate-key simon
```

The command prints one App API key:

```text
opk_...
```

Use that value as the OpenAI API key inside the app. Store it in the app's
secret store or environment. Do not put the printed key in source files, logs,
or prompts.

Typical app settings:

```text
Base URL: http://127.0.0.1:5000/v1
API key:  opk_...
```

External apps can use the public canonical host:

```text
Base URL: https://ai.vivid.fish/v1
API key:  opk_...
```

The previous host `https://openai.vivid.fish/v1` remains a compatibility alias
— every route resolves identically — so apps that are already configured
against it do not need to change.

## What The Command Does

`generate-key` creates or rotates an App entry in `proxy_api_keys.yml`.

The file stores only a SHA-256 hash of the App API key:

```yaml
keys:
  - name: simon
    key_hash: sha256:...
    downstream:
      tabby: {}
```

The raw `opk_...` key is shown only once when generated. If it is lost,
generate a new key for the same App name and update that app's configuration.

If the same `<app-name>` already exists, the command rotates that App's API
key while preserving its downstream mapping, Profile block, and audit trail.

`generate-key` is the legacy-compatible App-key path. It remains useful for a
single-user install and for apps already wired to `proxy_api_keys.yml`, but it
is not the control plane. New multi-user/admin workflows should use
`/admin`, `/admin/api/*`, and the durable admin database.

## Admin Control Plane

The control plane is separate from the runtime `/v1` plane.

- App keys (`opk_...`) call `/v1`, `/api`, and `/gateway/v1`.
- Admin keys (`oak_...`) and signed OIDC dashboard sessions call `/admin`,
  `/admin/api/*`, `/status`, and `/v1/status`.
- An App key cannot administer the gateway, and an admin key should not be put
  into OpenAI-compatible apps.

Bootstrap the first service-account admin key from the gateway directory:

```bash
./venv/bin/python proxy.py generate-admin-key owner-cli
```

The command initializes the durable admin store if needed and prints one
`oak_...` key. Use it only for admin-plane requests:

```bash
curl -s https://ai.vivid.fish/admin/api/overview \
  -H "Authorization: Bearer oak_..."
```

OIDC browser login is configured under `admin.oidc` in `proxy_config.yml`.
The browser entry point is `/login`; `/me/login` and `/admin/login` are
compatibility redirects into that route. After login, normal users land on
`/`, while admins can follow the Admin link from the account portal or go
directly to `/admin`. `/me` is a legacy browser alias that redirects to `/`,
never an API-key error page.
Secrets should come from environment variables:

```yaml
admin:
  db_path: data/admin.db
  user_defaults:
    self_service_apps: true
    auto_create_personal_app: true
    default_profile_id: prof_family_default
    role_profile_ids:
      none: prof_family_default
      admin: prof_operator_default
    domain_profile_ids:
      vivid.fish: prof_operator_default
  oidc:
    enabled: true
    issuer: https://idp.example.com
    client_id: vivid-fish-ai-gateway
    client_secret_env: AI_GATEWAY_OIDC_CLIENT_SECRET
    redirect_uri: https://ai.vivid.fish/admin/oidc/callback
    signing_key_env: AI_GATEWAY_ADMIN_SESSION_KEY
    allowed_email_domains: ["vivid.fish"]
    admin_emails: ["owner@vivid.fish"]
```

The first allowed OIDC login becomes `owner` if no owner exists yet. Later
allowed logins get the configured role or no admin role until an owner grants
one.

`admin.user_defaults` is the self-service policy boundary. Users can create
Personal Apps and keys, but those Apps inherit only the Profile selected by the
operator's default/profile mappings. Omit `default_profile_id` to use the
gateway's built-in `default-local` profile.

## Give A Family Member Access

Concrete walkthrough of the canonical "share my gateway with another human"
flow:

1. **OIDC User.** The family member signs in once so the admin store has
   their **User** row. They do not need a global admin role.
2. **Personal App.** The account portal lets them create one or more
   self-service Personal Apps. One human can own several Apps; one App is the
   access boundary for one client, tool, workflow, or script.
3. **Inherited Profile.** By default, the Personal App inherits the user's
   effective Profile. Admins control that default with `admin.user_defaults`
   mappings, and can still attach an explicit App/Profile override when needed.
   For a typical chat-only setup:
   ```yaml
   profile:
     id: aunt-mary-chat
     protocols: ["openai.chat", "openai.audio"]
     fallback:
       cloud_allowed: false
     native_passthrough:
       enabled: false
     status_visibility:
       level: basic
   ```
4. **App API key.** They mint their own bearer token from the account portal.
   They can visit `https://ai.vivid.fish/`, sign in with OIDC, create their
   Personal App if one does not already exist, mint an `opk_...` key, and verify
   the base URL, SDK snippets, and effective capabilities visible to that key.
   Then they paste the same key into their chat app of choice
   (LibreChat, Open WebUI, an iOS shortcut, etc.) with base URL
   `https://ai.vivid.fish/v1`.

Rotation stays per-App: delete the old Credential and create a new key for the
same App while leaving the User, the Profile, and the audit history intact.
Dashboard/default key minting inherits the App's current Profile at runtime;
use a per-key Profile override only when you intentionally want a narrower or
temporary exception.

Signed-in users may create Personal Apps, create API keys for Apps they own,
and delete those keys from the account portal. Those Apps inherit the user's
default Profile unless an admin attaches an explicit App Profile. Self-service
does not let users change ownership, assign Profiles, enable cloud fallback, or
grant themselves new capability classes; those remain admin-plane actions.

## Request Auth Rules

All gateway runtime traffic requires a valid App API key. `/`, `/health`,
`/me`, `/me/api/session`, and the `/.well-known/*` discovery surfaces are public.
`/me` redirects to `/`. The account portal is a shell only: it does not show
private capability, model, or job data until the browser sends either the
signed OIDC session cookie or the user's `opk_...` key to the normal
authenticated runtime APIs. State-changing
`/me/api/*` requests require a valid signed OIDC session and the
`x-ai-gateway-user-action: 1` plus `x-ai-gateway-csrf` headers generated by the
same-origin account page. This includes `POST /me/api/apps`,
`POST /me/api/apps/{app_id}/keys`, `DELETE /me/api/apps/{app_id}`, and
`DELETE /me/api/credentials/{credential_id}`.
`GET /me/api/credentials/{credential_id}/access` uses the signed OIDC session
to inspect an owned key's effective capabilities without revealing the secret.
Admin browser mutations use the same CSRF pattern;
bearer-token automation still authenticates with `Authorization: Bearer oak_...`.
The operator status UI (`/status`) and status JSON (`/v1/status`) require an
admin OIDC session or `oak_...` key, including from loopback. Use `/health` for
public liveness probes.

Accepted auth headers:

```http
Authorization: Bearer opk_...
```

or:

```http
x-api-key: opk_...
```

or:

```http
api-key: opk_...
```

Do not send Tabby admin keys or downstream provider keys as App auth. The
gateway strips inbound auth headers before forwarding requests and injects the
appropriate downstream credentials itself.

## Smoke Test

After generating a key, test that the gateway accepts it. `OPENAI_PROXY_API_KEY`
is the historical env-var name and remains the canonical one for now — new
docs may also reference `AI_GATEWAY_API_KEY` as an alias:

```bash
export OPENAI_PROXY_API_KEY='opk_...'

curl -s http://127.0.0.1:5000/health

curl -s http://127.0.0.1:5000/v1/voices \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Expected behavior:

- `/health` returns `status: healthy` without auth.
- `/v1/voices` returns voices with a valid key.
- `/v1/voices` returns `401` without a key.

The full local test script also expects the key:

```bash
OPENAI_PROXY_API_KEY='opk_...' ./test_e2e.sh
```

## Downstream Credential Mapping

By default, App API keys map to the global downstream provider credentials
configured for the gateway:

- Tabby API key: `TABBY_API_KEY` or Tabby's `api_tokens.yml`
- Tabby admin key: `TABBY_ADMIN_KEY` or Tabby's `api_tokens.yml`
- TTS, STT, ComfyUI, Kobold: no extra auth unless configured per key

Per-App overrides can be added in `proxy_api_keys.yml` when an App needs
different downstream credentials:

```yaml
keys:
  - name: simon
    key_hash: sha256:...
    downstream:
      tabby:
        api_key_env: SIMON_TABBY_API_KEY
        admin_key_env: SIMON_TABBY_ADMIN_KEY
      tts:
        headers:
          Authorization:
            env: SIMON_TTS_API_KEY
            prefix: Bearer
      comfyui:
        headers:
          X-API-Key:
            env: SIMON_COMFY_API_KEY
```

Supported downstream keys:

- `tabby.api_key` or `tabby.api_key_env`
- `tabby.admin_key` or `tabby.admin_key_env`
- `<service>.headers` for arbitrary backend headers

Header values can be literal strings:

```yaml
headers:
  X-API-Key: local-dev-secret
```

or environment-backed values:

```yaml
headers:
  Authorization:
    env: SIMON_TTS_API_KEY
    prefix: Bearer
```

The gateway reloads `proxy_api_keys.yml` when it changes, so adding, rotating,
or deleting an App API key does not require a service restart. Profile blocks
(see below) reload alongside the entries they live on.

## Policy Profiles

A Profile is the authorization and audience boundary attached to an App. In
the durable admin store, Apps point at stored Profiles and App keys inherit
that Profile at runtime. Legacy `proxy_api_keys.yml` entries may also carry an
optional `profile:` block loaded by `gateway.profiles.load_profile_from_entry`.
When mounted in the request path, the Profile decides:

- which protocols the App may use (e.g. `openai.chat`, `openai.images`,
  `openai.audio`, `openai.responses`, `anthropic.messages`, `ollama.chat`,
  `ollama.generate`)
- which workflows, models, and providers the App may target
- whether cloud fallback is allowed
- whether — and where — native passthrough is permitted
- how much status, discovery, and receipt detail the App sees

Missing `profile:` blocks stay backward-compatible: the loader falls back to
`DEFAULT_PROFILE` (local-only, basic visibility), so existing Apps keep
working unchanged.

Allow-lists are conservative by construction:

- omit a list → "no opinion" for that dimension (no restriction)
- set a list to `[]` → "deny all" for that dimension

As the gateway grows, avoid managing Profiles as long lists of model and
workflow IDs. Prefer category selectors backed by capability metadata:

```yaml
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

Then a new stable interactive chat model becomes available to matching Apps
without editing every Profile. Experimental, expensive, native, or admin-power
workflows remain opt-in through their own metadata.

`status_visibility.level` maps an App to a discovery/receipt audience tier.
"Client" here is the protocol/audience tier name (kept for compatibility),
not a product noun — the requester is still an App:

| Level      | Audience       | What the App sees                              |
|------------|----------------|------------------------------------------------|
| `none`     | client         | No receipts; minimal discovery                 |
| `basic`    | client         | Client-tier receipts (request/job IDs, status) |
| `standard` | admin          | Routing facts, fallback reasons                |
| `full`     | internal       | Backend, policy source, manifest hash, diags   |

Example profile block on a key entry:

```yaml
keys:
  - name: image-agent
    key_hash: sha256:...
    profile:
      id: image-agent
      protocols: ["openai.images"]
      workflows: ["image.edit.controlnet.example"]
      models: ["sdxl-base"]
      backends: ["comfyui.local"]
      fallback:
        cloud_allowed: false
      status_visibility:
        level: basic
```

### Native passthrough

`native_passthrough` is opt-in and scoped per profile. It is not a blanket
ComfyUI DSL — it is a privileged lane for specific operations on specific
backends, and every use is recorded in receipts.

```yaml
profile:
  id: comfy-tinkerer
  native_passthrough:
    enabled: true
    allowed_backends: ["comfyui.local"]
    allowed_operations: ["queue_prompt", "history"]
    audit: true
```

### Gateway-owned controls live under extensions

Backend-native power (denoise, controlnet strength, model-specific knobs)
should not be added as global OpenAI fields. It belongs under namespaced
gateway extensions:

```json
{
  "extensions": {
    "com.vividfish.workflow": {
      "denoise": 0.35,
      "controlnet_strength": 1.1
    }
  }
}
```

Unknown extension inputs are rejected with a reason; known-but-unsupported
inputs are recorded as ignored. The gateway never silently drops inputs.

### Templates for current Apps

Safe starter Profiles for the agent/UI/harness Apps this gateway serves today.
Drop these under the matching key entry — they use non-secret placeholders
only. Tighten the allow-lists when an App has a narrow role; leave them
omitted to keep "no opinion" semantics until you need to restrict.

```yaml
# Agent/coding apps (Claude Code-style, Continue, Cline):
- name: simon
  key_hash: sha256:...
  profile:
    id: simon
    # Omit protocols/models to stay permissive; add lists when narrowing.
    fallback:
      cloud_allowed: false
    status_visibility:
      level: standard

# UI apps (LibreChat, Open WebUI):
- name: librechat
  key_hash: sha256:...
  profile:
    id: librechat
    protocols: ["openai.chat", "openai.responses", "openai.audio"]
    fallback:
      cloud_allowed: false
    status_visibility:
      level: basic

- name: open-webui
  key_hash: sha256:...
  profile:
    id: open-webui
    protocols: ["openai.chat", "openai.responses", "openai.audio", "openai.images"]
    fallback:
      cloud_allowed: false
    status_visibility:
      level: basic

# Compatibility harness / test App:
- name: claude-test
  key_hash: sha256:...
  profile:
    id: claude-test
    protocols: ["anthropic.messages"]
    fallback:
      cloud_allowed: false
    status_visibility:
      level: full

# Helper agents (daisy, cap): leave profile omitted to inherit
# DEFAULT_PROFILE (local-only, basic visibility) until a narrower
# boundary is needed.
```

## Rotation And Deletion

Rotate one App's API key:

```bash
./venv/bin/python proxy.py generate-key simon
```

Then update that App to use the newly printed `opk_...` value.

Delete an App's API key:

1. Remove that App's entry from `proxy_api_keys.yml`.
2. Save the file.
3. The gateway will reject that key on subsequent `/v1` requests.

## Common Failures

`401 Missing API key.`

The request reached an authenticated endpoint without
`Authorization: Bearer opk_...` or another accepted auth header.

`401 Invalid API key.`

The presented App API key does not match any hash in `proxy_api_keys.yml`.

`503 Proxy auth is enabled, but no proxy API keys are configured.`

Auth is fail-closed and no usable App API keys are loaded. Generate at least
one.

`502` or `503` backend errors.

The App API key was accepted, but the selected downstream provider was
unavailable or rejected its own backend credentials.



---

# Source: docs/CORE_CONTRACTS.md

# Core Contracts

Date: 2026-05-15
Status: live gateway integration in progress
Product: **Vivid Fish AI Gateway** (project directory still named
`openai-proxy`; canonical host `https://ai.vivid.fish`; alias
`https://openai.vivid.fish` preserved for compatibility).

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
- `gateway/profiles.py` — loader from existing proxy key-entry dicts into
  `AppProfile`, without reading files or resolving secrets. `profile:` blocks
  on key entries hot-reload with the rest of `proxy_api_keys.yml`.
- `gateway/runtime_auth.py` — pure bridge from proxy key-entry dicts to
  `RuntimeIdentity` (name, key hash, downstream config, loaded
  `AppProfile`). Supplies `evaluate_and_apply` and a
  `discovery_audience_for_profile` helper that maps
  `status_visibility.level` → audience tier.
- `gateway/manifests.py` — workflow manifest load/validate plus
  `apply_backend_binding` and receipt-ready `bind_backend_request`. Image
  generation/edit handlers use this binding layer for supported workflow
  extension inputs.
- `gateway/extensions.py` — typed namespaced request-extension validation with
  accepted/rejected/ignored outcomes. The canonical namespace for
  gateway-owned controls is `com.vividfish.workflow`.
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
  `image.generate` jobs from this surface and returns completed image payloads
  at `/gateway/v1/media/jobs/{job_id}/content`.
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
Legacy `proxy_api_keys.yml` entries may also carry optional `profile:` blocks
that hot-reload alongside the entries that carry them. Missing blocks fall
back to `DEFAULT_PROFILE` (local-only, basic visibility) so existing Apps keep
working unchanged.

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
(`extensions.com.vividfish.workflow.*`), not as global OpenAI fields. Native
passthrough remains opt-in, scoped per profile, and is not a blanket ComfyUI
DSL.

OpenAI-native media fields remain top-level even when the local backend needs
adapter work. For example, `/v1/images/edits` receives `image` and `mask` as
standard multipart fields. The route may normalize and bind those assets to a
workflow or apply a documented preservation fallback, but `mask` is not a
workflow extension.

Non-standard conditioning assets remain workflow extensions. For multipart
image edits, an extension value such as
`extensions.com.vividfish.workflow.control_image={"multipart_field":"control_image"}`
may point at an extra uploaded form part. That form part is transport only; the
selected manifest must still declare and bind `control_image` before the route
will apply it.

Generated media may also surface a gateway asset handle. For image outputs,
`data[].asset_id` is a content-addressed `img_<sha256>` handle and
`data[].asset_url` is a signed content URL under
`/v1/assets/{asset_id}/content`. These handles are gateway-native metadata:
OpenAI-compatible fields such as `b64_json` and `url` remain intact, while
agents can reuse `asset_id` where a workflow manifest declares an asset input
or where a route explicitly accepts an input reference, such as video
`input_reference`. Asset IDs are scoped to the authenticated API key that
minted them; signed content URLs still require bearer auth and a valid HMAC
signature.

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



---

# Source: docs/SLVP_SCOPE.md

# SLVP Scope And Extension Shape

Date: 2026-05-15

This document scopes **Vivid Fish AI Gateway** (the product formerly known as
`openai-proxy`) around the top target users without turning one operator's
current machine into the product spec.

Canonical public host: `https://ai.vivid.fish`.
Compatibility alias: `https://openai.vivid.fish` (kept indefinitely so apps
already configured against the legacy host keep working).

## Implementation Status

The extension ladder, manifest layering, and unknown-parameter policy below
describe the implemented first slice plus the remaining target behavior.
Status on `main`:

- Pure manifest validation and binding helpers exist in
  `gateway/manifests.py`.
- Pure typed request-extension validation exists in `gateway/extensions.py`.
- Pure workflow registry with portable input/output summaries exists in
  `gateway/discovery.py`.
- A mounted `/gateway/v1` router exists in `proxy.py` for workflow discovery,
  capabilities, durable receipts, and media jobs.
- A pure capability model exists in `gateway/capabilities.py`.
- Pure app-profile policy module exists in `gateway/policy.py`.
- A pure app-profile loader for existing key-entry config exists in
  `gateway/profiles.py`.
- `/admin` provides the thin operator loop for Apps, Profiles, runtime keys,
  credential revocation, access-issue previews, and policy simulation.
- `/` provides the thin normal-user account portal; `/me` is a legacy browser
  alias that redirects to `/`. Signed-in users can create and delete their own
  Personal Apps, mint/revoke their own App keys, inspect an owned key's
  effective access, paste an `opk_...` key locally for setup help, and copy SDK
  settings. Media jobs remain on the gateway-native `/gateway/v1/media/jobs`
  API rather than being a user-page feed.
- Profile-aware image-route receipts run in
  `/v1/images/generations` and `/v1/images/edits`, recording safe field-name
  accepted/rejected/ignored outcomes, manifest identity, durable persistence,
  and body-free firehose events.
- The OpenAI-compatible image handlers accept a namespaced `extensions`
  container for supported workflow-manifest controls and reject unknown
  top-level gateway-native controls instead of silently dropping them.
- `/gateway/v1/media/jobs` provides the first generic durable media-job API;
  asset serving and backend scheduling are still follow-up work.

## Scope Decision

The core product should optimize for three archetypes:

1. Local agent/coding tool power user
2. Multi-backend GPU tinkerer
3. Local multimodal workflow builder

The common need across all three is:

```text
make existing apps and existing engines work together through one reliable
local endpoint, while preserving enough local control to be useful.
```

The product should not promote every local trick into core. It should provide
clear extension points so local tricks can be expressed, tested, and shared when
they become generally useful.

The anti-micromanagement rule:

```text
Profiles select capability metadata.
Capabilities declare metadata.
Per-model/per-workflow allow-lists are overrides.
```

Adding a model/workflow/service should usually mean editing that thing's
manifest or adapter metadata, not every App or Profile. New stable interactive
chat models can become available automatically to chat Profiles. Experimental,
expensive, native, or admin-power capabilities require opt-in.

Community fit:

- r/selfhosted wants ordinary admin UX: OIDC, App keys, revoke, audit, preview.
- r/LocalLLaMA wants model/backend visibility and OpenAI/Anthropic/Ollama
  compatibility without hiding local power.
- r/StableDiffusion wants named workflows with typed controls and dependency
  checks, not a flattened toy image API.
- The SLVP advisor wants this proven through a few complete flows before
  building a broad RBAC/quota/scheduler UI.

Admin access UX research and prior-art notes live in
`docs/ADMIN_ACCESS_UX_RESEARCH.md`. That note is the reference for why the UI
uses task-shaped grant flows while preserving Apps, Profiles, and Keys as the
durable access model.

## Product Model (Brief)

Detail lives in `docs/CORE_CONTRACTS.md`; the short version the dashboard
should optimize for is:

```text
User -> owns App -> has Profile -> has Keys -> produces Activity
```

Capabilities, Providers, Workflows, Jobs, Receipts, UsageRecords, resource
claims, service accounts, roles, and audit events are still real internal
facts. They should surface when they answer a concrete operator question, not
as concepts every family member or app integrator has to learn.

Invariant: every executed request resolves an App + Profile + policy decision
before it runs, and produces exactly one body-free Receipt. Anything that
violates that invariant is out of scope for the SLVP.

## What Belongs In Core

Core should include:

- public protocol frontends for OpenAI, Anthropic, and Ollama
- backend adapter contract
- workflow manifest schema
- media job/assets API
- access plane: Users, Apps, Profiles, Keys
- route receipts
- app templates, policy Profiles, and compatibility harness
- diagnostics
- resource-aware scheduling boundary
- native passthrough lanes with policy controls

Core should not include:

- every ComfyUI node parameter as a gateway-level concept
- every sampler/controlnet/denoise/upscale setting as a public protocol field
- hardcoded behavior for one operator's workflow names
- backend admin passthrough as a default public surface
- silent acceptance of parameters that do nothing

## Extension Ladder

Use this ladder to decide where a feature belongs.

### 1. Local Manifest Input

Use this for operator-specific workflow controls.

Examples:

- `denoise`
- `controlnet_strength`
- `controlnet_model`
- `preserve_geometry`
- `style_reference_weight`
- custom music generation parameters
- custom video workflow knobs

These live in workflow manifests and map to backend nodes/fields.

### 2. Shared Manifest Convention

Promote to a convention when several workflows use the same concept.

Examples:

- `seed`
- `size`
- `duration_seconds`
- `background`
- `guidance_scale`
- `negative_prompt`
- `strength`

These are still manifest inputs, but the gateway can give them nicer validation,
docs, and app/profile support.

### 3. Canonical Intent Field

Promote to canonical intent only when the concept applies across multiple
backend families and public protocols.

Examples:

- chat messages
- tool calls
- image prompt
- speech voice
- transcription language
- media duration
- output format

### 4. Public Protocol Field

Expose as public protocol compatibility only when a real client protocol already
expects it or the gateway owns the protocol.

Examples:

- OpenAI `response_format`
- Anthropic `tools`
- Ollama `keep_alive`
- gateway media job `parameters`

Gateway-owned protocols should live under `/gateway/v1/*`, not under
vendor-compatible `/v1/*`. This keeps OpenAI-compatible routes clean while still
giving power users a richer API.

## Rule For Weird Local Needs

If a feature is valuable to one local setup but not obviously general:

- support it as a typed manifest input
- validate it
- include it in route receipts
- test that it mutates the backend request
- keep it out of global protocol semantics

That keeps the first user in the product without making the first user's whole
machine the product.

## Example: Image Edit Denoise And ControlNet

An agent observed that passing extra params to `/v1/images/edits` did not change
the output. That matches the current implementation shape: the OpenAI-compatible
edit handler parses standard fields such as:

- `image`
- `mask`
- `prompt`
- `model`
- `size`
- `background`
- `n`
- `response_format`

It then mutates the selected ComfyUI workflow for image upload, prompt, size,
seed/background behavior, and adaptive batching. The current gateway now has a
typed workflow-extension contract for manifest-backed controls. For
`image.edit.flux2-klein-4b`, that includes `denoise`, `steps`, and `guidance`.
It still does not include ControlNet because the local workflow and model
inventory do not contain a real ControlNet path.

The right product answer is not "add arbitrary passthrough params to OpenAI
Images." The right answer is:

1. Create or select a workflow that actually has denoise/controlnet nodes.
2. Describe those controls in a workflow manifest.
3. Accept them through an explicit gateway extension field or media job request.
4. Validate them.
5. Map them through a backend-specific binding.
6. Record them in the route receipt.
7. Test that the backend request changed.

The manifest should not be a universal ComfyUI-flavored DSL. It should have two
layers:

1. **Portable contract:** inputs, outputs, validation, resources, and user-facing
   semantics.
2. **Backend binding:** how this specific backend realizes those semantics.

Example manifest fragment:

```yaml
id: flux2-edit-controlnet
intent: image.edit
backend: comfyui
workflow: workflows/images/flux2-edit-controlnet.json
inputs:
  prompt:
    type: string
    required: true
  denoise:
    type: number
    minimum: 0
    maximum: 1
    default: 0.45
  controlnet_strength:
    type: number
    minimum: 0
    maximum: 2
    default: 0.8
  control_image:
    type: asset
    required: false
outputs:
  image:
    media_type: image/png
bindings:
  comfyui:
    workflow: workflows/images/flux2-edit-controlnet.json
    map:
      denoise:
        - node: "31"
          input: denoise
      controlnet_strength:
        - node: "44"
          input: strength
      control_image:
        - node: "42"
          input: image
```

If the backend were A1111/Forge, the binding would not mention nodes:

```yaml
bindings:
  a1111:
    endpoint: /sdapi/v1/img2img
    body:
      denoising_strength: $inputs.denoise
      alwayson_scripts.controlnet.args[0].weight: $inputs.controlnet_strength
      alwayson_scripts.controlnet.args[0].input_image: $inputs.control_image
```

If the backend were a future purpose-built image-edit API, the same portable
contract could bind to ordinary JSON fields:

```yaml
bindings:
  image_edit_service:
    endpoint: /edit
    body:
      prompt: $inputs.prompt
      denoise: $inputs.denoise
      control_strength: $inputs.controlnet_strength
      control_image_asset: $inputs.control_image
```

OpenAI-compatible request shape should stay compatible. If a client/SDK can
carry extra request body safely, gateway-specific controls should be contained
in a namespaced `extensions` object, for example:

```json
{
  "workflow": "flux2-edit-controlnet",
  "extensions": {
    "com.vividfish.workflow": {
      "denoise": 0.35,
      "controlnet_strength": 1.1
    }
  }
}
```

For multipart image edits, the same concept can be represented as a JSON form
field such as `extensions`. Asset-valued extension controls can reference an
extra multipart field:

```bash
curl -s http://127.0.0.1:5000/v1/images/edits \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F model=flux2-edit-controlnet \
  -F prompt='preserve the geometry, watercolor style' \
  -F image=@input.png \
  -F control_image=@edge-map.png \
  -F 'extensions={"com.vividfish.workflow":{"control_image":{"multipart_field":"control_image"},"controlnet_strength":1.1}}'
```

The form field `control_image` is upload transport. The portable control remains
`extensions.com.vividfish.workflow.control_image`, and it only works when the
resolved manifest declares and binds that asset input.

For gateway-owned media jobs, use a cleaner native shape:

```json
{
  "intent": "image.edit",
  "workflow": "flux2-edit-controlnet",
  "input": {
    "prompt": "preserve the geometry, watercolor style",
    "image": "asset_123"
  },
  "parameters": {
    "denoise": 0.35,
    "controlnet_strength": 1.1
  }
}
```

## Proving The Feature

Do not prove this by eyeballing generated images.

Prove it with a fake ComfyUI backend and manifest contract tests:

1. Submit an image-edit request with `extensions.com.vividfish.workflow.denoise=0.35`, or a gateway media job with `parameters.denoise=0.35`.
2. Capture the backend request produced by the adapter.
3. Assert the configured backend binding changed to `0.35`.
4. Assert the route receipt lists `denoise` as accepted.
5. Submit an unknown input.
6. Assert the gateway rejects it or records it as ignored according to policy.
7. Submit the same request through an OpenAI-compatible client.
8. Assert compatibility fields still work.

Optional visual regression tests can be added later, but they should not be the
only correctness oracle.

## Unknown Parameter Policy

Silent no-ops are bad product behavior.

For public protocol frontends:

- accept protocol-defined fields
- accept documented gateway extension containers
- reject unknown top-level gateway extension inputs
- record accepted/ignored/rejected inputs in route receipts

For native passthrough lanes:

- allow backend-native shapes when the key has passthrough scope
- label the request as non-portable
- still emit route receipts

## Promotion Criteria

Promote a local extension toward core only when at least two are true:

- two or more users need it
- two or more backend families expose the same concept
- a target app/client already has a field for it
- it can be tested without subjective output judgment
- it affects routing, scheduling, or safety
- it is needed for the SLVP compatibility matrix

Under this rule, denoise/controlnet probably starts as a workflow-manifest
extension. It may later become a shared image-edit convention, but it should not
be a global public protocol promise on day one.

See `docs/CONDITIONING_CONTROLS.md` for the concrete ControlNet/conditioning
readiness gate and naming convention.



---

# Source: docs/SELF_HOSTED_APP_QUIRKS_AUDIT.md

# Self-Hosted App Quirks Audit

Date: 2026-05-15

This audit looks at the currently running/configured local stack and asks what architectural questions it imposes on the gateway design.

## Observed Stack

Configured in `proxy_config.yml`:

- `openai-proxy` on `127.0.0.1:5000`
- `llama-turbo / kobold` on `127.0.0.1:5051`
- standby `TabbyAPI (Qwen3-VL)` on `127.0.0.1:5050`
- `voxtral-int4-tts` on `127.0.0.1:8001`
- `parakeet-stt` on `127.0.0.1:5092`
- `ComfyUI` on `127.0.0.1:7890`

Live services:

- `openai-proxy.service` active
- `llama-turbo.service` active, serving `Qwen3.6-27B-UD-Q4_K_XL.gguf` with mmproj vision support and 131k context
- `comfyui.service` active with `--reserve-vram 9 --lowvram`
- `voxtral-int4-tts.service` active
- `parakeet-stt.service` active
- `tabbyapi.service` inactive/dead
- `koboldcpp.service` inactive/dead
- PDPP containers on port `3000`
- Playwright MCP on port `3100`
- other Node/Next services on `8000` and `3001`

Workflow inventory:

- 5 image workflows
- 3 audio/music workflows: ACE-Step and TangoFlux
- 6 video workflows: LTX and Wan variants

Proxy status observations:

- Health is `warn`, not `ok`: disk `/media/windows` 89.1% full, CPU temp warning, recent GPU Xid events, stale Daisy audit.
- Two RTX 3090 GPUs.
- GPU0 carries desktop, TTS, and ComfyUI; GPU1 carries llama-server.
- TTS is over its configured VRAM budget: about 5.86 GiB used against a 4 GiB budget.
- ComfyUI is connected via websocket and queue is idle.
- TabbyAPI is down but expected as standby.
- `daisy` key is active and high-traffic; anonymous unauthenticated traffic is hitting random WordPress/PHP exploit paths.
- `/v1/status` exposes rich machine state and therefore belongs behind the admin auth path.

## Architecture Questions Raised

### 1. Expected-Down Is Not Failure

TabbyAPI is configured as a standby vision backend and is down at idle. A naive health system would mark the gateway unhealthy, but this is expected.

Architecture requirement:

- backend lifecycle state must be explicit: `required`, `optional`, `standby`, `cold`, `externally_managed`, `degraded`, `failed`
- readiness should distinguish "can serve now" from "can serve after cold start"
- route selection should know cold-start latency and whether it may start a backend

Open question:

- Should the router ever choose a cold standby backend automatically, or only when a route explicitly allows cold start?

### 2. Public Protocol Compatibility And Backend Management Are Separate

`llama-server`, TabbyAPI, LM Studio, Ollama, and others may all expose OpenAI-compatible endpoints, but model management and health are backend-native.

Architecture requirement:

- keep an OpenAI-compatible baseline adapter for inference
- add native management adapters for lifecycle, health, model state, slots, LoRA, pull/download, load/unload
- do not pretend `/v1/models` is enough to describe local state

Open question:

- Should backend-native management APIs be exposed publicly through a normalized model-management protocol, or remain internal/admin-only?

### 3. GPU Placement Is A First-Class Constraint

The current stack is already manually partitioned:

- GPU0: desktop, Voxtral TTS, ComfyUI
- GPU1: llama-server

This is more specific than "the machine has VRAM."

Architecture requirement:

- resource claims must include GPU identity, not just total VRAM
- scheduler must model service reservations and headroom per GPU
- runtime observations must be able to override static budgets
- over-budget services should produce warnings and influence scheduling

Open question:

- Should scheduling be advisory first, or should it actively block jobs when a service exceeds budget?

### 4. Warm/Cold Resource Estimates Are Different

The status page already tracks ComfyUI VRAM estimates keyed by workflow, size, background-removal state, and warm model state.

Architecture requirement:

- resource estimates need keys for workflow, model, size, batch, backend version, GPU, low-vram mode, and warm/cold state
- estimates need trust metadata and sample counts
- scheduler should surface "estimate missing" separately from "not enough VRAM"

Open question:

- Should new workflows require a calibration run before they are exposed to shared clients?

### 5. ComfyUI Is A Workflow Runtime, Not A Model Endpoint

This machine uses ComfyUI for image, video, audio/music workflows. The workflow itself is the product unit, not the backend process.

Architecture requirement:

- workflow manifests must be first-class
- manifests must validate custom nodes, model refs, inputs, outputs, and resource estimates
- manifests must cover image, video, audio, music, SFX, upscaling, inpainting, and edits
- API graph JSON and UI graph JSON need drift checks

Open question:

- Should workflow manifests live with the gateway, with the ComfyUI install, or in independent workflow packages?

### 6. Audio Is Not Just TTS/STT

The stack has OpenAI-compatible TTS/STT services and ComfyUI audio workflows for ACE-Step and TangoFlux. Current capability language often collapses this into `audio`.

Architecture requirement:

- distinguish `speech.synthesize`, `speech.transcribe`, `audio.translate`, `audio.generate`, `sound.generate`, and `music.generate`
- route TTS/STT differently from long-running music/SFX generation
- support both OpenAI Audio compatibility and gateway job/workflow APIs

Open question:

- Should `/v1/audio/generate` remain a gateway-specific endpoint, or move under a generic media job API?

### 7. Long-Running Jobs Need Durable Metadata

Video jobs, audio generation, image generation, and long TTS can take tens of seconds to minutes. Current status shows at least one succeeded video job with null alias/workflow metadata.

Architecture requirement:

- job records should include alias, workflow, input hash, key identity, route receipt, resource claims, timings, output asset IDs, and error details
- job state should survive process restarts where possible
- cancellation and idempotency should be part of the job contract

Open question:

- Should all media requests become jobs internally, even when the public API presents a synchronous OpenAI-shaped response?

### 8. Status And Diagnostics Are Sensitive

`/v1/status` exposes backend URLs, model paths, GPU state, key names, disk warnings, service state, recent usage, and workflow metadata. Anonymous scanners are already hitting the public proxy port with exploit paths.

Architecture requirement:

- status endpoints need visibility levels: public health, authenticated user diagnostics, admin diagnostics
- key names, local paths, model paths, process IDs, GPU UUIDs, and recent request metadata need redaction by audience
- all nontrivial status/diagnostic endpoints should share the proxy auth plane
- public bind on `0.0.0.0` should be treated as an internet-exposed deployment risk

Open question:

- Should the default shipped config bind to localhost and require an explicit opt-in for LAN/public exposure?

### 9. Per-Key Behavior Is A Product Primitive

The status page already tracks per-key traffic. `daisy` is active, while keys for `open-webui`, `librechat`, and others exist but are inactive. Anonymous 4xx traffic is visible.

Architecture requirement:

- per-key scopes should control allowed protocols, intents, backends, models, cloud fallback, and native passthrough
- per-key usage should include tokens, media jobs, GPU time, bytes, route choices, and errors
- keys need app identity, not just bearer auth

Open question:

- Should generated compatibility smoke tests create dedicated keys so failures can be attributed per app?

### 10. Route Receipts Are Currently Incomplete

Recent events show successful chat/image events but often lack resolved model metadata.

Architecture requirement:

- every request should produce a route receipt
- receipt should include protocol, intent, requested model, resolved model, backend, workflow, scheduler queue, resource claims, and fallback reasons
- receipts should be available in logs and optionally response headers/debug endpoints

Open question:

- Should clients be able to request a route receipt in-band, or should it remain admin-only?

### 11. Externally Managed Services Are Normal

Most configured backends are `managed: false`, but the system still has user-level systemd units. The gateway lives beside external process managers rather than owning every process.

Architecture requirement:

- lifecycle provider should be pluggable: none, systemd user, Docker, compose, process command, remote URL
- adapters should expose what lifecycle operations are available
- scheduler should not assume it may restart a service

Open question:

- Should "managed by gateway" be a rare advanced mode rather than the default?

### 12. Native Low-Level Flags Matter

ComfyUI runs with `--lowvram --reserve-vram 9`; llama-server runs with `--ctx-size 131072`, `--jinja`, mmproj, and custom cache types. These flags materially change capabilities and scheduling.

Architecture requirement:

- backend probes should capture effective runtime config
- compatibility matrix should include backend runtime flags/version where relevant
- routing should avoid stale static config when runtime probes disagree

Open question:

- Should the gateway maintain an operator-reviewed "effective capability registry" generated from probes plus manual overrides?

### 13. Operational Incidents Should Influence Routing

The status page reports CPU thermal warnings, disk pressure, and recent GPU Xid events. These are not just dashboard facts; they affect whether long media jobs should start.

Architecture requirement:

- scheduler should ingest operational health signals
- policies should support "pause video jobs when disk is above threshold" or "avoid GPU after Xid events"
- disk space should be a resource claim for media jobs

Open question:

- Which health warnings should be advisory, and which should block new work?

## Changes This Suggests For The Architecture

The architecture already points in the right direction, but this audit suggests several additions should be promoted:

1. Add explicit backend lifecycle states.
2. Add admin/status visibility tiers and redaction.
3. Add a model/resource management intent family: `model.show`, `model.running`, `model.pull`, `model.load`, `model.unload`.
4. Add richer media intents: `audio.generate`, `sound.generate`, `music.generate`.
5. Make workflow manifests mandatory for ComfyUI-style backends.
6. Make route receipts a core event/output type.
7. Treat operational health as scheduler input.
8. Model GPU identity and disk space as resources.
9. Separate OpenAI-compatible inference adapters from native management adapters.
10. Treat app-specific compatibility keys as first-class test fixtures.

## Deeper Implementation Findings

The first pass looked mostly at the running services, config, and status surface. A deeper pass through the current implementation adds several more architecture constraints.

### 14. Middleware Metrics Are Best-Effort, Not A Source Of Truth

The request counter middleware records path, status, latency, and a best-effort key name without reading request bodies. It mirrors data into SQLite samples and `firehose_metrics`, but the event model is path-oriented and synthesized after the handler returns.

Architecture requirement:

- normalized events should be emitted inside the request pipeline, not inferred only from FastAPI middleware
- every intent execution should produce a route receipt independent of HTTP path
- request accounting should distinguish protocol path, intent, backend, model, workflow, and job ID
- metrics sinks should be pluggable and durable enough for multi-worker later

Open question:

- Should route receipts be generated by the router before execution and finalized by the backend adapter, rather than by handlers/middleware?

### 15. Current Activity Persistence Assumes One Worker

`firehose_metrics` explicitly assumes a single proxy process. It keeps counters in memory and replays a 24h append-only `cache/key_activity.jsonl` tail on startup. Responses use SQLite with WAL and 7-day retention, but request activity and recent events are not a general durable event log.

Architecture requirement:

- event persistence should be designed now as single-writer/simple, but with a clear upgrade path to SQLite/Postgres/Redis
- in-memory progress and metrics should be caches over durable facts where possible
- multi-worker deployment should be a declared unsupported mode until event/progress state is centralized

Open question:

- Is a local SQLite event log sufficient for the self-hosted product, or should the architecture abstract event storage from the start?

### 16. Catch-All Passthrough Is Useful And Dangerous

The current proxy includes explicit route handlers plus a catch-all `/{path:path}` that forwards other methods and paths to the LLM backend. This is useful for backend admin/model-load endpoints, but it blurs the product boundary.

Architecture requirement:

- native passthrough lanes should be explicit and scoped
- per-key policy should decide whether native passthrough is allowed
- passthrough should be visible in route receipts and logs
- public protocol frontends should not accidentally expose backend admin APIs

Open question:

- Should backend-native passthrough live under namespaced paths such as `/providers/tabby/*`, rather than the root catch-all?

### 17. Responses API Is A Stateful Translation Layer

`/v1/responses` translates into Chat Completions and persists stateful chains in SQLite when `store=true` or previous response chains are used. Hosted tools are intentionally rejected because there is no local hosted-tool surface.

Architecture requirement:

- protocol frontends may need state stores independent of backend execution
- state stores need retention, ownership, key scoping, and deletion semantics
- unsupported hosted tools should be protocol-shaped errors, not silent no-ops
- prior-response expansion is a protocol concern, not a backend concern

Open question:

- Should all protocol-specific state live in frontend-owned stores, or should the gateway provide a shared conversation/session store?

### 18. Video Progress Is Partly Volatile

Video jobs use ComfyUI prompt IDs and keep realtime progress in memory from a single websocket listener. Polling can fall back to ComfyUI `/history`, and a reconciliation loop reads recent history, but the implementation notes that proxy state is stateless across restarts and clients are expected to keep IDs.

Architecture requirement:

- generic job state should have durable submission records
- realtime progress can be cached, but terminal job metadata should persist
- job IDs should not rely entirely on backend process history lifetime
- reconnect/reconcile behavior should be a backend adapter contract

Open question:

- What is the minimum durable job record: submitted request, workflow alias, backend prompt ID, key identity, status, output assets, and errors?

### 19. Audio Generation Already Uses The Media Job Pattern

The repo has OpenAI-compatible speech/transcription plus a gateway-specific `/v1/audio/generate` for ACE-Step and TangoFlux through ComfyUI. This confirms that `audio` is not one capability.

Architecture requirement:

- keep OpenAI Audio for speech/transcription compatibility
- route music/SFX through a generic media workflow/job API
- expose model/workflow listing for audio generation separately from TTS voices
- usage accounting should measure duration, generated seconds, and job runtime, not only tokens

Open question:

- Should the public endpoint remain `/v1/audio/generate` as a convenience alias over `/v1/media/jobs`?

### 20. Service Lifecycle Is Already Mixed

The code can start systemd user services for backend switching, yet the config says most backends are `managed: false`, and live services are externally managed. Current code has specific knowledge of Tabby/Kobold service names and systemd behavior.

Architecture requirement:

- lifecycle management should be an adapter/provider, not hardcoded in routing
- supported lifecycle providers should include `none`, `systemd_user`, `docker`, `compose`, `process`, and `remote`
- router/scheduler should ask lifecycle providers what operations are allowed
- start/stop actions need per-key/admin policy controls

Open question:

- Should automatic backend startup be limited to admin-owned requests, or can ordinary client requests trigger cold starts when policy allows it?

### 21. Health Is Richer Than Liveness

The status surface includes health, aux health, queues, workflow drift, budgets, image cache, GPU attribution, thermal warnings, disk pressure, Xid events, and repo state. This is more than liveness and should influence routing.

Architecture requirement:

- separate liveness, readiness, capability, capacity, and safety health
- scheduler should consume capacity/safety health, not just backend up/down
- diagnostics should be queryable by scope: user, app, admin, operator

Open question:

- Which safety signals block new jobs by default: disk pressure, thermal warnings, Xid events, backend over budget, workflow drift?

### 22. Runtime Config Beats Static Config

Runtime probes reveal concrete flags and state: llama-server uses 131k context, mmproj, `--jinja`, and custom KV cache; ComfyUI runs with `--lowvram` and `--reserve-vram 9`; TTS has its own flow-step setting. Static YAML cannot fully describe this.

Architecture requirement:

- adapters need runtime probes and effective config snapshots
- route selection should prefer probed capability over declared capability when they differ
- compatibility reports should include backend version and effective flags
- manual overrides should exist for cases probes cannot infer

Open question:

- Should a backend be considered routable only after probe results have been normalized into an effective capability document?

### 23. Status Exposure Needs A Security Model

The service file notes external exposure through `openai.vivid.fish` via Traefik. The live status surface includes local paths, GPU UUIDs, process state, key names, usage counts, recent events, and operational warnings. Anonymous exploit traffic is already visible.

Architecture requirement:

- split status into `/healthz` public liveness, authenticated user diagnostics, and admin/operator diagnostics
- redact paths, key names, process IDs, GPU UUIDs, and recent events unless admin
- include an explicit `exposure_mode`: localhost, LAN, public
- warn loudly when sensitive diagnostics are exposed on public bindings

Open question:

- Should the default dashboard/status page be disabled unless an admin key or localhost access is present?

### 24. Workflow Drift Is Already A First-Class Safety Check

The repo has API workflow JSON, UI workflow JSON, and a drift check script. The status page reports drift. This is exactly the kind of local workflow portability/safety issue a future architecture must preserve.

Architecture requirement:

- workflow packages should include API graph, UI graph, manifest, dependency list, and drift hash
- drift should affect readiness for that workflow
- clients should get actionable workflow-not-ready errors

Open question:

- Should workflow drift block only that workflow, or mark the whole ComfyUI backend degraded?

### 25. Compatibility Keys Should Be Modeled As Apps

The current key registry already has named keys such as `open-webui`, `librechat`, `claude-test`, and `daisy`. This points to app-specific behavior and test fixtures.

Architecture requirement:

- `AuthIdentity` should include app/client type, not only name
- scopes should support protocol families and passthrough rights
- compatibility harness should provision dedicated keys and verify per-key routing/usage
- app-specific quirks should be expressed as policy/config, not hardcoded handler logic

Open question:

- Should the gateway ship app profiles for Open WebUI, LibreChat, SillyTavern, Continue, Cline, Claude Code, and n8n?



---

# Source: docs/OWNER_EXECUTION_PLAN.md

# Owner Execution Plan

Date: 2026-05-15
Status: working plan

This plan turns the architecture into a sequence of small, provable steps. The
goal is to keep the project broad enough for real self-hosted stacks without
letting one operator's current machine become the core product.

## Implemented So Far

The status of each build-order item below reflects what currently lives on
`main`. Anything not listed here is still aspirational.

- [x] Pure app-profile policy module (`gateway/policy.py`, `tests/test_policy.py`)
- [x] Pure app-profile loader for existing key-entry config (`gateway/profiles.py`, `tests/test_profiles.py`)
- [x] Pure runtime-auth/profile bridge (`gateway/runtime_auth.py`,
      `tests/test_runtime_auth.py`) — `RuntimeIdentity`, identity registry,
      policy bridge, profile→audience helper
- [x] Pure workflow registry with audience-aware summaries (`gateway/discovery.py`, `tests/test_workflow_discovery_registry.py`)
- [x] Workflow manifest parsing/validation/binding with receipt-ready binding provenance (`gateway/manifests.py`)
- [x] Typed request-extension validation contract (`gateway/extensions.py`, `tests/test_extensions.py`)
- [x] Image-extension/manifest-binding layer (`gateway/image_extensions.py`)
- [x] Pure capability model and policy-aware summaries (`gateway/capabilities.py`, `tests/test_capabilities.py`)
- [x] Pure Anthropic and Ollama protocol-frontend translators (`gateway/protocols/`)
- [x] Workflow discovery, capability, receipt, and media-job router (`gateway/api.py`, `tests/test_gateway_api.py`, `tests/test_gateway_media_api.py`)
- [x] Route receipt data model (`gateway/receipts.py`)
- [x] Durable SQLite receipt store (`gateway/receipt_store.py`, `tests/test_receipt_store.py`)
- [x] Gateway media-job model and durable store (`gateway/media_jobs.py`)
- [x] Image-route receipts wired into `/v1/images/generations` and `/v1/images/edits` with profile-aware policy decisions, safe field-name outcomes, manifest identity, durable persistence, and body-free firehose events
- [x] Live `/gateway/v1/*` mount in `proxy.py` for workflows, capabilities, receipts, and media jobs
- [x] Durable `image.generate` media-job execution through `POST /gateway/v1/media/jobs` with retry, idempotency, persisted job state, and completed image content retrieval
- [x] Live non-streaming Anthropic Messages and Ollama chat/generate/tags frontends over the existing chat route
- [x] Live image workflow extension binding for supported manifest controls
- [x] `generate-key` preserves existing `profile:` blocks on rotation
- [x] SDK compatibility harness with executable registered probes (`scripts/compat/`, `tests/compat/`, `docs/SDK_COMPATIBILITY_HARNESS.md`)

In-progress / next integration targets:

- Expand the live capability inventory beyond the initial hand-built catalog.
- Extend durable media-job execution to video, audio/music, and generic gateway assets.
- Add streaming protocol-frontend coverage where real clients need it.
- Backend adapter contract extracted from `proxy.py`.

## North Star

```text
One reliable local endpoint that makes a messy self-hosted AI stack feel
coherent without hiding what actually happened.
```

## Product Shape

The gateway has three surfaces:

1. **Compatibility surfaces**
   Vendor/client-shaped APIs that real SDKs and apps can target.

2. **Gateway-native power surface**
   `/gateway/v1/*` for discovery, diagnostics, media jobs, assets, workflows,
   and controlled native passthrough.

3. **Local extension surface**
   Workflow manifests, backend adapters, app profiles, and policy files that
   can be hacked locally and promoted only when they prove general.

## Core Archetypes

### Local Agent/Coding Power User

Must work:

- OpenAI Chat/Responses enough for Open WebUI, Continue, Cline/OpenCode
- Anthropic Messages enough for Claude Code-style clients
- tool-call and streaming behavior
- route receipts
- model/capability discovery

### Multi-Backend GPU Tinkerer

Must work:

- adapters expose capability, runtime config, health, and resource facts
- model lifecycle state is visible
- native management lanes exist but are scoped
- diagnostics explain why routing failed

### Local Multimodal Workflow Builder

Must work:

- OpenAI Images/Audio for simple app integration
- `/gateway/v1/media/jobs` for advanced media workflows
- workflow manifests with portable contract + backend binding
- durable job/assets model
- ComfyUI remains an authoring/runtime engine, not the public protocol

## First Slice

The first slice should not add another endpoint just to add endpoints. It should
prove the architecture can carry one advanced local feature without breaking
compatibility.

Recommended first slice:

```text
Typed workflow extension for image edit denoise/controlnet,
proved against a fake ComfyUI backend,
with route receipt output.
```

Why this slice:

- it comes from a real agent/user need
- it exposes the current silent-noop failure mode
- it tests manifest semantics without requiring a full scheduler rewrite
- it proves portable contract vs backend binding
- it keeps OpenAI Images compatibility separate from gateway-native power use

Success criteria:

- unknown extension parameters are not silently dropped
- accepted parameters are schema-validated
- backend request mutation is proven by test
- route receipt records accepted/rejected extension inputs
- route receipt records policy decision and client/admin/internal visibility
- manifest identity includes version, trust state, and content hash
- current `/v1/images/edits` behavior remains compatible

## Build Order

### 1. Route Receipts — live first slice done

Add a small route receipt data model first. Do not wait for the full event
system.

Minimum receipt fields:

- request ID
- protocol
- path
- intent
- auth identity name
- app profile
- policy decision and policy source
- requested model/workflow
- resolved model/workflow
- backend
- manifest ID/version/hash when applicable
- job ID/parent request ID when applicable
- accepted extensions
- rejected extensions
- warnings
- visibility audience

Initial delivery:

- [x] pure Python model/helper (`gateway/receipts.py`)
- [x] unit tests
- [x] internal/admin/client redaction behavior (`RouteReceipt.to_dict(...)` and
      `gateway/receipt_store.py` audience filtering)
- [x] attach profile-aware receipts internally to image generation/edit paths
      with safe field-name accepted/rejected/ignored outcomes
- [x] persist receipts durably and expose audience-filtered views under
      `/gateway/v1/receipts`
- [x] body-free debug event via
      `firehose_metrics.record_event("image_route_receipt", ...)`

### 2. Manifest Contract — live first slice done

Add pure manifest parsing/validation.

Minimum concepts:

- manifest ID
- intent
- backend
- inputs with JSON Schema-ish validation
- outputs
- backend bindings

Initial delivery:

- [x] fake binding mutation tests (`gateway/manifests.py` exposes
      `apply_backend_binding` and `bind_backend_request`)
- [x] live image generation/edit handlers call the manifest binding layer for
      supported namespaced controls
- [x] sample image edit and image generation manifests committed under
      `manifests/workflows/`

### 3. Fake Backend Proof — done for image extension binding

Build a fake ComfyUI-like request capture.

Initial delivery:

- [x] tests submit namespaced workflow extensions
- [x] adapter applies binding to backend request object
- [x] tests assert exact mutation
- [x] tests assert unknown extension rejection

### 3.5. Live Integration Points — live first slice done

Only after the pure modules stay green, thread them into the existing proxy at
the narrowest points:

- [x] `images_generations` and `images_edits` for handler-level image receipts
      after auth succeeds (profile-aware, post-auth, safe field-name capture)
- [ ] `_build_image_response_payload` for final response metadata
- [ ] `load_comfy_workflow` for preflight validation of workflow shape and
      manifest drift
- [x] `proxy_comfy_request` and `proxy_comfy_edit_request` for applying
      manifest bindings where node mutation already happens
- [x] `_firehose_request_counter` only for coarse request counters, not receipt
      truth, because middleware should not read request bodies
- [ ] status capability builders as source material for curated `/gateway/v1`
      discovery, not as a raw payload to expose

Do not spread policy checks through handlers. Resolve profile/policy once, pass
the decision into receipt and routing helpers, and keep client/admin/internal
visibility decisions explicit.

### 4. `/gateway/v1` Namespace Skeleton — live first slice done

`gateway/api.py` provides workflow discovery, capability, receipt, and media
job endpoints. `proxy.py` mounts the namespace behind proxy-key auth.

Initial endpoints:

- [x] `GET /gateway/v1/capabilities`
- [x] `GET /gateway/v1/workflows`
- [x] `GET /gateway/v1/workflows/{id}`
- [x] `GET /gateway/v1/receipts`
- [x] `GET /gateway/v1/receipts/{request_id}`
- [x] `POST /gateway/v1/media/jobs`
- [x] `GET /gateway/v1/media/jobs`
- [x] `GET /gateway/v1/media/jobs/{job_id}`
- [x] `POST /gateway/v1/media/jobs/{job_id}/events`
- [x] `POST /gateway/v1/media/jobs/{job_id}/cancel`

### 5. Media Jobs — storage/API first slice done

- [x] durable job record
- [x] audience-filtered API surface
- [x] image job content retrieval through `/gateway/v1/media/jobs/{job_id}/content`
- [x] ComfyUI image-generation adapter integration for long-running jobs
- [ ] generic `/gateway/v1/assets/*` serving
- [ ] ComfyUI video/audio adapter integration for long-running jobs

### 6. SDK/App Harness — executable harness landed

A harness scaffold lives under `scripts/compat/` and `tests/compat/`, with
documentation in `docs/SDK_COMPATIBILITY_HARNESS.md`. The matrix runner now
executes registered unittest probes when SDK and environment preconditions are
present. Treat current matrix output as infrastructure validation until live
compatibility claims are explicitly published.

Start with smoke tests, not full integration complexity.

Initial targets:

- OpenAI Python against `/v1/chat/completions`
- OpenAI Python against `/v1/images/edits`
- Anthropic Python against `/v1/messages`
- Ollama clients against `/api/chat`, `/api/generate`, and `/api/tags`

## Guardrails

- Do not add new public compatibility claims without tests.
- Do not add arbitrary top-level request parameters.
- Do not let `/v1/*` become gateway-native.
- Do not make ComfyUI node IDs part of portable semantics.
- Do not make native passthrough available to ordinary keys by default.
- Do not start scheduler enforcement before route receipts exist.
- Do not turn local workflows into core product requirements unless they repeat.

## Promotion Policy

Local extension -> shared feature only when at least two are true:

- two users need it
- two backend families support it
- a target client already has a field for it
- it can be proven without subjective output judgment
- it affects safety, routing, or scheduling
- it is needed for the compatibility matrix

## Immediate Decisions

1. Use `/gateway/v1/*` for gateway-native surfaces.
2. Keep OpenAI `/v1/*` compatibility clean.
3. Use JSON Schema-style validation for extensions/manifests.
4. Treat ComfyUI bindings as backend-specific, not a universal DSL.
5. Build route receipts before broad scheduler work.
6. Make the first proof slice image-edit extension binding.

## Deferred

- full plugin loading
- multi-worker event storage
- hosted provider marketplace behavior
- full Gemini-native protocol
- full ElevenLabs-compatible protocol
- scheduler preemption
- UI/dashboard redesign
- broad app-profile installers



---

# Source: docs/API_TARGETS.md

# API And SDK Target Inventory

Date: 2026-05-15

This file tracks the API surfaces this gateway should care about. It separates public client-facing compatibility from backend adapter support.

The product goal is broad backend coverage without pretending every API is equally supported. A second user with a different pile of engines should be able to add adapters and workflow manifests without changing the core protocol/router/scheduler model.

## Compatibility Tiers

```text
Tier A: SDK-compatible
Real SDK/client tests pass for the documented subset.

Tier B: useful subset
Common requests work, and unsupported features return protocol-shaped errors.

Tier C: explicit native/passthrough
Useful backend-native operations are exposed without broad compatibility claims.

Adapter-only
The gateway calls this API behind the scenes, but does not expose it as a public client protocol.

Deferred
Known surface, not worth first-class support yet.
```

## Client-Facing Protocols

| Priority | Surface | Initial Tier | Why It Matters |
|---|---|---:|---|
| P0 | OpenAI Chat Completions | A/B | Local tooling lingua franca for Open WebUI, LibreChat, Continue, Cline-like agents, vLLM, llama.cpp, LM Studio, TabbyAPI, TGI, OpenRouter-style clients. |
| P0 | OpenAI Responses | B | Strategic OpenAI-native agent surface; this repo already has a translator/store. |
| P0 | OpenAI Images | B | Familiar app API for image generation/editing while hiding ComfyUI/A1111 workflow details. |
| P0 | OpenAI Audio | B | Familiar API for TTS/STT while routing to local or cloud voice engines. |
| P0 | OpenAI Embeddings | B | Common vector/search integration surface. |
| P0 | Anthropic Messages | A/B | Important for Claude-shaped coding agents and tool-use semantics. |
| P0 | Ollama native API | B/C | Self-hosters treat Ollama as a first-class local protocol, especially for model management. |
| P1 | Gateway job/assets API | A | Needed for video, music, sound effects, long image jobs, durable polling, cancellation, and assets. |
| P1 | ElevenLabs-compatible speech | B | Useful if voice agents and TTS clients become a real wedge. |
| P1 | Gemini GenerateContent | B | Useful for Gemini-native apps and cloud fallback, but not first wedge. |
| P1 | KoboldAI-compatible API | B/C | Relevant for SillyTavern/roleplay users where native sampler controls matter. |
| P2 | OpenRouter-specific extensions | C | Mostly OpenAI-compatible; support provider metadata/routing when needed. |
| P2 | A1111/Forge-compatible image API | C | Useful for SD clients, but more valuable initially as backend adapter. |
| P2 | ComfyUI native API passthrough | C | Useful for power users, but raw graph/queue API should not be the primary public contract. |
| Defer | Bedrock/Vertex full native APIs | Deferred | Hosted enterprise surfaces; too broad for the local-first wedge. |
| Defer | Fine-tuning/training/admin APIs | Deferred | Not part of the gateway's first job. |

## Backend Adapter Targets

| Priority | Backend API | Adapter Role |
|---|---|---|
| P0 | Ollama | Chat, generate, embeddings, model list/show, load/unload/ps semantics where possible. |
| P0 | llama.cpp server | OpenAI-compatible chat/completions/embeddings, multimodal/tool support where available, local model metadata. |
| P0 | vLLM OpenAI server | High-throughput chat/completions/embeddings, streaming, tool support, model metadata. |
| P0 | LM Studio local server | Desktop local-AI endpoint, OpenAI-compatible and any documented Anthropic-compatible behavior. |
| P0 | TabbyAPI | EXL2/GPTQ/FP16 local backend, model load/unload/admin API, OpenAI-compatible chat. |
| P0 | KoboldCpp/KoboldAI | OpenAI-compatible plus native Kobold parameters for SillyTavern-style use. |
| P0 | ComfyUI | Image/video/audio workflow execution, queue/history/websocket progress, upload/download/assets. |
| P0 | local TTS engines | Speech synthesis behind OpenAI/ElevenLabs/gateway voice APIs. |
| P0 | local STT engines | Transcription behind OpenAI/gateway audio APIs. |
| P0 | music/SFX engines | Music/sound generation behind gateway job API. |
| P1 | A1111/Forge SD API | Image generation/edit/upscale/inpaint backend. |
| P1 | InvokeAI/SwarmUI | Image workflow backend if users ask. |
| P1 | Hugging Face TGI | Text generation backend, preferably through OpenAI-compatible route first. |
| P1 | Hugging Face TEI | Embeddings/rerank backend. |
| P1 | OpenRouter | Hosted fallback/provider pool through OpenAI-compatible surface. |
| P1 | OpenAI/Anthropic/Gemini hosted APIs | Optional cloud fallback backends with local-only policy controls. |
| P1 | ElevenLabs | TTS/SFX backend. |
| P2 | Stability/Replicate/Fal/etc. | Optional hosted media fallback backends. |
| P2 | Stable Horde / AI Horde | Optional community compute backend. |

## Backend Adapter Pattern

Use two layers for local LLM backends:

1. OpenAI-compatible baseline adapter.
   This covers the common request path for vLLM, llama.cpp, LM Studio, TabbyAPI, text-generation-webui, TGI, Aphrodite, LocalAI, and parts of KoboldCpp.

2. Backend-native management adapter.
   This covers lifecycle, discovery, health, and extra controls that OpenAI-compatible APIs do not expose well.

Examples:

- Ollama native: `/api/tags`, `/api/show`, `/api/ps`, `/api/pull`, `/api/chat`, `/api/generate`, `keep_alive`.
- LM Studio native: model list/load/unload/download APIs.
- llama.cpp native: `/health`, `/props`, `/slots`, `/completion`, `/reranking`.
- TabbyAPI admin: model load/unload and admin-key-protected operations.
- Kobold native: `/api/v1/generate`, streaming extras, legacy sampler controls.
- TEI: embedding-only backend.

The OpenAI-compatible baseline gets requests running quickly. The native adapter makes the gateway good for self-hosters.

## Adapter Capability Metadata

Every backend adapter should report:

- supported intents
- supported protocol-native passthrough operations
- model list
- model residency if known
- load/unload support
- streaming support
- tool-call support
- structured-output support
- vision/audio/input modalities
- embeddings/rerank support
- context limits
- max concurrency
- cancellation support
- resource estimates
- health and readiness probes
- known compatibility caveats

## Canonical Intents Must Stay Broad

The intent set should cover the first user's full machine and a second user's different machine:

- `chat.generate`
- `text.complete`
- `text.embed`
- `text.rerank`
- `speech.synthesize`
- `speech.transcribe`
- `audio.generate`
- `sound.generate`
- `music.generate`
- `image.generate`
- `image.edit`
- `image.upscale`
- `image.inpaint`
- `video.generate`
- `video.edit`
- `audio.translate`
- `model.list`
- `model.load`
- `model.unload`
- `model.pull`
- `model.show`
- `model.running`
- `job.create`
- `job.get`
- `job.cancel`
- `asset.get`

Not every protocol needs to expose every intent. The router should return honest unsupported-capability errors when a client API asks for something no configured backend can do.

## Media API Shape

Media and multimodal work should expose three layers:

1. OpenAI-compatible layer.
   Images, image edits, audio speech, audio transcriptions, audio translations, embeddings, and eventually Responses image/audio behavior.

2. Provider-native passthrough layer.
   Explicit paths for backend-specific controls such as ComfyUI graph/queue operations, A1111 script args, ElevenLabs voice settings, or backend-specific audio/music parameters.

3. Generic workflow/job layer.
   Durable jobs for video, music, sound effects, long-running image workflows, upscaling batches, and hosted queue-style media APIs.

Implemented generic job surface:

```text
POST /gateway/v1/media/jobs
GET  /gateway/v1/media/jobs
GET  /gateway/v1/media/jobs/{id}
POST /gateway/v1/media/jobs/{id}/events
POST /gateway/v1/media/jobs/{id}/cancel
```

Gateway-native discovery and extension schemas should also live outside
vendor-compatible namespaces. `gateway/api.py` is mounted by `proxy.py` for
workflow discovery, capabilities, receipts, and media jobs. The currently live
discovery/receipt surface is:

```text
GET /gateway/v1/capabilities
GET /gateway/v1/extensions
GET /gateway/v1/models/{model}/capabilities
GET /gateway/v1/workflows
GET /gateway/v1/workflows/{workflow}
GET /gateway/v1/receipts
GET /gateway/v1/receipts/{request_id}
```

Additional discovery can layer onto this surface as more adapters and manifests
land.

These are not OpenAI/Ollama/Anthropic standards. They are the gateway's own
introspection contract.

Workflow-specific conditioning controls such as ControlNet, IP-Adapter, and
style-reference images belong in this gateway extension/job layer unless a
vendor-compatible protocol already owns the field. See
`docs/CONDITIONING_CONTROLS.md`.

The job request should carry:

- intent
- provider/backend or route policy
- workflow alias
- typed inputs
- input assets
- idempotency key
- requested output formats

This is the right abstraction for ComfyUI, video generation, music generation, SFX, long audio, and queued hosted media APIs.

## Reference Links

- OpenAI API reference: https://platform.openai.com/docs/api-reference
- OpenAI Python SDK: https://github.com/openai/openai-python
- OpenAI Node SDK: https://github.com/openai/openai-node
- Anthropic Python SDK: https://github.com/anthropics/anthropic-sdk-python
- Anthropic TypeScript SDK: https://github.com/anthropics/anthropic-sdk-typescript
- Ollama API: https://docs.ollama.com/api
- Ollama Python SDK: https://github.com/ollama/ollama-python
- Ollama JavaScript SDK: https://github.com/ollama/ollama-js
- vLLM OpenAI-compatible server: https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html
- llama.cpp server: https://www.mintlify.com/ggml-org/llama.cpp/inference/server
- LM Studio server docs: https://lmstudio.ai/docs/app/api
- TabbyAPI: https://github.com/theroyallab/tabbyapi
- KoboldCpp: https://koboldai.com/KoboldCpp/
- Text Generation WebUI API docs: https://github.com/oobabooga/text-generation-webui/wiki
- Hugging Face TGI API: https://huggingface.co/docs/text-generation-inference/reference/api_reference
- Hugging Face TEI: https://huggingface.co/docs/text-embeddings-inference
- ComfyUI server routes: https://docs.comfy.org/development/comfyui-server/comms_routes
- Automatic1111 API: https://github.com/AUTOMATIC1111/stable-diffusion-webui/wiki/API
- InvokeAI API docs: https://invoke-ai.github.io/InvokeAI/
- ElevenLabs API: https://elevenlabs.io/docs/api-reference
- Gemini OpenAI compatibility: https://ai.google.dev/gemini-api/docs/openai
- Gemini API docs: https://ai.google.dev/gemini-api/docs
- OpenRouter API: https://openrouter.ai/docs/api-reference/overview
- LibreChat endpoints: https://www.librechat.ai/docs/configuration/librechat_yaml/ai_endpoints
- Open WebUI OpenAI-compatible provider docs: https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible
- Open WebUI Ollama provider docs: https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-ollama/
- SillyTavern API connections: https://docs.sillytavern.app/usage/api-connections/
- Continue model providers: https://docs.continue.dev/customize/model-providers
- Cline docs: https://docs.cline.bot/



---

# Source: docs/EXTENSION_TARGETS.md

# Extension Targets

This document tracks where gateway-specific request controls belong. The rule
is conservative: standard API fields stay top-level, policy stays in profiles,
and backend/workflow knobs only become request extensions when they are
validated against an explicit contract.

## Namespaces

| Namespace | Purpose | Status |
|---|---|---|
| `extensions.com.vividfish.workflow` | Manifest-backed workflow controls for local generation/editing jobs. | Implemented for image, audio, and video workflow routes. |
| `extensions.com.vividfish.chat` | Local chat/runtime behavior that is not an OpenAI field. | Proposed. |
| `extensions.com.vividfish.routing` | Per-request routing hints that profiles may allow or deny. | Proposed. |
| `extensions.com.vividfish.telemetry` | Receipt/debug verbosity preferences without prompt/body capture. | Proposed. |

Do not use extensions for credentials, profile permissions, backend URLs,
cloud fallback policy, native passthrough permission, cache TTLs, or status
visibility. Those remain configuration/profile concerns.

The live discovery contract is:

- `GET /gateway/v1/extensions` for extension namespaces and route support.
- `GET /gateway/v1/models/{model}/capabilities` for model-specific controls.
- `GET /gateway/v1/workflows` and `GET /gateway/v1/capabilities` for the
  broader workflow catalog.
- `docs/CONDITIONING_CONTROLS.md` for ControlNet/IP-Adapter/style-reference
  conventions and readiness gates.

## Current Surface

| API | Extension Target | Current State | Notes |
|---|---|---|---|
| `POST /v1/images/generations` | `extensions.com.vividfish.workflow` | Implemented | Binds direct workflow controls such as `steps`, `guidance`, `seed`, `batch_size`, `negative_prompt`, `sampler`, `scheduler`, and `seam_blending` on `sdxl-seamless-tile` (`0..0.5`). Also supports gateway-composed controls on `flux2-klein-4b-seamless-tile`, including `seam_mask_width`, `seam_mask_expand`, `seam_mask_feather`, `seam_steps`, `seam_guidance`, `seam_denoise`, and `seam_prompt`. Unknown top-level workflow controls are rejected. |
| `POST /v1/images/edits` | `extensions.com.vividfish.workflow` | Implemented | Binds `steps`, `guidance`, and `denoise` for `flux2-klein-4b-edit`. Top-level `mask` is a standard OpenAI multipart field, not an extension; masked requests auto-select `flux2-klein-4b-inpaint` when available, which applies the normalized mask inside ComfyUI and exposes `seed`, `mask_expand`, and `mask_feather`. Future conditioning controls such as `control_image` and `controlnet_strength` stay namespaced and manifest-backed; multipart extension assets can reference an extra form field with `{"multipart_field":"control_image"}`. ControlNet is not exposed until a real ControlNet workflow and model are present. |
| `POST /v1/audio/generate` | `extensions.com.vividfish.workflow` | Implemented | ACE-Step accepts top-level `tags` and `lyrics`; `prompt` remains an alias for ACE-Step `tags` when `tags` is absent. TangoFlux accepts namespaced `steps`, `guidance`, and `batch_size`; standard `duration`, `seed`, `tags`, and `lyrics` stay top-level route-managed fields. |
| `POST /v1/videos` | `extensions.com.vividfish.workflow` | Implemented | LTX 2.3 distilled accepts namespaced `steps`, `stage1_cfg`, and `stage2_cfg`; standard `size`, `seconds`, `seed`, prompt, and image-reference fields stay top-level. |
| `POST /v1/chat/completions` | `extensions.com.vividfish.chat`, `extensions.com.vividfish.routing` | Proposed | Candidate controls: local sampler extras, backend role hints, prompt-cache preferences, fallback preference, receipt verbosity. |
| `POST /v1/responses` | `extensions.com.vividfish.chat`, `extensions.com.vividfish.routing`, `extensions.com.vividfish.responses` | Proposed | Should share chat extension validation where possible. |
| `POST /v1/completions` | `extensions.com.vividfish.chat`, `extensions.com.vividfish.routing` | Proposed | Same concerns as chat completions, narrower request shape. |
| `POST /v1/audio/speech` | `extensions.com.vividfish.audio` | Proposed | Candidate controls only if Voxtral exposes stable non-OpenAI voice controls. `voice`, `speed`, and `format` stay top-level. |
| `POST /v1/audio/transcriptions` | `extensions.com.vividfish.audio` | Proposed | Candidate controls only for stable local STT controls not expressible in OpenAI fields. |
| `POST /v1/messages` | Native Anthropic shape first | Proposed bridge | Prefer Anthropic-native fields. Gateway-only controls can be translated from `metadata.vividfish` or an explicitly allowed `extensions` block. |
| `POST /api/chat`, `POST /api/generate` | Native Ollama `options` first | Partial native support | Ollama options are not gateway extensions. Use gateway extensions only for gateway routing/diagnostics. |
| `/gateway/v1/*` | First-class native fields | Implemented | Gateway-native routes should usually use explicit `inputs`, `workflow`, `assets`, and receipt fields instead of an extension envelope. |

ControlNet readiness: there is an audit trail for ControlNet, but the current
local system is not ready. Do not expose fake `controlnet_*`, conditioning,
adapter, or preprocessor request controls until a real workflow, model,
manifest, and readiness check are present. When support is present, put those
controls under `extensions.com.vividfish.workflow` or a gateway-native media job
shape, not as global OpenAI-compatible fields.

Variant semantics are not a separate extension family. Use top-level `n` where
the public protocol owns batch count, and use manifest-backed `seed` or
route-native `seed` where the selected route already supports it. Do not add
ad hoc extension names such as `variant_count` or `variations` unless a
gateway-native route defines them explicitly.

## Standard Fields

These should not be extensions:

`model`, `messages`, `prompt`, `input`, `instructions`, `tools`,
`tool_choice`, `response_format`, `text.format`, `reasoning`, `metadata`,
`stream`, `temperature`, `top_p`, `max_tokens`, `max_output_tokens`, `stop`,
`n`, `size`, `quality`, `style`, `background`, `voice`, `speed`, `format`,
and multipart file fields such as image-edit `image` and `mask`.

## Implementation Standard

Every extension-bearing route should:

1. Accept only namespaced controls.
2. Reject unknown top-level workflow controls instead of silently dropping them.
3. Validate names, types, ranges, and enums against a manifest or explicit spec.
4. Mutate only documented backend fields.
5. Record accepted/rejected/ignored names and reasons, never raw prompts,
   media bytes, tokens, credentials, or extension values.
6. Expose supported controls through `/gateway/v1/workflows`,
   `/gateway/v1/capabilities`, and the public discovery docs.



---

# Source: docs/SDK_COMPATIBILITY_HARNESS.md

# SDK Compatibility Harness

Date: 2026-05-15

The gateway should prove compatibility with real SDKs and apps as it evolves. Curl tests are not enough: SDKs validate response envelopes, streaming event names, multipart uploads, tool-call shapes, pagination, and error types.

## Harness Principles

- Pin SDK versions in test fixtures.
- Run SDK tests against fake backend adapters first.
- Run optional smoke tests against real local backends when present.
- Publish a generated compatibility matrix.
- Test unsupported features as carefully as supported ones.
- Do not claim broad compatibility unless SDK tests pass for that endpoint.

## P0 SDKs

| SDK | Package | Protocols To Test |
|---|---|---|
| OpenAI Python | `openai` | Responses, Chat Completions, Images, Audio, Embeddings, Files where needed |
| OpenAI Node | `openai` | Responses, Chat Completions, Images, Audio, Embeddings |
| Anthropic Python | `anthropic` | Messages, streaming, tool use, image blocks |
| Anthropic TypeScript | `@anthropic-ai/sdk` | Messages, streaming, tool use, image blocks |
| Ollama Python | `ollama` | chat, generate, list/tags, show, ps, embeddings |
| Ollama JavaScript | `ollama` | chat, generate, list/tags, show, embeddings |

## P1 SDKs And Consumers

| SDK/App | Why It Matters |
|---|---|
| Vercel AI SDK | Common abstraction used by apps and agents. Tests OpenAI-compatible and Anthropic-compatible behavior from a different client layer. |
| Google Gen AI SDKs | Gemini-native compatibility and cloud fallback. |
| ElevenLabs SDKs | Voice clients and TTS/SFX workflows. |
| Open WebUI | High-value self-hosted frontend smoke target. |
| LibreChat | Multi-provider chat frontend smoke target. |
| SillyTavern | Roleplay/local-backend compatibility and Kobold/OpenAI quirks. |
| Continue | Coding assistant compatibility target. |
| Cline/OpenCode-style agents | Tool-call, streaming, Anthropic/OpenAI behavior, and long-context smoke target. |
| Claude Code | Anthropic-compatible API, `ANTHROPIC_BASE_URL`, streaming, tool use, gateway model discovery. |
| n8n | Automation workflows for image/audio/video APIs. |
| Home Assistant custom OpenAI-compatible integrations | Voice assistant/entity-control smoke target; built-in HA OpenAI integration is official-OpenAI-only. |

## Test Cases By Protocol

### OpenAI-Compatible

- client construction with custom `base_url`
- bearer auth with proxy key
- `GET /v1/models`
- chat non-streaming
- chat streaming
- function/tool calls
- JSON/schema response format
- image input content blocks
- Responses non-streaming
- Responses streaming where supported
- stored Responses get/delete
- embeddings
- image generation
- image edit multipart upload
- image responses with gateway-native extra fields (`asset_id`, `asset_url`,
  `media_type`) still parse through official SDK response models
- audio speech
- audio transcription multipart upload
- protocol-shaped validation error
- backend unavailable error
- unsupported hosted-tool error

### Anthropic Messages

- client construction with custom `base_url`
- bearer or `x-api-key` auth mapping
- message non-streaming
- message streaming event names
- system prompt
- tools and tool results
- image content blocks
- max token validation
- unsupported feature error
- backend unavailable error

### Ollama Native

- client construction with custom host
- model list/tags
- show model metadata
- chat non-streaming
- chat streaming
- generate non-streaming
- generate streaming
- embeddings
- running model/process list where supported
- pull/load/unload semantics if mapped
- unsupported operation error

### Gateway Job/Asset API

- create image/video/audio/music job
- idempotency key handling
- queue position
- progress events
- cancellation
- success asset URL
- signed/local asset fetch
- failure with actionable diagnostic

## App And Agent Smoke Tests

These are not unit tests for the gateway protocol contract. They are high-signal integration tests that prove real self-hosted clients can connect.

| App/Agent | Smoke Test |
|---|---|
| Open WebUI | Configure OpenAI-compatible URL ending in `/v1`; test `/v1/models`, non-stream chat, streaming chat, embeddings, images, STT, and TTS where enabled. |
| LibreChat | Use `librechat.yaml` custom endpoint with `baseURL`; verify generated requests hit `/v1/chat/completions`; test streaming and tool calls. |
| SillyTavern | Configure `Custom (OpenAI-compatible)` with `/v1`; run long character/system prompt, streaming chat, and multimodal content if model supports it. |
| Continue | Configure `provider: openai`, `apiBase`; test chat, autocomplete via `/completions`, streaming, and behavior when `/responses` is disabled. |
| Cline | Configure OpenAI-compatible provider; run a minimal task, a tool-using task, streaming response, and image input if the model declares vision. |
| OpenCode | Configure custom provider with `@ai-sdk/openai-compatible`; run text and tool-use prompts, verify headers and model metadata. |
| Claude Code | Set `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY`; test `/v1/messages`, streaming event sequence, tool use, and `/v1/models` discovery if enabled. |
| n8n | Validate OpenAI credential, run AI Agent with an OpenAI Chat Model and a simple tool, test streaming trigger where supported, and raw HTTP Request fallback. |
| Home Assistant custom integration | Test one conversation turn, one entity-control tool call, image generation action if supported, and timeout/error handling. |

## Compatibility Matrix Output

Generate a machine-readable report, for example:

```json
{
  "protocol": "openai",
  "sdk": "openai-python",
  "sdk_version": "x.y.z",
  "gateway_version": "local",
  "results": {
    "chat.non_streaming": "pass",
    "chat.streaming": "pass",
    "chat.tools": "partial",
    "responses.streaming": "unsupported",
    "images.edits": "pass"
  },
  "notes": {
    "responses.streaming": "Gateway returns protocol-shaped unsupported error."
  }
}
```

The README should eventually render this into a human table.

## Running The Scaffold

A first pass of this harness lives under `tests/compat/` and
`scripts/compat/`. It is intentionally minimal:

- `tests/compat/harness/` is the shared, stdlib-only library (env
  validation, matrix builder, fake response shapes).
- `tests/test_compat_harness.py` covers the harness library and is run
  by the repo's default unittest discovery.
- `tests/compat/python/probe_*.py` are optional live probes that
  auto-skip when their SDK or the env is missing.
- `tests/compat/node/` is an isolated Node project for JS SDK probes —
  it does not touch the Python install.
- `scripts/compat/run_matrix.py` aggregates whatever probes are
  available and emits a JSON document matching
  `tests/compat/matrix/compatibility.schema.json`.

Quick commands:

```bash
PROXY_PYTHON=${PROXY_PYTHON:-venv/bin/python}
PROXY_PIP=${PROXY_PIP:-venv/bin/pip}

# Core unit tests (no SDKs, no network):
$PROXY_PYTHON -m unittest discover -s tests -p 'test_*.py' -v

# Compatibility matrix dry-run:
$PROXY_PYTHON scripts/compat/run_matrix.py --dry-run

# Optional SDK probe (Python):
$PROXY_PIP install -r tests/compat/python/requirements-openai.txt
export OPENAI_PROXY_BASE_URL=http://127.0.0.1:8080
export OPENAI_PROXY_API_KEY=sk-your-proxy-key
$PROXY_PYTHON -m unittest tests.compat.python.probe_openai -v
```

Full setup, env vars, and instructions for adding new probes live in
[`tests/compat/README.md`](../tests/compat/README.md).

## Local Protocol-Frontend Smoke Script

`scripts/compat/smoke_protocol_frontends.py` is a stdlib-only HTTP smoke
runner intended for the owner's local box once live routes are wired. It
is *not* a replacement for the SDK probes above — it complements them.

When to use which:

| Use this script | Use the SDK probes / matrix |
|---|---|
| Quick "is the gateway answering on the right paths?" check during local dev. | Proving SDK-shaped responses, streaming event names, tool-call envelopes. |
| Running on a stripped machine without `openai`, `anthropic`, or `ollama` SDKs installed. | Compatibility matrix output for the README. |
| Verifying auth wiring (`/health` no auth, `/gateway/v1/capabilities` auth). | End-to-end SDK behavior including retries, pagination, multipart. |
| Optional tiny LLM round-trip behind an explicit opt-in flag. | Realistic SDK-driven chat/streaming/tool flows. |

Env vars:

- `OPENAI_PROXY_BASE_URL` (e.g. `http://127.0.0.1:5000`)
- `OPENAI_PROXY_API_KEY` (e.g. `opk_...`)
- `OPENAI_PROXY_SMOKE_LIVE_LLM=1` to enable the Anthropic `/v1/messages`
  and Ollama `/api/chat` round-trips. Default off — those checks skip.
- `OPENAI_PROXY_SMOKE_TIMEOUT` (seconds, default `10`)
- `OPENAI_PROXY_SMOKE_ANTHROPIC_MODEL`, `OPENAI_PROXY_SMOKE_OLLAMA_MODEL`
  to override the default probe models.

The script never prints the API key, prompts beyond the fixed string
`ping`, or raw response bodies. Output is one compact `[PASS|SKIP|FAIL]`
row per check plus a summary line. With no env set, it exits cleanly
with all checks marked `skip` and performs no network I/O.

## Suggested Repo Layout

```text
tests/compat/
  README.md
  conftest.py
  fake_backends/
    openai_chat.py
    media.py
    ollama.py
  python/
    test_openai_sdk.py
    test_anthropic_sdk.py
    test_ollama_sdk.py
  node/
    package.json
    openai.test.ts
    anthropic.test.ts
    ollama.test.ts
  fixtures/
    requests/
    expected/
  matrix/
    compatibility.schema.json
```

## Reference Links

- OpenAI Python SDK: https://github.com/openai/openai-python
- OpenAI Node SDK: https://github.com/openai/openai-node
- OpenAI API reference: https://platform.openai.com/docs/api-reference
- Anthropic Python SDK: https://github.com/anthropics/anthropic-sdk-python
- Anthropic TypeScript SDK: https://github.com/anthropics/anthropic-sdk-typescript
- Anthropic Messages API: https://docs.anthropic.com/en/api/messages
- Ollama Python SDK: https://github.com/ollama/ollama-python
- Ollama JavaScript SDK: https://github.com/ollama/ollama-js
- Ollama API: https://docs.ollama.com/api
- Vercel AI SDK: https://ai-sdk.dev/docs
- Google Gen AI SDK docs: https://ai.google.dev/gemini-api/docs/sdks
- ElevenLabs SDK/API docs: https://elevenlabs.io/docs



---

# Source: docs/CONDITIONING_CONTROLS.md

# Conditioning Controls

Date: 2026-05-15

Conditioning covers workflow controls that steer generation with extra media or
model-side adapters: ControlNet, IP-Adapter, style references, depth/edge maps,
pose maps, masks, regional prompting, and similar mechanisms.

## Product Rule

Offer conditioning in the API, but only as manifest-backed workflow controls.
Do not add global OpenAI-compatible top-level fields such as
`controlnet_strength` or `control_image`. They are not standard OpenAI fields,
and most workflows cannot honor them.

The live contract is:

1. A workflow manifest declares the input, type, range, and description.
2. The manifest maps that input to a real backend field or ComfyUI node.
3. Discovery advertises the input through `/gateway/v1/workflows` and
   `/gateway/v1/extensions`.
4. The route validates, binds, and receipts the input by name.
5. Unknown top-level controls are rejected instead of silently dropped.

Agent rule: discovery is authoritative. Treat examples and naming conventions
as patterns only; do not send conditioning controls unless the selected
workflow advertises them through `/gateway/v1/workflows` or
`/gateway/v1/extensions`.

## OpenAI-Compatible Image Edit Shape

For multipart image edits, extension-valued image assets can reference an extra
multipart field:

```bash
curl -s http://127.0.0.1:5000/v1/images/edits \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY" \
  -F model=flux2-klein-4b-edit-controlnet \
  -F prompt='preserve the geometry, paint it as underwater rock' \
  -F image=@input.png \
  -F control_image=@edge-map.png \
  -F 'extensions={"com.vividfish.workflow":{"control_image":{"multipart_field":"control_image"},"controlnet_strength":0.75}}'
```

`control_image` in the form is transport only. The actual API control is still
`extensions.com.vividfish.workflow.control_image`, and it must be listed by the
selected workflow manifest as an `asset` input.

JSON clients that already have a backend-ready or gateway asset reference can
send the asset value directly under the same extension name:

```json
{
  "model": "image.edit.example-controlnet",
  "prompt": "preserve geometry",
  "extensions": {
    "com.vividfish.workflow": {
      "control_image": {"asset_id": "asset_123"},
      "controlnet_strength": 0.75
    }
  }
}
```

Live image results expose gateway-native `asset_id` handles such as
`img_<sha256>`. Those handles can be reused for asset-valued workflow inputs
only when the selected manifest declares and binds that input. Reuse is scoped
to the authenticated API key that minted the asset.

## Naming Convention

Use common names when they honestly bind to real workflow behavior:

| Name | Type | Meaning |
|---|---|---|
| `control_image` | `asset` | Conditioning image, edge map, depth map, pose map, or similar. |
| `controlnet_strength` | `number` | ControlNet weight/strength. Typical range is workflow-specific. |
| `controlnet_start` | `number` | Optional start fraction/step if the workflow exposes it. |
| `controlnet_end` | `number` | Optional end fraction/step if the workflow exposes it. |
| `controlnet_model` | `string` | Optional model selector if safe for the profile and backend. |
| `controlnet_preprocessor` | `string` | Optional preprocessor selector if the workflow has a real preprocessor stage. |

Other conditioning families should use equally explicit workflow controls, for
example `ip_adapter_image`, `ip_adapter_weight`, `style_reference_image`, or
`regional_prompt`. Keep these as manifest inputs until multiple users and
backend families prove a stronger convention is needed.

## Readiness Gate

A ControlNet-capable workflow should not be advertised until all of these are
true:

- `scripts/local/audit_controlnet_readiness.py` reports the local ComfyUI
  install can enable ControlNet.
- The manifest has `requires.custom_nodes` and `requires.models` entries for
  the ControlNet nodes and model artifacts it depends on.
- The workflow JSON contains real loader/apply/preprocessor nodes as needed.
- Every advertised control has a backend binding.
- Tests assert the backend request changes at the intended node or field.

Current local status:

- `image.generate.flux2-klein-4b-seamless-tile` is the recommended local
  texture-tile endpoint when agents need an actually tileable result today. It
  is gateway-composed: generate with `flux2-klein-4b`, offset by 50%/50%, mask
  the centered seam cross, repair that mask through `flux2-klein-4b-inpaint`,
  then offset back. It exposes base `steps`, `guidance`, and `seed`, plus
  seam-specific `seam_mask_width`, `seam_mask_expand`, `seam_mask_feather`,
  `seam_steps`, `seam_guidance`, `seam_denoise`, and `seam_prompt`.
- `image.generate.sdxl-seamless-tile` is still available as the direct SDXL
  workflow. It uses `Image Seamless Texture` postprocessing and accepts
  `steps`, `guidance`, `seed`, `batch_size`, `negative_prompt`, `sampler`,
  `scheduler`, and `seam_blending` as workflow extensions. `seam_blending` is
  constrained to `0..0.5`, matching the installed WAS/img2texture backend. This
  is not the full circular SDXL stack; this machine does not currently have
  Make Circular VAE, Asymmetric/Tiled KSampler, or ControlNet Tile weights
  installed.
- `image.edit.flux2-klein-4b` exposes `steps`, `guidance`, and `denoise`.
  Standard OpenAI `mask` is accepted; without a native mask workflow it falls
  back to gateway post-composite preservation.
- `image.edit.flux2-klein-4b-inpaint` is auto-selected for
  `flux2-klein-4b-edit` requests that include `mask`. It binds the normalized
  OpenAI mask into ComfyUI with `SetLatentNoiseMask` and exposes
  `steps`, `guidance`, `denoise`, `seed`, `mask_expand`, and `mask_feather` as
  workflow extensions.
- ControlNet and IP-Adapter are not advertised on this machine yet. ComfyUI has
  node support, but the local inventory is missing ControlNet weights,
  IP-Adapter weights, and CLIP-vision assets. ControlNet-like controls are
  rejected until a real workflow, manifest, and model artifacts are installed.



---

# Source: inbox/2026-05-08-status-page-audit.md

# Status page content audit — 2026-05-08

## Executive summary

Three highest-impact gaps the page should address before anything else:

1. **Root disk is at 99% (18 GB free of 1.4 TB) and the dashboard cannot show it.** The disk card only probes `/media/windows`. A user looking at a "green banner" page right now has no way to see that ComfyUI output, the image cache, journals, models, or any other write to `/` will start failing in a few image-gen runs. This is the single most actionable missing signal on the page.
2. **No live-job visibility for `/v1/videos`.** The proxy keeps a fully populated `_video_progress` dict (status_endpoint never reads it) with `{percent, step, total, node, phase, error}` per `prompt_id`. A 5-15 minute LTX-long render is currently invisible to anyone who didn't kick it off — they can only see "ComfyUI: 1 running" in the queue card, with no way to know which alias, which prompt, how far in, or expected ETA.
3. **No request/error/identity activity panel.** AUTH_REGISTRY has 6 named keys (`simon`, `daisy`, `librechat`, `cap`, `open-webui`, ...). The proxy cannot tell anyone whether daisy has hit it in the last hour, whether anything is 4xx-ing, or whether the recent `/v1/chat/completions` rate is flat or spiking. After the Xid-109 → 503 cascade pattern documented in the inbox, the dashboard's main job is "is the service healthy *for callers*", and right now it can only answer "are the backends up".

The rest of this report is ranked by similar-impact gaps and a small set of removals/restructures.

## Method

Read `proxy.py` (4 974 lines), `status_endpoint.py` (730 lines), `proxy_config.yml`, the SKILL doc, and three inbox docs (final-state, honesty-audit, video-proposal). Probed live: `/v1/status` (cache-warm), llama.cpp `/slots` and `/props` on :5051, ComfyUI `/system_stats`, `/queue`, `/history`, `/prompt`, voxtral `/health`, parakeet `/health`. Inspected disk, dmesg, `nvidia-smi --query-compute-apps`, `sensors`, the file system layout under `/home/tnunamak/applications/{openai-proxy/cache,ComfyUI/output,daisy-audit}`. No code or config changes.

## What's currently on the page

(Verified from `/v1/status` JSON @ generated_at=1778279187.)

- Banner (`ok`/`warn`/`bad`) keyed off backend `role` × `live_status`.
- Backend grid: 6 backends with live_status, role, latency_ms, last_error, note.
- GPU strip: `index`, `memory_used_mb`, `memory_total_mb`, `instantaneous_util_pct`, plus `active_workload` annotation (ComfyUI queue depth on GPU0, llama.cpp slot count on GPU1).
- `comfy_queue`: `running`, `pending` counts.
- `models`: `loaded_llm_path`, `loaded_llm_name`, `mmproj_path`, `vision_capable`, `ctx_size`, `ctx_used` (max `n_past` across slots), `active_tts_engine`, `image_workflows`, `video_aliases`.
- `capabilities` matrix: 13 video aliases with `workflow`, `max_seconds`, `max_size`, `frame_rate`, `supports_i2v`, `supports_audio`.
- `recent_renders`: 5 most recent `*.mp4` from `/home/tnunamak/applications/ComfyUI/output/video/` with filename, size, mtime, alias.
- `driver`: last 5 Xid 109 timestamps (24 h window), `uptime_since`, `disk_avail_models` (only `/media/windows`).
- `budgets` (in-flight from sister agent): GPU0/GPU1 budget breakdown, image batch reserves, top-5 measured peak-VRAM per (workflow,size).
- Footer: links to `/health`, `/v1/models`.

## Recommended additions (ranked by impact)

### High value

#### Root disk fill warning
- **What**: Disk-pressure card surfacing `/`, `/home`, and `/media/windows` together. Color-warn when any partition crosses 85%, banner-warn at 95%.
- **Why**: Right now `df -h /` reports `/dev/nvme0n1p5  1.4T  1.3T  18G  99% /`. Nothing on the page surfaces this. ComfyUI writes to `~/applications/ComfyUI/output` (3 GB and growing — 84 files in the last 24 h), the image cache writes to `~/applications/openai-proxy/cache/images/`, daisy's audit log writes to `~/applications/daisy-audit/`, and journalctl/systemd write to `/var/log`. All of those land on the 18 GB. A single LTX-long-15-minute render at 1280×720 has been measured at ~2 MB output but several GB of intermediate disk pressure (see model-loading dumps under `/var/log/journal`). One thoughtful video session can fill the disk.
- **Source**: `df -h --output=source,size,used,avail,pcent,target /` plus `/home` and `/media/windows`. Already wired pattern: `_disk_avail_models` in `status_endpoint.py:582`. ~5 ms.
- **Cost**: One subprocess call (already budgeted for at 1 s in `_run`). Add ~3 ms to status build.
- **Caveat**: None. Banner-warn rules need a config knob so a user with predictably-tight `/` doesn't see a permanent yellow.

#### Live video-job panel (replaces or augments "ComfyUI Queue")
- **What**: For each active video job, show: alias, requested seconds, requested size, percent, step/total, phase (`encoding`/`sampling`/`decoding`), node id, age, ETA. When idle: show `0 jobs` and the most recent terminal status (success/fail timestamp).
- **Why**: The proxy already has all this in `_video_progress` (proxy.py:2835), populated live by the websocket listener. A user who didn't initiate the job currently sees only the bare queue depth. With a 15 min `ltx-2.3-long` 1280×720 wall time, that's 15 minutes of "ComfyUI: 1 running" with no way to gauge whether to wait, kill, or reroute. The inbox doc explicitly calls block-swap serialization a fragility — the dashboard is exactly where that should be visible. Daisy specifically benefits: she hands the user back `video_<id>` and currently has no reason to ever look at the dashboard.
- **Source**: Add a getter that returns a list-of-snapshots from `_video_progress` filtered to `status in {queued,running}` (or, for "recent", the last N entries). The current `/v1/status` is a separate module that imports `proxy.py`-internal state — surface this via a small shim function in `proxy.py` exported into `build_status_payload`. Polling every 8 s is plenty (the websocket pushes every step internally).
- **Cost**: Trivial — read in-process dict. No probe.
- **Caveat**: Prompt text is not in `_video_progress` today (it's in the request body which is discarded post-submit). To show prompt text would require capturing it at submit time. Suggest *not* showing prompt text by default — privacy concern across multiple keys. Show alias + size + seconds + age as the identifying triple instead.

#### Per-key request activity (last 60 minutes)
- **What**: Per-named-key counters: `requests_total`, `errors_4xx`, `errors_5xx`, `last_seen_ts`, optionally `top_endpoint` for the window. Sorted by traffic.
- **Why**: After the Xid-109 → llama-turbo restart cascade pattern, "is daisy still working" is the question this dashboard exists to answer. With six named keys (simon, daisy, librechat, cap, open-webui, +1), surface alone tells you which integration broke. Today the page can show "all backends up" and daisy could be 100% 503-ing because of an in-flight model swap — invisible.
- **Source**: Wrap the auth middleware to increment counters in a `collections.Counter` keyed by `identity.name` × bucket × status_class. AUTH_REGISTRY identity is already resolved at `proxy.py:883`. The current endpoint `prepare_downstream_headers` is the central choke point. Wrapped into a small async-safe `_REQUEST_STATS` dict with a 60-minute rolling window (1-min buckets so memory is bounded).
- **Cost**: ~50 lines of code in `proxy.py`. O(1) per request. No persistence, no Prometheus, just in-process.
- **Caveat**: This adds in-process state that didn't exist; calling out so it's a deliberate decision. If durability matters later, switch to writing 1-min summaries to a JSONL file. Keep request-body visibility *out* — only metadata.

#### Loaded LLM details: speculative decoder, total slots, slot saturation, spec hit rate
- **What**: Extend the models card with: `total_slots` (4), `active_slots`, `speculative` flag (and target model when on), per-slot `n_decoded`, the model alias from `/props` (`Qwen3.6-27B-UD-Q4_K_XL.gguf`).
- **Why**: `ctx_used` today is only `max(n_past)` — a single user filling slot 0 looks identical to four concurrent users on slots 0-3. Slot saturation is the actual scarce resource: a 5th caller's request blocks. Speculative-decoding state is also useful for "why is this slow" debugging — the current build has `speculative.type=none` per `/slots`, but a rebuild could flip that. The `/props` endpoint also reports `modalities.vision: true` directly, which is a more honest source of `vision_capable` than "mmproj path was on the cmdline".
- **Source**: `/props` (already partially used by the slot-counter heuristic in `_gpu_workloads`). One additional GET to `:5051/props`, ~10 ms.
- **Cost**: Already paying for `/slots`; add a parallel `/props` read in the same gather. Trivial.
- **Caveat**: `/props` exposes detailed sampler defaults — fine to display the few load-bearing fields, don't dump the whole payload.

#### Recent renders include images, not just videos
- **What**: Merge `~/applications/ComfyUI/output/*.png` (most recent 5) into the "recent renders" view; show video and image rows distinguished by icon. Show prompt text *only* if it can be cheaply extracted from PNGInfo (it's there — workflow embedded since 2026-05-08).
- **Why**: Image gen is the higher-volume endpoint per the eviction-loop logs (12+ restarts of the cache-evictor in 6 hours, all under 4 minutes apart). Today the recent-renders panel shows only mp4s, so the user can never confirm image gen is producing output through the dashboard. After an eviction-loop bug or a workflow change, this panel is the fastest "is text-to-image still working" check.
- **Source**: Same `Path.iterdir` pattern at `/home/tnunamak/applications/ComfyUI/output/`; filter to `*.png` and `*.mp4` excluding the `video/` subdir vs including it. Decode PNGInfo via PIL only if cheap (skip if PIL not on the path).
- **Cost**: Trivial directory listing. PNGInfo decode is the only added cost; safe to skip.
- **Caveat**: 3 GB output dir. iterdir scans all entries; cap with mtime sort. Don't render thumbnails — just filename + size + mtime + workflow stem.

#### Tabby cold-start in flight
- **What**: When TabbyAPI is being cold-started by `ensure_target()`, show a transient "starting…" pill with the target model and elapsed time; on success flip to "loaded".
- **Why**: Tabby is `standby` (correctly down at idle). But when a vision request flips it on, there's a 10–60 s window where the request hangs and the dashboard shows "tabby: down". A user troubleshooting that hang sees "down + last_error: connect refused", not "swap in progress". The proxy already serializes via `tabby_swap_lock` at proxy.py:482 — a `swap_in_progress` boolean exists in process state.
- **Source**: Expose the lock status (`tabby_swap_lock.locked()`) and the most recent `ensure_target()` target/start time. Add a small in-process state container.
- **Cost**: Few lines.
- **Caveat**: Same for kobold via `kobold_swap_lock`. For now `pin_loaded_model: false` in config means kobold may swap on role change too.

### Medium value

#### Per-process VRAM breakdown
- **What**: For each GPU, show the top-3 processes by VRAM use with PID, name, MiB. (E.g. on GPU1 right now: `llama-server 17666 MiB`; on GPU0: `voxtral python 4070 MiB, sunshine 262 MiB`.)
- **Why**: GPU memory bars are accurate in MB total but say nothing about *what* is using the memory. Useful when budget cards say "expected 13 GB ComfyUI" and reality is 4 GB ComfyUI + 9 GB orphaned llama-turbo from a botched swap.
- **Source**: `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader`. Already a 1 s subprocess budget pattern.
- **Cost**: One extra subprocess.
- **Caveat**: PIDs leak nothing useful; cmd lines may. Truncate process_name to the binary basename.

#### Image-cache footprint
- **What**: Show count of cached images, total bytes, age of oldest entry. Only meaningful since 2026-05-08 honesty-audit fix landed (`response_format: "url"` cache).
- **Why**: Currently the cache is 1.3 MB / 2 entries. Once daisy or another client flips to `response_format: "url"` in production, this becomes the disk-pressure component most likely to grow silently. The eviction loop runs every hour but only honors a 24 h TTL — heavy use over a single day can swell the cache to GB scale. Tied directly to the root-disk warning.
- **Source**: `Path.iterdir` over `cache_dir`, sum sizes, oldest mtime.
- **Cost**: Trivial.
- **Caveat**: None.

#### Last-N-minutes 4xx/5xx samples
- **What**: A small "recent failures" panel: timestamp, endpoint, status code, key-name, error class. Capped at 5 entries, last 60 min.
- **Why**: A 503 cascade during a kobold swap currently has no first-class surface on the dashboard. The proxy logs land in `journalctl --user -u openai-proxy`, but the dashboard is what humans look at. Five recent failures is a much faster signal than chasing journals.
- **Source**: Same auth/middleware wrapper as the per-key counter; ring-buffer of (ts, identity_name, path, status, short_error).
- **Cost**: ~30 lines.
- **Caveat**: Don't include request body. Truncate `short_error` to 80 chars. Drop entries on key rotation.

#### CPU thermal / RAM-DIMM thermal
- **What**: One number: `cpu_tctl` from `k10temp` and the highest RAM module temp. Color-warn at >85 °C.
- **Why**: Inbox `final-state.md` line 153 says "CPU sat at 95 °C during heavy concurrent ComfyUI + daisy" and explicitly names it as a deferred fragility. Right now it's at 76.8 °C — fine. But during a long-form video render plus chat traffic, it climbs into thermal-throttling territory and the dashboard can't see it. The DIMMs are at 51 °C with their `high` threshold at 55 °C — also worth a quiet line.
- **Source**: `sensors` parse, or `cat /sys/class/hwmon/*/temp*_input`. Subprocess, ~5 ms.
- **Cost**: One subprocess call.
- **Caveat**: `sensors` output is not stable across distros. Pre-resolve the hwmon paths once at module load.

#### ComfyUI-side memory headroom
- **What**: ComfyUI's own view of `vram_free` from `/system_stats` (shows `11418140672` right now = 10.9 GiB). Show alongside the nvidia-smi number — if they disagree by >2 GB, surface a discrepancy badge.
- **Why**: ComfyUI's `vram_free` accounts for `--reserve-vram 9` (9 GB it deliberately won't touch). nvidia-smi shows total free. The two together let a user understand "image gen has 10.9 GB to work with right now even though nvidia-smi says 11.2 GB free" — important for the budgets card and for understanding why a request 503'd.
- **Source**: `GET http://127.0.0.1:7890/system_stats` (already probed for backend health). Pull `devices[0].vram_free`.
- **Cost**: Already paying for the request; just extract more fields.
- **Caveat**: System_stats `devices[]` is single-card on this host (only the GPU ComfyUI binds to). That's fine — call it out as "ComfyUI's view" so users know it's not GPU1.

#### Workflow drift indicator
- **What**: A small dot per workflow showing whether the `*.ui.json` twin is in sync with the API workflow. Red = stale. Run `scripts/check_workflow_drift.py` lazily and cache for an hour.
- **Why**: Outputs embed the UI workflow in metadata so dragging into ComfyUI rebuilds the graph. Stale UI twins → lying about reproducibility. The drift checker exists already (`scripts/check_workflow_drift.py`) but produces no surface a human checks.
- **Source**: Run the script subprocess, parse exit code, cache for 60 min. Or read the last cached run if the script writes status to a file.
- **Cost**: Subprocess takes ~10 s (drives Playwright). MUST be backgrounded with a long TTL — not in the 12 s status cache.
- **Caveat**: If Playwright isn't installed in the proxy venv, this needs a separate worker. Punt to a daily systemd timer that writes a JSON file the dashboard reads.

#### Voxtral / Parakeet richer health
- **What**: Voxtral health includes `vram_gb` (3.78 currently). Parakeet health includes `speedup` (20.7×) and the loaded model. Surface those.
- **Why**: TTS quality is a frequent question — the model name change between voxtral-int4 and voxtral-tts is something the dashboard could display directly. Parakeet's speedup metric is a useful "is the GPU still snappy" signal.
- **Source**: Already probed (`/health` calls). Parse the JSON and show 1-2 fields.
- **Cost**: Zero added probe; parse what's already fetched.
- **Caveat**: None.

#### Last-N-minutes Xid event window
- **What**: Currently shows the last 5 Xid 109 events from the past 24 h. Add: count of events in the last 60 min (high-frequency = imminent reboot needed), count in 24 h, and *also* surface Xid codes other than 109 (Xid 31 also appears in dmesg right now).
- **Why**: The inbox doc explicitly says "84+ llama-turbo crashes today" — 24 h is the wrong window for "is the host healthy *now*". A 1-hour window with a count gives a glanceable freshness signal. Xid 31 (MMU fault) is also in journals at 15:29:11.
- **Source**: Same `journalctl -k` parse, just two windows + multi-code regex.
- **Cost**: Same subprocess (already budgeted). Add ~5 lines.
- **Caveat**: None.

### Low value / nice to have

- **TabbyAPI loaded model name** — when up, surface the EXL3 model id from `/v1/models`. Today the catch-all says "Qwen3-VL" in the backend label but the dashboard never confirms which model is actually resident.
- **Image-edit vs image-gen workflow split** — distinguish `flux2-klein-4b` from `flux2-klein-4b-edit` in the workflows list (they're sibling rows now indistinguishable except by the `-edit` suffix).
- **VRAM-estimate cache age** — the budgets card shows top-5 estimates; add the count of total estimates and last-modified date for the file, so users know whether the adaptive batcher is operating on stale data.
- **Process restart counter** — show how many times the proxy systemd unit has restarted since boot. Recovery-from-Xid pattern would be visible.
- **OpenAI alias coverage hint** — surface the count of generated aliases in `openai_model_aliases.generated.yml` (size 11.2 K). Validates that the alias generator ran.
- **Comfy custom-node warnings** — `GET /extensions` and `GET /embeddings` can flag missing custom nodes; show count of embeddings and a warning banner if `/extensions` returns a load-error list. Useful after a ComfyUI upgrade.
- **Skill-doc drift hint** — note when `~/code/dotfiles/ai/skills/local/openai-vivid-fish/SKILL.md` was last updated. Helps users spot stale docs without leaving the page.
- **Streaming connection count** — number of active SSE connections through the proxy (per ASGI lifespan it's tracked indirectly).
- **Daisy audit-log freshness** — `~/applications/daisy-audit/audit.jsonl` mtime + line count. Daisy's most recent activity in one number.

## Recommended removals

- **`instantaneous_util_pct`** in the GPU strip. The status_endpoint.py:284 comment already calls it "misleading for LLM/diffusion workloads (memory-bandwidth-bound)". The in-flight Budgets agent is reportedly removing it from the UI; if so, drop the field from the JSON payload too. Otherwise: keep emitting but never show it on the public page.
- **The "recent_renders" alias regex when the file follows a different convention.** The current regex `^([A-Za-z0-9.\-]+?)_\d{4,}_` works for `ltx-2.3-distilled_00017_.mp4` but silently empties `alias` on anything off-pattern. Either drop the field when it's empty (instead of `""`) or compute alias from the parent dir / `image_workflow` JSON metadata instead of the filename.
- **Redundant `live_status` field in the backend grid.** Today it duplicates `status` 1-for-1 (both are computed identically per status_endpoint.py:88-110). The intent was that `status` could differ from `live_status` ("show as up because role is standby even though we couldn't reach it") — but the actual implementation never diverges. Either implement the divergence (e.g. role-aware `status`) or drop `live_status`. Right now it adds JSON size and reader confusion with no signal.

## Recommended restructuring

- **Banner section should answer a single question: "what should I do *right now*?"** Today it summarizes backend health. Make it a true incident-summary line that combines: any required-down (red), root-disk >95% (red), recent Xid surge (yellow), in-flight swap (yellow), nothing (green). Each red/yellow gets one click-to-detail.
- **Collapse "Models" + "Capabilities" into a single "What can I ask for?" card.** The matrix today lists 13 video aliases, 5 image workflows, 1 LLM, 1 TTS, 1 STT — all in three different cards. A single 2-column layout (left: chat/audio; right: image/video) would cut the page height by 40% and put related signals side-by-side. The capability matrix's wide table is overkill for the 5 unique workflows behind 13 aliases — group by workflow, list aliases as pills under each.
- **GPU strip + Budgets card should fuse.** Both describe the same scarce resource. Stack the budgets bar under the live VRAM bar so the user sees "2.3 GB remaining of 4 GB ComfyUI budget" instead of mentally subtracting.
- **Move "Recent Renders" up.** It's a high-confidence "this thing actually still works" signal. Today it sits below driver/system, which is the right home for kernel-level chatter but the wrong place for a sample of working output.
- **Footer should expose the OpenAPI surface link**, not just `/health` and `/v1/models`. Links to `/v1/audio/voices`, the SKILL.md (could be served as `/SKILL.md`), and `/v1/status` (raw) help anyone landing on the page without context.

## Out of scope / open questions

- Per-key counters require new in-process state. Acceptable for the current bare-VPS deployment, but if the proxy ever runs >1 worker, counters need to move to a shared store (sqlite, redis). Decide before implementing.
- Recent-failures buffer leaks user identity and endpoints into a public-rendered page. Status page is bearer-protected (`/v1/status` requires auth) but the rendered HTML at `/` doesn't display sensitive data today. Decide whether the page itself should require auth before shipping per-key panels.
- "Recent renders" thumbnails would massively improve the visual confirmation, but require either signed image URLs in the page or a separate gallery endpoint with its own caching. Worth discussing whether the image cache (already content-addressed) should serve as the source.
- Workflow-drift checker depends on Playwright. Decide whether to run it from a daily systemd timer (writing a JSON status file) or to pre-bake it into a lightweight static-comparison checker.
- The skill doc claims things the dashboard could verify — e.g., "vision routing emits `X-Vision-Routed` header on every chat-completions response". A "self-test pass count" panel that pings `/v1/chat/completions` with a known payload nightly would surface regressions, but is meaningfully more code than the rest of this report.



---

# Source: inbox/2026-05-08-status-page-bugs-found.md

# Status page bugs found — 2026-05-08

Investigation phase before edits. Cross-referenced rendered HTML, `/v1/status`
JSON, source code (`status_endpoint.py`, `firehose_metrics.py`,
`firehose_render.py`, `proxy.py`), and ground truth (`df`, `nvidia-smi`,
`journalctl`, ComfyUI APIs, llama-server APIs, sensors, audit log).

## P0 — wrong data displayed

### P0-1. Disk `pct_used` calc misreports 99% as 93.7%

- **Section:** Disk pressure / banner.
- **Evidence:**
  - `df -h /` → `1.4T total, 1.3T used, 17G avail, 99% used` (the standard `df`
    formula `used / (used + avail)` excluding reserved blocks).
  - JSON `disk.partitions[/].pct_used = 93.7` (`status_endpoint.py:976`,
    formula `used / size`).
  - The 5% reserved blocks are not user-writable space; the page should match
    `df`'s notion of full.
  - Banner threshold is `>= 95` for `bad`; the bug keeps the banner at `warn`
    instead of `bad`, hiding an imminent disk-full condition.
- **Fix:** `pct_used = used / (used + avail)` (matches `df`).

### P0-2. `voices_summary` returns null because the fetch is unauthenticated

- **Section:** Voices summary (page shows "unavailable").
- **Evidence:**
  - Direct authed call to `/v1/audio/voices` returns 37 voices.
  - `status_endpoint.py:1255-1259` calls `httpx.get('http://127.0.0.1:5000/v1/audio/voices')`
    with no `Authorization` header. `/v1/audio/voices` is not in `auth.public_paths`,
    so it returns 401 → `_voices_summary()` returns `None`.
- **Fix:** Either add `/v1/audio/voices` to `auth.public_paths` (it's a static
  list of voice names, no secrets) — but the cleaner fix is reading the voice
  registry directly from the proxy module (no HTTP at all, no auth concern).

### P0-3. Anonymous "key" is filled by the dashboard's own polling

- **Section:** Per-API-key activity (24h).
- **Evidence:**
  - `auth_keys[0] = {name: anonymous, requests_24h: 16, top_endpoints: [/v1/audio/voices x11, / x5]}`.
  - `_FIREHOSE_NO_RECORD_PREFIXES` excludes `/health`, `/v1/status`,
    `/firehose`, etc. — but **not `/`**, **not `/_next/`** (it's there but as a
    prefix that excludes), and `_voices_summary` calls
    `127.0.0.1:5000/v1/audio/voices` (above) with no key, so each status build
    increments the "anonymous" counter.
  - The "anonymous" row is therefore a self-poll artifact, not real user
    traffic. It also obscures whether real un-authed calls are happening.
- **Fix:** (a) Stop the unauthenticated self-call (P0-2 fix). (b) Add `/`
  to no-record list. (c) Continue counting truly anonymous external calls
  separately if useful, but don't display rows whose top endpoints are 100%
  internal probe paths.

### P0-4. `_kernel_recent_events()` returns `[]` despite Xid storms in last 24h

- **Section:** Driver & kernel events / "Recent kernel events".
- **Evidence:**
  - 9 Xid 109 events + 1 Xid 31 are present in the journal in the last
    24h — confirmed via direct `journalctl -k --since "24 hours ago"` filter
    that matches `nvrm|nvidia|xid`.
  - `_kernel_recent_events()` calls `journalctl -k -n 200 --since "24 hours ago"`.
    The `-n 200` cap returns the *last 200 lines* of any kernel output;
    on a chatty kernel (USB, nf_conntrack, scheduler, etc.) the Xids fall
    off the tail. The display ends up empty.
- **Fix:** Drop `-n 200` and grep first; or grep with `-g`/`-G` (journalctl
  match), or pipe `journalctl -k --since "24 hours ago" | grep ...` and tail.

### P0-5. `last` command not present → `reboot_history` always empty

- **Section:** Driver & kernel events / "Reboot history".
- **Evidence:** `/usr/bin/last` does not exist on this host (`util-linux`
  variant). `_run(["last", ...])` returns rc=127 and `_reboot_history` returns
  `[]`. Section silently shows empty.
- **Fix:** Use `journalctl --list-boots` (always present where journald is)
  or `who -b`. Surface `unavailable_reason` when both fail.

### P0-6. `openai_aliases_count` is 0 despite 134 generated aliases

- **Section:** Configuration snapshot.
- **Evidence:**
  - `openai_model_aliases.generated.yml` has top-level `models: [...]` with
    134 items.
  - `status_endpoint.py:1635` does
    `sum(len(v) if isinstance(v, dict) else 0 for v in aliases_yaml.values())`
    — `models` is a list, not a dict, so it sums to 0.
- **Fix:** Treat list values as their `len`, dict values as their `len`,
  scalars as 1; or directly read `models` and `len()` it.

### P0-7. `cache_age_s` displayed without unit & rounded inconsistently

- **Section:** Banner.
- **Evidence:** Banner shows `cache_age=0.74s · ...` (renderer adds "s") but
  in the response with `cache_age_s` in seconds. After fix to disk it should
  surface anyway. Soft-P0 for cosmetic confusion.
- **Fix:** Show `cache_age_s=0.7s` (single decimal); always display unit.

## P1 — missing data the page should surface

### P1-1. Banner fails to escalate to `bad` when disk is actually 99%

- **Section:** Banner.
- **Evidence:** Disk fix (P0-1) → real pct = 98.7% → escalates from `warn`
  to `bad`. Without P0-1, banner reasons say `93.7% full` which is wrong.

### P1-2. Daisy audit "recent_entries" don't decode anything useful

- **Section:** Daisy audit.
- **Evidence:**
  - Audit JSON shape: `{"ts","tool","cwd","command","risk_rule","approval_id","decision","decision_ts",...}`.
  - Parser at `status_endpoint.py:1304-1308` looks for keys
    `kind|type|summary|prompt|action`, none of which exist → all entries are
    `{"kind": null, "summary": ""}`.
- **Fix:** Map to actual fields: `tool`, `risk_rule` for kind; `command`
  truncated for summary; `decision`.

### P1-3. Daisy "Last modified" is ~4 days old; not flagged as stale

- **Section:** Daisy audit.
- **Evidence:** `last_modified: 1777928046` (May 4) vs now (May 8). Section
  shows it relative ("4d ago") but no warning. Probably out of scope, soft
  flag.
- **Fix (light):** Color the "last modified" pill warn if >24h.

### P1-4. `recent_renders` is a strict subset of `recent_videos` — duplicate render

- **Section:** Recent videos vs Recent renders.
- **Evidence:** `recent_renders = recent_videos[:5]` per `status_endpoint.py:1723`,
  preserved for "backwards-compatible alias for the existing Next.js page".
  Now that the Next.js page is being removed, this duplicate becomes pure
  noise on the firehose render (which renders both sections).
- **Fix:** Render either Recent videos OR Recent renders (not both).

### P1-5. `vision_capable` source field rendered as bare `"props"` w/ no explanation

- **Section:** Models / LLM.
- **Evidence:** Renderer shows "Vision source: props" with no context. The
  user can't tell whether that means "trusted upstream answer" or "fallback".
- **Fix (light):** Render as "props (live `/props` modalities.vision)" or
  similar tooltip.

### P1-6. ComfyUI `vram_free` (10.5 GiB) ≠ nvidia-smi free (10.9 GiB) but no
discrepancy badge.

- **Section:** GPU strip vs ComfyUI view.
- **Evidence:**
  - GPU0: 13425 / 24576 MiB used → 11151 MiB free (~10.89 GiB).
  - ComfyUI device.vram_free = 11276517376 bytes = 10.50 GiB.
  - Difference = `--reserve-vram 9` (hold-back) less Comfy's own reservation.
  - The audit doc explicitly called this out as worth surfacing.
- **Fix:** Show the delta as a small "headroom mismatch" line.

### P1-7. No live banner for "image gen produced output" (but it's there)

- **Section:** Recent images was added but the page doesn't promote it to
  the high-confidence "is image gen working" position.
- **Fix:** Section ordering improvement (covered in restructure).

### P1-8. `proxy_restarts` always 0 (no restart since last service-config edit)

- Not a bug per se — `NRestarts` is correctly read. But there's no surface
  for "uptime of the proxy itself" (we have host uptime). Useful when chasing
  503 cascades.
- **Fix:** Add proxy-process-uptime alongside host-uptime.

### P1-9. Skill doc claims `X-Vision-Routed` and `X-Duration-Adjusted` headers exist
— not surfaced

- **Section:** Capabilities.
- **Evidence:** SKILL.md mentions both but the dashboard never reflects
  whether they're being emitted recently.
- **Fix:** Defer (would require capturing recent response headers; not in
  current data model).

### P1-10. `recent_events` only has video submits, but the doc says it should
include 4xx/5xx samples

- **Section:** Recent activity timeline.
- **Evidence:** `firehose_metrics.record_event` is only called from video
  submit. The audit report explicitly recommended last-N 4xx/5xx samples,
  not landed.
- **Fix:** In the firehose middleware, emit `record_event("error_4xx", ...)`
  for non-2xx statuses with truncated path/key.

### P1-11. Hotspot "comfy_output_root" total includes the `video/` subdir
counted again as a separate hotspot.

- **Section:** Disk hotspots.
- **Evidence:** `comfy_output_root` (3.1 GB, 2071 files) walks the full tree,
  which already includes `comfy_output_video` (135 MB, 99 files) shown
  separately. The separate-line is fine, but the parent total is misleading
  (looks like more disk pressure than is actually attributable).
- **Fix (light):** Annotate the parent row, or list the `video/` hotspot as
  a child entry.

## P2 — cosmetic / structural

### P2-1. `live_status` duplicates `status` 1:1 — wastes JSON & confuses viewers
Already called out in the audit doc; can be hidden in render.

### P2-2. Recent-render alias regex silently empties on non-conforming names
E.g., a hypothetical `wan-2.2-ti2v-5b_00006_.mp4` works; an `outputs/foo.mp4`
without the `_NNNN_` suffix gets `alias: null`. Render this as "—" rather
than empty string for clarity.

### P2-3. Page is two endpoints (`/` and `/firehose`) — the user wants ONE.
Resolution: serve maximalist HTML at `/`, redirect `/firehose` → `/`, drop
the Next.js page. Restructure into a clearer hierarchy (banner & actionable
warnings → live activity → static config → diagnostics).

### P2-4. Dense data shown without collapsibility
Many sections (config snapshot, vram estimates, capability matrix) are large
tables shown by default. Use `<details>` for low-priority sections.

### P2-5. Numeric values often miss units
Latencies show "X" then a separate "ms" cell, but VRAM "used_mb" is bare.
Render with units everywhere.

### P2-6. Timestamps relative-only, no absolute UTC alongside.
"2m ago" is good for live data; bad for "is this stale?". Show
`2m ago (17:01 UTC)`.

### P2-7. Throttle-reasons hex `0x0000000000000001` is unparsed.
Bit 0 = "GPU IDLE". Decode to a human label.

### P2-8. NRestarts label says "Proxy NRestarts" — should be "since last config reload" or "since unit start". Clarify.

## Out of scope / deferred

- Workflow drift cached report doesn't exist (`unavailable_reason` set
  correctly). The skill doc said this is fine to defer to a daily timer.
- TabbyAPI cold-start in-flight indicator: deferred (audit notes it's nice
  to have; the swap_state currently only reflects the lock — adequate for
  now).
- `/v1/status` `cache_age_s` rounding: cosmetic.

## Restructuring summary (collapse to /)

- Drop the Next.js `/` page.
- Mount maximalist HTML at `/` (loopback bypass + bearer auth).
- `/firehose` → 301 to `/`.
- New section ordering:
  1. **Banner + reasons** (with click-to-action).
  2. **Anything currently bad/warn** (red/yellow boxes promoted).
  3. **Live activity** — video jobs, ComfyUI queue, LLM slots.
  4. **Health snapshot** — backends, GPUs, swap state, ws state, aux health.
  5. **Resources** — VRAM attribution, budgets, disk pressure, image cache,
     thermal, CPU.
  6. **Capabilities** — models/LLM, video & image workflows, voices.
  7. **Recent renders** — videos + images.
  8. **Per-key activity** + **Recent events**.
  9. **Diagnostics (collapsed by default)** — driver/kernel events, ComfyUI
     view, daisy audit, workflow drift, config snapshot, VRAM measurements,
     errors, raw service-PIDs map.
- Always-show units. Add absolute UTC alongside relative timestamps.
- Color-code: red bad / yellow warn / green ok / grey muted.



---

# Source: inbox/2026-05-08-honesty-audit.md

# OpenAI proxy honesty audit — 2026-05-08

## TL;DR

- `n` parameter on `/v1/images/generations` and `/v1/images/edits` is silently capped at 1: the workflow only generates one image, then the proxy slices `[:n]`. Doc lists `n` as a supported field, OpenAI clients expecting `n=4` get `n=1`. **P0**.
- `response_format: "url"` on image gen is silently downgraded to `b64_json`: the doc explicitly lists `url` as a valid value, but the code returns `b64_json` regardless and the comment even admits it's "not implemented". **P1**.
- `/v1/models` doc claim that it "needs an admin-tier key" is wrong: any valid proxy key works; the proxy uses its own server-side `ADMIN_KEY` to talk to Tabby. **P2**.
- Doc claim "vision messages auto-route to TabbyAPI when model name matches a vision pattern (`vl`, `vision`, …)" is misleading: those patterns only set Tabby's `vision: true` flag. Routing is by filesystem detection (`detect_model_backend`) for unknown names, by role for role names, and by alias→default-chat for OpenAI aliases — never by the listed patterns. With kobold hot and an OpenAI alias request carrying images, the request goes to kobold and the image content silently disappears. **P1**.
- `/v1/voices` lists fewer voices than `/v1/audio/speech` accepts (no `it_*`, `nl_*`, `pt_*`, `ar_male`, `hi_*`). The voice mapper accepts those, the listing endpoint hides them. **P2**.

## Methodology

Read the SKILL.md cover to cover (470 lines), then read all 4031 lines of `proxy.py` plus `proxy_config.yml` and the workflow `_meta` blocks. For each documented operation I located its handler, traced the request/response transformation, and noted parameters that are read vs ignored. I verified flat `_meta.<field>` schema by reading the consuming code (lines 2782–3012), confirming I2V wiring is real for all configured workflows. I did not run live HTTP probes — all findings are from code inspection. No edits were made.

## Confirmed honesty gaps

### `n` parameter on image endpoints is silently capped at 1
- **Claim**: SKILL.md line 79 example shows `"n":1`, line 110 lists "`n` (optional, int, default 1)" as a documented multipart field for `/v1/images/edits`, line 79/110 doc both imply OpenAI's standard `n` semantics ("how many images to generate").
- **Reality**: `proxy.py:1809` and `proxy.py:1987` read `n` but the ComfyUI workflows are submitted once and produce one image; the only use of `n` is `images_data[:n]` (`proxy.py:1938`, `proxy.py:2174`) — slicing a 1-element list. `n=4` returns 1 image with no warning.
- **Severity**: **P0** — this is exactly the "endpoint accepts a parameter but ignores it / returns 200 instead of warning" pattern.
- **Suggested fix**: Either (a) loop the workflow submission `n` times in the handler (preferred — matches OpenAI semantics), or (b) reject `n>1` with a 400 and stop documenting it.

### `response_format: "url"` is silently downgraded to `b64_json`
- **Claim**: SKILL.md line 111 lists "`response_format` (optional): `b64_json` (default) or `url`" as a documented multipart field for `/v1/images/edits`. Image-gen recipe (line 79) uses `b64_json` but the doc still implies `url` is valid.
- **Reality**: `proxy.py:1810` reads `response_format`, but the only place it's consulted is `proxy.py:1919-1924`, where the `else` branch (anything other than `b64_json`) base64-encodes the image anyway with the comment `# For URL format, we'd need to serve the image - not implemented`. The image-edit handler (`proxy.py:2159-2160`) doesn't even branch on `response_format` — always returns `b64_json`. So a client that asks for `url` gets a `b64_json`-shaped response (the `data[].url` field never appears).
- **Severity**: **P1** — wrong response shape for OpenAI clients that branch on `data[0].url` vs `data[0].b64_json`.
- **Suggested fix**: Either implement `url` (proxy serves the file) or 400 on `response_format=url` with a clear "url not implemented" message and remove `url` from the doc.

### `/v1/models` does not require an admin-tier key
- **Claim**: SKILL.md line 44: "Everything except `/health` requires the bearer token. `/v1/models` and `/v1/model/list` need an admin-tier key."
- **Reality**: The proxy's auth (`authenticate_request`, `proxy.py:825-843`) has no notion of admin tiers — any key in `AUTH_REGISTRY` passes. `apply_tabby_auth` (`proxy.py:907-918`) injects the *server's* `ADMIN_KEY` (or downstream secret from the matched identity) on the way to Tabby. So the user-facing key just needs to be valid; "admin-tier" is a fiction the doc invented.
- **Severity**: **P2** — misleading but defensive (doc claims more restriction than reality).
- **Suggested fix**: Doc edit — remove the admin-tier claim or implement a tier system.

### "Vision auto-routes to TabbyAPI when model name matches a vision pattern" is misleading
- **Claim**: SKILL.md line 60: "Vision: send a chat message with `image_url` content parts; auto-routed to TabbyAPI when the model name matches a vision pattern (`vl`, `vision`, `pixtral`, `llava`, `cogvlm`, `internvl`)."
- **Reality**: `VISION_MODEL_PATTERNS` (`proxy.py:161-168`) and `is_vision_model` are used in exactly one place: `proxy.py:3912` sets `body_json["vision"] = is_vision_model(...)` for Tabby. They do *not* drive routing. Routing logic (`proxy.py:3732-3823`) prioritizes:
  1. Literal GGUF path → kobold
  2. Role name → role's backend
  3. OpenAI alias (`infer_openai_model_category`) → soft path; honor preferred backend if hot, else fall back to "whichever backend is hot"
  4. Else → `ensure_backend_for_model` (`detect_model_backend` filesystem scan)
  None of these paths look at `VISION_MODEL_PATTERNS`. With kobold hot and a request `model: "qwen3-vl-30b"`, path 4 may flip to tabby via filesystem detection — but the doc's stated mechanism (pattern match) isn't what actually fires.
  Worse: with kobold hot and an OpenAI alias like `gpt-4o` carrying image content, path 3 keeps the request on kobold (`proxy.py:3805-3813` — soft path, no auto-swap), and the `image_url` content is silently dropped or ignored.
- **Severity**: **P1** — the doc tells users vision routing is automatic via name patterns; the actual contract is "filesystem detection or role-based, and OpenAI vision aliases stay on whatever's hot."
- **Suggested fix**: Doc rewrite — describe the real routing rules and warn that OpenAI vision aliases (`gpt-4o`, `gpt-4-vision-*`) won't auto-switch backends.

### `/v1/voices` lists a strict subset of voices the speech endpoint accepts
- **Claim**: SKILL.md line 31 lists `/v1/voices` as "List TTS voices". Lines 414-429 lists the *speech endpoint's* full mapping including `it_female/male`, `nl_female/male`, `pt_female/male`, `ar_male`, `hi_female/male`.
- **Reality**: `/v1/voices` handler (`proxy.py:3563-3593`) hard-codes 18 voices: 6 OpenAI aliases + 5 native English + only `fr_*`, `de_*`, `es_*`. The `VOICE_MAP` (`proxy.py:1575-1596`) accepts all the `ar_male`, `hi_*`, `it_*`, `nl_*`, `pt_*` IDs as native pass-through. So a client that lists voices and uses one of those listed cannot discover `it_male`, but if they happen to send it, it works.
- **Severity**: **P2** — incomplete listing, not silently broken; just inconsistent.
- **Suggested fix**: Code edit — add the missing native voices to `list_voices` so the listing matches `VOICE_MAP`.

### Catch-all "proxy to TabbyAPI" can fall through to kobold
- **Claim**: SKILL.md line 42: "`* /<any other>` | tabby | Catch-all proxy to TabbyAPI (admin/model load endpoints, etc.)".
- **Reality**: `proxy_request` (`proxy.py:3713-4000`) defaults to `TABBY_HOST` but `proxy.py:3855-3866` flips to `KOBOLD_HOST` if Tabby is unhealthy and kobold is the active backend. So a `POST /v1/model/load` issued while llama-turbo is hot would silently hit kobold (which has no such endpoint, so a 404 would result — not a 200 lie, but the doc's "Catch-all proxy to TabbyAPI" implies tabby always).
- **Severity**: **P2** — error path is honest (404), but the doc oversells the routing guarantee.
- **Suggested fix**: Doc edit — note that the catch-all may route to kobold when tabby isn't running.

### Models response shape: kobold-loaded GGUF entry lacks `created`
- **Claim**: SKILL.md line 30: "Returns the active backend's catalog plus the currently-loaded kobold GGUF if any."
- **Reality**: `proxy.py:3699-3703` appends `{"id": loaded_gguf, "object": "model", "owned_by": "kobold"}` — no `created` timestamp. OpenAI's models endpoint always includes `created` (Unix ts) on every model object. SDKs that destructure `created` get `undefined`/None for the kobold entry only.
- **Severity**: **P2** — minor schema gap; SDKs typically tolerate missing `created` but it can fail strict validators.
- **Suggested fix**: Code edit — include `"created": int(time.time())` in the appended entry.

## False positives investigated

- **I2V wiring across all video aliases** — confirmed real. `_meta.image_node` and either `_meta.image_strength_node` (LTX) or `_meta.image_target_node`+`_meta.image_target_field` (Wan) are present in `video-ltx-2.3-distilled.json`, `video-ltx-2.3-pure-distilled.json`, `video-ltx-2.3-detail.json`, `video-ltx-2.3-long.json`, `video-wan-2.2-ti2v-5b-q8.json`. The proxy consumes them at `proxy.py:2982-3012` via flat schema — matches the workflow JSONs.
- **`_meta` schema flat vs nested** — code uses flat `meta.get("image_node")` etc. (`proxy.py:2784-2792`, `2910`, `2982`); workflow JSONs match. No nested `_meta.openai_proxy.*` indirection in this codebase.
- **`seed` parameter on `/v1/videos`** — honored (`proxy.py:2857-2868`), threads through to all `seed_nodes` listed in `_meta`, including the `value` key for INTConstant pattern noted in the comment.
- **`negative_prompt` on `/v1/videos`** — honored (`proxy.py:2800-2802`).
- **`image_strength` clamp + default 1.0** — honored (`proxy.py:2997-3000`).
- **Wan has no strength knob** — confirmed: Wan workflow has `image_target_node` but not `image_strength_node`, so the strength code-path no-ops, matching SKILL.md line 158, 172.
- **`status=failed` includes truncated `error`** — confirmed `proxy.py:3168-3176`, truncates JSON-stringified message to 500 chars.
- **`progress`, `step`, `total`, `node`, `phase` fields during a run** — confirmed populated from the live websocket listener (`proxy.py:2286-2375`) with history fallback (`proxy.py:3115-3150`).
- **Voice mapping defaults to `neutral_female`** — confirmed `proxy.py:1597`, `1612`.
- **TTS forwards `speed`/`response_format` to Voxtral** — confirmed; the body is forwarded after voice/model swap (`proxy.py:1611-1624`). Whether Voxtral honors `opus`/`speed` is a backend question, not a proxy honesty gap.
- **Streaming chat completions** — works: `proxy.py:3934-3950` detects `text/event-stream` and streams. Doc doesn't claim or deny streaming.
- **`/health` is public** — confirmed (`proxy.py:652`, `805`).

## Areas not audited

- **TabbyAPI auto-load and idle-unload behavior** — read but not deeply checked for honesty gaps; doc doesn't make user-facing claims about idle behavior.
- **Voxtral and Parakeet response shapes** — backend-internal; the proxy's job is to forward bodies and shape responses match upstream. Not in scope for proxy honesty.
- **`/v1/embeddings` with OpenAI aliases** — `default_model_for_operation("embeddings", …)` returns `""`, which raises `ModelCompatibilityError` ("No local default model is configured for embeddings requests"). Doc shows embeddings as supported but doesn't promise OpenAI-alias rewrite for embeddings, so I called this acceptable. Worth reviewing if you start documenting embedding aliases.
- **The full `/v1/chat/completions` SDK response shape** (usage tokens, finish_reason, tool calls) — these are produced by the LLM backend, not the proxy. Not a proxy honesty surface unless the doc claims specific fields (it doesn't).
- **Auth race conditions, rate limits, IPv6, TLS** — out of scope.
- **`generate-image-via-vivid` and other daisy helpers** — not read in this pass.

## Open questions for the user

1. Should `n>1` on image gen/edit loop the workflow (slower but truthful) or 400 (reject)? Looping is the OpenAI contract; rejecting is cheaper.
2. Is the `/v1/models` admin-tier claim a leftover from a planned tier system, or just incorrect doc text? If you want admin-only model listing, that needs code, not just doc.
3. For the vision-routing doc claim: do you want vision auto-switching to actually happen on OpenAI aliases (`gpt-4o` → switch to tabby when image content present), or do you want the doc to honestly say "use the `vision` role"?
4. The `created` field missing from the appended kobold model entry is trivial to fix in code — fix or leave?

## Status (2026-05-08, ideal-fix pass)

- **`n>1` silently capped (P0)** — **FIXED**. `proxy.py:proxy_comfy_request` and `proxy_comfy_edit_request` now invoke `_generate_images_with_adaptive_batching`. Workflow `_meta.batch_size_node` declared on every image workflow. Per-(workflow,size) VRAM cost measured on first request and persisted to `cache/vram_estimates.json`. Probe (n=4, 512x512) returns 4 distinct PNGs in ~13s; 503 with `Retry-After: 30` if VRAM cannot fit even one image. Validation: `/tmp/honesty-fix-gen-n4-{0..3}.png`.
- **`response_format: "url"` silently downgraded (P1)** — **FIXED**. `_build_image_response_payload` writes content-addressed PNGs to `cache/images/<sha256>.png`, returns hostname-aware HMAC-signed URLs (`?expires=&sig=`). New `GET /v1/images/{id}/content` enforces bearer auth + signature + TTL; 401 (no auth), 403 (bad/expired sig), 404 (evicted), 200 returns the actual PNG. Eviction loop runs in lifespan, deletes entries older than 24h. Validation: `/tmp/honesty-fix-url-img.png` is a valid 512x512 PNG fetched via signed URL.
- **`/v1/models` admin-tier doc claim (P2)** — **FIXED**. Skill doc updated to "no auth tier system; any valid proxy key passes for every endpoint."
- **Vision routing misleading (P1)** — **FIXED**. Pre-flight scan in `proxy_request` (`has_vision_content` checks `image_url` / `input_image` blocks). Decisions: (a) kobold w/ mmproj loaded → no switch; (b) tabby active → no switch; (c) text-only kobold + `roles.vision` configured → ensure_target switch; (d) no path → 400 `vision_unsupported`. Every chat-completions response carries `X-Vision-Routed: true | false | not-needed`. Validation: data-URL image to `model: default` returned `x-vision-routed: true` and a correct visual description.
- **`/v1/voices` strict subset (P2)** — **FIXED**. Listing now derived from module-scope `VOICE_MAP` (single source of truth). 37 entries (was 18) with `language`, `gender`, `description`, `mapped_to` metadata. All `it_*`, `nl_*`, `pt_*`, `ar_*`, `hi_*` IDs present.
- **Catch-all routing flip (P2)** — **FIXED**. The silent tabby→kobold fallthrough is gone. Catch-all routes that target tabby return 503 with `Retry-After: 10` and `code: backend_unavailable` when tabby is unreachable. Completion paths additionally accept `allow_backend_fallback: true` in the body to opt into kobold fallback when applicable.
- **`/v1/models` missing `created` (P2)** — **FIXED**. Kobold entry now populates `created` from `os.path.getmtime` of the GGUF file (with `int(time.time())` fallback when stat fails). Validation: response shows `created: 1777824597` (Sun May 3 11:09:57 2026).
- **Skill doc (Fix 7)** — **FIXED**. `~/code/dotfiles/ai/skills/local/openai-vivid-fish/SKILL.md` updated: vision routing rewritten, admin-tier claim removed, `/v1/images/{id}/content` documented, `n` and `response_format=url` truthful, observability headers (`X-Image-Rounds`, `X-Image-Returned`, `X-Vision-Routed`, `X-Proxy-Warning`) listed.

Pyright: no new diagnostics introduced. Pre-existing errors (lines 41, 42 fastapi imports; 63, 70, 97, 103, 109 env-var int casts; 4761 uvicorn) untouched.



---

# Source: inbox/2026-05-09-deferred-status-features.md

# Deferred status-page features (2026-05-09)

These were in the original spec for the status-page polish pass but pushed
out for clear reasons — captured here so the next pass can pick them up
without re-scoping.

- **Storage trend (sparkline of disk used over time)** — needs persistence.
  We currently only sample disk on each request, so we can't draw a trend
  without a sidecar logger. Smallest path: a tiny TSV under
  `cache/disk-trend.tsv`, appended every N minutes by a systemd timer.
- **5-min thermal max** — needs a sampler. Single-shot temps are noisy and
  miss spikes. Want a rolling 5-min max per-zone, which means either a
  background task in the proxy or scraping `journalctl -k` for thermal
  throttle events.
- **Network throughput (per-NIC TX/RX rate)** — too noisy to be useful as a
  single number. Needs a 1-min smoothed rate plus per-service attribution
  via cgroups (which we don't expose yet). Can revisit when the RAM
  attribution pattern proves itself.



---

# Source: inbox/2026-05-07-video-generation-proposal.md

# Proposal: Add video generation to openai-proxy

**Author:** Claude (research draft)
**Date:** 2026-05-07
**Status:** Draft for Tim
**Target service:** `~/applications/openai-proxy/` (`https://openai.vivid.fish/v1`)

---

## 1. Goal

Extend the local OpenAI-compatible proxy with a `/v1/videos` family of endpoints so any client that speaks OpenAI's Sora-shaped API (or our existing in-house clients) can request short text-to-video and image-to-video generations against local hardware. Today the proxy fronts text, TTS, STT and image generation; video is the last consumer-facing modality missing. Adding it locally means:

- No per-second cloud video bills (Sora 2 is $0.10–$0.50/s of output, [Apidog 2025-10-06](https://apidog.com/blog/sora-2-pro-api/)).
- Same auth, logging and rate-limiting story as everything else behind `proxy.py`.
- Clips can be fed straight back into other local pipelines (TTS dub, STT for captions, B-roll for chats).

We are not trying to compete with Sora 2 or Veo on quality. We are trying to deliver a "good enough, no-egress" 720p / 5s pipeline on existing hardware.

## 2. Survey of current local-video options (April 2026)

Numbers below are from cited sources running on a 24 GB consumer card unless noted. "Quantized VRAM" refers to the most popular GGUF/FP8 variant the community actually runs.

| Model | Params | License | FP16 VRAM | Quantized VRAM (FP8/GGUF) | Max output | Latency on RTX 3090, 5s 720p | ComfyUI native? |
|---|---|---|---|---|---|---|---|
| **LTX-Video 2.3** | 13B (full) / 2B (distilled) | OpenRAIL-M (Lightricks) | ~32 GB bf16 ([ltx.io](https://ltx.io/model/model-blog/how-to-generate-20-second-ai-videos)) | 14–20 GB FP8/GGUF Q8 ([zenvanriel](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/)) | 4K @ 50 fps, ~20 s ([studio.aifilms.ai](https://studio.aifilms.ai/blog/ltx-2-open-source-ai-video-model)) | 2–11 min for 5–20 s @ 720p ([fb post 2026-01-23](https://www.facebook.com/groups/comfyui/posts/882350594537642/), [LTX 2.3 RTX 3090 demo](https://www.youtube.com/watch?v=OL_4VY0h8eQ)) | Yes — built into ComfyUI core ([Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)) |
| **Wan 2.2 TI2V-5B** | 5B (dense) | Apache-2.0 ([Wan-Video/Wan2.2](https://github.com/Wan-Video/Wan2.2)) | ~16 GB | 8–10 GB GGUF Q8 ([dev.to 2025-08](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6)) | 720p @ 24 fps, 5 s | ~3–6 min ([neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/), [Wan 2.2 r/comfyui](https://www.reddit.com/r/comfyui/comments/1ogalrt/wan_22_hardware_requirements/)) | Yes ([Comfy docs](https://docs.comfy.org/tutorials/video/wan/wan2_2)) |
| **Wan 2.2 A14B (MoE 27B total)** | 14B active | Apache-2.0 | ~60 GB native ([stablediffusiontutorials](https://www.stablediffusiontutorials.com/2025/08/wan-2.2-video-generation.html)) | 16–24 GB FP8 with offload ([Wan-Video/Wan2.2 README](https://github.com/Wan-Video/Wan2.2)) | 720p @ 24 fps, 5–8 s | 6–10 min on a tight 24 GB card; community calls it "tight squeeze" ([neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/)) | Yes |
| **HunyuanVideo 1.5** | 8.3B | Tencent Hunyuan Community License (territorial; ≤100 M MAU) ([HF LICENSE](https://huggingface.co/tencent/HunyuanVideo-1.5/blob/main/LICENSE), [DeepWiki](https://deepwiki.com/Tencent-Hunyuan/HunyuanVideo/1.2-licensing)) | ~24 GB | ~14 GB with offload ([HF model card](https://huggingface.co/tencent/HunyuanVideo-1.5)) | 1080p, 5 s ([quasa.io](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source)) | <4 min for 5 s @ 1080p on 3090 Ti w/ TensorRT ([quasa.io](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source)) | Yes (unofficial nodes); HunyuanVideo 1.5 native support landing |
| **CogVideoX-5B** | 5B | CogVideoX License (custom; 2B variant is Apache-2.0) ([studioinstance/CogVideo](https://github.com/studioinstance/CogVideo-1)) | ~20 GB ([clore.ai](https://docs.clore.ai/guides/video-generation/cogvideox)) | ~12 GB w/ TorchAO INT8 ([DeepWiki perf](https://deepwiki.com/zai-org/CogVideo/7-performance-optimization)) | 720×480, 6 s, 49 frames | ~5–8 min | Yes |
| **Mochi 1** | 10B | Apache-2.0 ([Genmo blog](https://www.genmo.ai/blog/mochi-1-a-new-sota-in-open-text-to-video)) | ~40 GB native | ~20 GB w/ ComfyUI optimisations ([Apatero](https://www.apatero.com/blog/mochi-1-consumer-hardware-comfyui-optimization-guide-2025)) | 480p, 5.4 s | 8–15 min on a 3090 | Yes, but stalled — see "ecosystem stability" below |

Read this honestly:

- **LTX-2.3** is the most actively iterated model in the ecosystem right now. The author (Lightricks) ships first-party ComfyUI nodes, the community has Q8 GGUFs, and there are distilled variants down to 2B. It is bleeding-edge — workflows still break between point releases ([Comfy-Org issue #11726](https://github.com/Comfy-Org/ComfyUI/issues/11726)) — but it's the model people are choosing on a 3090 today.
- **Wan 2.2** is the conservative pick. The 5B TI2V is dramatically smaller than what we have headroom for and is Apache-2.0 with no caveats. The 14B MoE is higher quality but uncomfortably tight on 24 GB.
- **HunyuanVideo 1.5** is technically excellent but has a license that excludes EU/UK/South Korea distribution and caps commercial use at 100 M MAU — not a problem for a single-user proxy, but it's a tripwire if Tim ever exposes the endpoint to others.
- **CogVideoX-5B** is mature but at ≤6 s / 720×480 it's effectively a previous generation. The 2B variant is Apache-2.0 if license matters more than quality.
- **Mochi 1** is Apache-2.0 and was a 2024 SOTA, but Genmo has not shipped meaningful updates and the community has largely moved to Wan/LTX. Skip.

Tim's "~140 s for 720p 5-second video" data point is **plausible but optimistic**: it lines up with LTX-2 distilled variants on a 4090 ([wavespeed quickstart](https://wavespeed.ai/blog/posts/blog-ltx-2-comfyui-quickstart/), [insiderllm](https://insiderllm.com/guides/local-ai-video-generation/) cites "5-second clip in 4 seconds on an RTX 4090" for distilled LTX), but full-quality 13B LTX on a 3090 is closer to 2–11 minutes depending on resolution and step count ([Facebook ComfyUI group, 2026-01-23](https://www.facebook.com/groups/comfyui/posts/882350594537642/), [Patreon Silent Snow](https://www.patreon.com/posts/ltx-2-3-in-first-152406236) reports 45–60 min at 24 GB / 128 GB RAM at full quality I2V). Treat 2–6 minutes as the realistic working envelope.

## 3. Recommended model: **Wan 2.2 TI2V-5B (FP8) as primary, LTX-2.3 distilled as fast fallback**

> **2026-05-07 update — see Addendum at end of doc.** Re-ranked under quality-first criteria with offloading allowed; the recommended primary changes to **LTX-2.3 13B (full, FP8 + weight streaming)**. Wan 2.2 TI2V-5B becomes the conservative-fallback alias.
>
> **2026-05-07 second update — see Addendum 2.** Reconciled with `gpu_budget.sh`: GPU0 has only ~8–10 GB usable for video, not 24 GB. Combined with the 3090's lack of native FP8 hardware, the recommended primary changes again — to **LTX-2.3 13B GGUF Q8 with block-swap** (peak ~9–11 GB, ~3–5 min/720p/5s), with LTX-2.3 distilled 2B as the fast preview alias.

Justification:

1. **VRAM headroom on GPU0 next to ComfyUI image-gen.** Wan 2.2 5B at FP8 fits in 8–10 GB ([dev.to](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6)). With ComfyUI image-gen (flux2-klein-4b ~6–8 GB) we're still under 24 GB on GPU0 if both ever overlap, though we should not run them simultaneously (see §7).
2. **Latency.** ~3–6 min for 720p / 5 s is acceptable for a "request, walk away" UX.
3. **License.** Apache-2.0, no territorial or MAU clauses ([Wan2.2 GitHub](https://github.com/Wan-Video/Wan2.2)). Future-safe.
4. **ComfyUI first-class.** Native workflow templates exist ([Comfy docs](https://docs.comfy.org/tutorials/video/wan/wan2_2)). Reuses the ComfyUI process we already operate.
5. **Both T2V and I2V in one model** (TI2V = Text-and-Image-to-Video). Avoids loading two checkpoints.

LTX-2.3 distilled is the recommended **fast** model alias for short-iteration workflows where Tim wants ~30–60 s previews ([crepal](https://crepal.ai/blog/aivideo/blog-ltx-2-vram-requirements/)). It's added as a second model entry, not a separate service.

We are **not** recommending Wan 2.2 14B at this time: the [community thread on 24 GB VRAM](https://www.reddit.com/r/StableDiffusion/comments/1pfuhvz/best_wan_22_models_and_workflows_for_24gb/) describes it as workable with offloading but slow and OOM-prone. Tim already lost evenings to VRAM-edge crashes on the LLM side — don't volunteer for more.

We are **not** recommending HunyuanVideo as primary because of the license; we can revisit it as a `model: "hunyuan-video-1.5"` opt-in for personal use only.

## 4. Recommended integration path: extend the **existing ComfyUI** with new workflows

The proxy already routes `/v1/images/*` to ComfyUI. Adding video means: drop the model files into `ComfyUI/models/`, add new workflow JSONs, and have `proxy.py` POST to ComfyUI's existing `/prompt` API on a separate workflow ID. No new daemons.

| Path | Pros | Cons |
|---|---|---|
| **A. Extend existing ComfyUI** (recommended) | Reuses already-running process and GPU pinning. One operational surface. ComfyUI has first-class Wan 2.2 and LTX nodes. Already integrated with the proxy. | ComfyUI's queue is single-threaded by default; image and video requests serialise. Mitigated by long-tail nature of video reqs. |
| B. Run **Wan2GP** as a second service | Easy-button install, aggressive RAM offloading built in ([Wan2GP README](https://github.com/deepbeepmeep/Wan2GP)), supports Wan/LTX/Hunyuan with a single config knob. | Adds a Gradio/HTTP service to babysit. Wan2GP's API is not OpenAI-shaped — proxy.py would need a new adapter. Duplicate model files unless we symlink. |
| C. Dedicated video server (vLLM-style) | Cleanest separation. | Nothing comparable to vLLM exists for diffusion video models in May 2026. Would mean writing one. Not worth it. |

**Pick A.** Wan2GP is the right tool when you don't already have ComfyUI; we do. The pinokio-style automation is solving a problem we already solved in a different way.

If we ever outgrow ComfyUI's serialised queue (e.g. concurrent image + video), switching to Wan2GP-as-second-service is a one-week refactor, not a rewrite. Not yet.

## 5. API surface

The OpenAI-compatible community has converged on Sora 2's shape: `POST /v1/videos` with async polling at `GET /v1/videos/{video_id}` and content fetch at `GET /v1/videos/{video_id}/content` ([OpenAI docs](https://developers.openai.com/api/docs/guides/video-generation), [LaoZhang blog 2026-03-28](https://blog.laozhang.ai/en/posts/openai-sora-api), [Azure Foundry](https://timhanewich.medium.com/how-to-use-the-sora-api-for-video-genein-azure-openai-service-azure-ai-foundry-86fa79da94b4)). We mirror that.

### Proposed endpoints

```
POST   /v1/videos                       Create job
GET    /v1/videos/{id}                  Poll status
GET    /v1/videos/{id}/content          Download MP4 (binary)
DELETE /v1/videos/{id}                  Cancel / cleanup
GET    /v1/videos                       List recent jobs (debug)
```

### Request body (POST /v1/videos)

Sora's actual payload uses `prompt`, `model`, `size`, `seconds`, `n_variants` ([Sora 2 prompting guide](https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide), [Azure example](https://timhanewich.medium.com/how-to-use-the-sora-api-for-video-genein-azure-openai-service-azure-ai-foundry-86fa79da94b4)). Mirror it exactly so OpenAI client SDKs Just Work:

```json
{
  "model": "wan-2.2-ti2v-5b",
  "prompt": "A red fox walking through a snowy forest at dusk, cinematic",
  "size": "1280x720",
  "seconds": 5,
  "n_variants": 1,
  "input_reference": null,
  "seed": 42
}
```

Notes:
- `model`: `wan-2.2-ti2v-5b` (default), `ltx-2.3-distilled` (fast), optional `hunyuan-video-1.5` later. Map via `proxy_config.yml` exactly like chat-completion model aliases.
- `size`: limited to `768x768`, `1280x720`, `1024x576` initially. Reject anything ComfyUI can't tile.
- `seconds`: int 2–8 for Wan, 2–20 for LTX. Reject above per-model max.
- `input_reference` (optional): URL or base64 image for I2V mode (Wan 2.2 supports this natively).

### Response shape

Sync-looking response with async semantics, matches Sora 2:

```json
{
  "id": "video_2026050722f3a1",
  "object": "video.generation",
  "model": "wan-2.2-ti2v-5b",
  "status": "queued",
  "progress": 0,
  "created_at": 1746556800,
  "expires_at": 1746643200,
  "size": "1280x720",
  "seconds": 5
}
```

`status` transitions: `queued` → `running` → `succeeded` | `failed` | `canceled`.

When `succeeded`, the client fetches `GET /v1/videos/{id}/content` which returns `Content-Type: video/mp4`. We do **not** return base64 in the JSON body — videos at 720p / 5 s are 2–8 MB and base64 inflates the JSON layer for no reason. (OpenAI's own approach: separate `/content` endpoint, [video gen guide](https://developers.openai.com/api/docs/guides/video-generation).)

### Sync vs async

Generation takes 2–10 minutes. Sync HTTP at openai.vivid.fish would mean:
1. Traefik default 60 s timeout — would 504 every request.
2. Browser tabs would just hang.

Two acceptable patterns:

1. **Polling (recommended, matches Sora 2).** Return immediately with `id` + `status: queued`. Client polls `GET /v1/videos/{id}` every 5–10 s. Most OpenAI SDKs already implement this for Sora.
2. **SSE on the same endpoint.** `Accept: text/event-stream` upgrades the response to a streamed sequence of `progress` events ending in a final `succeeded` event. Useful for the SillyTavern UI but secondary; ship polling first.

Job state lives in `data/videos/{id}/` on disk: a `meta.json` with status fields and an `output.mp4` once finished. Survives restarts; cron purges entries older than 24 h.

## 6. Resource accounting

Per-request envelope, isolated:

| Step | GPU | VRAM peak | Disk write | Wall time |
|---|---|---|---|---|
| Wan 2.2 TI2V-5B FP8 load | GPU0 | ~9 GB | 0 (cached) | ~10–30 s cold, ~2 s warm |
| Diffusion (40 steps, 720p, 5 s) | GPU0 | ~12–14 GB peak ([dev.to bench](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6)) | latents to disk if offloading | 3–5 min |
| VAE decode + ffmpeg encode | GPU0 + CPU | brief +2–3 GB VAE spike | 2–8 MB MP4 | 5–15 s |
| **Total** | GPU0 | **~14 GB peak**, 9 GB resident | ~10 MB / job + 30 GB one-time models | **3–6 min** |

Disk: Wan 2.2 5B FP8 GGUF ~6 GB; LTX-2.3 distilled GGUF ~5 GB; tokenizer/VAE ~2 GB. Plan for **~30 GB** under `ComfyUI/models/` after we add 1–2 model variants and LoRAs.

Under contention:

- **Image-gen on GPU0 + video on GPU0:** ComfyUI serialises the queue. Image jobs (~10 s with flux2-klein-4b) will block behind a video job in progress. Mitigation in §7.
- **LLM on GPU1 + video on GPU0:** independent GPUs; should be clean. PCIe contention is theoretical; system RAM offloading is on GPU0's NUMA node only.
- **Voxtral TTS / Parakeet STT on GPU0:** these are small (<3 GB) and quick (<1 s for typical chunks). They'll run between video diffusion steps if ComfyUI yields. **Open question (see §9):** whether ComfyUI's GPU lock blocks TTS during a video job; if yes, voice latency suffers during video gen. Need to measure.
- **GPU1's llama-turbo at 20–21 GB:** untouched by video on GPU0. The llama-turbo VRAM-edge crashes Tim has already seen are unrelated and stay our problem on the LLM side.

## 7. Coexistence strategy

Hardware reality: one 24 GB card runs ComfyUI (image + video + TTS + STT), one 24 GB card runs the LLM. We should not pretend we have more.

Recommended policy:

1. **Default:** video runs on GPU0 alongside existing ComfyUI image/TTS/STT. ComfyUI's queue serialises; that's a feature, not a bug, for our scale (single-user workstation).
2. **Image + video coexistence:** rely on ComfyUI's "smart memory" mode to evict the inactive model. Verify that the image checkpoint unloads when a video request comes in and reloads when done. Document the resulting "first image after a video takes 15 s extra" behaviour.
3. **TTS / STT priority:** keep voxtral-tts and parakeet-stt on **separate processes** (they already are) so a stuck ComfyUI queue doesn't kill voice. They each carve out their own VRAM region (~2–3 GB combined). Any ComfyUI model swap must respect this — set ComfyUI's `--reserve-vram 4` or equivalent.
4. **GPU1 fallback (future):** if Tim wants faster video and the LLM is idle, we can opt-in route video to GPU1 via a `priority: gpu1-when-idle` config flag and a systemd `Conflicts=` similar to the existing kobold/turbo arrangement (per Tim's MEMORY.md). Out of scope for v1.
5. **systemd ordering:** no new units. Video generation is a workflow inside the existing `comfyui.service`. The proxy's existing `After=comfyui.service` ordering already covers us.

## 8. Risks

1. **ComfyUI queue head-of-line blocking.** A 5-minute video stalls all image / TTS calls on GPU0. For a single user this is annoying, not fatal. If multi-user, separate the services.
2. **VRAM spikes during VAE decode.** Several community reports cite the VAE as the OOM culprit ([Comfy-Org #11726](https://github.com/Comfy-Org/ComfyUI/issues/11726)). The Tiny VAE preview pattern Tim mentioned is real and worth wiring as the seed-preview step.
3. **Model licenses.**
   - Wan 2.2: Apache-2.0, clean ([Wan-Video/Wan2.2](https://github.com/Wan-Video/Wan2.2)).
   - LTX-2.3: OpenRAIL-M-style, with use-case restrictions; check for any "no deepfakes / no defamation" clauses before exposing publicly.
   - HunyuanVideo 1.5: Tencent Community License excludes EU/UK/South Korea distribution and caps at 100 M MAU ([HF LICENSE](https://huggingface.co/tencent/HunyuanVideo-1.5/blob/main/LICENSE)). Keep gated behind a personal-use opt-in.
4. **Disk usage.** ~30 GB initial + ~10 MB per job + LoRAs. Plan for 100 GB headroom under `~/applications/comfyui/models/`.
5. **Determinism.** Diffusion video is seed-deterministic only when sampler, scheduler and node graph are byte-identical. Don't promise reproducibility across model upgrades.
6. **NSFW handling.** Wan 2.2 and LTX have soft safety filters; HunyuanVideo's filter is more aggressive. Document policy explicitly in the proxy README.
7. **External exposure (`openai.vivid.fish`).** Long-running jobs are an obvious DoS vector. Even with personal auth, add a per-key concurrency cap of 1 video and a rate limit (e.g. 12/hour). Existing proxy auth machinery is sufficient.
8. **Tim's "140 s" benchmark is for an unrealistically light path.** Be ready to write down "first run took 7 minutes" and not have it feel like a regression.

## 9. Open questions for Tim

1. **Primary use case:** loose 720p/5 s generations for fun, or longer-form (10–20 s) with audio (LTX-2.3 with audio is a ~28–32 GB workflow per [LTX](https://ltx.io/model/model-blog/how-to-generate-20-second-ai-videos))? This decides whether we add the LTX audio path now or later.
2. **TTS/STT during video gen — acceptable to block briefly, or must voice stay always-responsive?** Drives whether ComfyUI gets exclusive use of GPU0 or whether we move TTS/STT to GPU1 (where llama-turbo currently lives at the VRAM edge).
3. **Do we need I2V (image-to-video) or only T2V?** I2V doubles the test surface but is the more interesting use case for character cards / chats.
4. **Web access policy:** is `openai.vivid.fish/v1/videos` reachable from outside the LAN? If yes, what concurrency cap and what NSFW posture?
5. **Storage retention:** keep videos forever, 24 h, or signed URLs only? Affects disk planning.
6. **HunyuanVideo opt-in:** willing to accept the territorial license for personal use, or skip it entirely?
7. **OK with polling-only v1?** SSE / progress streaming adds work; happy to defer if polling is acceptable.
8. **Budget for one-time setup** (model downloads, workflow tuning): half a day, a weekend, or "as long as it takes"?

## 10. References

- LTX-Video / LTX-2 ecosystem
  - [Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)
  - [LTX-2 VRAM requirements (crepal.ai)](https://crepal.ai/blog/aivideo/blog-ltx-2-vram-requirements/)
  - [LTX-2 / WaveSpeed VRAM reality check](https://wavespeed.ai/blog/posts/blog-ltx-2-vram-requirements/)
  - [LTX-2 quickstart — WaveSpeed](https://wavespeed.ai/blog/posts/blog-ltx-2-comfyui-quickstart/)
  - [LTX-2 zenvanriel guide](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/)
  - [LTX 2.3 RTX 3090 demo — YouTube](https://www.youtube.com/watch?v=OL_4VY0h8eQ)
  - [LTX 2.3 first look — Patreon Silent Snow](https://www.patreon.com/posts/ltx-2-3-in-first-152406236)
  - [Studio AI Films LTX-2 deep-dive](https://studio.aifilms.ai/blog/ltx-2-open-source-ai-video-model)
  - [ComfyUI LTX-2 VAE/RAM issue #11726](https://github.com/Comfy-Org/ComfyUI/issues/11726)
  - [r/comfyui LTX 2.3 dual-GPU thread (3090 + 4060 Ti)](https://www.reddit.com/r/comfyui/comments/1rr67bc/ltxvideo_23_workflow_for_dualgpu_setups_3090_4060/)
  - [ltx.io 20s video guide](https://ltx.io/model/model-blog/how-to-generate-20-second-ai-videos)
  - [Facebook ComfyUI group LTX-2 RTX 3090 timing data point](https://www.facebook.com/groups/comfyui/posts/882350594537642/)
- Wan 2.2 ecosystem
  - [Wan-Video/Wan2.2 (Apache-2.0)](https://github.com/Wan-Video/Wan2.2)
  - [Wan2.2-T2V-A14B HF model card](https://huggingface.co/Wan-AI/Wan2.2-T2V-A14B)
  - [ComfyUI Wan 2.2 native workflow](https://docs.comfy.org/tutorials/video/wan/wan2_2)
  - [Wan 2.2 GGUF setup tutorial](https://www.stablediffusiontutorials.com/2025/08/wan-2.2-video-generation.html)
  - [Run Wan 2.2 on 8 GB VRAM (dev.to)](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6)
  - [r/StableDiffusion best workflows on 24 GB / 32 GB RAM](https://www.reddit.com/r/StableDiffusion/comments/1pfuhvz/best_wan_22_models_and_workflows_for_24gb/)
  - [r/comfyui Wan 2.2 hardware thread](https://www.reddit.com/r/comfyui/comments/1ogalrt/wan_22_hardware_requirements/)
  - [WAN 2.2 vs LTX-2 — neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/)
  - [LTX 2.3 vs WAN 2.2 — WaveSpeed](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/)
  - [Simplismart: serving Wan 2.2 (Apache-2.0)](https://simplismart.ai/blog/deploy-wan-2-2)
- HunyuanVideo
  - [Tencent-Hunyuan/HunyuanVideo-1.5 GitHub](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5)
  - [HF model card with VRAM specs](https://huggingface.co/tencent/HunyuanVideo-1.5)
  - [HunyuanVideo 1.5 LICENSE (territorial)](https://huggingface.co/tencent/HunyuanVideo-1.5/blob/main/LICENSE)
  - [Licensing summary — DeepWiki](https://deepwiki.com/Tencent-Hunyuan/HunyuanVideo/1.2-licensing)
  - [HunyuanVideo 1.5 hands-on — Quasa](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source)
  - [arXiv: HunyuanVideo 1.5 technical report](https://arxiv.org/abs/2511.18870)
- CogVideoX / Mochi
  - [zai-org/CogVideo perf optimisations — DeepWiki](https://deepwiki.com/zai-org/CogVideo/7-performance-optimization)
  - [studioinstance/CogVideo licensing](https://github.com/studioinstance/CogVideo-1)
  - [CogVideoX guide — Clore.ai](https://docs.clore.ai/guides/video-generation/cogvideox)
  - [Mochi 1 announcement — Genmo blog](https://www.genmo.ai/blog/mochi-1-a-new-sota-in-open-text-to-video)
  - [Mochi 1 on consumer GPUs — Apatero](https://www.apatero.com/blog/mochi-1-consumer-hardware-comfyui-optimization-guide-2025)
- Wan2GP / offloading tooling
  - [deepbeepmeep/Wan2GP](https://github.com/deepbeepmeep/Wan2GP)
  - [deepbeepmeep/mmgp memory manager](https://github.com/deepbeepmeep/mmgp)
  - [WanGP webui guide — Apatero](https://apatero.com/blog/wangp-webui-complete-guide-low-vram-video-2025)
  - [BrightCoding Wan2GP overview](https://www.blog.brightcoding.dev/2025/09/17/open-source-video-generation-for-low-vram-gpus-how-wan2gp-puts-cinematic-ai-in-reach-of-the-gpu-poor/)
- API conventions
  - [OpenAI Sora video generation guide](https://developers.openai.com/api/docs/guides/video-generation)
  - [Sora 2 prompting guide](https://developers.openai.com/cookbook/examples/sora/sora2_prompting_guide)
  - [Apidog: Sora 2 / Sora 2 Pro API](https://apidog.com/blog/sora-2-pro-api/)
  - [LaoZhang AI: OpenAI Videos API in 2026](https://blog.laozhang.ai/en/posts/openai-sora-api)
  - [Azure OpenAI Sora — Tim Hanewich Medium](https://timhanewich.medium.com/how-to-use-the-sora-api-for-video-genein-azure-openai-service-azure-ai-foundry-86fa79da94b4)
  - [Microsoft Foundry Sora 2 docs](https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/video-generation)
  - [fal.ai Kling 2.1 image-to-video API shape](https://fal.ai/models/fal-ai/kling-video/v2.1/master/image-to-video/api)
- Cross-cutting reviews / benchmarks
  - [Video generation comparison — Clore.ai](https://docs.clore.ai/guides/comparisons/video-gen-comparison)
  - [Video generation VRAM requirements 2026](https://willitrunai.com/blog/video-generation-gpu-guide-2026)
  - [Top 6 ComfyUI text-to-video — Apatero](https://apatero.com/blog/top-6-comfyui-text-to-video-models-you-need-to-try-in-2025-the-ultimate-performance-guide)
  - [Local AI video generation in 2026 — InsiderLLM](https://insiderllm.com/guides/local-ai-video-generation/)
  - [Top open-source text-to-video — Modal blog](https://modal.com/blog/text-to-video-ai-article)

---

## Addendum: Quality-first re-evaluation (2026-05-07)

The original ranking optimised for **VRAM safety, license, stability, quality, latency** — in that order. Tim's pushback (paraphrased: "LTX is supposedly way better, so why isn't that the primary?") is fair. This addendum re-ranks with **quality, ecosystem activity, latency, VRAM (offloading allowed), license, stability**, treating dynamic VRAM offloading as a first-class fit strategy on a 3090 with 64 GB+ system RAM.

### A. Reframed criteria

| # | Criterion | Why it moved |
|---|---|---|
| 1 | **Output quality** | This is the actual user-felt deliverable. We have a 3090, not a phone. |
| 2 | **Ecosystem activity** | A model that gets weekly LoRAs / nodes / fixes ages well; a stalled model decays. |
| 3 | **Latency under our hardware** | "Acceptable" not "fast." 3–8 min per clip is fine on a personal proxy. |
| 4 | **VRAM with offloading allowed** | Block-swap and weight-streaming are mature; we should use them. |
| 5 | **License** | Still matters for `openai.vivid.fish` if exposed; demoted because we can gate per-model. |
| 6 | **Stability** | Demoted but not ignored — a model that OOMs on the second sampler is unusable regardless of quality. |

### B. Quality comparison (Mar–Apr 2026 sources)

| Attribute | Winner | Loser | Source |
|---|---|---|---|
| Cinematic camera language (dolly, tilt, parallax) | **Wan 2.2** | LTX-2.3 ("kept framing steady"; LTX wobbled on camera moves) | [WaveSpeed Mar-2026 head-to-head](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/) |
| Fine micro-texture (fabric, skin, wood grain) | **Wan 2.2** | LTX-2.3 (softens textures under busy motion) | [WaveSpeed](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/) |
| Multi-subject prompt fidelity | **Wan 2.2 (MoE)** | LTX-2 | [neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/) |
| Native synced audio in one pass | **LTX-2.3** | Wan 2.2 (needs S2V add-on), Hunyuan | [neurocanvas](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/), [zenvanriel](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/) |
| Speed at equivalent settings | **LTX-2.3** (10–14× Wan; "18×" once on simple) | Wan 2.2 | [WaveSpeed](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/) |
| 1080p+ resolution ceiling | **LTX-2.3** (stable 720p, ok-ish 1080p), **Hunyuan 1.5** (native 1080p via two-stage SR) | Wan 2.2 (needs babysitting >768p) | [WaveSpeed](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/), [Quasa Hunyuan 1.5](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source) |
| Geometric / multi-view consistency (static scenes, GeCo benchmark, Apr-2026) | **Wan 2.2** = best open-source overall; HunyuanVideo and LTX-Video trail | — | [arXiv 2512.22274 GeCo](https://arxiv.org/html/2512.22274v2) Table 5 |
| Motion coherence vs closed leaders (VBench, T2V-Score, GenAI-Arena) | **HunyuanVideo 1.5** neck-and-neck with Kling 1.5; ahead of Runway Gen-3 / Luma / Pika 1.5 | — | [Quasa](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source) (3rd-party summary; primary VBench numbers not yet replicated by an independent run we found) |
| Long-form (>10 s) | **LTX-2.3** (4K @ 50 fps, ~20 s; 40 s with audio reported) | Wan 2.2 (5–8 s practical), Hunyuan (5–10 s) | [ltx.io 20s guide](https://ltx.io/model/model-blog/how-to-generate-20-second-ai-videos), [FB ComfyUI Mar-2026](https://www.facebook.com/groups/592850912909945/posts/1295579505970412/) |

**Where sources disagree:** WaveSpeed and neurocanvas both say Wan 2.2 wins cinematic quality but disagree on whether LTX is *competitive* on motion (WaveSpeed: "snappier but floaty"; neurocanvas: "AI float"). The Quasa Hunyuan article overstates Hunyuan's license (claims "Apache 2.0"); the [HF LICENSE file](https://huggingface.co/tencent/HunyuanVideo-1.5/blob/main/LICENSE) is the Tencent Community License with territorial exclusions. Trust the LICENSE, not the blog. The GeCo benchmark is narrow (static scenes, slow camera) and doesn't generalise to character motion.

**Net read:** there is no single quality winner. Wan 2.2 wins **cinematic photoreal**; LTX-2.3 wins **iteration speed, audio, longer clips, prompt-iteration UX**; Hunyuan 1.5 wins **headroom-per-VRAM and human-preference benchmarks** but the ecosystem is younger and the license is gated.

### C. Dynamic VRAM offloading: what it costs and what it enables

ComfyUI offers two distinct mechanisms, often confused:

1. **Block swap** — a model-architecture-aware node from kijai's wrappers ([WanVideoBlockSwap in `ComfyUI-WanVideoWrapper`](https://github.com/kijai/ComfyUI-WanVideoWrapper), [HyVideoBlockSwap in `ComfyUI-HunyuanVideoWrapper`](https://www.runcomfy.com/comfyui-nodes/ComfyUI-HunyuanVideoWrapper/hy-video-block-swap)) that holds N transformer blocks (range 0–40) in CPU RAM and streams each block to VRAM only when its forward pass executes. The HunyuanVideo node also has independent `offload_txt_in` / `offload_img_in` toggles for the text/image embedding layers.
2. **Weight streaming / `--novram` smart memory** — ComfyUI core's generic mode that keeps weights in RAM and pages whole modules per sampler step. This is what LTX-2.3 native workflows use ("With ComfyUI, it can use weight streaming (offloading), so it can work on less than 32GB VRAM too, but can be significantly slower" — [Lightricks RTX Quick Start, Jan-2026](https://www.reddit.com/r/StableDiffusion/comments/1q5b1aw/new_quick_start_guide_for_ltx2_in_comfyui_on/)).

**Latency penalty (3090, PCIe 4.0 x16, DDR4 64 GB):**

- A 1 GB tensor moves CPU↔GPU in ~50 ms over PCIe 4.0 x16 (theoretical 32 GB/s; practical ~20 GB/s). A 14B-class video transformer has 30–40 swappable blocks × ~0.5–1 GB each. For Wan 2.2 14B on 24 GB with `block_swap=10`, the [r/StableDiffusion Aug-2025 thread](https://www.reddit.com/r/StableDiffusion/comments/1mfzvl5/debate_best_wan_22_t2v_settings_steps_sampler_cfg/) reports **>40 minutes** to render 81 frames @ 720p — i.e., the swap dominates wall time once you push past ~20 blocks.
- For Wan 2.2 5B (no swap needed), `~3–5 min` is the unoffloaded baseline (existing §6).
- For LTX-2.3 13B FP8 with weight streaming on 64 GB RAM, real-world numbers from January 2026: **2:10 for 720p / 5 s on RTX 3090 + 64 GB RAM** ([FB ComfyUI group, Jan-2026](https://www.facebook.com/groups/comfyui/posts/884945090944859/)); **~6:43 for 720p with audio** ([FB ComfyUI Mar-2026](https://www.facebook.com/groups/592850912909945/posts/1295579505970412/)). The `~140 s` Tim cited is consistent with these.
- HunyuanVideo 1.5 native on 3090 Ti with TensorRT and offloading: **<4 min for 1080p / 5 s** ([Quasa](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source)). 13.6 GB BF16 fits cleanly in 24 GB so swap is mostly unnecessary; this is the cheapest-to-run quality model on this list.

**Architecture suitability for swap:**

- LTX-2 / LTX-2.3 is a **DiT** with relatively independent transformer blocks; the Lightricks ComfyUI nodes ship weight-streaming-friendly workflows out of the box ([Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)). The known fragility is *not* in the transformer — it's in the second-stage upscaler / VAE step, where ComfyUI's smart memory fails to release VRAM between samplers and OOMs at 1080p ([Comfy-Org #11726, Jan-2026](https://github.com/Comfy-Org/ComfyUI/issues/11726)). On a 3090 with 24 GB and `--cache-none --disable-smart-memory`, this is a workaround; on 16 GB it's unworkable.
- Wan 2.2 14B MoE swaps "well" architecturally but the *combined* model is huge and 10+ blocks of swap pushes per-step time past breaking ([r/StableDiffusion Aug-2025](https://www.reddit.com/r/StableDiffusion/comments/1mfzvl5/debate_best_wan_22_t2v_settings_steps_sampler_cfg/)). The kijai wrapper also has a documented memory-leak class of bug where RAM creeps across sequential runs ([WanVideoWrapper #805, Jul-2025](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/805); [#1644 BlockSwap broken, Nov-2025](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/1644)) — operational hazard on a long-lived ComfyUI process.
- HunyuanVideo's BlockSwap (`HyVideoBlockSwap`, kijai) is mature and rarely needed at 24 GB since the model is 13.6 GB BF16.

**Verdict:** offloading is a real, sourced fit strategy and lets us run **LTX-2.3 13B** and **HunyuanVideo 1.5** comfortably on a 3090. It does **not** rescue Wan 2.2 14B for interactive use — the >40-minute number is not interactive at any UX.

### D. Re-ranked recommendation

Honest scoring under the new criteria (1 = bad, 5 = great):

| Model | Quality | Ecosystem | Latency on 3090 | VRAM fit (offload OK) | License | Stability | Notes |
|---|---|---|---|---|---|---|---|
| **LTX-2.3 13B (full, FP8 + weight streaming)** | 4 | 5 | 4 (~2–7 min/5 s 720p) | 4 (FP8 ~14–20 GB; streaming on 24 GB clean) | 3 (OpenRAIL-M, use restrictions) | 3 (sampler/VAE OOM at 1080p, [#11726](https://github.com/Comfy-Org/ComfyUI/issues/11726); 720p stable) | Native audio. Up to 20 s. Lightricks ships first-party nodes ([Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo)). |
| **HunyuanVideo 1.5** | 5 (per-VBench/Arena claims) | 3 (younger, fewer LoRAs) | 4 (<4 min/5 s 1080p, 3090 Ti TensorRT) | 5 (13.6 GB BF16, no swap needed) | 2 (territorial Tencent license, MAU cap) | 4 | Best quality-per-VRAM. Gated to personal use. |
| **Wan 2.2 14B MoE** | 5 (cinematic / camera control) | 5 | 1 (>40 min/5 s 720p with `block_swap=10`) | 2 (24 GB is OOM-edge even with swap) | 5 (Apache-2.0) | 2 (BlockSwap leaks, [#805](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/805) / [#1644](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/1644)) | Best photoreal but disqualified on latency for interactive use. |
| **Wan 2.2 TI2V-5B** (original primary) | 3 | 5 | 5 (3–6 min/5 s 720p) | 5 (8–10 GB FP8) | 5 | 5 | Solid but quality-capped; can't go past ~768p without trouble. |

**New recommended primary: LTX-2.3 13B (full, FP8 quant, weight streaming).** Reasoning:

1. **Quality.** Beats Wan 2.2 5B on resolution ceiling and longer durations; trades blows with Wan 2.2 14B on detail (Wan wins texture/camera; LTX wins motion fluidity, audio, length). The MoE 14B's slight quality lead doesn't survive its 40-minute latency on a 3090.
2. **Ecosystem.** Lightricks ships first-party ComfyUI nodes weekly; LTX-2.3 was released 2026-03-05 and already has audio + 4K paths.
3. **Latency on a 3090.** 2–7 min for 720p / 5 s with weight streaming and 64 GB RAM is well within the "request and walk away" envelope.
4. **VRAM with offloading.** FP8 13B + weight streaming fits comfortably in 24 GB next to ComfyUI image-gen, which is exactly the fit pattern the r/StableDiffusion 3060 thread describes — except we have 2× the VRAM and 2× the RAM.

**HunyuanVideo 1.5 added as a `model: "hunyuan-video-1.5"` opt-in alias** for personal use; it is genuinely the best-quality option per VBench but the license keeps it off the public-by-default surface. Wan 2.2 TI2V-5B stays as a `model: "wan-2.2-ti2v-5b"` *fallback* alias for cases where stability or photoreal-texture matters more than fluidity. Wan 2.2 14B is **not** added; latency is disqualifying.

API-surface consequence (§5 amendment): per-model `seconds` caps tighten — `ltx-2.3` accepts 2–20 s, `hunyuan-video-1.5` 2–10 s, `wan-2.2-ti2v-5b` 2–8 s. The proxy must reject above per-model max.

### E. The r/StableDiffusion 3060 thread — what it tells us about a 3090

Direct read of the thread Tim cited ([r/StableDiffusion 3060 12 GB / 32 GB RAM](https://www.reddit.com/r/StableDiffusion/comments/1t5tq7e/recommendation_for_rtx_3060_12_vram_32_gb_ram/), 2026-05; the page is gated to logged-in Reddit but the search-engine snippets are explicit) and corroborating sources:

- The headline data point — "5-second 1920×800 in 280 s, 720p in 140 s on LTX 2.3" — is from a 3060 12 GB / 32 GB RAM build using Q4–Q6 GGUF quants of LTX-2.3 with Wan2GP-style aggressive offload, Sage attention, and Tiny VAE preview. Confirmed by the [zenvanriel guide](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/) and the [Lightricks RTX Quick Start](https://www.reddit.com/r/StableDiffusion/comments/1q5b1aw/new_quick_start_guide_for_ltx2_in_comfyui_on/) ("works on less than 32 GB VRAM too, but can be significantly slower").
- 3060 users are solving for **fit**: just-make-it-run. They accept Q4 quants, distilled variants, Tiny VAE previews, and 8 GB-class compromises because the alternative is can't-run-at-all.
- A 3090 has **2× the VRAM and 2× the RAM**. We don't have to choose. We can run **FP8 13B LTX-2.3 (not Q4 distilled)** with weight streaming and get better quality than the 3060 user, at comparable wall-time to their distilled Q4 — because we never have to compromise on quant.
- The techniques the 3060 thread uses (Sage attention, Tiny VAE previews, GGUF quants, Wan2GP) are still useful on a 3090 as **optimisations**, not survival kit. Sage attention + FP8 + weight streaming on a 3090 is the right baseline for LTX-2.3 13B; we can drop Wan2GP entirely (per §4 of original; ComfyUI is already running).

**Translation for our 3090 build:** the 3060 thread proves LTX-2.3 13B is operable on 12 GB with offload — so 24 GB is a comfortable home for it, not a tight squeeze.

### F. Open-questions delta

The original §9 questions still apply. New / promoted ones from quality-first reranking:

1. **I2V vs T2V split (was Q3, now Q1).** Wan TI2V-5B handled both natively; LTX-2.3 has separate I2V and T2V workflows with different VRAM profiles ([Lightricks/ComfyUI-LTXVideo example_workflows](https://github.com/Lightricks/ComfyUI-LTXVideo)). Need to decide: ship both `ltx-2.3-t2v` and `ltx-2.3-i2v` as model aliases, or one workflow with mode switching?
2. **HunyuanVideo as default-or-opt-in.** If Tim is fine with a personal-use license gate (no public exposure), Hunyuan 1.5 is arguably *the* primary on quality. Want it gated behind `auth.role=admin`, or off entirely?
3. **VAE-OOM workaround for LTX-2.3 1080p ([Comfy-Org #11726](https://github.com/Comfy-Org/ComfyUI/issues/11726)).** Acceptable to cap LTX output at 720p in v1, or invest in the `--cache-none --disable-smart-memory` workaround now?
4. **Audio path on LTX-2.3.** Native single-pass audio is the killer feature vs Wan and Hunyuan. Worth wiring as an opt-in `with_audio: true` parameter in v1, or defer?
5. **Long-form ceiling.** LTX up to 20 s changes the per-model `seconds` cap and the disk-retention story (one 20 s clip ≈ 30 MB, not 8 MB). Confirm 24 h retention still right.

(Original §9 Q1 — "longer-form with audio" — collapses into Q4 above. Q3 promotes to Q1. Everything else stands.)

### G. Sources added in this addendum

- [WaveSpeed: LTX-2.3 vs WAN 2.2 head-to-head, Mar-2026](https://wavespeed.ai/blog/posts/ltx-2-3-vs-wan-2-2-comparison-2026/) — primary quality comparison, 4090 test bench.
- [neurocanvas: WAN 2.2 vs LTX-2 winners-by-attribute](https://neurocanvas.net/blog/wan-2-2-vs-ltx-2-comparison/) — corroborates WaveSpeed; explicit "tight squeeze" verdict on 14B at 24 GB.
- [arXiv 2512.22274 GeCo geometric-consistency benchmark, Apr-2026](https://arxiv.org/html/2512.22274v2) — independent academic comparison of WAN 2.2, HunyuanVideo, LTX-Video, CogVideoX vs Sora 2 / Veo 3.1.
- [Quasa: HunyuanVideo 1.5 hands-on](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source) — VBench / GenAI-Arena claims (note license error in the article).
- [zenvanriel: LTX-2.3 deployment guide](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/) — 12 GB minimum, 16 GB+ comfortable, FP8 sweet spot.
- [Comfy-Org/ComfyUI #11726 LTX-2 inter-sampler VRAM bug, Jan-2026](https://github.com/Comfy-Org/ComfyUI/issues/11726) — known fragility.
- [kijai/ComfyUI-WanVideoWrapper #805 BlockSwap RAM leak](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/805) and [#1644 BlockSwap broken Nov-2025](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/1644) — operational hazards for Wan 2.2 14B path.
- [r/StableDiffusion: best Wan 2.2 t2v settings](https://www.reddit.com/r/StableDiffusion/comments/1mfzvl5/debate_best_wan_22_t2v_settings_steps_sampler_cfg/) — 81 frames @ 720p with `block_swap=10` >40 min on 24 GB.
- [r/StableDiffusion: Lightricks LTX-2 RTX Quick Start, Jan-2026](https://www.reddit.com/r/StableDiffusion/comments/1q5b1aw/new_quick_start_guide_for_ltx2_in_comfyui_on/) — "weight streaming works on <32 GB VRAM, significantly slower."
- [FB ComfyUI group: LTX-2 720p / 3090 / 64 GB / 2:10, Jan-2026](https://www.facebook.com/groups/comfyui/posts/884945090944859/) and [Mar-2026 audio test, 403 s on 3090](https://www.facebook.com/groups/592850912909945/posts/1295579505970412/) — concrete 3090 wall-times.
- [RunComfy: HyVideoBlockSwap node reference](https://www.runcomfy.com/comfyui-nodes/ComfyUI-HunyuanVideoWrapper/hy-video-block-swap) — block-swap mechanics (0–40 single blocks, txt/img layer offloads).
- [Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo) — first-party LTX nodes / example workflows.
- [Spheron GPU cheat sheet 2026](https://www.spheron.network/blog/gpu-requirements-cheat-sheet-2026/) — datacenter VRAM numbers for Wan 2.2 720p (65–80 GB H100) confirming 14B is *native*-unfit on 24 GB.

---

## Addendum 2: Reconciled with gpu_budget.sh (2026-05-07)

The first addendum framed the budget as "a 24 GB card." That is the card's total, not what's *available* to a video workload given the services already pinned to GPU0. Reading the actual budget changes the recommendation.

### A. The actual budget

From `~/applications/config/gpu_budget.sh`:

```
GPU0_DESKTOP=3       # Xorg / window manager
GPU0_TTS=4           # voxtral / TheMHD1 int4
GPU0_COMFYUI=2       # ComfyUI baseline (image-gen idle)
GPU0_HEADROOM=15     # absorbs ComfyUI peaks (flux-dev 8–12 GB)

GPU1_LLM=23          # llama-turbo
GPU1_HEADROOM=1
```

Two binding constraints:

1. **GPU0 peak budget for video = 8–10 GB**, not 24 GB. The 15 GB "headroom" is already earmarked for ComfyUI image-gen peaks (flux-dev hits 8–12 GB per the file's own comment). A video workload that consumes the full 15 GB excludes image-gen entirely; the practical sustainable share, assuming we want to keep image-gen working in the same ComfyUI process, is **8–10 GB peak**. Live `nvidia-smi` confirms ~15 GB used / ~8.5 GB free at idle.
2. **GPU1 is unavailable** — owned by llama-turbo at 23 GB. We don't touch it.
3. **System RAM ≥ 64 GB free** for offload — well above the 32 GB the 3060 build in [r/StableDiffusion 1t5tq7e](https://www.reddit.com/r/StableDiffusion/comments/1t5tq7e/recommendation_for_rtx_3060_12_vram_32_gb_ram/) used to run LTX-2.3.

The budget *does* permit 14–20 GB resident if we're willing to suspend image-gen during a video job — but the §7 coexistence policy explicitly wants both available. So 8–10 GB is the right working ceiling.

### B. Per-model peak VRAM and latency under the real budget

All numbers are 720p / 5 s on a 3090 (Ampere — **no native FP8**; FP8 weights run at FP16 speed per [Comfy-Org #2970 community benchmarks](https://github.com/Comfy-Org/ComfyUI/discussions/2970)). Quality vs. fully-resident is unchanged for all candidates: offloading affects timing only ([ComfyUI async offload blog post, Dec-2025](https://blog.comfy.org/p/new-comfyui-optimizations-for-nvidia)).

| Candidate | Peak VRAM (aggressive offload) | Wall time, 720p/5s on 3090 | Source |
|---|---|---|---|
| **LTX-2.3 13B GGUF Q8 + block-swap** | ~9–11 GB | ~3–5 min | [unsloth/LTX-2.3-GGUF](https://huggingface.co/unsloth/LTX-2.3-GGUF), [civitai 2443867 12GB GGUF workflow](https://civitai.com/models/2443867/ltx-23-22b-gguf-workflows-12gb-vram), [HF Lightricks/LTX-2.3 #16](https://huggingface.co/Lightricks/LTX-2.3/discussions/16) (5090 FP8 = 82 s / 481 frames @ 720p; 3090 ≈ 2.5–3× slower) |
| **LTX-2.3 13B FP8 + weight streaming** (prior recommendation) | ~14–20 GB | ~2:10 reported on 3090 / 64 GB RAM | [FB ComfyUI Jan-2026](https://www.facebook.com/groups/comfyui/posts/884945090944859/) — but exceeds the 8–10 GB GPU0 budget; would have to evict ComfyUI image-gen first |
| **LTX-2.3 distilled 2B Q4 GGUF** | ~5–7 GB | ~30–90 s ([wavespeed quickstart](https://wavespeed.ai/blog/posts/blog-ltx-2-comfyui-quickstart/)) | Fits cleanly under image-gen co-tenancy |
| **Wan 2.2 TI2V-5B GGUF Q8** (3090-correct quant) | ~8–10 GB resident | ~5–10 min ([dev.to 8 GB workflow](https://dev.to/aitechtutorials/run-wan-22-in-comfyui-with-just-8gb-vram-full-image-to-video-ai-workflow-2gb6); Q8 on 3060/3080-class typically 7 min @ 1024×574 per [HF Wan2.2-Animate #4](https://huggingface.co/Wan-AI/Wan2.2-Animate-14B/discussions/4)) | At the budget edge |
| **Wan 2.2 TI2V-5B FP8** (note: no 3090 hw acceleration) | ~8–10 GB resident | ~7–12 min — FP8 slower than Q8 on 3090 per [Comfy-Org #2970](https://github.com/Comfy-Org/ComfyUI/discussions/2970) ("Q8 results are slower than FP8 on the 3090, which is actually running at the same speed as FP16") | **Disqualified** — FP8 is the wrong quant for Ampere |
| **HunyuanVideo 1.5 BF16 + HyVideoBlockSwap** | 13.6 GB native; with `block_swap=20` ~7–9 GB | ~6–10 min on 3090 (no TensorRT); the [Quasa](https://quasa.io/media/tencent-s-hunyuanvideo-1-5-just-made-hollywood-level-video-generation-truly-open-source) "<4 min" figure is 3090 Ti + TensorRT, not vanilla 3090 | License-gated (territorial) |

### C. Offload-mode latency penalty

The penalty for streaming weights vs. fully resident on a 3090 with PCIe 4.0 x16 + DDR4:

- **Flux-DEV reference (image, partial unload)**: 16.71 s resident → 29.81 s with partial unload — **1.78× penalty** ([Comfy-Org #9002](https://github.com/Comfy-Org/ComfyUI/discussions/9002)).
- **ComfyUI's own async-offload + pinned-memory path** is enabled by default since Dec 2025 and "broadly applicable to all NVIDIA GPUs" ([ComfyUI blog Dec-2025](https://blog.comfy.org/p/new-comfyui-optimizations-for-nvidia)).
- **LTX-2.3 architecture-specific**: the DiT block layout is swap-friendly (per [Lightricks RTX Quick Start](https://www.reddit.com/r/StableDiffusion/comments/1q5b1aw/new_quick_start_guide_for_ltx2_in_comfyui_on/) — "weight streaming works on <32 GB VRAM, significantly slower"). Combined with the 3060 thread evidence (LTX-2.3 Q4 on 12 GB / 32 GB RAM = ~140 s for 720p / 5 s), a 3090 with 64 GB RAM running Q8 with light block-swap should finish in **~3–5 min** — about 1.5–2.5× the resident baseline of ~2:10, well within the [Flux 1.78×] envelope.
- **Wan 2.2 14B with `block_swap=10`** is the cautionary case: >40 min for 81 frames @ 720p ([r/StableDiffusion 1mfzvl5](https://www.reddit.com/r/StableDiffusion/comments/1mfzvl5/debate_best_wan_22_t2v_settings_steps_sampler_cfg/)) — confirms swap is fine for moderate-sized DiTs and disastrous for very large MoE.

**Bottom line: 1.5–2× penalty is the realistic case for LTX-2.3 13B with block-swap on a 3090. The 6–10× catastrophic case only shows up for the Wan 14B MoE — not on the table here.**

### D. Order-of-magnitude check (Tim's hard constraint)

| Comparison | Ratio | Verdict |
|---|---|---|
| LTX-13B Q8 + block-swap (~3–5 min) vs LTX-13B FP8 resident (~2:10) | ~1.5–2× | **In tolerance** |
| LTX-13B Q8 + block-swap (~3–5 min) vs Wan-5B Q8 resident (~5–10 min) | LTX is **faster**, not slower | Trivially in tolerance |
| LTX-13B Q8 + block-swap (~3–5 min) vs Cloud Sora 2 (~30–60 s) | ~5× cloud-faster | Outside our control; not the comparison Tim flagged |
| Wan-14B with `block_swap=10` (>40 min) vs anything | 8–20× | **Order-of-magnitude blow-up — disqualified, as already flagged in addendum 1** |

The recommendation below stays inside the "no order-of-magnitude regression" envelope.

### E. New recommended primary: **LTX-2.3 13B GGUF Q8 + block-swap**, with LTX-2.3 distilled 2B as the preview alias

Reasoning:

1. **Quality.** Same model as addendum 1's recommendation — Q8 GGUF preserves quality vs FP8/BF16 within community-reported "5–8% softening of fine detail, identical motion coherence" ([dev.to LTX guide](https://www.vgoodslab.com/run-ltx-video-on-12gb-vram-a-step-by-step-comfyui-gguf-workflow-guide/), [zenvanriel](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/)). On a 3090, **Q8 is the right quant** because FP8 has no hardware acceleration on Ampere ([Comfy-Org #2970](https://github.com/Comfy-Org/ComfyUI/discussions/2970)).
2. **Fits the actual budget.** Q8 13B with block-swap holds ~9–11 GB peak — inside the 8–10 GB working ceiling with modest tightening (or the full 15 GB headroom if image-gen is suspended for the request).
3. **Latency stays in the 3–5 min envelope** for 720p / 5 s. ~1.5–2× the FP8-resident baseline — within "no order-of-magnitude regression."
4. **Co-tenancy with image-gen survives.** Ample RAM (64 GB) absorbs the swapped blocks; ComfyUI's `--async-offload` is on by default; the [Comfy-Org #11726 VAE/smart-memory bug](https://github.com/Comfy-Org/ComfyUI/issues/11726) still applies at 1080p so cap LTX output at 720p in v1 (already an open question, Q3 of addendum 1 §F).

**Aliases (updated from §3 / addendum 1):**

- `ltx-2.3` (default) — 13B GGUF Q8 + block-swap, 720p, 2–10 s.
- `ltx-2.3-fast` (preview) — distilled 2B Q4, 720p, 30–90 s.
- `wan-2.2-ti2v-5b` (fallback for cinematic / texture-heavy) — GGUF Q8, not FP8, ~5–10 min.
- `hunyuan-video-1.5` (opt-in, license-gated) — BF16 + HyVideoBlockSwap, ~6–10 min.

Wan 2.2 14B remains disqualified (latency).

### F. Open-questions delta (append-only)

Adding to §9 / addendum 1 §F:

9. **GPU0 budget realism.** Should video generation hold to the 8–10 GB working ceiling (image-gen co-tenant), or is it acceptable to fully suspend ComfyUI image-gen during a video job and consume the whole 15 GB headroom? The latter unlocks LTX-2.3 13B FP8-resident path (faster — ~2:10 — but Ampere has no FP8 acceleration so on a 3090 it's effectively the same wall time as Q8 anyway).
10. **Quant selection on a 3090.** Confirm: GGUF Q8 over FP8 for all 3090 video workloads, since FP8 has no Ampere hardware path. (This contradicts addendum 1's "FP8 + weight streaming" framing — appended here, not retracted there.)
11. **Block-swap operator-burden.** kijai-wrapper memory leaks ([WanVideoWrapper #805](https://github.com/kijai/ComfyUI-WanVideoWrapper/issues/805)) are well-documented for Wan; Lightricks's first-party LTX nodes do not have a comparable bug log. Worth periodic ComfyUI restart in cron, or trust the daemon over multi-day uptime?

### G. Sources added in this addendum

- [r/StableDiffusion 1t5tq7e — 3060 12 GB / 32 GB RAM LTX-2.3 thread (gated)](https://www.reddit.com/r/StableDiffusion/comments/1t5tq7e/recommendation_for_rtx_3060_12_vram_32_gb_ram/) — the "140 s for 720p / 5 s" data point. Page is logged-in only; corroborated indirectly via [zenvanriel](https://zenvanriel.com/ai-engineer-blog/ltx-2-3-open-source-video-generation-guide/) and [civitai 2443867](https://civitai.com/models/2443867/ltx-23-22b-gguf-workflows-12gb-vram). **Not directly verified.**
- [unsloth/LTX-2.3-GGUF](https://huggingface.co/unsloth/LTX-2.3-GGUF) — Q4..Q8 GGUF quants of LTX-2.3.
- [HF Lightricks/LTX-2.3 discussion #16 — Render Times](https://huggingface.co/Lightricks/LTX-2.3/discussions/16) — RTX 5090 FP8 = 82 s / 481 frames @ 720p; 3090 extrapolation = ~3–4× slower → ~3–5 min for 121 frames @ 720p, consistent with our envelope.
- [Comfy-Org/ComfyUI Discussion #2970 — GPU benchmark thread](https://github.com/Comfy-Org/ComfyUI/discussions/2970) — "3090 doesn't support FP8 and will run at FP16 speed; use GGUF on 3090, FP8 on 5070+." Drives the Q8-not-FP8 selection.
- [Comfy-Org/ComfyUI Discussion #9002 — Flux DEV FP8 5090/4090/3090 benchmark](https://github.com/Comfy-Org/ComfyUI/discussions/9002) — concrete 1.78× partial-unload penalty (16.71 s → 29.81 s).
- [ComfyUI blog — async offload + pinned memory, Dec-2025](https://blog.comfy.org/p/new-comfyui-optimizations-for-nvidia) — `--async-offload` enabled by default for all NVIDIA GPUs.
- [vgoodslab LTX-Video 12 GB GGUF workflow guide](https://www.vgoodslab.com/run-ltx-video-on-12gb-vram-a-step-by-step-comfyui-gguf-workflow-guide/) — operational details for low-VRAM LTX runs (Sage attention, Tiny VAE, --novram).
- [HF Wan2.2-Animate-14B discussion #4](https://huggingface.co/Wan-AI/Wan2.2-Animate-14B/discussions/4) — Wan 2.2 Q8 GGUF on 5060 Ti 16GB = ~7 min for 1024×574 / 5 s, our closest direct Wan-Q8 latency anchor.


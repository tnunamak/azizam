# Core Contracts

Date: 2026-05-15
Status: live proxy integration in progress

This document defines the smallest set of stable contracts the gateway needs
before it grows more endpoints, adapters, or scheduler behavior.

The goal is not to freeze implementation details. The goal is to make every
important boundary explicit enough that compatibility, routing, native power
use, and diagnostics can evolve without turning into one large special case.

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
  the `/gateway/v1/media/jobs` surface.

Backend adapter extraction, a richer live capability inventory, resource
claims, and asset serving described below are still in progress.

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
should not skip these concepts once a request affects routing, native controls,
credentials, resource use, or durable assets.

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
intent: chat.generate
model: qwen3-coder
backend: llama.cpp.gpu1
input_modalities: ["text"]
output_modalities: ["text"]
context_window: 65536
streaming: true
tools: partial
state: warm
available: true
limits:
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
- streaming support
- lifecycle state
- health availability
- policy availability
- resource estimate
- caveats that affect correctness

## App Profile

An app profile is a named policy bundle tied to an API key or client identity.
Profiles live as optional `profile:` blocks on each key entry in
`proxy_api_keys.yml` and hot-reload alongside the keys that carry them.
Missing blocks fall back to `DEFAULT_PROFILE` (local-only, basic visibility)
so existing keys keep working unchanged.

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

Protocol strings are concrete (e.g. `openai.chat`, `openai.images`,
`openai.audio`, `openai.responses`, `anthropic.messages`, `ollama.chat`,
`ollama.generate`). Allow-list semantics are conservative: an omitted list
means "no opinion" for that dimension; an empty list means "deny all".

Profiles are where the gateway maps:

- which protocols a client can use
- which workflows, models, and backends it may target
- whether cloud fallback is allowed
- which credentials may be used downstream
- how much status, discovery, and receipt detail it may see
  (`status_visibility.level`: `none` / `basic` / `standard` / `full` →
  client / client / admin / internal audience tiers)
- which native passthrough lanes are allowed

Backend-native power knobs (denoise, controlnet strength, model-specific
controls) belong under namespaced request extensions
(`extensions.com.vividfish.workflow.*`), not as global OpenAI fields. Native
passthrough remains opt-in, scoped per profile, and is not a blanket ComfyUI
DSL.

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

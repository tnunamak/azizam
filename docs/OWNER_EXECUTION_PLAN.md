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
- [x] Live Anthropic Messages and Ollama chat/generate/tags/show/ps/version frontends over the existing chat route, including Anthropic SSE and Ollama NDJSON streaming
- [x] Live OpenAI and Ollama embeddings routed to a dedicated CPU `nomic-embed-text-v1.5` backend with no GPU budget claim
- [x] Live image workflow extension binding for supported manifest controls
- [x] `generate-key` preserves existing `profile:` blocks on rotation
- [x] SDK compatibility harness with executable registered probes (`scripts/compat/`, `tests/compat/`, `docs/SDK_COMPATIBILITY_HARNESS.md`)

In-progress / next integration targets:

- Expand the live capability inventory beyond the initial hand-built catalog.
- Extend durable media-job execution to video and audio/music.
- Expand SDK compatibility probes for Anthropic/Ollama streaming, Ollama read-only model routes, and embeddings.
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
- [x] `GET /gateway/v1/media/jobs/{job_id}/events`
- [x] `POST /gateway/v1/media/jobs/{job_id}/events`
- [x] `POST /gateway/v1/media/jobs/{job_id}/cancel`

### 5. Media Jobs — storage/API first slice done

- [x] durable job record
- [x] audience-filtered API surface
- [x] image job content retrieval through `/gateway/v1/media/jobs/{job_id}/content`
- [x] ComfyUI image-generation adapter integration for long-running jobs
- [x] ComfyUI audio/music/SFX adapter integration for long-running jobs
- [x] generic `/gateway/v1/assets/*` serving
- [x] pure audio/video media-job contract helpers for safe metadata, assets, and route-shaped status payloads
- [x] pure resource-admission decision core for service availability, reserved resources, queue depth, and per-app concurrency facts
- [ ] ComfyUI video adapter integration for long-running jobs

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
- Ollama clients against `/api/chat`, `/api/generate`, `/api/embed`, and `/api/tags`

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

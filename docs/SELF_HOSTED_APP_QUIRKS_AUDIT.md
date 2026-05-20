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

The proxy originally included explicit route handlers plus a catch-all `/{path:path}` that forwarded other methods and paths to the LLM backend. That was useful for backend admin/model-load endpoints, but it blurred the product boundary. The current contract is stricter: unknown public routes return 404, and backend-native/provider-native access must be exposed through explicit, policy-gated namespaces.

Architecture requirement:

- native passthrough lanes should be explicit and scoped
- per-key policy should decide whether native passthrough is allowed
- passthrough should be visible in route receipts and logs
- public protocol frontends should not accidentally expose backend admin APIs

Open question:

- Add backend-native passthrough under namespaced paths such as `/gateway/v1/providers/tabby/*` when we are ready to support it publicly.

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

# Known Work

Date: 2026-05-28
Status: canonical owner backlog for current `main`

This file is the source of truth for outstanding known work. Historical worker
notes, scratch reports, and `tmp/workstreams/*` are useful evidence, not the
backlog. If a task is not here or in a linked machine-readable matrix, treat it
as untriaged until revalidated.

Current baseline:

- GitHub has no open issues for this repo at the time this file was created.
- The core test suite, Python compatibility matrix, and Node SDK probes passed
  on the latest pushed main before this note.
- Runtime product state is DB/dashboard-owned; bootstrap YAML/env are seed and
  early-start inputs only. See `docs/CONFIG_SOURCE_OF_TRUTH.md`.
- LiteLLM is the commodity LLM execution substrate. Gateway code still owns
  product policy, discovery, media jobs, assets, receipts, protocol edges, and
  non-LLM backends.

## Active Core Work

### 1. Dynamic Capability And Service Inventory

The live capability surface still needs to become more complete and more
data-driven. The target is not "one bigger hard-coded catalog"; it is a merged
view of service registry records, workflow manifests, adapter health, model
inventory, resource estimates, and profile policy.

Done means:

- `/gateway/v1/capabilities`, `/gateway/v1/workflows`, `/gateway/v1/providers`,
  `/v1/models`, `/ollama/api/tags`, and admin service views agree on the same
  underlying facts.
- Disabled/down/unconfigured services are visible with clear reasons.
- Advertised capabilities map to actual routes or explicit unsupported errors.
- The user-facing list uses friendly descriptions instead of exposing raw tags
  as the primary interface.

Primary references: `docs/API_TARGETS.md`, `docs/CORE_CONTRACTS.md`,
`docs/SLVP_SCOPE.md`.

### 2. Backend Adapter Contract Extraction

`proxy.py` still contains too much backend-specific request execution. The
contract is known: protocol request -> canonical intent -> App/Profile policy
-> capability match -> resource claim -> backend binding -> receipt/events.
The next extraction should be narrow and behavior-preserving.

Done means:

- P0 backend families expose a shared adapter shape for health, inventory,
  invoke, cancellation where available, and native-operation metadata.
- Local LLM execution stays behind LiteLLM where LiteLLM fits.
- Media and workflow engines such as ComfyUI, TTS, STT, music/SFX, and video
  use gateway adapters rather than route-local special cases.
- Provider-native passthrough stays under `/gateway/v1/providers/*`; there is
  no root catch-all backend shim.

Primary references: `docs/API_TARGETS.md`, `docs/CORE_CONTRACTS.md`,
`docs/LITELLM_SUBSTRATE_DECISION.md`.

### 3. Resource Admission And Mode Controls

The pure admission core and media-job constraints exist, but the product slice
is not fully closed. Self-hosted AI is resource constrained: loaded models,
VRAM, queues, disabled services, and concurrent users must be legible before a
request starts expensive work.

Done means:

- Admins can see service availability, queues, resource estimates, and the
  reason a request would be admitted, queued, or rejected.
- Profiles can express simple limits that matter in practice: protocol access,
  model/workflow access, native passthrough, queue/concurrency caps, and
  expensive-media restrictions.
- Runtime responses return honest 403/409/429/503 errors with actionable
  reasons and `Retry-After` where appropriate.
- Full scheduler preemption or multi-worker event storage remains deferred
  unless a concrete deployment needs it.

Primary references: `docs/CORE_CONTRACTS.md`, `docs/CONFIG_SOURCE_OF_TRUTH.md`.

### 4. Long-Tail App Verification

SDK probes exist for OpenAI Node/Python, Anthropic Node/Python, Ollama JS/Python,
and Vercel AI SDK. The remaining work is not more curls; it is app/client-shaped
smoke coverage for the targets self-hosters actually run.

Track target status in
`tests/compat/matrix/long_tail_targets.json` first, then sync
`docs/COMPATIBILITY_TARGET_MATRIX.md`.

Highest-value unclosed targets:

- Open WebUI automated smoke beyond manual status.
- LibreChat streaming/tool-call/media behavior.
- Continue chat/completions/embeddings behavior.
- Vercel AI SDK cases not covered by the current Node probe.
- Cline, SillyTavern, n8n, and Home Assistant smoke probes.

Primary references: `docs/COMPATIBILITY_TARGET_MATRIX.md`,
`docs/SDK_COMPATIBILITY_HARNESS.md`, `tests/compat/README.md`.

### 5. Media, Jobs, And Assets Honesty

The gateway has durable job and asset paths for image/audio/music/SFX/video, but
every advertised media API still needs live compatibility pressure. The bar is
simple: works through the claimed route, or returns a protocol-shaped explicit
unsupported/error response.

Done means:

- OpenAI Images, Audio, and Video-shaped paths are verified against current
  local services and SDK probes where available.
- Gateway-native media jobs cover long-running image/audio/music/SFX/video with
  durable polling, cancellation, receipt linkage, and asset retrieval.
- Workflow-specific controls such as ControlNet, masks, denoise, seeds, and
  conditioning images are discoverable through extension/job schemas, not
  silently dropped.
- Backend failures do not look like successful empty responses.

Primary references: `docs/CONDITIONING_CONTROLS.md`,
`docs/LOCAL_CLIENT_RECIPES.md`, `tests/compat/python/probe_openai_responses_media.py`.

### 6. Extension Package Validation And Install UX

The extension architecture is documented and partial code exists for package
review. It still needs enough real packages to prove that providers, services,
workflows, capabilities, routes, and adapter/translator declarations can move
out of core code without becoming unsafe or confusing.

Done means:

- At least a small bundled package set proves protocol/frontend, provider,
  service, workflow, capability, and test-case objects.
- Imported packages from local directory and Git/GitHub URL go through stage,
  review, validate, configure, test, enable.
- Data-only packages can be enabled safely; code-bearing packages require an
  explicit operator trust decision before runnable adapter/translator code is
  used.
- Enabled records store provenance and content hashes; live runtime does not
  execute mutable source directories by surprise.

Primary reference: `docs/EXTENSION_ARCHITECTURE.md`.

### 7. Provisioning Export And Restore

The first-run story is DB/dashboard-first and current runtime keys live in the
admin DB. The remaining operator gap is an as-code export/import story for
non-secret dashboard-owned settings.

Done means:

- Exportable product state is derived from the same source-of-truth inventory
  used by the dashboard.
- Secrets are redacted or referenced, never exported raw.
- Provisioned service rows can round-trip without becoming UI-owned by
  accident.
- The first-run path still works without requiring this feature.

Primary reference: `docs/CONFIG_SOURCE_OF_TRUTH.md`.

### 8. Declared, Visible Model Aliases For LLM Routing

A client that sends a well-known model name it doesn't have locally (e.g.
`claude-sonnet-4-6`, `gpt-4o-mini`) gets a 400 "Invalid model name" because the
LLM request path (`_prepare_litellm_local_llm_request`) does an exact-match
lookup and the LiteLLM substrate rejects unknown names. Aliasing already exists
for video (`videos.aliases`) and as a metadata/category map
(`MODEL_COMPAT_ALIASES`), and `config_sources.py` already declares the intent
("UI-managed targets and aliases live in admin.db") — but the operator-defined
name→name alias layer for text LLMs is unbuilt and not surfaced in the UI.

This must NOT become silent aliasing: routing `claude-sonnet-4-6` to a local
Qwen without telling the client violates the no-fake-compatibility principle.
The SLVP shape is a **declared, visible** alias: data in the registry/admin.db,
editable in the UI like everything else, applied in the LLM request path, and
recorded in the receipt so "you asked for X, served by local Y" is inspectable.

Done means:

- Operator-defined model aliases live in admin.db (data, not code), editable
  from `/admin`, seedable from `proxy_config.yml`.
- The OpenAI/Anthropic/Ollama LLM paths resolve aliases before LiteLLM, so an
  aliased name routes instead of 400-ing.
- The applied alias is surfaced (receipt + response metadata), never silent.
- Unknown, unaliased models still fail explicitly (no silent default).

Primary references: `docs/CONFIG_SOURCE_OF_TRUTH.md`,
`docs/LITELLM_SUBSTRATE_DECISION.md`.

### 9. LiteLLM Sidecar Config Sync And Visibility

The LiteLLM substrate runs as a sidecar (`start.sh` launches `litellm --config
data/litellm.config.yaml` on :4011); the gateway proxies LLM execution to it.
The gateway compiles the config from the service registry, but nothing
regenerates the file and reloads the sidecar when the registry changes. A live
incident (2026-06-03): the sidecar was running with a stale `model_list: []`,
so every chat/Anthropic/Ollama call 400'd "Invalid model name" while local
kobold/tabby models were registered and healthy. The failure was silent — an
empty model list with no breadcrumb pointing at the sidecar.

Done means:

- The gateway regenerates the LiteLLM config and reloads/restarts the sidecar
  on service-registry change (or at least reconciles on startup).
- `/admin` surfaces the sidecar's live model count so an empty list is visible.
- A stale or empty sidecar produces a diagnosable error, not a generic 400.

Primary references: `docs/LITELLM_SUBSTRATE_DECISION.md`, `start.sh`,
`gateway/litellm_substrate.py`.

## Deliberately Deferred

These are not MVP blockers unless a real target client or local deployment
turns them into core work:

- Full Gemini-native protocol support.
- Full ElevenLabs-compatible protocol support.
- KoboldAI native public compatibility beyond targeted SillyTavern needs.
- A1111/Forge and ComfyUI native public APIs beyond policy-gated provider
  passthrough.
- Bedrock/Vertex full native APIs.
- Fine-tuning, training, Assistants, Files, Batches, and Realtime APIs.
- Hosted provider marketplace behavior.
- Full plugin marketplace/signing infrastructure.
- Scheduler preemption and multi-worker event storage.

## Not Authoritative

These paths may contain useful evidence but should not be treated as current
truth without revalidation:

- `tmp/workstreams/*`
- `.git/workstreams/cards/*`
- `/tmp/*.md` research/audit notes
- old agent transcripts
- stale comments in worker branches

## How To Update Status

- Protocol/API priority changes: update `docs/API_TARGETS.md`.
- App/client target changes: update
  `tests/compat/matrix/long_tail_targets.json`, run
  `python -m unittest tests.test_compat_long_tail_matrix -v`, then sync
  `docs/COMPATIBILITY_TARGET_MATRIX.md`.
- Source-of-truth/config changes: update `docs/CONFIG_SOURCE_OF_TRUTH.md`,
  `gateway/config_sources.py`, and `tests/test_config_sources.py` together.
- Extension model changes: update `docs/EXTENSION_ARCHITECTURE.md` and the
  relevant package validation tests.
- SDK probe changes: update `docs/SDK_COMPATIBILITY_HARNESS.md`; Python probes
  feed `scripts/compat/run_matrix.py`, while Node probes remain in
  `tests/compat/node` until a Node JSON reporter exists.
- Owner execution status changes: update this file first, then keep
  `docs/OWNER_EXECUTION_PLAN.md` as a build-history and design-order document.

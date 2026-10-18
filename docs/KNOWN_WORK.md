# Known Work

Date: 2026-08-11
Status: canonical owner backlog for current `main`, reconciled against code,
tests, and history through `edeb9e9`

This file is the source of truth for outstanding known work. Historical worker
notes, scratch reports, and `tmp/workstreams/*` are useful evidence, not the
backlog. If a task is not here or in a linked machine-readable matrix, treat it
as untriaged until revalidated.

Current baseline:

- Runtime product state is DB/dashboard-owned; bootstrap YAML/env are seed and
  early-start inputs only. See `docs/CONFIG_SOURCE_OF_TRUTH.md`.
- LiteLLM is the commodity LLM execution substrate. Gateway code still owns
  product policy, discovery, media jobs, assets, receipts, protocol edges, and
  non-LLM backends.

## Active Core Work

### 1. Reconcile The Live Capability And Service Inventory

The registry now projects service, workflow, capability, and route facts, and
the capability router selects embeddings, TTS, and STT from declared service
capabilities. The remaining gap is cross-surface truth: model discovery,
workflow discovery, service health, queue facts, and access plan policy do not yet
have one live inventory with an end-to-end agreement proof.

Done means:

- `/gateway/v1/capabilities`, `/gateway/v1/workflows`, `/gateway/v1/providers`,
  `/v1/models`, `/ollama/api/tags`, and admin service views have an explicit,
  tested agreement on their shared facts.
- Disabled/down/unconfigured services are visible with clear reasons.
- Advertised capabilities map to actual routes or explicit unsupported errors.
- The user-facing list uses friendly descriptions instead of exposing raw tags
  as the primary interface.

Primary references: `docs/API_TARGETS.md`, `docs/CORE_CONTRACTS.md`,
`docs/SLVP_SCOPE.md`.

### 2. Backend Adapter Contract Extraction

`proxy.py` still contains too much backend-specific request execution. The
contract is known: protocol request -> canonical intent -> App/access-plan policy
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

### 3. Extend Resource Admission Beyond Media Jobs

The admission core, HTTP error translation, admin controls, and live
image/audio/video media-job checks exist. The remaining product gap is applying
the same truthful admission decision to other expensive or constrained routes,
especially LLM execution, rather than only the media-job paths.

Done means:

- The admission decision covers each constrained route that the gateway
  advertises, with its reason exposed consistently to the caller and admin.
- Access plan limits, service availability, queue/concurrency caps, and resource
  reservations feed that decision without route-local policy copies.
- Denied routes return the documented 403/409/429/503 shape and `Retry-After`
  where a retry is meaningful.
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

### 6. Finish Extension Package Disable/Removal Semantics

Data-owned packages now validate from local directories and Git/GitHub sources,
retain resolved source/provenance/content hashes, and enable registry records
through the admin API. Bundled data and code-bearing review fixtures prove the
boundary: code is staged for review and is not executed merely by installing a
package.

Lifecycle implementation snapshots normalized installed records. Removal deletes
exact matches and detaches edited, copied or unproven legacy rows, restoring
enabled on a row the package had disabled, and reports each affected record.
Disable saves each row's previous enabled state; enable (a reinstall of the same
content hash) restores it without replacing operator edits. The route
`DELETE /admin/api/extensions/{id}` takes `?mode=disable`. Removal answers 409
`extension_package_referenced` when a record that stays, or an access plan,
still names an owned model or service id. A model alias the package owns is
removed with its base model. Registry, index and audit writes commit together
with generation checking.

Still open: access plans are read before the registry transaction, and a plan
write does not bump the registry generation, so a plan saved during a removal
can be missed. The registry has no pool concept; its stored references are
alias records, service config models and access plans.

Done means:

- Disable/remove has explicit, tested effects on every registry record owned by
  the package, with safe handling for operator-copied records.
- The index, service registry, audit trail, and live reload agree after the
  lifecycle change.
- The deliberate deferral remains: no plugin marketplace/signing infrastructure
  or general code-plugin loader is implied.

Evidence: `gateway/admin_runtime.py::remove_extension_package`,
`gateway/extension_packages.py::remove_extension_package`, and
`tests/test_admin_api.py`.

### 7. Complete Provisioning Restore And Reconciliation

An authenticated `GET /admin/api/config/export` now emits a service-registry
YAML export and strips ephemeral runtime state. The remaining gap is a proven,
operator-safe restore/reconciliation workflow. The current export is read-only;
it does not make a checked-in file authoritative, and it does not cover all
dashboard-owned settings.

Done means:

- The service-registry export can be imported into a fresh instance with a
  round-trip test and clear conflict/ownership semantics.
- The scope of any export beyond service records is explicit; secrets remain
  redacted or referenced, never exported raw.
- Operators can deliberately reconcile a provisioned source without making
  UI-managed rows file-owned by accident.
- The first-run path still works without requiring this feature.

Primary reference: `docs/CONFIG_SOURCE_OF_TRUTH.md`.

### 8. Declared, Visible Model Aliases For LLM Routing — completed

Service-declared model records now express a base model and optional policy
overlay. The pure resolver rejects missing or ambiguous alias policy, request
preparation uses the resolved target, and receipts retain both requested and
resolved model facts. This is data-derived policy, not silent fallback.

Evidence: `gateway/alias_policy.py`, `tests/test_service_registry.py`,
`tests/test_finite_output_default.py`, and `tests/test_receipt_store.py`.

### 9. LiteLLM Sidecar Config Sync And Visibility — completed

The LiteLLM substrate runs as a sidecar (`start.sh` launches `litellm --config
data/litellm.config.yaml` on :4011); the gateway proxies LLM execution to it.
The gateway compiles the config from the service registry. A registry mutation
now regenerates the artifact and hot-reloads the gateway; the managed LiteLLM
watcher notices the artifact and applies its own brief sidecar recycle. A live
incident (2026-06-03) recorded the former failure mode: the sidecar was
running with a stale `model_list: []`, so every chat/Anthropic/Ollama call
400'd "Invalid model name" while local kobold/tabby models were registered and
healthy. The failure was silent — an empty model list with no breadcrumb
pointing at the sidecar.

Completed:

- The gateway regenerates the LiteLLM config and hot-reloads on
  service-registry change; the managed watcher applies its own brief sidecar
  recycle.
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
- Trellis mesh route v1 (`POST /v1/meshes/generate`). It is planned, not
  implemented: this checkout has no Trellis route, helper, module, or tests.
  Do not start it until the fixed-route design's proof gates can be met,
  including an operator-declared exclusive route pool with an explicit
  whole-envelope capacity claim. See
  `docs/design/TRELLIS_MESH_ROUTE_DESIGN.md`.

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

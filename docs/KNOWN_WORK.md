# Known Work

Date: 2026-11-18
Status: canonical owner backlog for current `main`, reconciled against code,
tests, and history through `27b9af587`

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
- Engines are tracked as data, never as code. See
  `docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md`.

## Active Core Work

### 1. Reconcile The Live Capability And Service Inventory

Status: PARTLY
Must-have for release: no

Model names agree across the OpenAI models list, the Anthropic models list,
Ollama tags and the Models page (`tests/test_alias_and_model_names.py`,
`test_every_name_v1_models_lists_is_a_row_of_the_models_page`). Catalog
failures keep the protocol shape, and Ollama tags project the LiteLLM model
catalog (`tests/test_proxy_gateway_acceptance.py`,
`test_litellm_model_catalog_failures_are_protocol_shaped`,
`test_ollama_tags_project_litellm_model_catalog`). The workflow catalog agrees
with the gateway workflows endpoint and with the capability rows
(`tests/test_workflow_openapi.py`,
`test_catalog_matches_gateway_workflows_endpoint`,
`test_catalog_workflows_match_capability_rows`). A disabled service is
unavailable, shows its reason, and stays out of the routing index
(`tests/test_models_availability.py`,
`test_disabled_service_is_unavailable_and_still_not_in_the_routing_index`).
Every route intent maps to a capability (`tests/test_access_plans.py`,
`test_every_route_intent_maps_to_a_capability`;
`tests/test_route_policy_inventory.py`,
`test_http_route_policy_inventory`). The user-facing list uses friendly
descriptions (`tests/test_model_presentation.py`).

Next: one agreement test that ties the providers listing and the admin service
view rows to the capability rows.

### 2. Backend Adapter Contract Extraction

Status: DEFERRED
Must-have for release: no

An internal refactor; no user sees a change.

Next: revisit only when an engine change forces a shared adapter shape.

### 3. Extend Resource Admission Beyond Media Jobs

Status: PARTLY
Must-have for release: yes

Every execution route meets the shared admission: with the resource class
blocked, each route case (chat and streams, Anthropic, Ollama chat and
generate, embeddings, decisions, systemone, image, speech) answers 503
`resource_admission_denied` with reason codes before any executor runs
(`tests/test_dispatch_admission.py`,
`test_configured_resource_class_blocks_dispatch`). Completions, image edits and
variations, audio generation, transcription, translation, the Ollama legacy
embeddings and the `/ollama` copies refuse the same way
(`test_additional_routes_block_dispatch`); rerank is refused before its engine
starts (`test_rerank_is_refused_before_its_engine_starts`); and a video refusal
keeps its reasons (`test_video_refusal_names_its_admission_reasons`). Limit facts and maintenance
overrides project the refusal shape per protocol, with reason codes and
`Retry-After` where a retry is meaningful (`test_limit_facts_and_protocol_projection`),
and a late refusal keeps the protocol decision
(`test_late_dispatch_refusal_keeps_protocol_decision`). Queue denial reasons
are named per intent, video included (`tests/test_resource_admission.py`).

Next: tests for plan-limit and reservation denials and for the 403, 409 and
429 refusal shapes.

### 4. Long-Tail App Verification

Status: PARTLY
Must-have for release: yes

Open WebUI has a manual smoke (`docs/COMPATIBILITY_TARGET_MATRIX.md` lists it
as `manual-smoke`). The Vercel AI SDK probe
(`tests/compat/node/vercel-ai-sdk.test.mjs`) passed its chat, streaming, forced
tool call, structured output and Responses API cases against a live gateway on
2026-11-18; it runs by hand. The gate runs
`tests/compat/node/probe-gates.test.mjs` only.

Next: one check each for LibreChat and Continue with the real client, recorded
in `docs/COMPATIBILITY_TARGET_MATRIX.md`; Anthropic Messages streaming through
the Vercel AI SDK needs the `@ai-sdk/anthropic` package.

### 5. Media, Jobs, And Assets Honesty

Status: CLOSED
Must-have for release: yes

Job results are read back through the advertised link, and an unknown result
answers one uniform 404 (`tests/test_media_job_results.py`). The link between
a job and the request that created it is stored and tested
(`tests/test_media_jobs_contract.py`, `creating_receipt_id` and
`parent_request_id`). A control a job does not know is refused, not silently
dropped (`tests/test_c2d_group4_extension_controls.py`). Music and SFX jobs
are covered (`tests/test_audio_media_jobs.py`). Denoise is documented
(`docs/EXTENSION_ARCHITECTURE.md`).

### 6. Finish Extension Package Disable/Removal Semantics

Status: CLOSED
Must-have for release: no

The former plan race is fixed: the plan check runs inside the registry write
lock, so a plan saved during a removal is either seen by the removal or waits
for it (`tests/test_extension_packages.py`,
`test_a_plan_saved_between_the_removal_read_and_write_is_seen_by_the_removal`,
`test_a_plan_save_waits_for_a_removal_in_its_transaction`). Removal and
disable have explicit, tested effects on each record kind, with detach for
operator-copied or edited rows
(`test_package_lifecycle_removes_unchanged_all_kinds` and the detach tests in
the same file). Index, registry and audit agree after a lifecycle change
(`test_package_lifecycle_runtime_index_registry_audit_agree`), and each
disable, enable or removal writes the LiteLLM config once and fires the
registry-changed listener once
(`test_package_lifecycle_fires_one_reload_per_change`). No plugin marketplace
or general code-plugin loader is implied.

### 7. Complete Provisioning Restore And Reconciliation

Status: DEFERRED
Must-have for release: no

A fresh setup does not need restore: `GET /admin/api/config/export` is
read-only, and the first-run path works without importing anything.

Next: revisit only when an operator asks to restore a provisioned source.

### 8. Declared, Visible Model Aliases For LLM Routing — completed

Status: CLOSED
Must-have for release: no

Service-declared model records express a base model and an optional policy
overlay; the resolver rejects missing or ambiguous alias policy, request
preparation uses the resolved target, and receipts keep both the requested
and the resolved model facts (`gateway/alias_policy.py`,
`tests/test_service_registry.py`, `tests/test_finite_output_default.py`,
`tests/test_receipt_store.py`).

### 9. LiteLLM Sidecar Config Sync And Visibility — completed

Status: CLOSED
Must-have for release: no

The gateway compiles the LiteLLM config from the service registry and
hot-reloads on registry change; the managed watcher applies its own brief
sidecar recycle (`gateway/litellm_substrate.py`, `start.sh`). The admin view
shows the sidecar's live model count, and a stale or empty sidecar produces a
diagnosable error. See `docs/LITELLM_SUBSTRATE_DECISION.md`.

### 10. First-Run ComfyUI Registration Path

Status: OPEN
Must-have for release: yes

ComfyUI is not yet add-engine data: the Add engine flow has no ComfyUI
definition, so a new operator cannot register a first ComfyUI server through
the supported path. The work is folded into the engines-as-data effort
(`docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md`).

Next: a ComfyUI engine definition as data, one workflow a fresh install can
run with its dependencies listed, a report that names each missing dependency,
and a documented first request with its receipt. Write the docs after the flow
exists.

### 11. Correlate A First App Request With Its Receipt

Status: CLOSED
Must-have for release: yes

OpenAI chat, Anthropic Messages and Ollama chat and generate replies carry
`X-Azizam-Receipt-Id` (`tests/fixtures/dispatch_admission_baseline.json` pins
the header on those route cases; `tests/test_proxy_gateway_acceptance.py`
drives the routes). The Anthropic request-id is the receipt id.
`docs/LOCAL_CLIENT_RECIPES.md` names the header.

### 12. Add-Engine Health Endpoint And Model Entry When Discovery Finds Nothing

Status: CLOSED
Must-have for release: yes

The Add engine dialog has no health endpoint field because the check stores
the path it read as the engine's health endpoint
(`tests/test_engine_admin_routes.py`,
`test_the_check_reports_the_readiness_read_and_creation_stores_the_route_it_read`).
An engine whose server lists no models is refused once with the
`empty_models` warning and is created when the owner acknowledges it, with no
models
(`test_a_server_that_lists_no_models_warns_empty_models_and_can_still_be_added`,
same file). The decisions are recorded in `docs/FIRST_RUN_SETUP.md`, and the
UI says what happens next
(`web/src/app/engines/add-engine/add-engine-dialog.spec.tsx`, cases e and
e1).

### 13. Open WebUI In A Container Cannot Reach The Loopback-Published Gateway

Status: OPEN
Must-have for release: yes

Decision: the supported paths are host networking, a reverse proxy, or
attaching the client container to the Azizam Compose network and using the
in-network base URL. `docker-compose.yml` runs the gateway as the service
`gateway` on container port 5000 (published on the host's loopback only) and
defines the Compose `default` bridge network, so a client container attached
to that network resolves the service name and reaches
`http://gateway:5000` with an App key. A container source address needs no
extra grant: the client address decides throttling and first-owner claiming
only (`docs/ops/PUBLIC_EXPOSURE.md`).

Next: one test or probe for the chosen path (the in-network base URL
`http://gateway:5000/openai/v1`).

### 14. Confirm Native Control Authority And Config Export Authorization

Status: CLOSED
Must-have for release: no

Config export is owner-only: an administrator and a member cannot read it,
and the owner export writes every secret as a configured entry with no value
(`tests/test_config_export_owner_only.py`,
`test_an_administrator_cannot_read_an_export`,
`test_a_member_cannot_read_an_export`,
`test_the_owner_reads_the_config_export_with_every_secret_as_a_configured_entry_and_no_value`).
Broken frontends are reported in the admin overview: the `frontends` list
names the broken row with its errors, and the `attention` list carries the
item. `docs/design/ENGINE_CONTRACT.md` names those two places.

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
- Trellis 3D route follow-ups. The route is live on `main`
  (`POST /v1/3d/generations/glb`, listing `GET /v1/3d/generations`; frontend
  rows `mesh-generations` and `mesh-glb` in `registry/protocol_frontends.yml`),
  and the default background-removal model (BiRefNet) loads. Still deferred,
  by lead decision after the Try experiment: the admin route override with its
  CAS, and the UI whole-envelope safety label. The route answers 503 while
  the VRAM observer service (`docs/ops/vram-observer.service`) is not
  installed or started. See
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
- Source-of-truth/config changes: update `docs/CONFIG_SOURCE_OF_TRUTH.md`. The
  inventory (`gateway/config_sources.py`) is generated from the section schemas and
  `registry/startup.schema.json`; a new key in either needs no inventory edit.
- Extension model changes: update `docs/EXTENSION_ARCHITECTURE.md` and the
  relevant package validation tests.
- SDK probe changes: update `docs/SDK_COMPATIBILITY_HARNESS.md`; Python probes
  feed `scripts/compat/run_matrix.py`, while Node probes remain in
  `tests/compat/node` until a Node JSON reporter exists.
- Owner execution status changes: update this file first, then keep
  `docs/OWNER_EXECUTION_PLAN.md` as a build-history and design-order document.

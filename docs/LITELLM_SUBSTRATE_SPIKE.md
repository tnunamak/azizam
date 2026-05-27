# LiteLLM Substrate Spike

Date: 2026-05-26
Status: architecture spike

Question: why not LiteLLM?

Short answer: use LiteLLM where it is already trying to be the commodity LLM
gateway, but do not let it become the product boundary for the AI Gateway.
LiteLLM can own generic OpenAI-shaped LLM execution, routing, model aliases,
virtual keys, budgets, rate limits, logging hooks, guardrails, and cost tracking
for the narrow standard LLM calls it fronts. The AI Gateway should retain the
Anthropic and Responses protocol frontends plus the local
media/resource/workflow/admin UX that makes this gateway useful for
self-hosters.

## Decision Hypothesis

Adopt LiteLLM as an optional substrate for commodity LLM execution and
governance if it can sit behind the gateway without changing these invariants:

- every external request still resolves to an AI Gateway App, Profile, policy
  decision, and Receipt;
- `/gateway/v1/*` remains the canonical namespace for gateway-native discovery,
  workflows, receipts, media jobs, assets, and provider-native passthrough;
- local media and model-lifecycle operations remain visible as first-class
  gateway capabilities, not hidden inside generic OpenAI-compatible model
  routing;
- the admin dashboard remains the operator source of truth for Apps, Profiles,
  service/provider records, resource admission, queues, media jobs, and local
  workflow controls.

This is not "replace the gateway with LiteLLM." It is "stop hand-rolling
commodity LLM proxy features when LiteLLM can be the tested dependency."

## Why LiteLLM Is A Serious Candidate

LiteLLM's own docs position the proxy as a central LLM gateway with
authentication, authorization, multi-tenant spend management, per-project
customization, virtual keys, and an admin UI. It also advertises provider
translation for many LLM providers through OpenAI-shaped inputs/outputs,
retry/fallback routing, and spend/budget tracking. See:

- [LiteLLM getting started](https://docs.litellm.ai/) for the high-level proxy
  and SDK feature split.
- [Virtual Keys](https://docs.litellm.ai/docs/proxy/virtual_keys) for model
  access control, spend tracking, aliases, and key management.
- [Router - Load Balancing](https://docs.litellm.ai/docs/routing) for routing,
  retries, fallbacks, cooldowns, and deployment groups.
- [Guardrails - Quick Start](https://docs.litellm.ai/docs/proxy/guardrails/quick_start)
  for request/response guardrail integration.
- [Custom Callbacks](https://docs.litellm.ai/docs/observability/custom_callback)
  for logging hooks, cost callbacks, and request lifecycle events.
- [Config Overview](https://docs.litellm.ai/docs/proxy/configs) for the proxy
  config model.

The overlap is real. If the AI Gateway keeps building every generic LLM governance
feature itself, the likely result is duplicated surface area, weaker provider
coverage, and more compatibility work than the local-first product needs.

## What LiteLLM Would Own

LiteLLM should own the commodity LLM plane:

- upstream hosted LLM provider adapters for OpenAI, Gemini/Vertex, Azure
  OpenAI, OpenRouter-style providers, and similar text/embedding providers
  exposed through OpenAI-shaped execution;
- standard OpenAI-compatible `/v1/chat/completions`, `/v1/completions`, and
  `/v1/embeddings` execution when no gateway-native workflow or local lifecycle
  decision is needed;
- provider request translation, exception normalization, streaming behavior,
  retries, fallbacks, cooldowns, and load balancing;
- model aliases and deployment groups for standard LLM routes;
- LiteLLM virtual keys or an equivalent internal key bridge for downstream
  access, depending on the chosen integration shape;
- provider spend, token usage, rate limits, budgets, and cost callbacks for
  standard LLM calls;
- generic LLM guardrail hooks where they are provider-agnostic and do not need
  local resource context;
- commodity LLM observability exports to Langfuse, MLflow, Helicone, OpenTelemetry,
  or other LiteLLM-supported sinks.

## What The AI Gateway Would Retain

The gateway should retain the product plane:

- external auth for `opk_...` App keys, `oak_...` admin keys, OIDC browser
  sessions, Users, Apps, Profiles, AdminRoles, and audit events;
- the `/admin`, `/setup`, and account portal UX;
- App/Profile policy as the front-door authority boundary;
- route receipts that describe the gateway policy decision, accepted/rejected
  inputs, resolved workflow/provider, and client/admin/internal visibility;
- Anthropic Messages and OpenAI Responses translation/state, including stored
  Responses retrieve/delete/cancel behavior;
- `/gateway/v1/*` discovery, receipts, capabilities, workflows, media jobs,
  assets, and provider-native passthrough;
- local ComfyUI image/video/audio workflow manifests, typed workflow extension
  validation, asset handles, durable jobs, polling, cancellation, and content
  retrieval;
- local resource admission: GPU/VRAM/concurrency/queue policy, service
  enablement, reserved resources, media queue limits, and long-running job
  lifecycle;
- self-hosted service/provider registry UX, including provisioned/builtin/UI
  ownership and local health/test actions;
- backend-native management where local operators expect it: Ollama tags/show/ps,
  TabbyAPI load/unload/admin operations, LM Studio model lifecycle, ComfyUI
  queue/history/upload/download, and similar local controls;
- gateway-native compatibility decisions for media APIs, asset reuse, workflow
  controls, and unsupported-capability errors.

## What We Must Give Up Or Defer To LiteLLM

If LiteLLM becomes the LLM substrate, the AI Gateway should deliberately stop
competing on these features for commodity LLM routes:

- broad hosted-provider adapter coverage for OpenAI-shaped text and embedding
  calls;
- custom retry/fallback/router implementation for those standard LLM execution
  calls;
- separate spend and token-cost calculation for those calls, except for
  aggregating LiteLLM facts into gateway Activity/Receipts;
- first-party key-level LLM rate limits and budgets that duplicate LiteLLM's
  proxy controls;
- first-party generic LLM guardrail plumbing unless the guardrail needs local
  workflow/resource context;
- bespoke integrations for generic LLM observability vendors already supported
  by LiteLLM callbacks;
- model alias semantics that conflict with LiteLLM deployment groups or virtual
  key aliases;
- perfect internal visibility into every upstream provider request mutation.
  The gateway can record what it asked LiteLLM to do and what LiteLLM returned,
  but detailed provider selection may need to come from LiteLLM logs/callbacks.

The important tradeoff: the gateway becomes less self-contained for text LLM
governance. That is acceptable only if the local media/workflow/admin plane
gets simpler and stronger as a result.

## Integration Shape

Preferred shape for a spike:

```text
external client
  -> AI Gateway auth/Profile/policy/receipt preflight
  -> LiteLLM proxy or SDK for commodity LLM execution
  -> AI Gateway receipt finalization and Activity aggregation
```

Rules:

- The AI Gateway remains the only external base URL for existing users.
- LiteLLM can be an internal sidecar/service or an embedded SDK dependency.
- The gateway should pass a stable request id, App/Profile identifiers, and
  redaction-safe metadata to LiteLLM callbacks.
- LiteLLM keys should not become the user-facing credential unless we decide to
  replace App keys. The first integration should bridge App identity to
  LiteLLM metadata or internal virtual keys.
- Gateway media and provider-native routes should not proxy through LiteLLM
  unless LiteLLM has a materially better implementation for that exact backend.
- Gateway-owned protocol frontends, including Anthropic Messages and Responses,
  should translate and manage state inside the gateway. Their final chat
  execution hop may use LiteLLM only after the gateway has normalized the
  request to `/v1/chat/completions`.

## Risks

- **Authority split-brain.** LiteLLM has its own keys, teams, budgets, model
  allowlists, admin UI, and database. If both products can change runtime
  access policy, operators will not know which source is authoritative.
- **Receipt gaps.** LiteLLM callbacks may not expose every routing or mutation
  fact needed for the AI Gateway's body-free Receipt model.
- **Semantic drift.** LiteLLM aliases, fallbacks, and guardrails may make a
  different routing decision than the gateway Profile implied.
- **Local-first mismatch.** LiteLLM is strongest at LLM provider brokering. It
  does not replace durable media jobs, ComfyUI workflow manifests, local asset
  handles, or GPU-aware admission.
- **Operational weight.** Running LiteLLM proxy well may add Postgres,
  separate migrations, separate admin state, and one more service to debug.
- **Version churn.** LiteLLM moves quickly. Provider behavior and proxy config
  semantics may change faster than this gateway's compatibility contracts.
- **Debug UX fragmentation.** Operators may need to inspect both AI Gateway
  receipts and LiteLLM logs to understand one failed LLM request.
- **Unsupported capability ambiguity.** LiteLLM may treat an unsupported field
  as provider-specific behavior while the gateway expects accepted/rejected/
  ignored field accounting.

## Go / No-Go Criteria

Go if a one-route spike proves:

- an OpenAI-compatible chat request can pass through gateway App/Profile policy
  into LiteLLM and stream back without breaking SDK compatibility;
- The AI Gateway can persist a useful Receipt containing request id, App/Profile,
  requested model, LiteLLM target/deployment group when available, policy
  decision, final status, token usage, cost, and redaction-safe error facts;
- LiteLLM spend/rate-limit/budget facts can be mapped back to gateway Activity
  without making LiteLLM's dashboard the only source of truth;
- the admin source-of-truth boundary is explicit: the AI Gateway owns external
  Apps/Profiles; LiteLLM owns internal LLM execution policy for a configured
  subset;
- failures are diagnosable from `/gateway/v1/receipts` plus a linked LiteLLM
  request id/log entry;
- local media jobs and `/gateway/v1/*` behavior remain untouched.

No-go if:

- LiteLLM must become the external auth surface to get useful budgets/routing;
- gateway Receipts cannot capture enough routing, usage, and failure facts;
- LiteLLM cannot preserve streaming behavior expected by current SDK tests;
- integration requires rewriting the admin model around LiteLLM teams/keys;
- local workflow/resource admission has to be expressed as generic LiteLLM
  policy;
- the operational cost is higher than the duplicated code it removes.

## Migration Phases

### Phase 0: Paper Boundary

Document exact ownership and identify the smallest commodity LLM path. Candidate:
OpenAI Chat Completions for hosted/OpenRouter-like providers only. Do not touch
images, video, audio, media jobs, assets, Ollama-native management, or
provider-native passthrough.

### Phase 1: Shadow LiteLLM

Run LiteLLM beside the gateway for a small set of configured models. Gateway
continues to execute the existing path, but optionally sends mirrored metadata
or synthetic probes to LiteLLM to validate config, model aliases, and usage
facts. No user traffic depends on it.

### Phase 2: One Internal Route

Route one opt-in Profile/model group through LiteLLM for non-media chat. Keep
external App keys and gateway Receipts. Compare SDK compatibility, streaming,
usage/cost accounting, error shapes, and operator debugging against the current
path.

Implementation checkpoint (2026-05-27):

- An enabled LiteLLM service in the gateway service registry now routes
  OpenAI-compatible `/v1/chat/completions`, `/v1/completions`, and
  `/v1/embeddings` through a narrow LiteLLM execution transport. Bootstrap env
  variables remain only as a fallback for early migration.
- The expected service record is `services.litellm` with
  `kind: llm-substrate`, `capabilities: [llm.execution]`, `config.base_url`,
  and optionally `config.api_key_env`.
- The seam is intentionally not a pluggable product framework. Gateway auth,
  Profile policy, malformed JSON handling, extension validation, current-loaded
  target policy, local service availability, and response normalization remain
  outside LiteLLM.
- Responses and Anthropic Messages remain gateway-owned protocol frontends.
  They translate to Chat Completions, and that final chat execution hop uses
  LiteLLM when the LiteLLM substrate is enabled. A live check showed direct
  LiteLLM Anthropic/Responses endpoints completed but did not populate
  client-visible text correctly for the local OpenAI-compatible backend; keeping
  the gateway translators is the simpler, working boundary.
- `/v1/models` reads commodity LLM model groups from LiteLLM when the LiteLLM
  service is enabled. Gateway-native media workflow model IDs are still merged
  into the OpenAI-shaped model list.
- Client `opk_...` keys are not forwarded to LiteLLM. The gateway sends an
  internal LiteLLM bearer key when configured and attaches redaction-safe
  gateway metadata for callbacks/receipts.
- The runtime source of truth is the DB-owned service registry; generated
  LiteLLM config remains a derived artifact, not a second editable policy file.
  The local sidecar supervisor watches this generated file and restarts LiteLLM
  when it changes, so DB/UI service edits are not stranded behind a manual
  process restart.
- Operators can inspect the derived sidecar config at
  `GET /admin/api/operator/litellm-config`; it is generated from service
  records and Profiles and should not be edited independently.

### Phase 3: Delegate Commodity LLM Governance

Move hosted OpenAI-shaped text/embedding provider routing, fallback, budgets,
and generic guardrails to LiteLLM for opted-in Profiles. The AI Gateway stores
the product policy and imports LiteLLM execution facts into Activity/Receipts.
Gateway admin screens should show the effective LiteLLM-backed state without
exposing two competing policy editors. Anthropic and Responses compatibility
remain gateway protocol/state concerns even when their translated chat
execution hop uses LiteLLM.

### Phase 4: Retire Duplicate Gateway Code

Remove or freeze gateway-owned generic LLM router/budget/provider code only
after compatibility harnesses and receipt coverage prove the LiteLLM path is
better. Keep local backend-native management and media/workflow code.

### Phase 5: Optional Advanced Integration

Consider deeper integration only after the simple substrate works:

- create internal LiteLLM virtual keys per App/Profile;
- ingest LiteLLM callback events into gateway Activity;
- expose a read-only LiteLLM execution panel inside `/admin`;
- support per-Profile fallback policies that compile to LiteLLM routing config.

## Recommendation

Proceed with a bounded spike, not a rewrite. The first implementation should
prove whether LiteLLM can remove commodity LLM governance work while preserving
the gateway's stronger local-first contracts. The decision should be reversed
quickly if the integration forces App/Profile authority, Receipts, media jobs,
or local resource admission to bend around LiteLLM's model.

## Live Checkpoint: 2026-05-26

A temporary LiteLLM proxy was run from `/tmp` with two model groups pointing
back at the current AI Gateway deployment:

- `ai-gateway-default` -> `https://ai.vivid.fish/openai/v1`, model `default`;
- `ai-gateway-embeddings` -> `https://ai.vivid.fish/openai/v1`, model
  `nomic-embed-text-v1.5`.

Observed results:

- LiteLLM booted cleanly and exposed both configured model groups at
  `/v1/models`.
- OpenAI-compatible embeddings passed through LiteLLM successfully, including
  gateway-owned `dimensions` trimming.
- OpenAI chat passed through LiteLLM successfully after the gateway activated
  the local `default` model first. Gateway-owned Anthropic Messages and
  Responses translations also returned visible text over the same chat
  execution path.
- LiteLLM returned `404` for Ollama-native `/api/tags`, supporting the
  recommendation that the AI Gateway keep Ollama client-facing protocol routes
  unless a future LiteLLM version explicitly covers that surface.

Next proof step: keep the compatibility gate focused on the product boundary:
LiteLLM owns commodity OpenAI-shaped LLM execution; the gateway owns protocol
frontends, Profiles, local activation, media, resource admission, and receipts.

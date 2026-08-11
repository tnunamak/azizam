# LiteLLM Substrate Decision

Status: adopted boundary; target rearchitecture is directional, not yet adopted.

This document has two deliberate layers:

- **Current state** records the boundary the running gateway implements today.
- **Target state** records the durable rearchitecture direction harvested from
  the LiteLLM memo. It is not evidence that the migration is complete.

The boundary is adopted. The exact target deployment, database topology, and
identity migration remain open until they are proven against the product's
self-hosted and multi-user requirements.

The target direction is supported by LiteLLM's documented proxy, routing,
virtual-key, database, user/team, access-group, pass-through, custom-provider,
and authentication capabilities. These external capabilities are inputs to the
direction, not promises about every installed LiteLLM version; verify them
again before implementation. The local implementation evidence is the gateway
substrate compiler, delegated-key path, virtual-key admin transport, service
registry, and sidecar launcher referenced below.

## Current state: adopted execution boundary

LiteLLM is the gateway's commodity LLM execution substrate. It owns
OpenAI-shaped LLM execution and model-group discovery for:

- `/v1/chat/completions`
- `/v1/completions`
- `/v1/embeddings`
- `/v1/models` LLM entries

The AI Gateway owns the product/control-plane responsibilities around that
substrate:

- runtime App keys, OIDC users, Profiles, and policy gates
- local service availability, resource admission, and model readiness
- media workflows, durable jobs, generated assets, and provider-native passthrough
- Anthropic/Ollama/Responses protocol frontends where the gateway has product
  semantics that LiteLLM does not own
- receipts, activity, admin UX, and discovery metadata

The boundary is deliberately strict:

1. Gateway authenticates and policy-gates the request.
2. Gateway prepares local resources only when the requested LiteLLM model group
   maps to a gateway-declared local target.
3. LiteLLM executes the commodity LLM request.
4. Gateway records receipts and returns the provider-compatible response.

OpenAI-shaped LLM routes are explicit Gateway handlers that call the LiteLLM
substrate. They do not flow through the generic backend-native catch-all. The
catch-all is reserved for explicit compatibility/native paths that are mounted
before it or policy-gated elsewhere.

There is no legacy local executor fallback for OpenAI-shaped LLM routes. If the
LiteLLM substrate is not configured or unavailable, those routes fail clearly
instead of silently routing to Tabby, Kobold, or an embeddings server.

Model discovery follows the same ownership:

- `/v1/models` returns LiteLLM's LLM catalog plus Gateway-native workflow model
  entries. If LiteLLM catalog discovery is unavailable, `/v1/models` returns a
  protocol-shaped `503 backend_unavailable`.
- `/anthropic/v1/models` and Ollama `/api/tags` project the LiteLLM catalog
  into their protocol shapes. If LiteLLM catalog discovery is unavailable, those
  discovery endpoints return valid empty lists rather than raising accidental
  gateway errors.

LiteLLM configuration is a generated artifact from the gateway service registry.
The service registry remains the source of truth; generated LiteLLM YAML must not
be edited as product configuration.

The current implementation is intentionally hybrid. LiteLLM already owns
execution, virtual-key metering, and its model catalog for eligible LLM calls,
but the gateway still owns the product identity and policy layer around those
calls. The gateway also performs local readiness and lifecycle work before a
local model group reaches LiteLLM. This is a real boundary, not a claim that
LiteLLM has already replaced the gateway's control plane.

## Current state: key ownership

There are two distinct kinds of key, owned by two distinct planes:

- **Gateway App keys** (`opk_`/`oak_`) are the product control plane. They live
  in `admin.db`, carry the App/Profile/policy identity, and authenticate product
  requests that enter through the gateway. The gateway owns their lifecycle
  (mint, list, revoke) under `/admin/api/apps/*` and `/admin/api`.
- **LiteLLM virtual keys** (`sk-…`) are the *execution* plane. LiteLLM mints and
  meters them for commodity LLM calls (per-key budgets, model allow-lists,
  spend). The gateway does not store them; it forwards a virtual key to the
  substrate as the `X-Litellm-Key` delegated header on execution.

A virtual key is an LLM-execution credential only: it reaches
`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` via the substrate
and unlocks nothing else — media, jobs, admin, and provider-native routes stay
behind gateway App-key auth and Profile policy.

### Operating LiteLLM virtual keys

The operator surface over the sidecar's `/key/*` management API lives under
`/admin/api/operator/litellm/keys` (list, create) and `…/keys/delete`, with a
panel under **Services → LiteLLM execution keys** in `/admin`. Properties:

- These routes are master-key protected at the sidecar. The gateway's sidecar
  bearer **is** the master key (`start.sh` sets
  `LITELLM_MASTER_KEY=$AI_GATEWAY_LITELLM_API_KEY`), so no separate credential is
  configured; the admin client authenticates with `runtime_config.api_key`.
- A minted key's secret is shown exactly once (reveal-once); the gateway never
  persists it. Listing redacts/masks any `sk-` material defensively.
- The operator never supplies the secret — the sidecar mints it. Create input is
  sanitized to the documented `/key/generate` fields (alias, team/user,
  duration, `max_budget`, `models`).
- When the substrate is unconfigured or unreachable, the routes return a
  protocol-shaped `503`/`502` with a diagnosable code, never a generic 500.

Code: thin transport in `gateway/backends/litellm_admin.py`
(`LiteLLMKeyAdminClient`), endpoints in `gateway/admin_api.py`, panel in
`gateway/admin_render.py`, live client wiring in `proxy.py`.

## Target state: LiteLLM owns LLM runtime access

The durable direction is to make LiteLLM the source of truth for **LLM-shaped
runtime access**, while keeping this gateway as the local-AI product and
orchestration layer. This is a target architecture, not a completed migration.

In the target state, LiteLLM owns:

- supported OpenAI- and Anthropic-shaped LLM execution and provider adapters;
- routing, fallback, and load balancing where deployments are genuinely
  interchangeable;
- virtual keys, users, teams, access groups, model access, budgets, rate limits,
  spend accounting, and the LLM request log where its product supports them;
- the durable LLM model/access catalog rather than a parallel gateway policy
  system.

The gateway continues to own:

- setup and self-hosted product UX, including OIDC login when LiteLLM's SSO
  path is not the right product fit;
- the local service registry, resource admission, readiness, and lifecycle or
  model swapping for mutable singleton engines;
- ComfyUI and other media workflows, durable jobs, assets, and provider-native
  passthrough;
- gateway receipts that explain local orchestration, enriched with LiteLLM
  cost and usage data;
- thin protocol-shape shims such as Ollama-compatible routes when LiteLLM does
  not expose the exact client contract.

The target must not make LiteLLM pretend that a local singleton swap is ordinary
load balancing. A lifecycle-backed model group should route through a
gateway-owned local target that makes the requested state real, then forwards
to the engine. `current` and `current-fast` remain gateway semantics until an
equivalent contract is proven elsewhere.

### Target state: durable ownership split

The intended split is two durable sources of truth, not three:

- LiteLLM's durable database holds LLM users, teams, virtual keys, access
  groups, model access, budgets, rate limits, and spend data.
- The gateway database holds local services, lifecycle declarations, resource
  state, workflows, media jobs/assets, OIDC-to-LiteLLM provisioning rules, and
  product settings that LiteLLM does not model.

Generated `data/litellm.config.yaml` may remain as a bootstrap, migration, or
export artifact while this is being implemented. It must not become a third
hand-edited runtime source of truth.

### Target state: migration constraints and open questions

The rearchitecture is conditional rather than a license to delete working
gateway behavior:

- A multi-user product likely requires LiteLLM's durable database for virtual
  keys, budgets, and model access. A genuinely database-free product mode would
  change that conclusion and has not been ruled out by an adopted product
  decision.
- Existing gateway App keys, Profiles, OIDC mappings, and policy cannot be
  removed until their replacement has equivalent coverage for media, local
  resources, and household/friend/agent UX. A Profile may eventually become a
  product-friendly view over a LiteLLM access group or key template, but it must
  not remain a second LLM authorization engine.
- The sidecar versus in-process library choice remains open. The sidecar is
  current and stays in place until recurring config-sync or operational pain
  justifies a measured change; library mode is not a target-state requirement.
- External LiteLLM capabilities and licensing boundaries can change. Recheck
  provider, SSO/RBAC, database, and API behavior before implementing a migration.

### Target state: migration order

Migration should reduce duplicate policy incrementally:

1. Define the supported single-user and multi-user deployment modes, including
   database requirements and honest feature limits.
2. Make LiteLLM virtual keys the primary LLM execution credentials, with a
   bounded bridge for existing gateway keys during migration.
3. Map gateway identity and Profiles to LiteLLM users, teams, access groups, or
   key templates; retain gateway-only permissions for media and local resources.
4. Move supported OpenAI/Anthropic LLM semantics behind LiteLLM while keeping
   gateway lifecycle preparation and receipts.
5. Replace generated YAML as runtime truth with supported provisioning paths;
   keep compatibility exports only as migration artifacts.
6. Delete duplicated gateway LLM policy and compatibility code only after
   integration tests prove the replacement boundary.

Do not build a second runtime key, budget, model-access, or generic provider
adapter system in the gateway. Do not delete local lifecycle, resource state,
media/workflow, receipts, or exact protocol shims merely because LiteLLM is the
chosen substrate.

Reference material for a future migration review:

- [LiteLLM proxy](https://docs.litellm.ai/) and [routing](https://docs.litellm.ai/docs/routing)
- [virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys) and
  [database ownership](https://docs.litellm.ai/docs/proxy/db_info)
- [users and teams](https://docs.litellm.ai/docs/proxy/user_management_heirarchy),
  [access groups](https://docs.litellm.ai/docs/proxy/access_groups), and
  [custom providers](https://docs.litellm.ai/docs/providers/custom_llm_server)

## Deployment shape: sidecar (current); library mode is an open question

LiteLLM currently runs as a separate server process (the `litellm[proxy]` CLI
on :4011); the gateway forwards OpenAI-shaped execution to it over HTTP. Running
LiteLLM in-process as a library (`litellm.Router` / `litellm.acompletion`) is a
possible alternative. **This is not decided.** The sidecar stays for now; the
deployment shape is deliberately left open to revisit.

Context for whoever revisits it:

- `litellm[proxy]` is already a gateway dependency (importable in the venv), so
  the choice is about process topology, not dependency footprint.
- The gateway owns its entire control plane (keys, Profiles, policy, admin UI,
-  receipts) **in the current state** and uses LiteLLM for raw execution,
  virtual-key administration, and `v1/models`; the target state moves eligible
  LLM runtime access to LiteLLM without moving local orchestration or product
  UX wholesale.
- A 2026-06-03 incident was a config-sync staleness bug (the generated YAML the
  sidecar reads went stale vs. the service registry), not an inherent flaw of
  either mode. Hardening that reconciliation is tracked as Known Work #9 and is
  worth doing regardless of which mode is chosen.

Trade-offs to weigh when revisiting: process/config-sync simplicity (favors
library) vs. crash isolation, noting an "AI gateway" largely fate-shares with
its LLM execution layer (weakens the isolation argument). Decide based on
whether the sidecar causes recurring operational pain, not in the abstract.

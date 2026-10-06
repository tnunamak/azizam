# LiteLLM Substrate Decision

Status: adopted boundary; revised target control-plane decision.

This document has two deliberate layers:

- **Current state** records the boundary the running gateway implements today.
- **Target state** records the durable ownership split for future work. It is
  not evidence that implementation is complete.

The boundary is adopted. The gateway remains the one product control plane for
identity, App keys, access plans, budget decisions, provider-key custody, spend
caps, receipts, and local orchestration. LiteLLM is a pinned, replaceable
execution substrate for chat-shaped local and remote models.

The local implementation evidence is the gateway substrate compiler,
delegated-key path, virtual-key admin transport, service registry, and sidecar
launcher referenced below.

## Current state: adopted execution boundary

LiteLLM is the gateway's commodity LLM execution substrate. It owns
OpenAI-shaped LLM execution and model-group discovery for:

- `/v1/chat/completions`
- `/v1/completions`
- `/v1/embeddings`
- `/v1/models` LLM entries

The Azizam owns the product/control-plane responsibilities around that
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

A virtual key authorizes eligible local LLM-shaped protocol frontends: the
three OpenAI execution routes above, the gateway's Anthropic Messages frontend,
and the LiteLLM-filtered Ollama catalog. That path bypasses gateway Profile
policy for those calls. It does not authorize gateway-owned media, jobs, admin,
or native passthrough routes; those remain behind gateway App-key auth and their
own gateway policy.

This bypass remains only for local models. Registry-declared paid models reject
virtual-key callers before execution. Paid calls use gateway App keys, access
plans, atomic spend reservations, and receipts before LiteLLM executes them.
See `REMOTE_PROVIDERS.md` for configuration and admin JSON APIs. The gateway
registry generates the sidecar's model catalog; do not add paid groups or paid
fallbacks independently in the sidecar.

### Operating LiteLLM virtual keys

The operator surface over the sidecar's `/key/*` management API lives under
`/admin/api/operator/litellm/keys` (list, create) and `…/keys/delete`, with a
panel under **Services → LiteLLM execution keys** in `/admin`. Properties:

- These routes are master-key protected at the sidecar. The gateway's sidecar
  bearer **is** the master key (`start.sh` sets
  `LITELLM_MASTER_KEY=$AZIZAM_LITELLM_API_KEY`), so no separate credential is
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

## Target state: gateway control plane, LiteLLM execution substrate

The durable direction is to keep the gateway as the one control plane and use
LiteLLM as a commodity execution substrate. LiteLLM runs chat-shaped local and
remote model calls. The gateway decides whether a person, app, access plan,
alias, provider, and budget allow the call before execution starts.

In the target state, LiteLLM owns:

- provider adapters and execution for supported OpenAI- and Anthropic-shaped
  LLM calls;
- routing, fallback, and load balancing where deployments are genuinely
  interchangeable;
- raw LiteLLM request execution, provider error normalization, and cost data that
  the gateway can record in receipts.

The gateway owns:

- setup and self-hosted product UX;
- built-in accounts, OIDC, people, Apps, App keys, access plans, and invites;
- provider-key custody through an encrypted write-only gateway store, with
  environment-variable references as an alternative;
- spend caps, budget decisions, policy gates, and paid-call admission;
- the local service registry, resource admission, readiness, and lifecycle or
  model swapping for mutable singleton engines;
- ComfyUI and other media workflows, durable jobs, assets, and provider-native
  passthrough;
- receipts, activity, admin UX, discovery metadata, and support matrices;
- thin protocol-shape shims such as Ollama-compatible routes when LiteLLM does
  not expose the exact client contract.

The target must not make LiteLLM pretend that a local singleton swap is ordinary
load balancing. A lifecycle-backed model group should route through a
gateway-owned local target that makes the requested state real, then forwards
to the engine. `current` and `current-fast` remain gateway semantics until an
equivalent contract is proven elsewhere.

### Why LiteLLM is not the product control plane

LiteLLM is a strong execution substrate, but its documented product shape does
not match the gateway's self-hosted control-plane needs:

- Enterprise licensing gates several controls the gateway needs as ordinary
  owner features. LiteLLM documents SSO as free only up to five users, with
  SSO beyond that, OIDC/JWT auth, audit logs with retention, secret-manager
  read/write, public/private route controls, RBAC, project management, and
  tag-based budgets in its Enterprise surface.
- Durable key, user, spend, config, virtual-key, and budget features depend on
  Postgres. LiteLLM's production guide lists PostgreSQL as required for proxy
  auth and tracking features. Its virtual-key guide requires a Postgres
  database. Its budget guide says budgets read spend from the database and do
  not cap a database-free deployment.
- Moving identity, keys, model access, and budgets into LiteLLM would create two
  policy engines: LiteLLM for chat-shaped LLM calls and the gateway for media,
  local resources, provider-native routes, jobs, assets, and receipts. That
  split would make paid-call admission harder to explain and test.
- LiteLLM is a supply-chain dependency. On 2026-03-24, LiteLLM reported that
  PyPI packages `litellm==1.82.7` and `litellm==1.82.8` were compromised and
  contained malicious payloads. The gateway should pin and hash-lock LiteLLM,
  keep it replaceable, and avoid making it the owner of product identity.

Generated `data/litellm.config.yaml` may remain as a bootstrap, migration, or
export artifact. It must not become a hand-edited product source of truth.

UI-entered provider keys are AES-GCM ciphertext in `admin.db`. A separate 0600
key file in the data directory supplies the encryption key. Generated LiteLLM
YAML contains only `os.environ/` references. The managed launcher decrypts keys
into its child process environment when LiteLLM starts. Key rotation and
deletion trigger configuration reconciliation; they must not require restarting
the gateway. See [provider key custody and threat model](REMOTE_PROVIDERS.md)
for backup requirements and the limits of encryption at rest.

Provider endpoints remain explicit through registry persistence and generated
`api_base`; missing remote endpoints are configuration errors. Credential
injection checks the approved scheme, hostname, and effective port. The
gateway uses provider-owned prices for settlement; packaged SDK vendor prices
are a fallback only for catalog-declared official APIs whose full endpoint
matches. A custom endpoint cannot inherit vendor prices by model name.

Do not build another generic provider adapter system in the gateway. LiteLLM is
the preferred substrate where its adapters fit. Do not delete local lifecycle,
resource state, media/workflow, receipts, access plans, paid-call admission, or
exact protocol shims merely because LiteLLM is the chosen substrate.

Reference material:

- [LiteLLM Enterprise](https://docs.litellm.ai/docs/enterprise)
- [LiteLLM production deployment](https://docs.litellm.ai/docs/proxy/deploy)
- [LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys)
- [LiteLLM budgets and rate limits](https://docs.litellm.ai/docs/proxy/users)
- [LiteLLM security update for the 2026-03-24 PyPI incident](https://docs.litellm.ai/blog/security-update-march-2026)

## Deployment shape: sidecar (current); library mode is an open question

LiteLLM currently runs as a separate server process (the `litellm[proxy]` CLI
on :4011); the gateway forwards OpenAI-shaped execution to it over HTTP. Running
LiteLLM in-process as a library (`litellm.Router` / `litellm.acompletion`) is a
possible alternative. **This is not decided.** The sidecar stays for now; the
deployment shape is deliberately left open to revisit.

Context for whoever revisits it:

- `litellm[proxy]` is already a gateway dependency (importable in the venv), so
  the choice is about process topology, not dependency footprint.
- The gateway owns its product control plane (App keys, Profiles, policy, admin
  UI, receipts) **in the current state** and uses LiteLLM for raw execution,
  virtual-key administration, and `v1/models`; the target state keeps product
  access policy in the gateway while LiteLLM executes eligible LLM calls.
- A 2026-06-03 incident exposed config-sync staleness. That reconciliation is
  now completed: registry writes regenerate the YAML artifact, and the managed
  launcher hash-watches it and restarts LiteLLM when it changes. The sidecar
  still has a short watcher interval, but this is no longer open hardening work.

Trade-offs to weigh when revisiting: process/config-sync simplicity (favors
library) vs. crash isolation, noting an "AI gateway" largely fate-shares with
its LLM execution layer (weakens the isolation argument). Decide based on
whether the sidecar causes recurring operational pain, not in the abstract.

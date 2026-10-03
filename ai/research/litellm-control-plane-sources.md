# LiteLLM Control-Plane Source Check

Date: 2026-10-03

Used by: [LiteLLM Substrate Decision](../../docs/LITELLM_SUBSTRATE_DECISION.md)

## Question

Should the gateway migrate identity, keys, model access, and budgets into
LiteLLM, or should LiteLLM remain a pinned, replaceable execution substrate while
the gateway stays the product control plane?

## Local Corpus Checked First

Read or searched before web research:

- The shared dotfiles research corpus (`ai/research/INDEX.md`).
- `docs/LITELLM_SUBSTRATE_DECISION.md`
- `docs/CONFIG_SOURCE_OF_TRUTH.md`
- `docs/KNOWN_WORK.md`
- `docs/GATEWAY_STREAM_LIVENESS_AUDIT_2026-07-13.md`
- `docs/design/ENGINE_REGISTRY_DESIGN.md`
- `scripts/research/reddit_trial_lab.py`
- `scripts/research/reddit_thread_from_lab.py`
- `scripts/research/reddit_simulation.py`

Relevant local findings:

- The shared corpus had no dedicated LiteLLM control-plane entry.
- Existing gateway docs consistently describe LiteLLM as an execution substrate
  while gateway policy, receipts, local readiness, and service-registry state
  remain gateway-owned.
- Local Reddit-simulation material records the expected OSS objection, "why not
  LiteLLM?", but it is generated research scaffolding, not primary evidence.
- No local entry established the 2026-03-24 LiteLLM PyPI incident or the current
  Enterprise/Postgres feature boundaries. Primary web sources were required.

Negative results and dead ends:

- No `ai/research/` directory existed in this worktree before this entry.
- The external research corpus could not be mutated from this lane, so this
  worktree-local capture records the source trail.
- Search hits for `LiteLLM` in the shared index mostly related to other systems
  or general Postgres/OIDC topics, not this decision.

## Primary Sources Read

### LiteLLM Enterprise

URL: https://docs.litellm.ai/docs/enterprise

Observed claims:

- LiteLLM lists SSO as free only up to five users.
- SSO beyond five users, OIDC/JWT auth, audit logs with retention,
  secret-manager read/write, public/private route controls, RBAC, project
  management, and tag-based budgets appear in the Enterprise feature surface.
- This supports treating those capabilities as unsuitable for ordinary gateway
  owner features unless the gateway deliberately adopts an Enterprise dependency.

Short excerpt:

> Free SSO up to 5 users.

### LiteLLM Production Deployment

URL: https://docs.litellm.ai/docs/proxy/deploy

Observed claims:

- The production deployment guide requires PostgreSQL for proxy authentication
  and tracking features.
- The source does not say PostgreSQL is needed for every possible LiteLLM library
  use or for every stateless execution call.

Short excerpt:

> Set up a PostgreSQL Database.

### LiteLLM Virtual Keys

URL: https://docs.litellm.ai/docs/proxy/virtual_keys

Observed claims:

- LiteLLM virtual-key management requires a database-backed proxy setup.
- This matters when considering whether LiteLLM should own user-facing runtime
  keys and key lifecycle.

Short excerpt:

> Requires LiteLLM Proxy + DB.

### LiteLLM Budgets And Users

URL: https://docs.litellm.ai/docs/proxy/users

Observed claims:

- Budget enforcement depends on spend recorded in the database.
- LiteLLM documents that budget caps are not enforced without database spend
  tracking.
- This supports the narrower statement: Postgres is required for LiteLLM-backed
  policy/auth/tracking features, not for all possible execution modes.

Short excerpt:

> If there is no db, litellm will not track spend.

### LiteLLM Security Update

URL: https://docs.litellm.ai/blog/security-update-march-2026

Observed claims:

- LiteLLM reported a 2026-03-24 PyPI compromise.
- The affected versions were `litellm==1.82.7` and `litellm==1.82.8`.
- The incident supports pinning and hash-locking LiteLLM and keeping it
  replaceable rather than making it the gateway's product identity store.

Short excerpt:

> Malicious payloads were uploaded to PyPI.

## Findings

1. LiteLLM is still the right substrate for chat-shaped execution where its
   adapters fit.
2. The documented Enterprise boundary covers several features that a self-hosted
   gateway owner expects as normal product controls.
3. The Postgres finding is specific: Postgres is needed for LiteLLM proxy
   authentication, virtual-key, spend tracking, and budget enforcement. It is
   not evidence that every LiteLLM execution path requires Postgres.
4. Moving identity, keys, model access, and budgets into LiteLLM would create
   two policy engines because the gateway must still own media, jobs, assets,
   local readiness, provider-native routes, receipts, and paid-call admission.
5. The PyPI compromise is not a reason to avoid LiteLLM. It is a reason to pin,
   hash-lock, and keep it replaceable.

## Documentation Impact

`docs/LITELLM_SUBSTRATE_DECISION.md` should say:

- current execution boundary remains adopted;
- gateway is the one control plane;
- LiteLLM is a pinned, replaceable execution substrate;
- LiteLLM should not own gateway identity, access plans, App keys, spend caps,
  or paid-call admission;
- the virtual-key bypass must be retired for paid models;
- Postgres claims must be scoped to LiteLLM policy/auth/tracking features.

# LiteLLM Substrate Decision

Status: adopted.

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
  receipts) and uses LiteLLM for raw execution + `v1/models` only — none of the
  proxy server's key/budget/guardrail/UI features.
- A 2026-06-03 incident was a config-sync staleness bug (the generated YAML the
  sidecar reads went stale vs. the service registry), not an inherent flaw of
  either mode. Hardening that reconciliation is tracked as Known Work #9 and is
  worth doing regardless of which mode is chosen.

Trade-offs to weigh when revisiting: process/config-sync simplicity (favors
library) vs. crash isolation, noting an "AI gateway" largely fate-shares with
its LLM execution layer (weakens the isolation argument). Decide based on
whether the sidecar causes recurring operational pain, not in the abstract.

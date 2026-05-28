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

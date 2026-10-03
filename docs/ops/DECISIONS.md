# Local decisions

`POST /v1/systemone` and `POST /typesafe/v1/systemone` accept the TypeSafe
System One request contract: `model`, `state`, and a map of typed `questions`.
Choice returns an option and its distribution. Score returns the expected
level (0 through the final level index), legend, distribution and confidence.
Noul returns P(yes). Questions are evaluated independently against shared state.

Use `decide` for the default local model, `decide-quality` for the larger local
model with a free fallback to `decide`, or `jev-*` for the default local alias.
A provider-qualified name is never sent to a paid provider by this local path.
The separate providers implementation can supply remote routing later.

The official TypeSafe SDK uses `TYPESAFE_BASE_URL=http://HOST/typesafe` and the
gateway App key as `TYPESAFE_API_KEY`. `GET /typesafe/v1/models` returns TypeSafe's
`models` array. Root `/v1/models` retains OpenAI's `object: list, data` contract.
Authenticated decision models and capabilities are filtered by access-plan
protocol, intent, model and backend rules. Restricted existing plans need an
explicit `typesafe` protocol and `decision.systemone` intent grant.

`POST /v1/decisions` returns an OpenAI-shaped 501 with code
`decisions_backend_unavailable`. Its public schema is not available in the
binding design. A dependency hook can supply the remote OpenAI passthrough;
this implementation does not guess a local translator.

## Engine deployment

Install the separate engine environment and the unit examples in
`docs/ops/decider.service` and `docs/ops/decider-quality.service`. Those examples
use deployment-specific model and llama-server paths; adjust them before use.
Do not enable the on-demand units at boot. The gateway's systemd lifecycle driver
starts the selected unit when a request arrives. Both units default to CPU and
exit after 600 seconds idle. The quality engine is opt-in.

The deployment variables are `LLAMA_SERVER`, `DECIDER_MODEL`, `DECIDER_MODEL_ID`,
`DECIDER_PORT`, `DECIDER_LLAMA_PORT`, `DECIDER_THREADS`, `DECIDER_CTX`,
`DECIDER_PLACEMENT=cpu|cuda:N`, `DECIDER_RESIDENCY=resident|on_demand`,
`DECIDER_IDLE_EXIT_S`, and `DECIDER_STARTUP_TIMEOUT_S`.
Placement and residency are configuration inputs. CUDA startup failure falls
back to the same model on CPU. The default listener pairs are 8023/8024 and
8025/8026; keep those independent from other engine listeners.

The gateway has bootstrap service rows in `proxy_config.yml`. After the service
registry has imported its seeds, the registry remains authoritative. Existing
instances need the two decision service records added through the admin service
API/UI, with systemd lifecycle units and `/health` checks. Do not modify an
existing database through this lane. No database schema migration is required.

Set service models to `decider-2b-v11` and `decider-4b-v2.1` and capabilities to
`decisions`. The alias engine specs identify those service IDs and upstream
models. Placement metadata must agree with the unit's configuration; runtime
`engine` is learned from health and response headers, never inferred from a
requested placement. Cold services report placement as unknown until observed.

## Receipts and limits

Response headers include `x-gateway-request-id`, `x-gateway-backend`,
`x-gateway-remote`, `x-gateway-cost-usd`, and the observed `x-gateway-engine`.
Local cost is zero. Receipt views expose the question count, actual CPU/GPU
engine, latency, requested model, resolved model, and local cost without state
or question text. The App owns the persisted receipt. Failures also get receipts.

The local routing deadline is 15 seconds across lifecycle startup and free
fallback attempts. Starting/unavailable engines return 529 with Retry-After;
the TypeSafe SDK can retry. Invalid requests return 422 before dispatch.
The model context is configured to 4096 tokens; engine tokenization must reject
an oversized prompt before inference. Local discovery advertises at most 4096
state tokens as an upper bound; question and rubric overhead reduce the usable
state budget within that context. A wider context requires an explicit operator
configuration change. There is no streaming or packed dependent-row
mode in the gateway contract.

# Local decisions

`POST /v1/systemone` and `POST /typesafe/v1/systemone` accept the TypeSafe
System One request contract: `model`, `state`, and a map of typed `questions`.
Choice returns an option and its distribution. Score returns the expected
level (0 through the final level index), legend, distribution and confidence.
Noul returns P(yes). Questions are evaluated independently against shared state.

Use `decide` for the default local model, `decide-quality` for the larger local
model with a free fallback to `decide`, or `jev-*` for the default local alias.
A provider-qualified `typesafe/*` name selects the configured paid TypeSafe
provider. Unprefixed `jev-*` names stay local; an unavailable local engine does
not trigger a paid fallback.

The official TypeSafe SDK uses `TYPESAFE_BASE_URL=http://HOST/typesafe` and the
gateway App key as `TYPESAFE_API_KEY`. `GET /typesafe/v1/models` returns TypeSafe's
`models` array. Root `/v1/models` retains OpenAI's `object: list, data` contract.
Authenticated decision models and capabilities are filtered by access-plan
protocol, intent, model, backend and capability-tag rules. Restricted existing plans need an
explicit `typesafe` protocol and `decision.systemone` intent grant.

`POST /v1/decisions` returns an OpenAI-shaped 501 with code
`decisions_backend_unavailable`. The route forwards the original JSON request bytes when an enabled OpenAI
provider declares the `decisions` capability. It uses paid admission and
receipts, without a local translator.

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

System One validation failures use TypeSafe's `detail` array with `loc`, `msg`
and `type`. Nullable instructions and Noul descriptions follow the pinned
official OpenAPI schema in `tests/fixtures/typesafe_openapi.json`. Score accepts
one to ten rubric levels locally. OpenAI Decisions retains its own error shape.

The gateway propagates its remaining deadline to the engine. Lock admission,
prefix preparation and every scoring row share that budget. Client disconnects
cancel dispatch and close active upstream requests; expired work stops before
another row starts. Engine deadline failures produce retryable 529 responses
through the gateway.

Startup rejects occupied child ports and verifies the listener's owned PID and
model identity. Health becomes unhealthy when the owned llama-server child exits,
and the wrapper exits with failure so the configured service restart policy can
recover it. Operator-requested graceful shutdown exits successfully.

## Paid provider deployment

Set `TYPESAFE_API_KEY` in the gateway process environment or deployment secret
store. Registry records contain the variable name, never its value. Add the
TypeSafe service through the admin service API/UI; the registry is authoritative:

```yaml
id: typesafe
kind: provider
enabled: true
capabilities: [typesafe-systemone]
config:
  provider_id: typesafe
  provider_type: paid-remote
  base_url: https://api.typesafe.ai
  api_key_env: TYPESAFE_API_KEY
  concurrency_limit: 2
  models:
    - model_group: typesafe/jev-latest
      upstream_model: jev-latest
```

Add the operator's current contracted token rates to the record's `price`
configuration before granting a capped plan. The provider module rejects unknown
estimates when any App, provider or global cap requires a bound. Grant the App
access plan the TypeSafe protocol, `decision.systemone` intent, applicable model,
backend and capability tags, plus `remote.providers: [typesafe]`. Set its
per-request, daily and monthly caps through the access plan and Providers admin
controls. Paid calls require an App credential; delegated or legacy credentials
cannot authorize them.

`GET /typesafe/v1/models` includes permitted configured TypeSafe model records.
Discovery reads registry metadata and key availability without calling vendors.
Actual settlement uses the provider hook's reported or computed final cost;
missing final cost stays unknown. Unknown paid cost appears as `unknown` in the
response header and `null` in the receipt, and its reservation remains held.
Ambiguous failures and cancellation also retain reservations because they do
not establish that billing stopped. These unresolved reservations consume the
provider's concurrency limit until reconciled under the provider ledger rules.

For OpenAI forwarding, add an enabled `openai` provider record with an
OpenAI-compatible protocol and the explicit `decisions` capability, and set its
configured key environment variable. The Decisions schema has no local cost
bound: opaque requests with any spend cap fail closed before dispatch. Explicitly
permitted uncapped requests can forward and settle a reported final cost. Do
not infer Decisions support from a chat provider alone. Without the configured
capability, `/v1/decisions` remains 501.

No schema migration is required. The paid provider module creates its spend
ledger beside the admin database. After configuring records and environment,
restart the gateway only with `scripts/safe-restart`; this lane does not deploy
or restart live services.

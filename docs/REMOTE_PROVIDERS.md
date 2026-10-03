# Remote Providers

Remote providers are paid or externally hosted backends that the gateway can
reach only when the owner configured them and the caller's access plan permits
them.  Secrets stay outside the gateway database.  The registry stores only the
environment variable name that holds each provider key.

## Service Registry Schema

Remote providers are service registry records with provider-shaped config:

```yaml
services:
  openrouter:
    id: openrouter
    kind: provider
    name: OpenRouter
    enabled: true
    capabilities: [openai-compatible]
    config:
      remote_provider: true
      provider_type: paid-remote
      provider_id: openrouter
      protocol: openai-compatible
      base_url: https://openrouter.ai/api/v1
      api_key_env: OPENROUTER_API_KEY
      models:
        - model_group: openrouter/anthropic/claude-sonnet
          upstream_model: anthropic/claude-sonnet
      price:
        input_usd_per_million_tokens: "3.00"
        output_usd_per_million_tokens: "15.00"
```

The important fields are:

- `remote_provider: true`, `paid_remote: true`, or `provider_type` /
  `cost_class` set to `remote`, `paid-remote`, or `cloud`.
- `api_key_env`: the environment variable name. The actual key value must be in
  the process environment or deployment secret store.
- `models`: the provider-qualified public model names the gateway may route.
- `price`: optional cost table used for pre-call estimates. If a capped request
  has no bounded cost estimate, the request fails closed before the provider
  call.

Inline provider keys are rejected by the service registry. Use `api_key_env`;
do not put raw keys in YAML, `admin.db`, receipts, logs, or tests. Remote or paid
providers cannot enable `native_passthrough`; their calls must use the paid
admission and accounting path. The native handler also rejects remote records
before injecting authentication or opening an upstream connection.

## Access Plans

Profiles grant paid provider access through the `remote` block:

```yaml
remote:
  providers: [openrouter, typesafe]
  max_usd_per_request: "0.05"
  budget_usd_per_day: "2.00"
  budget_usd_per_month: "20.00"
  allow_paid_fallback: false
```

The default profile has no remote providers.  A paid fallback also needs
`allow_paid_fallback: true`; naming a fallback in an alias is not enough.
Provider ids are canonical lowercase ids in configuration, profiles, admin
policy, and the spend ledger. Model paths after the provider prefix retain
their case.

The shared execution transport forwards only documented OpenAI chat,
completion, and embedding body fields, plus gateway-controlled retry settings.
It strips unknown top-level fields, all query parameters, and top-level
`metadata`, which LiteLLM consumes as routing context. Nested message content
and tool schemas are preserved. Gateway aliases
choose fallback targets, and each paid attempt needs its own permission and
reservation. Paid attempts use `num_retries: 0`; a single reservation cannot
authorize multiple potentially billable retries.

Paid execution has a stricter field allowlist tied to the token cost model.
It filters and normalizes the request before reservation, then estimates the
exact payload sent to the shared transport. Supported output limits
(`max_tokens` and `max_completion_tokens`) become one outbound `max_tokens`
using their maximum. Invalid limits return HTTP 400. `max_output_tokens` is
unsupported on this paid chat/completion path and returns HTTP 400 with
`paid_option_unsupported` before reservation or dispatch.

Unmodeled billing options also return `paid_option_unsupported`, including
search, service tiers, audio/modalities, prediction, storage/background work,
and unknown fields. This applies even without configured caps. Tools must be
client-executed function tools; provider-executed tools are unsupported.
Nested function schemas remain intact. The explicit field-list regression
requires a pricing decision when supported fields change.

Document/file blocks, nested `cache_control`, and model modifiers such as
`:online` and `:nitro` are rejected by default. An operator can explicitly
declare these dimensions in the provider price table, including per-model
overrides. For example, `price.dimensions.cache_control: true` or
`price.dimensions["model_modifier:online"]: true` opts into that feature.
Unbounded input estimates still fail closed when a cap requires a bound.
These declarations do not turn reservations into final bills.

## Admin API

The Providers UI uses:

- `GET /admin/api/providers`
- `GET /admin/api/providers/{provider}`
- `PUT /admin/api/providers/{provider}/caps`
- `DELETE /admin/api/providers/{provider}/caps`
- `PUT /admin/api/providers/{provider}/allowed-plans`
- `PUT /admin/api/providers/-/global-caps`
- `DELETE /admin/api/providers/-/global-caps`

`key_status` is only `set` or `missing` plus the env var name.  It never
contains the key value.

## Spend Ledger And Caps

Provider calls reserve spend in `provider_spend` before dispatch.  The ledger
counts both `reserved` and `settled` rows so concurrent requests cannot exceed
the available reservation budget by racing each other. Settlement prefers
provider-reported `usage.cost`; otherwise LiteLLM calculates cost from the
full typed response and usage, including cache, reasoning, and other billing
details. Streaming usage fields are merged across events before calculation.
Streaming headers cannot establish final cost before generation completes.
Unknown usage extensions or missing required SDK price metadata leave cost
unknown. The SDK catalog must include rates for reported cache reads and
writes; its packaged offline catalog may be incomplete. An operator can set
`price.cost_model` (or a per-model `price.models` override) to the SDK pricing
model and add `price.usage_price_requirements` rows containing `usage_path`
and `price_keys` arrays for additional billed dimensions. These rows validate
catalog completeness; they do not calculate fees. Provider-reported cost
remains authoritative.

A cap can be exceeded by at most the actual cost of requests already in flight when it was reached.

The Providers admin API includes this guarantee. Once settled spend reaches
a window cap, new paid requests are denied even when their estimate is zero.
Paid concurrency is limited per provider across all access plans and gateway
processes sharing the ledger. The default is two open reservations; set a
positive integer `config.concurrency_limit` on the provider record to change
it. The admin API projects the same effective value.

Unknown actual cost retains the reservation and its concurrency slot until
reconciliation. Disconnects and transport failures do not establish a zero
bill. A successful settlement or a known pre-dispatch release frees the slot;
an unresolved reservation cannot be replaced with another paid request.

Set the OpenRouter key's own credit limit equal to the gateway cap as a
provider-side backstop. The admin API recommends key-level credit limits for
providers that support them; operator-authored `recommendations` can replace
the default. Match the key's reset period to the intended budget window.
OpenRouter's `provider.max_price` constrains unit rates, not total request
cost, so the gateway does not populate it with the remaining dollar budget.
See [OpenRouter spend controls](https://openrouter.ai/docs/guides/best-practices/spend-controls)
and [provider price limits](https://openrouter.ai/docs/guides/routing/provider-selection).

The admin API stores operator cap policy in
`operator_settings.provider_admin`.  Execution passes `admin_db_path` to
`reserve_provider_spend(...)`; the reservation code reads those caps and checks
them in the same SQLite transaction that inserts the reservation.

The Providers UI spend totals are a read-only projection over `provider_spend`.
If the ledger is absent or unreadable, the UI reports spend as unknown.  An
empty readable ledger reports zero spend.

Completion estimates include prompt batches and generated candidates
(`n` and `best_of`). For paid chat execution, estimation uses the normalized
outbound limit described above. The generic estimator takes the maximum of
all supported limit fields; unsupported fields must first be rejected or
normalized by the executor.
Estimation follows the actual request operation, so a
provider that supports both chat and decisions uses the appropriate price
estimate for each call. Invalid streaming options fail before reserving spend;
known failures before transport dispatch release their reservation.

An absent provider `allowed_plans` restriction preserves the profile's provider
permission and appears as `null` in admin JSON. An explicit empty list denies
every plan. Updating only caps keeps
that distinction. Client receipt serialization omits `cost.provider` for both
newly created and persisted receipts; admin receipts retain it. Spend reports
group historical provider IDs under their canonical lowercase IDs, matching
cap enforcement.

## Error Semantics

- Missing provider key or denied access plan: fail before the provider call.
- Provider 401 or 403: return gateway `502 provider_auth_failed`.
- Provider timeout: return `504 provider_timeout`.
- LiteLLM upstream timeout responses (HTTP 408): return `504 provider_timeout`.
- Provider rate limit: return `429 provider_rate_limited` with `Retry-After`
  when supplied.
- Unknown cost with an active cap: fail closed before the provider call.

## Deployment Notes

Set provider keys in the gateway service environment, for example:

```bash
OPENROUTER_API_KEY=...
TYPESAFE_API_KEY=...
```

Do not restart the live service directly.  Use the repo's safe restart
procedure when deployment changes require a restart.  A provider with an unset
env var appears in admin JSON as `key_status.state == "missing"` and is not
called.

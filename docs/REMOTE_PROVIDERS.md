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
caps by racing each other.

The admin API stores operator cap policy in
`operator_settings.provider_admin`.  Execution passes `admin_db_path` to
`reserve_provider_spend(...)`; the reservation code reads those caps and checks
them in the same SQLite transaction that inserts the reservation.

The Providers UI spend totals are a read-only projection over `provider_spend`.
If the ledger is absent or unreadable, the UI reports spend as unknown.  An
empty readable ledger reports zero spend.

Completion estimates include prompt batches and generated candidates
(`n` and `best_of`). When output-limit fields conflict, reservation uses the
maximum positive limit across `max_tokens`, `max_completion_tokens`, and
`max_output_tokens`, because adapters can select different fields.
Estimation follows the actual request operation, so a
provider that supports both chat and decisions uses the appropriate price
estimate for each call. Invalid streaming options fail before reserving spend;
known failures before transport dispatch release their reservation.

An absent provider `allowed_plans` restriction preserves the profile's provider
permission and appears as `null` in admin JSON. An explicit empty list denies
every plan. Updating only caps keeps
that distinction. Client receipt serialization omits `cost.provider` for both
newly created and persisted receipts; admin receipts retain it.

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

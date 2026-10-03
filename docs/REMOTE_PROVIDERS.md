# Remote Providers

Remote providers are paid or externally hosted backends that the gateway can
reach only when the owner configured them and the caller's access plan permits
them. Owners can enter a write-only key in Providers or reference an environment
variable. UI-entered values are encrypted in `admin.db`; the registry carries
references, never plaintext keys.

## Connect a provider

Open **Providers** and select **Add provider**. Choose a shipped preset or a
generic compatible endpoint, enter a key or environment-variable name, and
test the connection. The test lists models; presets can also declare an
authentication probe when model listing alone does not verify the key.
Choose models, review their input/output prices, set daily and monthly caps,
and select the access plans that may use the provider. Missing prices require
operator input before enabling a paid model.

Saved configuration updates the registry and generated LiteLLM configuration.
The gateway applies registry changes live. The managed LiteLLM launcher reloads
its child when the configuration or stored credentials change. Allow the
configured watcher interval for that reconciliation; saving a record does not
prove that the upstream provider or execution sidecar is ready.

## Catalog data

`providers/catalog/*.yml` holds provider setup facts: names and logo keys, base
URLs, protocols, authentication headers, model-list paths and price mappings,
documentation/key URLs, supported billing dimensions, and recommendations.
Add a catalog file to extend setup choices. Generic presets let an operator
connect another compatible service without a vendor-specific code change.
Catalog files are provisioning data; saved service records remain the runtime
source of truth. Missing model-list prices mean unknown, not free.

## Key custody and threat model

The provider secret store encrypts values with AES-256-GCM and a fresh nonce
for each write. Ciphertext is bound to its secret record identity. A separate
`provider-secrets.key` file in the directory containing `admin.db` supplies the
encryption key; its permissions are 0600. APIs return only whether a key is
set, its last four characters, and its update time. Owners can replace or
delete a key; no API retrieves its value.

Generated LiteLLM YAML contains `os.environ/` references. The managed launcher
decrypts UI-entered keys into the sidecar's environment immediately before
starting it. No plaintext provider key is written into YAML or command
arguments. Environment-variable references remain an alternative and do not
copy the environment value into the secret store.

This protects a database copy taken without its encryption key. It does not
protect against an attacker who controls the gateway's operating-system
account, can read both files, or can inspect gateway/sidecar process memory.
That account and privileged processes can also inspect the child environment.
Use HTTPS for browser-to-gateway traffic and provider endpoints, restrict
filesystem access, and keep request-body logging disabled on credential APIs.
Owner authentication and CSRF checks protect writes; upstream response bodies
and authentication headers must not become logs, receipts, or API errors.

Back up the database and encryption key with restricted access. Restoring only
the database cannot restore stored provider credentials. Losing the key file
requires entering replacement provider credentials. Rotating a provider key
replaces that credential, not the encryption key. Deleting a secret removes
the active record; historical backups may still contain its ciphertext.

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
- `api_key_env`: the environment variable name used by LiteLLM. For an
  environment reference, deployment supplies its value. For an encrypted UI
  key, the gateway supplies it only to the execution process at runtime.
- `models`: the provider-qualified public model names the gateway may route.
- `price`: optional cost table used for pre-call estimates. If a capped request
  has no bounded cost estimate, the request fails closed before the provider
  call.

Inline provider keys are rejected by the service registry. Use the Providers
credential API or an `api_key_env` reference; do not put raw keys in YAML,
registry records, receipts, or logs. Remote or paid
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

Model names and selected price-table indicators for `search`, `online`, or
`realtime` also require the matching `price.dimensions` declaration. This
includes implicit search models that enable billing without a request option.
For example, `price.dimensions.search: true` permits a search model; its full
response must still provide enough information for actual-cost settlement.

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
details. Streaming settlement uses LiteLLM's complete assembled response, retaining
annotations, citations, and other response metadata as well as merged provider
usage. Missing usage or incomplete assembly leaves cost unknown; a malformed
or oversized stream cannot settle from a partial response. The client still
receives the original stream bytes.
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

Alternatively, set provider keys in the gateway service environment, for example:

```bash
OPENROUTER_API_KEY=...
TYPESAFE_API_KEY=...
```

Do not restart the live service directly.  Use the repo's safe restart
procedure when deployment changes require a restart.  A provider with an unset
env var appears in admin JSON as `key_status.state == "missing"` and is not
called.

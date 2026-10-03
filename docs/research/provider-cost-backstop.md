# Provider cost backstop research

Date: 2026-10-03

Scope: answer whether OpenRouter can enforce a per-request remaining-dollar cap, how to get actual cost after dispatch, where pinned LiteLLM surfaces provider-reported cost, and how to handle OpenRouter model suffix modifiers without provider-specific production code.

## Sources checked

- Local research index: `~/code/dotfiles/ai/research/INDEX.md` was checked before web research, per repo policy. I did not find an existing entry that answered OpenRouter `provider.max_price` or `usage.cost` semantics directly.
- OpenRouter documentation index: <https://openrouter.ai/docs/llms.txt>
- OpenRouter generation metadata API: <https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation>
- OpenRouter provider selection markdown: <https://openrouter.ai/docs/guides/routing/provider-selection.md>
- OpenRouter model variants markdown: <https://openrouter.ai/docs/guides/routing/model-variants/overview.md>
- OpenRouter online variant: <https://openrouter.ai/docs/guides/routing/model-variants/online>
- OpenRouter nitro variant: <https://openrouter.ai/docs/guides/routing/model-variants/nitro>
- OpenRouter web search server tool: <https://openrouter.ai/docs/guides/features/server-tools/web-search>
- OpenRouter models API reference: <https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties>
- OpenRouter spend controls: <https://openrouter.ai/docs/guides/best-practices/spend-controls.md>
- Pinned local LiteLLM source: `/home/tnunamak/.tmp/litellm-inspect.vPs9Ez/venv/lib/python3.12/site-packages/litellm/llms/openrouter/chat/transformation.py`
- Pinned local LiteLLM source: `/home/tnunamak/.tmp/litellm-inspect.vPs9Ez/venv/lib/python3.12/site-packages/litellm/cost_calculator.py`

## Findings

### 1. OpenRouter has final actual-cost fields, but they are settlement data, not pre-dispatch enforcement

The generation metadata endpoint returns request-level billing information after a generation exists. The example response includes `total_cost`, `upstream_inference_cost`, `usage`, token counts, provider name, model, and whether the response streamed. That makes `/api/v1/generation?id=...` a usable settlement/backstop source when the completion response itself does not carry enough cost detail.

OpenRouter chat responses can also include cost in the response `usage` object when the request asks for usage. Pinned LiteLLM’s OpenRouter adapter relies on that behavior: in `OpenrouterConfig.transform_request`, it adds `usage: {"include": true}` if absent, and in `transform_response` it reads `response_json["usage"].get("cost")` into the hidden header key `llm_provider-x-litellm-response-cost`.

OpenRouter’s image docs show the same pattern for images: the final response/stream completion event can include `usage.cost`. This reinforces the general rule: actual provider-side cost is available after completion, not before dispatch.

### 2. `provider.max_price` is a provider price/rate filter, not a remaining-dollar cap

OpenRouter’s provider selection docs list `provider.max_price` as request-scoped routing input: “The maximum pricing you want to pay for this request.” The concrete examples and SDK docs use shape `provider: { max_price: { prompt: 0.5, completion: 2 } }`.

The clearer primary wording is in OpenRouter’s current provider-selection markdown and related integration docs: these values cap prompt/completion prices per million tokens. OpenRouter’s models API also exposes `max_price` query filtering as maximum prompt price in dollars per million tokens, and model `pricing` fields are per-token strings such as prompt and completion.

Decision: do not treat `provider.max_price` as `remaining_budget_usd`. It cannot bound total request spend because total spend is the product of rate, input units, output units, tool calls, cache writes/reads, service tier, media, and provider-specific dimensions. It can only reject providers/endpoints whose advertised rates are above configured ceilings.

Useful but bounded use: inject `provider.max_price` from data-configured rate ceilings for OpenRouter models if the model’s priced dimensions are known. This reduces surprise routing to expensive endpoints, but the gateway still needs its own reservation and post-call settlement.

### 3. Pinned LiteLLM has a provider-reported-cost seam, but it is not a hard-cap mechanism

Pinned LiteLLM source:

- `llms/openrouter/chat/transformation.py`: `transform_request` ensures `usage.include` for OpenRouter requests.
- The same file’s `transform_response` stores `usage.cost` into `_hidden_params["additional_headers"]["llm_provider-x-litellm-response-cost"]`.
- `cost_calculator.py:get_response_cost_from_hidden_params` returns that hidden header as a float.
- `response_cost_calculator` prefers that provider response cost before falling back to `completion_cost(...)`.

This is the right seam for reporting and reconciliation when the gateway uses LiteLLM. It is not a way to stop a request mid-flight when the generated output exceeds estimate. It only exists after the provider response is available.

### 4. Recommended gateway backstop

Minimal, engine-agnostic behavior:

1. Keep pre-dispatch reservation based on the gateway’s normalized request body and model pricing table.
2. For OpenRouter-like providers that can report actual cost, configure a provider capability field such as `actual_cost_source` with values like `response_usage_cost` and `generation_metadata`. This is data, not provider-name branching.
3. During paid execution, require one of these after dispatch when configured:
   - final response `usage.cost`, or
   - provider metadata lookup by generation ID/request ID.
4. Settle ledger against the actual value when available; otherwise settle against the gateway-estimated value only if the provider/model config explicitly permits estimated settlement.
5. If actual cost exceeds reservation, record an overage debt/negative balance or reject future spend immediately. Do not pretend `max_price` prevents this case.
6. Use configured `provider.max_price` only as a rate-ceiling hint/guard. The field should be derived from provider data and pricing policy, not from per-request remaining balance.

The hard boundary: no provider can guarantee a per-request total cap from `max_price` alone unless it accepts a total-budget parameter. I found no OpenRouter primary-source parameter that does that for chat completions.

### 5. Output-limit and priced-dimension hazards to keep covered

The final pre-dispatch estimate must be based on the exact sanitized body that goes over the wire. Watch these dimensions because they change billing or make actual cost exceed the estimate:

- chat output limits: `max_tokens`, `max_completion_tokens`, provider-specific aliases;
- reasoning budgets and effort fields that can create reasoning tokens;
- service tier / priority / flex routing;
- web search or server tool calls, especially `openrouter:web_search`, which has per-request search charges;
- image/audio/video dimensions: count, duration, resolution, quality, references, and modality-specific usage units;
- cache write/read pricing where write can be more expensive and read can be discounted;
- BYOK accounting if provider reports an inference-cost value different from the gateway’s catalog estimate.

For cost-priced allowlist behavior, the safe default remains: reject paid requests that contain a supported provider feature whose price dimension is not represented in the provider/model price data.

## DATA-only guard for OpenRouter-style suffix modifiers

OpenRouter documents two categories of suffixes:

- catalog variants, such as `:free`, can have their own catalog metadata and pricing;
- routing variants, such as `:nitro`, `:floor`, `:exacto`, and deprecated `:online`, are accepted at request time and change routing or tooling while the base model metadata remains the same.

The model-variants docs say routing variants are not model catalog entries and should be dropped when resolving metadata. They also say only listed suffixes should be treated as variants.

Recommended implementation shape without provider-name code:

```yaml
providers:
  openrouter:
    model_suffix_policy:
      unknown_suffix: reject_paid
      catalog_suffixes:
        - suffix: ":free"
          catalog_priced: true
          default_paid: reject_unless_declared_model
      routing_suffixes:
        - suffix: ":nitro"
          effect: provider_sort_throughput_priority_eligible
          default_paid: reject
          requires_pricing_dimensions: [prompt, completion, service_tier]
        - suffix: ":floor"
          effect: provider_sort_price_flex_eligible
          default_paid: reject
          requires_pricing_dimensions: [prompt, completion, service_tier]
        - suffix: ":exacto"
          effect: provider_sort_quality
          default_paid: reject
        - suffix: ":online"
          effect: web_search_plugin_deprecated
          default_paid: reject
          requires_pricing_dimensions: [web_search_request]
```

The generic compiler/admin path should consume `model_suffix_policy` as data:

1. Split configured provider model IDs into base model plus suffix tokens.
2. Resolve catalog metadata with data rules: keep catalog suffixes, drop routing suffixes.
3. For paid models, reject unknown suffixes and reject routing suffixes by default unless the provider config declares them allowed and all affected price dimensions are present.
4. Let the admin UI expose these as editable provider config rows/presets. Seed OpenRouter defaults as data only.
5. Do not add `if provider == "openrouter"` or hard-coded `:nitro`/`:online` production branches. Tests can assert seeded data behavior through the same generic suffix-policy mechanism.

Practical call: make `:online` default-deny for paid traffic. OpenRouter now marks it deprecated and recommends the `openrouter:web_search` server tool. That server tool has separately documented usage and pricing, which fits better with explicit priced-dimension validation.

## Confidence and limits

Confidence: high on `max_price` semantics. Primary docs consistently describe rate ceilings, with prompt/completion examples and per-million model filters. Confidence: high that LiteLLM can surface OpenRouter `usage.cost` through hidden params for non-streaming transformed responses. Confidence: medium on streaming details because the final SSE usage/cost behavior can vary by endpoint; generation metadata is the more robust settlement fallback when a generation ID is available.

I did not call live OpenRouter APIs or use secrets. This research used public docs and pinned local LiteLLM source only.

## Appendix: pinned LiteLLM Anthropic cache-cost oracle

Parent asked for the exact pinned `completion_cost`/`cost_per_token` behavior for the failing synthetic cache fixture.

Using the pinned LiteLLM environment at `/home/tnunamak/.tmp/litellm-inspect.vPs9Ez/venv`, `litellm.get_model_info("anthropic/claude-sonnet-4-5-20250929")` returns these relevant rates:

- input: `0.000003` per token
- output: `0.000015` per token
- 5-minute cache creation: `0.00000375` per token
- 1-hour cache creation: `0.000006` per token
- cache read: `0.0000003` per token

The pinned Anthropic calculator routes through `litellm.llms.anthropic.cost_calculation.cost_per_token`, which first calls the generic calculator and then preserves cache costs separately from geo/speed multipliers. For 1-hour cache creation, the cache duration must be present in `prompt_tokens_details.cache_creation_token_details`.

Oracle command:

```python
from litellm.cost_calculator import cost_per_token
from litellm.types.utils import Usage

model = "claude-sonnet-4-5-20250929"
usage = Usage(
    prompt_tokens=10000,
    completion_tokens=1,
    total_tokens=20001,
    prompt_tokens_details={
        "cache_creation_tokens": 10000,
        "cache_creation_token_details": {"ephemeral_1h_input_tokens": 10000},
        "cache_hit_tokens": 0,
    },
)
print(cost_per_token(model=model, custom_llm_provider="anthropic", usage_object=usage))
```

Expected tuple: `(0.060000000000000005, 1.5e-05)`, total `0.060015000000000006`.

If the same fixture is represented only as `cache_creation_input_tokens=10000` without `prompt_tokens_details.cache_creation_token_details`, pinned LiteLLM prices it at the normal 5-minute cache write rate and returns `0.0375` prompt cost for zero completion. That is not the 1-hour premium path.

This matters for normalized OpenAI-style full usage: OpenAI-style `prompt_tokens` can include cached/cache-write tokens, while Anthropic `input_tokens` semantics can exclude or separately report cache dimensions. The generic gateway usage object should preserve the provider's explicit cache detail fields and duration detail, then feed the exact full-usage shape to the LiteLLM oracle. Do not infer 1-hour cache creation from `cache_creation_input_tokens` alone.

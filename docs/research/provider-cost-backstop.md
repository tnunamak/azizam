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

## Appendix: pinned LiteLLM local-catalog underbill with 1-hour cache writes

Date: 2026-10-03 follow-up.

Scope: diagnose why the hermetic cache fixture returned only the completion-token cost when the helper used pinned LiteLLM with `LITELLM_LOCAL_MODEL_COST_MAP=true`.

No network was used for this appendix. Evidence came from the pinned LiteLLM venv and its packaged model metadata at `/home/tnunamak/.tmp/litellm-inspect.vPs9Ez/venv/lib/python3.12/site-packages/litellm`.

### Reproduction

Fixture shape:

```python
model = "anthropic/claude-sonnet-4-5-20250929"
usage = {
    "input_tokens": 10000,
    "output_tokens": 1,
    "prompt_tokens": 10000,
    "completion_tokens": 1,
    "total_tokens": 10001,
    "prompt_tokens_details": {
        "cache_creation_tokens": 10000,
        "cache_creation_token_details": {
            "ephemeral_1h_input_tokens": 10000,
        },
        "cache_hit_tokens": 0,
    },
}
```

Observed with the pinned venv:

| `LITELLM_LOCAL_MODEL_COST_MAP` | `litellm.get_model_info(...).cache_creation_input_token_cost_above_1hr` | `completion_cost(...)` |
| --- | ---: | ---: |
| unset | `6e-06` | `0.060015000000000006` |
| `false` | `6e-06` | `0.060015000000000006` |
| `true` | `None` | `0.000015` |

The packaged backup/local metadata for `claude-sonnet-4-5-20250929` contains the normal 5-minute cache write price but lacks the 1-hour cache write price:

```text
cache_creation_input_token_cost: 3.75e-06
cache_creation_input_token_cost_above_1hr: None
cache_read_input_token_cost: 3e-07
input_cost_per_token: 3e-06
output_cost_per_token: 1.5e-05
```

### Root cause

Pinned LiteLLM's calculator is still the right oracle, but it does not fail closed when the active model catalog lacks a price key for a positive usage dimension.

Relevant local source behavior:

- `litellm_core_utils/llm_cost_calc/utils.py:_get_token_base_cost` reads `cache_creation_input_token_cost_above_1hr` from model info.
- `calculate_cache_writing_cost` multiplies `ephemeral_1h_input_tokens` by that 1-hour rate.
- With the stale local catalog, the 1-hour rate is missing, and the cache-write portion is effectively dropped instead of raising.

This explains the hermetic result: the output token is still priced (`1 * 0.000015`), but the 10,000 one-hour cache creation tokens are priced at zero.

### Required guard

Do not add manual cache-pricing formulas in gateway code. Keep the SDK oracle, but trust its returned amount only when the active SDK/model catalog is complete for every positive billed usage dimension in the response.

The generic guard should be data/table driven:

```python
_REQUIRED_PRICE_KEYS_BY_USAGE_PATH = {
    ("prompt_tokens_details", "cache_creation_token_details", "ephemeral_1h_input_tokens"):
        ("cache_creation_input_token_cost_above_1hr",),
    ("prompt_tokens_details", "cache_creation_token_details", "ephemeral_5m_input_tokens"):
        ("cache_creation_input_token_cost",),
    ("prompt_tokens_details", "cache_creation_tokens"):
        ("cache_creation_input_token_cost",),
    ("prompt_tokens_details", "cache_hit_tokens"):
        ("cache_read_input_token_cost",),
}
```

For each positive usage path, check `litellm.get_model_info(model)` or an operator-provided model price override for the required key. If any required price key is missing or `None`, return unknown cost. The reservation must remain open for manual/provider reconciliation instead of silently settling an underbilled amount.

Expected behavior:

- Fresh/complete SDK catalog: the fixture settles via SDK at `0.060015000`.
- Stale/incomplete local SDK catalog: the helper returns unknown, leaving the reservation open.

This preserves the “actual provider or SDK oracle only” decision while closing the specific silent-underbill failure mode.

## Round-5 streaming search accounting

The independent review reproduced ten sequential mocked streams from a declared search model under a $0.01 daily cap. The gateway retained only usage-bearing events, losing the earlier citation annotation. Each call therefore settled token cost $0.000035, releasing its concurrency slot. The complete responses priced by the pinned SDK totaled $0.350350.

The new test-first hermetic regression independently compared a complete `ModelResponse` with citation annotations against the handler's streamed result. The SDK oracle returns $0.035035 for ten prompt tokens, one completion token, and one search charge using the test catalog snapshot. Before implementation, all ten requests returned 200. Default guards also admitted search/realtime model names and a search-price table without a declared dimension.

Local primary source inspected: LiteLLM 1.86.2 `litellm/main.py:7487` (`stream_chunk_builder`) and `litellm/litellm_core_utils/streaming_chunk_builder_utils.py` (`ChunkProcessor`). The builder collects `delta.annotations` across chunks into the final message, combines provider-specific search/citation metadata, and assembles tool, reasoning, audio, and image content. Its base-response construction requires chunk identity, object, creation time, model, choices, and assistant role metadata. Gateway normalization supplies missing envelope defaults while retaining received fields.

Settlement must use this complete SDK assembly, overlaid with the original merged provider usage. SDK-generated token counts cannot substitute for missing vendor usage. Failed, malformed, truncated, or oversized assembly must retain unknown cost and the open reservation rather than price a partial response. Stream bytes remain unchanged for the client.

The second defense is generic billing-dimension data: names and selected price-table indicators for search, online, or realtime require matching declarations in `price.dimensions`. This adds no vendor or engine-specific parser.

No vendor calls, credential reads, or new web research were used for this investigation.

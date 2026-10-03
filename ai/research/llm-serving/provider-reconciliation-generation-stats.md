# Provider Reconciliation Generation Stats

Date: 2026-10-03

Scope: capture the provider-generation metadata evidence used for the generic reconciliation lookup. This lane is prohibited from writing to the external research corpus, so this note lives in the worktree under `ai/research/llm-serving/`.

## Sources Checked

- Local research index: `/home/tnunamak/code/dotfiles/ai/research/INDEX.md`, searched for OpenRouter and generation metadata before web research. The relevant existing worktree note is `docs/research/provider-cost-backstop.md`.
- OpenRouter official API reference: <https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation.md>

## Findings

OpenRouter documents a `GET /api/v1/generation` endpoint for request and usage metadata. Its example uses a bearer token and query parameter `id=gen-1234567890`. The example response places the final generation cost at `data.total_cost` and also includes routing, provider, token, timing, request, and stream metadata.

Implementation consequence: OpenRouter can be seeded as data with:

```yaml
generation_stats:
  path: /generation
  id_param: id
  cost_path:
    - data
    - total_cost
```

The runtime lookup should stay provider-agnostic: use the provider's configured `base_url`, configured auth env reference, configured path, configured query parameter, and configured cost path. It should return unknown on any failure, missing key, malformed JSON, redirect, timeout, or non-200 response.

No live OpenRouter API calls were made, and no secrets were read or printed.

## Evidence limits and implementation decisions

The local `docs/research/provider-cost-backstop.md` already identifies generation
metadata as settlement data, not admission enforcement. No source inspected
establishes an upper bound on generation lookup propagation delay. The runtime
therefore does not treat unavailable metadata as a zero bill, and retries on a
later admission. There was no evidence supporting a provider-independent
request-dollar cap parameter, so this change adds none.

| Field | Source shape | Use |
| --- | --- | --- |
| Generation ID | completion/event `id` | correlate the metadata request |
| Request URL | provider base URL plus `/generation` | same configured origin and auth reference |
| Query | `id=<generation ID>` | provider generation identity |
| Final bill | `data.total_cost` | actual USD settlement, including a verified zero |
| HTTP failure, absent bill, malformed bill | unavailable evidence | retain needs-reconciliation state |

The source's relevant response fields are captured above. Ancillary timing,
routing, and token diagnostics are not used for admission or settlement. No
live account probe was authorized in this lane; regression evidence uses
`httpx.MockTransport` and pinned LiteLLM 1.86.2. Stats results are validated for
finite nonnegative USD and representability in the ledger's decimal quantum.
Automatic and owner reconciliation race tests check the ledger's first actual
settlement wins and the completing receipt reports that persisted value.

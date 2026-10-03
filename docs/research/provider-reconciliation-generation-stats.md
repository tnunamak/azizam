# Provider Reconciliation Generation Stats

Date: 2026-10-03

Scope: capture the provider-generation metadata evidence used for the generic reconciliation lookup. This lane is prohibited from writing to the external research corpus, so this note lives in the worktree under `docs/research/`.

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

# Provider catalog and key custody

Date: 2026-10-03

Used by: provider catalog, Providers setup, and
[remote provider documentation](../../docs/REMOTE_PROVIDERS.md).

## Questions and existing evidence

Which connection, authentication, model-listing, and price facts belong in
provider preset data? How can UI-entered credentials reach the execution
sidecar without plaintext configuration files?

Read `ai/research/INDEX.md` before browsing. Read its LiteLLM control-plane
entry, `docs/research/provider-cost-backstop.md`, the configuration ownership
document, and the existing provider execution/configuration modules. The
existing cost research establishes that OpenRouter model-list prices are
per-token strings and that provider credit limits are a useful backstop.
Neither entry covers the other listing shapes or an encrypted write-only store.

The incumbent interface provides status and cap controls but no provider
creation action. Its empty state cannot complete the setup task. The shipped
Dialog, field, table, and skeleton components provide the existing visual and
interaction pattern; this lane extends that pattern.

## Primary sources read

| Source | Connection and response evidence | Consequence |
|---|---|---|
| [OpenRouter model listing](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties) | The response contains `data` rows with `id`, `name`, and `pricing.prompt` / `pricing.completion` strings. Example rates are 0.00003 and 0.00006. Its query price filters explicitly use dollars per million tokens. | Normalize response rates separately from query filter units; the catalog carries paths and scale. Do not copy an example rate as a current model price. |
| [OpenRouter current key](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key) | `GET https://openrouter.ai/api/v1/key` uses a Bearer header, returns key information, and documents HTTP 401. The model catalog alone does not prove credential validity. | A data-configured authentication probe can precede model listing. Do not return the key-info response wholesale to the UI. |
| [OpenAI model listing](https://developers.openai.com/api/reference/resources/models/methods/list) | `GET /v1/models` lists model identity and ownership, rather than a complete billing table. | Leave missing rates unknown until the operator supplies them. |
| [Anthropic model listing](https://platform.claude.com/docs/en/api/models/list) | `GET https://api.anthropic.com/v1/models` uses `X-Api-Key` and `anthropic-version: 2023-06-01`. The list has `data`, `has_more`, and cursor IDs. | Authentication header and static headers are data. A partial first page must not be represented as an exhaustive catalog. |
| [Together model listing](https://docs.together.ai/reference/models) | `GET https://api.together.ai/v1/models` uses Bearer auth. Its example response is a bare array with `id`, `display_name`, type, and `pricing.input`, `output`, and `cached_input`. | The list path can be empty. The parser must handle an array root without a vendor branch. |
| [Together pricing](https://www.together.ai/pricing) | Serverless chat rates are displayed per one million tokens, with separate input/output/cache entries. The model-list example uses decimal rates such as 0.3. | Together rate scale differs from OpenRouter's per-token response rates. Do not assume a common scale. |
| [Groq models](https://console.groq.com/docs/models) | The documented listing endpoint is `https://api.groq.com/openai/v1/models`, with Bearer authentication. Pricing is presented separately from the SDK listing example. | Use the OpenAI-compatible endpoint and leave absent API pricing unknown. |
| [Mistral models](https://docs.mistral.ai/api/endpoint/models) | The curl example lists models at `https://api.mistral.ai/v1/models` using Bearer authentication. | Standard header and list configuration suffice. |
| [DeepSeek listing](https://api-docs.deepseek.com/api/list-models/) and [pricing](https://api-docs.deepseek.com/quick_start/pricing/) | A successful domain-filtered search found the trailing-slash listing page: `GET /models`, a `data` array, and `id` / optional `name` fields. Pricing documents the OpenAI-format base `https://api.deepseek.com` and rates per million tokens. [Its integration example](https://api-docs.deepseek.com/quick_start/agent_integrations/workbuddy/) uses Bearer auth. | Listing response metadata does not supply token prices in the documented schema. Use manual prices rather than the pricing-page examples. |
| [Google OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai) | The OpenAI SDK base is `https://generativelanguage.googleapis.com/v1beta/openai/`. Both chat and model-list examples use the same base. The REST listing uses a Bearer header. The document calls compatibility beta. | Configure the OpenAI-compatible base in data. Do not use the native Gemini models response parser or claim full protocol parity. |
| [TypeSafe models](https://docs.typesafe.ai/models) | `GET https://api.typesafe.ai/v1/models` uses Bearer auth. Rows live under `models`, with `name`, `description`, and `release_date`. The document says input tokens are charged and output tokens are free; rates are documented on the page rather than the list schema. | TypeSafe uses a `models` list and `name` ID path. Keep listing prices unknown unless explicitly authored. Execution code dispatches by System One protocol. |
| [TypeSafe index](https://docs.typesafe.ai/llms.txt) | This is the vendor's documentation index, used to locate models and SDK references. | Prefer the first-party schema over independent guides. |
| [Cryptography AEAD API](https://cryptography.io/en/latest/hazmat/primitives/aead/) | AES-GCM authenticates ciphertext and associated data. Nonces must not repeat under a key; decrypt rejects invalid authentication. | Use fresh random nonces, bind ciphertext to its record identity, and fail closed on a missing key or invalid ciphertext. The installed hash lock contains cryptography 46.0.7; newer APIs in the latest docs are not assumed available. |
| [Cryptography 46.0.7 AEAD API](https://cryptography.io/en/46.0.7/hazmat/primitives/aead/) | The versioned documentation confirms the AESGCM interface for the installed dependency. | Use the pinned implementation rather than newer encryption APIs. |

Short excerpts (one per source):

> Together: "Lists all of Together’s open-source models and metadata including pricing, chat template, and context."

> TypeSafe: "Charged per input token. Output tokens are free."

> Cryptography: "NEVER REUSE A NONCE with a key."

## Negative results and source limits

The initial search mistakenly used `site.docs` instead of a `site:` filter.
It returned independent Jev guides, generic papers, and discussion sites.
Those results were not used as implementation authority. The first-party
TypeSafe models page resolved the endpoint and row schema.

These requested pages failed to fetch: TypeSafe `/reference/api`,
`/api-reference`, and `/http-api.md`; Together `/reference/models-1`;
DeepSeek `/api/list-models`, `/api/list-models.md`, and `/api/get-user-models`.
Together's `/reference/models` succeeded. A later domain-filtered DeepSeek
search found the trailing-slash `/api/list-models/` page and its documented
schema. Direct fetch failures did not establish that the endpoint was absent.
No live provider call was made.

No real credentials were read. No provider requests were issued. Documentation
examples establish shapes, not account reachability or current model prices.
Catalog prices must come from the tested response or operator input. No static
example rate is sufficient evidence that a particular paid request is bounded.

## Design reasoning and threat boundary

Endpoint URLs, listing headers, response paths, numeric scales, and capability
facts can be authored as catalog data. Authentication fields are also data,
but must match the selected protocol adapter's supported scheme. The catalog
rejects other schemes so a successful listing check cannot imply unsupported
execution authentication. Listing uses generic HTTP; execution uses existing
LiteLLM protocol adapters or the System One seam. Adding a compatible vendor
requires a data row, not another branch in request handling.

AES-GCM protects a database copy without its key file. It cannot protect a
compromised gateway account that can read both files or inspect process memory.
The sidecar must obtain plaintext at execution time; child environment injection
keeps it out of generated YAML and command arguments. The managed watcher must
also notice secret rotation, since changing ciphertext need not change an
`os.environ/` reference. Backup guidance must require both database and key
while preserving access separation. Rotation changes provider credentials, not
the encryption key, and deletion cannot erase historical backups.

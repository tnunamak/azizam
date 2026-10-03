# Home client connection recipes

Date: 2026-10-03

Used by: `web/src/app/home/recipes.ts`, Home connection onboarding.

## Question and local evidence

Which settings connect the ten requested clients to a gateway using a
person's own key and a permitted stable alias? Which settings depend on the
client's network path or current configuration rules?

The implementation lane read the repository research index and the shared
dotfiles research index before browsing. The existing corpus covered Open
WebUI, LibreChat, SillyTavern, realtime and protocol facades, but did not settle
current Cursor or Codex setup. The parent also read the repository index.
The work used these local sources:

| Source read | Relevant evidence |
| --- | --- |
| `docs/LOCAL_CLIENT_RECIPES.md`, Open WebUI and LibreChat sections | Connections settings, canonical OpenAI URL, custom endpoint YAML with an environment key reference and model discovery |
| `docs/LOCAL_CLIENT_RECIPES.md`, Anthropic SDK section | Gateway root supports Claude Code's `/v1/messages`; bearer authentication works; local model names can produce a client cost-table warning |
| `docs/SDK_COMPATIBILITY_HARNESS.md` | SillyTavern Chat Completion / Custom OpenAI-compatible path and protocol compatibility coverage |
| `gateway/stable_aliases.py`, `gateway/local_capabilities.py`, public routing in `proxy.py` | Alias meaning, discovery rows and resolved request targets |
| Product strategy, UI spec and supplied `ui0` Apps screenshots | Quiet connection surface, reveal-once keys, exact settings, truthful availability, both themes and phone widths |

Visual precedent came from the supplied foundation screenshots and product
specification. There was no additional external visual-design search.

## Primary web sources read

| Source | Verified detail | Excerpt or limitation |
| --- | --- | --- |
| [OpenAI custom model providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers) | Top-level `model` and `model_provider`; provider table `name`, `base_url`, `env_key`, `wire_api = "responses"`; user-level configuration for an authentication redirect | “Custom providers can’t reuse the reserved built-in provider IDs.” Reserved names include `openai`, `ollama` and `lmstudio`. |
| [Cursor API keys](https://cursor.com/help/models-and-usage/api-keys) | Settings → Models → provider → paste key → Save. Custom keys cover chat; Tab retains built-in models. Keys travel through Cursor's servers for prompt construction and are not persisted there. | “Custom API keys only work with chat models.” This document does not specify the custom base-URL override or custom model entry. |
| [Claude Code gateway guide](https://code.claude.com/docs/en/llm-gateway) and [gateway connection guide](https://code.claude.com/docs/en/llm-gateway-connect) | `ANTHROPIC_BASE_URL`; `ANTHROPIC_AUTH_TOKEN` becomes bearer authorization, while `ANTHROPIC_API_KEY` uses `x-api-key`. `/status` checks the current connection. Shell exports affect processes launched from that shell; settings can take precedence. | “A gateway credential variable takes precedence over a saved” was the short excerpt retained by the implementation lane. |
| [Claude Code model configuration](https://code.claude.com/docs/en/model-config) | `ANTHROPIC_MODEL` selects the session model; `ANTHROPIC_DEFAULT_HAIKU_MODEL` controls the helper alias. | “ANTHROPIC_SMALL_FAST_MODEL is deprecated in favor of ANTHROPIC_DEFAULT_HAIKU_MODEL.” The recipe was corrected to the current variable. |
| [Cursor staff reply on global override](https://forum.cursor.com/t/the-custom-override-of-the-openai-base-url-is-unusable/152675/3), 2026-02-26 | Primary support reply confirms the `Override OpenAI Base URL` field applies across models and reports some Responses payloads. The reply also discusses HTTP/1.1 as a workaround for the reported TLS problem. | This is a staff support reply about a specific failure, not a complete client compatibility contract. Home does not prescribe the network workaround. |
| [Cursor staff routing explanation](https://forum.cursor.com/t/routing-ambiguity-when-custom-model-name-matches-a-built-in-cursor-model/161439), 2026-05-26 | Search-indexed primary reply confirms routing is controlled by the key plus the override, rather than renaming the model. It recommends endpoint failure or gateway logs to verify routing. | The full thread appeared in search; directly opening its individual `/5` reply failed. No successful Cursor run was claimed. |

The Cursor reachability statement is an inference from its documented server
request path: those servers cannot connect to a gateway address that exists
only on the person's localhost or private LAN. A remotely reachable gateway
is required. This does not prove that every custom local-model alias works in
the current Cursor release.

## Per-client settings and decisions

Every recipe substitutes the API-provided base URL and an API-advertised chat
alias. It never copies a minted key into the recipe or invents a fallback
model when the account has none.

| Client | Settings used | Documentation linked from Home |
| --- | --- | --- |
| Open WebUI | Settings → Connections → OpenAI; gateway base URL, saved key, advertised model; refresh models | [OpenAI-compatible provider setup](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible) |
| LibreChat | `endpoints.custom`, `apiKey: "${GATEWAY_API_KEY}"`, gateway `baseURL`, `models.default`, `fetch: true`; restart after YAML change | [Custom endpoint configuration](https://www.librechat.ai/docs/configuration/librechat_yaml/ai_endpoints) |
| SillyTavern | Chat Completion → Custom OpenAI-compatible; endpoint, saved key, custom model | [API connections](https://docs.sillytavern.app/usage/api-connections/) |
| Cursor | OpenAI key, Override OpenAI Base URL, custom model; explain server reachability and chat-only custom keys | [API key guide](https://cursor.com/help/models-and-usage/api-keys) |
| Codex | User-level TOML provider with `env_key`, `wire_api = "responses"`, gateway URL and model | [Custom model providers](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers) |
| Claude Code | Gateway root URL, bearer token from environment, session model and Haiku helper override | [Gateway connection guide](https://code.claude.com/docs/en/llm-gateway-connect) |
| Python | `OpenAI(base_url=..., api_key=os.environ[...])`; `chat.completions.create` | [OpenAI Python SDK](https://github.com/openai/openai-python#usage) |
| JavaScript | Server-side `OpenAI({baseURL, apiKey: process.env[...]})`; `chat.completions.create` | [OpenAI JavaScript SDK](https://github.com/openai/openai-node#usage) |
| curl | One POST to `/chat/completions`, bearer header and JSON model/messages | [Chat completion API](https://platform.openai.com/docs/api-reference/chat/create) |
| Phone | Existing Open WebUI in Safari/Chrome, with the owner configuring its server connection; add to home screen | [Open WebUI quick start](https://docs.openwebui.com/getting-started/quick-start/) |

The Open WebUI, LibreChat, SillyTavern, SDK, curl and Phone links are references
provided with the recipes; the lane used their existing local gateway
contracts rather than newly reading all these web pages. Phone is a browser
workflow, not a claim that a new native app exists.

## Searches, redirects and negative results

Initial implementation queries:

- `site.developers.openai.com codex custom model provider env_key wire_api base_url`
- `site.cursor.com docs api keys override base url OpenAI`
- `site.code.claude.com docs llm gateway ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN`

Search results included stale and third-party setup pages; those did not
establish the implementation. Direct official reads resolved the current
settings. The OpenAI `developers.openai.com/codex/config-advanced/` URL
redirected to `learn.chatgpt.com/docs/config-file/config-advanced`. The old
`docs.cursor.com/settings/api-keys` route redirected to the docs homepage;
the retry at `cursor.com/docs/settings/api-keys` led to the current help page.

`ANTHROPIC_MODEL` was absent from the gateway page, so the lane read the
separate model-configuration page. That read exposed the deprecated helper
variable. No unsupported `wire_api = "chat"` configuration was retained.

The parent checked Cursor's undocumented override with these queries:

- `site.cursor.com "Override OpenAI Base URL"`
- `site.cursor.com "Add model" "API"`
- `site.docs.cursor.com "base URL"`

Primary Cursor support replies corroborated the override and its limits.
Older user reports discussed custom-model entry but did not prove current
support. Third-party blogs and Reddit appeared in the results and were not
used as technical authority. The official guide still omits the custom-model
field. This leaves a client-specific limitation to verify with an actual
Cursor release and a reachable test gateway.

## Executed checks and remaining limits

The lane executed generated shell recipes with `/bin/sh`, mocked `curl` and
`claude` functions, and synthetic environment credentials. URLs and models
contained single quotes, `$()` and backticks. The exact URL, JSON model and
Claude environment values were preserved; no command substitution occurred.
The parent retained this regression in `web/tests/lib/home-recipes.spec.ts`.
Tests also check all ten recipes use an advertised alias and an empty alias
catalog returns no invented model.

The browser harness exercises the gateway, key creation, real public chat
dispatch, and receipt ownership using an isolated fake engine. It does not
launch all ten external clients. No paid provider requests were made. Home
tests chat only for an allowed `fast` target with a positive local execution
declaration; unknown targets receive one model-catalog request instead.

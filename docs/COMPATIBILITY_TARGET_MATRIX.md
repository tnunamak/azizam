# Long-Tail Compatibility Target Matrix

Date: 2026-05-15
Status: curated, machine-readable. Companion to `docs/SDK_COMPATIBILITY_HARNESS.md`.

This document inventories the long-tail self-hosted clients and agent
frameworks the gateway should remain compatible with. Each row says
**what the client expects, where it points, and which gateway endpoints
it exercises** — enough that a future worker can lift a row directly
into a `tests/compat/python/probe_<id>.py` or a Node probe without
re-doing the research.

The machine-readable source of truth is
[`tests/compat/matrix/long_tail_targets.json`](../tests/compat/matrix/long_tail_targets.json),
validated against
[`tests/compat/matrix/long_tail_targets.schema.json`](../tests/compat/matrix/long_tail_targets.schema.json).
This markdown is a human view; keep them in sync.

## Why a separate matrix

`SDK_COMPATIBILITY_HARNESS.md` covers the protocol-shaped SDKs (OpenAI,
Anthropic, Ollama, Gemini, ElevenLabs). The **target** matrix here
covers downstream consumers that *use* those protocols — chat
frontends, coding agents, automation runtimes, and voice assistants
that self-hosters actually deploy. A gateway can pass every SDK probe
and still break Open WebUI because of an envelope quirk; the long-tail
matrix is how we keep those targets in our sights.

## Scope

In scope:

- Self-hosted apps and CLIs the owner is likely to point at this proxy.
- Library/SDK frontends that wrap one of our supported protocols.
- Targets where a future probe is realistic without heavy installs.

Out of scope:

- Cloud-only products with no self-host story.
- Targets covered exhaustively by the existing per-SDK harness rows
  (OpenAI Python/Node, Anthropic Python/TS, Ollama Python/JS).

## How to read a row

Each target row in the JSON file has:

| Field | Meaning |
|---|---|
| `id` | Stable kebab-case key (also the probe-file suffix). |
| `category` | What kind of thing this is (chat frontend, coding agent, etc.). |
| `protocols` | Which gateway protocol surfaces it touches. |
| `client_config.base_url_template` | How the user points the client at the gateway. `${PROXY_BASE_URL}` is the gateway root. |
| `endpoints` | Gateway paths the client hits in normal use. |
| `smoke_cases` | Case-grid keys a probe should emit results for. |
| `known_quirks` | Behaviors that have historically broken self-hosters. |
| `priority` | `P0` blocks compatibility claims; `P1` is a high-value smoke target; `P2` is opportunistic. |
| `status` | `unverified`, `manual-smoke`, `automated-probe`, or `broken`. |

## Current targets at a glance

| ID | Name | Category | Protocols | Priority | Status |
|---|---|---|---|---|---|
| `open-webui` | Open WebUI | chat-frontend | OpenAI chat/embeddings/images/audio | P0 | manual-smoke |
| `librechat` | LibreChat | chat-frontend | OpenAI chat/images/audio | P0 | manual-smoke (API: models, chat, tools, images, speech, transcription; browser UI not driven) |
| `continue` | Continue | coding-agent | OpenAI chat/completions/embeddings | P0 | manual-smoke (CLI: streaming chat and tools; autocomplete and embeddings need the editor) |
| `cline` | Cline | coding-agent | OpenAI chat, Anthropic messages | P1 | unverified |
| `sillytavern` | SillyTavern | roleplay-frontend | OpenAI chat, Anthropic messages | P1 | unverified |
| `vercel-ai-sdk` | Vercel AI SDK | sdk-framework | OpenAI chat/responses, Anthropic messages | P0 | automated-probe (5 of 6 cases; Anthropic streaming not covered) |
| `n8n` | n8n | automation | OpenAI chat/embeddings/images/audio | P1 | unverified |
| `home-assistant-extended-openai` | HA Extended OpenAI Conversation | voice-assistant | OpenAI chat | P2 | unverified |

The Vercel AI SDK row rests on `tests/compat/node/vercel-ai-sdk.test.mjs`: its chat,
streaming, forced tool call, structured output and Responses API cases passed against a
live gateway on 2026-11-18. It runs by hand, not in the gate. Anthropic Messages
streaming needs the `@ai-sdk/anthropic` package and is not covered.

The LibreChat row rests on one run of real LibreChat (v0.8.9, stable image) against a
live gateway on 2026-11-19, driven through LibreChat's own HTTP API with a local user.
The gateway was a custom endpoint (`endpoints.custom[]`, `models.fetch: true`) reached
from the container through a `host-gateway` mapping. Every case passed: the model list,
streaming and non-streaming chat, an OpenAPI action call, `image_gen_oai` to an image
model, speech (`speech.tts.openai.url`) and transcription (`speech.stt.openai.url`).
LibreChat refuses private addresses unless they are in `allowedAddresses`, and an action
domain needs its scheme. The browser UI, embeddings and RAG uploads were not covered.

The Continue row rests on one run of the real Continue CLI (`@continuedev/cli` 1.5.47,
headless `cn -p`) against a live gateway on 2026-11-19. Streaming chat passed, a `Read`
tool call round-tripped, and an unknown model got one OpenAI-shaped 400
(`invalid_request_error`, `model_not_found`) with no retries. The CLI sends only streaming
chat, so `/v1/models`, non-streaming chat, autocomplete (`/v1/completions`) and embeddings
are not covered; those roles live in the editor extension.

`models.list` (`GET /v1/models`) appears in nearly every row. That
endpoint is load-bearing for almost every self-hosted frontend's
"is the provider reachable?" check.

## Lifting a row into a probe

A future worker should be able to follow this path with no extra
research:

1. Pick a row by `id`. Read the `client_config` and `endpoints` fields.
2. Drop a new module at `tests/compat/python/probe_<id>.py` (or a
   `node:test` file under `tests/compat/node/` for JS SDK targets).
3. Use the `smoke_cases` array as the probe's case-grid keys — each
   Python case becomes a `test_<case>` method or a `ProbeResult`
   emission; Node cases become `node:test` tests.
4. Honour `known_quirks` when constructing requests — those are bugs
   we've already paid for.
5. Register Python/unittest probes in `scripts/compat/run_matrix.py` so
   the cases show up in the matrix output. Node probes intentionally run
   as an isolated npm suite (`tests/compat/node`, `npm run probe:all`);
   add a dedicated Node JSON reporter before merging them into the matrix.

The probe should auto-skip when its driving client/SDK is missing,
matching the convention in `tests/compat/README.md`.

## Updating this matrix

- Edit `tests/compat/matrix/long_tail_targets.json` first; the JSON is
  the source of truth.
- Re-run `python -m unittest tests.test_compat_long_tail_matrix -v` to
  confirm the schema still validates.
- Sync the at-a-glance table above when adding/removing rows.
- Bump the file's top-level `generated_at` when the change is
  meaningful (new targets, status flips, quirk additions).

## Reference links per target

See the `references` array on each JSON row for the upstream docs and
source repository.

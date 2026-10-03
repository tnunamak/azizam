# AI Gateway Product Strategy

Status: product direction for the current release push.

This document is the product source of truth for the gateway. Where it conflicts
with older product or UI notes, this document wins unless a later decision record
updates it.

## Product promise

**Your own AI provider.** One endpoint and one key give existing clients every
supported capability: chat, images, video, speech, transcription, decisions,
embeddings, rerank, Realtime, MCP, Anthropic, Ollama, A1111, Open WebUI,
LibreChat, SillyTavern, coding agents, and SDKs. The gateway runs on the
operator's hardware first. It reaches remote providers only when the operator
chooses that path.

Principles, in order:

1. **Unopinionated and composable.** Speak vendor protocols exactly. Do not
   require a gateway-specific client. The owner decides policy; the product
   offers all supported options with safe defaults.
2. **Honest.** Do not show fake data, green states, or silent fallbacks. Every
   request has a receipt. Every limit is explained in plain language. Nothing
   costs money without an explicit, visible choice.
3. **Calm.** The system has many moving parts. The first screen of every surface
   must answer one question, then reveal detail when the user asks for it.
4. **Local-first, remote-capable.** Local engines and remote providers are
   first-class peers behind the same aliases and policy model.
5. **Simple over clever.** Use deep modules with small interfaces. Make state
   and effects explicit. Verify behavior. Avoid abstraction that only moves
   complexity.

## People

| Persona | Comes to | Must feel |
| --- | --- | --- |
| Owner | Set up the box, connect engines and providers, invite people, see health and spend | In control, never surprised |
| Member | Get an app working on a phone or laptop, make things, see their own work | It works, with nothing dangerous to break |
| Builder | Find an endpoint, a key, model names, capabilities, and receipts | Exact, documented, scriptable |
| Contributor | Understand, run, extend, and test the project | Clear boundaries and fast checks |

## Information architecture

The product is one web app with one shell. Role controls what navigation groups
appear. Members must not see admin vocabulary.

### Everyone: You

**Home** shows the endpoint URL, the user's keys, and a "Connect an app" gallery
of copyable recipe cards. Each card has exact settings for a client and a "Test
connection" action that makes one real cheap request and shows the result. Home
also shows what the user can use now as capability tiles, based on both policy
and live engine availability.

**Models** is a catalog grouped by capability. Each model card shows the
friendly name, stable alias, local or remote provider, speed hint, cost hint,
current availability, and a "Use this" action that copies the alias and an
example.

**Try** gives one small real-request panel per capability, such as chat, image,
speech, transcribe, and decide. It is a verification and onboarding tool, not a
chat app. The panel shows the result, time, receipt link, and copyable curl or
Python.

**Library** shows the user's generated images, video, and audio. It filters by
kind and date. A detail drawer shows provenance, download, and delete actions.
It is owner-scoped and never shows another person's media.

**Usage** shows the user's requests over time by capability and model, remote
cost when present, and receipt detail. It starts with a plain monthly summary.

**Apps & keys** shows the user's apps and keys.

### Owner: Gateway

**Status** shows what is true now: local engines, remote providers, GPU and VRAM,
queues, and reachability.

**Engines** shows local services. The navigation label changes from "Services"
to "Engines", but the existing Services route stays unchanged for compatibility.

**Providers** shows remote providers. It lets the owner enable or disable a
provider, enter an encrypted write-only key or use an environment reference, configure
model allow-lists and spend caps, view spend to date, and decide which access
plans may use it. It must state that requests go remote only when a model or
alias names a remote provider. The gateway must never fall back from free to
paid without an explicit alias, access-plan permission, and a receipt.

**People** shows people, invites, and access plans. "Invite someone" creates an
invite link with an access plan and expiry. The list shows each person's plan,
last activity, and usage this month. Access plans are the product label for the
current `profile` concept; keep `profile` internally for compatibility. Access
plans live as a tab here and keep the current "Test access" simulator.

**Activity** shows the audit log and receipts.

**Settings** covers sign-in, extensions, media admission, and defaults.

## First-run flows

Owner first-run starts at `/setup` and remains visible from Home until done:

1. Claim owner.
2. Connect a local engine or a remote provider.
3. Test the connection with a real request.
4. Connect the first app from a recipe card.

Each step shows its true state. A click is not success.

Member first-run starts with an invite link, then sign-in, then Home with a key
already created. The next action is to choose an app card and test it.

## Visual and interaction rules

Progressively disclose detail. Each surface starts with a one-line answer, then
a list, table, or cards. Put advanced settings behind an "Advanced" disclosure.

Use one icon and one color token per capability across Home, Models, Usage,
Library, and receipts.

Use exactly four status states in user-facing UI: Ready, Starting, Busy, and
Unavailable. Each state needs a reason sentence. Member UI must not expose engine
jargon.

Use direct money language. Local means "Local · no provider charge." Remote calls show
a price per request or per million tokens and a Paid badge. A paid call must not
sit one click away from a local call without that badge.

Empty states must teach. Say what the list will contain and name the one action
that will fill it.

Use sentence case and plain words. Use numbers with units. Keep the copy factual.

Design phone-first for member surfaces. Owner surfaces must also work at phone
width.

Meet WCAG AA contrast. Keep keyboard paths, focus rings, 44 px targets, and
reduced-motion behavior.

## Architecture decisions

Integration knowledge is data. Shipped provider catalog files declare endpoints,
protocols, authentication, model listing, pricing paths, and billing dimensions.
Operators can extend those files and configure providers through the UI. Code
dispatches by protocol, never by vendor name.

LiteLLM is the execution substrate for chat-shaped local and remote models. The
gateway is the one control plane for keys, people, access plans, budgets, spend
caps, encrypted write-only provider-key custody, and receipts. Environment
references remain an alternative to UI-entered keys. LiteLLM must
be pinned, hash-locked, and treated as a replaceable execution dependency. The
virtual-key bypass is to be retired for paid models.

The gateway must never fall back from free to paid without an explicit alias,
access-plan permission, and a receipt.

Decision routes use the TypeSafe `/v1/systemone` shape as canonical. The
gateway also supports `/typesafe/v1/*`. `/v1/decisions` is reserved for OpenAI
passthrough only until OpenAI publishes a schema.

Local decision execution uses `decider-2b` v11 Q8_0 on CPU as an on-demand
unit. `decider-4b` is an opt-in alias. Remote decision aliases may target
`typesafe/jev-latest`, OpenRouter models, or other configured remote providers.

Built-in accounts sit alongside OIDC so a home operator can invite people
without running an external identity provider. Invites are owner-created links
with an access plan and expiry.

Cross-user isolation is a release gate. The suite must include negative tests
over jobs, assets, receipts, and signed URLs.

Do not split `proxy.py` because it is large. New features should land as deep
modules with small interfaces and register from `proxy.py`. Extract an execution
boundary only behind behavior-preservation tests.

## Waves

**W0 truth and foundation** covers first-run proof, fresh-instance e2e, docs
truth, route matrix, LiteLLM decision updates, OSS hygiene, depersonalized
examples, and dependency pinning.

**W1 engines and providers** covers decisions, provider registry, encrypted keys
and environment references, LiteLLM routing, spend caps, cost receipts, policy, and
OpenAI `/v1/decisions` passthrough reservation.

**W2 accounts** covers built-in accounts, invites, cross-user isolation, and LLM
admission parity.

**W3 UI** covers the shell, role-aware information architecture, and the Home,
Connect, Models, Try, Library, Usage, Providers, People, and Activity surfaces.

**W4 polish and release-readiness** covers screenshot tours, README updates,
independent UX review fixes, and upgrade notes.

## Release bar

1. A new operator can run `docker compose up`, follow `/setup`, and get a
   successful chat in under 10 minutes, proven in CI by an automated
   fresh-instance e2e check with a tiny fake or real engine.
2. The owner can invite a member who connects an app with copy-paste settings
   and no admin help.
3. Every planned UI surface exists, uses real data, has designed empty, error,
   and loading states, passes phone and desktop checks, and passes an
   independent UX review.
4. Decisions and remote providers work end to end with receipts and spend caps.
5. OSS hygiene is current: contributing, security, code of conduct, issue and
   PR templates, depersonalized examples, pinned images, route support matrix,
   and screenshots.
6. The hermetic suite and CI are green, with no regression in the live clients
   the gateway is meant to support.

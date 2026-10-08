# Product

<!-- impeccable:product-schema 1 -->

Sources: the owner's own words, collected in `~/Documents/azizam/vision-from-logs.md` (quotes dated and attributed there), and `docs/PRODUCT_STRATEGY.md`. Where a fact is the repository's interpretation rather than the owner's statement, it says so. Open decisions are marked **Open**.

## Platform

web (one Next.js app with one shell; mobile web is a first-class target, not a reduced view)

## Users

- **Household member.** Invited by the owner. Often not technical, often on a phone. Job: get an AI app working, make things (chat, images, voice), and find their own work again. The owner's bar: "an experience my wife can understand and find delightful" (2026-06-14) and "an ai proxy instance I can confidently share with family members that is not a toy" (2026-10-03).
- **Owner/operator.** Runs the box. Job: connect local engines and remote providers, invite people, keep it healthy, control spend, and answer "it doesn't work" from a family member. Must feel in control and never surprised (repository strategy).
- **Builder.** Writes or configures apps and agents (Codex, Pi, Daisy, SDKs, LibreChat). Job: find the endpoint, a key, exact model names, capabilities and receipts. Must be exact and scriptable.
- **First-time open-source visitor.** Arrives from r/selfhosted or r/LocalLLaMA. Decides in minutes whether this is a real product or a toy. "r/selfhosted operators should find it delightful to use, too" (2026-05-17).

Admins are users with an extra view, not a separate product: "admins are users too right? don't apps usually have an admin nav from the normal user page" (2026-05-18).

## Product Purpose

Your own AI provider. One endpoint and one key give existing clients chat, images, video, speech, transcription, embeddings and more, served by the owner's own hardware first and by remote providers only when the owner chooses. Success: a household uses it daily without help; the owner can explain any failure in one look; the repository can be shared on technical forums "as an OSS project that ppl would pay for as a product (but it is free)" (2026-10-03).

## Positioning

A self-hosted gateway that speaks the vendors' own protocols exactly, so existing apps work unchanged, and that is honest about health, cost and data: every request has a receipt, nothing costs money without a visible choice, and failures name their layer. Neighbouring products (LiteLLM, OpenRouter, Open WebUI) are either developer proxies without a household product, or chat apps rather than a provider.

## Operating Context

- Runs on the owner's home server behind their reverse proxy; members reach it on the LAN or through a public hostname.
- Members use it mostly through other apps (Open WebUI, LibreChat, SillyTavern, phone apps, Home Assistant, coding agents) and visit the web UI to connect an app, try something, or find a creation.
- The owner visits when something is wrong, when adding an engine or person, or to check spend.
- Real clients today: Daisy (a Telegram agent), Pi, Codex CLI, LibreChat, SillyTavern.

## Capabilities and Constraints

- Capabilities: chat, images and image edits, video, speech, transcription, embeddings, rerank, music and sound effects, workflows (ComfyUI), decisions, Realtime, MCP, plus Anthropic, Ollama and A1111 protocol shapes.
- Principles in priority order (repository strategy): unopinionated and composable; honest; calm (each surface answers one question first); local-first and remote-capable; simple over clever.
- The owner's attention rule: "everything in the design that requires cognitive processing carries a cost" (2026-06-04), and "this burns my attention by being repetitive" (2026-10-04).
- Diagnosability is a hard requirement: "it's not clear if there is a gateway problem or a user problem or a model problem or workflow problem ... users shouldn't have this kind of trouble" (2026-10-02).
- Terminology: see `docs/design/VOICE.md` (glossary). One name per concept.
- **Open:** whether members see the Status page at all, or only a readiness line on Home.
- **Open:** guests as a distinct persona (not described by the owner).

## Brand Commitments

- Name: Azizam (adopted 2026-10-06). Domain: azizam.dev. The previous working name (Vivid Fish, vivid.fish) survives in the designer handoff art.
- No marketing voice inside the product: "we shouldn't have a big slogan like 'Build on AI in minutes' - that doesn't fit the vibe/identity" (2026-06-25).
- Binding visual authority, per the owner: the designer handoff in `docs/design/assets/reference-ui/` ("the absolute authority on look and feel and UX best practices"). Its visual grammar is binding input to the direction round; its marketing copy and metrics are illustrative only (see that folder's README).
- **Open (checkpoint T1):** the visual world. The shipped cream-and-serif look diverges from the designer handoff's dark, deep-water grammar; the owner picks between rendered directions.

## Evidence on Hand

- Designer handoff: 7 PNGs in `docs/design/assets/reference-ui/` (desktop admin overview, status, apps and access; mobile portal overview).
- UX audit corpus (2026-10-07): 34 source-level reports, on branch `ux-evidence` under `ux-evidence/reports/`.
- Screenshots of the current UI: `~/Documents/azizam/ux-findings/screens/2026-10-07/` (68 captures, thin seed).
- A lived-in household fixture is being built (branch `feat/household-seed`).
- Absent, and must not be fabricated: real usage statistics for marketing, testimonials, user counts, uptime claims.

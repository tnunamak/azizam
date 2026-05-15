# AI Gateway: Problem Statement

## Context

Local AI inference has reached a quality threshold where it's a credible substitute for hosted APIs across many tasks: chat, embeddings, TTS, STT, image generation, video generation, music generation, and sound effects. The local-inference toolchain is rich — ComfyUI, vLLM, llama.cpp, TabbyAPI, Kobold, ExLlamaV3, Voxtral, Parakeet, and many model-specific servers — and each speaks its own protocol.

At the same time, application developers have settled into using a small number of widely-adopted SDKs from hosted-AI vendors: OpenAI, Anthropic, ElevenLabs, and others. These SDKs are well-documented, type-safe, idiomatic in their target languages, and serve as the integration point for a growing ecosystem of agent frameworks, IDE extensions, and end-user products.

The gap between "I run my own models" and "I use a popular SDK against my own models" is currently bridged ad-hoc — per deployment, per stack, with one-off shims.

## The Core Problem

There is no widely-used way to expose a local inference stack such that:

- Multiple hosted-AI SDKs can target it as a drop-in `base_url`, simultaneously.
- Each capability (chat, TTS, music, etc.) is served by whichever local backend is best suited to it.
- Adding a new SDK or a new local backend doesn't require modifying core code.

Existing gateways tend to be opinionated in one of two directions:

- **Single-protocol, multi-backend.** Speak OpenAI's API; route to many local backends. Works well for chat-shaped work; awkward or impossible for capabilities OpenAI doesn't have endpoints for (music, video, voice design, conversational agents).
- **Per-product proxies.** One project mimics ElevenLabs; another mimics Anthropic; another mimics OpenAI. Users running multiple workloads have to run multiple proxies, each with its own auth, config, and observability surface.

Neither shape lets a user say *"I have local models for these capabilities; expose them through whichever SDK my application happens to already use."*

## Why This Is Hard

- **Protocol surfaces are large and inconsistent.** Each vendor's SDK has its own request shape, response shape, streaming conventions, error envelope, and auth header. SDKs validate responses against generated types, so partial compatibility fails fast.
- **Capabilities don't map 1:1 across protocols.** OpenAI has no music endpoint. ElevenLabs has limited tool-use semantics. Anthropic has no image generation. A federated gateway must handle gaps gracefully — return appropriate errors for unsupported routes, route to compatible backends when capabilities overlap.
- **Local backends vary in shape.** ComfyUI is graph-based and asynchronous. Voxtral is a streaming HTTP service. llama.cpp is already OpenAI-compatible. TabbyAPI and Kobold differ again. Each has its own idea of "model" and its own resource cost. The gateway cannot assume a uniform backend interface.
- **Resource orchestration is unavoidable.** Local hardware has a fixed VRAM budget. Models must be loaded, unloaded, and swapped. Some workloads compete; some compose. The gateway sits at the natural choke point for these decisions, but doing so without leaking hardware concerns into the protocol layer is non-trivial.
- **Operational concerns scale with surface area.** Auth, multi-tenancy, observability, rate-limiting, request retries, and audit logging are needed even for a single-user deployment, and become essential as more SDKs and backends are added.
- **Protocols evolve.** Vendors ship new endpoints and deprecate old ones. The gateway has to track those changes without rewrites.

## Who This Is For

- **Self-hosters** running local models who want to use applications written against hosted APIs without rewriting them.
- **Developers** building products that should work against both hosted and local inference, transparently.
- **Researchers and tinkerers** experimenting with new local models, who want familiar SDKs as their interaction layer.
- **Small teams** running shared local inference infrastructure, who need multi-tenant auth and observability without standing up a full ops platform.

## What Success Looks Like

A user can:

1. Install the gateway.
2. Point it at the local inference services they already run.
3. Configure which SDK protocols they want to expose.
4. Hand any compatible application an SDK `base_url` and an API key.
5. Have the application work — for every capability the gateway has a backend for — without modification.

A maintainer or contributor can:

1. Add support for a new SDK by writing one focused module, without touching unrelated protocols.
2. Add support for a new local backend by writing one focused module, without touching protocol code.
3. Reason about routing, auth, and resource policy in one place.

## Non-Goals

- Replacing the local inference engines themselves. The gateway is *only* the protocol/routing layer.
- Becoming an inference framework, training platform, or model hub.
- Providing a UI. The gateway is API-only; UIs are a separate concern.
- Reimplementing every feature of every SDK. Some capabilities (e.g. ElevenLabs voice cloning, OpenAI Assistants threads) may have no local equivalent and are out of scope by definition.
- Hosted deployment as a managed service. The gateway is designed to be self-hosted; a managed offering is a downstream decision, not a project goal.

## Open Questions

- What is the right level of abstraction between protocols and backends? A canonical "intent" type? Direct protocol-to-backend wiring? A hybrid?
- How are conflicts resolved when two SDKs use the same path (`/v1/models`) with different response shapes? Path namespacing, header-based dispatch, union responses, something else?
- How explicit should the routing config be? Fully declarative YAML? Code-level registration? Both?
- How do we handle capability gaps gracefully — return SDK-shaped 404s, route to a fallback, or surface the gap to the user?
- What does the plugin contract look like such that an out-of-tree plugin can ship without forking the project?
- How does the gateway represent resource orchestration (VRAM budgets, model swaps) in a way that's useful without being prescriptive?
- What's the right shape for observability — protocol-shaped logs, backend-shaped logs, or a normalized event stream?
- How do we test multi-protocol compatibility durably as SDKs evolve?

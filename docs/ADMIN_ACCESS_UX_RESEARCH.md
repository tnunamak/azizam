# Admin Access UX Research

Date: 2026-05-18

This note records the design research behind the admin access-management UI for
Vivid Fish AI Gateway. It is intentionally product-facing: the durable backend
model can stay explicit, but the UI should help operators complete access tasks
without hand-authoring policy documents.

## Durable Model

Keep the existing model:

```text
User or service account -> App -> Profile -> API keys
```

Use these nouns consistently:

- **App**: the thing using the gateway, such as LibreChat, Open WebUI,
  Continue, Home Assistant, a Telegram bot, a cron job, or a family member's
  device.
- **Profile**: a reusable access policy and runtime envelope. A Profile says
  what an App may use.
- **Key**: a secret credential issued to one App. Apps can have multiple keys
  for rotation.
- **Service**: a backend or workflow host, such as llama.cpp, ComfyUI, STT,
  TTS, or a custom HTTP tool.

The admin mental model should be:

```text
This App uses this Key, and the Key runs under this Profile.
```

Do not add another stored abstraction called Access, Plan, Template, or Grant
unless the implementation proves it needs one. Presets/templates are UI helpers
for generating existing objects.

## Prior Art

The useful pattern across mature developer products is: expose the operator's
intent first, then push raw permissions into advanced/custom views.

- Stripe restricted API keys expose key type, name, per-resource permissions,
  one-time reveal, and rotation/restriction actions. The raw security mechanics
  are not the first screen.
- Cloudflare API token templates start from a known task and let the user
  modify permissions/resources afterward.
- GitHub fine-grained personal access tokens ask for token name, expiration,
  resource owner/repository scope, and permission levels, while the endpoint
  mapping stays in documentation/reference material.
- Tailscale OAuth clients expose client identity, scopes, and tags. OAuth
  protocol details and token exchange mechanics stay out of the main admin
  decision path.

Reference docs:

- https://docs.stripe.com/keys
- https://developers.cloudflare.com/fundamentals/api/reference/template/
- https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens
- https://tailscale.com/kb/1215/oauth-clients

## UX Principles

1. **Start from a task, not a schema.**
   The primary flow is "Grant access to LibreChat" or "Create a family chat
   policy", not "create service account, profile id, app name, protocol,
   intent, tags, JSON".

2. **Keep Apps and Profiles separate.**
   Many Apps can share one Profile, and an App can move Profiles without
   rotating all of its keys.

3. **Keep Keys separate from Apps.**
   A single App may need current, staging, and retiring keys. Revoking a key
   should preserve App history.

4. **Make Profiles understandable as reusable policies.**
   A Profile should read like "Family chat: chat only, local stable routes,
   no cloud fallback" before it reads like JSON.

5. **Progressively disclose implementation details.**
   Profile ids, service account owner names, protocol selectors, exact
   intents, status visibility, native passthrough, and raw JSON belong in
   advanced sections.

6. **Treat media workflows as first-class risk/cost surfaces.**
   Image, audio, and video workflows have different latency, VRAM, storage,
   and safety implications from chat.

7. **Never grant newly discovered services implicitly.**
   A service can be available without being allowed by any Profile.

8. **Make denials and routing explainable.**
   Self-hosted operators need to know whether a failure was authentication,
   profile denial, upstream unavailable, queue/resource pressure, timeout, or
   invalid request shape.

## Main Workflows

### Grant access to a service App

Use when the admin wants to connect LibreChat, Open WebUI, Continue, a bot, an
agent, or another shared service.

Visible fields:

- Service app name
- What it may use, as a small preset list:
  - OpenAI-compatible chat
  - Chat and embeddings
  - Image generation
  - Media workflows
  - Everything local and stable
- Create API key now
- Optional key label

Derived fields:

- service account owner
- App name
- Profile id
- Profile label
- protocol list
- intent list
- tags

Advanced fields:

- generated Profile/App/owner overrides
- raw policy JSON override

### Create reusable access policy

Use when the admin wants to define a policy that several Apps can share, such
as Family chat, Coding agents, Image generation, Automation, Experimental, or
Locked down.

Visible fields:

- Policy name
- Start from template
- Allowed capabilities: chat, embeddings, images, audio, video
- Model/workflow access:
  - Recommended safe defaults
  - All local and stable
  - Only explicitly tagged workflows

Advanced fields:

- generated policy id
- status detail level
- protocol selectors
- tags
- cloud fallback
- native passthrough
- raw JSON override

### Rotate or revoke keys

Use when a key leaks, a tool is retired, or access needs to be reduced.

The UI should show active keys by App, support instant revoke, preserve history,
and eventually support rotation windows.

### Debug access

The policy simulator is valuable, but it is a diagnostics tool. It should be
named as testing/debugging access, not part of the everyday creation path.

It should help answer:

- Which App/key made the request?
- Which Profile applied?
- What model/workflow/backend was requested?
- Was the request allowed, denied, queued, or failed upstream?
- What changed would allow it?

## Anti-Patterns

Avoid:

- one giant permission matrix as the first experience
- per-key policy sprawl
- OAuth-first UX for self-hosted tools that mostly need API keys
- SaaS billing nouns such as seats, plans, or workspace limits unless they are
  truly part of the product
- hiding routing decisions from operators
- deleting Apps as the main revocation path
- exposing raw ids and JSON in common flows
- making "Access" a durable product noun when it only means a UI preset

## SLVP Bar

The SLVP ideal is not fewer backend concepts. It is fewer concepts in the
operator's immediate path.

Ship:

- task-shaped grant flow for service Apps
- plain-language reusable Profile creation
- key creation/revocation with one-time key handling
- readable summaries of what each App/Profile can use
- advanced escape hatches for the operator who needs exact control

Do not ship:

- Profile creation that requires handtyping ids, protocols, intents, resource
  tiers, tags, and JSON for normal cases
- service access flows that show duplicated generated names as separate
  required fields
- UI-only abstractions that become a second policy model

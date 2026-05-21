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
- **App API key**: an `opk_...` runtime credential issued to one App. Apps can
  have multiple keys for rotation.
- **Admin automation key**: an `oak_...` control-plane credential for scripts
  that manage the gateway itself. It is not an OpenAI-compatible runtime key.
- **Service**: a backend or workflow host, such as llama.cpp, ComfyUI, STT,
  TTS, or a custom HTTP tool.
- **API surface**: a client-facing compatibility surface, such as
  OpenAI-compatible `/v1`, Anthropic Messages, Ollama-compatible `/api`, or
  gateway-native `/gateway/v1`. API surfaces are request shapes; they are not
  backend services and they are not Profiles.

The admin mental model should be:

```text
This App uses this App API key, and the key runs under this Profile.
```

Do not add another stored abstraction called Access, Plan, Template, or Grant
unless the implementation proves it needs one. Presets/templates are UI helpers
for generating existing objects.

The gateway has two sides that must both be visible to operators:

```text
compatible APIs -> gateway policy/routing -> hosted Services
```

For example, LibreChat talks to the OpenAI-compatible API; the gateway may route
that to llama.cpp, TabbyAPI, ComfyUI, Voxtral, Parakeet, or another configured
Service depending on the request and Profile.

Keep the plane split explicit:

```text
opk_... -> runtime App API key -> /v1, /gateway/v1, model/workflow execution
oak_... -> admin automation key -> /admin/api, /status, gateway management
```

`Full runtime access` and `Gateway admin automation` are different products.
The former can spend GPU/storage/time on runtime capabilities. The latter can
change gateway configuration, Apps, Profiles, and keys. Never blur them in the
same creation flow.

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
   The primary flow is "Create an API key for an app" or "Create a family
   chat policy", not "create service account, profile id, app name, protocol,
   intent, tags, JSON".

2. **Keep Apps and Profiles separate.**
   Many Apps can share one Profile, and an App can move Profiles without
   rotating all of its keys.

3. **Keep Keys separate from Apps.**
   A single App may need current, staging, and retiring keys. Revoking a key
   should preserve App history.

4. **Separate runtime access from gateway administration.**
   Runtime App keys are for apps and SDK tools. Admin automation keys are for
   trusted scripts that mutate gateway state. A broad runtime Profile must not
   imply control-plane permission.

5. **Make Profiles understandable as reusable policies.**
   A Profile should read like "Family chat: chat only, local stable routes,
   no cloud fallback" before it reads like JSON.

6. **Progressively disclose implementation details.**
   Profile ids, service account owner names, protocol selectors, exact
   intents, status visibility, native passthrough, and raw JSON belong in
   advanced sections.

7. **Treat media workflows as first-class risk/cost surfaces.**
   Image, audio, and video workflows have different latency, VRAM, storage,
   and safety implications from chat.

8. **Never grant newly discovered services implicitly.**
   A service can be available without being allowed by any Profile.

9. **Make denials and routing explainable.**
   Self-hosted operators need to know whether a failure was authentication,
   profile denial, upstream unavailable, queue/resource pressure, timeout, or
   invalid request shape.

## Holistic Admin IA

The admin UI should be organized around operator jobs, not storage tables:

- **APIs**: exposed compatible protocol surfaces, route bases, support tier,
  common clients, and which Profile protocol families gate them.
- **Services**: configured backends and workflow hosts, health/readiness,
  capabilities, queues, current loaded LLM, and runtime notes.
- **Apps & Keys**: create App API keys, see active Apps, rotate/revoke runtime
  keys.
- **Profiles**: reusable runtime access policies, created from named templates
  and summarized in plain language.
- **Identity**: runtime Profile mappings from OIDC groups/roles, exact emails,
  domains, gateway admin roles, or all signed-in users. These mappings choose
  defaults for self-service Apps; they do not grant admin access.
- **Workflows**: media workflow catalog, dependency/status checks, manifest
  controls.
- **Activity**: receipts, jobs, denials, and audit events.
- **Status**: backends, queues, GPU/VRAM, and maintenance modes.
- **Admin**: admin users, admin automation keys, OIDC/config diagnostics.

The current thin dashboard may render several of these on one page, but the
mental model should follow this IA.

APIs and Services are visibility/debug surfaces in the SLVP. They should not
become a second policy editor. Policy remains on Profiles; keys remain under
Apps; Services hide provider credentials and backend implementation detail.

## SLVP Policy Choices

The best first product surface is a short set of named choices:

- **Chat only**
  - Stable local chat.
  - No embeddings, media workflows, cloud fallback, native passthrough, or
    admin access.
  - Use for family chat, LibreChat, and basic Open WebUI.
- **Chat + embeddings**
  - Chat plus retrieval/search embeddings.
  - No media workflows, backend operation, or admin access.
  - Use for RAG apps, Continue, local search, and document ingestion.
- **Chat + images**
  - Chat and approved image generation/edit workflows.
  - No video, music/audio generation, cloud fallback, native passthrough, or
    admin access by default.
  - Use for friends/family, game-dev collaborators, and normal creative use.
- **Images + audio**
  - Approved image, edit, speech, transcription, music, and sound workflows.
  - No video by default. This avoids making a normal media Profile implicitly
    grant the most expensive queue.
- **Video producer**
  - Explicitly grants expensive video-generation capabilities.
  - Should be visibly different from chat and ordinary image/audio access.
- **Full runtime access**
  - Broad runtime access to local models and workflows.
  - May include stable, opt-in, experimental, batch, and expensive runtime
    capabilities, depending on the gateway policy.
  - Does not grant permission to manage Apps, Profiles, keys, users, or
    gateway settings.
- **Gateway admin automation**
  - Creates an `oak_...` admin automation key, not an `opk_...` App API key.
  - Can manage gateway control-plane resources according to its role.
  - Does not imply model/media runtime access.
- **Advanced native passthrough**
  - A dangerous modifier or advanced template, not a default option.
  - Must scope explicit backends and operations and keep audit on by default.

Do not use vague labels like "All local and stable" in the SLVP UI. They
encode implementation metadata, not an operator decision.

## Main Workflows

### Create an API key for an App

Use when the admin wants to connect LibreChat, Open WebUI, Continue, a bot, an
agent, or another shared service.

Visible fields:

- App or service name
- ownership: shared App or personal/user-owned App
- Profile selection

Creating an App, creating a Profile, and minting a key must remain separate
flows. Operators need to reuse Profiles across Apps, move an App between
Profiles without rotating keys, and rotate/revoke individual keys without
deleting the App.

Derived fields:

- service account owner
- App name
- Profile id
- Profile label
- protocol list
- intent list
- tags

Do not put generated ids, owner overrides, raw policy JSON, or service-account
language in this first flow. Those controls belong in separate advanced
maintenance tools, not the primary path.

### Create reusable Profile

Use when the admin wants to define a policy that several Apps can share, such
as Chat only, Chat + embeddings, Chat + images, Images + audio, Video producer,
Full runtime access, or a custom advanced policy.

Visible fields:

- Policy name
- Start from Profile template
- Target policy:
  - allowed catalog vs available now
  - may activate vs existing only

Advanced fields:

- generated policy id
- status detail level
- protocol selectors
- optional model allow-list
- optional workflow allow-list
- tags
- cloud fallback
- native passthrough
- raw JSON override

### Model visibility and current-loaded mode

Self-hosted LLM operators are often VRAM-bound. A Profile therefore needs two
separate controls:

- **static model allow-list**: exact model/workflow ids that a Profile may use.
- **current loaded LLM mode**: dynamic behavior that only advertises and accepts
  ids resolving to the active loaded LLM.

This is not a new durable noun. It is a Profile field because it changes what
an App can see on `/v1/models` and whether chat/completions requests can trigger
model/backend swaps. The runtime must fail clearly when a current-loaded Profile
requests a non-loaded model instead of silently swapping or serving a different
model.

The operator-facing explanation should be:

```text
/v1/models is filtered by the caller's Profile.
Current-loaded Profiles only see the active loaded LLM ids.
```

### Map OIDC identities to runtime Profiles

Use when the admin wants signed-in users to create personal Apps and keys
without hand-picking a Profile every time.

Visible fields:

- identity source type: OIDC group, OIDC role, exact email, email domain,
  gateway admin role, or all signed-in users
- identity source value
- default runtime Profile

Rules:

- These mappings are data-plane defaults only. They must not grant admin
  control-plane rights.
- OIDC group/role names should accept common aliases such as `/trusted-users`,
  `trusted-users`, `trusted_users`, and `trusted users`.
- More specific mappings should win over broad mappings: email, domain, OIDC
  group, OIDC role, gateway role, then all signed-in users.
- Existing Apps keep their assigned Profile unless the admin changes the App or
  the App inherits a default dynamically.

### Create admin automation key

Use when the admin wants a script to manage Apps, Profiles, keys, status, or
audit data.

Visible fields:

- Automation name
- Admin role: admin or owner

Rules:

- The generated key is an `oak_...` key and should be shown once.
- It is for `/admin/api` control-plane calls, not for OpenAI-compatible
  runtime calls.
- The UI must warn: "Do not paste this into chat apps, SDK tools, browser
  code, prompts, logs, or issue trackers."
- Creating owner automation keys should require an existing owner actor.

### Rotate or revoke keys

Use when a key leaks, a tool is retired, or access needs to be reduced.

The UI should show active keys by App, support instant revoke, preserve history,
and eventually support rotation windows.

### Policy Test

The policy simulator is valuable, but it is a diagnostics tool. It should be
named as policy testing, not part of the everyday creation path.

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
- implying that Full runtime access can manage the gateway
- putting admin automation keys into runtime App-key flows
- creating an `oak_...` key without strong one-time-secret and danger copy

## SLVP Bar

The SLVP ideal is not fewer backend concepts. It is fewer concepts in the
operator's immediate path.

Ship:

- task-shaped App API key creation
- separate admin automation key creation
- plain-language reusable Profile creation
- runtime Profile mapping for OIDC groups/roles, emails, domains, and defaults
- key creation/revocation with one-time key handling
- readable summaries of what each App/Profile can use
- advanced escape hatches for the operator who needs exact control

Do not ship:

- Profile creation that requires handtyping ids, protocols, intents, resource
  tiers, tags, and JSON for normal cases
- service access flows that show duplicated generated names as separate
  required fields
- UI-only abstractions that become a second policy model
- vague labels like "All local and stable"
- any UI that makes `opk_...` and `oak_...` keys feel interchangeable

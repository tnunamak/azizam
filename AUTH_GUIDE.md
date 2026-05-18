# Vivid Fish AI Gateway — Auth Guide

This document explains how Vivid Fish AI Gateway (the project formerly known as
`openai-proxy`) handles authentication for the apps that call it.

The gateway uses **App API keys** that are separate from downstream provider
keys. Each app — LibreChat, Open WebUI, Continue, Cline, a personal chat
client, an agent — authenticates to the gateway with its own `opk_...` key.
The gateway then maps that key to the provider credentials needed for Tabby,
Kobold, TTS, STT, ComfyUI, or any other configured backend.

The legacy term "client" is preserved only inside protocol/SDK compatibility
contexts (the OpenAI Python "client", "client receipts" as an audience tier).
The product entity is an **App**.

## Access model at a glance

```text
User -> owns App -> has Profile -> has Keys -> produces Activity
```

- **User** — a human, typically authenticated via OIDC.
- **App** — an access boundary for a tool, device, agent, or family member.
- **Profile** — the policy bundle attached to an App.
- **Key** — a rotatable bearer secret, usually an App API key (`opk_...`) or
  admin automation key (`oak_...`).
- **Activity** — usage, audit events, receipts, jobs, and errors rendered as
  facts.

Service accounts, credentials, admin roles, audit events, capabilities,
providers, workflows, jobs, receipts, and usage records are still precise
internal facts. Most operators should meet them through App/Profile/Key screens
or Activity views, not as a pile of top-level concepts.

The invariant: every executed request resolves an App + Profile + policy
decision before it runs, and produces exactly one body-free **Receipt**.

## Quick Start For Apps

Run this from the gateway directory:

```bash
cd /home/tnunamak/applications/openai-proxy
./venv/bin/python proxy.py generate-key <app-name>
```

Example:

```bash
./venv/bin/python proxy.py generate-key simon
```

The command prints one App API key:

```text
opk_...
```

Use that value as the OpenAI API key inside the app. Store it in the app's
secret store or environment. Do not put the printed key in source files, logs,
or prompts.

Typical app settings:

```text
Base URL: http://127.0.0.1:5000/v1
API key:  opk_...
```

External apps can use the public canonical host:

```text
Base URL: https://ai.vivid.fish/v1
API key:  opk_...
```

The previous host `https://openai.vivid.fish/v1` remains a compatibility alias
— every route resolves identically — so apps that are already configured
against it do not need to change.

## What The Command Does

`generate-key` creates or rotates an App entry in `proxy_api_keys.yml`.

The file stores only a SHA-256 hash of the App API key:

```yaml
keys:
  - name: simon
    key_hash: sha256:...
    downstream:
      tabby: {}
```

The raw `opk_...` key is shown only once when generated. If it is lost,
generate a new key for the same App name and update that app's configuration.

If the same `<app-name>` already exists, the command rotates that App's API
key while preserving its downstream mapping, Profile block, and audit trail.

`generate-key` is the legacy-compatible App-key path. It remains useful for a
single-user install and for apps already wired to `proxy_api_keys.yml`, but it
is not the control plane. New multi-user/admin workflows should use
`/admin`, `/admin/api/*`, and the durable admin database.

## Admin Control Plane

The control plane is separate from the runtime `/v1` plane.

- App keys (`opk_...`) call `/v1`, `/api`, and `/gateway/v1`.
- Admin keys (`oak_...`) and signed OIDC dashboard sessions call `/admin` and
  `/admin/api/*`.
- An App key cannot administer the gateway, and an admin key should not be put
  into OpenAI-compatible apps.

Bootstrap the first service-account admin key from the gateway directory:

```bash
./venv/bin/python proxy.py generate-admin-key owner-cli
```

The command initializes the durable admin store if needed and prints one
`oak_...` key. Use it only for admin-plane requests:

```bash
curl -s https://ai.vivid.fish/admin/api/overview \
  -H "Authorization: Bearer oak_..."
```

OIDC browser login is configured under `admin.oidc` in `proxy_config.yml`.
Secrets should come from environment variables:

```yaml
admin:
  db_path: data/admin.db
  oidc:
    enabled: true
    issuer: https://idp.example.com
    client_id: vivid-fish-ai-gateway
    client_secret_env: AI_GATEWAY_OIDC_CLIENT_SECRET
    redirect_uri: https://ai.vivid.fish/admin/oidc/callback
    signing_key_env: AI_GATEWAY_ADMIN_SESSION_KEY
    allowed_email_domains: ["vivid.fish"]
    admin_emails: ["owner@vivid.fish"]
```

The first allowed OIDC login becomes `owner` if no owner exists yet. Later
allowed logins get the configured role or no admin role until an owner grants
one.

## Give A Family Member Access

Concrete walkthrough of the canonical "share my gateway with another human"
flow:

1. **OIDC User.** The family member signs in once so the admin store has
   their **User** row. They do not need a global admin role.
2. **Personal App.** An owner/admin registers one App for them —
   `aunt-mary-chat` — owned by their User. One human can own several Apps;
   one App is the access boundary that ordinary keys use at runtime.
3. **Profile.** Attach a Profile to the App that grants only what they need.
   For a typical chat-only setup:
   ```yaml
   profile:
     id: aunt-mary-chat
     protocols: ["openai.chat", "openai.audio"]
     fallback:
       cloud_allowed: false
     native_passthrough:
       enabled: false
     status_visibility:
       level: basic
   ```
4. **App API key.** Mint the bearer token through the admin plane:
   ```bash
   curl -s https://ai.vivid.fish/admin/api/apps \
     -H "Authorization: Bearer oak_..." \
     -H "Content-Type: application/json" \
     -d '{"name":"aunt-mary-chat","kind":"personal","owner_user_id":"user_...","profile_id":"prof_..."}'

   curl -s https://ai.vivid.fish/admin/api/apps/app_.../keys \
     -H "Authorization: Bearer oak_..." \
     -H "Content-Type: application/json" \
     -d '{"label":"Aunt Mary laptop"}'
   ```
   They can visit `https://ai.vivid.fish/me`, sign in with OIDC or paste the
   printed `opk_...` value locally in the browser, and verify the base URL,
   SDK snippets, effective capabilities, and recent media jobs visible to that
   key. Then they paste the same key into their chat app of choice (LibreChat,
   Open WebUI, an iOS shortcut, etc.) with base URL `https://ai.vivid.fish/v1`.

Rotation stays per-App: revoke the old Credential and mint a new key for the
same App while leaving the User, the Profile, and the audit history intact.
Dashboard/default key minting inherits the App's current Profile at runtime;
use a per-key Profile override only when you intentionally want a narrower or
temporary exception.

Signed-in users may mint or revoke runtime keys for Apps they already own from
`/me` once an operator has attached a Profile to the App. If they have no App,
they can request access from `/me`, which creates a pending Personal App for an
operator to review and profile. That does not let them change ownership, assign
Profiles, enable cloud fallback, or grant themselves new capability classes;
those remain admin-plane actions.

## Request Auth Rules

All gateway runtime traffic requires a valid App API key. `/health`, `/me`,
`/me/api/session`, and the `/.well-known/*` discovery surfaces are public.
`/me` is a shell only: it does not show private capability, model, or job data
until the browser sends either the signed OIDC session cookie or the user's
`opk_...` key to the normal authenticated runtime APIs. State-changing
`/me/api/*` requests require a valid signed OIDC session and the
`x-ai-gateway-user-action: 1` plus `x-ai-gateway-csrf` headers generated by
the same-origin `/me` page. Admin browser mutations use the same CSRF pattern;
bearer-token automation still authenticates with `Authorization: Bearer oak_...`.

Accepted auth headers:

```http
Authorization: Bearer opk_...
```

or:

```http
x-api-key: opk_...
```

or:

```http
api-key: opk_...
```

Do not send Tabby admin keys or downstream provider keys as App auth. The
gateway strips inbound auth headers before forwarding requests and injects the
appropriate downstream credentials itself.

## Smoke Test

After generating a key, test that the gateway accepts it. `OPENAI_PROXY_API_KEY`
is the historical env-var name and remains the canonical one for now — new
docs may also reference `AI_GATEWAY_API_KEY` as an alias:

```bash
export OPENAI_PROXY_API_KEY='opk_...'

curl -s http://127.0.0.1:5000/health

curl -s http://127.0.0.1:5000/v1/voices \
  -H "Authorization: Bearer $OPENAI_PROXY_API_KEY"
```

Expected behavior:

- `/health` returns `status: healthy` without auth.
- `/v1/voices` returns voices with a valid key.
- `/v1/voices` returns `401` without a key.

The full local test script also expects the key:

```bash
OPENAI_PROXY_API_KEY='opk_...' ./test_e2e.sh
```

## Downstream Credential Mapping

By default, App API keys map to the global downstream provider credentials
configured for the gateway:

- Tabby API key: `TABBY_API_KEY` or Tabby's `api_tokens.yml`
- Tabby admin key: `TABBY_ADMIN_KEY` or Tabby's `api_tokens.yml`
- TTS, STT, ComfyUI, Kobold: no extra auth unless configured per key

Per-App overrides can be added in `proxy_api_keys.yml` when an App needs
different downstream credentials:

```yaml
keys:
  - name: simon
    key_hash: sha256:...
    downstream:
      tabby:
        api_key_env: SIMON_TABBY_API_KEY
        admin_key_env: SIMON_TABBY_ADMIN_KEY
      tts:
        headers:
          Authorization:
            env: SIMON_TTS_API_KEY
            prefix: Bearer
      comfyui:
        headers:
          X-API-Key:
            env: SIMON_COMFY_API_KEY
```

Supported downstream keys:

- `tabby.api_key` or `tabby.api_key_env`
- `tabby.admin_key` or `tabby.admin_key_env`
- `<service>.headers` for arbitrary backend headers

Header values can be literal strings:

```yaml
headers:
  X-API-Key: local-dev-secret
```

or environment-backed values:

```yaml
headers:
  Authorization:
    env: SIMON_TTS_API_KEY
    prefix: Bearer
```

The gateway reloads `proxy_api_keys.yml` when it changes, so adding, rotating,
or deleting an App API key does not require a service restart. Profile blocks
(see below) reload alongside the entries they live on.

## Policy Profiles

A Profile is the authorization and audience boundary attached to an App. In
the durable admin store, Apps point at stored Profiles and App keys inherit
that Profile at runtime. Legacy `proxy_api_keys.yml` entries may also carry an
optional `profile:` block loaded by `gateway.profiles.load_profile_from_entry`.
When mounted in the request path, the Profile decides:

- which protocols the App may use (e.g. `openai.chat`, `openai.images`,
  `openai.audio`, `openai.responses`, `anthropic.messages`, `ollama.chat`,
  `ollama.generate`)
- which workflows, models, and providers the App may target
- whether cloud fallback is allowed
- whether — and where — native passthrough is permitted
- how much status, discovery, and receipt detail the App sees

Missing `profile:` blocks stay backward-compatible: the loader falls back to
`DEFAULT_PROFILE` (local-only, basic visibility), so existing Apps keep
working unchanged.

Allow-lists are conservative by construction:

- omit a list → "no opinion" for that dimension (no restriction)
- set a list to `[]` → "deny all" for that dimension

As the gateway grows, avoid managing Profiles as long lists of model and
workflow IDs. Prefer category selectors backed by capability metadata:

```yaml
allow:
  intents: ["chat.completions", "speech.synthesis"]
  trust_tiers: ["stable"]
  resource_tiers: ["interactive"]
  exposure: ["default"]
fallback:
  cloud_allowed: false
native_passthrough:
  enabled: false
```

Then a new stable interactive chat model becomes available to matching Apps
without editing every Profile. Experimental, expensive, native, or admin-power
workflows remain opt-in through their own metadata.

`status_visibility.level` maps an App to a discovery/receipt audience tier.
"Client" here is the protocol/audience tier name (kept for compatibility),
not a product noun — the requester is still an App:

| Level      | Audience       | What the App sees                              |
|------------|----------------|------------------------------------------------|
| `none`     | client         | No receipts; minimal discovery                 |
| `basic`    | client         | Client-tier receipts (request/job IDs, status) |
| `standard` | admin          | Routing facts, fallback reasons                |
| `full`     | internal       | Backend, policy source, manifest hash, diags   |

Example profile block on a key entry:

```yaml
keys:
  - name: image-agent
    key_hash: sha256:...
    profile:
      id: image-agent
      protocols: ["openai.images"]
      workflows: ["image.edit.controlnet.example"]
      models: ["sdxl-base"]
      backends: ["comfyui.local"]
      fallback:
        cloud_allowed: false
      status_visibility:
        level: basic
```

### Native passthrough

`native_passthrough` is opt-in and scoped per profile. It is not a blanket
ComfyUI DSL — it is a privileged lane for specific operations on specific
backends, and every use is recorded in receipts.

```yaml
profile:
  id: comfy-tinkerer
  native_passthrough:
    enabled: true
    allowed_backends: ["comfyui.local"]
    allowed_operations: ["queue_prompt", "history"]
    audit: true
```

### Gateway-owned controls live under extensions

Backend-native power (denoise, controlnet strength, model-specific knobs)
should not be added as global OpenAI fields. It belongs under namespaced
gateway extensions:

```json
{
  "extensions": {
    "com.vividfish.workflow": {
      "denoise": 0.35,
      "controlnet_strength": 1.1
    }
  }
}
```

Unknown extension inputs are rejected with a reason; known-but-unsupported
inputs are recorded as ignored. The gateway never silently drops inputs.

### Templates for current Apps

Safe starter Profiles for the agent/UI/harness Apps this gateway serves today.
Drop these under the matching key entry — they use non-secret placeholders
only. Tighten the allow-lists when an App has a narrow role; leave them
omitted to keep "no opinion" semantics until you need to restrict.

```yaml
# Agent/coding apps (Claude Code-style, Continue, Cline):
- name: simon
  key_hash: sha256:...
  profile:
    id: simon
    # Omit protocols/models to stay permissive; add lists when narrowing.
    fallback:
      cloud_allowed: false
    status_visibility:
      level: standard

# UI apps (LibreChat, Open WebUI):
- name: librechat
  key_hash: sha256:...
  profile:
    id: librechat
    protocols: ["openai.chat", "openai.responses", "openai.audio"]
    fallback:
      cloud_allowed: false
    status_visibility:
      level: basic

- name: open-webui
  key_hash: sha256:...
  profile:
    id: open-webui
    protocols: ["openai.chat", "openai.responses", "openai.audio", "openai.images"]
    fallback:
      cloud_allowed: false
    status_visibility:
      level: basic

# Compatibility harness / test App:
- name: claude-test
  key_hash: sha256:...
  profile:
    id: claude-test
    protocols: ["anthropic.messages"]
    fallback:
      cloud_allowed: false
    status_visibility:
      level: full

# Helper agents (daisy, cap): leave profile omitted to inherit
# DEFAULT_PROFILE (local-only, basic visibility) until a narrower
# boundary is needed.
```

## Rotation And Revocation

Rotate one App's API key:

```bash
./venv/bin/python proxy.py generate-key simon
```

Then update that App to use the newly printed `opk_...` value.

Revoke an App's API key:

1. Remove that App's entry from `proxy_api_keys.yml`.
2. Save the file.
3. The gateway will reject that key on subsequent `/v1` requests.

## Common Failures

`401 Missing API key.`

The request reached an authenticated endpoint without
`Authorization: Bearer opk_...` or another accepted auth header.

`401 Invalid API key.`

The presented App API key does not match any hash in `proxy_api_keys.yml`.

`503 Proxy auth is enabled, but no proxy API keys are configured.`

Auth is fail-closed and no usable App API keys are loaded. Generate at least
one.

`502` or `503` backend errors.

The App API key was accepted, but the selected downstream provider was
unavailable or rejected its own backend credentials.

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

## First-Run Setup

For a fresh deployment, the normal entry point is the web UI, not the CLI.
The short version:

1. Start the gateway. It defaults to `data/admin.db` and creates a persistent
   browser-session signing key if none is supplied.
2. Open `/setup`.
3. Claim or create the first owner — OIDC sign-in when configured, or a local
   owner account created in the browser when OIDC is not configured yet.
4. The app seeds default Profiles and identity/Profile mappings, creates the
   signed-in user's first App, and mints one `opk_...` runtime key shown once.
5. Manage providers, users, Apps, Profiles, keys, admission limits, and
   service availability from `/admin`. OIDC provider settings are dashboard
   owned after setup. Provisioning / as-code config export is deferred; default
   Profiles live in `admin.db`, not YAML.

The full operator-facing walkthrough is in
[`docs/FIRST_RUN_SETUP.md`](docs/FIRST_RUN_SETUP.md). What follows in this
guide is the runtime/auth model and the CLI recovery paths.

## Quick Start For Apps (Dashboard)

Once the gateway is running and you have signed in, the normal way to mint
an App API key is from setup, the dashboard, or the account portal:

- During first run, a signed-in owner/admin opens `/setup` and mints the first
  `opk_...` key for their Personal App. This is the quickest smoke-test path.
- An operator opens `/admin`, creates an App, attaches a Profile, and
  generates an `opk_...` key for it.
- A signed-in user opens `/` (the account portal), creates a Personal App,
  and mints their own `opk_...` key bound to that App.

Dashboard-minted keys live in `admin.db`. The raw `opk_...` value is shown
exactly once; the hash is what persists. Use it as the OpenAI API key
inside the consuming app. Store it in the app's secret store or
environment. Do not put the printed key in source files, logs, or prompts.

## Quick Start For Apps

Runtime App keys are database-backed. Use `/setup`, the account portal at `/`,
or `/admin` to create an App and mint an `opk_...` key. The key value is shown
once:

```text
opk_...
```

Use that value as the OpenAI API key inside the consuming app. Store it in the
app's secret store or environment. Do not put the printed key in source files,
logs, or prompts.

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

## What Key Creation Does

Creating a key inserts one active Credential row for an App in `admin.db`. The
database stores the App, its default Profile, the key hash, display label,
expiry/revocation metadata, and audit timestamps. The raw `opk_...` key is
shown only once. If it is lost, create a new key for the same App and update
the consuming app's configuration.

## Admin Control Plane

The control plane is separate from the runtime `/v1` plane.

- App keys (`opk_...`) call `/v1`, `/api`, and `/gateway/v1`.
- Admin keys (`oak_...`) and signed OIDC dashboard sessions call `/admin`,
  `/admin/api/*`, `/status`, and `/v1/status`.
- An App key cannot administer the gateway, and an admin key should not be put
  into OpenAI-compatible apps.

The normal way to gain admin access on a fresh deployment is `/setup`: OIDC
when available, or a local owner account when the operator has not configured
an IdP. From there, everything is dashboard-driven.

### Recovery / headless admin key

If OIDC is unreachable or misconfigured, or you are provisioning the
gateway from a config-management tool, you can mint an admin key from the
CLI. This is the recovery/headless path that `/setup/claim` can exchange for a
browser admin session:

```bash
./venv/bin/python proxy.py generate-admin-key owner-cli
```

The command initializes the durable admin store if needed and prints one
`oak_...` key. Paste it into `/setup` when claiming a browser session, or use
it directly for admin-plane requests:

```bash
curl -s https://ai.vivid.fish/admin/api/overview \
  -H "Authorization: Bearer oak_..."
```

### OIDC configuration

OIDC browser login is normally configured from `/admin` after the local owner
has signed in. `admin.oidc` in `proxy_config.yml` remains a bootstrap fallback
for operators who need IdP login before the first browser claim. `/login` is
the raw OIDC redirect starter, while `/me/login` and `/admin/login` are
compatibility redirects into that route. After login, normal users land on
`/`, while admins can follow the Admin link from the account portal or go
directly to `/admin`. `/me` is a legacy browser alias that redirects to `/`,
never an API-key error page.

Dashboard-saved OIDC client secrets are stored in `admin.db`, redacted in UI
and API responses, and preserved when the secret field is left blank. If your
deployment requires external secret storage, keep the secret in env and use
the YAML bootstrap fallback instead:

```yaml
admin:
  db_path: data/admin.db
  user_defaults:
    self_service_apps: true
    auto_create_personal_app: true
    default_profile_id: prof_family_default
    role_profile_ids:
      none: prof_family_default
      admin: prof_operator_default
    domain_profile_ids:
      vivid.fish: prof_operator_default
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

`admin.user_defaults` and the legacy user-access env flags are bootstrap seeds
for self-service defaults and identity/Profile mappings. On startup, those
defaults are imported into `admin.db`; after that, `/admin` identity mappings
and operator settings are the runtime policy boundary. Omit
`default_profile_id` to use the gateway's built-in `default-local` profile.

## Give A Family Member Access

Concrete walkthrough of the canonical "share my gateway with another human"
flow:

1. **OIDC User.** The family member signs in once so the admin store has
   their **User** row. They do not need a global admin role.
2. **Personal App.** The account portal lets them create one or more
   self-service Personal Apps. One human can own several Apps; one App is the
   access boundary for one client, tool, workflow, or script.
3. **Inherited Profile.** By default, the Personal App inherits the user's
   effective Profile. Admins control that default with `/admin` identity
   mappings, which may be seeded once from `admin.user_defaults`, and can still
   attach an explicit App/Profile override when needed.
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
4. **App API key.** They mint their own bearer token from the account portal.
   They can visit `https://ai.vivid.fish/`, sign in with OIDC, create their
   Personal App if one does not already exist, mint an `opk_...` key, and verify
   the base URL, SDK snippets, and effective capabilities visible to that key.
   Then they paste the same key into their chat app of choice
   (LibreChat, Open WebUI, an iOS shortcut, etc.) with base URL
   `https://ai.vivid.fish/v1`.

Rotation stays per-App: delete the old Credential and create a new key for the
same App while leaving the User, the Profile, and the audit history intact.
Dashboard/default key minting inherits the App's current Profile at runtime;
use a per-key Profile override only when you intentionally want a narrower or
temporary exception.

Signed-in users may create Personal Apps, create API keys for Apps they own,
and delete those keys from the account portal. Those Apps inherit the user's
default Profile unless an admin attaches an explicit App Profile. Self-service
does not let users change ownership, assign Profiles, enable cloud fallback, or
grant themselves new capability classes; those remain admin-plane actions.

## Request Auth Rules

All gateway runtime traffic requires a valid App API key. `/`, `/health`,
`/me`, `/me/api/session`, and the `/.well-known/*` discovery surfaces are public.
`/me` redirects to `/`. The account portal is a shell only: it does not show
private capability, model, or job data until the browser sends either the
signed OIDC session cookie or the user's `opk_...` key to the normal
authenticated runtime APIs. State-changing
`/me/api/*` requests require a valid signed OIDC session and the
`x-ai-gateway-user-action: 1` plus `x-ai-gateway-csrf` headers generated by the
same-origin account page. This includes `POST /me/api/apps`,
`POST /me/api/apps/{app_id}/keys`, `DELETE /me/api/apps/{app_id}`, and
`DELETE /me/api/credentials/{credential_id}`.
`GET /me/api/credentials/{credential_id}/access` uses the signed OIDC session
to inspect an owned key's effective capabilities without revealing the secret.
Admin browser mutations use the same CSRF pattern;
bearer-token automation still authenticates with `Authorization: Bearer oak_...`.
The operator status UI (`/status`) and status JSON (`/v1/status`) require an
admin OIDC session or `oak_...` key, including from loopback. Use `/health` for
public liveness probes.

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

The current DB runtime path does not expose per-App downstream credential
overrides. Use Profiles to decide which protocols, models, workflows, and
provider-native routes an App may call; use the gateway's global provider
configuration to decide which local services and backend credentials those
allowed calls use.

## Policy Profiles

A Profile is the authorization and audience boundary attached to an App. Apps
point at stored Profiles in the durable admin store, and App keys inherit that
Profile at runtime. When mounted in the request path, the Profile decides:

- which protocols the App may use (e.g. `openai.chat`, `openai.images`,
  `openai.audio`, `openai.responses`, `anthropic.messages`, `ollama.chat`,
  `ollama.generate`)
- which workflows, models, and providers the App may target
- whether cloud fallback is allowed
- whether — and where — native passthrough is permitted
- how much status, discovery, and receipt detail the App sees

Missing Profile bindings fall back to `DEFAULT_PROFILE` (local-only, basic
visibility), so simple Apps keep working unchanged.

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

Example stored Profile body:

```yaml
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
id: comfy-tinkerer
native_passthrough:
  enabled: true
  allowed_backends: ["comfyui.local"]
  allowed_operations: ["queue_prompt", "history"]
  audit: true
```

Provider-native HTTP passthrough is deliberately namespaced below
`/gateway/v1/providers/{provider}/{path...}`. Do not expose backend-native
routes at `/v1`, `/api`, `/queue`, or other root prefixes. The protocol
frontends keep those roots: OpenAI-compatible uses `/openai/v1` with `/v1` as a
legacy convenience alias, Anthropic Messages uses `/anthropic/v1`, and
Ollama-compatible uses `/ollama/api` with `/api` as a convenience alias. When a
provider's native path collides with a frontend path, the frontend wins and the
native call must go through `/gateway/v1/providers/{provider}/{path...}` with a
profile that explicitly enables `native_passthrough`.

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
Create these in the dashboard as stored Profiles, then attach them to Apps.
They use non-secret placeholders only. Tighten the allow-lists when an App has
a narrow role; leave fields unset to keep "no opinion" semantics until you need
to restrict.

```yaml
id: simon
# Omit protocols/models to stay permissive; add lists when narrowing.
fallback:
  cloud_allowed: false
status_visibility:
  level: standard

---
id: librechat
protocols: ["openai.chat", "openai.responses", "openai.audio"]
fallback:
  cloud_allowed: false
status_visibility:
  level: basic

---
id: open-webui
protocols: ["openai.chat", "openai.responses", "openai.audio", "openai.images"]
fallback:
  cloud_allowed: false
status_visibility:
  level: basic

---
id: claude-test
protocols: ["anthropic.messages"]
fallback:
  cloud_allowed: false
status_visibility:
  level: full
```

## Rotation And Deletion

Rotate one App's API key:

1. Open `/` for keys you own, or `/admin` as an operator.
2. Delete or revoke the old Credential.
3. Create a new key on the same App.
4. Update that App to use the newly printed `opk_...` value.

Delete an App's API key from `/` or `/admin`; it is revoked immediately in
`admin.db` and rejected on subsequent runtime requests.

## Common Failures

`401 Missing API key.`

The request reached an authenticated endpoint without
`Authorization: Bearer opk_...` or another accepted auth header.

`401 Invalid API key.`

The presented App API key does not match any active App credential in
`admin.db`, or the owning App is disabled.

`401/403 after deleting a key.`

Deleted credentials are revoked immediately. Create a new key on the same App
and update the consuming app.

`502` or `503` backend errors.

The App API key was accepted, but the selected downstream provider was
unavailable or rejected its own backend credentials.

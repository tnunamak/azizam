# Azizam — Auth Guide

This document explains how Azizam handles authentication for the apps that call it.

The gateway uses **App API keys** that are separate from downstream provider
keys. Each app — LibreChat, Open WebUI, Continue, Cline, a personal chat
client, an agent — authenticates to the gateway with its own `opk_...` key.
The gateway then maps that key to the provider credentials needed for
Kobold, TTS, STT, ComfyUI, or any other configured backend.

The legacy term "client" is preserved only inside protocol/SDK compatibility
contexts (the OpenAI Python "client", "client receipts" as an audience tier).
The product entity is an **App**.

## Access model at a glance

```text
Person -> answers for App -> App has Keys -> Keys produce Activity
Person and App each hold an Access plan
```

- **Person** — a human, typically authenticated via OIDC. A person holds one
  access plan, or none. A person with no plan cannot generate anything.
- **App** — an access boundary for a tool, device, agent, or family member.
  Every App has an **accountable person** and exactly one assignment: an
  access plan, or **Same as its person**. An App with no assignment is refused.
- **Access plan** — a named, reusable list of what may run: the capabilities
  with their models and workflows, the paid providers, direct engine access,
  spending caps, whether models may be loaded, and how much receipt detail
  and file metadata a caller gets. Nothing is allowed unless a plan lists it.
- **Key** — a rotatable bearer secret, usually an App API key (`opk_...`) or
  admin automation key (`oak_...`). A key acts for its App and has no plan of
  its own.
- **Activity** — usage, audit events, receipts, jobs, and errors rendered as
  facts.

A request runs only if **both** the accountable person's plan and the App's
plan allow it. The gateway has no built-in default plan: there is no keyless
LAN access, no path token, no auth-off switch, and no plan that applies
because nothing else did. Roles (Owner, Admin, Member) control the management
screens, never generation.

The invariant: every executed request resolves an App, its accountable
person, and both plans before it runs, and produces exactly one body-free
**Receipt**.

## First-Run Setup

For a fresh deployment, the normal entry point is the web UI, not the CLI.
The short version:

1. Start the gateway. It defaults to `data/admin.db` and creates a persistent
   browser-session signing key if none is supplied.
2. Open `/setup`.
3. Claim or create the first owner — OIDC sign-in when configured, or a local
   owner account created in the browser when OIDC is not configured yet.
4. The owner gets one starting plan, **Local only** (every local model and
   workflow, no paid providers, no direct engine access), creates their first
   App ("Same as its person"), and mints one `opk_...` runtime key shown once.
   The plan is an ordinary plan the owner can edit or delete. Nobody else gets
   it: people who sign in later have no plan until the owner chooses one.
5. Manage providers, people, Apps, access plans, keys, admission limits, and
   service availability from the web app (`/settings/`, `/people/`,
   `/apps/`, `/engines/`, `/providers/`). OIDC provider settings are dashboard
   owned after setup. The narrow, authenticated service-registry export at
   `GET /admin/api/config/export` is shipped; only full deployment-bundle
   restore and authoritative file-wins reconciliation remain deferred.

The full operator-facing walkthrough is in
[`docs/FIRST_RUN_SETUP.md`](docs/FIRST_RUN_SETUP.md). What follows in this
guide is the runtime/auth model and the CLI recovery paths.

## Quick Start For Apps (Dashboard)

Once the gateway is running and you have signed in, the normal way to mint
an App API key is from setup, the dashboard, or the account portal:

- During first run, a signed-in owner/admin opens `/setup/` and mints the first
  `opk_...` key for their Personal App. This is the quickest smoke-test path.
- An operator opens `/apps/`, creates an App, chooses its access plan (or
  "Same as {person}'s access plan"), and generates an `opk_...` key for it.
- A signed-in member opens `/` (the Apps page), creates a Personal App
  ("Same as my access plan"), and mints their own `opk_...` key bound to it.

Dashboard-minted keys live in `admin.db`. The raw `opk_...` value is shown
exactly once; the hash is what persists. Use it as the OpenAI API key
inside the consuming app. Store it in the app's secret store or
environment. Do not put the printed key in source files, logs, or prompts.

## Quick Start For Apps

Runtime App keys are database-backed. Use `/setup/`, or the Apps page at `/apps/`,
to create an App and mint an `opk_...` key. The key value is shown
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
Base URL: https://ai.example.com/v1
API key:  opk_...
```

If you change public hostnames, keep old hostnames as compatibility aliases
until every configured app has moved.

## What Key Creation Does

Creating a key inserts one active Credential row for an App in `admin.db`. The
database stores the App, the key hash, display label,
expiry/revocation metadata, and audit timestamps. The raw `opk_...` key is
shown only once. If it is lost, create a new key for the same App and update
the consuming app's configuration.

## Admin Control Plane

The control plane is separate from the runtime `/v1` plane.

- App keys (`opk_...`) call `/v1`, `/api`, and `/gateway/v1`.
- Admin keys (`oak_...`) and signed OIDC dashboard sessions call
  `/admin/api/*`, `/status/raw`, and `/v1/status`.
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
curl -s https://ai.example.com/admin/api/overview \
  -H "Authorization: Bearer oak_..."
```

### OIDC configuration

OIDC browser login is normally configured from the Settings page (`/settings/`) after the local owner
has signed in. `admin.oidc` in `proxy_config.yml` remains a bootstrap fallback
for operators who need IdP login before the first browser claim. `/login?return_to=...`
is the raw OIDC redirect starter (the default return path is `/`). The old
`/me/login` and `/admin/login` routes no longer exist. After login, every user
lands on the requested page, or on Home (`/`) by default; admins reach the
admin pages from the same app, for example `/people/` and `/settings/`. The
OIDC redirect URI stays `/admin/oidc/callback`. To sign out, use the app's
sign-out link (`/auth/logout-confirm`) or `POST /logout`, which answers
`next: "/sign-in/"`.

Dashboard-saved OIDC client secrets are stored in `admin.db`, redacted in UI
and API responses, and preserved when the secret field is left blank. If your
deployment requires external secret storage, keep the secret in env and use
the YAML bootstrap fallback instead:

```yaml
admin:
  db_path: data/admin.db
  oidc:
    enabled: true
    issuer: https://idp.example.com
    client_id: azizam
    client_secret_env: AZIZAM_OIDC_CLIENT_SECRET
    redirect_uri: https://ai.example.com/admin/oidc/callback
    signing_key_env: AZIZAM_ADMIN_SESSION_KEY
    allowed_email_domains: ["example.com"]
    admin_emails: ["owner@example.com"]
```

The first allowed OIDC login becomes `owner` if no owner exists yet. Later
allowed logins get the configured role or no admin role until an owner grants
one. A person who signs in without an invitation has **no access plan**: they
can sign in, see their account and ask for access, and nothing else, until an
owner chooses a plan in People.

## Give A Family Member Access

Concrete walkthrough of the canonical "share my gateway with another human"
flow:

1. **Invite them.** In People, choose **Invite someone** and pick one of your
   access plans. There is no "None" option, and the plan is stored with the
   invitation. Settings → People and access sets which plan the form
   preselects (initially **Ask each time**); it affects future invitations
   only.
2. **They sign in** with the invitation. Accepting it gives them that plan and
   a first App, "Same as its person".
3. **Personal App.** The account portal lets them create more Apps. Members
   cannot choose a plan: a member's Apps are always "Same as my access plan".
   Owners and admins can give an App its own plan, which can only narrow what
   the person's plan allows.
4. **App API key.** They mint their own bearer token from the account portal,
   verify the base URL and the capabilities visible to that key, and paste the
   key into their chat app of choice (LibreChat, Open WebUI, an iOS shortcut,
   etc.) with base URL `https://ai.example.com/v1`.

Rotation stays per-App: delete the old Credential and create a new key for the
same App while leaving the person, the plans, and the audit history intact.

Signed-in users may create Personal Apps, create API keys for Apps they own,
and delete those keys from the account portal. Self-service does not let users
change ownership, choose plans, enable paid-provider fallback, or grant
themselves new capabilities; those remain admin-plane actions.

## Request Auth Rules

All gateway runtime traffic requires a valid App API key whose App and accountable person each hold an access plan. `/` (the web app's Home page), `/health`,
`/me/api/session`, and the `/.well-known/*` discovery surfaces are public.
`/me` no longer exists. The web app shell is public only: it does not show
private capability, model, or job data until the browser sends either the
signed OIDC session cookie or the user's `opk_...` key to the normal
authenticated runtime APIs. State-changing
`/me/api/*` requests require a valid signed OIDC session and the
`x-azizam-user-action: 1` plus `x-azizam-csrf` headers generated by the
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

Do not send backend admin keys or downstream provider keys as App auth. The
gateway strips inbound auth headers before forwarding requests and injects the
appropriate downstream credentials itself.

## Smoke Test

After generating an App key, set `AZIZAM_API_KEY` and test that the gateway
accepts it:

```bash
export AZIZAM_API_KEY='opk_...'

curl -s http://127.0.0.1:5000/health

curl -s http://127.0.0.1:5000/v1/voices \
  -H "Authorization: Bearer $AZIZAM_API_KEY"
```

Expected behavior:

- `/health` returns `status: healthy` without auth.
- `/v1/voices` returns voices with a valid key.
- `/v1/voices` returns `401` without a key.

The full local test script also expects the key:

```bash
AZIZAM_API_KEY='opk_...' ./test_e2e.sh
```

Refusals use the OpenAI error shape with `type: "access_error"` and a `code`:
`key_required`, `key_invalid`, `app_disabled`, `person_disabled`,
`no_access_plan` (the `details.side` says whether the person or the App has
none), `not_in_access_plan`, `cannot_start_model`, `blocked_by_category`,
`not_reviewed`, `cap_reached`.

Only the owner can mint, delete or edit access plans and provider-spend keys;
an administrator who tries gets 403 `owner_required`. The same holds for every
write to the providers (add, retarget models, remove, set or delete a key, spend
caps and global caps, record a charge, run a connection check), and for attaching
a credential reference to a service record or moving a service record that
already carries one. An administrator can still manage local engine services
that name no credential. A service's key is sent only to the host it was
attached for; a record without that mark compiles with no key. Schema 8
marked every record that existed then with its current host, once, and wrote
one audit entry `operator.credential_origin.migrate` naming them.

An administrator never acts over someone with more access. Turning on or
renaming an App, removing its trusted address, and revoking its keys answer 403
`app_outranks_you` or `credential_outranks_you` when the App or key belongs to
the owner.

## Downstream Credential Mapping

By default, App API keys map to the global downstream provider credentials
configured for the gateway:

- Backend credentials are service data (`auth_inject` rules naming a `secret_env` variable). The gateway has no built-in credential variables.
- TTS, STT, ComfyUI, Kobold: no extra auth unless configured per key

The current DB runtime path does not expose per-App downstream credential
overrides. Use access plans to decide which models, workflows, and
provider-native routes an App may call; use the gateway's global provider
configuration to decide which local services and backend credentials those
allowed calls use.

## Access plans

An access plan is the authorization and audience boundary for a person or an
App. Plans are stored in `admin.db` and edited in People → Access plans, or
through `/admin/api/access-plans`. A plan document has these fields, all
required:

| Field | Meaning |
|---|---|
| `can_use` | Per capability (`chat`, `image`, `video`, `speech`, `transcribe`, `music`, `sfx`, `audio`, `decide`, `embed`, `rerank`): `{"items": "all"}` or a list of `{"model": name}` / `{"workflow": name}`. A capability that is not listed grants nothing. |
| `direct_engine_access` | `{"engines": [...] or "all", "operations": ["METHOD /path", ...]}`. Empty lists grant nothing. |
| `paid_providers` | Providers a granted item may route to. Empty means engines on this gateway only. |
| `provider_fallback` | Whether a paid provider may be used when the engine is unavailable. |
| `caps` | `usd_per_request`, `usd_per_day`, `usd_per_month`: dollar amounts or `null` for no cap. |
| `can_load_models` | Whether a request may start a model that is not running. |
| `receipt_detail` | `none`, `basic`, `standard` or `full`: how much of a receipt the caller sees. |
| `media_metadata` | `none` or `full`: the most provenance metadata delivered files may carry. |

A request is judged against the accountable person's plan, then the App's plan,
and every part composes by restriction:

- a model or workflow must be granted by both plans;
- paid providers are the intersection; fallback needs both;
- model loading needs both; receipt detail and file metadata use the lower;
- a spending cap applies to the counter of whoever holds the plan: the
  person's plan caps what the person has spent in total (across every App they
  answer for), the App's plan caps the App's total. Provider and gateway caps
  (Providers page) apply too. Changing, renaming or reassigning a plan never
  resets a counter.

When both sides hold the same plan it is evaluated once.

`receipt_detail` maps to a discovery/receipt audience tier. "Client" here is
the protocol/audience tier name, not a product noun — the requester is still
an App:

| Level      | Audience       | What the App sees                              |
|------------|----------------|------------------------------------------------|
| `none`     | client         | No receipts; minimal discovery                 |
| `basic`    | client         | Client-tier receipts (request/job IDs, status) |
| `standard` | admin          | Routing facts, fallback reasons                |
| `full`     | internal       | Backend, policy source, manifest hash, diags   |

Example plan document:

```json
{
  "name": "Family",
  "can_use": {
    "chat": {"items": [{"model": "family-chat"}]},
    "image": {"items": "all"}
  },
  "direct_engine_access": {"engines": [], "operations": []},
  "paid_providers": [],
  "provider_fallback": false,
  "caps": {"usd_per_request": null, "usd_per_day": "2.00", "usd_per_month": "20.00"},
  "can_load_models": true,
  "receipt_detail": "basic",
  "media_metadata": "none"
}
```

A plan cannot be deleted while a person, an App or an open invitation uses it.
Removing a provider removes it from every plan's `paid_providers`.

### Direct engine access

`direct_engine_access` is opt-in and scoped per plan. It is not a blanket
ComfyUI DSL — it is a privileged lane for specific operations on specific
engines, and every use is recorded in receipts.

```json
{"engines": ["comfyui-local"], "operations": ["POST /prompt", "GET /history/*"]}
```

`"*"` as an operation lets the App do anything the engine's own API allows,
including loading or deleting models; narrow it with operation scopes.

Provider-native HTTP passthrough is deliberately namespaced below
`/gateway/v1/providers/{provider}/{path...}`. Do not expose backend-native
routes at `/v1`, `/api`, `/queue`, or other root prefixes. The protocol
frontends keep those roots: OpenAI-compatible uses `/openai/v1` with `/v1` as a
legacy convenience alias, Anthropic Messages uses `/anthropic/v1`, and
Ollama-compatible uses `/ollama/api` with `/api` as a convenience alias. When a
provider's native path collides with a frontend path, the frontend wins and the
native call must go through `/gateway/v1/providers/{provider}/{path...}` with a
plan that grants direct engine access.

### Gateway-owned controls live under extensions

Backend-native power (denoise, controlnet strength, model-specific knobs)
should not be added as global OpenAI fields. It belongs under namespaced
gateway extensions:

```json
{
  "extensions": {
    "com.azizam.workflow": {
      "denoise": 0.35,
      "controlnet_strength": 1.1
    }
  }
}
```

Unknown extension inputs are rejected with a reason; known-but-unsupported
inputs are recorded as ignored. The gateway never silently drops inputs.

## Rotation And Deletion

Rotate one App's API key:

1. Open `/apps/` for keys you own, or as an operator.
2. Delete or revoke the old Credential.
3. Create a new key on the same App.
4. Update that App to use the newly printed `opk_...` value.

Delete an App's API key from `/apps/`; it is revoked immediately in
`admin.db` and rejected on subsequent runtime requests.

## Common Failures

`401 key_required`.

The request reached an authenticated endpoint without
`Authorization: Bearer opk_...` or another accepted auth header.

`401 key_invalid`.

The presented App API key does not match any active App credential in
`admin.db`, or the owning App is disabled.

`401/403 after deleting a key.`

Deleted credentials are revoked immediately. Create a new key on the same App
and update the consuming app.

`502` or `503` backend errors.

The App API key was accepted, but the selected downstream provider was
unavailable or rejected its own backend credentials.

`403 no_access_plan.`

The App's accountable person has no access plan, or the App has none. People
and Apps show "Choose a plan"; an owner picks one.

## Every device needs a key

There is no keyless access. A request from the LAN, from loopback or from a
Tailscale address needs an App key like any other; a source address is not an
identity. Give each device its own App and key in Apps.

Startup refuses a config that still sets `auth.lan_bypass_enabled`,
`auth.lan_bypass_cidrs`, `auth.path_tokens`, `auth.enabled: false` or
`auth.ollama_compat_discovery`, and names the key. Placeholder keys (`lan`,
`sk-local`, `EMPTY`) and unknown `sk-` keys are `401 key_invalid`; the gateway
no longer forwards them to LiteLLM.

### Client address, throttling and first-run setup

The client address still decides throttling and which networks may claim the
first owner (`auth.setup_allowed_cidrs`, env `AZIZAM_SETUP_ALLOWED_CIDRS`); it
never decides access. By default the gateway trusts **only the TCP socket
peer** and never reads `X-Forwarded-For`. You opt into forwarded headers by
listing your proxy in `trusted_proxies`; the gateway then walks the header
**right-to-left** and takes the first IP that is not one of your trusted
proxies. IPv4-mapped IPv6 (`::ffff:1.2.3.4`) is normalized before matching.

| Deployment | gateway config | proxy config |
|---|---|---|
| **No proxy / bare port** | `trusted_proxies: []` | — (socket peer *is* the client) |
| **nginx** (same host) | `trusted_proxies: ["127.0.0.1"]` | `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` |
| **nginx / Traefik** (other host) | `trusted_proxies: ["<proxy-ip>"]` | forward XFF (Traefik: `forwardedHeaders.trustedIPs`) |
| **Caddy** | `trusted_proxies: ["127.0.0.1"]` | `reverse_proxy` sets XFF automatically; set Caddy's own `trusted_proxies` |
| **Nginx Proxy Manager** | `trusted_proxies: ["<npm-ip-or-cidr>"]` | Advanced tab: `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` |
| **Cloudflare Tunnel** | `cloudflare_mode: true`, `trusted_proxies: []` | none (CF-Connecting-IP used; ranges fetched + verified at startup, fail-closed) |
| **Docker bridge** | `trusted_proxies: ["<proxy-container-ip>"]` | NOT the bridge gateway; the gateway warns at startup if peer looks like Docker NAT with no `trusted_proxies` |

> **Cloudflare note:** `cf-connecting-ip` is honored *only* when the connection
> arrives from a verified Cloudflare edge IP. Firewall your origin so it only
> accepts Cloudflare, or an attacker can re-front your origin and forge the header.

### Header-less clients (`?api_key=`)

Some apps (dictation tools, embedded clients) expose only a base-URL field with
no place for an auth header. Enable a query-param key fallback (off by default):

```yaml
auth:
  allow_query_param_key: true
```

Env override: `AZIZAM_QUERY_PARAM_KEY=true`. The client uses base URL
`https://your-host/v1?api_key=YOURKEY`. The gateway strips `api_key` before
forwarding upstream so it never reaches the backend or its logs. **Note:** query
params can still appear in *front-proxy* access logs — prefer the header when
the client allows it.

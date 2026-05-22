# First-Run Setup

The first-run contract is web-first. A new self-hoster should be able to:

1. Start the gateway with near-zero `.env`.
2. Open `/setup` in a browser.
3. Claim or create the first local owner.
4. Let the app seed sane defaults and mint the first App + `opk_...` runtime key.
5. Manage providers, OIDC, users, Apps, Profiles, keys, limits, and service
   availability from `/admin`.

`/setup` is intentionally small. It does not configure backends, edit OIDC, or
create policy abstractions. It only answers: "Can this browser administer the
gateway, and does this operator have one working `opk_...` key to test a
client?"

## Short Path

If you just want a working gateway, in order:

1. Provide an admin session signing key. Either set
   `AI_GATEWAY_ADMIN_SESSION_KEY` to a 32+ byte random string, or set
   `admin.session_signing_key` in `proxy_config.yml`. Without one, the browser
   admin session cannot be issued.
2. Start the service (`./start.sh`, the systemd unit, or `docker compose up`
   once a compose file is provided for your deployment).
3. Open `http://127.0.0.1:5000/setup`.
4. Claim or create the first owner (see [Setup States](#setup-states)).
5. Mint the first `opk_...` runtime key from the same page and configure your
   client against `/openai/v1`.
6. Everything else — Profiles, identity/Profile mappings, role/model bindings,
   provider toggles, admission caps, OIDC details, additional Apps and keys —
   is dashboard work at `/admin`. Restart is only required for bootstrap
   changes (listener, TLS, OIDC issuer/client, signing keys, provider base URLs
   and secrets).

> **Implementation gap.** The session signing key is not auto-generated yet.
> If the env var and YAML value are both missing, `/setup/claim` will refuse to
> issue a browser admin session. Set one explicitly until the app persists a
> generated key on first boot.

## Bootstrap Config

Before opening the page, the service needs only the things required to start
safely:

- listener host/port and deployment shape,
- `admin.session_signing_key` (or `AI_GATEWAY_ADMIN_SESSION_KEY`),
- optional `admin.oidc` issuer/client/trust configuration,
- backend base URLs, model directories, and provider secrets,
- optional `admin.user_defaults` or user-access env flags as a one-time
  bootstrap seed for self-service defaults and identity/Profile mappings.

Everything that can change after boot belongs in `admin.db` and the dashboard.
On startup, bootstrap user defaults are imported into `admin.db`; after that,
dashboard/API state is authoritative. See
[CONFIG_SOURCE_OF_TRUTH.md](CONFIG_SOURCE_OF_TRUTH.md).

If no `db_path` is set, the gateway uses `data/admin.db` next to `proxy.py`.
That directory is host-local runtime state — gitignored, but durable across
restarts. For container deployments, mount a volume at the same path so the
owner row, Apps, and Profiles survive `docker compose down`.

## Setup States

### OIDC Configured, No Owner

`/setup` shows one primary action: sign in with OIDC.

The first allowed OIDC login becomes `owner` when no owner/admin exists yet.
After the callback, the browser lands back on `/setup`, shows the signed-in
account, and can mint the first key for the user's Personal App.

### No OIDC, No Owner

`/setup` shows the host-side recovery command that mints the first admin key:

```bash
./venv/bin/python proxy.py generate-admin-key owner-cli
```

The command prints one `oak_...` admin key. Paste that key into `/setup`;
`POST /setup/claim` validates it, creates a signed browser admin session, and
redirects back to `/setup`. This is the only normal-path use of the CLI; once
the browser session is active, all subsequent owner/admin work happens through
`/setup` and `/admin`.

An `opk_...` key is a runtime App key and is rejected here with a clear error.
It should be used in OpenAI/Ollama/Anthropic clients, not to administer the
gateway.

### Signed-In Owner/Admin

`/setup` shows:

- links to the account portal, admin dashboard, and operator status;
- a compact "Connect a client" card that creates or reuses the signed-in
  user's Personal App and mints an `opk_...` key shown once;
- the OpenAI-compatible base URL for new clients: `/openai/v1`;
- a small setup doctor for owner/admin state, browser session, runtime key
  presence, config source-of-truth, gateway health, and backend reachability.

If self-service Apps are deliberately disabled, the setup key action falls
back to creating an admin-owned service App/key instead of bypassing that
policy.

### Owner Exists, Viewer Not Signed In

`/setup` remains a thin handoff page:

- unsigned operators with OIDC configured can sign in;
- unsigned operators without OIDC can paste an existing `oak_...` admin key to
  open a browser admin session;
- no runtime secrets, backend details, or admin-only setup doctor are shown.

## Recovery And Automation

`generate-admin-key` remains the recovery/headless path. It is useful when:

- OIDC is down or misconfigured,
- the operator is installing over SSH,
- an existing browser session was lost,
- a previously claimed owner needs to be re-issued from the host.

Admin keys are never runtime keys. Use `oak_...` only for `/admin`,
`/admin/api/*`, `/status`, `/v1/status`, or `/setup/claim`.

## What Lives Where After First Run

| Concern                                             | Edited via       | Restart? |
|-----------------------------------------------------|------------------|----------|
| Listener, TLS, OIDC issuer/client, signing keys     | `proxy_config.yml` / env / Docker secrets | yes |
| Provider base URLs, model directories, secrets      | `proxy_config.yml` / env / Docker secrets | yes |
| Owner/admin roles, Apps, Profiles, App API keys     | `/setup`, `/`, `/admin`                  | no  |
| Identity → Profile mappings, role/model bindings    | `/admin`                                 | no  |
| Admission caps, throttles, runtime routing toggles  | `/admin`                                 | no  |
| Provider/service enablement                         | `/admin`                                 | no  |

Provisioning bundles and as-code config export are deliberately out of scope
for first-run. Default Profiles and user-access defaults are seeded into
`admin.db` on startup; once seeded, dashboard state is authoritative.

## What Not To Build Here

Do not turn `/setup` into a second dashboard or YAML editor:

- no backend-specific wizards,
- no in-browser OIDC secret editor,
- no Profile picker before the first App key,
- no duplicate policy editor,
- no separate "setup mode" flag.

The state is derived from durable facts: owner/admin roles, OIDC availability,
browser session validity, active App keys, and cached gateway/backend health.

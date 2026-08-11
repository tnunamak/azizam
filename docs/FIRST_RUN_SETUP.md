# First-Run Setup

The first-run contract is web-first. A new self-hoster should be able to:

1. Start the gateway with near-zero `.env`.
2. Open `/setup` in a browser.
3. Claim or create the first local owner.
4. Let the app seed sane defaults and mint the first App + `opk_...` runtime key.
5. Manage the service/provider registry, OIDC, users, Apps, Profiles, keys,
   limits, and service availability from `/admin`.

`/setup` is intentionally small. It does not configure backends, edit OIDC, or
create policy abstractions. It only answers: "Can this browser administer the
gateway, and does this operator have one working `opk_...` key to test a
client?"

## Docker Compose Quick Start

The fastest path for a self-hosted deployment:

```bash
docker compose up -d
```

Then open `http://127.0.0.1:5000/setup` to claim the first owner account and
mint a runtime key. No `.env` file is required.

To override the port, store paths, or session key, copy `.env.example` to
`.env` and uncomment the relevant lines before starting:

```bash
cp .env.example .env   # then edit as needed
docker compose up -d
```

Persistent state (SQLite databases, session keys) is stored in the
`gateway-data` Docker volume, which maps to `/app/data` inside the container.
Store paths can be overridden via env vars (`AI_GATEWAY_ADMIN_DB`,
`AI_GATEWAY_RESPONSES_DB`, `AI_GATEWAY_METRICS_DB`, `AI_GATEWAY_RECEIPTS_DB`,
`AI_GATEWAY_MEDIA_JOBS_DB`) — see `.env.example` for the full list.
To use a host directory instead of a named volume, replace the volume entry in
`docker-compose.yml`:

```yaml
volumes:
  - ./data:/app/data
```

## Short Path

If you just want a working gateway, in order:

1. Start the service (`./start.sh`, `docker compose up -d`, or the systemd unit).
2. Open `http://127.0.0.1:5000/setup`.
3. Claim or create the first owner (see [Setup States](#setup-states)).
4. Mint the first `opk_...` runtime key from the same page and configure your
   client against `/openai/v1`.
5. Everything else — Profiles, identity/Profile mappings, role/model bindings,
   service/provider records, admission caps, OIDC details, additional Apps
   and keys — is dashboard work at `/admin`. Restart is only required for
   deployment bootstrap changes such as listener/TLS settings or secret/env
   changes.

## Bootstrap Config

Before opening the page, the service needs only the things required to start
safely:

- listener host/port and deployment shape,
- optional `admin.session_signing_key` (or
  `AI_GATEWAY_ADMIN_SESSION_KEY`) when you want to externalize the browser
  session secret. If omitted, the gateway creates a persistent
  `data/admin_session_key` file next to `admin.db`,
- optional bootstrap `admin.oidc` issuer/client/trust configuration when you
  want OIDC before the first local owner login. Otherwise configure OIDC from
  `/admin` after setup,
- service/provider seed or provisioning input, model directories, and
  provider secret references,
- optional `admin.user_defaults` or user-access env flags as a one-time
  bootstrap seed for self-service defaults and identity/Profile mappings.

Everything that can change after boot belongs in `admin.db` and the dashboard.
The DB/UI service registry is the product source of truth for providers and
services. `proxy_config.yml` can seed or provision records, but it is not the
normal live operator surface. After the one-time seed, every non-managed
registry service can be edited, enabled or disabled, and removed in `/admin`
regardless of its `source`; source is provenance, not permission. The managed
LiteLLM substrate separately locks its endpoint/protocol fields and removal,
while allowing its name, enabled state, and note to be changed.
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

`/setup` shows a short local owner form: email, password, confirmation. That
creates a local `owner` user, stores a password verifier in `admin.db`, issues
the browser admin session, and redirects back to `/setup`.

The host-side `generate-admin-key owner-cli` command remains in a recovery
disclosure on the page. It is not the happy path.

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
- unsigned operators without OIDC can sign in with the local owner account;
- an existing `oak_...` admin key can still open a browser admin session from
  the recovery disclosure;
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
| Listener, TLS, session signing key                  | `proxy_config.yml` / env / generated `data/admin_session_key` | yes for explicit changes |
| OIDC provider settings                              | `/admin` (optional bootstrap fallback in `proxy_config.yml`) | no |
| Service/provider registry                           | `/admin`; `source` (`ui`, `provisioned`, `builtin`, `discovered`) is provenance | no for non-managed records |
| Provider seed/provisioning input, model directories, secret references | `proxy_config.yml` / env / Docker secrets | yes |
| Owner/admin roles, Apps, Profiles, App API keys     | `/setup`, `/`, `/admin`                  | no  |
| Identity → Profile mappings, role/model bindings    | `/admin`                                 | no  |
| Admission caps, throttles, runtime routing toggles  | `/admin`                                 | no  |
| Provider/service enablement                         | `/admin` for every non-managed registry record; LiteLLM remains a managed substrate | no  |
| Service-registry export                             | Authenticated `GET /admin/api/config/export`; read-only download of service records | no |

First-run does not require the shipped service-registry export. Full
provisioning bundles, restore, and authoritative-file reconciliation remain out
of scope. Default Profiles and user-access defaults are seeded into `admin.db`
on startup; once seeded, dashboard state is authoritative.

## What Not To Build Here

Do not turn `/setup` into a second dashboard or YAML editor:

- no backend-specific wizards,
- no Profile picker before the first App key,
- no duplicate policy editor,
- no separate "setup mode" flag.

The state is derived from durable facts: owner/admin roles, OIDC availability,
browser session validity, active App keys, and cached gateway/backend health.

# First-Run Setup

The first-run contract is web-first:

1. Start the gateway with minimal bootstrap config.
2. Open `/setup`.
3. Claim a browser admin session.
4. Mint the first runtime App key from the same page.
5. Use `/admin` for Profiles, users, role mappings, shared Apps, and runtime access policy.

`/setup` is intentionally small. It does not configure backends, edit OIDC, or create policy abstractions. It only answers: "Can this browser administer the gateway, and does this operator have one working `opk_...` key to test a client?"

## Bootstrap Config

Before opening the page, the service needs only the things required to start safely:

- listener/port and deployment shape,
- `admin.session_signing_key` or `AI_GATEWAY_ADMIN_SESSION_KEY`,
- optional `admin.oidc` issuer/client/trust configuration,
- backend base URLs, model directories, and provider secrets.
- optional `admin.user_defaults` or user-access env flags as a one-time
  bootstrap seed for self-service defaults and identity/Profile mappings.

Everything that can change after boot belongs in `admin.db` and the dashboard.
On startup, bootstrap user defaults are imported into `admin.db`; after that,
dashboard/API state is authoritative. See
[CONFIG_SOURCE_OF_TRUTH.md](CONFIG_SOURCE_OF_TRUTH.md).

## Setup States

### OIDC Configured, No Owner

`/setup` shows one primary action: sign in with OIDC.

The first allowed OIDC login becomes `owner` when no owner/admin exists yet. After the callback, the browser lands back on `/setup`, shows the signed-in account, and can mint the first key for the user's Personal App.

### No OIDC, No Owner

`/setup` shows one host-side command:

```bash
./venv/bin/python proxy.py generate-admin-key owner-cli
```

The command prints one `oak_...` admin key. Paste that key into `/setup`; `POST /setup/claim` validates it, creates a signed browser admin session, and redirects back to `/setup`.

An `opk_...` key is a runtime App key and is rejected with a clear error. It should be used in OpenAI/Ollama/Anthropic clients, not to administer the gateway.

### Signed-In Owner/Admin

`/setup` shows:

- links to the account portal, admin dashboard, and operator status;
- a compact "Connect a client" card that creates or reuses the signed-in user's Personal App and mints an `opk_...` key shown once;
- the OpenAI-compatible base URL for new clients: `/openai/v1`;
- a small setup doctor for owner/admin state, browser session, runtime key
  presence, config source-of-truth, gateway health, and backend reachability.

If self-service Apps are deliberately disabled, the setup key action falls back to creating an admin-owned service App/key instead of bypassing that policy.

### Owner Exists, Viewer Not Signed In

`/setup` remains a thin handoff page:

- unsigned operators with OIDC configured can sign in;
- unsigned operators without OIDC can paste an existing `oak_...` admin key to open a browser admin session;
- no runtime secrets, backend details, or admin-only setup doctor are shown.

## Recovery And Automation

`generate-admin-key` remains the recovery/headless path. It is useful when:

- OIDC is down or misconfigured,
- the operator is installing over SSH,
- config management needs an admin-plane credential,
- an existing browser session was lost.

Admin keys are never runtime keys. Use `oak_...` only for `/admin`, `/admin/api/*`, `/status`, `/v1/status`, or `/setup/claim`.

## What Not To Build Here

Do not turn `/setup` into a second dashboard or YAML editor:

- no backend-specific wizards,
- no in-browser OIDC secret editor,
- no Profile picker before the first App key,
- no duplicate policy editor,
- no separate "setup mode" flag.

The state is derived from durable facts: owner/admin roles, OIDC availability, browser session validity, active App keys, and cached gateway/backend health.

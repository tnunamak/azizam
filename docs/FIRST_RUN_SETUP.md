# First-Run Setup

The first-run contract is web-first:

1. Start the gateway with minimal bootstrap config.
2. Open `/setup`.
3. Claim a browser admin session.
4. Use `/admin` for Apps, Profiles, keys, users, role mappings, and runtime access policy.

`/setup` is intentionally small. It does not configure backends, edit OIDC, or create policy abstractions. It only answers: "Can this browser administer the gateway yet? If not, what is the next host-side action?"

## Bootstrap Config

Before opening the page, the service needs only the things required to start safely:

- listener/port and deployment shape,
- `admin.session_signing_key` or `AI_GATEWAY_ADMIN_SESSION_KEY`,
- optional `admin.oidc` issuer/client/trust configuration,
- backend base URLs, model directories, and provider secrets.

Everything that can change after boot belongs in `admin.db` and the dashboard. See [CONFIG_SOURCE_OF_TRUTH.md](CONFIG_SOURCE_OF_TRUTH.md).

## Setup States

### OIDC Configured, No Owner

`/setup` shows one primary action: sign in with OIDC.

The first allowed OIDC login becomes `owner` when no owner/admin exists yet. After the callback, the browser lands back on `/setup`, which then points to `/admin`.

### No OIDC, No Owner

`/setup` shows one host-side command:

```bash
./venv/bin/python proxy.py generate-admin-key owner-cli
```

The command prints one `oak_...` admin key. Paste that key into `/setup`; `POST /setup/claim` validates it, creates a signed browser admin session, and redirects back to `/setup`.

An `opk_...` key is a runtime App key and is rejected with a clear error. It should be used in OpenAI/Ollama/Anthropic clients, not to administer the gateway.

### Owner Exists

`/setup` becomes a thin handoff page:

- signed-in admins see links to `/admin`, `/status`, and `/`;
- unsigned operators with OIDC configured can sign in;
- unsigned operators without OIDC can paste an existing `oak_...` admin key to open a browser admin session.

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
- no Profile picker before the first App,
- no duplicate policy editor,
- no separate "setup mode" flag.

The state is derived from durable facts: owner/admin roles, OIDC availability, browser session validity, and whether a session signing key exists.

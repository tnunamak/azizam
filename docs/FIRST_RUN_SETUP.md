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

Docker Compose starts two containers:

- `gateway`, the public API and admin UI on port 5000;
- `litellm`, the managed LiteLLM sidecar that executes chat-shaped requests.

`AI_GATEWAY_LITELLM_BASE_URL` is the URL of that sidecar as seen by the
gateway container. Compose sets it to `http://litellm:4000`. Do not use this
variable for Ollama, llama.cpp, vLLM, TabbyAPI, or another user engine.
Add user engines as Services records instead.

Compose publishes the gateway as `127.0.0.1:${OPENAI_PROXY_PORT:-5000}:5000`
and sets `AI_GATEWAY_CONFIG_PATH=/app/first-run.yml`. That file is a minimal
fresh-install config:

- `proxy.host: 127.0.0.1`, so bare Python stays local by default;
- `admin.oidc.cookie.secure: false`, so localhost HTTP setup clients can store
  the browser admin session cookie;
- no engine records.

Compose overrides only the container listener with
`python3 -m uvicorn proxy:app --host 0.0.0.0 --port 5000`, then publishes that
container port only on host loopback.

`cookie.secure: false` is only for localhost HTTP first-run. Before exposing
the gateway over HTTPS, change the deployment config back to secure cookies
(`admin.oidc.cookie.secure: true`) and publish it behind TLS.

On Linux, Compose also maps `host.docker.internal` to `host-gateway` for the
gateway container. The engine must still listen on an address reachable from
the container. If a local engine only binds to `127.0.0.1` on the host, either
start it on `0.0.0.0` for local setup or use another host address the container
can reach.

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

The LiteLLM sidecar image is pinned to the same LiteLLM version used by the
Python dependency lock: `ghcr.io/berriai/litellm:1.86.2` at digest
`sha256:cae1ac3492d6d0bea69c26f4485381624e073eb753f3534ae7703a4204a4ce6b`.
The gateway Docker image, native quickstart, and CI install Python packages
from the existing `tests/requirements-hermetic.lock` with hash verification.
That reviewed closure includes test tooling; no package versions are upgraded
for first-run. The Docker installer is uv 0.12.21, pinned by image digest.

## Short Path

If you just want a working gateway, in order:

1. Start the service with Docker Compose:

   ```bash
   docker compose up -d
   ```

   For bare Python, use the same first-run config and the hash-locked
   dependency file:

   ```bash
   AI_GATEWAY_CONFIG_PATH=first-run.yml \
   AI_GATEWAY_REQUIREMENTS_FILE=tests/requirements-hermetic.lock \
   AI_GATEWAY_REQUIREMENTS_HASHES=true \
   ./start.sh
   ```

2. Open `http://127.0.0.1:5000/setup`.
3. Claim or create the first owner (see [Setup States](#setup-states)).
4. Mint the first `opk_...` runtime key from the same page and configure your
   client against `/openai/v1`.
5. Add a chat engine from `/admin` -> Services. Use the engine's
   OpenAI-compatible base URL as seen by the gateway and add at least one model
   mapping. For generic OpenAI-compatible engines, set health endpoint
   `/v1/models`. Example: public model `my-llama` -> upstream model
   `TheBloke_Llama-3-8B`.
6. Test chat with the minted key:

   ```bash
   curl -s http://127.0.0.1:5000/v1/chat/completions \
        -H "Authorization: Bearer opk_YOUR_KEY_HERE" \
        -H "Content-Type: application/json" \
        -d '{"model":"my-llama","messages":[{"role":"user","content":"Hello"}]}'
   ```

Everything else — Profiles, identity/Profile mappings, role/model bindings,
service/provider records, admission caps, OIDC details, additional Apps and
keys — is dashboard work at `/admin`. Restart is only required for deployment
bootstrap changes such as listener/TLS settings or secret/env changes.

For headless automation, first fetch the current service-registry generation,
then post the service with `If-Match`:

```bash
etag="$(
  curl -si http://127.0.0.1:5000/admin/api/operator/service-registry \
       -H "Authorization: Bearer oak_YOUR_ADMIN_KEY_HERE" \
  | awk 'BEGIN{IGNORECASE=1} /^etag:/ {gsub("\r",""); print $2}'
)"

curl -s -X POST http://127.0.0.1:5000/admin/api/operator/services \
     -H "Authorization: Bearer oak_YOUR_ADMIN_KEY_HERE" \
     -H "If-Match: ${etag}" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "Local chat engine",
       "preset": "openai-compatible",
       "base_url": "http://host.docker.internal:8080/v1",
       "health_endpoint": "/v1/models",
       "capabilities": ["chat"],
       "models": [
         {"public": "my-llama", "upstream": "TheBloke_Llama-3-8B"}
       ]
     }'
```

Do not send inline provider or engine secrets to this API. If a service needs a
key, set the key in the process environment and send its variable name as
`api_key_env`.

The executable first-run check is `scripts/e2e/fresh_instance.sh`. It uses only
HTTP APIs:

1. `GET /setup` confirms the local-owner form exists.
2. `POST /setup/local-owner` creates the first owner and browser session.
3. `GET /admin/api/session` reads the signed-in actor and CSRF token.
4. `POST /setup/first-app` mints the first `opk_...` runtime key.
5. `GET /admin/api/operator/service-registry` reads the registry `ETag`.
6. `POST /admin/api/operator/services` registers the fake OpenAI-compatible
   engine with `If-Match`, `health_endpoint: /v1/models`, and a model mapping.
7. `POST /openai/v1/chat/completions` verifies unauthenticated 401 first, then
   authenticated chat success.

Run the native variant with:

```bash
scripts/e2e/fresh_instance.sh
```

Run the Compose variant, when Docker is available, with:

```bash
scripts/e2e/fresh_instance.sh --compose
```

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

# First-Run Setup

The first-run contract is web-first. A new self-hoster should be able to:

1. Start the gateway with near-zero `.env`.
2. Open `/setup/` in a browser.
3. Claim or create the first local owner.
4. Let the app seed sane defaults and mint the first App + `opk_...` runtime key.
5. Manage the service/provider registry, OIDC, users, Apps, Profiles, keys,
   limits, and service availability from the web app (`/settings/`,
   `/people/`, `/apps/`, `/engines/`, `/providers/`).

`/setup/` is intentionally small. It does not configure backends, edit OIDC, or
create policy abstractions. It only answers: "Can this browser administer the
gateway, and does this operator have one working `opk_...` key to test a
client?"

The server-rendered operator setup console (the old `/setup?operator=1`) is at
`/setup/console`. The setup forms post to `/setup/claim`, `/setup/local-owner`,
`/setup/local-login` and `/setup/first-app`.

## Docker Compose Quick Start

The fastest path for a self-hosted deployment:

```bash
docker compose up -d
```

Then open `http://127.0.0.1:5000/setup/` to claim the first owner account and
mint a runtime key. No `.env` file is required.

Docker Compose starts two containers:

- `gateway`, the public API and admin UI on port 5000;
- `litellm`, the managed LiteLLM sidecar that executes chat-shaped requests.

`AZIZAM_LITELLM_BASE_URL` is the URL of that sidecar as seen by the
gateway container. Compose sets it to `http://litellm:4000`. Do not use this
variable for Ollama, llama.cpp, vLLM, or another user engine.
Add user engines as Services records instead.

Compose publishes the gateway as `127.0.0.1:${AZIZAM_PORT:-5000}:5000`
and sets `AZIZAM_CONFIG_PATH=/app/first-run.yml`. That file is a minimal
fresh-install config:

- `proxy.host: 127.0.0.1`, so bare Python stays local by default;
- `admin.oidc.cookie.secure: false`, so localhost HTTP setup clients can store
  the browser admin session cookie;
- no engine records.

Compose overrides only the container listener with
`python3 -m uvicorn proxy:app --host 0.0.0.0 --port 5000`, then publishes that
container port only on host loopback.

`cookie.secure: false` is only for localhost HTTP first-run. Before exposing
the gateway over HTTPS, set `admin.oidc.cookie.secure: true` in
`first-run.yml`, then run `docker compose up -d --build`. The image copies that
file in at build time, and no environment variable overrides this setting.
Then publish the gateway behind TLS.

On Linux, Compose also maps `host.docker.internal` to `host-gateway` for the
gateway container. Docker Desktop (Mac, Windows) provides that name itself.
The engine must still listen on an address reachable from the container. If a local engine only binds to `127.0.0.1` on the host, either
start it on `0.0.0.0` for local setup or use another host address the container
can reach. Binding to `0.0.0.0` exposes the engine to your whole network; restrict
its port with your firewall so only the gateway container can reach it.

To override the port, store paths, or session key, copy `.env.example` to
`.env` and uncomment the relevant lines before starting:

```bash
cp .env.example .env   # then edit as needed
docker compose up -d
```

Persistent state (SQLite databases, session keys) is stored in the
`gateway-data` Docker volume, which maps to `/app/data` inside the container.
Generated media and the download-link signing key are in the `gateway-cache`
volume, which maps to `/app/cache`.
Store paths can be overridden via env vars (`AZIZAM_ADMIN_DB`,
`AZIZAM_RESPONSES_DB`, `AZIZAM_METRICS_DB`, `AZIZAM_RECEIPTS_DB`,
`AZIZAM_MEDIA_JOBS_DB`) — see `.env.example` for the full list.
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

## Bare-metal install (no Docker)

Use this path when you cannot or do not want to run Docker. The steps were
written for one Linux computer with GNU coreutils. Other systems are not
tested here.

Install these first:

- **uv**, a Python installer. See the
  [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).
  `start.sh` uses uv to create a Python 3.12 environment, so uv must be able
  to find or download Python 3.12.
- **Node.js 22** with `npm`. The Docker build uses Node 22.
- **sha256sum** and **timeout**. `start.sh` calls both. Check them with
  `command -v sha256sum timeout`.

Then follow these steps. Each step has one command or one action.

1. Get the code and enter its folder:

   ```bash
   git clone https://github.com/tnunamak/azizam.git
   cd azizam
   ```

2. Build the web interface. Pages such as `/setup/` come from this build.
   Without it, those pages answer `503`.

   ```bash
   npm --prefix web ci
   npm --prefix web run build
   ```

3. Start the gateway. Leave this terminal open:

   ```bash
   AZIZAM_CONFIG_PATH=first-run.yml \
   AZIZAM_REQUIREMENTS_FILE=tests/requirements-hermetic.lock \
   AZIZAM_REQUIREMENTS_HASHES=true \
   ./start.sh
   ```

   The first start creates `venv/` and installs the pinned Python packages.
   This can take several minutes. `start.sh` also starts the LiteLLM chat
   sidecar on `127.0.0.1:4011`.

   Keep `AZIZAM_CONFIG_PATH=first-run.yml` on the first start. That file
   makes the gateway listen only on this computer. Without it, the default
   config listens on all network interfaces. Its setup rule then allows
   claiming the owner account from any `192.168.x.x`, `10.x.x.x` or
   `100.64.0.0/10` address.

4. Check that the gateway answers. In a second terminal:

   ```bash
   curl -i http://127.0.0.1:5000/health
   ```

   The first line of the reply should be `HTTP/1.1 200 OK`.

5. Create the owner account. Open `http://127.0.0.1:5000/setup/`, then enter
   an email address, a password, and the password again.

6. Add an engine. Go to [Connect an engine by where it runs](#connect-an-engine-by-where-it-runs).

To stop the gateway, press Ctrl+C in the terminal that runs `start.sh`. To
restart after a config change, use `scripts/safe-restart`. See
[docs/ops/SAFE_RESTART.md](ops/SAFE_RESTART.md). Do not restart with a plain
`systemctl` command.

## Connect an engine by where it runs

The address you type in **Add engine** is the address the **gateway** uses.
It is not the address your browser uses. Find where the gateway runs and where
the engine runs. Then use the matching section below. For an engine that the
Add engine dialog does not list, see
[Add an HTTP engine as data](guides/ADD_HTTP_ENGINE_AS_DATA.md).

Two settings matter:

- **Address:** the URL the gateway calls. Keep `/v1` for OpenAI-compatible
  servers such as llama.cpp, vLLM and LM Studio. Leave `/v1` off for Ollama.
- **Listener:** the network address the engine accepts connections on. An
  engine that listens only on `127.0.0.1` accepts calls from its own computer
  only. On Linux, a Docker container cannot reach it. Docker Desktop is the
  exception, as the Compose section below explains.

### Engine on the same computer, gateway running bare metal

- Address: `http://127.0.0.1:8080/v1`. For Ollama, use `http://127.0.0.1:11434`.
- Listener: the default loopback listener is fine.
- Reachability check, run on the same computer:

  ```bash
  curl -i http://127.0.0.1:8080/v1/models
  ```

  Any HTTP status, including `401`, means the engine answered. "Connection
  refused" means nothing listens on that port.

### Engine on the same computer, gateway in Docker Compose

Inside the gateway container, `127.0.0.1` means the container itself. Use the
name `host.docker.internal` instead.

- Address: `http://host.docker.internal:8080/v1`. For Ollama, use
  `http://host.docker.internal:11434`. Compose maps this name on Linux. Docker
  Desktop provides it too.
- Listener: start the engine so it accepts connections on all interfaces, not
  only loopback. Examples: llama.cpp `llama-server --host 0.0.0.0 --port 8080`,
  vLLM `--host 0.0.0.0`, LM Studio **Serve on Local Network**, Ollama
  `OLLAMA_HOST=0.0.0.0:11434`. If Ollama runs as a service, run
  `sudo systemctl edit ollama`, add `Environment="OLLAMA_HOST=0.0.0.0:11434"`
  under the `[Service]` line, then restart it. Check your engine's
  documentation for its flag names.

  The engine must accept connections from the Compose network. Compose creates
  that network's bridge address only when `docker compose up` runs, so the
  engine cannot bind to that address beforehand. On Linux, bind to `0.0.0.0`.
  On Docker Desktop, `host.docker.internal` reaches the host's loopback address,
  so a loopback-only engine may already answer. The check below shows which
  case applies to you.
- Firewall: allow only the Compose network to reach the engine port. By default
  that network is `172.30.251.0/24` (`AZIZAM_DOCKER_SUBNET`). On Linux, for
  ufw:

  ```bash
  sudo ufw allow from 172.30.251.0/24 to any port 8080 proto tcp
  ```

  Use your own subnet if you changed `AZIZAM_DOCKER_SUBNET`.

  ufw ignores rules until it is enabled. Run `sudo ufw status`. If it says
  `inactive`, the engine port is open to your whole network. Enable ufw first
  (allow SSH before you do, with `sudo ufw allow OpenSSH`), or set an API key
  on the engine.
- Reachability check, run from the gateway container:

  ```bash
  docker compose exec gateway python3 -c "import urllib.request as r; r.urlopen('http://host.docker.internal:8080/v1/models', timeout=5)"
  ```

  No output means the engine answered with a 2xx status. An `HTTPError` with
  code `401` or `403` means the engine answered and needs a key, so the path
  works. A timeout or `Connection refused` error means the listener or the
  firewall still blocks the path.

### Engine on another computer on your network

- Address: the other computer's LAN address, for example
  `http://192.168.1.50:8080/v1`. On that computer, run `ip addr` to find it.
  Do not use `127.0.0.1`, `localhost` or `host.docker.internal`. Those names
  point to the gateway's own computer.
- Listener: bind to `0.0.0.0` or the LAN address, not only `127.0.0.1`. Use the
  flags from the Docker section above, such as `--host 0.0.0.0` for llama.cpp
  or vLLM, LM Studio **Serve on Local Network**, or `OLLAMA_HOST=0.0.0.0:11434`
  for Ollama.
- Firewall on the engine's computer: allow only the gateway computer. On Linux,
  for ufw, where `192.168.1.10` is the gateway computer's LAN address:

  ```bash
  sudo ufw allow from 192.168.1.10 to any port 8080 proto tcp
  ```

  A gateway in Docker sends traffic with its host's LAN address, so use the
  host address here.

  ufw ignores rules until it is enabled. Run `sudo ufw status`. If it says
  `inactive`, the engine port is open to your whole network. Enable ufw first
  (allow SSH before you do, with `sudo ufw allow OpenSSH`), or set an API key
  on the engine.
- Reachability check. For bare metal, run this on the gateway computer:

  ```bash
  curl -i http://192.168.1.50:8080/v1/models
  ```

  For Compose, run this from the gateway container:

  ```bash
  docker compose exec gateway python3 -c "import urllib.request as r; r.urlopen('http://192.168.1.50:8080/v1/models', timeout=5)"
  ```

  Read the result the same way as in the Docker section.

### Open WebUI or another client

A client connects to the gateway, not to an engine. Use your App key and the
`/openai/v1` path. Choose the row that matches your setup.

- **Same computer, client run directly on the host** (gateway bare metal or
  Compose). Base URL: `http://127.0.0.1:5000/openai/v1`. Check the gateway
  with `curl -i http://127.0.0.1:5000/health`.
- **Same computer, Open WebUI in a Docker container.** Compose publishes the
  gateway only on the host's loopback address, so a container cannot reach it
  through `host.docker.internal`. Run Open WebUI with host networking. Add
  `--network host -e PORT=3000` to your usual Open WebUI `docker run` command.
  Host networking shares the host's ports. Open WebUI would otherwise take port
  8080, which the engine examples above use. Open http://localhost:3000. Use the
  base URL `http://127.0.0.1:5000/openai/v1`. If another program uses port 3000,
  choose another `PORT`.
- **Another device on your network.** The gateway's loopback address is not
  reachable from that device. Put an HTTPS reverse proxy on the gateway
  computer that forwards to `http://127.0.0.1:5000`. See
  [reverse proxy setup](REVERSE_PROXY.md). Before you use HTTPS, set
  `admin.oidc.cookie.secure: true` in `first-run.yml`. With Compose, then run
  `docker compose up -d --build`. On bare metal, restart with
  `scripts/safe-restart`. This step makes the gateway reachable beyond your computer, so read the security
  section of the [README](../README.md#security-model) first. Then use the base
  URL `https://your-gateway-name/openai/v1`. Check it from the other device
  with `curl -i https://your-gateway-name/health`.

In Open WebUI, go to **Settings → Connections → OpenAI**. Paste the base URL.
In the API key field, paste the `opk_...` value itself. Do not type the text
`$AZIZAM_API_KEY`. Then refresh the model list.

## Short Path

If you just want a working gateway, in order:

1. Start the service with Docker Compose:

   ```bash
   docker compose up -d
   ```

   For bare metal, follow [Bare-metal install](#bare-metal-install-no-docker).

2. Open `http://127.0.0.1:5000/setup/`.
3. Claim or create the first owner (see [Setup States](#setup-states)).
4. Mint the first `opk_...` runtime key from the same page and configure your
   client against `/openai/v1`.
5. Add an engine from Gateway → Engines (`/engines/`). Choose the address for
   your layout in [Connect an engine by where it runs](#connect-an-engine-by-where-it-runs).
   Then keep the public model name the dialog suggests, or rename it. For
   example, the public name `my-llama` can map to the upstream model
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
keys — is work in the web app. Restart is only required for deployment
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
       "definition": "openai-compatible",
       "name": "Local chat engine",
       "values": {"address": "http://host.docker.internal:8080/v1"},
       "capabilities": ["chat"],
       "models": [{"id": "TheBloke_Llama-3-8B", "public_name": "my-llama"}]
     }'
```

This is the same path the Add engine dialog takes. `GET /admin/api/engine-definitions`
lists the kinds of server and their fields. The gateway checks the server
before it saves: an error (unreachable, key refused) answers 422 with the
check; a warning answers 409 until you send `"acknowledge_warnings": true`;
`"disabled": true` saves the engine turned off without a check. To see the
check without saving, `POST /admin/api/engines/check` with the same
`definition` and `values`.

A key goes in `values.api_key` (owner only). The gateway stores it encrypted,
binds it to that address and never returns it. `values.api_key_env` names a
server environment variable instead. `values.headers` rows are sent with every
request to the engine and saved as plain text, so a key never goes there.

Before you add it, `POST /admin/api/engines/access` with
`{"capabilities": ["chat"], "models": ["my-llama"]}` says which access plans
will let people and apps use those model names. New models follow the plans
that exist: a plan that allows all chat models includes them at once; a plan
that lists models does not.

The executable first-run check is `scripts/e2e/fresh_instance.sh`. It uses only
HTTP APIs:

1. `GET /setup/` confirms the local-owner form exists.
2. `POST /setup/local-owner` creates the first owner and browser session.
3. `GET /admin/api/session` reads the signed-in actor and CSRF token.
4. `POST /setup/first-app` mints the first `opk_...` runtime key.
5. `GET /admin/api/operator/service-registry` reads the registry `ETag`.
6. `POST /admin/api/operator/services` registers the fake OpenAI-compatible
   engine as a raw record (`protocol: openai-compatible`) with `If-Match`,
   `health_endpoint: /v1/models`, and a model mapping.
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
  `AZIZAM_ADMIN_SESSION_KEY`) when you want to externalize the browser
  session secret. If omitted, the gateway creates a persistent
  `data/admin_session_key` file next to `admin.db`,
- optional bootstrap `admin.oidc` issuer/client/trust configuration when you
  want OIDC before the first local owner login. Otherwise configure OIDC from
  the web app after setup,
- service/provider seed or provisioning input, model directories, and
  provider secret references,
- optional `admin.user_defaults` or user-access env flags as a one-time
  bootstrap seed for self-service defaults and identity/Profile mappings.

Everything that can change after boot belongs in `admin.db` and the dashboard.
The DB/UI service registry is the product source of truth for providers and
services. `proxy_config.yml` can seed or provision records, but it is not the
normal live operator surface. After the one-time seed, every non-managed
registry service can be edited, enabled or disabled, and removed in the web app
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

`/setup/` shows one primary action: sign in with OIDC.

The first allowed OIDC login becomes `owner` when no owner/admin exists yet.
After the callback, the browser lands back on `/setup/`, shows the signed-in
account, and can mint the first key for the user's Personal App.

### No OIDC, No Owner

`/setup/` shows a short local owner form: email, password, confirmation. That
creates a local `owner` user, stores a password verifier in `admin.db`, issues
the browser admin session, and redirects back to `/setup/`.

The host-side `generate-admin-key owner-cli` command remains in a recovery
disclosure on the page. It is not the happy path.

An `opk_...` key is a runtime App key and is rejected here with a clear error.
It should be used in OpenAI/Ollama/Anthropic clients, not to administer the
gateway.

### Signed-In Owner/Admin

`/setup/` shows:

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

`/setup/` remains a thin handoff page:

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

Admin keys are never runtime keys. Use `oak_...` only for
`/admin/api/*`, `/status/raw`, `/v1/status`, or `/setup/claim`.

## What Lives Where After First Run

| Concern                                             | Edited via       | Restart? |
|-----------------------------------------------------|------------------|----------|
| Listener, TLS, session signing key                  | `proxy_config.yml` / env / generated `data/admin_session_key` | yes for explicit changes, with `scripts/safe-restart` |
| OIDC provider settings                              | the web app (optional bootstrap fallback in `proxy_config.yml`) | no |
| Service/provider registry                           | the web app; `source` (`ui`, `provisioned`, `builtin`, `discovered`) is provenance | no for non-managed records |
| Provider seed/provisioning input, model directories, secret references | `proxy_config.yml` / env / Docker secrets | yes |
| Owner/admin roles, Apps, Profiles, App API keys     | `/setup/`, `/apps/`, `/people/`           | no  |
| Identity → Profile mappings, role/model bindings    | the web app                                 | no  |
| Admission caps, throttles, runtime routing toggles  | the web app                                 | no  |
| Provider/service enablement                         | the web app for every non-managed registry record; LiteLLM remains a managed substrate | no  |
| Service-registry export                             | Authenticated `GET /admin/api/config/export`; read-only download of service records | no |

First-run does not require the shipped service-registry export. Full
provisioning bundles, restore, and authoritative-file reconciliation remain out
of scope. Default Profiles and user-access defaults are seeded into `admin.db`
on startup; once seeded, dashboard state is authoritative.

## What Not To Build Here

Do not turn `/setup/` into a second dashboard or YAML editor:

- no backend-specific wizards,
- no Profile picker before the first App key,
- no duplicate policy editor,
- no separate "setup mode" flag.

The state is derived from durable facts: owner/admin roles, OIDC availability,
browser session validity, active App keys, and cached gateway/backend health.

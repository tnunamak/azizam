# Connect surface, UI at the root, outgoing mail

Status: design revision 1, 2026-10-11. **Not approved for implementation (P20).**
Revision 1 resolves the independent red-team of the first draft (`bddd2489`).
Baseline: `origin/release/r18` at `8a3147f0`. All `file:line` references are at
that commit unless a line says otherwise. This document proposes behavior. It
changes no product code.

## Inputs and reading rules

Owner's words (2026-10-11): "Also the UI doesn't reflect that there is an mcp
etc? And do we need /ui or can it move to /?"; "this is unreleased"; on mail:
"whether mail is worth building at all < yes are there no brainer libraries to
outsource complexity to? (libsodium is an example of a no brainer library)"; on
password reset: "Isn't the usual thing to have a self service link?" Later the
same day: the UI move to `/` ships first, on its own.

Research (all in `~/Documents/azizam/ux-findings/research-results/`, read in full):

- **CS**: `azizam-research-connect-surface-20261011-b.md` (18 products, client handoff table).
- **UR**: `azizam-research-ui-at-root-c36a31f-c.md` (route inventory, collisions, 9 products).
- **OM**: `azizam-research-outgoing-mail-20261011.md` (17 products, library evaluation).
- **PR**: `azizam-research-self-hosted-password-reset-20261011.md` (21 products, copy).
- **RT**: `azizam-redteam-connect-root-mail-bddd248-r0-cap6.md`, the red-team of
  the first draft. Its findings are cited as RT-B1, RT-M6 and so on, to keep
  them apart from the owner decisions M1-M3 in §3.12.
- **BL**: `~/Documents/azizam/ux-findings/reports/principles-fix-backlog-2026-10-11.md`, sections D2, D3, D6.
- **UB**: `design/ux-batch1:docs/design/UX_BATCH1.md` §3 (account recovery, draft `ccb42202`).

Governing documents: `docs/design/PRINCIPLES.md` (from `docs/owner-principles`),
`docs/design/VOICE.md`, `docs/design/HEADLESS_AND_RESOURCES.md`, `AGENTS.md`,
and the code-quality canon (`~/code/divan/code-quality/CANONICAL-CODE-QUALITY-THEORY.phase1.md`).

Code evidence for revision 1 came from a read-only sweep of the r18 tree and
from my own reads of every line that a decision depends on. I also listed the
local static export (`web/out`, built by Next 15.5.19, `web/package.json:37`)
to establish the real export shape.

Labels: FACT is in the code or a cited source. DECISION is this design's choice.
UNVERIFIED is not established; implementation must prove it before relying on it.

### Changes in release 18 that this revision uses

- **Try keys are bound to sessions** (commit `936e0794`). `credentials.session_id`
  (`gateway/admin_store.py:166-167`); sign-out revokes them
  (`gateway/accounts.py:170-181`). Password reset does **not** revoke them; see §3.1.
- **The OIDC client secret is in the encrypted store** under ids such as
  `oidc-client-secret-<hex>` (`gateway/admin_runtime.py:1197`). So the store
  already holds a non-provider secret under its provider-named API (§3.7).
- **Public failure projection**: `gateway/public_failure.py:14-17` maps failure
  keys to a layer and one next action. New member-facing errors in this design
  use it.
- **Keep-data store guards**: `gateway/sqlite_startup.py` (`connect` at `:92`,
  `store_startup` at `:181`). New tables in §3.8 are created through it.
- **The global `"reset"` throttle bucket is fixed.** Redemption now throttles
  per link (`gateway/accounts_api.py:200-211`, commit `1f4a7f43`). The first
  draft's "fix it" item is closed.

---

## Build order

Each step ships, deploys and is verified before the next one starts. Each has
its own owner go decision (P20).

| Step | Content | Depends on | Owner action at deploy |
|---|---|---|---|
| **1. UI at the root** (§2) | The web app at `/`, the page registry, the UI route, the deletion of the old server pages and aliases, receipt deep links into Usage, the revocation fix, and the `safe-restart` publication order. | Nothing else in this document. | Point any reverse-proxy probe at `GET /health` before the deploy (§2.5). No IdP change, no configuration change. |
| 1b. Browser auth under `/auth/` (§2.10) | `/login`, `/logout` and the OIDC callback move to `/auth/`. | Step 1. | Add the new redirect URI to the IdP client before the deploy; remove the old one after. |
| **2. Connect** (§1) | The Connect sheet, recipe registry, one validated public origin, the non-generating connection check, the MCP origin fix. | Step 1 (root paths in recipes and links). | Set the public address in Settings if it is empty. |
| **3. Mail** (§3) | Optional SMTP, the outbox, recovery addresses, self-service reset. | Step 1 (root links). Step 2's public-origin module. UB §3 for recovery screens. | Relay account, From domain with SPF and DKIM. |

Step 1b is independent of steps 2 and 3. It can ship whenever the owner can
change the IdP client.

---

## 1. Connect surface (P21) — build step 2

### 1.1 Current state

- **No surface mentions MCP, the API reference, `llms.txt` or the headless
  guide.** No file in `web/src` contains `mcp`, `llms` or `HEADLESS` (BL PF-D201).
  MCP is discoverable only through `/llms.txt`
  (`gateway/discovery_metadata.py:346-348`) and `docs/LOCAL_CLIENT_RECIPES.md:1492`.
  `/.well-known/azizam` (`proxy.py:18586-18595`, `gateway/discovery_metadata.py:166`)
  lists `openapi`, `llms_txt` and `skill_catalog` as root-relative links
  (`:180-200`), but not `/mcp`.
- **There are two creation paths with different recipes and different base URLs.**
  - Apps & keys: `web/src/app/apps/page.tsx`. `CLIENTS` is at `:54`. The base
    URL is `discovery.recipes.openai.canonical_base_url`, else
    `window.location.origin + "/openai/v1"` (`:167-171`). The reveal step is the
    only place the instructions appear. Saving the key closes them (BL PF-D202).
  - Home: the "Connect an app" card (`web/src/app/home/page.tsx:572-575`),
    recipes in `web/src/app/home/recipes.ts`. It uses `home.api_base_url` =
    `{base}/v1` (`gateway/home_api.py:387`).
  - The two surfaces disagree. Discovery says `canonical_base_url: /openai/v1`
    and `compatibility_base_url: /v1` (`registry/protocol_frontends.yml:159-160`).
- **One-time secret: FACT.** App keys start with `opk_` (`gateway/admin_runtime.py:46`).
  Only `sha256:` hashes are stored (`gateway/runtime_auth.py:70`). The secret is
  returned once (`gateway/user_runtime.py:475`), and later reads strip it
  (`gateway/user_runtime.py:44`). `SecretReveal`
  (`web/src/components/product/secret-reveal.tsx:38`) says "Copy this key now.
  You can't view it again after closing this step."
- **Connection check spends a generation.** `POST /me/api/connection-test`
  (`gateway/home_api.py:457-513`) sends a one-token chat to the alias `fast`
  when it is allowed (`:487-494`), otherwise `GET /v1/models`. It calls the app
  in-process through `httpx.ASGITransport` (`:500-509`). The `fast` path
  violates P12 (BL PF-176b) and silently runs a generation.
- **MCP: FACT.** `POST /mcp` (`gateway/mcp_server.py:610-647`). GET and DELETE
  return 405 with `Allow: POST` (`:648-650`). Auth is the same bearer app key.
  The protected-resource metadata has no `authorization_servers` (`:652-662`).
  The tool list is static: `capabilities`, `run`, `job` (`:141-200`, `:587`).
  So `tools/list` proves authentication and transport, not permission to run
  every tool. Live tests: Claude Code 2.1.286, `pi-mcp-adapter` 4.0.0, MCP
  Python SDK 1.26.0 (`docs/SUPPORT_MATRIX.md:81`).
- **MCP Origin check trusts `Host`.** `gateway/mcp_server.py:125-134` accepts a
  request with no `Origin`, or an `Origin` whose netloc equals
  `X-Forwarded-Host` or `Host`. It compares netloc only, not scheme.
- **Automation keys: FACT.** `POST /admin/api/admin-keys`
  (`gateway/admin_api.py:2280-2325`) accepts sessions (with CSRF) and admin
  bearer keys (`:1150-1194`). Any admin-control role (`owner`, `admin`,
  `superadmin`, `gateway/admin_runtime.py:49`) may mint an `admin` key; only an
  `owner` may mint an `owner` key (`gateway/admin_api.py:2299-2304`,
  `gateway/admin_runtime.py:603-650`). `docs/ops/HEADLESS_ACCESS.md:184`
  documents this. Credentials list (`gateway/admin_api.py:2327-2342`) carries
  `subject_kind` (`service_account` for automation keys) but no role field
  (`gateway/admin_store.py:151-170`). Any admin may revoke any credential,
  including an owner's key (`gateway/admin_api.py:2366-2384`). Host bootstrap:
  `proxy.py generate-admin-key` (`proxy.py:2716`).
- **The public address can come from `Host`.** `_public_base_url`
  (`proxy.py:18528-18548`) returns the saved `base_url` (whose default is
  `AZIZAM_PUBLIC_URL`, `proxy.py:15485-15494`), else `X-Forwarded-Host`/`Host`
  with `X-Forwarded-Proto`. The returned value is not validated. A value saved
  through Settings is validated (`gateway/settings_api.py:301-346`: `http` or
  `https`, a host, no user info, query or fragment, path empty or `/`), but the
  environment default skips that check. Callers: MCP challenge and metadata
  (`gateway/mcp_server.py:607`, `:653`), discovery and `llms.txt`
  (`proxy.py:18590`, `:18603`, `:18630`), the workflows catalog (`:15982`),
  Home (`gateway/home_api.py:377-386`), the setup console (`proxy.py:17299`) and
  the old portal (`:15674`, deleted in step 1).
- **Sessions carry no authentication time.** `AdminSession` has `issued_at`
  (`gateway/admin_oidc.py:863-889`); `session_from_claims` ignores `auth_time`
  (`:925-940`). The OIDC start never sends `max_age` or `prompt=login`
  (`gateway/oidc_browser_flow.py:50-52`).
- **Per-key access data exists but the web app does not use it.**
  `GET /me/api/credentials/{id}/access` (`proxy.py:16186-16223`) returns the
  key's effective catalog. No `web/src` file calls it.
- **`/v1/models` mixes kinds.** It merges LLM rows, workflow rows and alias rows
  (`proxy.py:25480-25512`) and then applies `_filter_model_list_for_profile`
  (`:25522`). There is no single capability field per row.

### 1.2 Decision

**D1.1 One Connect sheet, in Apps & keys, reachable from every key.** Each app
row in `/apps/` gets a **Connect** action. Creating a key continues into the
same sheet instead of a dead-end reveal. The Home card is reduced to one
entry row, "Connect an app", that opens the same sheet. The row shows only
until the person's first app key records a request (P1, P2). The recipe code
lives in one place.
Grounding: CS P0 "One contextual Connect surface" (Supabase and Neon contextual
dialog, LiteLLM key-to-code handoff).

**D1.2 Two first choices.** The sheet asks "What are you connecting?" with two
answers:
- **An app**: chat apps, coding tools, scripts.
- **An AI agent (MCP)**: tools that call Azizam's actions.

The client list under each comes from server data. Protocol words appear only
in the recipe and in Technical details (CS P0; VOICE rule 3).

**D1.3 Recipes are server data, rendered by one core.** Move `CLIENTS`
(`web/src/app/apps/page.tsx:54`) and `web/src/app/home/recipes.ts` into
`registry/connect_recipes.yml`. The server renders them with the configured
address and model names the app may use. Client names are third-party setup
examples, which P12 allows as data.
Parity is narrowed honestly (RT-N3): the rendered client snippets need the
app's model list, so they are session-only. Headless callers get the same
underlying facts from discovery (addresses, MCP transport, auth header form)
plus `GET /v1/models` with their own key. That is the documented headless
path; no new public endpoint is added.

**D1.4 Snippets never contain a secret, and never put one in a process list.**
Every snippet references `AZIZAM_API_KEY` through the client's own secret
mechanism (`${env:…}`, `${input:…}`, `bearer_token_env_var`), an SDK reading
the environment in-process, or a private header file (RT-M3). No snippet
expands the key on a command line (`-H "Authorization: Bearer $AZIZAM_API_KEY"`
or `claude mcp add … -H "…${AZIZAM_API_KEY}"`): the shell puts the value into
the process arguments. The secret appears only in the one-time reveal block,
with "Copy key". Deep links carry configuration only. Base64 is not
encryption (CS "Client handoff mechanisms").

**D1.5 One validated public origin for every surface (RT-M5).** A new module
`gateway/public_origin.py` exposes one function, `configured_public_origin()
-> str | None`. It reads the saved `instance_presentation.base_url` (with its
`AZIZAM_PUBLIC_URL` default) and validates it with the same rules as the
Settings save (`gateway/settings_api.py:301-346`, moved into this module so
both use one validator): `http` or `https`, a host, no user info, no query, no
fragment, no path other than `/`. An invalid value is treated as unset and
logged once. It never reads `Host`, `X-Forwarded-Host` or
`X-Forwarded-Proto`. `_public_base_url` (`proxy.py:18528-18548`) is deleted and
every caller listed in §1.1 uses the new function.

Behavior when no valid origin is configured:

| Surface | Unconfigured behavior |
|---|---|
| Connect sheet | `address.state = not_configured`; no URLs, no recipes (§1.5 states). |
| `/.well-known/azizam`, `/.well-known/skills` | Links stay root-relative (they already are, `gateway/discovery_metadata.py:180-200`). Fields that need an absolute URL are omitted. Clients resolve relative links against the URL they fetched. |
| `llms.txt` | Root-relative paths. |
| MCP 401 challenge | `WWW-Authenticate: Bearer` with no `resource_metadata` parameter. |
| MCP protected-resource metadata | 404 `public_address_not_configured`. RFC 9728 requires an absolute `resource`; Azizam has no OAuth, so bearer clients do not need it. |
| MCP Origin check | Only requests with no `Origin`, or an `Origin` in `mcp.allowed_origins`, pass (below). |
| Mail (§3) | Mail cannot be `ready`. |
| Setup console, workflows catalog, Home | Root-relative links. |

`http` is allowed (a household LAN gateway may have no TLS). Connect then
shows one warning row: "This address doesn't use HTTPS. Apps on other devices
send your key unencrypted." Mail requires `https` (§3.7).

**D1.5a The MCP Origin check uses the same origin.** `gateway/mcp_server.py:125-134`
compares the full `Origin` (scheme, host and port, normalized with default
ports) with `configured_public_origin()` and with each entry of
`mcp.allowed_origins`. It no longer reads `Host` or `X-Forwarded-Host`. A
request with no `Origin` still passes (non-browser clients). This closes the
Host-trust issue the first draft deferred. Ingress header sanitation stops
mattering for this check. UNVERIFIED: whether a browser-based MCP client is
used against a gateway without a configured origin; it would now get 403 and
needs `base_url` or `mcp.allowed_origins`.

**D1.6 Checking does not generate, and says what it proves.** "Check
connection" stays an in-process call (`gateway/home_api.py:500-509`); the key
is never sent to the configured external URL (RT-M5). The `api` check calls
`GET /v1/models`. The `mcp` check calls `initialize` and `tools/list`. Neither
generates. Remove the `fast` chat path (P12, BL PF-176b). The result copy says
it proves the key works on this gateway, not that apps can reach `{base}`
(§1.5). The check is available while the new secret is still in memory.
Afterwards the sheet shows the key's observed use (`last_used_at`) instead of
asking for the key again.

**D1.7 Recipes carry an honest support tier.** Each recipe has `tested:
{client, version, date}` or no `tested` field. Deep-link buttons appear only on
tested recipes. Every client-format claim in §1.4 stays UNVERIFIED until the
client matrix (§1.7) runs for that exact client version; no recipe without
`tested` claims support. Claude Desktop and claude.ai custom connectors: the
sheet says they are not supported yet. The reason (they need OAuth) comes from
CS "CRITICAL FACT" and is UNVERIFIED by a test here.

**D1.8 Gateway automation: list, revoke and guide only, in this step (C1
changed).** Shown to admin-control roles (`owner`, `admin`, `superadmin`), the
same audience as the rest of the Gateway section (§2.2 D2.6). It lists
automation keys (`subject_kind = service_account`) with revoke, and links the
API reference (`/docs`), `/openapi.json` and the headless guide. It shows the
host command for creating a key. Browser creation of automation keys is
**not** in this step. Reason: a browser-minted automation key outlives the
session, so creation needs a trustworthy fresh-authentication step (RT-M1).
Nothing else in this design needs OIDC step-up (§3 uses password re-entry for
local accounts only). Building step-up for one convenience is not justified
yet. §1.9 C1 keeps the option open with its prerequisites.

**D1.9 The automation-key API contract is unchanged and now written down
(RT-M2).** This design does not tighten any existing route. The matrix below is
the current behavior (FACT, citations in §1.1); tests pin it.

| Caller role | Credential | Requested role | `POST /admin/api/admin-keys` |
|---|---|---|---|
| owner | session (CSRF) or owner/admin key | `admin` or `owner` | 201, one-time `api_key` |
| admin or superadmin | session (CSRF) or admin key | `admin` | 201 |
| admin or superadmin | any | `owner` | 403 `admin_role_forbidden` |
| member (no admin role) | session | any | 403 `admin_forbidden` |
| any | app key (`opk_`) | any | 403 `admin_forbidden` |
| none | none | any | 401 |

`GET /admin/api/credentials` and `POST /admin/api/credentials/{id}/revoke`
accept the same admin-control callers. Known and unchanged: an admin can
revoke an owner's automation key. The UI lists only `service_account` keys and
promises nothing broader. Narrowing revocation is a separate decision, recorded
in §1.8.

**Rejected alternatives:**
- A separate top-level "Connect" page. It splits keys from their instructions
  and needs a third creation path.
- Secret-bearing deep links, config downloads or a key pre-filled into
  snippets. These enter client history, logs and screenshots (CS "P0 One-time
  secret").
- Re-revealing keys, as Supabase, Stripe test mode and Open WebUI do. Azizam
  keeps its stricter one-time contract (CS "Source disagreements" 1).
- Advertising Claude Desktop or web connectors through an "OAuth" button. No
  authorization server exists.
- A generating test request on a green badge. It costs money (CS "P2").
- Keeping recipes in TypeScript. That keeps two copies.
- Checking a new key against the configured external URL. It sends a fresh
  secret over the network only to test reachability.
- "Recent sign-in" based on the cookie's issue time. After silent IdP single
  sign-on a new cookie does not prove the person re-authenticated (RT-M1).

### 1.3 API, CLI and MCP shape first

**Member, session (new):** `GET /me/api/apps/{app_id}/connect`. Same session
and CSRF rules as other `/me/api` routes. 404 `app_not_found` for an app the
caller does not own. A bearer key gets the existing 401 `session_required`.

```text
200 {
  app: {id, name},
  address: {state: "ready", https: bool} | {state: "not_configured"},
  urls: {                         // present only when address.state == ready
    openai_base: "{base}/v1",     // owner decision C2
    anthropic_base: "{base}/anthropic",
    mcp: "{base}/mcp",
    api_reference: "{base}/docs",
    llms_txt: "{base}/llms.txt"
  },
  key: {prefix, created_at, last_used_at|null} | null,   // the key the models come from
  models: {chat: [name], other_count: n},
  recipes: [{
    id, label, kind: "app"|"agent",
    summary,
    fields: [{label, value, copy: true}],
    files: [{path_hint, language, content}],
    commands: [{language, content}],
    deep_link?: {label, url},           // only when tested
    docs_url?, tested?: {client, version, date}
  }],
  keys: {active: n, last_used_at: epoch|null}
}
```

- **Models come from one key's effective catalog (RT-M4).** The server uses the
  app's newest active key: the one just created, or the newest one for an
  existing app. It reads that key's catalog through the same core function as
  `GET /me/api/credentials/{id}/access` (`proxy.py:16186-16223`). `models.chat`
  lists names whose declared capability is chat. UNVERIFIED: that this catalog
  carries a chat capability per entry; if it does not, the implementer adds
  the capability from the same declared data the Models page uses, never from
  a name or alias (P12). Only chat models get a code example (§1.4).
- **Rendering is typed, not text formatting (RT-N3).** Recipe files in the YAML
  are data trees, not code strings, for JSON and TOML. Only string leaves may
  contain the tokens `{{base}}`, `{{model}}` and `{{name}}`. The server
  replaces tokens in leaves, then serializes with `json.dumps` or a TOML
  string writer. Shell, Python and JavaScript snippets are text templates, and
  a token may be replaced only by a value that matches a strict character set:
  the validated origin; a model name matching `[A-Za-z0-9._:/@+-]{1,200}`; an
  app name is never put in code. A value outside the set drops that snippet
  and keeps the fields. No escaping is attempted. Deep-link URLs are built with
  `urllib.parse` from the serialized object.
- `address` is `not_configured` when `configured_public_origin()` is `None`.

**Member, session (changed):** `POST /me/api/connection-test`.
Body `{api_key, kind: "api"|"mcp"}`. In-process only. The response is
`{ok, layer, message, checked: "models"|"mcp_tools", tool_count?, model_count?, request_id}`.
`layer` is one of `key`, `plan`, `gateway`, `mcp`, `unknown` (P3), mapped
through `gateway/public_failure.py`. The key must belong to the caller (the
current rule).

**Public discovery (changed):** `GET /.well-known/azizam` gains:
- `links.mcp = "/mcp"`, `links.api_reference = "/docs"`,
  `links.oauth_protected_resource = "/.well-known/oauth-protected-resource/mcp"`.
- `recipes.mcp = {path: "/mcp", transport: "streamable-http", auth_header: "Authorization: Bearer <app key>", oauth: false}`.
- Drop `links.user_portal_alias` (`/me`, deleted in step 1) (`gateway/discovery_metadata.py:184`).

**Owner and admin, automation keys:** existing routes only (D1.9). The UI uses
`GET /admin/api/credentials` filtered client-side to `subject_kind =
service_account`, and `POST /admin/api/credentials/{id}/revoke`. CLI: the same
OpenAPI operations through Restish (`docs/ops/HEADLESS_ACCESS.md:72`).
`generate-admin-key` stays the host bootstrap.

**MCP:** no new tools (HEADLESS_AND_RESOURCES "MCP keeps `capabilities`,
`run`, `job`"). Connecting is configuration, not a tool.

### 1.4 Recipes (exact content)

Tokens: `{{base}}` is the configured origin, `{{model}}` is the first entry of
`models.chat`, `{{name}}` is the app name (fields only, never code).

**API: base URL and key.** Fields: "Address" = `{{base}}/v1`, "Model name" =
`{{model}}` (absent when there is no chat model), "Key" = "The key you saved".

Check the key from a terminal. The key goes into a file only you can read,
then curl reads the header from that file (the pattern in
`docs/ops/HEADLESS_ACCESS.md:24-58`):

```bash
# Once: save the key in a header file only you can read.
# printf is a shell builtin, so the key does not appear in the process list.
umask 077
printf 'Authorization: Bearer %s\n' "$AZIZAM_API_KEY" > ~/.azizam-header
# List the models this key can use (does not generate)
curl -sS -H @"$HOME/.azizam-header" {{base}}/v1/models
```

Copy under it: "Don't turn on shell tracing (`set -x`) or curl's verbose mode
with this file; they print the key."

When `models.chat` is not empty:

```python
import os
from openai import OpenAI
client = OpenAI(base_url="{{base}}/v1", api_key=os.environ["AZIZAM_API_KEY"])
reply = client.chat.completions.create(
    model="{{model}}", messages=[{"role": "user", "content": "Hello"}])
print(reply.choices[0].message.content)
```

```javascript
import OpenAI from "openai";
const client = new OpenAI({ baseURL: "{{base}}/v1", apiKey: process.env.AZIZAM_API_KEY });
const reply = await client.chat.completions.create({ model: "{{model}}", messages: [{ role: "user", content: "Hello" }] });
console.log(reply.choices[0].message.content);
```

When `models.chat` is empty, the Python and JavaScript snippets list models
instead (`client.models.list()`), and the card says "This app's plan has no
chat models. The address and key still work for its other models." No image,
audio or embedding model is ever put into a chat call.

The existing app recipes (Open WebUI, LibreChat, SillyTavern, Cursor models,
Codex, Claude Code models, phone) move unchanged in meaning into the YAML. One
proposed change: the Claude Code model recipe sets
`ANTHROPIC_BASE_URL={{base}}/anthropic`, not the bare origin
(`web/src/app/home/recipes.ts:120-125`). UNVERIFIED: that Claude Code accepts
the `/anthropic` prefix. Until the client matrix proves it, keep the origin
form and record the reason in the YAML.

**MCP: Claude Code** (tested 2.1.286, `docs/SUPPORT_MATRIX.md:81`), project
file `.mcp.json`:

```json
{"mcpServers": {"azizam": {"type": "http", "url": "{{base}}/mcp",
  "headers": {"Authorization": "Bearer ${AZIZAM_API_KEY}"}}}}
```

Summary: "Add this file to your project, set AZIZAM_API_KEY, then start Claude Code."
UNVERIFIED: that Claude Code expands `${AZIZAM_API_KEY}` in `.mcp.json` headers
for the tested version; the client matrix must prove it before the recipe gets
`tested`. Do not offer `claude mcp add … -H "Authorization: Bearer ${AZIZAM_API_KEY}"`
in double quotes, as `docs/LOCAL_CLIENT_RECIPES.md:1522-1523` does: the shell
expands the secret into the process arguments and the stored configuration.
Fix that doc in the same change.

**MCP: Cursor** (untested). `~/.cursor/mcp.json`:

```json
{"mcpServers": {"azizam": {"url": "{{base}}/mcp",
  "headers": {"Authorization": "Bearer ${env:AZIZAM_API_KEY}"}}}}
```

Deep link only after testing:
`cursor://anysphere.cursor-deeplink/mcp/install?name=azizam&config={base64(json of the inner server object)}`
(CS "Cursor deep link").

**MCP: VS Code** (untested). `.vscode/mcp.json`:

```json
{"inputs": [{"type": "promptString", "id": "azizam-key", "description": "Azizam app key", "password": true}],
 "servers": {"azizam": {"type": "http", "url": "{{base}}/mcp",
   "headers": {"Authorization": "Bearer ${input:azizam-key}"}}}}
```

No deep link until tested. UNVERIFIED: whether a `vscode:mcp/install` link can
carry `inputs` (CS "VS Code config").

**MCP: Codex** (untested). `~/.codex/config.toml`:

```toml
[mcp_servers.azizam]
url = "{{base}}/mcp"
bearer_token_env_var = "AZIZAM_API_KEY"
```

UNVERIFIED: the Codex version that supports streamable HTTP with
`bearer_token_env_var`. Ships only after a client-matrix pass.

**MCP: Claude Desktop and claude.ai connectors.** No recipe. Copy: "Claude
Desktop and claude.ai connectors need a sign-in method this gateway doesn't
offer yet. Use Claude Code, Cursor or another app that accepts a key."

**MCP: Other client.** Fields:
- "Server address" = `{{base}}/mcp`
- "Transport" = "Streamable HTTP"
- "Header" = `Authorization: Bearer <your key>`

Link: "MCP setup guide" (`/llms.txt`).

### 1.5 Screens and exact copy

The sheet is phone-first. At 390 px the first viewport shows the step title,
the choice or the recipe, and its primary action (P4, P5). Stack sections
vertically. Code blocks wrap; they never scroll horizontally on a phone.

**Create flow (Apps & keys → "New app"):**
1. Dialog title "New app". Field "App name". Action "Create app and key". Busy
   "Creating…". Remove the internal "App recipe" select (BL PF-D202).
2. Reveal step, same sheet. Title "Copy your key". The `SecretReveal` text
   stays. Action "Copy key". Acknowledgement "I've saved it". This advances to
   step 3 and does not close the sheet.
3. Title "Connect {name}". Choices: "An app" with "Chat apps, coding tools and
   scripts", and "An AI agent (MCP)" with "Lets an agent use this gateway's
   actions".
4. Client list (from data), then the recipe card: summary sentence, fields with
   copy buttons, code block, deep-link button if any, "Check connection".
5. Technical details (collapsed): "OpenAI-compatible address",
   "Anthropic-compatible address", "MCP server", "API reference", "Guide for AI
   agents (llms.txt)", "Models from the key ending {prefix}", and "Tested with
   {client} {version} on {date}" (only when tested).

**Existing app → row action "Connect":** opens at step 3. In place of the
secret it shows: "Use the key you saved when you created it. Lost it? Create a
new key." The link calls the existing mint flow.

**Gateway automation** (admin-control roles, below the app list):
- Heading "Gateway automation". Body: "Automation keys can change this gateway's
  settings, people and engines. Use an app key for apps and agents."
- List rows: name, "Last used {time}" or "Not used yet", action "Revoke"
  (confirm: "Revoke {name}? Scripts that use it stop working.").
- "Create an automation key on the gateway's computer:" then the command
  `./venv/bin/python proxy.py generate-admin-key {name} admin`, with a link to
  the headless guide. No create button in this step (D1.8).
- Links: "API reference" (`/docs`), "OpenAPI document" (`/openapi.json`),
  "Headless guide" (from discovery `repository_docs`; absent if discovery has
  no URL).

**States:**

| State | Copy and action |
|---|---|
| Loading Connect data | Skeleton for the recipe card. No "no access" text while pending (CS "Required state model"). |
| Connect data failed | "The gateway couldn't load connection details." / "Try again" (layer: gateway). |
| Address not set, member | "This gateway's public address isn't set, so apps can't reach it yet. Ask the owner to set it." |
| Address not set, admin role | "Set this gateway's public address so apps on other devices can reach it." / "Open Settings" (deep link to the field). |
| Address is `http` | "This address doesn't use HTTPS. Apps on other devices send your key unencrypted." |
| No chat models | "This app's plan has no chat models. The address and key still work for its other models." |
| No models at all | "This app can't use any models yet. Ask the owner to update its access plan." Admin role: "Update its access plan" opens People → Plans. |
| Key creation not allowed | The server reason when given. Otherwise: "You can't create keys for this app. Ask the owner to update its access." (BL PF-033). |
| Copied | "Copied" for 2 s on the button; screen readers get a polite status. |
| Copy failed | "Couldn't copy. Select the text to copy it." The text stays selectable. |
| Deep link opened | "Finish in {client}. Opening the client doesn't connect it; check the connection after." |
| Check: working | "This key works on this gateway. It can use {n} models." (API) / "This key works on this gateway. {n} tools are available." (MCP). Then: "Apps also need to reach {base} from their device." Show the check time. |
| Check: 401 | "The gateway didn't accept this key. Check that you pasted the whole key." (layer: key) |
| Check: no allowed models | "The key works, but its plan allows no models. Ask the owner to update its access plan." (layer: plan) |
| Check: gateway error | "The gateway couldn't run the check. Try again." (layer: gateway) |
| After secret is gone | "Not used yet. Use the key in {client}; this updates after its first request." / "Last used {time}". No Check button without a key. |
| Claude Desktop / web | The unsupported copy in §1.4. |

The first draft's "Check: unreachable" and "403 origin" rows are removed: the
check is in-process and sends no `Origin`.

Delete these strings: the Home card blurb "For chat apps, phone apps and coding
tools. Each app gets its own key." (`web/src/app/home/page.tsx:575`), "App
recipe", "Gateway responded with this key", and "Create access for an existing
app; its key appears once." (`web/src/app/apps/page.tsx`).

### 1.6 Data and migration

- New file `registry/connect_recipes.yml`, validated at boot against a small
  schema: `id`, `label`, `kind`, `summary`, `fields`, `files` (with `language`
  and a data tree or text), `commands`, `deep_link`, `docs_url`, `tested`. A
  bad row fails boot, as other registry files do.
- Delete `web/src/app/home/recipes.ts`, the `CLIENTS` constant, and the Home
  card's recipe code. Delete `/me/api/home` fields that only that card used
  (`api_base_url`, `new_app_aliases`). UNVERIFIED: other readers; find them
  with LSP references before deleting.
- New module `gateway/public_origin.py` (D1.5). Delete `_public_base_url`.
- Discovery: `recipes.openai.canonical_base_url` per decision C2. Keep both
  `/v1` and `/openai/v1` served.
- No database change.

### 1.7 Tests

| Level | Oracle |
|---|---|
| Server | With a valid origin, `/me/api/apps/{id}/connect` renders every recipe. Every `files[].content` parses as its language: `json.loads`, `tomllib.loads`, `ast.parse` for Python, `node --check` for JavaScript, `bash -n` for shell. Every deep link decodes to the same server object. No response contains `opk_`, `oak_` or a key-shaped string. |
| Server | A model name or origin outside the allowed character set drops the snippet and keeps the fields; no output contains an unescaped quote from input. |
| Server | Origin resolution: unset, invalid saved value, invalid environment default (`https://u:p@x/`, `https://x/path`, `https://x/?q`), forged `Host`/`X-Forwarded-Host`/`X-Forwarded-Proto`. Each surface in the D1.5 table gives its unconfigured behavior; no response contains the forged host. |
| Server | MCP Origin: no `Origin` passes; configured origin passes; same host with another scheme or port fails; `X-Forwarded-Host` equal to a forged `Origin` fails; `mcp.allowed_origins` entry passes. |
| Server | Models: zero models, image-only, embedding-only, chat plus image, and two keys with different plans. `models.chat` equals the chat subset of `GET /v1/models` called with the key named in `key.prefix`. No chat snippet when `models.chat` is empty. |
| Server | Another person's `app_id` → 404. A bearer key → 401. The D1.9 matrix, row by row, with no side effects on denied rows (these pin current behavior). |
| Server | `connection-test` never calls a generation route: the dispatch target set is `{GET /v1/models}` for `api` and `{initialize, tools/list}` for `mcp`, all in-process. |
| Server | `/.well-known/azizam` contains `links.mcp` and `recipes.mcp.oauth == false`. Route inventory (`tests/test_headless_access.py`) and `gateway/owner_openapi_routes.json` include the new member route. |
| Component | Create → copy → "I've saved it" → reopen, reload, Back/Forward, second tab: no secret anywhere in the DOM, storage or the URL. Each §1.5 state row renders. |
| Browser, 390 px | Member: New app → copy key → An AI agent → Claude Code → copy file → Check connection. Admin: the same, plus Gateway automation list and revoke. Screenshots for both (P4, P8). |
| Client matrix (gate per recipe) | On a disposable gateway, each MCP recipe completes `initialize` and `tools/list`, and each app recipe completes `GET /v1/models`, in the named client version, with the key supplied only through the recipe's own mechanism. Only then does the recipe get `tested` and its deep link. Never against the live gateway. |

### 1.8 Risks

- **Clients change their configuration formats.** Recipes are data with a
  tested date; no deep link until tested.
- **Owners pasting automation keys into agents.** The separate section and copy
  reduce this. They cannot prevent it.
- **An admin can revoke an owner's automation key** (current behavior,
  `gateway/admin_api.py:2366-2384`). Not changed here. If the owner wants
  owner keys revocable only by owners, that is a separate API change with its
  own test.
- **`http` origins.** Allowed with a warning; mail refuses them.

### 1.9 Owner decisions

1. **C1 Create automation keys in the browser?** Recommended default changed to
   **not in this step**. If the owner wants it later, the prerequisites are:
   (a) a fresh-authentication contract: local accounts re-enter the password;
   OIDC accounts complete an authorization request with `prompt=login` and
   `max_age=0`, and the callback accepts it only if the signed ID token's
   `auth_time` is present and not earlier than the request's start; the result
   is a single-use, five-minute record bound to the session id and the action;
   (b) service-account sessions from setup claim cannot step up and must use
   bearer; (c) fail closed when the IdP does not return `auth_time`
   (UNVERIFIED for the owner's IdP); (d) the D1.9 bearer contract stays as is.
2. **C2 Which OpenAI-compatible address do snippets show?** Recommended
   default: **`{base}/v1`**. Set discovery `canonical_base_url` to `/v1`.
   `/openai/v1` keeps working.
3. **C3 Claude Desktop and claude.ai connectors (OAuth).** Recommended
   default: **defer**. An OAuth authorization server is a separate design.

---

## 2. UI at the root — build step 1 (build-ready)

This section is self-contained. An implementer can build step 1 from it alone.

### 2.0 Scope of step 1

**In:** the web app served at `/`; a committed page registry; one UI route
before the unchanged catch-all; deletion of the `/ui` mount and of the old
server-rendered pages and browser aliases listed in D2.5; receipt deep links
into Usage; the session-revocation fix; the admin page guard keyed to the
registry; `safe-restart` publishing the export only immediately before the
restart.

**Out (later steps):** moving `/login`, `/logout` and the OIDC callback under
`/auth/` (step 1b, §2.10, needs an IdP change); Connect (§1); mail (§3);
converting the setup form posts to a JSON API (not planned, D2.5).

### 2.1 Current state (r18)

- **Mount and order: FACT.**
  - The UI is mounted at `/ui` by `mount_web_ui` (`proxy.py:13236-13251`,
    `UIStaticFiles(html=True, check_dir=False)`), called at `:13276`.
  - Middleware `_guard_admin_static_ui` (`:13214-13233`) guards `/ui/<owner
    page>` and `/ui/admin/*`. Middleware `_web_ui_trailing_slash`
    (`:13253-13266`) adds a relative 308 slash for `/ui/<page>`. `GET /ui`
    redirects to `/ui/` (`:13269-13274`).
  - The final route is the catch-all `unmatched_route`, `/{path:path}`
    (`proxy.py:30104-30142`). Methods GET, POST, PUT, DELETE, PATCH and OPTIONS,
    not HEAD. It authenticates first, then answers JSON 404 `not_found`. It never
    dispatches.
  - FastAPI `APIRoute` does not add HEAD to GET routes. In the route inventory
    (`tests/fixtures/headless_routes.json` plus
    `tests/fixtures/headless_non_control_routes.json`, 427 rows) only five paths
    accept HEAD: `/`, `/admin`, `/openapi.json`, `/api/blobs/{digest}` and
    `/ollama/api/blobs/{digest}`. So today `HEAD /v1/models` gets 405 with
    `Allow: GET` from the partially matching route, and `HEAD /anything-else`
    gets 405 with the catch-all's `Allow` list.
- **Owner-page guard.** `OWNER_UI_PAGES = {engines, providers, people,
  activity, settings}` (`gateway/browser_routes.py:20-22`). The guard calls
  `ADMIN_AUTH` (`proxy.py:13218-13228`), which accepts any of `owner`, `admin`,
  `superadmin` (`gateway/admin_runtime.py:49`, `:1517-1533`), by session or
  admin bearer key. The shell gates the same pages with `viewer.admin =
  ["owner", "superadmin", "admin"]` (`web/src/components/shell/session.tsx:42`,
  `:55`) over the "Gateway" group minus Status
  (`web/src/components/shell/app-shell.tsx:657-661`, copy at `:726`: "This
  page is available to gateway owners and admins."). So these pages are
  admin-capable, not owner-only, and `categories` is missing from the server
  set.
- **Revocation.** `_account_session_revocation` (`proxy.py:15618-15640`)
  exempts `/auth/api/*`, five exact login/logout paths, `/ui/sign-in*` and
  `/ui/invite*`. For every other path with a revoked cookie it returns 401
  `session_revoked`, or a 303 to `/ui/sign-in/` when the path has no `/api/`
  and is not `/gateway/*`. It runs before routing and before bearer or
  signed-URL authentication.
- **Every session consumer already checks revocation.** The cookie is decoded in
  only three places: `ADMIN_AUTH` (`gateway/admin_runtime.py:1498-1508`, raises
  "session revoked or account disabled"), `_oidc_session_from_request`
  (`proxy.py:15345-15361`, returns no session) and the revocation middleware.
  `authenticate_request` (`proxy.py:2460`), used for `/v1`, `/gateway/v1`,
  signed media and the catch-all, reads no cookie.
- **Collisions at the root.**
  - `GET /` redirects HTML clients to `/ui/home/` and renders the old user
    portal for everyone else (`proxy.py:25717-25721`). `HEAD /` (`:18506-18509`)
    and `HEAD /admin` (`:18512-18515`) return 200 for proxy probes, as
    `docs/REVERSE_PROXY.md:13-15` recommends.
  - `GET /status`, `/status/` redirect HTML clients to `/ui/status/` and render
    the technical page otherwise (`proxy.py:25735-25740`). `GET /status/raw`
    renders it for admin-control roles (`:25743-25746`, `_render_status_html`
    `:25660-25713`).
  - `GET /setup` (`proxy.py:17323-17340`) sends a signed-in admin to the app,
    redirects to `/ui/setup/` when the export has a setup page, and otherwise
    renders the server setup console; `?operator=1` forces the console.
  - The web setup page posts native HTML forms to `/setup/local-owner`,
    `/setup/local-login` (`web/src/app/setup/page.tsx:77-80`) and
    `/setup/claim` (`:151-153`). Those handlers (`proxy.py:17362`, `:17577`,
    `:17680`, `:17783`) answer errors and the one-time key through
    `_setup_page_response` (`:17249`, 26 call sites) and success with
    redirects (`:17449`, `:17514`, `:17599`, `:17663`).
  - `GET /favicon.ico` returns 204 (`proxy.py:25758-25761`). The export has no
    icon (no `web/public`, no icon in `web/src/app/layout.tsx`).
  - Showcase (`gateway/showcase.py:121-160`): `GET /showcase` → 307 to
    `gallery_ui_url` (`/ui/showcase/`, `proxy.py:14307`); `GET /showcase/` →
    307 to `/showcase`; data routes `/showcase/static/{name}`,
    `/showcase/examples/index.json`, `/showcase/examples/{id}/{name}`,
    `/showcase/api/status`.
  - `GET /r/{id}` renders the old portal (`proxy.py:25724-25732`). Live web
    producers still link to it: `web/src/app/try/receipt-link.tsx:14`,
    `web/src/app/workflows/page.tsx:802`, `gateway/models_catalog.py:1164`.
    Home links to `/ui/usage/?receipt=` (`gateway/home_api.py:414`), but the
    Usage page does not read `receipt` from the query (no reader in
    `web/src/app/usage/page.tsx`).
  - `GET /firehose` → 301 to `/status/raw` (`proxy.py:25749-25755`).
  - HTML aliases: `GET /me`, `/me/` (`proxy.py:15689-15692`); `GET /admin`,
    `/admin/` dashboard (`:16915-16930`, HTML for bearer callers);
    `/admin/<section>` aliases (`:16979-16994`).
  - Browser auth: `GET /login` (`:17895`), `GET /admin/login` (`:17900`),
    `GET /me/login` (`:15643`), `GET /me/logout` and `GET /logout` (both 303 to
    `/auth/logout-confirm`, `:15648-15657`), `POST /logout`, `/me/logout`,
    `/admin/logout` (`:18070-18096`), `GET /auth/logout-confirm`
    (`:18055-18067`, its form posts to `/logout`), `GET /admin/oidc/callback`
    (`:17921`).
- **Next export: FACT.** `web/next.config.ts:6-13` sets `output: "export"`,
  `basePath: "/ui"`, `assetPrefix: "/ui"`, `trailingSlash: true`. The local
  export (Next 15.5.19) contains exactly these file shapes:
  - per page: `<page>/index.html` and `<page>/index.txt` (the client
    navigation payload, fetched as `index.txt?_rsc=…`);
  - root: `index.html`, `index.txt`, `404.html`, `404/index.html`;
  - assets: `_next/static/<build id>/…`, `_next/static/chunks/…`,
    `_next/static/css/…`, `_next/static/media/…`;
  - the build stamp `.azizam-build-complete` (`scripts/safe-restart:78`).
  `web/scripts/remove-legacy-polyfills.mjs:21-27` asserts `/ui/_next/...`.
  `gateway/setup_render.py:325`, `:330` bake hashed font URLs under
  `/ui/_next/static/media/`.
- **Publication order: FACT.** `scripts/safe-restart` builds into a staging
  directory and publishes it with an atomic rename (`publish_web_export`,
  `:113`, called at `:471`) **before** it drains and restarts. If draining
  times out (exit 3, "NOT restarted"), the new export stays published under the
  old process.
- **Cache.** `_is_hashed_ui_asset` (`gateway/ui_static.py:206-208`) marks only
  `/ui/_next/static/<hashed>` immutable. Everything else is `no-cache`.
  `UIStaticFiles.get_response` (`:123-203`) supplies compression, per-encoding
  ETags, `Vary`, 304, Range and bodyless HEAD.
- **Observability and drain.** Firehose skip prefixes include `/ui/` and
  `/_next/`; skip exact paths include `/`, `/me`, `/ui` (`proxy.py:18398-18415`).
  Drain exempts `/ui`, `/admin`, `/login`, `/setup` (`gateway/drain.py:41`) and
  rejects only POST, PUT and PATCH (`:45`). Request source marks UI traffic by a
  same-host referer path `/ui/try` or `/ui/home` (`gateway/request_source.py:53-56`).
- **Security headers.** `_security_headers_middleware` (`proxy.py:14201-14211`)
  adds headers to HTML responses and to `_BROWSER_UI_PREFIXES = ("/admin",
  "/setup", "/login", "/me", "/status-page")` (`:14180`). `/status-page` is
  stale. It adds no `Cache-Control`.
- **OIDC.** The callback is `/admin/oidc/callback`. Its path comes from
  `redirect_uri` in configuration (`gateway/admin_oidc.py:198`; settings key
  list `gateway/admin_runtime.py:65`); its origin is replaced by the public
  origin (`oidc_config_for_public_base`, `gateway/admin_oidc.py:131-137`, used
  at `proxy.py:17033`). Login start redirects to the callback host's `/login`
  when the request host differs (`proxy.py:17043-17046`). The return-path
  sanitizer `safe_browser_return_to` (`gateway/admin_oidc.py:105-128`) allows
  `/ui/*`, `/status/raw`, `/setup`; its client mirror is `safeContinuePath`
  (`web/src/app/sign-in/auth-api.ts:54-67`).
- **Route inventory test.** `tests/test_headless_access.py:33-48` exempts
  exactly `Mount("/ui", UIStaticFiles, name="web_ui")`;
  `tests/fixtures/headless_non_control_routes.json:103` lists `GET /ui`.
- **Single process.** `uvicorn.run(app, …)` with no worker count
  (`proxy.py:30203-30211`).

### 2.2 Decision

**D2.1 The UI lives at `/`.** Home is `/`. Pages are `/library/`, `/try/`,
`/apps/` and so on. There are no redirects, aliases or shims for any `/ui` URL,
bookmark or return path (P11). A browser that opens an old `/ui/…` URL gets
the app's 404 page (D2.2), which links to Home (`web/src/app/not-found.tsx:14`).
External contracts keep their paths and methods: OpenAI, Anthropic, Ollama,
A1111, MCP, signed media URLs, `/gateway/v1`.
Grounding: UR "Recommendation" (six of nine products serve the UI at the root).

**D2.2 A page registry, one UI route, and the catch-all unchanged (RT-M6, RT-M10).**

*Page registry.* New committed data file `gateway/ui_pages.json`:

```json
{"pages": {
  "":            "public",
  "account":     "member", "apps":     "member", "library":   "member",
  "models":      "member", "status":   "member", "try":       "member",
  "usage":       "member", "workflows":"member", "design":    "member",
  "activity":    "admin",  "categories":"admin", "engines":   "admin",
  "people":      "admin",  "providers":"admin",  "settings":  "admin",
  "invite":      "public", "setup":    "public", "showcase":  "public",
  "sign-in":     "public"
}}
```

Access classes: `public` (no session needed), `member` (the shell asks for a
session), `admin` (admin-control roles, server-guarded). `/` is `public`: at
r18 the root page is a signed-out landing that `router.replace("/home/")`s
signed-in viewers (`web/src/app/page.tsx:11-15`). Step 1 merges the two into
one `web/src/app/page.tsx`: the landing's signed-out content for signed-out
viewers, Home for signed-in ones, no client redirect. The registry is the
single source for the page set, the admin guard, the shell's public pages and
admin gating, the return-path sanitizer and request-source tagging. The web
app imports it the way it already imports `gateway/product_identity.json`
(`web/src/lib/product.ts:1`). Two build inputs must gain this file, or the web
build cannot import it: `WEB_BUILD_SHARED_INPUTS` in `scripts/safe-restart:80`,
and the web-build stage of the `Dockerfile` (`:6` copies only
`product_identity.json`). Step 1 deletes `home/`, `gallery/` and `services/` (BL PF-D301);
later steps add `recent/` (UB §2), `sign-in/recover/` (UB §3) and `verify/`
(§3) to the registry with their pages. `design` stays a member page; its
purpose is out of scope.

*Classifier.* New module `gateway/ui_routes.py`, pure functions over the
registry:

```text
classify(path) ->
  Page(name)            # "/" or "/{name}/" for a registry page
  PageSlashless(name)   # "/{name}" for a registry page (name != "")
  PagePayload(name)     # "/index.txt" or "/{name}/index.txt"
  Asset                 # "/_next/static/" + anything
  None                  # everything else
```

Shapes are exact strings over the registry; there is no pattern such as "any
file with a dot". That keeps, for example, `/llms.txt` out of the UI (a
pattern rule would claim it and change `HEAD /llms.txt` from 405 to 404).

*UI route.* `UiRoute`, a Starlette `BaseRoute` in `gateway/ui_routes.py`,
registered immediately before `unmatched_route`. Its `matches(scope)`:
- returns `Match.FULL` for an `http` request whose path classifies as
  `Page`, `PageSlashless`, `PagePayload` or `Asset`, for **every** method;
- returns `Match.FULL` for GET and HEAD with an explicit `text/html` `Accept`
  (`accepts_html`, `gateway/browser_routes.py:31-46`) when the path classifies
  as `None` and its first segment is not a registered first segment (below):
  the HTML 404 case;
- returns `Match.NONE` otherwise. It never returns `Match.PARTIAL`.

What protects API routes is the set of paths it matches fully, not its
position: Starlette takes the first full match even after an earlier partial
match (`starlette/routing.py:731-745`, Starlette 0.50.0). Its full matches are
limited to registry shapes that no registered route matches for any method
(test 2 in §2.8) and to the HTML 404 case on unregistered first segments, so
no registered route's full or partial (405) match can be displaced. Returning
`NONE` instead of `PARTIAL` keeps it out of Starlette's 405 choice. At startup,
boot fails if any registered route other than the catch-all has a parameter as
its first segment (for example `/{token}/v1/…`): the first-segment set must be
literal. WebSocket scopes are never matched.

`classify` returns `None` for any path with an empty, `.` or `..` segment
after percent-decoding, so `/_next/static/../../people/index.txt` cannot reach
an admin payload past the guard (r18 has the same gap through the mount).

*Behavior per class:*

| Class | GET / HEAD | Other methods |
|---|---|---|
| `Page` | Admin guard if the page is `admin` (D2.6). Then `<name>/index.html` through `UIStaticFiles`: 200, `no-cache`, ETag, compression; HEAD bodyless. | 405 JSON `{error:{code:"method_not_allowed"}}`, `Allow: GET, HEAD`. |
| `PageSlashless` | 308 to `/{name}/` with the query kept. Location is a path, never an absolute URL. | 405, `Allow: GET, HEAD`. |
| `PagePayload` | The same guard as its page, then the file, `no-cache`. | 405, `Allow: GET, HEAD`. |
| `Asset` | The file through `UIStaticFiles`, with its cache rule (D2.7). Missing → 404 with no HTML body. | 405, `Allow: GET, HEAD`. |
| HTML 404 case | `404.html` with status 404, `no-cache`; HEAD bodyless. | (not matched) |
| Everything else | Not matched. Registered routes and then the unchanged catch-all answer exactly as at r18. | Same. |

*Registered first segments.* At startup, after every router is included,
`ui_routes` records the first path segment of every registered route except
`UiRoute` and the catch-all, from `app.routes`. No hand-written reserved list.
At r18 this set is: `.well-known`, `admin`, `anthropic`, `api`, `auth`, `docs`,
`favicon.ico`, `gateway`, `health`, `internal`, `llms.txt`, `login`, `logout`,
`mcp`, `me`, `ollama`, `openai`, `openapi.json`, `redoc`, `rerank`, `sdapi`,
`setup`, `showcase`, `status`, `typesafe`, `v1`, `v2` (plus `firehose`, `r`
and `ui`, which step 1 deletes). A path under one of them never gets the HTML
404, so a CLI never receives HTML from an API namespace by mistake.

*Missing build.* Pages come from the registry, not the export. If
`web/out/index.html` is missing, `Page` and `PageSlashless` answer 503
`text/plain`: "The web app isn't built. Run the web build, then restart the
gateway." `PagePayload` and `Asset` answer 404. API boot does not fail
(`tests/test_web_ui_static_mount.py:26` keeps that rule). The HTML 404 case
answers 503 too, with the same text.

*Why not one dispatcher that replaces the catch-all (the first draft).* Adding
HEAD to a final catch-all makes it a full match for `HEAD /v1/models` and
`HEAD /mcp`, which beats the partial match that produces today's 405 (RT-M6).
Emulating 405 inside the dispatcher would copy Starlette's method logic. A
route that is never partial avoids the problem by construction.

**D2.3 Unknown HEAD and other method outcomes are deliberate.** See the matrix
in §2.3. In short: registered routes and the catch-all keep every r18 status
and `Allow` header; `HEAD /` stays 200 (now Home's headers); `HEAD /admin`
changes from 200 to 405; HEAD and GET with `text/html` on unknown, unregistered
first segments get the HTML 404.

**D2.4 Export shape is a contract (RT-M10).** The classifier knows exactly the
shapes in §2.1. A test over a real production build (§2.8 test 4) fails on any
other file shape, so a Next upgrade that adds new sidecar files (for example
per-segment prefetch files) fails CI instead of silently 404ing. Every
registry page must have both `index.html` and `index.txt`.

**D2.5 Old server pages and browser aliases (RT-M8, RT-M11).**

| r18 route | Step 1 |
|---|---|
| `Mount /ui`, `GET /ui`, `_guard_admin_static_ui`, `_web_ui_trailing_slash` (`proxy.py:13214-13277`) | Deleted. `UiRoute` replaces them. |
| `GET /`, `HEAD /` (`:25717`, `:18506`) | Deleted. `UiRoute` serves Home for GET and HEAD. |
| `GET /me`, `/me/` (`:15689`) | Deleted. |
| `GET /admin`, `/admin/` (`:16915`), 16 `/admin/<section>` aliases (`:16979-16994`), `HEAD /admin` (`:18512`) | Deleted. `/admin` becomes API-only except the OIDC callback (step 1b moves it). |
| `GET /status`, `/status/` (`:25735`) | Deleted. `/status/` is a page. |
| `GET /status/raw`, `/status/raw/` (`:25743`) | Kept (R2). Admin-capable (D2.6). Its logout link becomes `/auth/logout-confirm` (`:25693`). |
| Server-page nav links to deleted pages | The shared shell's "Admin" pill to `/admin` (`gateway/gateway_shell.py:459-460`, on by default for `/status/raw`, `firehose_render.py:239`, and set by the console, `gateway/setup_render.py:914`) is deleted; its "Status" pill points to `/status/` (`:462`). The console's "Open admin dashboard" link (`gateway/setup_render.py:655`) becomes "Open Azizam" → `/`. |
| `GET /firehose` (`:25749`) | Deleted. |
| `GET /r/{id}` (`:25724`) | Deleted. Producers link to `/usage/?receipt={request_id}` (D2.5a). |
| `GET /showcase`, `GET /showcase/` (`gateway/showcase.py:121`, `:130`) | Deleted with `gallery_ui_url` (`proxy.py:14307`). `/showcase/` is a page. |
| Showcase data routes (`gateway/showcase.py:134-160`) | **Unchanged.** They do not collide with the page or its payload (test 2). The first draft's move to `/showcase/api/` is dropped as unnecessary. |
| `GET /setup` (`:17323`) | Deleted. `/setup/` is the page. The server console moves to `GET /setup/console`, with the r18 `?operator=1` behavior: no signed-in-admin redirect (`proxy.py:17331-17336`). Its `login_url` becomes `/login?return_to=/setup/` (`:17288`, `gateway/setup_render.py:830`) and its sign-out link `/auth/logout-confirm` (`:17291` passes `/logout`, which the shared shell renders as a GET link, `gateway/gateway_shell.py:475-476`). |
| `POST /setup/claim`, `/setup/local-owner`, `/setup/local-login`, `/setup/first-app` | **Unchanged paths and contract.** They are browser form endpoints: errors and the one-time key come back as server HTML, success as 303. They are in the HTML allowlist of test 5. Only their redirect targets change: `/` and `/setup/`. The first draft's move to `/setup/api/*` is withdrawn (RT-M8): it needed typed JSON contracts for every branch and a rewritten setup client, and nothing in step 1 needs it. |
| `GET /login` (`:17895`) | Kept. Default return `/`. Moves in step 1b. |
| `GET /admin/login` (`:17900`), `GET /me/login` (`:15643`) | Deleted. Producers use `/login`. |
| `POST /logout` (`:18070`) | Kept. Answers `next: "/sign-in/"` or 303 to `/sign-in/`. Moves in step 1b. |
| `POST /me/logout`, `POST /admin/logout` (`:18071-18072`), `GET /me/logout`, `GET /logout` (`:15648-15657`) | Deleted. Producers use `POST /logout` or the link `/auth/logout-confirm`. |
| `GET /auth/logout-confirm` (`:18055`) | Kept. Signed-out redirect `/sign-in/`. Its form still posts `/logout`. |
| `GET /admin/oidc/callback` (`:17921`) | **Kept in step 1** (RT-M11). Moving it needs an IdP redirect-URI change; see §2.10. |
| `GET /favicon.ico` (`:25758`) | Kept: the export has no icon. Fix its stale comment. |

Renderers: delete `gateway/user_render.py` (portal) and
`gateway/admin_render.py` (dashboard) with their handlers. UNVERIFIED: no other
caller; confirm with LSP references before deleting. Keep
`gateway/setup_render.py` (console and form responses) and delete its baked
font URLs (`:325`, `:330`); the console uses the system font stack, so it no
longer depends on hashed export file names.

**D2.5a Receipt deep link.** `/usage/?receipt={request_id}` opens the receipt
sheet. The Usage page reads `receipt` from the query, fetches
`GET /me/api/receipts/{request_id}` (`proxy.py:16016`) and opens the existing
`RequestReceiptSheet` (`web/src/app/usage/page.tsx:17`). States: loading
("Opening receipt…"), not found or not yours ("This receipt isn't available.
It may belong to someone else or be deleted."), failure (layer gateway, "Try
again"). Closing the sheet removes `receipt` from the URL with
`history.replaceState`. UNVERIFIED: that the sheet accepts the
`/me/api/receipts/{id}` shape directly; adapt in the page, not in the API.

**D2.6 Guards and exemptions key on the registry.**

- **Admin page guard (RT-M9).** For `Page(name)` and `PagePayload(name)` with
  access `admin`: call `ADMIN_AUTH`, then check explicitly that
  `context.roles & ADMIN_CONTROL_ROLES` is not empty
  (`gateway/admin_runtime.py:49`). On failure, use `_admin_auth_failure_response`
  (`proxy.py:15309-15333`). The guard runs before any file lookup, ETag or 304
  handling, so a cached ETag cannot bypass it. The pages are admin-capable,
  matching the shell and its copy; nothing at page level is owner-only. Data
  authorization stays in each API route. Expected outcomes:

  | Caller | Admin page or its payload |
  |---|---|
  | Anonymous browser | 302 to `/login?return_to=%2F{name}%2F` |
  | Member session (no admin role) | 302 to `/` |
  | Admin, superadmin or owner session | 200 |
  | Revoked or expired cookie | 302 to `/login?…`; the response clears the cookie |
  | Admin or owner bearer key | 200 |
  | App key bearer | 403 JSON `admin_forbidden` |
  | Member with a valid `If-None-Match` | 302 (never 304) |

  The shell derives `ownerPage` and the Gateway nav group from the registry
  (`access == "admin"`, plus Status as a member page in that group).
  `OWNER_UI_PAGES`, `ADMIN_SECTIONS` and `admin_destination` are deleted from
  `gateway/browser_routes.py`.
- **Session revocation follows the authentication contract (RT-M7).**
  `_account_session_revocation` stops denying requests. Reason: every session
  consumer already rejects a revoked session (§2.1), so session-authorized
  actions still fail closed, while a request authenticated by an app key,
  admin key or signed media URL is no longer broken by a stale cookie the
  browser happens to send. The denial's public-path exemption list disappears:
  sign-in, invite, setup, showcase, assets and the 404 page simply load. The
  signed-media metadata check that needs a maker session keeps working: a
  revoked cookie yields no session there, so it gets the restricted view.
  Cookie clearing stays, narrowed so it cannot undo a sign-in:
  - it applies only to a cookie that decodes and is **revoked** (as at r18,
    `proxy.py:15631-15637`), never to one that fails to decode (another
    gateway on the same host may own it; cookies are not port-scoped);
  - it is skipped when the response already sets a cookie with that name. The
    sign-in responses do (`proxy.py:17451`, `:17515`, `:18033`,
    `gateway/accounts_api.py:138`); a second, deleting `Set-Cookie` after them
    would discard the new session and loop the person back to sign-in;
  - it is skipped for `Asset` responses (immutable, shared-cacheable).
- **A revoked cookie must not hide a bearer key.** `ADMIN_AUTH` raises
  "session revoked" before it looks at the bearer header
  (`gateway/admin_runtime.py:1506-1507`, bearer check at `:1537`). Change: a
  revoked session counts as no session, and the bearer check runs; with no
  bearer the result stays 401. This makes D2.6's "admin bearer key → 200" row
  true with a stale cookie present.
- **Firehose.** The skip rule asks `ui_routes.classify(path) is not None`.
  Delete `/ui/`, `/ui`, `/me` and `/firehose` from the skip lists
  (`proxy.py:18398-18415`).
- **Drain.** `gateway/drain.py:41` replaces the `/ui` prefix with
  `ui_routes.classify(path) is not None`; the other exemptions stay. UI GET and
  HEAD are never rejected, as today.
- **Request source.** UI traffic is a same-host referer whose path is `/` or
  `/try/` (`gateway/request_source.py:53-56`).
- **Security headers and no-store (RT-N2).** `_BROWSER_UI_PREFIXES` becomes
  `("/admin", "/setup", "/login", "/logout", "/me", "/auth")`: the stale
  `/status-page` is removed and `/logout`, `/auth` are added. API responses under
  `/admin` and `/me` keep the headers they get today. HTML responses keep getting the headers through the existing
  content-type rule, which covers pages, the 404 page and `/status/raw`. Every
  response from `/login`, `/logout`, `/auth/logout-confirm`,
  `/admin/oidc/callback` (success and failure) and the setup form posts gets
  `Cache-Control: no-store` and `Referrer-Policy: no-referrer` from one helper.

**D2.7 Cache rules.**
- `/_next/static/` files with a hashed name: `public, max-age=31536000,
  immutable`. Change `_is_hashed_ui_asset` to `/_next/static/`
  (`gateway/ui_static.py:207`).
- Page HTML, page payloads, `404.html`: `no-cache` with ETag.
- Browser auth responses: `no-store` (D2.6).
- Keep `UIStaticFiles`' compression, `Vary: Accept-Encoding`, per-encoding
  ETag, 304, Range and bodyless HEAD. `UiRoute` calls
  `UIStaticFiles.get_response` as a library (it already resolves paths with
  containment); it does not reimplement file serving. Two traps:
  - use an `html=False` instance for `Asset` and `PagePayload`; with
    `html=True` Starlette answers a missing file with `404.html`;
  - `UIStaticFiles` skips its cache headers on non-200 responses
    (`gateway/ui_static.py:125-126`), so `UiRoute` sets `no-cache` on the HTML
    404 and the 503 itself.

**D2.8 Return paths.** `safe_browser_return_to` and `safeContinuePath`:
- allow a path that classifies as `Page`, plus `/status/raw`, with its query
  and fragment;
- reject `//`, backslashes, control characters, schemes and hosts (current
  rules);
- default to `/`;
- drop the `/ui`, `/me`, `/status` and `/setup` mappings and
  `admin_destination`.
The OIDC callback runs the stored `return_to` through the sanitizer again
before it redirects, so an in-flight login that started before the deploy with
`/ui/home/` lands on `/`.

**D2.9 Stale tabs and publication order (RT-M11).**
- `safe-restart` publishes the staged export **after** the drain succeeds and
  immediately before `systemctl restart`. Today the staging tree lives inside a
  `TemporaryDirectory` block that ends before the drain
  (`scripts/safe-restart:446-471`). New lifetime: the staged `out` is moved to
  a sibling directory of `web/` (same filesystem) when the build succeeds, kept
  through the drain, and then:
  - drained, or `--force`: publish, then restart;
  - exit 3 (still busy), exit 4 without `--force`, exit 6, or interrupt:
    delete the staged export; the served one is untouched;
  - restart fails (exit 5) or health is not confirmed (exit 1) after publish:
    keep the previous export beside it (UNVERIFIED whether
    `publish_web_export` already keeps it through its rename; if not, move the
    old tree aside first) and print the one command that swaps it back. The
    operator decides; the script does not guess whether the new process runs. This matters for every release, and most for step 1: under the old
  order, the old process (serving `/ui/_next/…`) would serve new HTML that asks
  for `/_next/…` for the whole drain window, or indefinitely after exit 3.
- After a restart, an open tab of the previous build may request a chunk or
  payload that no longer exists. UNVERIFIED: that Next 15.5's client router
  falls back to a full page load on a payload whose build id differs or on a
  failed payload fetch. Test 13 decides it. If it does not, the root error
  boundary reloads the page once on a chunk-load error (an explicit
  reload-on-version-mismatch, not a compatibility path).
- Tabs open at `/ui/…` during the step-1 deploy cannot recover by themselves;
  their next navigation lands on the 404 page with "Go home". This is the P11
  clean cut, stated in the household notice (§2.4).

**Rejected alternatives:**
- Changing the mount path to `/` in place. It shadows every later API route.
- A root mount after the catch-all. GET never reaches it.
- A generic SPA fallback (`index.html` for any miss). API misses become HTML
  200 (Open WebUI counterexample, UR "Prior art" 1).
- One final dispatcher with HEAD that replaces the catch-all (RT-M6, D2.2).
- A hand-written reserved-namespace list. `app.routes` already knows it.
- A boot-time walk of the export as the classifier input. The registry gives
  the page set even without a build, and the export shape test keeps the two
  in step.
- Moving every private API under `/api`. That path is Ollama's.
- Keeping `/ui` (LiteLLM does). The owner asked for `/`.
- 308 redirects from old `/ui` URLs. P11 forbids them.
- Generalizing the trailing-slash rule to all paths. It would redirect
  `/v1/models` and `/mcp`.

### 2.3 API, CLI and MCP shape first

- **No external contract changes.** OpenAI (`/v1`, `/openai/v1`), Anthropic
  (`/anthropic/v1`, `/v1/messages`), Ollama (`/api`, `/ollama/api`, including
  `HEAD /api/blobs/{digest}`), A1111 (`/sdapi/v1`), MCP (`/mcp`,
  `/.well-known/oauth-protected-resource*`), signed media (Range included),
  realtime WebSockets, SSE streams and `/gateway/v1` (including provider-native
  OPTIONS) keep every path, method, status and `Allow` header.
- **Private APIs** (`/admin/api`, `/me/api`, `/auth/api`, `/setup/api/state`)
  keep their paths.
- **Method matrix after step 1** (r18 behavior unless marked):

  | Request | r18 | Step 1 |
  |---|---|---|
  | `HEAD` on a registered GET-only API path (`/v1/models`, `/health`, `/llms.txt`) | 405, the route's `Allow` | unchanged |
  | `HEAD /mcp` | 405 from the first partially matching route (`Allow: POST`) | unchanged |
  | `HEAD /api/blobs/{d}`, `HEAD /openapi.json` | registered HEAD | unchanged |
  | `GET`/`POST` on an unknown path under a registered first segment | catch-all: 401 unauthenticated, else 404 JSON | unchanged |
  | `HEAD` on such a path | 405, catch-all `Allow` | unchanged |
  | `HEAD /` | 200 empty | **200, Home's headers** (503 if no build) |
  | `HEAD /admin` | 200 empty | **405** (catch-all partial); probes move to `GET /health` |
  | `GET`/`HEAD` page, slashless page, payload, `/_next/static/…` | `/ui/…` only | **served at the root** (D2.2 table) |
  | Other methods on those paths | catch-all 401/404 | **405 JSON, `Allow: GET, HEAD`** |
  | `GET`/`HEAD` with `Accept: text/html`, unknown path, unregistered first segment (for example `/ui/home/`) | catch-all 401/404 JSON (or `/ui` mount) | **404 HTML page** |
  | Same without `text/html` (curl's `*/*`, API clients) | catch-all | unchanged |

- **Changed browser endpoints:** only the deletions in D2.5 and
  `GET /setup/console`. They are updated in the headless route inventory and
  `docs/ops/HEADLESS_ACCESS.md`.
- **Invite and reset link producers** return root paths:
  `/invite/#token=…` and `/sign-in/#reset=…` (`gateway/accounts_api.py:245`,
  `:264`, `:272`).
- **Discovery** keeps `links.login = "/login"` until step 1b and drops
  `links.user_portal_alias` (`gateway/discovery_metadata.py:182-184`).

### 2.4 Data and migration

- **No database schema change. No configuration change.** The OIDC redirect
  URI, the IdP client and the `oidc_provider_config` setting are untouched in
  step 1, so the deploy is one atomic restart.
- **Web build:** remove `basePath` and `assetPrefix` from `web/next.config.ts`
  and update its comments. Replace the two dev rewrites with one fallback
  rewrite that sends every path Next does not serve to the gateway
  (`rewrites: { fallback: [{ source: "/:path*", destination: "http://127.0.0.1:5000/:path*" }] }`,
  dev only, as today).
- **Clean cut (P11).** Old `/ui/...` bookmarks, installed home-screen shortcuts,
  and outstanding invite and reset links that carry `/ui/` stop working.
  - Invites: tokens are derived from the link id (`gateway/accounts.py:462-472`),
    so after the deploy the owner can copy each pending invite again from People
    (`GET /admin/api/invites/{id}/link`, `gateway/accounts_api.py:266`); the
    copy has the root path and the same token. Nothing needs reissuing.
  - Resets: tokens are random (`gateway/accounts.py:254`). The owner reissues
    any pending reset after the deploy.
  - Household notice: "Azizam moved from /ui to the main address. Open
    {base}/ and add it to your home screen again."

### 2.5 Deploy steps (owner-run; this design deploys nothing)

1. **Before the deploy:** check the reverse proxy's health probe. UNVERIFIED:
   whether it uses `HEAD /admin` (`docs/REVERSE_PROXY.md:13-15` suggests it).
   If so, change it to `GET /health` and confirm the gateway still reports up.
   Not `HEAD /`: it answers 503 when the web build is missing, which would mark
   a working API down. `docs/REVERSE_PROXY.md` then recommends `GET /health`
   only. List pending invites and resets
   (`GET /admin/api/invites`, People).
2. Run the full suite, the route tests (§2.8) and the production-build browser
   tests on the release commit.
3. Deploy with `scripts/safe-restart` (it builds, drains, then publishes and
   restarts, per D2.9). This is the only state change: one restart, no
   configuration, IdP or database change.
4. Run the updated `scripts/post-deploy-smoke`: `GET /` → 200 HTML;
   `GET /library` → 308 `/library/`; `GET /_next/static/<a chunk>` → 200
   immutable; `GET /v1/models` without a key → 401 JSON; `HEAD /v1/models` →
   405; `GET /mcp` → 405 `Allow: POST`; `GET /ui/home/` with `text/html` → 404
   HTML; one OIDC sign-in round trip ends on `/`.
5. Re-copy pending invites, reissue pending resets, send the household notice.

**In-flight sign-ins** during the restart: the callback path is unchanged; the
callback re-sanitizes `return_to` (D2.8). UNVERIFIED: whether OIDC state
survives a restart; if it does not, the person sees the sign-in page with
"try again", as on any restart today.

**Rollback:** redeploy the previous release with `scripts/safe-restart`. No
data, configuration or cookie format changed (`CookieConfig.path` is `/`,
`gateway/admin_oidc.py:165`), so sessions stay valid. Links copied after step 1
(root paths) stop working under the old release and must be copied again.

### 2.6 Every producer of a removed browser path, changed directly

Search pattern for `/ui` as a URL (not `components/ui`):
`(^|["'\`(=\s:,])/ui([/"'\`?#)\s]|$)`. At r18, non-test hits:

| File | Lines (r18) | Change |
|---|---|---|
| `proxy.py` | 13203-13277 | Delete mount, `GET /ui`, both middlewares; register `UiRoute` before `unmatched_route` (`:30104`). |
| `proxy.py` | 14307 | Delete `gallery_ui_url`. |
| `proxy.py` | 15295, 15301 | `_safe_admin_return_to` default `/`; `_login_url` unchanged path. |
| `proxy.py` | 15618-15640 | Revocation per D2.6. |
| `proxy.py` | 15643-15657, 15689-15692 | Delete `/me/login`, `GET /me/logout`, `GET /logout`, `/me`. |
| `proxy.py` | 16915-16994, 17900-17907 | Delete `/admin` dashboard, section aliases, `/admin/login`. |
| `proxy.py` | 17016, 17125, 17449, 17599, 17663, 17897, 17913, 17915 | Sign-in fallback `/sign-in/?return_to=…`; defaults `/`; setup redirects `/` and `/setup/`; `_browser_signin_failure` → `/sign-in/?error=provider…`. |
| `proxy.py` | 17043-17046 | Canonical-host redirect keeps `/login` (moves in step 1b). |
| `proxy.py` | 17288-17291, 17323-17340 | Setup console at `GET /setup/console`; its `login_url` and `logout_url` unchanged paths. |
| `proxy.py` | 18059, 18071-18072, 18085-18086 | Logout-confirm redirect `/sign-in/`; delete `/me/logout`, `/admin/logout` POST aliases; logout `next` `/sign-in/`. |
| `proxy.py` | 14180, 14201-14211 | Security-header prefixes and no-store helper (D2.6). |
| `proxy.py` | 18398-18415, 18446 | Firehose skip via classifier. |
| `proxy.py` | 18506-18515, 25717-25761 | Delete `HEAD /`, `HEAD /admin`, `GET /`, `/r/{id}`, `/status`, `/status/`, `/firehose`; keep `/status/raw` (logout link `:25693`) and `/favicon.ico` (comment). |
| `proxy.py` | 25654 | Comment. |
| `gateway/accounts_api.py` | 87, 147, 198, 208, 245, 264, 272 | `oidc_url` → `/login?return_to=/invite/` and `/login?…`; `next: "/"`, `"/sign-in/"`; `/sign-in/#reset=`, `/invite/#token=`. |
| `gateway/admin_oidc.py` | 105-128 | Sanitizer per D2.8. |
| `gateway/browser_routes.py` | 8-22, 49-56 | Delete `ADMIN_SECTIONS`, `OWNER_UI_PAGES`, `admin_destination`. Keep `accepts_html`, `with_query`. |
| `gateway/home_api.py` | 414, 569-572 | Receipt `/usage/?receipt=`; checklist hrefs `/engines/`, `/#connect`, `/people/`. |
| `gateway/models_catalog.py` | 1164 | `/usage/?receipt={id}`. |
| `gateway/discovery_metadata.py` | 182-184 | Drop `user_portal_alias`. |
| `gateway/api_reference.py` | 44 | "Open gateway" → `/`. |
| `gateway/drain.py` | 41 | Per D2.6. |
| `gateway/request_source.py` | 53-56 | Per D2.6. |
| `gateway/ui_static.py` | 207 | `/_next/static/`. |
| `gateway/workflows_catalog.py` | 203 | `try_url` `/try/?workflow=`. |
| `gateway/setup_render.py` | 9, 325, 330, 830 | Drop baked fonts; docstring and default `login_url` unchanged path. |
| `gateway/admin_api.py` | 1782 | Docstring. |
| `gateway/user_render.py`, `gateway/admin_render.py` | whole files | Deleted (D2.5). |
| `scripts/safe-restart` | 80, 113, 471 | Add `gateway/ui_pages.json` to shared inputs; publish after drain (D2.9). |
| `web/next.config.ts` | 3-38 | D2.4 (§2.4). |
| `web/scripts/remove-legacy-polyfills.mjs` | 21-27 | Match `^/_next/static/chunks/polyfills-…`. |
| `web/src/lib/product.ts` | 1-11 | Import the registry; delete `/ui` stripping and the `/services`, `/home` aliases; delete the "Next adds /ui" comment. |
| `web/src/components/shell/app-shell.tsx` | 118-133, 657-661, 715-726 | `publicPages`, return paths and admin gating from the registry; `signInHref` → `/sign-in/?return_to=`. |
| `web/src/components/shell/account-menu-content.tsx` | 52, 58, 132 | `fetch("/logout")` unchanged; return paths without `/ui`. |
| `web/src/app/sign-in/auth-api.ts` | 54-67 | Client sanitizer per D2.8. |
| `/r/` links | `web/src/app/try/receipt-link.tsx:14`, `web/src/app/workflows/page.tsx:802`, `web/src/app/library/page.tsx:1925,2000`, `web/src/app/home/page.tsx:841`, `web/src/app/models/models-client.tsx:913`, `web/src/app/status/status-client.tsx:343` | `/usage/?receipt=`. |
| Links to deleted pages | `web/src/app/page.tsx:15` (`/home/`), `web/src/app/status/status-view.tsx:365` (`/home`) | Removed by the merge; `/`. |
| `Dockerfile` | 6 | Also copy `gateway/ui_pages.json`. |
| `gateway/gateway_shell.py`, `gateway/setup_render.py`, `firehose_render.py` | 459-462; 655, 830, 914; 239 | Per D2.5 "Server-page nav links". |
| `web/src/app/usage/page.tsx` | 324 and new code | `/ui` string; receipt deep link (D2.5a). |
| `web/src/app/setup/page.tsx` | 71 | `/login?return_to=/setup/`. |
| `web/src/app/apps/page.tsx` | 491 | `/login`. |
| `web/src/app/invite/page.tsx` | 139, 161, 328, 343 | Root paths; `/login?return_to=/invite/`. |
| Other `web/src` `/ui` hits | `account/page.tsx:66`, `activity/page.tsx:368`, `activity/requests.tsx:304`, `engines/engine-checks.tsx:338`, `home/page.tsx:1400`, `library/page.tsx:2717`, `models/models-client.tsx:409,429,590,1048`, `showcase/page.tsx:200`, `sign-in/page.tsx:40,156`, `status/status-view.tsx:315`, `try/page.tsx:2475`, `components/product/error-state.tsx:52` | Root paths. Merge `home/page.tsx` into `app/page.tsx` (D2.2, it replaces the landing's redirect); delete `gallery/`, `services/`, `home/`. |
| Scripts | `scripts/post-deploy-smoke:33`, `scripts/security/exposure_probe.py:23`, `scripts/gen_support_matrix.py:37`, `scripts/ui/check-cutover-container.py`, `scripts/ui/craft_numbers.json`, `scripts/research/reddit_*.json` (`/admin/login`) | Root paths. Delete the cutover checker if it only checks the old `/ui` cutover. |
| Docs | `docs/design/UI_CUTOVER.md`, `docs/guides/FOR_MEMBERS.md`, `docs/guides/OPERATOR.md`, `docs/ops/HEADLESS_ACCESS.md` (rows 180-182 and `/ui` mentions), `docs/ops/SAFE_RESTART.md`, `docs/REVERSE_PROXY.md:13-17`, `docs/PEOPLE_AND_ACCOUNTS.md`, `docs/FIRST_RUN_SETUP.md` (`?operator=1` → `/setup/console`), `docs/CORE_CONTRACTS.md`, `docs/CONFIG_SOURCE_OF_TRUTH.md`, `AUTH_GUIDE.md`, `web/README.md`, `firehose_render.py:4` | Root paths, `GET /health` probes. Mark `UI_CUTOVER.md` historical or delete it. |
| Tests | `tests/` and web tests that use `/ui`, `/admin/login`, `/me/logout`, `/r/` | Update expectations. Regenerate `docs/design/craft-system/` data; do not hand-edit it. |

**Completion check (P22, AGENTS rule 5):** after the change, the search
pattern above over `proxy.py gateway firehose_render.py web/src web/scripts
web/next.config.ts scripts docs tests`, plus these two searches over the same
paths and `firehose_render.py`:

```text
rg -n '"/(admin|me)/log(in|out)"|/r/\$\{|"/r/|href="/admin"|"/admin"\)|/firehose|operator=1'
rg -n "[\"'\`(](/home|/gallery|/services)([/\"'\`?#]|\$)"
```

return nothing except an explicit allowlist of historical records. A test runs
all three searches.

### 2.7 Risks

| Risk | Severity | Control |
|---|---|---|
| A page shadows an API route, or an API miss becomes HTML | critical | `UiRoute` is never partial and only fully matches registry shapes; test 2 proves no registered route matches any of them; HTML 404 only for unregistered first segments. |
| An API method/status changes | high | Baseline matrix test (test 1) recorded at r18. |
| An admin page loses its guard | high | Registry-keyed guard on page and payload; test 7 over every admin page and role. |
| Sign-in or setup loops | high | No revocation denial left in middleware; cookie clearing skipped when the response sets a new session; test 8 signs in with a revoked cookie through every sign-in route. |
| UI broken during the deploy window | high | Publish after drain (D2.9); test 14. |
| Reverse proxy marks the gateway down (`HEAD /admin` probe) | medium | Deploy step 1 (§2.5). |
| Next upgrade emits new export files | medium | Export shape test (test 4). |
| Stale tabs after a release | medium | Test 13; one-time reload on chunk errors if needed. |
| Stale links and shortcuts | medium | Invite re-copy, reset reissue, household notice. |
| UI clicks counted as API usage | medium | Firehose classifier test. |

### 2.8 Tests (route manifest and behavior)

1. **Baseline matrix (RT-M6).** Before any code change, a script on r18
   `8a3147f0` records, for every registered route (path parameters filled
   with sample values) and for the bare and one unknown sub-path of each
   registered first segment, the status and `Allow` header for GET, HEAD, POST,
   PUT, PATCH, DELETE and OPTIONS, with and without a valid admin key and an app
   key. Saved as `tests/fixtures/route_method_baseline.json`. After step 1 the
   same matrix must equal the baseline except an explicit, reviewed diff list:
      computed, then reviewed: every (method, path) whose path is deleted by
   D2.5 or now classifies as a UI shape (for example bare `/status`, `/setup`
   and `/showcase`, now 308 from `UiRoute`, and non-GET methods on page paths,
   now 405 `Allow: GET, HEAD`), plus `HEAD /` and `HEAD /admin`. Nothing else
   may differ.
2. **No collision.** For every registry page path, its slashless form, its
   payload path and `/_next/static/x`, and for every method above, every route
   in `app.routes` other than `UiRoute` and `unmatched_route` returns
   `Match.NONE`. No registered route's first segment is `_next`. No `Mount` in
   `app.routes` (replaces `test_only_named_ui_static_mount_is_exempt`,
   `tests/test_headless_access.py:104-110`; the fixture loses `GET /ui`).
   `_registered_routes` (`:33-48`) asserts every route is a `Route` or
   `WebSocketRoute`; it gains one exemption by type for `UiRoute`, as it had for
   the `/ui` mount. Boot fails on a non-literal first segment (D2.2). The
   last two routes are `UiRoute` then `unmatched_route`.
3. **Registry parity.** `ui_pages.json` keys equal the directories of
   `web/src/app/**/page.tsx`. A component test shows the Gateway nav group and
   the shell's owner gate follow the registry, including `categories`.
4. **Export shape (real production build in CI).** Every file under `web/out`
   is one of the shapes in §2.1; every registry page has `index.html` and
   `index.txt`. Any other shape fails with its path.
5. **API errors stay API errors.** For each registered first segment: one known
   path, one unknown sub-path, the bare prefix; all methods; `Accept`
   `text/html`, `application/json`, `*/*`. No `text/html` response except the
   allowlist: `/docs`, `/redoc`, `/status/raw`, `/auth/logout-confirm`,
   `/setup/console` and the four setup form posts.
6. **Pages.** Each page: GET and HEAD → 200, HEAD bodyless with the same
   `content-length`. Slashless → 308 with query, `Location` a path. Other
   methods → 405 JSON `Allow: GET, HEAD`. Payload fetch with `?_rsc=x` → 200.
   Traversal and encoded slashes (`/_next/static/..%2f..%2fproxy.py`,
   `/library/..%2findex.html`) cannot leave the export root, and
   `/_next/static/../../people/index.txt` and
   `/_next/static/%2e%2e/%2e%2e/people/index.txt` are not served as assets. A
   missing asset is 404 with no HTML body.
7. **Admin guard.** The D2.6 table, row by row, for every admin page and its
   payload.
8. **Revocation (RT-M7).** Revoked cookie plus a valid app key on
   `GET /v1/models` → 200 and a cookie deletion. Revoked cookie plus a signed
   media URL with `Range` → 206. Revoked cookie on every session-only and
   session-view route in `tests/fixtures/headless_routes.json` → its normal
   401/403 with no side effect. Revoked cookie plus a valid admin key on an
   `/admin/api` route → 200. Revoked cookie on each public page, a payload and
   the 404 page → 200/404 as for no cookie, plus one deleting `Set-Cookie`;
   on an asset → no `Set-Cookie`. **Sign-in with a revoked cookie present**,
   through the OIDC callback, `POST /auth/api/login`, `/setup/local-login` and
   `/setup/claim`: the response carries exactly one `Set-Cookie` for the
   session name, it is the new session, and the next request is signed in. A
   cookie that fails to decode is never deleted.
9. **Cache.** Hashed `/_next/static` → immutable. HTML and payloads →
   `no-cache`. br/gzip/identity, `Vary`, ETag/304, Range and HEAD as in
   `tests/test_ui_static_serving.py`, with the paths moved.
10. **Return paths.** One fixture table shared by the server and client
    sanitizers: page paths with query and fragment pass; `/status/raw` passes;
    `//evil`, `https://…`, `/v1/models`, `/admin/api/x`, `/ui/home/`,
    `/setup/console` fall back to `/`.
11. **Missing export.** No `web/out` → API boots; pages and the HTML 404 case
    give 503 plain text; payloads and assets 404; the test 1 matrix for API
    paths is unchanged.
12. **Production-build browser test** (isolated gateway on a free port, never
    5000 or 15000): open `/`, use client navigation to every nav destination,
    open a receipt deep link, reload a deep link; no console errors; every
    payload fetch 200; at 390 px.
13. **Stale tab.** Serve build A, open `/library/`, publish build B through
    `publish_web_export` and restart, then click a nav link in the old tab. It
    must end on a working page with no unhandled chunk error.
14. **Publication order.** `safe-restart` with a drain that times out leaves
    `web/out` byte-identical; on success the publish happens after the drain
    and before the restart call.
15. **Browser auth headers.** Every response listed in D2.6 has `no-store` and
    `no-referrer`, including the OIDC callback success.
16. **Protocol contracts.** `POST /mcp` works; `GET`/`DELETE /mcp` → 405
    `Allow: POST`; protected-resource metadata unchanged; Ollama
    `HEAD /api/blobs/{d}` unchanged; realtime WebSockets unchanged; `OPTIONS`
    on `/gateway/v1/providers/{p}/{path}` unchanged; SSE streams incrementally;
    `tests/test_catchall_never_dispatches.py` passes.
17. **Stale strings.** The two searches in §2.6 run in CI.

### 2.9 Owner decisions

1. **R1 Move browser auth to `/auth/`, including the OIDC callback?**
   Recommended default: **yes, as step 1b, not in step 1.** Step 1 keeps
   `/admin/oidc/callback`, `/login` and `POST /logout` so its deploy needs no
   IdP change.
2. **R2 Keep the technical status page `/status/raw`?** Recommended default:
   **keep it, admin-capable** (owner, admin, superadmin; session or admin key),
   which is its current `ADMIN_AUTH` behavior. Delete it when the Status page
   shows its diagnostics; that later change names the replacement.

### 2.10 Step 1b: browser auth under `/auth/` (later)

- Routes: `GET /auth/login`, `POST /auth/logout`, `GET /auth/oidc/callback`.
  `/login`, `/logout`, `/admin/oidc/callback` are deleted. `/admin` becomes
  API-only.
- The callback path becomes a code constant, `/auth/oidc/callback`. The
  callback URL is `configured_public_origin()` plus that path. The
  `redirect_uri` setting and YAML field are deleted (P11). Then the deploy
  changes no live setting, so there is no window in which a running process
  uses a callback it does not serve (RT-M11).
- Procedure: (1) the owner adds `{base}/auth/oidc/callback` to the IdP client's
  allowed redirect URIs, keeping the old one; (2) deploy with `safe-restart`;
  (3) after a sign-in round trip works, the owner removes the old URI.
  Sign-ins started before the restart return to the old callback and get 404;
  the person signs in again. Rollback: redeploy the previous release before
  step (3); both URIs are still allowed.
- Producers: the step-1 table rows for `/login`, `/logout`, the canonical-host
  redirect (`proxy.py:17043-17046`), `links.login` in discovery, the setup
  console, the logout-confirm form action, `account-menu-content.tsx:52`,
  `setup/page.tsx:71`, `apps/page.tsx:491`, `invite/page.tsx:328`,
  `gateway/accounts_api.py:87`, `:147`, the revocation of the OIDC config
  validator's `redirect_uri` rule, `docs/ops/HEADLESS_ACCESS.md`.

---

## 3. Outgoing mail — build step 3

### 3.1 Current state

- **The gateway sends no mail.** There is no SMTP code. The docs say so
  (`README.md:101`, `docs/PEOPLE_AND_ACCOUNTS.md:175-177`).
- **Recovery today.**
  - The sign-in page says "Forgot your password? Ask the person who invited you
    for a reset link." (`web/src/app/sign-in/page.tsx:249`, BL PF-D601).
  - Owner-issued reset: `POST /admin/api/people/{id}/reset`
    (`gateway/accounts_api.py:239-245`). The UI asks for 3600 s
    (`web/src/app/people/people-api.ts:73`).
  - Reset tokens are `secrets.token_urlsafe(32)`, stored as SHA-256 and
    returned once (`gateway/accounts.py:254`). A new reset revokes the user's
    unused older resets in the same transaction (`:256`, `:266`).
  - Redemption (`gateway/accounts.py:357-373`) changes the verifier, sets
    `require_registered_session`, deletes all the person's sessions, marks the
    link used and audits, in one `BEGIN IMMEDIATE` transaction. It takes no
    signing key.
  - **Defect found in this revision:** redemption deletes the person's session
    rows but does not revoke the Try keys bound to those sessions. Sign-out
    does (`gateway/accounts.py:178-180`). So after a reset, a Try key minted in
    a stolen session keeps working until it expires (at most the session
    lifetime, 8 hours by default, `gateway/admin_oidc.py:161`). Fix in step 3
    (or earlier): in the same transaction, revoke credentials whose
    `session_id` belongs to the deleted sessions.
- **Invites.** 7-day default; tokens are HMAC-SHA256 of `gateway.invite.v1:` +
  `link_id` under the session signing key (`gateway/accounts.py:462-472`),
  stored as SHA-256, single use; the owner can re-copy them
  (`:475-484`). Links are relative fragments; the fragment never reaches the
  server, a log or a `Referer`.
- **No address is verified.** `users.email` is nullable and not unique
  (`gateway/admin_store.py:88-97`). Only OIDC sign-ins record `email_verified`
  (`gateway/accounts.py:449-459`).
- **Throttle.** `accounts.throttle` (`gateway/accounts.py:194-207`): 60 per IP
  and 10 per "email" bucket per 15 minutes. Reset redemption now uses a
  per-link bucket (`gateway/accounts_api.py:200-211`). Client IP comes from
  `resolve_real_client_ip` (`proxy.py:2306`).
- **Secret store: FACT.** `gateway/secret_store.py`, AES-256-GCM with a 0600
  key file; table `provider_secrets` (`:66`); `put_provider_secret` (`:83`),
  `get_provider_secret` (`:121`), `delete_provider_secret` (`:141`),
  `provider_secret_revision` (`:163`). `approved_origin` must be an HTTP(S)
  origin and is bound into the AES-GCM associated data (`:234-252`). The OIDC
  client secret is stored without an origin, with a new id per write and the
  old one deleted after (`gateway/admin_runtime.py:1190-1233`).
- **Settings.** `operator_settings` with a generation revision, `If-Match`,
  in-transaction validation and audit (`gateway/admin_store.py:2379-2449`;
  for example `gateway/accounts_api.py:287-307`).
- **Background work.** One uvicorn process (`proxy.py:30203-30211`). Pattern:
  a lifespan task with a stop event (`_prompt_memory_maintenance_loop`,
  `proxy.py:20954-20965`, wired at `:13124-13135`); startup resume of
  unfinished jobs (`gateway/me_make.py:269-293`).
- **Dependencies.** Python 3.12 (`Dockerfile:10`). `email-validator==2.3.0` is
  in the hashed lock only as a transitive dependency of pydantic
  (`tests/requirements-hermetic.lock:584-587`). `aiosmtplib` and `aiosmtpd` are
  absent.

### 3.2 Decision

**D3.1 Mail is an optional capability that the owner turns on.** The gateway is
complete without it. When mail is off, every flow keeps its link-sharing path
(Gitea gates reset on mail; Immich and Home Assistant keep assisted recovery;
OM §5, PR "Recovery entry-point states").

**D3.2 Transport: one SMTP relay, through the standard library.** Deliverability
(DKIM, SPF and DMARC alignment, IP reputation, bounces) is outsourced to a relay
the owner chooses (an ISP, Fastmail, Amazon SES SMTP, Postmark, Resend or
Mailgun all accept SMTP; OM "Provider SDKs versus SMTP"). Azizam hands the relay
a correct message over verified TLS. No direct-to-MX sending and no local MTA
fallback (OM "Disagreements"). No provider SDK.

**D3.3 Library stack (the answer to "no-brainer libraries").** No Python
package clears the libsodium bar for the whole account-recovery lifecycle (OM).
The pieces that do clear it:

| Piece | Choice | Why | Rejected |
|---|---|---|---|
| Message construction | `email.message.EmailMessage` with `email.policy.SMTP` (stdlib) | Ships and is patched with Python. A header value with CR or LF raises, so header injection fails closed. | Hand-built MIME |
| Transport | `smtplib` (stdlib) in the worker thread | No new dependency. `SMTP_SSL` or `starttls()` with `ssl.create_default_context()` verifies certificate and host name; `starttls()` raises when not offered, so no silent downgrade. `send_message` only. Debug level 0. | `aiosmtplib` (single maintainer, opportunistic STARTTLS default, 2026 advisories per OM; UNVERIFIED here); `fastapi-mail` (adds a layer, no lifecycle) |
| Address syntax | `email-validator 2.3.0`, promoted to a direct pinned dependency | The validator behind pydantic's `EmailStr`. Syntax and normalization only; no DNS on any request path. | MX checks per request |
| Message text | Plain text from fixed Python templates over validated values | No template engine, no HTML, no escaping surface. | Jinja2 HTML in v1 |
| Secret at rest | The existing `gateway/secret_store.py` | Reviewed and tested; `cryptography` is the libsodium-level library here. | Plaintext settings; a new store |
| Test sink: integration and e2e | Mailpit 1.31.5, pinned, loopback only | REST API for assertions. Never in production. | A real relay in CI |
| Test sink: failure injection | `aiosmtpd 1.4.6`, test-only | Programmable replies, disconnects, STARTTLS. Loopback only. | Hand-written socket fakes |
| Later, not v1 | Apprise for non-account channels | Too broad for account mail. | |

Every version and advisory statement in this table comes from OM and is
UNVERIFIED here; check primary advisories before the release decision (RT
"keep" 11).

**D3.4 Messages in v1.** Six messages: invitation; password reset link; confirm
recovery email (NIST 800-63B-4: recovery addresses must be verified); password
changed notice (OWASP Forgot Password); recovery email changed notice to the old
address (OWASP Authentication); test email. Completion emails are not in v1
(decision M3).

**D3.5 Recovery consumes mail through one public state.** The mail module
computes `delivery_state ∈ {off, ready, unavailable}`. UB §3 publishes it as
`recovery.email_delivery` in `GET /auth/api/options`. `ready` requires all of:
- mail enabled;
- a configured `https` origin (`configured_public_origin()`, §1.2 D1.5);
- a relay password stored for the current destination;
- an accepted test whose transport fingerprint equals the current one (D3.9);
- no permanent transport failure since that test;
- urgent-mail budget left in the current hour (§3.7).
Enabled but not `ready` is `unavailable`. The state is global; it never
depends on the account in a request.

**D3.6 Durable outbox in `admin.db`, one worker thread.** Account changes and
their outbox rows commit in one transaction (accounts and mail share
`admin.db`). The worker is a lifespan task in the existing pattern. Test sends
use the same outbox and worker, so there is one sending path (D3.10).

**D3.7 The outbox holds no secrets and no bodies.** Reset and verify tokens are
derived from the link id, as invites already are, so the worker can recompute a
token at send time and a retry sends the same link. The outbox stores purpose,
link id, recipient snapshot, immutable event facts and state, never a token or
a rendered body.

**D3.8 Derived tokens carry a key id (RT-M12).** The first draft claimed that
rotating the signing key invalidates unused links. That is false for delivered
links: redemption checks only the stored hash, so a delivered token keeps
working, and anyone holding the old key and a link id could derive one.
Decision:
- Each derived link stores `key_id`, a public fingerprint of the derivation
  key (first 16 hex characters of HMAC-SHA256(key, `azizam.key-id.v1`)).
- Redemption requires `link.key_id` to equal the current key id, in the same
  transaction that consumes the link. A rotation therefore invalidates every
  unused derived link (invite, reset, verify) at once.
- Before sending, the worker recomputes the token and checks its hash against
  the stored hash; a mismatch or a key-id mismatch cancels the row
  (`canceled`, layer `key_rotated`). A dead link is never mailed.
- Migration: existing invites get the current key id (they were derived from
  it). Existing unused random reset links are revoked (P11; resets are short
  and the owner reissues them). Owner-issued resets also become derived from
  then on, so one redemption rule applies.
UNVERIFIED: whether a key-rotation procedure exists; with this rule any
rotation fails closed.

**D3.9 Transport fingerprint separate from the setting revision (RT-B1, RT-M16).**
- The `mail` operator setting has its normal revision for optimistic
  concurrency (`If-Match`).
- The **transport fingerprint** is SHA-256 over host, port, security, username,
  From address and the password reference id. `enabled`, From name, Reply-To
  and the timeout are not part of it.
- A test records the fingerprint it used. Turning mail on requires an accepted
  test with the current fingerprint. Changing `enabled` does not change the
  fingerprint, so Save → Test → Turn on works.
- Any write that changes the fingerprint, in the same transaction: sets
  `enabled=false`, and cancels every queued or retrying non-test row
  (`canceled`, layer `mail_reconfigured`).
- **The password is bound to its destination.** Host, port, security and
  username form the destination. A write that changes the destination clears
  the password reference in the same transaction; the old secret is deleted
  after commit. A test or the worker resolves the password only through the
  current reference, so a saved password can never be sent to a newly typed
  server. The owner enters the password again.
- Password writes follow the OIDC pattern: store the new secret under a new id
  (`mail-smtp-password-<hex>`), commit the setting with the new reference
  (which changes the fingerprint), then delete the old secret.
- The password is stored only in the secret store. The first draft's
  environment-variable reference is dropped: its value can change without any
  write, so no fingerprint could follow it.

**D3.10 Owner-visible, honest states.** Capability: `Off`, `Not tested`,
`Testing`, `Ready` ("Accepted by the mail server {time}"), `Failing` ("Last send
failed {time}: {layer}"), `Delayed` ("The hourly email limit was reached"). SMTP
250 means the relay accepted the message, not that it reached the inbox; the
copy says so.

### 3.3 API, CLI and MCP shape first

All `/admin/api/mail` routes require the **owner** role, checked explicitly
from the context roles (as `gateway/admin_api.py:2299-2304` does), by session
with CSRF or by owner bearer key. Writes need `If-Match` on the `mail` setting
revision (428 / 409 as `gateway/settings_api.py`). They go into
`gateway/owner_openapi_routes.json`, `docs/ops/HEADLESS_ACCESS.md` and the route
inventory.

```text
GET  /admin/api/mail
  200 {
    enabled, state: "off"|"not_tested"|"testing"|"ready"|"failing"|"delayed",
    config: {host, port, security: "tls"|"starttls"|"none", username,
             from_address, from_name, reply_to, timeout_s},
    password: {state: "set"|"unset"},           // no suffix: short passwords would leak
    public_origin: {state: "https"|"http"|"unset"},
    last_test: {id, at, to_domain, outcome, layer, smtp_code?, fingerprint_matches: bool} | null,
    last_failure: {at, purpose, layer, smtp_code?} | null,
    revision
  }
  ETag: revision

PUT  /admin/api/mail                       If-Match
  body: {enabled, host, port, security, username, from_address, from_name, reply_to, timeout_s}
  - Fingerprint change → enabled forced false, queued rows canceled (D3.9).
  - Destination change → password reference cleared (D3.9).
  - enabled:true → 409 mail_not_tested unless the last accepted test matches the
    resulting fingerprint; 409 mail_needs_https without an https origin.
  - security "none" only for a loopback literal (127.0.0.0/8, ::1) or "localhost";
    otherwise 400 mail_tls_required.
  - enabled:false → queued non-test rows canceled (layer mail_off).
  Audit: mail.config.update.

PUT    /admin/api/mail/password  {password}   If-Match; 204; audit mail.password.set
DELETE /admin/api/mail/password               If-Match; 204; audit mail.password.clear

POST /admin/api/mail/test  {to}
  - Allowed while enabled is false. 5 per hour per gateway.
  - to: owner-supplied, validated by email-validator. This is the one place an
    envelope recipient comes from a request body (RT-N1); audit mail.test.send
    records the domain only.
  - Enqueues a test row (priority 0) and waits up to 45 s for a terminal state.
  200 {id, outcome: "accepted"|"failed"|"pending", layer, message, details: {smtp_code, tls_version, at}}
      "pending" means the worker is still trying; GET /admin/api/mail shows the result.
POST /admin/api/mail/test/{id}/received                 records received_at (optional)

GET  /admin/api/mail/deliveries?cursor=&purpose=&state=
  {id, purpose, recipient, state, maybe_delivered, attempts, created_at, finished_at, layer, smtp_code}.
  No tokens, no bodies, no server replies.
```

**Changes to existing owner routes:**
- `POST /admin/api/invites` gains `send_email: bool`, default `true` when mail
  is `ready` and `email` is present (decision M2). The response adds
  `delivery: {id, state} | null`. The `url` is still returned. One invite enqueues at most one invitation
  email; "Send again" on the invite row enqueues another, explicitly.
- `POST /admin/api/people/{id}/reset` gains `send_email: bool`. Mail goes only
  to that person's verified recovery address; `send_email: true` without one →
  409 `no_recovery_address`. The URL is still returned. As today, an owner reset
  revokes the person's older unused resets (a deliberate owner action).

**Public and member (the UB §3 contract, delivered by mail):**
- `GET /auth/api/options` → `recovery.email_delivery` (D3.5).
- `POST /auth/api/password-reset/request {email}` (RT-M15, RT-N1):
  - When `delivery_state` is not `ready`: 503 `recovery_delivery_unavailable`
    for every request.
  - Otherwise: normalize the address syntax, apply the per-address and per-IP
    throttles, insert one row into `reset_requests`, return 202 `{}`. That is
    the whole request path, identical for every identity. Account lookup,
    eligibility and link work happen later in the worker, so neither timing nor
    database work on the request path depends on the account.
  - The worker, for each request row: if a local, enabled account has this
    **verified** recovery address, and it has an active self-service reset link
    with at least 20 minutes left and the current key id, it re-sends that same
    link; otherwise it creates a new self-service link (60 minutes) **without
    revoking** any existing link. A public request never invalidates a link the
    person already received. At most three active self-service links per person;
    beyond that the request is dropped silently. The request row is deleted
    after processing (it is a transport queue, not a record).
- `POST /me/api/recovery-address/verification {address, current_password}`:
  session plus CSRF. Local-password accounts only; OIDC-only people get 409
  `local_password_required` (their IdP handles recovery). The password is
  checked with the login verifier and the login throttle. This replaces the
  first draft's "signed in within 10 minutes" (RT-M1).
- `POST /auth/api/recovery-address/verify {token}`: public, rate-limited,
  page `/verify/` (amendment to UB in §3.10).
- `DELETE /me/api/recovery-address {current_password}`: removes it, with a
  notice to that address.
- `POST /me/api/recovery-address/verification/cancel`: revokes pending verify
  links.

**CLI:** the owner routes are OpenAPI operations, so Restish covers them
(`docs/ops/HEADLESS_ACCESS.md:72`). The host-only owner recovery command is UB
§3's and does not depend on mail.

**MCP:** not supported, explicitly. Mail configuration and account recovery do
not belong on an inference connection.

### 3.4 Messages (exact text)

Common rules:
- Plain text, UTF-8. `From: "{from_name}" <{from_address}>`; `from_name`
  defaults to the instance name. `Reply-To` only when configured.
- Headers: `Auto-Submitted: auto-generated`, `Message-ID:
  <{outbox_id}@{from_domain}>`, stable across retries.
- No tracking, no rewritten links, no prompt text, no creations.
- Links use `configured_public_origin()`, never `Host`. Tokens travel in the
  fragment, so a link scanner's GET cannot consume them. Redemption is a POST.
- `{instance}` and names: control characters stripped, 80 characters max.
- Times in notices come from the row's `event_at`, never "now" at send time.

**1. Invitation.** Subject: `{inviter} invited you to {instance}`

```text
{inviter} invited you to {instance}.

Accept the invitation:
{base}/invite/#token={token}

This link works once and expires on {expires_date}.
If you weren't expecting this, you can ignore this email.
```

**2. Password reset.** Subject: `Reset your {instance} password`

```text
Someone asked to reset the password for your {instance} account.

Choose a new password:
{base}/sign-in/#reset={token}

This link works once and expires at {expires_time} UTC.
If you didn't ask for this, ignore this email. Your password stays the same.
```

**3. Confirm recovery email.** Subject: `Confirm your recovery email for {instance}`

```text
Confirm that password reset emails for {instance} can come to this address:
{base}/verify/#token={token}

This link expires at {expires_time} UTC.
If you didn't ask for this, ignore this email.
```

**4. Password changed.** Subject: `Your {instance} password was changed`

```text
The password for your {instance} account was changed on {event_time} UTC.
You were signed out everywhere. Your app keys keep working.

If you didn't do this, tell the person who manages {instance} now.
```

**5. Recovery email changed** (to the old address). Subject: `Your {instance} recovery email was changed`

```text
On {event_time} UTC, password reset emails for your {instance} account were
moved to a different address. This address will no longer receive them.

If you didn't do this, tell the person who manages {instance} now.
```

**6. Test.** Subject: `Test email from {instance}`

```text
This is a test email from {instance}.
If you can read it, {instance} can send email through your mail server.
```

### 3.5 Behavior when mail is off

| Flow | Mail off or not `ready` | Mail `ready` |
|---|---|---|
| Forgot password | UB copy: "This Azizam doesn't send password reset emails. Ask the person who manages it for a reset link." | UB email form and uniform reply |
| Owner reset | "Create link" → copy link (current) | Same, plus "Email it to {verified address}" when the person has one |
| Invite | Copy link (current) | "We'll email this invitation to {email}." with an opt-out; copy link remains |
| Account → Recovery email | "This gateway doesn't send email. If you forget your password, ask the owner for a reset link." No input field. OIDC-only people see nothing. | Add, confirm or change the address |
| Password changed | In-app security event at next sign-in (UB §3) | Email to the verified address, plus the in-app event |
| Mail breaks after it was ready | State `failing`; public state `unavailable`; queued rows wait and expire at `not_after`; owner-visible failure on Settings → Email and the setup checklist. Unrelated gateway work continues (P3, P18). | |

### 3.6 Settings, setup, and test-send flow

**Where:** Settings → **Email** (owner only). Also one optional card at the end
of first-run setup: "Email (optional): Send password reset links and invitations
by email." / "Set up email" / "Skip for now". The setup checklist
(`gateway/home_api.py`) gains an optional step "Set up email", reason "Lets
people reset their own password."

**Form copy:**
- Section title "Email". Off state: "Email is off. Password resets and
  invitations are shared as links." / "Set up email".
- Fields: "Mail server", "Port" (587, or 465 for TLS), "Security" ("STARTTLS
  (usually port 587)" or "TLS (usually port 465)"), "Username", "Password"
  (write-only), "From address", "From name" (placeholder: instance name).
- Technical details: "Reply-to address", "Connection timeout".
- Password once saved: "Saved" / "Replace" / "Remove". A blank field means
  unchanged.
- After changing the server, port, security or username: "Enter the password
  again for this mail server." The password field is empty and required.
- Public origin not `https`: "Set this gateway's public address to an https
  address first. Email links use it." / "Set public address". "Send test email"
  stays enabled (the test has no link); "Turn on email" is disabled with that
  reason.
- Actions: "Save", then "Send test email" (field "Send to", prefilled with the
  owner's verified recovery address if any). Busy "Sending test…". If the 45 s
  wait ends first: "Still trying. The result appears here when the mail server
  answers."
- After an accepted test: "The mail server accepted the test at {time}. Check
  your inbox and spam folder." / "I got it" / "Turn on email". The switch stays
  disabled until a test with the current settings is accepted: "Send a test
  with these settings first."

**Test-send result copy** (layer, then message; Details show the SMTP code, TLS
version and time; never a server reply or traceback):

| Layer | Message |
|---|---|
| dns | "Couldn't find the mail server {host}. Check the server name." |
| connect / timeout | "Couldn't connect to {host}:{port}. Check the port, and that this computer can reach the internet on that port." |
| tls | "Couldn't verify the mail server's certificate. Check the server name." |
| starttls_missing | "The mail server doesn't offer STARTTLS on this port. Use TLS on port 465, or check the port." |
| auth | "The mail server rejected the username or password." |
| sender | "The mail server rejected the From address. Use an address your mail service lets you send from." |
| recipient | "The mail server rejected the address {to}. Check it." |
| temporary (4xx) | "The mail server asked to try again later. Try again in a few minutes." |
| unknown_after_data | "The connection ended before the mail server confirmed. The email may still arrive." |
| config | "Enter the password for this mail server." |
| accepted | "The mail server accepted the test. Check your inbox and spam folder." |

**Member: Account → Recovery email** (local-password accounts):
- Empty: "Add an email for password resets." / "Email address" / "Current
  password" / "Send confirmation".
- Pending: "Check {address} for a confirmation link. It expires at {time}." /
  "Send again" / "Cancel".
- Verified: "Password reset emails go to {address}." / "Change" / "Remove"
  (both ask for the current password).
- While a change is pending, the old address stays verified and active.

**Verify page `/verify/`** (public, added to the page registry):
- Busy: "Confirming your email…"
- Success: "Your recovery email is confirmed." / "Go to {instance}".
- Failure (expired, used, revoked, key rotated, or the address is taken):
  "This confirmation link no longer works." / "Send a new one from Account"
  (signed in) or "Back to sign in" (signed out). The page never says which.

### 3.7 Security rules

**Tokens:**
- 256-bit, in the URL fragment.
- Invite, reset and verify tokens are HMAC-SHA256 under keys derived with HKDF
  from the session signing key, labels `gateway.invite.v1` (unchanged),
  `azizam.reset.v1`, `azizam.verify.v1`, over the link id.
- Stored as SHA-256 with `key_id` (D3.8).
- Bound to the person, the purpose, and for verify the normalized address.
- Single use: consumption and the state change commit in one `BEGIN IMMEDIATE`
  transaction (the `accept_invite` and `reset_password` pattern,
  `gateway/accounts.py:319`, `:362`).

**Lifetimes:** self-service reset 60 minutes (re-sent while at least 20
minutes remain); owner reset as today; verify 24 hours; invite 7 days.

**Rate limits** (new buckets in the existing throttle table):

| Action | Limit |
|---|---|
| Request reset | Per normalized address: 1 per minute, 5 per hour. Per client IP: 20 per hour. Counted for nonexistent addresses too. |
| Redeem reset or verify | Existing per-link bucket (`gateway/accounts_api.py:200-211`) plus per IP 30 per 15 minutes. |
| Start verification | Per person: 3 per hour, plus the login throttle on the password check. |
| Test send | 5 per hour per gateway. |
| All mail | 60 messages per hour per gateway. Invites may use at most 30 of them, so at least 30 remain for resets, verifications and notices. |

Throttle rows older than their window are deleted by the worker's maintenance
pass, so the table stays bounded. Requests never lock an account (OWASP).

**Priority and expiry (RT-M15).** The worker claims rows by priority (0 test,
1 reset and verify, 2 notices, 3 invites), then by `next_attempt_at`. When the
hour's budget for priority 1 is used up, `delivery_state` becomes
`unavailable` for everyone until the hour rolls over: new public requests get
503 instead of a 202 that could never be honored. A row not sent before its
`not_after` becomes `expired`; the owner's history shows "Expired before
sending", distinct from "Failed".

**No enumeration.** The public request returns the same status, body and
headers for existing, absent, unverified, disabled, OIDC-only and throttled
identities, and does the same work (§3.3 request path). Per-recipient failures
are visible only to the owner.

**Redemption.**
- A successful reset revokes the person's sessions, the Try keys bound to them
  (§3.1 defect), and all other unused reset links; keeps app keys; does not
  sign the person in.
- It enqueues message 4 to the verified address (with `event_at`) and records
  the in-app event.
- It never re-enables a disabled person and never changes a role.
- A successful verify, in one transaction: checks the link (unused, unexpired,
  current key id, kind verify); inserts or replaces the person's row in
  `account_recovery_addresses`; enqueues message 5 to the old address if there
  was one; marks the link used; revokes the person's other verify links;
  cancels queued resets to the old address (`superseded`). If another person
  already has this address verified, the UNIQUE constraint fails, the
  transaction rolls back, and the page shows the generic failure; nothing about
  the other person is revealed. Two concurrent redemptions for the same
  address: exactly one succeeds.

**Send-time checks (RT-M13).** At claim time, in the claim transaction, the
worker reads the current authoritative state and cancels the row instead of
sending when:

| Purpose | Cancel when |
|---|---|
| any non-test | mail is not enabled, or the fingerprint changed since the row was queued (rows are canceled at the change itself; this is the backstop) |
| invite | link used, revoked or expired; key id not current |
| reset | link used, revoked or expired; key id not current; person disabled or removed; recipient is no longer the person's verified address |
| verify | link used, revoked or expired; key id not current; a newer verify link exists |
| password_changed, address_changed | person removed |

Security events cancel atomically in their own transactions: disabling or
removing a person (`gateway/accounts_api.py:220-237`) cancels all their rows;
revoking an invite (`:274`) cancels its row; a successful reset cancels queued
resets for the person; turning mail off or changing the fingerprint cancels
queued non-test rows. The canceled state is terminal. A row already handed to
the SMTP server when a cancel commits may still be delivered; that boundary is
documented in the owner's history ("May have been sent before it was
canceled").

**Relay secret.**
- Stored only in the secret store, id `mail-smtp-password-<hex>`, referenced by
  the `mail` setting (D3.9). Never in `operator_settings` values, never in
  exports. `GET /admin/api/config/export` shows `password: {state}` only.
- Bound to its destination (D3.9).
- Never logged: `smtplib` debug level 0; SMTP exceptions are mapped to a layer
  and code, and their text (which may echo the username or AUTH data) is
  discarded. Audit metadata has the domain and outbox id only.
- The store keeps its provider-named API in this step (RT-N4): it already holds
  the OIDC secret under that API. Renaming the table and functions is a
  separate P11 cleanup with its own migration plan, not part of mail.

**Transport safety.**
- `security` must be `tls` or `starttls`; `none` only for loopback.
- Certificates are verified with the system trust store. No custom CA and no
  "ignore certificate errors" in v1 (OM "Disagreements").
- Envelope sender and recipients come only from saved configuration and
  account records, except the owner's test recipient (§3.3).
- Links require an `https` public origin (D3.5).

**Audit:** `mail.config.update`, `mail.password.set`, `mail.password.clear`,
`mail.test.send`, `mail.send.failed` (permanent), `mail.send.canceled`
(security cancels), `account.recovery_address.verify_start`,
`account.recovery_address.verified`, `account.recovery_address.removed`.

### 3.8 Data and migration

Additive in `admin.db`, opened through `gateway/sqlite_startup.py`, under the
next admin schema version, with a backup before the migration:

```text
mail_outbox(
  id TEXT PRIMARY KEY,               -- Message-ID local part, stable across retries
  purpose TEXT NOT NULL CHECK (purpose IN
    ('invite','reset','verify','password_changed','address_changed','test')),
  priority INTEGER NOT NULL,         -- 0 test, 1 reset/verify, 2 notices, 3 invite
  link_id TEXT,                      -- account_links.id for invite/reset/verify
  user_id TEXT,
  recipient TEXT NOT NULL,           -- normalized address snapshot
  event_at INTEGER,                  -- immutable fact for notices
  fingerprint TEXT NOT NULL,         -- transport fingerprint when queued
  state TEXT NOT NULL CHECK (state IN
    ('queued','sending','retrying','accepted','failed','expired','canceled')),
  maybe_delivered INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL,
  not_after INTEGER NOT NULL,        -- link expiry; never send after it
  lease_owner TEXT, lease_until INTEGER,
  layer TEXT, smtp_code INTEGER,
  created_at INTEGER NOT NULL, finished_at INTEGER)

account_recovery_addresses(          -- verified addresses only (RT-B2)
  user_id TEXT PRIMARY KEY,
  address TEXT NOT NULL UNIQUE,
  verified_at INTEGER NOT NULL)

reset_requests(                      -- transport queue; rows deleted after processing
  id TEXT PRIMARY KEY, address TEXT NOT NULL, created_at INTEGER NOT NULL)

account_links:
  + address TEXT NULL                -- normalized address, verify links only
  + key_id  TEXT NULL                -- derived links (D3.8)
  CHECK ((kind = 'verify') = (address IS NOT NULL))
```

- Pending verification lives in `account_links` (kind `verify`, with its
  address); `account_recovery_addresses` holds only verified rows. So the first
  pending verification needs no placeholder, and a change keeps the old
  verified row until redemption (RT-B2).
- `create_link` (`gateway/accounts.py:245-273`) accepts kind `verify`.
- Mail configuration is the `operator_settings` key `mail` (no secret inside).
- Rollback: an older release ignores the new tables and columns. Revoked reset
  links stay revoked.

**Worker rules (RT-M14):**
- **Claim:** one statement claims one row and fences it:
  `UPDATE mail_outbox SET state='sending', lease_owner=:token, lease_until=:now+300, attempts=attempts+1
   WHERE id=(SELECT id FROM mail_outbox WHERE state IN ('queued','retrying') AND next_attempt_at<=:now
   ORDER BY priority, next_attempt_at LIMIT 1) AND state IN ('queued','retrying') RETURNING *`,
  inside the claim transaction that also runs the send-time checks.
  UNVERIFIED: the SQLite version in the Docker image supports `RETURNING`
  (3.35+); otherwise use `BEGIN IMMEDIATE`, select, update, commit.
- **Attempt deadline:** each SMTP operation has a socket timeout (`timeout_s`,
  default 15). The loop waits for the attempt thread for at most 90 s in total
  (DNS, connect, TLS, AUTH, MAIL, RCPT, DATA). On the deadline the loop moves
  the row to `retrying` with `maybe_delivered=1` and clears `lease_owner`,
  guarded by `lease_owner = :token`. The late thread's own finalization is then
  rejected by the same guard. The lease (300 s) is longer than the deadline.
- **Finalization:** every terminal or retry update is guarded by
  `lease_owner = :token`.
- **Reclaim:** every loop pass (not only at boot) moves `sending` rows whose
  lease expired to `retrying` with `maybe_delivered=1`.
- **Retry:** 4xx, connection errors and unknown-after-DATA retry after 1, 5 and
  15 minutes plus jitter, at most 4 attempts, never after `not_after`. A
  `maybe_delivered` row that runs out of attempts becomes `failed` with layer
  `unknown_after_data` ("May have been delivered"). Delivery is at least once:
  a retry may duplicate a message with the same `Message-ID` and the same link,
  which is harmless. There is no exactly-once promise.
- **Permanent:** 5xx for sender or recipient → `failed`, no retry. `auth` or
  `tls` → `failed`, capability `failing`, claims of non-test rows pause.
- **Supervision:** the loop catches and logs every exception per pass and
  sleeps with backoff; a crashed attempt never stops the loop. Shutdown sets the
  stop event, starts no new claim, and lets leases expire for reclaim at the
  next boot.
- One attempt at a time (one process, one worker).

**Existing data:** every `users.email` stays unverified. Nothing is marked
verified by migration, invite acceptance or OIDC claims. No bulk email when mail
is first turned on (decision M1).

**Retention:** outbox rows are delivery receipts; keep them under the receipt
retention rules (P19). `reset_requests` rows are a transport queue and are
deleted after processing.

**Dependencies:** `email-validator==2.3.0` becomes direct in
`requirements.txt` (already in the lock). `aiosmtpd==1.4.6` is added to the
test requirements and the hashed lock. Mailpit 1.31.5 is a pinned image used
only by the e2e harness.

**Docs:** `README.md:101` and `docs/PEOPLE_AND_ACCOUNTS.md:175-177` change from
"no mail" to "optional mail"; `CONFIG.md` gains the mail settings.

### 3.9 Tests

| Level | Fixture and oracle |
|---|---|
| Unit | Message builder: CR/LF in names, subject or addresses raises before any network. Header and MIME snapshots for all six messages. Links use the configured origin under forged `Host` and `X-Forwarded-Host`. Notice times come from `event_at`. No token or password in any log record (capture logs, including SMTP AUTH failures). |
| Unit | Tokens: purpose confusion fails; wrong person or address fails; expiry ±1 s; replay fails; two concurrent redemptions give one success; after a key change every unused invite, reset and verify link fails and the worker cancels their queued rows without sending. Migration: existing invites keep working; existing random resets are revoked. |
| Fingerprint (RT-B1, RT-M16) | Save → test → turn on succeeds. Turning on after any fingerprint change → 409. Changing only From name keeps `ready`. Changing host clears the password; a test then fails with layer `config` and no connection carries the old password (assert on the aiosmtpd sink). Password replace changes the fingerprint and disables mail. Concurrent config and password writes with the same `If-Match`: one 409. |
| SMTP (aiosmtpd, loopback) | Implicit TLS with a test CA; STARTTLS required and offered; required and not offered → `starttls_missing` with no AUTH sent; wrong CA or host → `tls`; 535 → `auth`; 550 on RCPT → `recipient` with no retry; 451 → `retrying` then `accepted`; disconnect after DATA → `retrying` with `maybe_delivered` then a resend with the same link and `Message-ID`; a sink that stalls → the 90 s deadline moves the row and the late finalization is rejected. |
| Outbox | Kill the loop between claim and send without restarting the process; the next pass reclaims and resends once. Expired rows never send. Disabling mail with queued and in-flight rows: queued → canceled; in-flight finishes and is recorded. Each send-time cancel rule in §3.7, one test per row. Disable a person with queued resets → canceled in the same transaction. Account change and outbox row roll back together. |
| Budget | 31st invite in an hour waits; resets still go. Priority-1 budget used up → public state `unavailable` → requests 503 for everyone. Rows past `not_after` → `expired`, shown as "Expired before sending". |
| Reset requests (RT-M15) | Repeated public requests for one address re-send the same active link and never revoke it; a new link only when under 20 minutes remain; at most three active. An owner reset still revokes older resets. |
| Recovery address (RT-B2) | First pending verification with no verified row; change keeps the old row until redemption; cancel; two people verifying one address: one succeeds, the other sees the generic failure, the response does not differ by the other person's existence. OIDC-only person → 409. Wrong current password → 403 and the login throttle counts it. |
| API | Enumeration: existing, absent, unverified, disabled, OIDC-only and throttled identities give byte-identical responses, and the request path executes the same SQL statements (statement trace). Timing over 200 requests each is recorded with its threshold; no claim beyond the measured result. Members, admins without the owner role and app keys get 403 on every `/admin/api/mail` route with no side effect. |
| API | The password is write-only: no GET, export, error, audit or log returns it or a suffix. |
| Headless | Route inventory, owner OpenAPI allowlist and `HEADLESS_ACCESS.md` include every new route. |
| E2E (Mailpit, isolated gateway on a free port, never 5000/15000) | Owner configures mail → test → "I got it" → turn on. Member adds a recovery email with the current password → reads the confirmation from Mailpit → opens `/verify/` in a second browser context → confirmed. Signs out → Forgot password → reads the reset mail → new password → old password fails, other sessions and their Try keys rejected, app key still works, password-changed mail received. Repeat at 390 px. Repeat with mail off: no email field anywhere, the link-sharing copy shows. |
| Rendering | Every message in Mailpit: plain text, complete clickable link, no line break inside the URL, no tracking. |

### 3.10 Relation to UX_BATCH1 §3 (recovery)

This section supplies UB's mail dependency. UB keeps the recovery screens, copy,
the owner handover dialog and the sole-owner host command. Amendments proposed
for UB's review:

1. **Verify through a public endpoint.** A confirmation link is often opened on
   a phone with no session. The token proves control of the mailbox and is
   bound to the person and address. Proposal:
   `POST /auth/api/recovery-address/verify {token}`, public, rate-limited, page
   `/verify/`. Starting verification stays session-only and asks for the
   current password.
2. **Paths are root paths.** UB's `/recent/` and `/sign-in/recover/` are root
   pages in the registry (§2.2).
3. **Self-service requests never revoke links** (§3.3), and the request path is
   uniform (account work in the worker).

The global `"reset"` bucket the first draft asked UB to fix is already fixed at
r18 (§3.1).

### 3.11 Risks

| Risk | Control |
|---|---|
| Mail is marketed as stronger than it is (an inbox is not MFA) | Copy never says "secure"; mail is recovery only, not sign-in. Magic links are deferred (OM §4). |
| A household shares one inbox | One verified address per person, unique. Others keep assisted recovery. |
| The relay accepts, then the message lands in spam or bounces | Copy says "accepted". "I got it" records observed receipt. Owner docs cover SPF, DKIM and DMARC. Bounce webhooks are not in v1. |
| Key rotation invalidates unused links | By design (D3.8); people ask again, owners re-copy invites. |
| Duplicate mail after an unknown-after-DATA retry | Same link and `Message-ID`; harmless; history shows "may have been delivered". |
| Advisories in `email-validator` or the stdlib | Pinned, hashed lock; advisory check at each release. |
| The owner's "email" is a username | Mail uses verified recovery addresses only. |

### 3.12 Owner decisions

1. **M1 Existing people's recovery email.** Recommended default: **no bulk
   email**. Local-password people see one line in Account: "Add an email so you
   can reset your password yourself." Until they confirm one, they keep
   assisted recovery.
2. **M2 Email invitations automatically?** Recommended default: **yes, when mail
   is ready and the owner typed an address**, with an opt-out checkbox and the
   copy-link path kept.
3. **M3 Completion ("done") emails.** Recommended default: **not in v1.**

Owner prerequisite: a relay account and a From domain with SPF and DKIM set up
at that relay, and an `https` public address.

---

## Red-team resolution

RT = `azizam-redteam-connect-root-mail-bddd248-r0-cap6.md`. "Applied" means the
change asked for is in this revision; "Simplified" means the problem is solved
with less machinery than proposed; "Rejected" gives code evidence.

| Finding | Resolution | Where |
|---|---|---|
| RT-B1 Enable cannot satisfy its own revision rule | Applied. Transport fingerprint (host, port, security, username, From, password reference) separate from the setting revision; `enabled` is outside it; fingerprint changes disable mail and cancel queued rows atomically. Environment-variable password dropped, because its value changes without a write. | D3.9, §3.3, §3.9 |
| RT-B2 First pending verification cannot be stored | Applied (the second option). Pending verification lives in `account_links` (kind `verify`, `address` column); `account_recovery_addresses` holds verified rows only, address UNIQUE; one redemption transaction; generic failure on conflicts. | §3.7, §3.8 |
| RT-M1 No trustworthy reauthentication | Simplified. The "signed in within ten minutes" rule is gone. Recovery-address changes ask for the current password (local accounts only; OIDC-only people have no local reset). Browser minting of automation keys is removed from step 2, so no OIDC step-up is needed; its prerequisites are recorded under C1. | D1.8, §1.9, §3.3 |
| RT-M2 Automation-key role and bearer contracts contradict | Applied. The role × credential × requested-role matrix documents current behavior; no route is tightened. UI lists and revokes only. Admins revoking owner keys is recorded as unchanged. | D1.9, §1.8 |
| RT-M3 curl recipe puts the key in argv | Applied. Mode-600 header file written with the `printf` builtin, `curl -H @file`; SDK snippets read the environment in-process; tracing warning. | D1.4, §1.4 |
| RT-M4 Non-chat apps get a chat example | Applied. Models come from one key's effective catalog; chat examples only for declared chat models; otherwise `models.list()`. | §1.3, §1.4 |
| RT-M5 One public address does not cover discovery and MCP | Applied. `configured_public_origin()` with the Settings validator, no header fallback, used by every surface; per-surface unconfigured table; MCP Origin check compares full origins against it (no longer deferred); connection check stays in-process. | D1.5, D1.5a, D1.6 |
| RT-M6 HEAD on the catch-all changes API methods | Applied by redesign. The catch-all is unchanged; `UiRoute` is never partial and fully matches only registry shapes, so every registered route keeps its 405 and `Allow`. Deliberate changes: `HEAD /` stays 200, `HEAD /admin` → 405, HTML 404 for unregistered segments. Baseline matrix test. | D2.2, D2.3, §2.3, §2.8 test 1 |
| RT-M7 Revocation overrides bearer and signed media | Applied, simplified. The middleware no longer denies; it only clears a revoked cookie, never over a response that sets a new session, never on assets. `ADMIN_AUTH` falls through to the bearer check on a revoked session. Evidence: every session decoder already checks revocation (`gateway/admin_runtime.py:1507`, `proxy.py:15358`); `authenticate_request` reads no cookie. The public-path exemption list disappears. | D2.6, §2.8 test 8 |
| RT-M8 Setup POST rename keeps an HTML contract | Simplified. The form posts keep their paths and HTML contract (they are native form actions, `web/src/app/setup/page.tsx:77-80`, `:151-153`) and are on the HTML allowlist; `setup_render` stays. No JSON conversion in step 1. | D2.5, §2.8 test 5 |
| RT-M9 ADMIN_AUTH is not an owner guard | Applied. The pages are admin-capable, as the shell and its copy already say (`session.tsx:42`, `app-shell.tsx:726`); the guard checks `ADMIN_CONTROL_ROLES` explicitly, runs before 304, and comes from the registry (adds `categories`). `/status/raw` is stated as admin-capable. Four-role outcome table. | D2.2, D2.6, §2.9 R2 |
| RT-M10 Classifier and export contract incomplete | Applied. Page set from the committed registry (works with no build); exact payload and asset shapes established from the real export; export shape test on a production build; production-build navigation test; missing-build 503. | D2.2, D2.4, §2.8 tests 4, 11, 12 |
| RT-M11 Non-/ui producers; live OIDC setting makes deploy non-atomic | Applied. Producer inventory covers `/login`, `/admin/login`, `/me/*`, logout forms, `/r/`, discovery; the callback stays in step 1, so step 1 has no configuration or IdP change; step 1b makes the callback path a code constant and uses a staged IdP registration. Found and fixed: `safe-restart` published before draining. Stale-tab test. | D2.5, D2.9, §2.5, §2.6, §2.10 |
| RT-M12 HMAC derivation does not implement rotation invalidation | Applied. `key_id` on derived links, checked at redemption and before sending; the false claim is withdrawn; migration for existing links. | D3.8 |
| RT-M13 Outbox lacks cancellation rules | Applied. Per-purpose send-time checks in the claim transaction; atomic cancels on security events and mail-off; terminal `canceled`; `event_at` for notices. | §3.7, §3.8 |
| RT-M14 Worker liveness and deadlines | Applied. Fenced single-row claim, 90 s total attempt deadline under a 300 s lease, guarded finalization, periodic reclaim, supervised loop, at-least-once with stable `Message-ID`. | §3.8 |
| RT-M15 Requests can invalidate links; budget can expire requests | Applied. Self-service requests coalesce and never revoke; priorities and an invite cap; budget exhaustion makes the public state `unavailable`; "expired" distinct from "failed"; throttle pruning. | §3.3, §3.7 |
| RT-M16 SMTP credential has no destination binding | Applied, simplified. A destination change clears the password in the same transaction; resolution only through the current reference; no suffix; no server replies stored. | D3.9, §3.7 |
| RT-N1 Test oracles too broad | Applied. Route-class oracles (§2.8 tests 1, 5, 7); the test-recipient exception is stated and audited; enumeration resolved by moving account work off the request path and tracing SQL statements, not by a timing claim. | §2.8, §3.3, §3.9 |
| RT-N2 Cache and security headers not uniform | Applied. One helper puts `no-store` and `no-referrer` on every browser auth response, including the OIDC callback; stale prefixes removed; guard before 304. | D2.6, D2.7, §2.8 test 15 |
| RT-N3 Recipe parity, serialization, client gates | Applied. Parity narrowed to facts in discovery; typed rendering with strict character sets and a parse test per language; every client claim UNVERIFIED until its matrix run. | D1.3, D1.7, §1.3 |
| RT-N4 Migration is not all additive | Simplified. The secret-store rename is dropped from this design (the store already holds the OIDC secret under its current API); mail's schema change is additive plus two nullable columns, with a backup and a stated rollback. | §3.7, §3.8 |
| RT "keep" items 1-11 | Kept. Item 6 changes in one detail: the showcase data routes stay where they are, because they do not collide with the page. | §2.2 D2.5 |

---

## Completion record

- Research inputs: CS, UR, OM, PR, BL (D2, D3, D6), UB §3, RT. All read in full.
- Code evidence: r18 `8a3147f0`, read-only. I read every line a step-1
  decision depends on (mount, middlewares, catch-all, revocation, session
  decoders, `ADMIN_AUTH`, shell gating, setup forms, logout, sanitizers,
  `safe-restart` publication) and listed the local export. A delegated sweep
  supplied the Connect and mail citations; I spot-checked the load-bearing ones
  (reset redemption, session revocation, admin-key role checks, discovery
  links).
- Review of this revision: a separate reviewer (Claude Opus, not the author)
  checked §2 against r18. Verdict: build-ready after fixes. All fourteen of
  its findings are applied. One was a blocker: clearing a revoked cookie on
  every response would have cancelled the new session cookie and looped
  sign-in. The other main fixes: the setup console's GET sign-out link, server
  links to `/admin`, the missed `/r/` and `/home` links, the Dockerfile build
  input, `ADMIN_AUTH` ignoring a bearer key behind a revoked cookie, and asset
  path traversal.
- Not done:
  - A full independent red-team of this revision, covering §1 and §3 (P20
    requires one before implementation).
  - The r18 baseline matrix (§2.8 test 1), the client matrix, any test run, any
    visual comp.
- Confidence:
  - High: step 1 routing (`UiRoute` plus unchanged catch-all), the revocation
    simplification, the deploy atomicity of step 1.
  - Moderate: the Next stale-tab behavior (test 13 decides), the mail worker
    timing constants, the recipe content for untested clients.
  - UNVERIFIED: every item so marked above, notably the reverse-proxy probe,
    renderer callers, the access-catalog capability field, and the SQLite
    `RETURNING` support in the image.
- Next step: a non-maker red-team of step 1 (§2) at minimum, then the owner's
  go for step 1, then implementation of step 1 alone.

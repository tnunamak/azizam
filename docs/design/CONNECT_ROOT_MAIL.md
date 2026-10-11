# Connect surface, UI at the root, outgoing mail

Status: design draft, 2026-10-11. **Not approved for implementation (P20).**
Baseline: `origin/release/r17` at `0061d150`. All `file:line` references are at
that commit. This document proposes behavior. It changes no product code.

## Inputs and reading rules

Owner's words (2026-10-11): "Also the UI doesn't reflect that there is an mcp
etc? And do we need /ui or can it move to /?"; "this is unreleased"; on mail:
"whether mail is worth building at all < yes are there no brainer libraries to
outsource complexity to? (libsodium is an example of a no brainer library)"; on
password reset: "Isn't the usual thing to have a self service link?"

Research (all in `~/Documents/azizam/ux-findings/research-results/`, read in full):

- **CS**: `azizam-research-connect-surface-20261011-b.md` (18 products, client handoff table).
- **UR**: `azizam-research-ui-at-root-c36a31f-c.md` (route inventory, collisions, 9 products).
- **OM**: `azizam-research-outgoing-mail-20261011.md` (17 products, library evaluation).
- **PR**: `azizam-research-self-hosted-password-reset-20261011.md` (21 products, copy).
- **BL**: `~/Documents/azizam/ux-findings/reports/principles-fix-backlog-2026-10-11.md`, sections D2, D3, D6.
- **UB**: `design/ux-batch1:docs/design/UX_BATCH1.md` §3 (account recovery, draft `ccb42202`).

Governing documents: `docs/design/PRINCIPLES.md` (from `docs/owner-principles`),
`docs/design/VOICE.md`, `docs/design/HEADLESS_AND_RESOURCES.md`, `AGENTS.md`,
and the code-quality canon (`~/code/divan/code-quality/CANONICAL-CODE-QUALITY-THEORY.phase1.md`).

Code evidence came from three read-only sweeps of the r17 tree. I checked the
load-bearing lines myself: the `/ui` mount (`proxy.py:13329`), the catch-all
(`proxy.py:30188`), the revocation exemptions (`proxy.py:15703`), the throttle
(`gateway/accounts.py:192`), `create_link` (`gateway/accounts.py:243`), the Apps
base-URL fallback (`web/src/app/apps/page.tsx:105`), the `fast` connection test
(`gateway/home_api.py:485`), the secret-store origin rule
(`gateway/secret_store.py:232`), the MCP origin check (`gateway/mcp_server.py:125`)
and the discovery recipe (`registry/protocol_frontends.yml:154`).

Labels: FACT is in the code or a cited source. DECISION is this design's choice.
UNVERIFIED is not established; implementation must prove it before relying on it.

### How the three sections depend on each other

1. **Root first.** Connect snippets, email links and the new pages
   (`/verify/`, `/sign-in/recover/`, `/recent/`) all use root paths. Ship
   section 2 before, or in the same release as, sections 1 and 3.
2. **One public address.** Connect and mail both need the configured public
   address (`instance_presentation.base_url`, `gateway/settings_api.py:17`).
   Neither falls back to the request `Host` header.
3. **Mail is the delivery capability that UB §3 recovery consumes.** This
   document does not redesign recovery. It supplies UB's
   `recovery.email_delivery` state, the transport, the outbox and the messages.
   Two small amendments to UB are listed in §3.10.

---

## 1. Connect surface (P21)

### 1.1 Current state

- **No surface mentions MCP, the API reference, `llms.txt` or the headless
  guide.** No file in `web/src` contains `mcp`, `llms` or `HEADLESS` (BL PF-D201).
  MCP is discoverable only through `/llms.txt`
  (`gateway/discovery_metadata.py:348`) and `docs/LOCAL_CLIENT_RECIPES.md:1489`.
  `/.well-known/azizam` (`proxy.py:18643`, `gateway/discovery_metadata.py:166-283`)
  lists `openapi`, `llms_txt` and `skill_catalog`, but not `/mcp`.
- **There are two creation paths with different recipes and different base URLs.**
  - Apps & keys: `web/src/app/apps/page.tsx`. `CLIENTS` is at `:52`. The create
    dialog is at `:495-562` and the reveal dialog at `:619-718`. The base URL is
    `discovery.recipes.openai.canonical_base_url`, else
    `window.location.origin + "/openai/v1"` (`:105-109`). The reveal step is the
    only place the instructions appear. Saving the key closes them (BL PF-D202).
  - Home: the "Connect an app" card (`web/src/app/home/page.tsx:543`), recipes
    in `web/src/app/home/recipes.ts`. It uses `home.api_base_url` = `{base}/v1`
    (`gateway/home_api.py:385`).
  - The two surfaces disagree. Discovery says `canonical_base_url: /openai/v1`
    and `compatibility_base_url: /v1` (`registry/protocol_frontends.yml:158-160`).
- **One-time secret: FACT.** App keys are `opk_` + 32 random bytes
  (`gateway/admin_runtime.py:127`). Only `sha256:` hashes are stored
  (`gateway/runtime_auth.py:68`). The secret is returned once as
  `credential.api_key` (`gateway/user_runtime.py:461-464`), and later reads strip
  it (`gateway/user_runtime.py:42-44`). `SecretReveal`
  (`web/src/components/product/secret-reveal.tsx:31-38`) already says "Copy this
  key now. You can't view it again after closing this step."
- **Connection check spends a generation.** `POST /me/api/connection-test`
  (`gateway/home_api.py:455`) sends a one-token chat to the hard-coded alias
  `fast` when it is allowed (`:485-492`), otherwise `GET /v1/models`. This
  violates P12 (BL PF-176b) and silently runs a generation.
- **MCP: FACT.** `POST /mcp` only. GET and DELETE return 405 with
  `Allow: POST` (`gateway/mcp_server.py:610`, `:648`). Auth is the same bearer
  app key (`:614`). There is no OAuth server: the protected-resource metadata
  has no `authorization_servers` (`:661`, CS "Azizam baseline"). The tool list is
  static (`:586`). Live tests: Claude Code 2.1.286, `pi-mcp-adapter` 4.0.0, MCP
  Python SDK 1.26.0 (`docs/SUPPORT_MATRIX.md:81`).
- **Owner automation keys have no UI.** Owners create them with the host command
  `proxy.py generate-admin-key` (`proxy.py:2717`) or with
  `POST /admin/api/admin-keys` (`gateway/admin_api.py:2280`). The guide is
  `docs/ops/HEADLESS_ACCESS.md:8-40`. `/me/api/*` routes reject bearer keys,
  and app keys cannot administer.
- **The public address can come from `Host`.** `_public_base_url`
  (`proxy.py:18585-18607`) uses the saved `base_url`, then `AZIZAM_PUBLIC_URL`,
  then `x-forwarded-host`/`host` without validation. Settings edits the saved
  value as "Public base URL" (`web/src/app/settings/page.tsx:413-425`).
- **Per-key access data exists but is unused.** `GET /me/api/credentials/{id}/access`
  (`proxy.py:16243`) returns the models the key may use. No web file calls it.

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
(`web/src/app/apps/page.tsx:52`) and `web/src/app/home/recipes.ts` into
`registry/connect_recipes.yml`. The server renders them with the configured
address and a model name the app may use. Client names are third-party setup
examples, which P12 allows as data. The browser and headless callers get the
same output (P21).

**D1.4 Snippets never contain a secret.** Every snippet references
`$AZIZAM_API_KEY` or the client's own secret mechanism (`${env:…}`,
`${input:…}`). The secret appears only in the one-time reveal block, with
"Copy key". Deep links carry configuration only. Base64 is not encryption
(CS "Client handoff mechanisms").

**D1.5 Only the configured public address.** If `base_url` is unset, Connect
shows an error state. It never falls back to `window.location.origin` or the
`Host` header (BL PF-D202, CS "Put three correct values together").

**D1.6 Checking does not generate.** "Check connection" uses `GET /v1/models`
with the key (authentication plus policy, no generation). For MCP it uses
`initialize` plus `tools/list`. The check is available while the new secret is
still in memory. Afterwards the sheet shows the key's observed use
(`last_used_at`) instead of asking for the key again. Remove the `fast` chat
path from `/me/api/connection-test` (P12, BL PF-176b).

**D1.7 Recipes carry an honest support tier.** Each recipe has `tested:
{client, version, date}` or no `tested` field. Deep-link buttons appear only on
tested recipes. Claude Desktop and claude.ai custom connectors need OAuth, so
the sheet says they are not supported yet (CS "CRITICAL FACT", "P1 Assistant
picker").

**D1.8 Owners get one added section: "Gateway automation".** It is shown to
the owner role only, at the bottom of `/apps/` (P8: owner tools are an added
section). It lists automation keys, creates them with a one-time reveal, and
links the API reference (`/docs`), `/openapi.json` and the headless guide. Copy
states that app keys cannot manage the gateway (BL PF-D201 fix text).

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
- A generating test request on a green badge. It costs money (CS "P2"). The
  connection-test `fast` path is removed for the same reason.
- Keeping recipes in TypeScript. That leaves headless callers without them and
  keeps two copies.

### 1.3 API, CLI and MCP shape first

**Member, session (new):** `GET /me/api/apps/{app_id}/connect`. It uses the
same session and CSRF rules as the other `/me/api` routes. It returns 404
`app_not_found` for an app the caller does not own.

```text
200 {
  app: {id, name},
  address: {state: "ready"} | {state: "not_configured"},
  urls: {                         // present only when address.state == ready
    openai_base: "{base}/v1",     // see owner decision C2
    anthropic_base: "{base}/anthropic",
    mcp: "{base}/mcp",
    api_reference: "{base}/docs",
    llms_txt: "{base}/llms.txt"
  },
  model_names: [{name, capability}],   // what this app's plan allows; from the same filter as /v1/models
  recipes: [{
    id, label, kind: "app"|"agent",
    summary,                            // one sentence, plain words
    fields: [{label, value, copy: true}],
    files: [{path_hint, language, content}],
    commands: [{language, content}],
    deep_link?: {label, url},           // only when tested
    docs_url?, tested?: {client, version, date}
  }],
  keys: {active: n, last_used_at: epoch|null}
}
```

- `model_names` comes from the core profile filter used by `/v1/models`
  (`proxy.py:25580`, `_filter_model_list_for_profile`). It is never a
  hard-coded alias. An empty list is a valid state.
- Rendering uses Python `str.format_map` over trusted registry text and
  validated values. URLs are built from the configured origin plus fixed paths.
  Shell values go through one quoting function. JSON values are rendered with
  `json.dumps`.
- `address` is `not_configured` when `instance_presentation.base_url` is empty.
  The response then has no `urls` and no recipes.

**Member, session (changed):** `POST /me/api/connection-test`.
Body `{api_key, kind: "api"|"mcp"}`. The `kind: "api"` check calls
`GET /v1/models`. The `kind: "mcp"` check calls `initialize` and `tools/list`
on `/mcp`. Neither generates. The response is
`{ok, layer, message, checked: "models"|"mcp_tools", tool_count?, model_count?, request_id}`.
`layer` is one of `key`, `plan`, `gateway`, `mcp`, `unknown` (P3). The key must
belong to the caller, which is the current rule.

**Public discovery (changed):** `GET /.well-known/azizam` gains:
- `links.mcp`, `links.api_reference` and `links.oauth_protected_resource`.
- `recipes.mcp = {url, transport: "streamable-http", auth: "Authorization: Bearer $AZIZAM_API_KEY", oauth: false}`.

`llms.txt` keeps its MCP line. With the discovery document plus `/v1/models`
called with the key itself, an agent or script gets the same facts with no
session. This is the headless path for members. No new public endpoint is
needed.

**Owner, automation keys (existing routes, new UI):**
- `GET /admin/api/credentials` (`gateway/admin_api.py:2327`), filtered to
  admin subjects. UNVERIFIED: whether the response already identifies admin
  service-account keys. If not, add a `subject_kind` filter.
- `POST /admin/api/admin-keys {name, role}` (`gateway/admin_api.py:2280`).
  Owner role required, plus a session younger than 10 minutes. If the session
  is older, the server returns 403 `recent_sign_in_required`, and the UI sends
  the owner through sign-in and back.
- `POST /admin/api/credentials/{id}/revoke` (`gateway/admin_api.py:2366`).
- CLI: these are owner OpenAPI operations. Restish uses them as documented in
  `docs/ops/HEADLESS_ACCESS.md:72`. `generate-admin-key` remains the host-only
  bootstrap.

**MCP:** no new tools (HEADLESS_AND_RESOURCES "MCP keeps `capabilities`,
`run`, `job`"). Connecting is configuration, not a tool. Owner MCP tools stay
deferred there.

**Unsupported surfaces return explicit errors:** a bearer key on
`/me/api/apps/{id}/connect` gets the existing 401 `session_required`. MCP has
no connect tool.

### 1.4 Recipes (exact content)

Placeholders: `{base}` is the configured origin, `{model}` is the first chat
model name in `model_names`, `{name}` is the app name. When an app has no chat
model, the snippet uses the first model name of any capability and the label
says what it is for.

**API: base URL and key.** Fields: "Address" = `{base}/v1`, "Model name" =
`{model}`, "Key" = "The key you saved".

```bash
# Check that the key works (lists models; does not generate)
curl {base}/v1/models -H "Authorization: Bearer $AZIZAM_API_KEY"
```

```python
from openai import OpenAI
import os
client = OpenAI(base_url="{base}/v1", api_key=os.environ["AZIZAM_API_KEY"])
print(client.chat.completions.create(
    model="{model}", messages=[{"role": "user", "content": "Hello"}]
).choices[0].message.content)
```

```javascript
import OpenAI from "openai";
const client = new OpenAI({ baseURL: "{base}/v1", apiKey: process.env.AZIZAM_API_KEY });
const r = await client.chat.completions.create({ model: "{model}", messages: [{ role: "user", content: "Hello" }] });
console.log(r.choices[0].message.content);
```

The existing app recipes (Open WebUI, LibreChat, SillyTavern, Cursor models,
Codex, Claude Code models, phone) move unchanged in meaning into the YAML. One
change: the Claude Code model recipe sets
`ANTHROPIC_BASE_URL={base}/anthropic`, not the bare origin
(`web/src/app/home/recipes.ts:115`). This keeps one Anthropic address.
UNVERIFIED: that Claude Code accepts the `/anthropic` prefix. Prove it in the
client matrix before switching. Until then, keep the origin form and record the
reason in the YAML.

**MCP: Claude Code** (tested 2.1.286, `docs/SUPPORT_MATRIX.md:81`), project
file `.mcp.json`:

```json
{"mcpServers": {"azizam": {"type": "http", "url": "{base}/mcp",
  "headers": {"Authorization": "Bearer ${AZIZAM_API_KEY}"}}}}
```

Summary: "Add this file to your project, set AZIZAM_API_KEY, then start Claude Code."
Do not offer `claude mcp add … -H "Authorization: Bearer ${AZIZAM_API_KEY}"`
in double quotes, as `docs/LOCAL_CLIENT_RECIPES.md:1521` does. The shell
expands the secret into the process arguments and the stored configuration.
Fix that doc in the same change. UNVERIFIED: whether Claude Code expands
`${VAR}` in user-scope configuration added by the CLI. Until proven, the
recipe shows only the project file.

**MCP: Cursor** (untested in Azizam). `~/.cursor/mcp.json`:

```json
{"mcpServers": {"azizam": {"url": "{base}/mcp",
  "headers": {"Authorization": "Bearer ${env:AZIZAM_API_KEY}"}}}}
```

Deep link after testing:
`cursor://anysphere.cursor-deeplink/mcp/install?name=azizam&config={base64(json of the inner server object)}`
(CS "Cursor deep link").

**MCP: VS Code** (untested). `.vscode/mcp.json`:

```json
{"inputs": [{"type": "promptString", "id": "azizam-key", "description": "Azizam app key", "password": true}],
 "servers": {"azizam": {"type": "http", "url": "{base}/mcp",
   "headers": {"Authorization": "Bearer ${input:azizam-key}"}}}}
```

No deep link until it is tested. UNVERIFIED: whether a `vscode:mcp/install`
link can carry `inputs`, and Agent Host behavior (CS "VS Code config").

**MCP: Codex** (untested). `~/.codex/config.toml`:

```toml
[mcp_servers.azizam]
url = "{base}/mcp"
bearer_token_env_var = "AZIZAM_API_KEY"
```

UNVERIFIED: the Codex version that supports streamable HTTP with
`bearer_token_env_var`. The recipe ships only after a client-matrix pass.

**MCP: Claude Desktop and claude.ai connectors.** No recipe. Copy: "Claude
Desktop and claude.ai connectors need a sign-in method this gateway doesn't
offer yet. Use Claude Code, Cursor or another app that accepts a key."

**MCP: Other client.** Fields:
- "Server address" = `{base}/mcp`
- "Transport" = "Streamable HTTP"
- "Header" = `Authorization: Bearer <your key>`

Link: "MCP setup guide" (`/llms.txt`).

### 1.5 Screens and exact copy

The sheet is phone-first. At 390 px the first viewport shows the step title,
the choice or the recipe, and its primary action (P4, P5). Stack sections
vertically. Code blocks wrap; they never scroll horizontally on a phone.

**Create flow (Apps & keys → "New app"):**
1. Dialog title "New app". Field "App name". Action "Create app and key". Busy
   "Creating…". Remove the internal "App recipe" select (BL PF-D202); the client
   is chosen later.
2. Reveal step, same sheet. Title "Copy your key". The `SecretReveal` text
   stays: "Copy this key now. You can't view it again after closing this step."
   Action "Copy key". Acknowledgement "I've saved it". This advances to step 3
   and does not close the sheet.
3. Title "Connect {name}". Choices: "An app" with "Chat apps, coding tools and
   scripts", and "An AI agent (MCP)" with "Lets an agent use this gateway's
   actions".
4. Client list (from data), then the recipe card: summary sentence, fields with
   copy buttons, code block, deep-link button if any, "Check connection".
5. Technical details (collapsed): a list of addresses:
   - "OpenAI-compatible address"
   - "Anthropic-compatible address"
   - "MCP server"
   - "API reference"
   - "Guide for AI agents (llms.txt)"
   - "Tested with {client} {version} on {date}" (shown only when tested)

**Existing app → row action "Connect":** opens at step 3. In place of the
secret it shows: "Use the key you saved when you created it. Lost it? Create a
new key." The link calls the existing mint flow.

**Owner section "Gateway automation"** (owner only, below the app list):
- Heading "Gateway automation". Body: "Automation keys can change this gateway's
  settings, people and engines. Use an app key for apps and agents."
- List rows: name, role ("Owner" or "Admin"), "Last used {time}" or "Not used
  yet", action "Revoke".
- Action "Create automation key". Dialog fields:
  - "Name"
  - "Role", with two options:
    - "Admin: manage engines, apps and people" (default)
    - "Owner: everything, including other owners' keys"
  
  Action "Create key". The reveal then uses the same component as app keys,
  plus "Store it in a file only you can read. Never paste it into an app or
  agent."
- Links: "API reference" (`/docs`), "OpenAPI document" (`/openapi.json`),
  "Headless guide". The guide link points to the repository doc URL taken from
  discovery `repository_docs`. If discovery has no URL for it, the link is
  absent; the UI does not guess one.

**States:**

| State | Copy and action |
|---|---|
| Loading Connect data | Skeleton for the recipe card. No "no access" text while pending (CS "Required state model"). |
| Connect data failed | "The gateway couldn't load connection details." / "Try again" (layer: gateway). |
| Address not set, member | "This gateway's public address isn't set, so apps can't reach it yet. Ask the owner to set it." |
| Address not set, owner | "Set this gateway's public address so apps on other devices can reach it." / "Open Settings" (deep link to the field). |
| No models allowed | "This app can't use any models yet. Ask the owner to update its access plan." Owner: "Update its access plan" opens People → Plans. No invented default model. |
| Key creation not allowed | Use the server reason when given. Otherwise: "You can't create keys for this app. Ask the owner to update its access." (BL PF-033). |
| Copied | "Copied" for 2 s on the button; screen readers get a polite status. |
| Copy failed | "Couldn't copy. Select the text to copy it." The text stays selectable. |
| Deep link opened | "Finish in {client}. Opening the client doesn't connect it; check the connection after." |
| Check: working | "Connected. This key can use {n} models." (API) / "Connected. {n} tools available." (MCP). Show the check time. |
| Check: 401 | "The gateway didn't accept this key. Check that you pasted the whole key." (layer: key) |
| Check: no allowed models | "The key works, but its plan allows no models. Ask the owner to update its access plan." (layer: plan) |
| Check: 403 origin (MCP) | "The gateway refused this request because of where it came from. Run the check from the app, not a web page." (layer: gateway) |
| Check: unreachable | "Couldn't reach the gateway at {base}. Check the address and your network." (layer: unknown) |
| After secret is gone | "Not used yet. Use the key in {client}; this updates after its first request." / "Last used {time}". There is no Check button without a key. |
| Claude Desktop / web | The unsupported copy in §1.4. |
| Automation key create denied (role) | "Only an owner can create owner keys." |
| Automation key needs recent sign-in | "Sign in again to create an automation key." / "Sign in" (returns to this sheet). |

Delete these strings: the Home card blurb "For chat apps, phone apps and coding
tools. Each app gets its own key." (`web/src/app/home/page.tsx:546`), "App
recipe", "Gateway responded with this key" (`:805`), and "Create access for an
existing app; its key appears once." (`web/src/app/apps/page.tsx`).

### 1.6 Data and migration

- New file `registry/connect_recipes.yml`. It is data, validated at boot
  against a small schema: `id`, `label`, `kind`, `summary`, `fields`, `files`,
  `commands`, `deep_link`, `docs_url`, `tested`. A bad row fails boot, as other
  registry files do.
- Delete `web/src/app/home/recipes.ts`, the `CLIENTS` constant, and the Home
  card's recipe code. Delete `/me/api/home` fields that only that card used
  (`api_base_url`, `new_app_aliases`). UNVERIFIED: other readers. Find them
  with LSP references before deleting.
- Discovery: set `recipes.openai.canonical_base_url` to the owner's choice
  (decision C2). Keep both routes served. `/openai/v1` stays a working alias;
  it is not removed in this change.
- No database change. Automation keys already exist (`admin_store.py:154-171`).

### 1.7 Tests

| Level | Oracle |
|---|---|
| Server | `/me/api/apps/{id}/connect` with `base_url` set renders every recipe. Every `files[].content` parses as its declared language (JSON, TOML). Every deep link decodes back to the same server object. No response contains `opk_`, `oak_` or a key-shaped string. |
| Server | With `base_url` empty, the response has `address.state = not_configured` and no URLs, even with a forged `Host` and `X-Forwarded-Host`. |
| Server | `model_names` equals the `id` set returned by `GET /v1/models` called with a fresh key for that app (mint the key in the test). |
| Server | Another person's `app_id` returns 404. A bearer key returns 401. Automation routes reject members and admins without owner role for `role: owner`, and reject sessions older than 10 minutes, with no side effects. |
| Server | `connection-test` never calls a generation route. Assert the dispatch target set is `{GET /v1/models}` for `api` and `{initialize, tools/list}` for `mcp`. |
| Server | `/.well-known/azizam` contains `links.mcp` and `recipes.mcp.oauth == false`. The headless route inventory (`tests/test_headless_access.py`) and `gateway/owner_openapi_routes.json` include the new member route classification. |
| Component | Create → copy → "I've saved it" → reopen, reload, Back/Forward, second tab: no secret anywhere in the DOM, storage or the URL. Address-not-set, no-models, copy-failed and each check result render their row from §1.5. |
| Browser, 390 px | Member journey: New app → copy key → An AI agent → Claude Code → copy file → Check connection. Owner journey: the same, plus Gateway automation. Screenshots for both (P4, P8). |
| Client matrix (gate per recipe) | On a disposable gateway, each MCP recipe completes `initialize` and `tools/list`, and each app recipe completes `GET /v1/models`, in the named client version. Only then does the recipe get `tested` and its deep link. Never against the live gateway. |

### 1.8 Risks

- **Clients change their configuration formats.** Recipes are data, have a
  tested date, and ship no deep link until tested. Stale recipes stay a
  maintenance cost; the tested date makes the age visible.
- **Recent-sign-in rule for automation keys is new.** UNVERIFIED: whether the
  session carries an authentication time. If it does not, add `auth_time` to
  the session claims (`gateway/admin_oidc.py:919`).
- **Owners pasting automation keys into agents.** The copy and the separate
  section reduce this risk. They cannot prevent it.
- **The MCP origin check trusts `Host` and `X-Forwarded-Host`**
  (`gateway/mcp_server.py:125-136`). This is out of scope here. Record it for
  the security review.

### 1.9 Owner decisions

1. **C1 Create automation keys in the browser?** Recommended default: **yes**.
   Owner role only, a sign-in younger than 10 minutes, one-time reveal, role
   defaults to Admin. The alternative keeps the host command only, which leaves
   owners on a phone without a way to automate.
2. **C2 Which OpenAI-compatible address do snippets show?** Recommended
   default: **`{base}/v1`**. It is what OpenAI-compatible apps expect, and Home
   already uses it. Set discovery `canonical_base_url` to `/v1`. `/openai/v1`
   keeps working.
3. **C3 Claude Desktop and claude.ai connectors (OAuth).** Recommended
   default: **defer**. Show the "not supported yet" line. An OAuth
   authorization server is a separate design with its own threat model (CS
   "Future OAuth").

---

## 2. UI at the root

### 2.1 Current state

- **Mount and order: FACT.**
  - The UI is mounted at `/ui`
    (`proxy.py:13329-13332`, `UIStaticFiles(html=True)`, registered at `:13360`).
  - The mount comes before every router (`include_router` from `:14385` to
    `:16075`).
  - The final route is the catch-all `/{path:path}` (`proxy.py:30188-30193`).
    It handles GET, POST, PUT, DELETE, PATCH and OPTIONS, but not HEAD. It
    authenticates first and then answers JSON 404 `not_found`. It never
    dispatches.
  - So a `Mount("/")` before the catch-all would shadow it, and a mount after it
    would never see a GET (UR "Collision and dispatch analysis" 7).
- **`/ui` is hard-coded in 140 non-test places.** Counts by area:
  - `proxy.py`: 35
  - `gateway/`: 33. Files: `accounts_api.py` 6, `admin_oidc.py` 4,
    `browser_routes.py` 10, `home_api.py` 5, `setup_render.py` 2,
    `api_reference.py`, `drain.py`, `request_source.py`, `ui_static.py`,
    `workflows_catalog.py`, `admin_api.py`.
  - Web, non-test: 32, plus `web/next.config.ts`: 6.
  - Scripts: 21.
  - Docs outside the generated craft-system data: 54.
  
  Tests hold 99 more in `tests/`, and the web tests about 570 more. Full lists
  are in the evidence sweep; the producer table is in §2.6.
- **Owner-page guard.** `_guard_admin_static_ui` (`proxy.py:13298-13317`)
  matches only `/ui/<page>` for `OWNER_UI_PAGES = {engines, providers, people,
  activity, settings}` (`gateway/browser_routes.py:20-22`). `categories` is
  missing from that set, although its page gates on the owner role on the client.
- **Revocation exemptions.** `_account_session_revocation`
  (`proxy.py:15703-15724`) exempts `/ui/sign-in*` and `/ui/invite*`. It decides
  "API or browser" with `"/api/" in path or path.startswith("/gateway/")`. A
  revoked cookie on `/v1/...` therefore gets an HTML redirect, not a 401 (UR 11).
- **Collisions at the root.**
  - `GET /` sends HTML clients to `/ui/home/` and renders the old user portal
    otherwise (`proxy.py:25785`). `HEAD /` (`:18563`) and `HEAD /admin`
    (`:18569`) return 200 for proxy probes.
  - `GET /status` and `/status/` send HTML clients to `/ui/status/` (`:25803`).
  - `GET /setup` (`:17380`), next to `POST /setup/{claim,local-owner,local-login,first-app}`
    (`:17419`, `:17634`, `:17737`, `:17840`).
  - `GET /favicon.ico` returns 204 (`:25826`).
  - The showcase router (`gateway/showcase.py:119-160`, `SHOWCASE_PREFIX =
    "/showcase"` at `:29`) has these routes:
    - `GET /showcase` → 307 to `gallery_ui_url`, which is `/ui/showcase/`
      (`proxy.py:14391`).
    - `GET /showcase/` → 307 to `/showcase`.
    - Public data routes `/showcase/static/{name}`, `/showcase/examples/…` and
      `/showcase/api/status`.
    
    Once the page itself moves to `/showcase/`, the two redirects form a loop,
    and the data routes share the page's first segment. UR marked this overlap
    UNVERIFIED; this sweep confirmed it.
  - `GET /r/{id}` renders the old portal (`:25792`).
  - `GET /firehose` returns 301 (`:25817`).
  - `/me`, `/admin` and `/admin/<section>` are HTML aliases (`:15778`, `:16972`, `:17036-17051`).
- **Next export.** `web/next.config.ts:6-14` sets `output: "export"`,
  `basePath: "/ui"`, `assetPrefix: "/ui"` and `trailingSlash: true`. Each page
  is `page/index.html`. `web/scripts/remove-legacy-polyfills.mjs:21,26` asserts
  `/ui/_next/...` and fails the build without it. `gateway/setup_render.py:325,330`
  bakes hashed font URLs under `/ui/_next/static/media/`.
- **Cache.** `_is_hashed_ui_asset` (`gateway/ui_static.py:206-208`) marks only
  `/ui/_next/static/<hashed>` as immutable for one year. Everything else is
  `no-cache`.
- **Observability and drain.** Firehose skip lists contain `/ui/` and `/ui`
  (`proxy.py:18455-18472`). Drain exempts `/ui` (`gateway/drain.py:41`).
  Request source marks UI traffic by a `/ui/try` or `/ui/home` referer
  (`gateway/request_source.py:55`).
- **OIDC.** The callback is `/admin/oidc/callback` (`proxy.py:17978`). It is
  configured as `admin.oidc.redirect_uri` in YAML and the `oidc_provider_config`
  setting (`gateway/admin_oidc.py:471`, `gateway/admin_runtime.py:55-64`). The
  return-path sanitizer `safe_browser_return_to` (`gateway/admin_oidc.py:105-128`)
  allows only `/ui/*`; its client mirror is `safeContinuePath`
  (`web/src/app/sign-in/auth-api.ts:54-67`).
- **Route inventory test.** `tests/test_headless_access.py:40` exempts exactly
  `Mount("/ui", UIStaticFiles)` and fails on any other opaque mount.

### 2.2 Decision

**D2.1 The UI lives at `/`.** Home is `/`. Pages are `/library/`, `/try/`,
`/apps/` and so on. There are no redirects, aliases or shims for any `/ui` URL,
invite link, bookmark or return path (P11). External contracts keep their
paths: OpenAI, Anthropic, Ollama, A1111, MCP, signed media URLs.
Grounding: UR "Recommendation" and the prior-art synthesis (six of nine
products serve the UI at the root).

**D2.2 No mount. One final dispatcher.** Replace both the `/ui` mount and the
catch-all with one final route, `/{path:path}`, registered last. It handles
GET, HEAD, POST, PUT, PATCH, DELETE and OPTIONS. It calls one classifier:

```text
classify(path) ->
  Reserved(namespace)    # API/protocol namespace or exact API resource
  UiPage(name)           # a page in the export manifest
  UiAsset(path)          # /_next/static/*, or an allowed root file from the export
  Unknown
```

| Class | GET / HEAD | Other methods |
|---|---|---|
| Reserved | Current catch-all behavior: authenticate, then JSON 404 `not_found` (401 when unauthenticated). Never HTML. | Same. |
| UiPage | The owner guard (D2.6). Then serve `page/index.html`: 200, `no-cache`. A slashless path gets a relative 308 to `path/`, query kept. | 405 JSON `{error:{code:"method_not_allowed"}}`, `Allow: GET, HEAD`. |
| UiAsset | File with its own content type and cache rule (D2.7). A missing asset is a plain 404 with no HTML body. | 405, `Allow: GET, HEAD`. |
| Unknown | `404.html` with status 404 when `Accept` includes `text/html`; JSON 404 otherwise. | Same as Reserved. |

Registered API routes still win because they come earlier. The dispatcher
never sees them. Prior art: Immich (the shell skips `/api`, GET/HEAD only, and
requires `text/html`); LibreChat (`apiNotFound` before the SPA); Home Assistant
(page allowlist, GET only) (UR "Prior art" 2-4).

**D2.3 Reserved namespaces are segment-exact.** A path is reserved if its first
segment is one of:

`v1`, `v2`, `openai`, `anthropic`, `api`, `ollama`, `sdapi`, `typesafe`,
`gateway`, `admin`, `me`, `auth`, `internal`, `mcp`, `.well-known`, `docs`,
`redoc`

Also reserved:
- The prefixes `setup/api`, `showcase/api` and `status/raw`.
- The exact resources `/health`, `/openapi.json`, `/llms.txt`, `/rerank` and
  `/robots.txt`.

The list lives in one module, `gateway/route_space.py`. A test derives the
expected set from `app.routes` (§2.8), so a new API family cannot be forgotten.
`/api` stays Ollama's surface. No wholesale private-API rename (UR
"Recommendation").

**D2.4 Pages come from the export manifest.** At boot, read the page list from
`web/out` (directories with `index.html`). The test oracle is the page set from
`web/src/app/**/page.tsx`. Page set after this change:

```text
/  account/  activity/  apps/  categories/  design/  engines/  invite/  library/
models/  people/  providers/  recent/  settings/  setup/  showcase/  sign-in/
sign-in/recover/  status/  try/  usage/  verify/  workflows/
```

- Deleted with no redirect: `home/` (its content becomes `/`), `gallery/`,
  `services/` (BL PF-D301).
- Added: `recent/` from UB §2, `sign-in/recover/` from UB §3, and `verify/`
  from §3.
- A test asserts the page set and the reserved set do not intersect.
- If `web/out` is missing, UI paths answer 503 `text/plain`: "The web app isn't
  built. Run the web build, then restart the gateway." API boot does not fail
  (`tests/test_web_ui_static_mount.py:26` keeps that rule).

**D2.5 Browser entry points move to `/auth/` and old HTML pages are deleted.**
The `/admin` namespace becomes API-only (`/admin/api/*`).

| Today | After |
|---|---|
| `GET /login`, `/admin/login`, `/me/login` | `GET /auth/login` (OIDC start) |
| `GET /admin/oidc/callback` | `GET /auth/oidc/callback` (decision R1) |
| `POST /logout`, `/me/logout`, `/admin/logout`, `GET /me/logout`, `GET /logout` | `POST /auth/logout` |
| `GET /auth/logout-confirm` | unchanged |
| `POST /setup/claim`, `/setup/local-owner`, `/setup/local-login`, `/setup/first-app` | `POST /setup/api/claim`, `/setup/api/local-owner`, `/setup/api/local-login`, `/setup/api/first-app`, next to the existing `GET /setup/api/state` |
| `GET /` portal, `GET /me`, `GET /admin` dashboard, `/admin/<section>` aliases, `/ui/admin/*`, `GET /status` redirect, `GET /setup` redirect, `GET /firehose`, `GET /r/{id}`, `GET /ui`, `HEAD /admin`, `GET /favicon.ico` 204 | Deleted. The dispatcher serves `/`, `/status/`, `/setup/` and the built favicon. |
| `GET /showcase`, `GET /showcase/` (redirects) | Deleted. The dispatcher serves the `/showcase/` page. |
| `/showcase/static/{name}`, `/showcase/examples/…`, `/showcase/api/status` | `/showcase/api/static/{name}`, `/showcase/api/examples/…`, `/showcase/api/status`. The public-path rule (`is_public_showcase_path`, `proxy.py:2431`) and the showcase page's fetches change with them. |
| Receipt links `/r/{id}` | `/usage/?receipt={request_id}`. UNVERIFIED: the Usage receipt sheet does not open from a query parameter yet; add it. |
| `GET /status/raw` | Kept as a registered owner route (decision R2). |

The old server-rendered pages go with their handlers: the user portal
(`gateway/user_render.py`), the admin dashboard (`gateway/admin_render.py`)
and the server setup page (`gateway/setup_render.py`). That also removes the
baked font URLs. UNVERIFIED: whether any renderer has a caller other than these
handlers. Check with LSP references before deleting. If one does (for example
`/status/raw`), keep only that function.

**D2.6 Guards and exemptions key on the classifier.**
- **Owner guard:** for `UiPage(name)` with `name` in `OWNER_UI_PAGES`, run the
  current `ADMIN_AUTH` check. Unauthenticated browsers get a 303 to
  `/sign-in/?return_to=/name/`. Add `categories` to `OWNER_UI_PAGES`. A test
  asserts `OWNER_UI_PAGES` equals the pages the shell gates on the owner role.
  UNVERIFIED: `web/src/lib/product.ts` has no owner flag per destination today;
  add one so the two lists come from one source.
- **Revocation:** the public pages are `sign-in`, `sign-in/recover`, `invite`,
  `setup`, `showcase` and `verify`, plus `/auth/*` and `/setup/api/*`. They
  accept a revoked cookie. Every other path with a revoked cookie gets 401 JSON
  `session_revoked` if it is Reserved, and a 303 to `/sign-in/` if it is a
  UiPage. This replaces the `"/api/" in path` heuristic and fixes `/v1/*`
  getting HTML.
- **Firehose, drain, request source, security headers:** each asks the
  classifier.
  - UiPage and UiAsset hits are never recorded as API traffic.
  - Drain exempts GET and HEAD on UiPage and UiAsset (it rejects only
    POST, PUT and PATCH today).
  - Request source marks a request as from the UI when the referer path
    classifies as `UiPage(try|"")`.
  - Security headers apply to every UiPage response and to the reserved
    browser routes `/auth/*` and `/setup/api/*`. The `_BROWSER_UI_PREFIXES`
    tuple (`proxy.py:14264`) is replaced.

**D2.7 Cache rules.**
- Hashed assets under `/_next/static/` with a hashed filename:
  `public, max-age=31536000, immutable`. Change `_is_hashed_ui_asset` to
  `/_next/static/` (`gateway/ui_static.py:207`).
- HTML and unhashed root files (favicon, icons, web manifest): `no-cache` with
  ETag.
- Auth responses stay `no-store`.

Keep the existing compression, `Vary: Accept-Encoding`, ETag per encoding, 304
and Range behavior in `UIStaticFiles`. The dispatcher calls it as a library
rather than mounting it. Deploy the export and the server together; the
manifest is read at boot, so a chunk mismatch cannot outlive a restart.

**D2.8 OIDC.** The callback becomes `/auth/oidc/callback` (decision R1). Update
the IdP client's registered redirect URI, `admin.oidc.redirect_uri` in config,
and the `oidc_provider_config` setting together, in one deploy step. No old
callback route and no code forwarding (UR "Server-rendered/legacy disposition").
`safe_browser_return_to` and `safeContinuePath`:
- allow a path whose classification is `UiPage`, with its query and fragment;
- reject `//`, backslashes, control characters, schemes and hosts (current
  rules);
- default to `/`;
- drop the `/ui`, `/me` and `/status` mappings.

**Rejected alternatives:**
- Changing the mount path to `/` in place. It shadows every later API route.
- A root mount after the catch-all. GET never reaches it.
- A generic SPA fallback (`index.html` for any miss). API misses become HTML
  200, the Open WebUI counterexample in UR "Prior art" 1.
- Moving every private API under `/api`. That path is Ollama's.
- Keeping `/ui` (LiteLLM does). There is no technical reason, and the owner
  asked for `/`.
- 308 redirects from old `/ui` URLs. P11 forbids them for an unreleased
  product.
- Generalizing the trailing-slash middleware to all paths. It would redirect
  `/v1/models` and `/mcp`.

### 2.3 API, CLI and MCP shape first

- **No external contract changes.** OpenAI (`/v1`, `/openai/v1`), Anthropic
  (`/anthropic/v1`, `/v1/messages`), Ollama (`/api`, `/ollama/api`), A1111
  (`/sdapi/v1`), MCP (`/mcp`, `/.well-known/oauth-protected-resource*`), signed
  media and the gateway API (`/gateway/v1`) keep every path, method and status.
- **Private owner and member APIs** (`/admin/api`, `/me/api`, `/auth/api`)
  keep their paths.
- **Changed browser endpoints:** the `/auth/*` and `/setup/api/*` moves in
  D2.5. They are listed in the owner OpenAPI allowlist where they are owner
  operations, and in the headless route inventory.
- **Invite and reset link producers** return root paths:
  `/invite/#token=…` and `/sign-in/#reset=…` (`gateway/accounts_api.py:242`,
  `:261`, `:269`).
- **Unknown paths:** JSON 404 for headless callers, HTML 404 for browsers. A
  CLI or agent never receives HTML from a reserved namespace.

### 2.4 Data and migration

- **No database schema change.**
- **Configuration:** the OIDC redirect URI moves to `/auth/oidc/callback` (YAML,
  the `oidc_provider_config` setting, the IdP registration). Remove `basePath`
  and `assetPrefix` from `web/next.config.ts`, and update its dev rewrites to
  `/admin/api`, `/me/api`, `/auth`, `/setup/api`, `/gateway/v1`,
  `/.well-known`, `/mcp` and `/v1`.
- **Clean cut:** old `/ui/...` bookmarks, outstanding invite and reset links
  issued with `/ui/` paths, and installed home-screen shortcuts stop working.
  Before deploying, list pending invites and resets
  (`GET /admin/api/invites`). The owner reissues any that are still needed.
  Household members re-add home-screen shortcuts. This is a deliberate P11
  consequence, not a migration.

### 2.5 Deploy steps (owner-run; this design deploys nothing)

1. Build the web export without a base path. Run the full suite and the
   route-manifest tests (§2.8).
2. Update the IdP redirect URI and the gateway OIDC setting to
   `/auth/oidc/callback`.
3. Restart with `scripts/safe-restart`. Run the updated `scripts/post-deploy-smoke`
   (`GET /` → 200 HTML, `GET /v1/models` without a key → 401 JSON,
   `GET /mcp` → 405).
4. UNVERIFIED: whether the owner's reverse proxy probes `HEAD /admin`
   (`docs/REVERSE_PROXY.md:13-17` recommends it). Point probes at `GET /health`
   before step 3. `HEAD /` still returns 200 because it serves Home's headers.

### 2.6 Every `/ui` producer, changed directly

| File | Lines | Change |
|---|---|---|
| `proxy.py` | 13287-13360 | Delete the `/ui` mount, the `GET /ui` redirect and `_web_ui_trailing_slash`. Re-key `_guard_admin_static_ui` on the classifier (D2.6). |
| `proxy.py`, `gateway/showcase.py` | 14391, 2431; showcase 29, 119-160 | Delete `gallery_ui_url` and the two redirect routes. Move the data routes under `/showcase/api/` (D2.5), and update `is_public_showcase_path`. |
| `proxy.py` | 15379, 17182, 17506, 17954, 17961-17972 | Default return target `/`. Sign-in failure goes to `/sign-in/?error=provider`. |
| `proxy.py` | 15703-15724 | Revocation per D2.6. |
| `proxy.py` | 15778-15781, 16972-17051, 17073, 17380-17396, 18112-18143, 25785-25826 | Delete or move per D2.5. Logout `next` is `/sign-in/`. |
| `proxy.py` | 18455-18472 | Firehose skip asks the classifier. |
| `proxy.py` | 18563-18574 | Delete `HEAD /` and `HEAD /admin`. The dispatcher answers `HEAD /`. |
| `proxy.py` | 30188-30193 | Replace the catch-all with the dispatcher (D2.2). Add HEAD. |
| `gateway/accounts_api.py` | 87, 198, 205, 242, 261, 269 | `/auth/login?return_to=/invite/`, `next: "/"`, `next: "/sign-in/"`, `/sign-in/#reset=`, `/invite/#token=`. |
| `gateway/admin_oidc.py` | 105-128 | Sanitizer per D2.8. |
| `gateway/browser_routes.py` | 8-56 | Delete `ADMIN_SECTIONS` and `admin_destination`. Keep `OWNER_UI_PAGES` (+`categories`), `accepts_html` and `with_query`. |
| `gateway/home_api.py` | 412, 567-570 | Receipt `/usage/?receipt=`. Checklist hrefs `/engines/`, `/#connect` and `/people/`. |
| `gateway/api_reference.py` | 44 | "Open gateway" → `/`. |
| `gateway/drain.py` | 41 | Classifier (D2.6). |
| `gateway/request_source.py` | 55 | Classifier (D2.6). |
| `gateway/ui_static.py` | 207 | `/_next/static/`. |
| `gateway/workflows_catalog.py` | 203 | `try_url` `/try/?workflow=`. |
| `gateway/setup_render.py` | 325, 330 | Deleted with the server setup page (D2.5). |
| `gateway/admin_api.py` | 1782 | Docstring. |
| `web/next.config.ts` | 4-37 | No `basePath` or `assetPrefix`; rewrites per §2.4. |
| `web/scripts/remove-legacy-polyfills.mjs` | 21, 26 | Match `^/_next/static/chunks/polyfills-…`. |
| `web/src/lib/product.ts` | 5-11 | Delete `/ui` stripping and the `/services` and `/home` aliases. Add an owner flag per destination. |
| `web/src/components/shell/app-shell.tsx` | 118-133, 646 | Return paths without `/ui`. `publicPages` adds `verify` and `sign-in/recover`. |
| `web/src/app/sign-in/auth-api.ts` | 54-67 | Client sanitizer mirrors D2.8. |
| Other `web/src` | `account-menu-content.tsx:58,132`, `error-state.tsx:52`, `sign-in/page.tsx:40,156`, `invite/page.tsx:139,161,328,343`, `home/page.tsx:1358`, `account/page.tsx:66`, `activity/page.tsx:368`, `activity/requests.tsx:342`, `engines/engine-checks.tsx:316`, `library/page.tsx:2536`, `models/models-client.tsx:410,430,1034`, `showcase/page.tsx:200`, `status/status-view.tsx:306`, `try/page.tsx:2444`, `usage/page.tsx:348,823`, `darshana.json:3,9` | Root paths. Delete `gallery/`, `services/`, `home/`. |
| Scripts | `scripts/post-deploy-smoke:33`, `scripts/security/exposure_probe.py:23`, `scripts/gen_support_matrix.py:37`, `scripts/ui/check-cutover-container.py:74-88`, `scripts/ui/craft_numbers.json:4-17` | Root paths. Delete the cutover checker if it only checks the old `/ui` cutover. |
| Docs | `docs/design/UI_CUTOVER.md`, `docs/guides/FOR_MEMBERS.md`, `docs/guides/OPERATOR.md`, `docs/ops/HEADLESS_ACCESS.md:166`, `docs/ops/SAFE_RESTART.md:44`, `docs/REVERSE_PROXY.md:13-17`, `docs/PEOPLE_AND_ACCOUNTS.md`, `docs/FIRST_RUN_SETUP.md`, `docs/CORE_CONTRACTS.md`, `docs/CONFIG_SOURCE_OF_TRUTH.md`, `AUTH_GUIDE.md:183`, `web/README.md:4,35` | Root paths, `/auth/oidc/callback`, `GET /health` probes. Mark `UI_CUTOVER.md` as historical, or delete it. |
| Tests | the 17 files in `tests/` and about 129 web test files with `/ui` | Update expectations. Generated craft-system JSON under `docs/design/craft-system/` is regenerated, not hand-edited. |

**Completion check (P22, AGENTS rule 5):** after the change, `rg -n '/ui\b'`
over `proxy.py gateway web/src web/scripts web/next.config.ts scripts docs tests`
must return nothing, except files listed in an explicit allowlist of historical
records. A test runs this check (§2.8).

### 2.7 Risks

| Risk | Severity | Control |
|---|---|---|
| A page shadows an API route, or an API miss becomes HTML | critical | The dispatcher runs last. Reserved classification comes first. Manifest tests in §2.8. |
| An owner page loses its guard | high | Classifier-keyed guard, the `categories` addition, and a parametrized test over every owner page and role. |
| Setup or sign-in loops | high | Tests for each public page with a revoked cookie, with no cookie, and as owner, member and anonymous. |
| An unaudited router claims a path the dispatcher assumes (UR "Imported registration coverage boundary") | high | The reserved-set test is derived from `app.routes` after all routers are included, not from a hand list. |
| Stale links, IdP mismatch, broken home-screen shortcuts | medium | Deploy steps 2 and 3. A pre-deploy list of pending links. Household notice. |
| Lost immutable caching, or chunk drift | medium | Cache test. Export and server deployed together. |
| UI clicks counted as API usage | medium | Firehose classifier test. |

### 2.8 Tests (route manifest and behavior)

Build the app with a fixture export tree whose pages equal the
`web/src/app/**/page.tsx` set, plus one hashed chunk, one unhashed root file
and `404.html`.

1. **Reserved set is complete.** For every route in `app.routes` except the
   dispatcher: its first segment, or its exact path, is in the reserved
   namespaces or exact resources. Fails when a new API family is added without
   reserving it.
2. **No collision.** The page set and the reserved set are disjoint. No
   registered route path equals a page path. There are no `Mount` objects in
   `app.routes`. This replaces `test_only_named_ui_static_mount_is_exempt` in
   `tests/test_headless_access.py:104`, and the fixture
   `headless_non_control_routes.json:103` loses `GET /ui`.
3. **Order.** The dispatcher is the last route and no other route has the path
   `/{path:path}`.
4. **API errors stay API errors.** Request matrix:
   - Paths: for each reserved namespace, one known path, one unknown sub-path
     and the bare prefix.
   - Methods: GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS.
   - `Accept`: `text/html`, `application/json`, `*/*`.
   - Auth: with and without credentials.
   
   No response has `content-type: text/html`, except the allowlist `/docs`,
   `/redoc`, `/status/raw` and `/auth/logout-confirm`. Status codes for known
   API routes equal the r17 baseline, recorded before the change.
5. **Protocol contracts.**
   - `POST /mcp` works. `GET` and `DELETE /mcp` give 405 with `Allow: POST`.
     Protected-resource metadata is unchanged.
   - Ollama `HEAD /api/blobs/{d}` is unchanged.
   - Realtime WebSockets are unchanged.
   - `OPTIONS` on `/gateway/v1/providers/{p}/{path}` is unchanged.
   - SSE streaming and signed media Range requests are unchanged
     (`tests/test_catchall_never_dispatches.py` keeps passing).
6. **Pages.**
   - Each page: GET and HEAD → 200 HTML, and HEAD has no body with a
     consistent `content-length`.
   - Slashless → relative 308 with the query kept.
   - POST → 405 with `Allow: GET, HEAD`.
   - Unknown page → 404 HTML. Unknown asset → 404, not HTML.
   - Traversal and encoded-slash paths cannot leave the export root.
7. **No redirect loops.** For every page, follow redirects from both slash
   forms, with at most 2 hops, and end at 200. `/showcase` and `/showcase/`
   are named cases.
8. **Guards.** Each owner page as anonymous, member, admin and owner, and with
   a revoked cookie. Each public page with a revoked cookie gives 200, not a
   loop. A revoked cookie on `/v1/models` gives 401 JSON.
9. **Cache.** Hashed `/_next/static` → immutable. HTML → `no-cache`.
   br/gzip/identity, `Vary`, ETag/304, Range and HEAD behave as in
   `tests/test_ui_static_serving.py`, with the paths moved.
10. **Return paths.** Safe page paths with query and fragment pass. `//evil`,
   `https://…`, `/v1/models`, `/admin/api/x` and `/ui/home/` fall back to `/`.
   The server and client sanitizers agree on a shared fixture table.
11. **Stale-string test.** The `rg` check in §2.6 runs in CI.
12. **Missing export.** No `web/out` → API boots, UI paths give 503 plain text,
    and API paths are unchanged.

### 2.9 Owner decisions

1. **R1 Move browser auth to `/auth/`, including the OIDC callback
   `/auth/oidc/callback`?** Recommended default: **yes, in the same deploy**.
   It needs one IdP redirect-URI change. The alternative keeps
   `/admin/oidc/callback` and `/admin/login`. They work, but leave member
   sign-in under the owner's API namespace.
2. **R2 Keep the technical status page `/status/raw`?** Recommended default:
   **keep it as an owner-only registered route** until the Status page covers
   its diagnostics. Delete it in a later P11 pass with a named replacement.

---

## 3. Outgoing mail

### 3.1 Current state

- **The gateway sends no mail.** There is no SMTP code anywhere. The docs say
  this is deliberate (`docs/PEOPLE_AND_ACCOUNTS.md:177`, `README.md:101`).
- **Recovery today.**
  - The sign-in page says "Forgot your password? Ask the person who invited you
    for a reset link." as plain text (`web/src/app/sign-in/page.tsx:247-252`,
    BL PF-D601). A web test pins it (`web/e2e/people.spec.ts:484`).
  - Owner-issued reset: `POST /admin/api/people/{id}/reset`
    (`gateway/accounts_api.py:236-242`). Default lifetime is 1 hour; the UI
    hard-codes 3600 (`web/src/app/people/people-api.ts:73`).
  - Reset tokens are `secrets.token_urlsafe(32)`, stored as SHA-256 and
    returned once (`gateway/accounts.py:252`, `:267`). A new reset revokes
    unused older ones (`:265`).
  - Redemption (`gateway/accounts.py:355-371`) changes the verifier, deletes
    all sessions and marks the link used, in one transaction.
- **Invites.**
  - 7-day default; 60 s to 30 days allowed (`gateway/accounts.py:248`).
  - Tokens are HMAC-SHA256 of `gateway.invite.v1:` + `link_id` under the
    session signing key, so they can be recomputed (`gateway/accounts.py:464-474`).
  - Stored as SHA-256 and single-use (`:274-281`, `:317`, `:346`).
  - Links are relative fragments: `/ui/invite/#token=` and
    `/ui/sign-in/#reset=`. The fragment never reaches the server, a log or a
    `Referer`.
- **No address is verified.** `users.email` is nullable and unverified
  (`gateway/admin_store.py:83-92`). Only OIDC sign-ins record
  `email_verified` (`gateway/accounts.py:45-47`). The owner's "email" can be
  any username (`proxy.py:17529-17530`, `:17673`).
- **Throttle.** `accounts.throttle` (`gateway/accounts.py:192-205`) allows 60
  per IP and 10 per "email" bucket per 15 minutes. Reset redemption passes the
  literal string `"reset"` as the email (`gateway/accounts_api.py:203`). So all
  clients share one bucket of 10 redemption attempts per 15 minutes. That is a
  cheap lockout of every reset. It is a defect regardless of mail.
- **Secret store: FACT.** `gateway/secret_store.py` is AES-256-GCM with a
  0600 key file next to `admin.db`. It is write-only through the API and
  returns status plus the last 4 characters. Its names are provider-specific
  (`provider_secrets`, `put_provider_secret`). `approved_origin` must be
  HTTP(S) or `None` (`:232-247`, `:84`).
- **Settings.** `operator_settings` in `admin.db`, with per-key revisions,
  `If-Match`, and the audit in the same transaction
  (`gateway/admin_store.py:2312-2384`, `gateway/settings_api.py:301-349`).
- **Background work.** There is no durable outbox. The process is one uvicorn
  worker (`proxy.py:30279`). Existing patterns: a periodic
  `asyncio.to_thread` loop (`proxy.py:21017-21029`) and startup resume of
  unfinished jobs (`gateway/me_make.py:273-295`).
- **Dependencies.**
  - Python 3.12 (`Dockerfile:10`).
  - Hashed lock `tests/requirements-hermetic.lock`.
  - `email-validator 2.3.0` and `jinja2 3.1.6` are locked, but only as
    transitive dependencies.
  - `aiosmtplib` and `aiosmtpd` are absent.

### 3.2 Decision

**D3.1 Mail is an optional capability that the owner turns on.** The gateway is
complete without it. When mail is off, every flow keeps its link-sharing path
(Gitea gates reset on mail; Immich and Home Assistant keep assisted recovery;
OM §5, PR "Recovery entry-point states").

**D3.2 Transport: one SMTP relay, through the standard library.** The real
complexity of mail is deliverability: DKIM signing, SPF and DMARC alignment,
IP reputation, bounces and complaints. That is outsourced to a relay service
that the owner already has or chooses (an ISP, Fastmail, Amazon SES SMTP,
Postmark, Resend or Mailgun all accept SMTP; OM "Provider SDKs versus SMTP").
Azizam only hands the relay a correct message over TLS. No direct-to-MX
sending and no local MTA fallback (OM "Disagreements": Plausible and Ghost).

No provider SDK. SMTP reaches all four providers that OM evaluated, so a test
sink receives exactly the same MIME as production. An SDK adds a dependency
per provider, and each SDK reports only acceptance (OM "Provider SDKs" table,
INFERENCE). Revisit only if a needed feature (signed bounce webhooks) has no
SMTP path.

**D3.3 Library stack (the answer to "no-brainer libraries").** The owner's bar
is libsodium: widely deployed, small API, hard to misuse, maintained, a clean
security record. For the whole problem, the honest answer is that no Python
package clears that bar. OM's conclusion: "no evaluated package safely
supplies the entire account-recovery lifecycle." The pieces that do clear it
are below.

| Piece | Choice and version | Why it clears the bar | Rejected |
|---|---|---|---|
| Message construction | `email.message.EmailMessage` with `email.policy.SMTP`, CPython 3.12 stdlib | It ships with Python and is patched with Python. With the modern policy, a header value containing CR or LF raises an exception: CRLF header injection fails closed. It handles MIME, encodings and `Message-ID`. | Hand-built MIME strings |
| Transport | `smtplib` (stdlib, CPython 3.12) in the outbox worker thread | No new dependency. Explicit `SMTP_SSL` or `starttls()` with `ssl.create_default_context()` verifies the certificate and host name. `starttls()` raises when the server does not offer it, so a required STARTTLS cannot silently downgrade. Use `send_message`, never raw `sendmail` with built strings. Timeouts are explicit. The worker is a thread (D3.6), so blocking I/O is correct. | `aiosmtplib 5.1.3`: one listed maintainer, and three injection advisories in 2026 (CVE-2026-53533, CVE-2026-55558, GHSA-chr7-p3h9-q87h). Its STARTTLS default is opportunistic. It is not libsodium-level, and the worker does not need async (OM "Security floor"). `fastapi-mail 1.6.8`: a wrapper over the same pieces that adds a layer and no lifecycle (OM table). |
| Address syntax | `email-validator 2.3.0`, promoted to a direct, pinned dependency in `requirements.txt` and the hermetic lock | Unlicense, small API, the validator behind pydantic's `EmailStr`, no published advisories (OM table; not a guarantee). It normalizes and checks syntax. DNS checks run only when an address is saved, never on a recovery request (avoids timing differences). | DNS/MX checks on every request; the regex in `normalize_email` (`gateway/accounts.py:77-81`) as the only check |
| Message text | Plain text only, from fixed Python templates with `str.format_map` over validated values | No template engine, no HTML and no escaping surface. Plain-text links are clickable in common mail apps. Fewer moving parts than Jinja2 autoescape (OM table: Jinja2 autoescape is off by default, and 3.1.3-3.1.6 had sandbox fixes). | Jinja2 HTML templates in v1. Revisit HTML only on a demonstrated need. |
| Secret at rest | The existing `gateway/secret_store.py` (AES-256-GCM via `cryptography 46.0.7`) | Already reviewed and tested (`tests/test_secret_store.py`). `cryptography` is the libsodium-level library here. | Plaintext `operator_settings`; a new store |
| Test sink: integration and preview | Mailpit 1.31.5, pinned, a container bound to `127.0.0.1`, no relay configured | MIT, REST API for assertions, chaos testing. Use only behind loopback: its 2026 advisories (SSRF, origin checks) matter when exposed (OM table). Never in production. | A real relay in CI |
| Test sink: failure injection | `aiosmtpd 1.4.6`, a test-only dependency | The maintained replacement for the removed stdlib `smtpd`. Programmable 4xx/5xx replies, disconnects and STARTTLS behavior. 1.4.5 and 1.4.6 fixed SMTP smuggling and STARTTLS injection (OM table). Bound to loopback in tests. | Hand-written socket fakes |
| Later, not v1 | Apprise 2.0.1 for non-account channels (ntfy, Matrix) | Too broad a boundary for account mail (redirect advisory, secrets in URLs; OM "Apprise"). | |

**D3.4 Messages in v1.** Six messages. Each one has a reason grounded in the
research:

1. **Invitation:** delivery of an existing owner action.
2. **Password reset link:** the owner's self-service ask (PR "Answer").
3. **Confirm recovery email:** without it, mail cannot be sent to an
   unverified address (OM §4, NIST 800-63B-4: recovery addresses must be
   verified).
4. **Password changed notice:** OWASP Forgot Password "notify the user".
5. **Recovery email changed notice, to the old address:** OWASP
   Authentication, on address changes.
6. **Test email:** setup only.

Completion ("done") emails are **not** in v1. UB §2 delivers completion in the
app first. HEADLESS_AND_RESOURCES says "none until asked" (decision M3).

**D3.5 Recovery consumes mail through one state.** The mail module computes
`delivery_state ∈ {off, ready, unavailable, unknown}`. UB §3 publishes it
unchanged as `recovery.email_delivery` in `GET /auth/api/options`. `ready`
requires all of:
- mail enabled;
- a configured public address (`instance_presentation.base_url`);
- a relay password that resolves;
- an accepted test send for the current configuration revision;
- no permanent transport failure since that test.

**D3.6 Durable outbox in `admin.db`, one worker thread.** Each account change
and its outbox row commit in the same transaction, because accounts and mail
share `admin.db`. The worker is a lifespan loop using the
`_prompt_memory_maintenance_loop` pattern (`proxy.py:21017`). It claims rows
with a lease, so a restart resumes them. FastAPI background tasks alone are not
enough (OM "Durable delivery").

**D3.7 The outbox holds no secrets and no bodies.** Reset and verification
tokens become HMAC-derived from the link id, as invites already are. The
worker recomputes the token at send time from `link_id`, renders the text and
sends it.

The outbox stores purpose, `link_id`, recipient snapshot and state. It never
stores a token or a rendered body. A retry sends the same link, so duplicates
are harmless (OM: "Preserve one logical token/message on retry").

Single use stays enforced by the stored hash and `used_at`. Nothing is added
to recover a reset link after creation. A key rotation invalidates unsent and
unused links; that is acceptable and documented.

Rejected alternatives:
- Encrypting rendered bodies in the outbox: it adds key handling and wipe
  logic.
- Sending inline after commit: crash loss, and SMTP latency only for real
  accounts makes enumeration possible.

**D3.8 Owner-visible, honest states.** The capability states are:

- `Off`
- `Not tested`
- `Testing`
- `Ready` ("Accepted by the mail server {time}")
- `Failing` ("Last send failed {time}: {layer}")

A recipient's "I got it" is recorded separately and is optional. SMTP 250
means the relay accepted the message, not that it reached the inbox; the copy
says so (OM "Test send").

### 3.3 API, CLI and MCP shape first

All owner routes are under `/admin/api/mail`, owner role, CSRF for sessions,
`If-Match` for writes. They go into `gateway/owner_openapi_routes.json`, the
`docs/ops/HEADLESS_ACCESS.md` table and the route inventory test
(`tests/test_headless_access.py:60-66`).

```text
GET  /admin/api/mail
  200 {
    enabled: bool,
    state: "off"|"not_tested"|"testing"|"ready"|"failing",
    config: {host, port, security: "tls"|"starttls"|"none",
             username, from_address, from_name, reply_to, timeout_s},
    password: {source: "secret"|"env"|"unset", state: "set"|"unset"|"missing", suffix?},
    public_address_set: bool,
    last_test: {id, at, to, outcome, layer, smtp_code?, config_revision, received_at?} | null,
    last_failure: {at, purpose, layer, smtp_code?} | null,
    revision
  }
  ETag: revision

PUT  /admin/api/mail                      If-Match required (428 / 409 as settings_api)
  body: {enabled, host, port, security, username, from_address, from_name, reply_to, timeout_s}
  - enabled:true is refused with 409 mail_not_tested unless last_test.outcome == accepted
    for the resulting config revision. Any config change resets state to not_tested and
    sets enabled:false in the same transaction. Audit: mail.config.update.
  - security "none" is accepted only when host is a loopback literal (127.0.0.0/8, ::1)
    or "localhost" (a local relay or test sink). Otherwise 400 mail_tls_required.

PUT    /admin/api/mail/password  {password}            write-only; 204; audit mail.password.set
PUT    /admin/api/mail/password  {env: "VAR_NAME"}     reference instead of stored value
DELETE /admin/api/mail/password                        audit mail.password.clear

POST /admin/api/mail/test  {to}
  - Synchronous, bounded by timeout_s (default 20, max 60). Rate limit 5 per hour per gateway.
  - to: validated with email-validator; any address (owner-only, audited mail.test.send with
    the domain only, not the full address).
  200 {id, outcome: "accepted"|"failed"|"unknown", layer, message, details: {smtp_code,
       smtp_reply_redacted, tls_version, at}, config_revision}
POST /admin/api/mail/test/{id}/received                 records received_at (optional)

GET  /admin/api/mail/deliveries?cursor=&purpose=&state=
  Owner history: {id, purpose, recipient, state, attempts, created_at, finished_at, layer, smtp_code}.
  Recipient is shown to the owner (it is their household). No tokens, no bodies.
```

**Changes to existing owner routes:**
- `POST /admin/api/invites` gains `send_email: bool`. The default is `true`
  when mail is `ready` and `email` is present (decision M2). The response adds
  `delivery: {id, state} | null`. The `url` is still returned, so the owner can
  share it another way.
- `POST /admin/api/people/{id}/reset` gains `send_email: bool`. The message
  goes only to that person's verified recovery address. With
  `send_email: true` and no verified address, the route returns 409
  `no_recovery_address`. The URL is still returned, so the owner can share it.

**Public and member (the UB §3 contract, delivered by mail):**
- `GET /auth/api/options` → `recovery.email_delivery` = `delivery_state`
  (D3.5).
- `POST /auth/api/password-reset/request {email}`.
  - The response is always 202 with `{}` while state is `ready`, and 503
    `recovery_delivery_unavailable` for every request when it is not.
  - An outbox row is written only for a local, enabled account whose
    **verified** recovery address matches.
  - Every request runs the same throttle writes, so database work is
    near-identical (§3.7).
- `POST /me/api/recovery-address/verification {address}` (session plus CSRF,
  sign-in within the last 10 minutes) starts verification.
- `POST /auth/api/recovery-address/verify {token}` completes it. Amendment to
  UB in §3.10.
- `DELETE /me/api/recovery-address` removes it, with a notice to that address.

**CLI:** the owner routes are OpenAPI operations, so Restish covers them
(`docs/ops/HEADLESS_ACCESS.md:72`). Example: `restish azizam mail-test --to me@example.com`.
The host-only recovery command is UB §3's `azizam account recover-owner` and
does not depend on mail.

**MCP:** not supported, explicitly. Mail configuration and account recovery do
not belong on an inference connection (UB §3; HEADLESS_AND_RESOURCES).

### 3.4 Messages (exact text)

Common rules for every message:
- Plain text, UTF-8.
- `From: "{from_name}" <{from_address}>`. `from_name` defaults to the instance
  name.
- `Reply-To` is set only when configured.
- Headers: `Auto-Submitted: auto-generated` and
  `Message-ID: <{outbox_id}@{from_domain}>`.
- No tracking pixels, no rewritten links, no prompt text, no creations.
- Links use the configured public address, never `Host`. Tokens travel in the
  URL fragment, so a link-scanner GET never sends the token and cannot consume
  it. Redemption is always a POST.
- `{instance}` is the instance name. Names are stripped of control characters
  and capped at 80 characters before use.

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

This link works once and expires in 30 minutes.
If you didn't ask for this, ignore this email. Your password stays the same.
```

**3. Confirm recovery email.** Subject: `Confirm your recovery email for {instance}`

```text
Confirm that password reset emails for {instance} can come to this address:
{base}/verify/#token={token}

This link expires in 24 hours.
If you didn't ask for this, ignore this email.
```

**4. Password changed.** Subject: `Your {instance} password was changed`

```text
The password for your {instance} account was changed on {datetime} UTC.
You were signed out of other browsers. App keys keep working.

If you didn't do this, tell the person who manages {instance} now.
```

**5. Recovery email changed** (to the old address). Subject: `Your {instance} recovery email was changed`

```text
Password reset emails for your {instance} account now go to a different address.
This address will no longer receive them.

If you didn't do this, tell the person who manages {instance} now.
```

**6. Test.** Subject: `Test email from {instance}`

```text
This is a test email from {instance}.
If you can read it, {instance} can send email through your mail server.
```

### 3.5 Behavior when mail is off

| Flow | Mail off (or not `ready`) | Mail `ready` |
|---|---|---|
| Forgot password | UB copy: "This Azizam doesn't send password reset emails. Ask the person who manages it for a reset link." | UB email form and uniform reply |
| Owner reset | "Create link" → copy link (current) | Same, plus "Email it to {verified address}" when the person has one |
| Invite | Copy link (current) | "We'll email this invitation to {email}." Copy link remains |
| Account → Recovery email | "This gateway doesn't send email. If you forget your password, ask the owner for a reset link." No input field. | Add, confirm or change the address |
| Password changed | In-app security event at next sign-in (UB §3) | Email to the verified address, plus the in-app event |
| Mail breaks after it was ready | State `failing`. Public options report `unavailable`. Queued recovery mail stops being enqueued; owner-visible failure on Settings → Email and the setup checklist. Unrelated gateway work continues (P3, P18). | |

### 3.6 Settings, setup, and test-send flow

**Where:** Settings → **Email** (owner page). Also one optional card at the
end of first-run setup: "Email (optional): Send password reset links and
invitations by email." Actions: "Set up email" and "Skip for now". The setup
checklist (`gateway/home_api.py:533-579`) gains an optional step `email`,
"Set up email", with the reason "Lets people reset their own password."

**Form copy:**
- Section title "Email". Off state: "Email is off. Password resets and
  invitations are shared as links." / "Set up email".
- Fields:
  - "Mail server" (host)
  - "Port", default 587, or 465 for TLS
  - "Security": "STARTTLS (usually port 587)" or "TLS (usually port 465)"
  - "Username"
  - "Password" (write-only)
  - "From address"
  - "From name" (placeholder: instance name)
- Technical details: "Reply-to address", "Connection timeout", "Read the
  password from an environment variable".
- Password once saved: "Saved · ends in {suffix}" / "Replace" / "Remove". A
  blank field means unchanged (OM "Separate transport configuration").
- If the public address is unset: "Set this gateway's public address first.
  Email links use it." / "Set public address". "Send test email" stays
  disabled with that reason.
- Actions: "Save", then "Send test email" (field "Send to", prefilled with the
  owner's verified recovery address if any). Busy "Sending test…".
- After an accepted test: "The mail server accepted the test at {time}. Check
  your inbox and spam folder." / "I got it" / "Turn on email". The switch
  stays disabled until a test for the current settings is accepted. Its
  reason: "Send a test with these settings first."

**Test-send result copy.** Each result names its layer; the protocol detail
goes under Details (OM "Test send", adapted to VOICE rules 1 and 4):

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
| config | "The password couldn't be read. Enter it again." (secret missing or the environment variable is unset) |
| accepted | "The mail server accepted the test. Check your inbox and spam folder." |

Details: SMTP code, the redacted server reply (the username and any
password-like token are removed), TLS version, time and configuration
revision. Never a traceback.

**Member: Account → Recovery email:**
- Empty: "Add an email for password resets." / field "Email address" / "Send
  confirmation".
- Pending: "Check {address} for a confirmation link. It expires in 24 hours." /
  "Send again" / "Cancel".
- Verified: "Password reset emails go to {address}." / "Change" / "Remove".
- Changing keeps the old address active until the new one is confirmed.

**Verify page `/verify/`** (public):
- Busy: "Confirming your email…"
- Success: "Your recovery email is confirmed." / "Go to {instance}".
- Failure: "This confirmation link no longer works." / "Send a new one from
  Account" (signed in) or "Back to sign in" (signed out).

### 3.7 Security rules

**Tokens:**
- 256-bit, carried in the URL fragment.
- Reset and verify tokens are HMAC-SHA256 under a key derived with HKDF from
  the session signing key, with labels `azizam.reset.v1` and `azizam.verify.v1`
  (invites keep `gateway.invite.v1`).
- Stored as SHA-256 only.
- Bound to the person, the purpose, and for verify the normalized address.
- Single use: consumption and the state change commit atomically. Concurrent
  redemptions give exactly one success (the `BEGIN IMMEDIATE` pattern in
  `gateway/accounts.py:317`).

**Lifetimes:**
- Reset: 30 minutes for mailed links; maximum 24 hours (UB §3; NIST caps
  emailed codes at 24 hours).
- Owner handover reset: as UB §3 decides.
- Verify: 24 hours.
- Invite: 7 days (current), owner-revocable.

**Rate limits.** These are new throttle buckets, not the `"reset"` literal:

| Action | Limit |
|---|---|
| Request reset | Per normalized address: 1 per minute and 5 per hour. Per client IP: 20 per hour. This includes nonexistent addresses. |
| Redeem reset or verify | Per IP: 30 per 15 minutes. Per link-id hash: 10 per 15 minutes. **Fix:** replace the global `"reset"` bucket (`gateway/accounts_api.py:203`). |
| Start verification | Per person: 3 per hour. |
| Test send | 5 per hour per gateway. |
| All mail | Global budget of 60 messages per hour. Above the budget, rows wait in the outbox. Recovery requests still return 202 and the owner sees "Email is delayed: the hourly limit was reached." |

Requests never lock an account (OWASP).

**No enumeration.** The public request returns the same status, body and
headers for existing, absent, unverified, disabled, OIDC-only and throttled
identities. It does the same throttle writes in every case; only the outbox
insert differs. The send happens later in the worker, so SMTP and DNS latency
never reach the response. Residual: one extra SQLite insert for eligible
accounts. A test measures it (§3.9). Per-recipient delivery failures are
visible only to the owner. The public state reports only global outages (PR
"Recovery entry-point states").

**Redemption.**
- A successful reset revokes sessions and outstanding reset links, keeps app
  keys, and does not sign the person in (current behavior plus UB §3).
- It sends message 4 to the verified address and records the in-app event.
- It never re-enables a disabled person and never changes a role.

**Relay secret.**
- Stored in the secret store under id `mail:smtp:password`, with
  `approved_origin=None`, or as an env reference `{env: "VAR"}` resolved at send
  time.
- Never in `operator_settings`, never in exports.
- `GET /admin/api/config/export` shows `password: {source, state}` only.
- Redact it from errors, audit metadata and logs.
- Generalize the store's names, since it now holds a non-provider secret:
  - `provider_secrets` becomes `secrets`;
  - `put_provider_secret` and `get_provider_secret` become `put_secret` and
    `get_secret`;
  - the provider id convention `provider:<id>:api_key` stays.
  
  This is one table rename in the release migration, with no dual-read (P11).
  UNVERIFIED: other readers of the table name outside `gateway/`. Check with
  grep before the rename.

**Transport safety.**
- `security` must be `tls` or `starttls`. `none` is allowed only for loopback.
- Certificates are verified with the system trust store. A custom CA file can
  be set in Technical details. There is no "ignore certificate errors" switch
  (OM "Disagreements").
- Envelope sender and recipients come only from saved configuration and
  account records. Never from request bodies, job output or model text.

**Audit.** These events record the domain and outbox id, never a full token,
password or message body:
- `mail.config.update`, `mail.password.set`, `mail.password.clear`
- `mail.test.send`
- `mail.send.failed` (permanent failures)
- `account.recovery_address.verify_start`, `account.recovery_address.verified`,
  `account.recovery_address.removed`

### 3.8 Data and migration

All additive in `admin.db`, created with `CREATE TABLE IF NOT EXISTS` like the
account tables (`gateway/accounts.py:28-51`).

```text
mail_outbox(
  id TEXT PRIMARY KEY,               -- also the Message-ID local part
  purpose TEXT NOT NULL,             -- invite | reset | verify | password_changed | address_changed | test
  link_id TEXT,                      -- account_links.id for invite/reset/verify
  user_id TEXT,
  recipient TEXT NOT NULL,           -- normalized address snapshot
  state TEXT NOT NULL,               -- queued | sending | accepted | retrying | failed | expired | unknown
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at INTEGER NOT NULL,
  not_after INTEGER NOT NULL,        -- link expiry; never send after it
  lease_until INTEGER,
  config_revision TEXT,
  layer TEXT, smtp_code INTEGER,
  created_at INTEGER NOT NULL, finished_at INTEGER)

account_recovery_addresses(
  user_id TEXT PRIMARY KEY,
  address TEXT NOT NULL,             -- verified address; UNIQUE (decision UB-1: one per person)
  verified_at INTEGER NOT NULL,
  pending_address TEXT, pending_link_id TEXT, pending_since INTEGER)
```

- `account_links.kind` gains `verify`. The existing `CHECK` is in code
  (`create_link`, `gateway/accounts.py:246`); extend it there.
- Mail configuration is the `operator_settings` key `mail`, with the existing
  revision table.

**Worker rules:**
- Claim: `UPDATE … SET state='sending', lease_until=now+120 WHERE id=? AND state IN ('queued','retrying') AND next_attempt_at<=now`.
  On boot, a `sending` row with an expired lease returns to `retrying`.
- Retry on 4xx, connection errors and `unknown` with backoff of 1, 5 and 15
  minutes plus jitter, at most 4 attempts, never after `not_after`.
- A 5xx reply for the recipient or sender is `failed` with no retry.
- An `auth` or `tls` failure is `failed`, and moves the capability to
  `failing`.
- An expired row becomes `expired` and is never sent.

**Existing data:**
- Every existing `users.email` stays unverified. Nothing is marked verified
  by migration, invite acceptance or OIDC claims (UB §3).
- No bulk email when mail is first turned on (decision M1). Existing reset
  and invite links are unchanged.

**Retention:** outbox rows are delivery receipts. Keep them under the receipt
retention rules (P19); the owner chooses deletion. They contain no secrets or
bodies.

**Dependencies:**
- `email-validator==2.3.0` becomes direct in `requirements.txt`, already in
  the lock.
- `aiosmtpd==1.4.6` is added to the test requirements and the hermetic lock
  with hashes.
- Mailpit 1.31.5 is a pinned container image used only by the e2e harness.

**Docs:** `docs/PEOPLE_AND_ACCOUNTS.md:162` and `:177` and `README.md:101`
change from "no mail" to "optional mail", and `CONFIG.md` gains the mail
settings.

### 3.9 Tests

| Level | Fixture and oracle |
|---|---|
| Unit | Message builder: CR/LF in names, subject or addresses raises before any network. Header and MIME snapshots for all six messages. Links use the configured address under forged `Host` and `X-Forwarded-Host`. No token in any log record (capture logs). |
| Unit | Token derivation: purpose confusion (a reset token on verify fails); wrong person or address fails; expiry boundary ±1 s; replay fails; two concurrent redemptions give exactly one success. |
| SMTP (aiosmtpd, loopback) | Implicit TLS with a test CA; STARTTLS required and offered; STARTTLS required and not offered → `starttls_missing` with no plaintext AUTH sent; wrong CA or host name → `tls`; 535 → `auth`; 550 on RCPT → `recipient` with no retry; 451 → `retrying` then `accepted`; disconnect after DATA → `unknown` then a retry sends the same link; timeout before connect → `connect`. |
| Outbox | Crash simulation: kill the worker between claim and send, restart, and the row is sent once more with the same link. Expired rows are never sent. The global budget delays without dropping. The account change and the outbox row roll back together. |
| API | Enumeration: existing, absent, unverified, disabled, OIDC-only and throttled identities give byte-identical responses. A timing distribution over 200 requests each shows no separable difference at the chosen threshold (record the threshold). State not `ready` → 503 for all. Members, admins without owner role, and bearer app keys get 403 on every `/admin/api/mail` route with no side effect. `enabled:true` before an accepted test → 409. A config change after the test resets `ready`. |
| API | The password is write-only: no GET, export or error returns it. The env reference resolves at send time, and a missing variable gives layer `config`. |
| Headless | The route inventory, owner OpenAPI allowlist and `HEADLESS_ACCESS.md` table include every new route (`tests/test_headless_access.py:60-66`). |
| E2E (Mailpit, isolated gateway on a free port, never 5000/15000) | Owner configures mail → test → "I got it" → turn on. Member adds recovery email → reads the confirmation from Mailpit's API → opens `/verify/` on a second browser context → confirmed. Signs out → Forgot password → request → reads the reset mail → opens the link → new password → old password fails, other sessions rejected, app key still works, password-changed mail received. Repeat at 390 px. Repeat with mail off: no email field anywhere, the link-sharing copy is shown. |
| Rendering | Every message in Mailpit: plain text, the link is complete and clickable, no line breaks inside the URL, no tracking. |

### 3.10 Relation to UX_BATCH1 §3 (recovery)

This section supplies UB's mail dependency and does not compete with it. UB
keeps the recovery screens, copy, the owner handover dialog and the sole-owner
host command. Two amendments are proposed for UB's review:

1. **Verify through a public endpoint.** UB puts
   `POST /me/api/recovery-address/verify {token}` behind a session. A
   confirmation link is often opened on a phone with no session. The token
   already proves control of the mailbox and is bound to the person and the
   address. Proposal: `POST /auth/api/recovery-address/verify {token}`, public,
   rate-limited, with the page at `/verify/`. Starting verification stays
   session-only.
2. **Paths are root paths.** UB's `/recent/` and `/sign-in/recover/` "under the
   app base path" become root pages (§2.4).

Also for UB: the global `"reset"` throttle bucket (§3.1) is a current defect
that UB's public request endpoint would make worse. Fix it in the first
recovery change.

### 3.11 Risks

| Risk | Control |
|---|---|
| Mail is marketed as stronger than it is (an inbox is not MFA; NIST: email is not out-of-band authentication) | Copy never says "secure"; mail is recovery only, not sign-in. Magic links are deferred (OM §4). |
| A household shares one inbox | One verified address per person (UB decision 1). Others keep assisted recovery. |
| The relay accepts, then the message lands in spam or bounces | Copy says "accepted", not "delivered". "I got it" records observed receipt. Owner docs cover SPF, DKIM and DMARC for the From domain. Bounce webhooks are not in v1; the From domain's bounce mailbox is the owner's. |
| Signing-key rotation invalidates unsent links | Documented. Rotation already invalidates invite copies today. |
| `email-validator` or `smtplib` advisories | Pinned and hashed lock; the advisory feed is checked at each release. The stdlib follows the Python 3.12 image. |
| The owner's "email" is a username | Mail never uses `users.email` directly. It uses verified recovery addresses only. |
| Global budget too low for invite bursts | 60 per hour covers a household. The owner sees "delayed", not lost. |

### 3.12 Owner decisions

1. **M1 Existing people's recovery email.** Recommended default: **no bulk
   email**. Show each person a one-line prompt in Account on next sign-in:
   "Add an email so you can reset your password yourself." Until they confirm
   one, they keep assisted recovery.
2. **M2 Email invitations automatically?** Recommended default: **yes, when mail
   is ready and the owner typed an address**. The invite dialog states "We'll
   email this invitation to {email}." with an opt-out checkbox. The copy-link
   path stays.
3. **M3 Completion ("done") emails.** Recommended default: **not in v1**. Ship
   in-app Recent (UB §2) first. Add opt-in completion mail through the same
   outbox only after a household asks for it.

Owner prerequisite, not a design decision: before turning mail on, the owner
needs a relay account and a From domain with SPF and DKIM set up at that
relay. Without them, a test may be accepted, but real mail will often land in
spam.

---

## Completion record

- Research inputs: CS, UR, OM, PR, BL (D2, D3, D6), UB §3. All read in full.
- Code evidence: three read-only sweeps of r17 `0061d150`. The load-bearing
  lines were checked by hand (listed under "Inputs").
- Not done:
  - Independent red-team of this design (required by P20 before
    implementation).
  - Client-matrix runs.
  - Any test execution.
  - Any visual comp.
- Confidence:
  - High: the routing decision (dispatcher plus reserved classifier) and the
    mail transport and stack decision.
  - Moderate: the recipe content for untested clients and the outbox details.
  - UNVERIFIED: every behavior marked so above.
- Next step: a non-maker red-team against P1-P22 and the research. Then
  resolve the findings in this file, then the owner decisions, then
  implementation in the order Root, Connect, Mail.

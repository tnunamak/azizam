# Access plans (design v3)

Status: proposed design, not implemented. 2026-10-11. v2 resolved the
independent red-team of v1 (`azizam-redteam-access-plans-e20b388-r0.md`: 3
blockers, 9 major, 3 minor). v3 resolves the second red-team of v2
(`azizam-redteam2-access-plans-e6e4965-r0.md`: 2 blockers, 5 major, 2 minor),
mostly by removing machinery: §10 lists what was cut. An independent
consistency check of this revision (a different model) found 20 defects (8
major: invitation plan storage, re-authorization after an owner change,
reservation expiry during a capacity wait, sign-out triggers, attempt
allocation, the lock inside `data/`, restore without a marker, backup
space); all are fixed here. The resolution table is §9. A go decision on this version is still required before any build starts
(P20).

This design replaces the built-in default access, the "Legacy …" plans and the
separate category-rule plans with one kind of thing: an **access plan** that
the owner names and assigns. It amends `CATEGORIES_AND_PLANS.md` (v3) where
§2.12 says. Everything else in v3 stays.

Inputs:
- Research: `~/Documents/azizam/ux-findings/research-results/azizam-research-access-plan-composition-20261011-b.md`.
  Cited as [R§n].
- Red-team of v1: `~/Documents/azizam/ux-findings/research-results/azizam-redteam-access-plans-e20b388-r0.md`.
  Cited as [RT B1] … [RT m3].
- Red-team of v2: `~/Documents/azizam/ux-findings/research-results/azizam-redteam2-access-plans-e6e4965-r0.md`.
  Cited as [RT2 B1] … [RT2 m2].
- Owner data preview: `~/Documents/azizam/ux-findings/reports/access-plan-migration-preview-2026-10-11.md`
  (read-only copy of `admin.db`). Cited as [P§n].
- Divan `local-ai-gateway/`, 2026-10-10: the categories v3 evidence.
- In-flight branch `fix/access-hygiene`, which this design assumes has landed:
  Try keys bound to the sign-in session and expiring with it; the OIDC client
  secret in the secret store; plan deletion refused while keys, apps, people,
  invitations or mappings use the plan; `legacy_identity_profile_grants`
  dropped. This design does not redesign those.
- Code: all `file:line` references are at `e20b3882`. The code there is the
  same as `0061d150` and `e6e4965b` (only this file differs).

## 1. Problem

### 1.1 The owner's questions

| Owner, 2026-10-11 | Answer |
|---|---|
| "I don't want legacy anything so why do we still have them vs mapping old keys to modern stuff? And why do we have so many?" | Five "Legacy …" plans came from a one-time import of `proxy_api_keys.yml` on 2026-05-22 [P§6]. Nothing mapped them afterwards. The instance has 9 plans for 15 live apps. After this design it has 5 (§8). |
| "Is None as an access plan supported by divan?" | No. Categories v3 rule A10: "A missing identity, an empty plan list or a failed lookup denies." The "None" option (`web/src/app/settings/page.tsx:954`) and "Choosing none does not block AI access" (`:986`) contradict it. |
| "I don't want the default to be anything local in my own instance" | There is no runtime default anywhere. A person with no plan cannot generate. Keyless LAN access is removed, not remapped (§2.8). |
| "merge category rules into access plans" | Yes (§2.2). |
| "tag models or workflows … as NSFW … have a default access plan filter out NSFW things?" | Define a category, tag items, add a Blocked row to a plan. Untagged items are blocked by that plan until tagged (§2.10). |
| "Invite someone … what are the options and specifically what is the default" | Today: all 9 plans, nothing preselected (`web/src/app/people/page.tsx:72-99`, `:953`). After: your plans with a summary each; the preselection is your Settings choice, initially "Ask each time" (§2.9). |

### 1.2 How a request gets its plan today

`authenticate_request` (`proxy.py:2461-2496`), in order:

| Step | Condition | Plan used | Code |
|---|---|---|---|
| 0 | Auth disabled, or a public path | Built-in default (`NO_AUTH_CONTEXT`) | `proxy.py:2468-2469`, `:13406-13409` |
| 1 | An app key | Key plan → app plan → owner's person plan (only if self-service apps are on and the owner is a person) → **built-in default**. First found wins; there is no intersection | `gateway/admin_runtime.py:1682-1720` (default `:1703`); `proxy.py:1997-2007` |
| 1a | Person plan | `account_access.profile_id`, then an active `runtime_profile_mappings` row, then nothing | `gateway/accounts.py:140-149`; `proxy.py:1919-1975` |
| 2 | No key, client IP in `lan_bypass_cidrs` | **Built-in default**, full media metadata, identity `lan-bypass` with no person or app | `proxy.py:2385-2401`, `:2487-2489` |
| 3 | No key, a configured path token | **Built-in default** | `proxy.py:2491-2494` |
| 4 | Unknown `sk-` key on a delegation route | **No Azizam plan**; LiteLLM decides | `proxy.py:2498-2535`, e.g. `:25490` |
| — | Browser surfaces | Person plan, else `default-local` | `proxy.py:1992-1994`; `gateway/user_runtime.py:110-116`; `gateway/home_api.py:49-65` |

Two more fallbacks make an empty plan mean "everything": `load_profile(None
or {})` (`gateway/profiles.py:365-371`) and `evaluate_and_apply` with a missing
identity (`gateway/runtime_auth.py:283-302`). 12 active keys on 5 apps resolve
to the default [P§4].

The default (`DEFAULT_PROFILE = AppProfile()`, `gateway/policy.py:280`) grants
every local model, workflow and capability, may load models, has no paid
providers, no direct engine access, basic receipt detail and no cap [P§1].

The repository config turns LAN bypass on for 127/8, ::1, 192.168/16, 10/8 and
100.64/10 (`proxy_config.yml:1432-1434`). The deployed config is
**UNVERIFIED**.

### 1.3 Two systems called "plan"

1. **Access plans**: `profiles` table, `AppProfile`, `gateway/policy.py:373`.
2. **Category plans**: a snapshot in `categories.db`, evaluated by
   `gateway/category_policy.py:375`, attached in `PolicyVersion.people/apps`.

The category gate only tightens an allowed decision, and it passes in four
cases: no gate configured (`gateway/category_enforcement.py:153-162`); a
store file that was never opened (`:68-70`); no active snapshot (`:87-90`,
`:112-113`); both the person and the app unlisted (`:116-117`; the LAN
identity has neither, so it always skips). `visible()` passes with no gate
(`:171-173`). The adapter resolves the service by its stem without a
revision and takes the first match (`:43-44`, `:135`). It never passes phase,
output or metadata completeness, so `decision_for` assumes `dispatch` and
complete metadata (`category_policy.py:482`, `:497`) [RT B1]. Whether an
active snapshot exists on the instance is **UNVERIFIED**.

### 1.4 Two provider allow-lists

A paid provider call passes only if both agree [RT M3]:
- the plan's `remote.providers` (`gateway/policy.py:517-534`);
- the provider's `allowed_plans` (`gateway/providers.py:1281-1302`). An absent
  key means no restriction. `null`, an empty list or an invalid value denies.
  A non-mapping provider policy counts as absent (`:1278`). Membership compares
  the plan's JSON `id`, not its row id.

On this instance both providers list `["owner-local"]` [P§2]. Changing a
plan's display name is safe: it does not change the JSON id
(`gateway/admin_store.py:1082-1092`). Changing the JSON id or merging plans
would drop provider access.

### 1.5 Spending today

Plan caps add up `provider_spend` rows by `profile_id`, the plan id
(`gateway/providers.py:303-316`, `:1060-1073`). The ledger has no person or
app column, so every app on one plan shares one total [RT M1]. The day is a
rolling 24 hours (`:425`, `:1131`); the month is the UTC calendar month
(`:1091-1093`). A per-request ceiling exists (`gateway/policy.py:74`,
`providers.py:406-408`). Unknown costs become `needs_reconciliation`, which
blocks new paid requests in scope until the owner reconciles them
(`providers.py:352-376`, `:564-570`, `:1032-1044`, `:613-649`). No stored plan
sets a cap [P§2]. Provider and global caps add up by provider only
(`providers.py:1000-1012`, `:1066-1075`).

### 1.6 Other legacy items (P11)

Five keys with a `legacy-` prefix and the `legacy-key-import` service account
that owns daisy, cap and open-webui [P§6]; `runtime_profile_mappings` (2 rows,
disabled) and the YAML bootstrap maps (`proxy.py:1939-1958`);
`ModelAccessPolicy` (`gateway/policy.py:137-141`, `:170-173`); the `default` and
`default-local` ids (`gateway/admin_store.py:473`, `:2001`); first-run seeding
of live plans (`gateway/default_profiles.py`); the category kernel's legacy
mode (`category_policy.py:256-257`, `:306`, `:327`); the "Let people create
their own apps" toggle changing which plan an app gets
(`settings/page.tsx:1060`; `proxy.py:1998`).

## 2. The model

### 2.1 An access plan

An **access plan** is a named, reusable object. Many people and apps can hold
the same plan.

| Part | UI label | Meaning |
|---|---|---|
| Grants | **Can use** | Per capability, the exact models and workflows (pinned revisions). Nothing is allowed unless listed. |
| Direct engine access | **Direct engine access** | Exact engines and operations an app may call through the engine's own API (§2.7). Empty means none. |
| Providers | **Paid providers** | Providers a granted item may route to. Empty means engines on this gateway only. |
| Provider fallback | **Use a paid provider when the engine is unavailable** | Off by default. A fallback route passes every other rule as its own bundle. |
| Category rules | **Blocked categories**, **Must match** | v3 deny and require rows over the shared category catalog. |
| Caps | **Spending caps** | Per request, per day, per month, for paid routes. A cap applies to the counter of whoever holds the plan (§2.11). |
| Settings | under **Details** | May load models that aren't running; receipt detail; metadata in delivered files. |

Grants are exact (v3 A9). Each capability in a plan has an authoring flag
**Offer new models and workflows** (`offer_new`). When the owner adds a model
or workflow, the add flow ends with "Who can use it?". It preselects every
plan whose flag is on for that capability and shows the gain preview. The
kernel sees only exact grants [R§21; Plex "All Libraries", R§17].

**Plans do not restrict by API protocol** (OpenAI, Anthropic, Ollama). A
protocol is the shape of a request, not what it may use. Protocol rules that
act as capability rules today are converted by the intent table in §2.2. No
product in the research restricts by wire protocol [R§11-§13].

### 2.2 Capabilities

The capability ids are the catalog's existing keys
(`gateway/model_presentation.py:121-124`). The evaluator receives the
capability, never a raw intent. `PROTOCOL_CAPABILITIES` (same file,
`:130-143`) becomes the one mapping from intent to capability [RT M4]. It
does not yet list every intent the routes emit: the build adds the intents
marked † below, and a test fails if a route emits an intent the map does not
list.

| Capability id | Label (catalog) | Intents mapped to it today |
|---|---|---|
| `chat` | Chat | `chat.completions`, `text.completions`†, `chat.responses`†, `chat.generate`†, `chat.messages`†, `chat.messages.count_tokens`† |
| `image` | Images | `image.generate`, `image.edit` |
| `video` | Video | `video.generate` |
| `speech` | Speech | `speech.synthesis`, `audio.speech` |
| `transcribe` | Transcription | `speech.transcription`, `speech.translation` |
| `music` | Music | `music.generate`† |
| `sfx` | Sound effects | `sfx.generate`† |
| `audio` | Audio | `audio.generate` (no subtype) |
| `decide` | Decisions | `decision.systemone` |
| `embed` | Embeddings | `text.embeddings` |
| `rerank` | Rerank | `text.rerank` |
| (§2.7) | Direct engine access | `provider.native` |

Not capabilities:
- Discovery (`models.list`, `models.show`, `models.running`,
  `models.version`) is allowed for any identity that passes step 1 of §2.5.
  The list is filtered by the same evaluator.
- Model management through Azizam's Ollama routes (`models.pull`, `push`,
  `create`, `copy`, `delete`) returns 501 (`proxy.py:27215-27281`) and is
  never grantable. Direct engine access is a separate grant (§2.7).

The catalog says "Speech" where `VOICE.md` says "Voice". This is an existing
P16 conflict, outside this design; the plan uses the id `speech`.

### 2.3 Category rules belong inside the plan

Category **rules** (Blocked categories, Must match) are part of each plan.
Category **definitions** and **tags** on models, workflows, routes and
engines are a shared catalog. Evidence: v3 already designed plans with
`grants`, `require` and `deny` together; Jellyfin, OpenRouter and Plex keep
restrictions on the same object that grants [R§17, R§18, R§11]; a second
engine beside the plans needs its own "who does it govern" rule, which is the
bypass in §1.3; P16 forbids two things called "access plan".

v3 semantics are kept unchanged [RT keep 5]: deny wins; a deny over a missing
value blocks (`DENY_INPUT_UNKNOWN`); `in` passes only when every accepted
value is in the row; every applicable member of the selected closure must
pass; alternative routes are separate bundles; a waiver waives only one
require row's missing value; outside metadata is evidence, never a grant.
The operators are the kernel's (`category_policy.py:270-279`): require `in`,
`exists`, `missing`; deny `in`, `missing`.

### 2.4 Assignment and the accountable person

- Every **person** has one plan, or none.
- Every **app** has exactly one assignment: a plan, or **Same as its
  person**. "Same as its person" is a stored choice, not a fallback. The API
  never returns success for an app without an assignment, and there is no
  operation that clears one; to stop an app, disable it [RT m2].
- **App creation writes the app row first, then the assignment** [RT2 M5].
  The app row is today's single `admin.db` transaction
  (`admin_store.py:1181-1221`; session apps `:1268-1303`). The assignment is
  a compare-and-swap in the access settings; the server accepts it only if
  the app row exists. No code deletes app rows (only `disabled_at` is set,
  `:1373-1388`), so "the app exists" never becomes false again and there is
  no orphan to clean up. If the second write fails or the process stops
  between the writes, the app exists without an assignment: step 3 denies it
  (`no_access_plan`, app side), the request returns an error, and Apps shows
  it with "Choose a plan". `ensure_session_app_for_user` checks both halves,
  so the next sign-in writes a missing "Same as its person". If invitation
  accept stops after its rows commit, the person and "My first app" exist
  with no plan; People shows "No access plan" with "Choose a plan", and the
  owner chooses. Plan deletion and assignment are changes to the same document,
  so its compare-and-swap orders them: an assignment to a deleted plan fails,
  and deletion of an assigned plan is refused.
- **Keys have no plan.** A key acts for its app. The key-level plan
  (`credentials.profile_id`) goes away. If an app's active keys have
  different effective plans today, the migration requires a choice per app:
  split the app, narrow every key to one plan, or revoke keys (§4.4) [RT M5].
- Every app has an **accountable person**: `apps.owner_user_id`, for both app
  kinds. The person must exist and be enabled; a missing account row denies
  (today it is accepted, `gateway/admin_runtime.py:1694-1701`) [RT B3].
  Service accounts no longer own apps.
- A request runs only if **both** the accountable person's plan and the
  app's assignment allow it (v3 A10). Composition is §2.6.
- The accountable person is **only** the policy and spending subject. It
  does not make a key act as that person for personal data. That stays as
  today: `person_for_app_credential` returns a person only for a `personal`
  app whose key was created by its owner (`admin_runtime.py:1562-1572`).
  Migrated apps keep kind `service`, so moving daisy to Tim does not give
  daisy's key Tim's saved prompts or Library [RT B3]. Which consumers read
  `person_for_app_credential` is **UNVERIFIED**; the build must trace them
  before it changes app ownership.
- A role (Owner, Admin, Member) controls management screens. It never grants
  generation. The admin API does not depend on plans, so the owner can always
  repair a plan that locks them out [R§20 item 5].
- **Browser surfaces** keep their existing identities [RT M7]. Make uses the
  per-person session app (`session_apps`, one row per person, credentials
  blocked by triggers, `gateway/admin_store.py:139-188`;
  `ensure_session_app_for_user`, `:1268-1303`). Home uses the person's
  personal app (`home_api.py:49-65`). Try uses the person's Try app with
  session-bound keys (fix/access-hygiene). Each is an app with an
  assignment. The migration sets "Same as its person" on them, except the
  owner's own, which follow decision 1 (§7). `ensure_session_app_for_user` writes
  "Same as its person" when it creates a missing row. **UNVERIFIED**: which
  app Library uses.

One plan per side makes "which plan applies?" one answer [R§20 Rank 1]. The
kernel keeps its list type; the store allows one entry per side.

### 2.5 Evaluation order

One evaluator answers runtime requests, discovery, Check access and the
change preview. The order is total, so the first reason is deterministic
[RT m1]. A request stops at the first failing step.

| # | Check | Fails with | Layer |
|---|---|---|---|
| 1 | Key or sign-in session resolves an enabled app; its accountable person exists and is enabled | 401 `key_required`, `key_invalid`; 403 `app_disabled`, `person_disabled` (a missing person row counts as disabled) | your key / your app |
| 2 | The gate is initialized and one valid active access revision is loaded | 503 `access_settings_unavailable` | gateway |
| 3 | The person has a plan, and the active revision has an assignment for the app | 403 `no_access_plan` (side named) | access plan |
| 4 | Blocked rows: person's plan, then the app's plan, over every member of the selected closure | 403 `blocked_by_category`, `not_reviewed` (unknown deny input) | access plan |
| 5 | Grants: person's plan, then the app's plan. Capability and item revision; each paid route in Paid providers; each fallback route needs fallback on; direct engine scope | 403 `not_in_access_plan` (side named) | access plan |
| 6 | Must match rows: person's plan, then the app's plan | 403 `not_reviewed` (missing value), `blocked_by_category` (known mismatch) | access plan |
| 7 | Capacity admission (`HEADLESS_AND_RESOURCES.md`, Release A) | as designed there | gateway / engine |
| 8 | Paid routes only: per-request ceiling, then one atomic reservation against every applicable cap (§2.11) | 429 `cap_reached`; 503 `spending_unavailable` | spending |

When both sides hold the same plan, it is evaluated once and the
explanation says "your access plan". The kernel today checks grants and
requirements per plan in one loop (`category_policy.py:421-443`); the build
changes it to the order above. Within one step, rows are checked in document
order and closure members in graph order, so the first reason is stable. No
reservation happens before steps 1-7 pass. v2 reserved before the capacity
wait; v3 reserves after it, so a reservation never waits and cannot time out
unsent [RT2 review].

Step 1's key or session lookup resolves the subjects and their state; steps
1-6 over those resolved subjects are one pure function, `evaluate` (§2.13). Discovery, Check
access, the change preview, the migration oracle and the download check
call only `evaluate`; they never reserve and never return a Permit. Only
`authorize`, called immediately before an external effect, runs steps 7-8.
A capacity slot taken at step 7 is released if step 8 denies.

### 2.6 How two plans compose

Every part has one rule [RT M9]. A gain on either side never widens the
other side.

| Part | Effective value |
|---|---|
| Can use (capability, item revision) | Both plans grant it |
| Direct engine access (engine, operation) | Both plans grant it |
| Paid providers | Intersection |
| Provider fallback | Both on |
| Blocked, Must match | Every row of both plans applies |
| Caps | The person's plan caps the person's counter; the app's plan caps the app's counter; provider and global caps apply too. One charge row counts in every counter once |
| May load models | Both on |
| Receipt detail | The lower of the two (`none` < `basic` < `standard` < `full`) |
| Metadata in delivered files | A bound: the lower of the two (`none` < `full`). A request may ask for less, never more. Today the plan value is a default that a request can override (`policy.py:148-158`); this is a behavior change, listed in §8 (N5) |

A change to a person's plan reaches every app that is "Same as its person"
and every app's intersection. The change preview lists all of those apps.

### 2.7 Direct engine access

Today `native_passthrough` has `enabled`, `allowed_backends`,
`allowed_operations` and `audit`; empty lists mean "unrestricted"
(`gateway/policy.py:93-98`, `:542-576`, `:326-366`) [RT B2]. A boolean would
lose those bounds.

The new grant:

```json
"direct_engine_access": {
  "engines": ["svc_llama_bee"],
  "operations": ["POST /v1/completions", "GET /props"]
}
```

- `engines` lists exact engine service ids. `operations` uses today's scope
  syntax (`"METHOD /path"`, `"METHOD *"`, a path without a method for any
  method, prefix ending in `*`, or `"*"`; `policy.py:326-366`), so stored
  scopes convert unchanged.
  An empty list grants nothing. There is no implicit "all".
- Azizam does not parse an engine's own API (AGENTS.md binding principle), so
  it cannot see which model a direct call uses. The **engine** is therefore
  the closure member that category rules check: a plan with a Blocked or Must
  match row allows direct access only to an engine whose own tags pass. The
  owner's tag on an engine is an assertion about everything behind its API.
  This is the one exception to pinned item revisions: a direct call pins the
  engine, not a model or helper revision behind it [RT2 note on B2].
- The action that `authorize` checks for a direct call is the engine id plus
  the canonical `METHOD /path` of the request, so `GET /props` and
  `POST /delete` on one engine are different actions (§2.13) [RT2 B1].
- `"*"` can include the engine's own model management (for example loading or
  deleting models). Azizam cannot tell which paths of an engine's API manage
  models without engine knowledge in code, so it does not try. The owner
  narrows with operation scopes, and the plan editor says in words: "This app
  can do anything {engine}'s own API allows, including loading or deleting
  models." Azizam's own management routes stay 501 whatever the plan says.
- Direct engine routes accept only app keys. A browser session identity is
  denied there.
- Direct engine access never bypasses identity, categories, caps or the
  provider rules. Paid remote targets are already refused for direct access
  (`proxy.py:29964-29976`).
- Every direct call writes a receipt (today's `audit` flag is never read by
  the evaluator; auditing becomes unconditional).

Rejected alternative: allow only "named native adapters whose effects can be
checked" [RT B2]. Checking effects needs a parser per engine API, which is
engine knowledge as code.

### 2.8 Deny by default on every path

| Path | After this design | Code removed or changed |
|---|---|---|
| App key | Steps 1-8 | `admin_runtime.py:1703` default; key-level plan |
| Browser session | Steps 1-8 as the person and the surface's app | `user_runtime.py:110-116`, `:229`; `proxy.py:15884`, `:16026`; `home_api.py:52-53` |
| LAN request without a key, or with a placeholder key | **Removed** (mandatory) [RT M8]. 401 `key_required` without a key; 401 `key_invalid` with a placeholder key | `proxy.py:2187-2230` (LAN part), `:2385-2402`, `:2487-2489`; `gateway/lan_auth_policy.py`; config `lan_bypass_enabled`, `lan_bypass_cidrs` |
| Path token | **Removed** | `proxy.py:2234`, `:2374-2384`, `:2491-2494`, `:30212-30214`; config `path_tokens` |
| Auth disabled | **Removed** | `proxy.py:2468`; config `auth.enabled` |
| Unknown `sk-` key forwarded to LiteLLM | **Removed**. Models served through LiteLLM become catalog items that plans grant | `proxy.py:2498-2535`; `delegated_to_litellm` skips, e.g. `:25490`; `allow_litellm_delegation=True` call sites |
| Public Ollama discovery | **Removed** | `proxy.py:2172-2175`, `:2436-2437` |
| Public paths (`/health`, showcase) | Kept. Never call the evaluator, dispatch, list protected models or return creations. Startup rejects a `public_paths` entry that matches a protected route in the route inventory | `is_public_path` (`proxy.py:2431-2444`) |
| `?api_key=` | Kept (it is a key). Off by default | — |
| Signed creation links | Kept as links. Each creation records person, app and closure members. Download runs `evaluate` on those against the current plans (§2.13), in addition to the link's own signature and ownership checks; it does not authenticate the recipient | `proxy.py:23631-23670`; the "URL without md" branch `:23655-23656` |
| Missing gate, snapshot or identity anywhere | Deny; never `DEFAULT_PROFILE` | `category_enforcement.py:68-70`, `:87-90`, `:112-117`, `:153-173`; `runtime_auth.py:292`; `proxy.py:13406-13409`, `:29875`; `profiles.py:365-371` |
| First-run setup network (`setup_allowed_cidrs`) | Kept as its own setting. It admits only first-run owner creation, and only while no owner exists. It no longer falls back to the LAN list (`proxy.py:2370-2371`) | — |

A source address is not an identity: Plex treats LAN clients without auth as
the owner; Home Assistant trusted networks still pick a user; Tailscale
separates network admission from grants [R§29]. There is no keyless option.
Every device gets an app and a key.

### 2.9 What "default" means

| Meaning | Behavior | Owner sees |
|---|---|---|
| No plan | Cannot generate. Can sign in, see their account and ask for access | "No access plan" |
| New invitation | The form preselects the plan chosen in Settings; the invitation stores the plan id when sent. Initial setting: **Ask each time** | "Access plan for new invitations" |
| New app | Members get "Same as my access plan" with no picker. Owners and admins pick, "Same as {person}'s access plan" first. The server rejects an app with no assignment | "Access plan" in New app |
| Starting templates | Bundled, read-only, inert until copied. Replaces first-run seeding of live plans (`default_profiles.py`) | "Use as a starting point" |

The "Default plan" setting (an `all_users` mapping,
`admin_store.py:1994-2024`) is deleted with every other runtime mapping.
OIDC sign-in without an invitation creates a person with no plan [R§22].
Invitation accept writes the person's plan and creates "My first app" with
"Same as its person" (today it copies the plan to the app and key,
`gateway/accounts.py:337-345`).

### 2.10 Tagging and a plan that blocks a category

1. **Define** (once): Categories → New category "Audience" with values
   "Everyone", "Teens", "Adults only", for models, workflows, routes and
   engines. Shipped as an editable starting category; the code never knows
   what a value means (P12).
2. **Tag**: one picker per category on each item page; bulk "Set category"
   in lists. Outside evidence shows as a dated suggestion ("Hugging Face
   lists this as not-for-all-audiences. Set Audience to Adults only?"). Only
   the owner's choice makes it a tag.
3. **Rule**: Access plans → Family → Blocked categories → "Audience is Adults
   only".
4. **Review and apply**: "4 access changes · Review" shows who gains, who
   loses, and **Not tagged yet**. "Apply changes" activates them together.

Results: Adults only is blocked; untagged is blocked ("This hasn't been
reviewed for your access plan yet."); {Everyone, Adults only} is blocked; a
workflow with an Adults-only or untagged member is blocked; discovery hides
blocked items; plans without the row ignore the tag.

### 2.11 Spending

Resolves [RT M1, M2; RT2 M1, M3].

- **Ledger**: one row per paid external call (an **attempt**). The row
  records the person, the app, the provider, the request id and the attempt
  number. It does not record a plan. A plan change, rename or reassignment
  never resets a counter. Counters are sums over the ledger by person, app,
  provider or all, per period. Each row counts once in each counter it
  belongs to; no row is ever copied to represent a second subject.
- **Caps** that apply to one paid attempt: the person plan's caps on the
  person counter, the app plan's caps on the app counter, the provider caps,
  the global caps. With "Same as its person" the same amounts cap both
  counters; the person counter already includes the app's spend, so that
  never charges twice.
- **Periods** stay as today: day = rolling 24 hours; month = UTC calendar
  month. `usd_per_request` stays. Copy shows the real reset: "You can spend
  again from 14:32" (when the oldest charge in the window leaves it) and
  "Resets on 1 November at 01:00" (the UTC boundary in the reader's time
  zone).
- **Attempt identity**: every paid call of a request is one attempt: each
  paid helper of a workflow, each retry, each fallback. The first
  `authorize` of an attempt allocates its number as one more than the
  request's highest in the ledger, inside the same `BEGIN IMMEDIATE`
  transaction that checks the caps (`providers.py:417-531`). So parallel
  helpers and a resumed job never reuse a number. The Permit carries the
  number; re-authorization passes it back instead of allocating. A unique
  index on (request id, attempt) makes a duplicate row impossible. The one
  insert path always sets both; today the request id is attached after the
  insert (`:579-589`), and it moves into the insert. Historic rows keep
  `attempt` null (§4.3), which the index ignores. A retry records no link
  to the attempt it replaces: each send is its own charge, and a link
  changes no counter.
- **One owner per attempt**: `authorize` reserves (§2.13); the provider-call
  boundary takes the Permit and only sends, settles or releases. TypeSafe
  stops reserving inside `call_typesafe_systemone` (`providers.py:848-873`),
  and the OpenAI-shaped callers stop reserving around `call_openai_json`
  (`:1754-1767`); both use the Permit's reservation.
- **States** of one attempt's row:

  | From | Event | To |
  |---|---|---|
  | — | `authorize` passes steps 1-8 | `reserved` |
  | `reserved` | re-authorization of the same, still unsent attempt passes | `reserved` (same row; no new insert) |
  | `reserved` | denied before the send, or the call fails before any byte is sent | `released` |
  | `reserved` | sent, cost known | `settled` |
  | `reserved` | sent, result or cost unknown | `needs_reconciliation` |
  | `reserved` | process restart or timeout (`recover_provider_spend`, `providers.py:352-376`) | `needs_reconciliation` |
  | `needs_reconciliation` | owner or automatic reconciliation | `settled` |

  The attempt owner releases in a `finally` block unless it has started the
  send. A Permit that is dropped without a send is therefore released in
  process, and a crash is handled by today's restart rule. A reservation is
  released only when no send started; anything else is never released.
  `needs_reconciliation` keeps today's rule with the new subjects: it blocks
  paid requests in every cap scope (person, app, provider, global) that
  contains the row and has a cap, until the owner reconciles it
  (`providers.py:1026-1044`; a scope without a cap is not blocked).
- **Unavailable**: if the ledger cannot be read or written, paid routes
  fail with 503 `spending_unavailable`. Local
  routes still run.

### 2.12 Storage and changes to categories v3

One versioned document, the **access settings**, holds categories, tags,
plans, assignments and the plan preselected for new invitations. It uses the existing snapshot store
(`gateway/category_store.py`): immutable revisions, compare-and-swap
activation, history, restore as a new revision. People, apps and keys stay in
`admin.db`. Plans are referenced only by an opaque, immutable plan id; there
is no second id inside the plan, so the `owner-local` coupling (§1.4) cannot
recur.

Removed from `admin.db`: `profiles`, `account_access.profile_id`,
`apps.default_profile_id`, `apps.owner_service_account_id`,
`credentials.profile_id`, `account_links.profile_id` (replaced by
`account_links.plan_id`, an opaque plan id), `runtime_profile_mappings`, the `user_access_defaults` setting.
Removed from provider settings: `allowed_plans`. Ledger: `profile_id`
replaced by `person_id`, `app_id` and `attempt` (all three nullable for
history only, §4.3), with a unique index on (request id, attempt). Added to `admin.db`: a
one-row `access_epoch` counter and the triggers that bump it (§2.13).

Creating a person or app writes the `admin.db` row, then the assignment
(§2.4). An invitation's plan was previewed when the owner sent it, so accept
commits it without another review; a concurrent change makes the
compare-and-swap retry. Open invitations stay in `admin.db`, so
`delete_plan`'s "in an open invitation" check is a cross-store read and can
race a new invitation. The race fails closed: accept's compare-and-swap
fails for a deleted plan, and the person gets no plan and shows "Choose a
plan" (§2.4).

| v3 text | Change |
|---|---|
| Migration R3: legacy mode with any-tag OR | Removed. The kernel drops `mode` and `legacy_tags`. No stored plan sets tags [P§2] |
| Plans live apart from access plans | The snapshot's plans are the access plans |
| Adapter: no gate, no snapshot, unlisted identities pass | Removed (§2.8) |
| Several plans per identity | The store allows one per side |
| Waiver as `missing: {row: allow}` on the plan | Kept in the kernel; the API puts it on the row as `waive_missing` |

### 2.13 The authorization contract

Resolves [RT B1; RT2 B1, M1, M2]. Reusing today's adapter is not enough: it
authorizes a service stem, not the work about to run. Two functions and one
small record; no general framework.

```
evaluate(settings, subjects, action) -> Allow | Denial        pure; steps 1-6
authorize(subjects, action, request_id, attempt) -> Permit | Denial
                                                              steps 1-8; reserves

subjects (server-resolved in step 1, never from the request body):
  person_id, app_id
  auth_kind        app_key | session

action (server-resolved after alias, wrapper and fallback resolution):
  capability       a capability id (§2.2), or direct_engine
  members          every route, helper and engine of the graph, each with its
                   revision; for direct_engine: the engine id
  operation        direct_engine only: canonical "METHOD /path"
  route            local, or the provider id; fallback: yes or no

Permit = subjects + action + the version it was decided under:
  settings_revision   the active access settings revision
  identity_epoch      admin.db access_epoch
  not_after           the key's or session's expiry
  plus, for a paid attempt: request_id, attempt, reservation_id, reserved_usd
```

`check(permit, effect)` runs at **every point of effect**: immediately
before the dispatcher sends to an engine and immediately before the
provider boundary sends to a provider, after any capacity wait. It passes
only if all of these hold:

1. **Fresh**: `settings_revision` is still active, `identity_epoch` is
   unchanged, and now is before `not_after`.
2. **Same action**: the effect's member is in `members`, its route equals
   `route`, and for a direct call its `METHOD /path` equals `operation`. The
   auth kind is the one the route accepts (direct engine routes: `app_key`
   only, §2.7).
3. **Same cost bound** (paid only): the boundary recomputes the bounded cost
   from the body it is about to send (`providers.py:790-815` derives it from
   the model, the input and the output limit). It must not exceed
   `reserved_usd`, and the row must still be `reserved`.

If 1 fails, the caller calls `authorize` again for the **same** attempt. If
steps 1-6 pass **and** the person and app equal the reservation row's, the
row is kept, its `expires_at` is refreshed, and steps 7-8 are not repeated (a
cap edit applies to new attempts). If the person or app changed (for example
a new app owner), the row is released and the call becomes a new attempt, so
the charge lands on the right counter. If denied, the row is released and
nothing is sent. If 2 fails, the effect is a
different action: a new attempt number and a new `authorize`. If 3 fails,
the boundary refuses and releases; this is a defect, not a user error.

**Revocation invalidates through the same freshness check** [RT2 M2]. The
`access_epoch` row in `admin.db` is bumped by SQLite triggers in the same
transaction as every change that step 1 reads: `credentials.revoked_at` and
`expires_at`; `apps.disabled_at` and `owner_user_id`; `account_access`
`disabled_at`, `removed_at` and `require_registered_session`; inserts into
`account_session_revocations` and deletes from `account_sessions`
(sign-out; `gateway/accounts.py:28-37`); and any other column the step 1
query reads (the test in §5.2 derives the list from that query). `admin.db` already enforces rules with
triggers (`admin_store.py:177-188`); a trigger cannot be skipped by a new code
path, as a call to "bump the epoch" could be. Today these changes are
independent writes (`admin_store.py:1373-1388`; checks at
`admin_runtime.py:1553-1558`, `:1691-1701`). Any revocation anywhere makes
every outstanding Permit stale, which costs one re-authorization per
in-flight request. That is accepted: revocations are rare at this scale, and
one counter is simpler than per-subject epochs.

**Linearization point**: the `check` call. A revocation that commits before
it stops the effect; a send that passed its check before the revocation is
not recalled. Long jobs check before each external call, so a revocation
stops the next one.

Other rules:
- Discovery (`/v1/models`, MCP `capabilities`, target details, alias lists,
  unknown-target suggestions) calls `evaluate` per item. Its result is never
  a Permit.
- A creation records the person, app and `members`. Download, share and
  signed links call `evaluate` with those against the current plans.
- Deny if: the gate is not initialized; no valid active revision; unknown,
  disabled or missing person or app; closure metadata missing for a member;
  an effect that cannot be checked before it happens.

## 3. Interfaces

API first (P21). UI, CLI and MCP call the same server operations.

### 3.1 Owner API

| Operation | Endpoint | Notes |
|---|---|---|
| Read | `GET /admin/api/access` | Categories, tags, plans, assignments, `revision`; `ETag` |
| Preview | `POST /admin/api/access/preview` | `{"base_revision", "changes"}`. Pure. Gains, losses, reason changes, `not_tagged`, covered scope |
| Apply | `POST /admin/api/access` with `If-Match` | 409 `stale_base` if the revision moved |
| History | `GET /admin/api/access/history` | Revisions with actor and time |
| Restore | `POST /admin/api/access/revisions/{n}/restore` | Returns a preview; apply as above |
| Check access | `POST /admin/api/access/check` | `{"person", "app", "capability", "model" or "workflow"}`, or `"engine"` and `"operation"`; optional `"route"` (a provider, fallback). Decision, side, rule, closure member |

Change operations: `create_plan`, `update_plan`, `rename_plan`,
`delete_plan` (refused while assigned or in an open invitation; lists the
users), `assign_person` (plan id or `null`), `assign_app` (plan id or
`"person"`; no `null`), `set_invite_plan` (plan id or `null` for "Ask each
time"), `create_category`, `update_category`, `tag`, `untag`,
`accept_evidence`.

Plan document. Every field is required; a missing field is a validation
error, not "no opinion" [RT M4]:

```json
{
  "id": "plan_7Hq2",
  "name": "Family",
  "can_use": {
    "chat":   {"items": [{"model": "family-chat", "revision": "r4"}], "offer_new": true},
    "image":  {"items": [{"workflow": "storybook-pictures", "revision": "r2"}], "offer_new": false}
  },
  "direct_engine_access": {"engines": [], "operations": []},
  "paid_providers": [],
  "provider_fallback": false,
  "blocked": [
    {"id": "family-no-adult", "category": "audience", "op": "in", "values": ["adults-only"]}
  ],
  "must_match": [
    {"id": "family-data", "category": "data_handling", "op": "in",
     "values": ["no-training"], "waive_missing": false}
  ],
  "caps": {"usd_per_request": null, "usd_per_day": null, "usd_per_month": null},
  "can_load_models": true,
  "receipt_detail": "basic",
  "media_metadata": "none"
}
```

- `id` is server-assigned and immutable. `can_use` keys are capability ids;
  a capability not listed grants nothing.
- An item without `revision` in a change is pinned to its current reviewed
  revision; the preview shows the pin.
- `blocked[].op` is `in` or `missing`; `must_match[].op` is `in`, `exists`
  or `missing`. `values` only with `in`. `waive_missing` only on `in` or
  `exists` rows.
- `null` cap means no cap for that period.

Removed endpoints, no redirect (P11): `/admin/api/profiles*`,
`/admin/api/runtime-profile-mappings*`, the user-access-defaults setting,
`PUT /{provider_id}/allowed-plans` (`gateway/provider_admin.py:563-575`),
`/admin/api/categories/*`.

Body changes: `POST /admin/api/invites` takes `access_plan` (required).
`POST /admin/api/apps` and the member endpoint take `access_plan` (plan id or
`"person"`, required; members may send only `"person"`); the server writes the
assignment through the access settings with compare-and-swap and returns the
app's effective access. A person's plan changes only through
`assign_person` (preview and `If-Match`); `PATCH /admin/api/people/{id}` does
not take a plan.

### 3.2 Member API

`GET /me/access`: plan name, a summary computed from the plan, each cap with
period, remaining amount and reset (§2.11), and per app whether it narrows
the plan. Discovery (`/v1/models`, MCP `capabilities`) lists only what
`evaluate` allows (§2.13).

### 3.3 Data-plane errors

OpenAI error shape, `type: "access_error"`. Members see the sentence;
`details` carries side, plan, rule, closure member, cap scope and period.

| Code | Message |
|---|---|
| `key_required` | This gateway needs an app key. Create one in Apps. |
| `key_invalid` | This key isn't valid. It may have been revoked. Create a new key in Apps. |
| `app_disabled` | This app is turned off. Ask {owner name} to turn it on. |
| `person_disabled` | Your account is turned off. Ask {owner name}. For a key: The person this app belongs to is turned off or missing. Ask {owner name}. |
| `no_access_plan` (person) | You don't have an access plan yet. Ask {owner name} to choose one. |
| `no_access_plan` (app) | This app has no access plan. Ask {owner name} to choose one. |
| `not_in_access_plan` (person) | {model name} isn't in your access plan. |
| `not_in_access_plan` (app) | Your access plan allows {model name}, but this app's access plan doesn't. Ask {owner name} to change the app's access plan. |
| `blocked_by_category` | {model name} is blocked for your access plan. |
| `not_reviewed` | {model name} hasn't been reviewed for your access plan yet. |
| `cap_reached` | Built from `details`: whose cap (yours, this app's, the gateway's), the period and the reset. Example: "Your monthly cap for paid models is reached (USD 5.00). It resets on 1 November at 01:00." Example: "The gateway's daily cap for {provider} is reached. You can use it again from 14:32." |
| `spending_unavailable` | Paid models are unavailable because spending can't be checked. Models on this gateway still work. |
| `access_settings_unavailable` | Access settings couldn't be loaded, so nothing can run. Ask {owner name} to check the gateway. |

### 3.4 UI copy

Follows `VOICE.md`. Member surfaces never show rule ids or "policy".

**Invite someone**: label "Access plan", placeholder "Choose an access
plan", no "None". Each option: name and summary ("Family — Chat, Images and
Speech. No paid models. Blocks Adults only."). Below: the chosen plan in full
(PF-D103). Preselected: the Settings choice, else nothing. No plans: "Create
an access plan before inviting someone." with "Create access plan". Load
failure: "Access plans couldn't be loaded. Try again." Send disabled until
a plan is chosen.

**New app**: member: no picker, "Access plan: Family (yours)" (replaces
`apps/page.tsx:337`). Owner or admin: picker, "Same as {person}'s access plan
({plan})" first. Helper: "This app can use only what both its access plan
and {person}'s access plan allow." App row: "Access plan: {name}" or "Access
plan: same as {person} ({name})".

**Settings → People and access**: "Access plan for new invitations", "Ask
each time" first. Removed: "Default plan", "None — built-in local access",
"Default local (built-in)", "Default (built-in)", the sentence at
`settings/page.tsx:986`, and the self-service sentence at `:1060`.

**People**: "No access plan" badge with "Choose a plan". A member with no
plan sees on Home: "You can't use models yet. Ask {owner name} to choose an
access plan for you."

**Access plans**: row: name, summary, "Used by 3 people and 5 apps". Editor
sections: Can use, Paid providers, Blocked categories, Spending caps, Details
(Must match, Direct engine access, model loading, receipt detail, file
metadata). "Capabilities follow gateway defaults" and "Model locations
follow gateway defaults" (`web/src/app/people/people-api.ts:134`, `:143`) are
removed. Pending bar: "{n} access changes · Review". Delete in
use: "{name} is used by 3 people and 2 apps. Move them to another plan
first." Settings unavailable: "Access settings couldn't be loaded. Requests
are stopped until this is fixed." with "Try again" and "View history".

**Providers**: "Allowed plans" becomes read-only "Access plans that can use
{provider}: Everything, Local and paid". None: "No access plan includes
{provider}." "Local only by default" (`providers/page.tsx:1856`) is removed.

**Check access** (owner): pick a person and an app; each item with its
first failing step. Replaces "Preview as" on Categories.

### 3.5 CLI and MCP

The CLI from `HEADLESS_AND_RESOURCES.md` Release B gains:

```
azizam access show [--json]
azizam access check --person tim --app open-webui --capability chat --model family-chat
azizam access preview --changes changes.json
azizam access apply --changes changes.json --base-revision 42
azizam access history
azizam access restore 41
azizam plans list
azizam people set-plan <person> <plan|none>
azizam apps set-plan <app> <plan|person>
azizam categories tag <model-or-workflow> audience=adults-only
```

MCP data-plane tools (`capabilities`, `run`, `job`) return the §3.3 codes.
Owner MCP tools stay deferred (`HEADLESS_AND_RESOURCES.md`).

## 4. Owner data migration

### 4.1 Shape

One-time and reviewed. No compatibility layer and no period where old and
new rules both run [R§24].

1. **Dry run**, from the release **before** the cut-over, so the old
   evaluator computes "today" and the new release ships none of it [RT m3]:
   `scripts/access-migrate dry-run --admin-db <copy> --categories-db <copy>
   --spend-db <copy> --receipts-db <copy> --config <copy> --out <dir>`. It
   opens inputs read-only and writes `report.md`, `report.json` (old
   decisions, §4.4) and `approval.yaml`.
2. **Owner review**: every row in `approval.yaml` starts as `decision:
   pending` with its listed alternatives. The owner sets `accept` or an
   alternative. The file records the candidate digest [RT M6].
3. **Apply** (new release): stop, back up, migrate, verify (§4.2).
4. **Accept**: the owner reads the verification report and accepts it, or
   restores the backup. The new gateway refuses to start until the
   migration is accepted, so no request runs on the new rules before the
   owner decides.

### 4.2 Stop, back up, migrate, verify, accept

Resolves [RT M6; RT2 B2, M4]. The stores are separate files, so one
transaction cannot cover them. v3 does not stage and swap them: it migrates
the stopped gateway in place after a verified full backup, and the only
recovery is to restore that backup, before any traffic.

`scripts/access-migrate apply`:

1. **Lock and preconditions** (apply refuses otherwise). The gateway holds
   an exclusive `flock` on `azizam.lock` in the parent directory of `data/`
   (outside it, so a restore that replaces `data/` cannot drop the lock)
   while it runs; apply and
   restore hold it to the end, so neither runs beside the gateway and the
   gateway cannot start during them. The lock ships in the release that
   runs the dry run, so the gateway being replaced already takes it. Every
   `approval.yaml` row is decided and its digest matches; the inputs match
   the dry run; no active key would keep a `legacy-` prefix (§4.5); no
   ledger row is `reserved` or `needs_reconciliation`; free space is at
   least twice the size of `data/` plus the config, for the backup and a
   possible restore copy (D11).
2. **Backup**. Checkpoint every SQLite database (`PRAGMA
   wal_checkpoint(TRUNCATE)`). Copy all of `data/` and the config to
   `data.access-backup-<id>/` on the same filesystem; fsync every file and
   directory. Write `backup.json` (path, size and SHA-256 of each copy)
   through a temporary file, fsync, rename and directory fsync. A backup
   without a matching `backup.json` is incomplete and is never used.
   Receipts, jobs and audit are in it, so a restore cannot leave them newer
   than the other stores.
3. **Marker**. Write `data/access-migration.json` (`state: migrating`, the
   backup path, the digest of `backup.json`) the same atomic way. No live
   file changes before it exists.
4. **Migrate in place**. `admin.db` first: its first transaction sets schema
   version 6, so the previous release refuses to open it from then on
   (`admin_store.py:294-296`). Then the access settings, the ledger and the
   config (temporary file, fsync, rename). Each store change is one SQLite
   transaction with `synchronous=FULL`.
5. **Verify** the live migrated files, gateway still stopped: the new
   schema; no removed table, column, setting, config key or plan name; the
   §4.4 oracle, run with `evaluate`, equals the approved matrix; provider and
   global ledger totals per period are unchanged; every approved revocation
   and disable is applied; every app has an assignment; the new release's
   startup checks (config validator, store opening, access settings load)
   pass against the migrated files without opening a listener. Checkpoint and fsync
   every changed file, then set the marker to `verified`. On any failure the
   marker stays `migrating` and the tool prints the restore command.
6. **Report**: "Verified. Nothing has run on the new rules yet. To start:
   `access-migrate accept`. To go back: `access-migrate restore` (allowed
   until you accept)." Before accepting, `access-migrate check --person …
   --app … --capability … --model …` runs the same `evaluate` offline.

`accept` sets the marker to `accepted`. The new release starts only then; with
no marker and the old schema it stops with "Run access-migrate".

`restore` runs only while the marker says `migrating` or `verified`. It
checks every backup file against `backup.json` and refuses, changing
nothing, on any mismatch. It copies the backup to `data.restoring/`, fsyncs
it, renames `data/` to `data.failed-<id>/` (kept, P19), renames
`data.restoring/` to `data/` and fsyncs the parent; the config likewise. Then
the owner runs the previous release.

**After accept there is no tool rollback** [RT2 B2]; `restore` refuses. The
backup has none of the traffic served since accept. Restoring it by hand
would delete every charge since accept (caps would undercount), every new
key, every revocation (a revoked key would work again), and every audit
event, receipt and job. Fixes after accept are forward, through the normal
API. An owner who still restores by hand accepts those losses and must
revoke again every key revoked since accept.

Interruptions [RT2 M4]:

| Stopped during | Found next | Result |
|---|---|---|
| Backup, before `backup.json` is written | no marker | Live files unchanged; apply deletes its incomplete backup and starts again |
| After `backup.json`, before the marker | no marker | Live files unchanged; apply starts again with a new backup |
| Marker write | old or new marker (rename is atomic) | As for that state |
| Migrate or verify | `migrating` | Gateway refuses to start; restore, then apply again |
| After `verified` | `verified` | All changes were fsynced first; accept or restore |
| Restore | `data.restoring/` | Restore again: it recopies an incomplete `data.restoring/`, or finishes the renames if `data/` is gone and the copy matches `backup.json` |
| Marker unreadable | — | Gateway, apply and restore all refuse; the owner decides by hand with the backup path that apply printed and logged at step 2, and the §4.2 losses apply if traffic was served |

### 4.3 Old field to new field

Every old field has one outcome [RT M4]. A stored value with no
representation blocks apply and is listed as unrepresentable.

| Old field (`gateway/policy.py`) | New | On this instance [P§2] |
|---|---|---|
| `id`, row id | Opaque plan id; the report maps both old ids | 9 plans |
| `protocols` | Removed. Report lists each capability the protocol rule withheld | Several plans; gains listed in §8 |
| `allow.intents` | Capabilities via `PROTOCOL_CAPABILITIES`; `None` = all | Assistant, Media creator |
| `models`, `workflows` (`None` = all) | `can_use` items; `None` = every current item, `offer_new: true` | None set |
| `backends` | Removed; exact grants pin routes | None set |
| `allow.exposure`, `trust_tiers`, `resource_tiers`, `tags` | Removed; categories replace them | None set |
| `fallback.cloud_allowed`, `remote.allow_paid_fallback` | `provider_fallback` = both true | Both false everywhere |
| `remote.providers` ∩ provider `allowed_plans` (§1.4 semantics) | `paid_providers` | Owner (local): OpenRouter, TypeSafe |
| `remote.max_usd_per_request`, `budget_usd_per_day`, `budget_usd_per_month` | `caps` | None set |
| `native_passthrough` | `direct_engine_access`; empty old lists become the explicit list of engines with a native target at dry run and `["*"]` | LibreChat full runtime, Legacy daisy (both unrestricted) |
| `status_visibility.level` | `receipt_detail` | basic, standard, full |
| `target_policy.activation` | `can_load_models` | **UNVERIFIED** per plan |
| `target_policy.visibility` | Removed: discovery lists granted items with their state (running or loadable). A stored `available` value is unrepresentable | **UNVERIFIED** per plan |
| `model_access.mode` | Read once into `target_policy`, then removed | **UNVERIFIED** |
| `media_metadata.default` | `media_metadata` as a bound (§2.6) | none |
| Category snapshot plans | If the active snapshot governs anyone, each attached plan's rows must map into one access plan per identity, or apply refuses | **UNVERIFIED** (store in `data/`) |
| Ledger `profile_id` | Each row stays one row [RT2 M3]. `app_id` from the receipt with the same request id; `person_id` = that app's accountable person after the move (D7). A row with no receipt match (`request_id` may be null, `providers.py:303-316`) is **unattributed**: `person_id` and `app_id` null; it counts in provider and global totals only. Unattributed rows inside the current day or month are `approval.yaml` rows, `pending` by default; the owner names a person and app, or accepts "unattributed". Ownership is never inferred from who holds a plan today | **UNVERIFIED** (ledger in `data/`); no stored plan sets a cap [P§2], so today no person or app counter depends on these rows |

### 4.4 Checks the dry run must report

| # | Check | Why |
|---|---|---|
| D1 | Every person, app and active key with today's effective plan; each app's active keys grouped by effective plan | An app with more than one group needs a split, narrow or revoke decision [RT M5] |
| D2 | Provider matrix: each old plan × provider, plan side, provider side (absent, null, empty, string, list), effective result; duplicate JSON ids | Paid access comes from the intersection [RT M3] |
| D3 | The oracle: for every **valid** (app, its accountable person) pair × capability × item revision × route (local, each paid provider, fallback) and each engine × direct operation, old decision and reason against new; plus per pair: receipt detail, metadata, model loading, caps. Results are reused per plan pair. Wrong-owner pairs are covered by fixed adversarial fixtures (§5.6), not by the full product [RT2 note] | Every difference must be a row of §8 or apply refuses. Covers known subjects only, not future traffic |
| D4 | Receipts in the last 90 days by app and for `api_key_name = "lan-bypass"` (written today, `proxy.py:2489`, `:29844`) by path and capability | Keyless traffic that will stop. Receipts have no client IP (`gateway/receipt_store.py:67-92`), so the owner must name the devices |
| D5 | Whether LiteLLM is on; delegated `sk-` traffic | Clients that need Azizam keys. Whether delegated requests write receipts is **UNVERIFIED** |
| D6 | The deployed config's `auth` block, `public_paths` and `ollama_compat_discovery` | Confirms LAN bypass, auth-off and public discovery state; flags public entries that match protected routes |
| D7 | Each app's accountable person after the move, that person's plan, and the app's effective access (person ∩ app); apps whose person would be missing or disabled | The owner approves the owner column of §8; a missing person blocks apply [RT B3] |
| D8 | Ledger rows in the current day and month with their attribution (§4.3); rows with no receipt match; any `reserved` or `needs_reconciliation` rows; provider and global totals per period | Reconcile before apply; unattributed in-window rows become `pending` approval rows; apply verifies the totals are unchanged [RT M1; RT2 M3] |
| D9 | Creations without a recorded person and app | Their signed links stop working unless the owner assigns them |
| D10 | Stored values listed as unrepresentable in §4.3 | Blocks apply |
| D11 | Size of `data/` and the config, and free space on that filesystem | The backup and a restore copy (§4.2) need twice that space; size on this instance is **UNVERIFIED** |

### 4.5 Prerequisites on this instance

Done by the owner through the normal Apps and Providers flows before apply
[RT M8, m3]. Revocations and disables listed in §8 are approved rows that
apply performs; these steps are the ones that need a client changed:
1. `fix/access-hygiene` has landed (it revokes unbound Try keys once).
2. Create new keys for **daisy** and **open-webui** in Apps, update those
   clients, and revoke their two `legacy-` keys. The other three `legacy-`
   keys (simon, librechat, cap) were never used; apply revokes them as
   approved rows. Apply refuses if any active key would still have a
   `legacy-` prefix after the approved revocations, so no legacy string stays
   in live data (P11). Receipts and audit events keep the old names as
   history (P19).
3. Give every LAN device found in D4 an app and a key.
4. Give every client found in D5 an Azizam app key.
5. Reconcile any ledger row that D8 lists.
6. Deploy the last release before the cut-over. It carries the dry-run tool
   and the gateway lock (§4.2), so the gateway that apply stops already
   takes the lock.

### 4.6 Removed data and code (P11)

The `legacy-key-import` service account; `runtime_profile_mappings`; the YAML
bootstrap maps; the `proxy_api_keys.yml` import (`gateway/runtime_auth.py`);
`ModelAccessPolicy`; the `default` and `default-local` ids; `DEFAULT_PROFILE`;
`seed_default_profiles`; `LEGACY_ACCESS_PLAN_NAMES`
(`settings/page.tsx:99-101`); the kernel's legacy mode; LAN bypass,
placeholder keys, path tokens, the auth-off switch and public Ollama
discovery; LiteLLM key delegation. Revoked keys that name a Legacy plan lose
the reference with the column. Disabled apps stay disabled; nothing is
purged (P19) [RT m3]. Whether service accounts have uses other than owning
apps is **UNVERIFIED**; this design removes only their app ownership.

## 5. Test plan

### 5.1 Evaluator (unit, pure)

- No plan on person; person allows and app does not, and the reverse; both
  allow; same plan on both sides evaluated once.
- First reason with simultaneous category deny, missing grant, requirement
  failure and exhausted cap: the order of §2.5; no reservation before step 8.
- v3 scenarios from `categories_model/`: deny over grant; unknown deny
  input; mixed values; each operator; waiver never overrides a deny, a known
  mismatch or a missing grant.
- Composition: one test per row of §2.6, including receipt detail and
  metadata minimum and a metadata request above the bound.
- Accountable person: a `service` app owned by a person gets no access to
  that person's saved prompts or Library; a `personal` app key created by
  someone other than the owner does not act as the owner; a missing person
  row denies.
- Missing plan field fails validation; empty `can_use` grants nothing; an
  empty `direct_engine_access` list grants nothing.

### 5.2 Authorization contract (§2.13)

- Stem unchanged, settings revision changed: `check` fails until
  re-authorized.
- One engine, one plan granting only `GET /props`: a Permit for
  `GET /props` is refused by `check` for `POST /delete`; the engine is not
  called [RT2 B1].
- An app-key Permit and a session Permit for the same action: the session
  one is refused on a direct engine route.
- The body is changed after `authorize` so its bounded cost exceeds
  `reserved_usd`: the provider boundary refuses and releases.
- A Permit for another person, app, member or route is refused at both
  boundaries.
- Alternative route changed, helper changed, retry, fallback: each is a new
  attempt number with its own `authorize`.
- Revoke the key, expire it, disable the app, disable or remove the person,
  or sign out the session between `authorize` and the send: zero sends; the
  reservation is released; no second reservation exists [RT2 M2].
- Change the app's owner between `authorize` and the send, to a person whose
  plan also allows it: the old row is released and the charge lands on the
  new person's counter as a new attempt.
- Trigger coverage: for each column that step 1 reads, an update bumps
  `access_epoch`; the test lists the columns from the step 1 query, so a new
  column without a trigger fails it.
- Download: a wrong recorded member denies; a plan changed after creation
  uses the current plan.
- Gate not initialized, store file missing, no active revision, no
  assignment for the person or the app: deny.
- `evaluate` never reserves and never returns a Permit; discovery output
  passed to dispatch is refused.

### 5.3 Server denial tests (every path)

Extend the route inventory test so every protected route lists its
enforcing boundary. A mock engine fails the test if called.

| Case | Expected |
|---|---|
| No key from 127.0.0.1, ::1, 192.168.1.50, 10.0.0.5, 100.64.1.1, a ULA and a public address | 401 `key_required` |
| Placeholder key from a LAN address | 401 `key_invalid` |
| Former path-token prefix | 401; no token stripping |
| Unknown `sk-` key on every former delegation route, LiteLLM on | 401 `key_invalid`; LiteLLM not called |
| Forged `X-Forwarded-For` from an untrusted peer | 401 |
| Config with `lan_bypass_enabled`, `path_tokens` or `auth.enabled: false` | Startup fails naming the key |
| `public_paths` matching a protected route | Startup fails naming the entry |
| Revoked key; disabled app; disabled person; app whose person row is missing | 401 / 403 |
| Person with no plan, on every browser surface and by key | 403 `no_access_plan` / empty list |
| Native request: ungranted engine; ungranted operation; engine blocked by category | 403; engine not called |
| Native request to a paid remote target | 400 `provider_native_paid_remote_forbidden` (today's code, `proxy.py:29964-29976`) |
| Native request from a browser session identity | 403; engine not called |
| Azizam management route (`/api/pull`, `/api/delete`, …) with a plan that has direct engine access `"*"` | 501 |
| Signed link after the app is disabled or the plan stops allowing the work | 403; file not returned |
| `/health` and showcase | 200; never dispatch, list or return creations |
| `/v1/models`, `/api/tags`, MCP `capabilities`, MCP target details, speech alias lists and unknown-target suggestions (`mcp_server.py:285-348`) | Equal to Check access; nothing outside it is named |
| MCP `run`, `job`, content for an item the plan does not allow | The §3.3 code; core route enforcement, not an MCP-only check |
| App creation: the assignment write fails after the app row commits | Error returned; the app denies with `no_access_plan` (app); Apps shows "Choose a plan" [RT2 M5] |
| `delete_plan` races `assign_app` to that plan; session app and invitation accept variants | Exactly one wins by compare-and-swap; never an assignment to a missing plan; the next `ensure_session_app_for_user` writes a missing assignment; a failed accept leaves a person and app that deny and show "Choose a plan" |

Static guard (like `tests/test_engine_knowledge_boundary.py`), an AST check
over `gateway/` and `proxy.py` [RT m3; RT2 m2]:
- `DEFAULT_PROFILE`, the LAN bypass functions and the delegation helpers are
  not defined or imported;
- the string `"allowed_plans"` is not used as a mapping key: not as a
  subscript, a `.get`/`.pop`/`.setdefault` argument, the left side of `in`
  or `not in`, or a dict literal key. Today's ACL (`providers.py:1288-1290`,
  `"allowed_plans" not in policy` and `policy.get("allowed_plans")`) is a
  fixture that must fail the guard.
The startup validator, which names removed config keys, is the one exempt
file. This is not a repository-wide string ban.

### 5.4 Spending

Concurrent requests against one cap; cap edit during a reservation; plan
reassignment and rename without a counter change; app recreated; same plan
on both sides charged once; restart with a `reserved` row; unknown cost;
reconciliation; ledger unavailable gives `spending_unavailable` while local
routes run; rolling-day and UTC-month boundaries; per-request ceiling.

Attempts [RT2 M1]: a workflow with two paid helpers writes two rows with
attempts 1 and 2; a second insert of one (request id, attempt) fails;
re-authorizing an unsent attempt keeps its one row; a retry and a fallback
each add one row; a Permit dropped before the send leaves the row
`released`; a send that started with an unknown result leaves it
`needs_reconciliation`, never `released`; TypeSafe and an OpenAI-shaped
route both reserve only through `authorize`. Each case: exactly one row per
actual send, and provider and global totals count each row once.

### 5.5 Category exclusion

"Family" blocks "Audience is Adults only"; models A (Everyone), B (Adults
only), C (untagged); workflow W (Everyone, helper B):
- A runs; B `blocked_by_category`; C `not_reviewed`; W `blocked_by_category`
  naming B.
- `/v1/models` for a Family app lists A only.
- Tagging C has no effect until apply; after apply C runs on the next
  request; restore blocks it again.
- A Family person with an app on Everything: B is blocked.

### 5.6 Migration

Fixture: synthetic `admin.db`, categories store and ledger with the owner's
shapes (9 plans, 22 apps, fake key hashes, disabled mappings, the service
account, two keys with different plans on one app). No copy of `data/`.

- Dry run writes only to `--out`; input hashes unchanged.
- Report matches a golden file, including every row of §8.
- `approval.yaml` with any `pending` row: apply refuses.
- Input changed after the dry run: apply refuses.
- An app with two key groups and no decision: apply refuses.
- Daisy and open-webui moved to the owner keep kind `service`;
  `person_for_app_credential` returns no person for their keys.
- A stored unrepresentable value: apply refuses and names it.
- Crash injected before and after each §4.2 step and each restore step,
  including a torn temporary marker and a backup with a missing or changed
  file: each row of the §4.2 interruption table holds; restore refuses a
  backup that does not match `backup.json`; the result is all migrated and
  verified, or all restored, for every file in `data/` [RT2 M4].
- Serve traffic after accept (a paid charge, a new key, a revocation, a
  reconciliation, an audit event), then run restore: it refuses and changes
  nothing [RT2 B2].
- The previous release started against a migrated `admin.db` refuses to
  open it.
- Unattributed ledger rows [RT2 M3]: a plan whose holder moved before the
  cut-over, a plan with no holders, two apps of one person and apps of two
  people, each with and without a receipt match: in-window unmatched rows
  are `pending`; provider and global totals are unchanged; no row is
  copied.
- Wrong-owner adversarial pairs (an app evaluated with a person who does
  not own it) deny in the oracle fixtures.
- Apply twice: the second run does nothing.
- After apply: no removed table, column, setting, config key or plan name
  exists; the Check access matrix equals the approved one.
- Rehearsal on a `.backup` copy of the real stores in `~/.tmp` (never
  `data/`), report reviewed by the owner before the real apply.

### 5.7 UI

Invite: no "None"; Send disabled without a plan; 390 px. Settings: "Ask each
time" first. Apps: member sees no picker. Review counts equal the API
preview.

## 6. Risks

| Risk | Mitigation |
|---|---|
| The owner narrows their own plan and loses generation | Preview warns "You will lose access to …"; the admin API never depends on plans |
| Unknown LAN devices or LiteLLM clients stop | D4 and D5 before apply; prerequisites 3-4 |
| Untagged items vanish for a blocking plan | Review counts "Not tagged yet"; Categories lists untagged items |
| Exact grants are tedious as models change | `offer_new` and "Who can use it?" |
| Direct engine access with `"*"` exposes an engine's own management | Explicit engines; editor warning; category check on the engine; receipts |
| The gate is now the only authority on the hot path | It caches by `PRAGMA data_version` (`category_enforcement.py:47-55`); measure overhead before release |
| Consumers of `person_for_app_credential` are not traced | Trace before changing ownership (§2.4) |
| Assignment writes contend on the snapshot store | Compare-and-swap with retry; assignment changes are small |
| Any revocation, and any settings revision (an unrelated tag or assignment), makes every in-flight Permit stale | One extra run of steps 1-6 per in-flight request; capacity and reservation are kept (§2.13). Measure before release |
| The owner restores the backup by hand after accept | Restore after accept is refused by the tool; §4.2 lists exactly what a manual restore loses |
| The full backup of `data/` is large | D11 reports size and free space; apply refuses without the space |
| A crash between app row and assignment | The app denies and shows "Choose a plan" (§2.4); never a widening |

## 7. Owner decisions

Each row has a recommended default. Everything else is decided above. The
recommended defaults add no paid or direct engine access to automated apps
(A10 and A11 gain smaller things, listed in §8). Decision 1 adds paid
models to your own browser use; decisions 3 and 4 remove access from apps
that have not used it, except Paid provider proof, whose one use you
confirm was a test. §8 lists every gain and loss.

| # | Decision | Recommended | Why | Alternatives |
|---|---|---|---|---|
| 1 | What your browser apps and Try (Tim) use | **Local and paid** | The browser acts as you, so paid models under the existing caps (global USD 2 per day) are expected. It does not get direct engine access: Home and Try use app keys, and no browser page needs an engine's own API | Same as you (Everything, adds direct engine access); Local only (no change) |
| 2 | t3-pi-agent | **Local and paid** (today's rules, except new items, N7) | An automated agent gains nothing. Everything would add direct engine access, which can delete models on an engine | Everything |
| 3 | Anna's Museum App | **Local only** | Its key was never used; a museum app has no shown need for paid models | Disable it; Local and paid (no change) |
| 4 | cap, waspflow-dsh-probe, Paid provider proof | **Disable and revoke their keys** | Never used; last used 2026-08-14; a test key made and used on 2026-10-11 15:41 (confirm it was a test). A disabled app can be turned on again | Keep, on Local only |
| 5 | Access plan for new invitations | **Ask each time** | No default anywhere; affects future invitations only | Preselect Chat |

## 8. Migration effect on this instance

Plans after (9 become 5):

Every plan below grants "every current item" as exact pinned grants with
`offer_new` on. File metadata is `none` in all five, as today. May load
models keeps each source plan's value (`target_policy.activation`,
**UNVERIFIED** per plan until D3; the built-in default may load).

| Plan | From | Rules | Held by (recommended) |
|---|---|---|---|
| **Everything** | new | All capabilities and items; OpenRouter, TypeSafe; direct engine access to every engine with a native target, all operations; receipt full | Tim Nunamaker |
| **Local and paid** | Owner (local), same rules | All capabilities and items; OpenRouter, TypeSafe; no direct access; receipt full | t3-pi-agent; Tim's browser apps and Try (decision 1) |
| **Local with direct engine access** | LibreChat full runtime; Legacy daisy merged | All capabilities and items, local only; direct engine access as Everything; receipt full | librechat.vivid.fish, daisy |
| **Local only** | new, exact copy of the built-in default; Legacy open-webui merged | All capabilities and items, local only; no direct access; receipt basic | Simon Agent, OpenWhispr, open-webui, Anna's Museum App, disabled apps |
| **Chat** | Assistant (chat + RAG), renamed | Chat and Embeddings, local only; receipt basic | Tim ODL; paperless |

Deleted: Legacy simon, Legacy librechat, Legacy cap (only never-used or
revoked keys use them), Media creator (unused). Provider `allowed_plans` is
deleted. Today only Owner (local) reaches OpenRouter and TypeSafe; after,
Local and paid (the same plan) and the new Everything do. The apps that reach
them are listed below (D2).

People:

| # | Person | Today | After | Change |
|---|---|---|---|---|
| P1 | Tim Nunamaker | no plan → built-in default | Everything | His person plan now bounds his apps (they all fit inside Everything) |
| P2 | Simon Agent | no plan → built-in default | Local only | None |
| P3 | Tim ODL | Assistant | Chat | None |

Apps. "Owner after" is the accountable person. Rows marked **UNVERIFIED**
are confirmed by D7, which also prints person ∩ app for each row; if the
owner turns out to be Simon (Local only), an app on Local and paid would lose
paid access, and D3 lists that as an unapproved difference. N7 applies to
every row, whether or not the Loses column names it (no stored plan lists
models today, §4.3). Approve each row or pick its
alternative.

| # | App | Owner after | Today | After | Gains | Loses | Keys | Alternative |
|---|---|---|---|---|---|---|---|---|
| A1 | Tim's browser apps (Tim personal access; session app if it exists) | Tim | built-in default | Local and paid — decision 1 | OpenRouter, TypeSafe within caps; receipt basic → full | New items (N7) | "Testing" (never used) revoked | Same as Tim (also direct engine access); Local only (no change) |
| A2 | Try (Tim) | Tim | built-in default | Local and paid — decision 1 | as A1 | New items (N7) | Session-bound keys only | as A1 |
| A3 | t3-pi-agent | Tim **UNVERIFIED** | Owner (local) | Local and paid — decision 2 | — | New items (N7) | 1 active, used 2026-10-11 | Everything (gains direct engine access) |
| A4 | OpenWhispr | Tim | built-in default | Local only | — | New items (N7) | 2 active | Local and paid (gains paid) |
| A5 | Try (Simon) | Simon | built-in default | Same as Simon (Local only) | — | New items (N7) | Session-bound keys only | — |
| A6 | Simon personal access | Simon | no app plan; one key on Legacy simon | Same as Simon (Local only) | — | Legacy simon key (never used) | Legacy simon key revoked | — |
| A7 | paperless | Tim ODL **UNVERIFIED** | Assistant (app and key) | Chat, explicitly assigned. Effective access is its accountable person's plan ∩ Chat, so it depends on the owner D7 confirms (Tim ODL: Chat ∩ Chat = Chat, no change) [RT2 m1] | — | — | 1 active | — |
| A8 | My first app (Tim ODL) | Tim ODL | Assistant (app and key) | Same as Tim ODL (Chat) | — | — | 1 active | — |
| A9 | librechat.vivid.fish | Tim **UNVERIFIED** | LibreChat full runtime (key level) | Local with direct engine access (app level) | — | New items and engines (N7) | Legacy librechat key (never used) revoked | — |
| A10 | daisy | Tim (was `legacy-key-import`); kind stays service | Legacy daisy | Local with direct engine access | Receipt standard → full; Decisions on a local engine if one exists (**UNVERIFIED**); Anthropic and Ollama shapes already allowed | New items and engines (N7) | Rotate before apply (§4.5) | Own plan with receipt standard (6 plans) |
| A11 | open-webui | Tim (was `legacy-key-import`); kind stays service | Legacy open-webui | Local only | Anthropic API shape for the same models | New items (N7) | Rotate before apply (§4.5) | — |
| A12 | Anna's Museum App | Tim **UNVERIFIED** | Owner (local) | Local only — decision 3 | — | OpenRouter, TypeSafe; receipt full → basic | 1 (never used) | Disable; Local and paid |
| A13 | cap | Tim (was `legacy-key-import`) | Legacy cap | Disabled, Local only — decision 4 | — | All (key never used) | Revoked | Keep on Local only |
| A14 | waspflow-dsh-probe | Tim (was service account `gateway-admin`) | built-in default | Disabled, Local only — decision 4 | — | All (last used 2026-08-14) | Revoked | Keep on Local only |
| A15 | Paid provider proof | Tim **UNVERIFIED** | Owner (local) | Disabled, Local only — decision 4 | — | All (key used 2026-10-11 15:41) | Revoked | Keep on Local only, or on Local and paid (no change) |
| A16-A18 | Temporary empathy Jev triage ×3 | Tim **UNVERIFIED** | disabled | disabled, Local only | — | — | Already revoked | — |
| A19-A22 | copy-triage ×4 (one names Owner (local)) | Tim **UNVERIFIED** | disabled | disabled, Local only | — | — | Already revoked | — |

Not apps:

| # | Who | Change |
|---|---|---|
| N1 | Any device that reaches the gateway from the LAN with no key or a placeholder key; any client of public Ollama discovery; any setup that ran with auth off | **Loses all access** (401). Devices from D4 need keys first |
| N2 | Any client that sends a LiteLLM virtual key | **Loses access** (401). Clients from D5 need Azizam keys |
| N3 | People created later through OIDC without an invitation | No plan until the owner chooses one (today: built-in default) |
| N4 | Signed creation links | Stop working if the creator's app or person is disabled or the plan no longer allows the work; links to creations without a recorded creator per D9 |
| N5 | Any request that asks for more file metadata than its plans allow | Gets the plan's level (today a request may override) |
| N6 | Settings and Providers pages | "Default plan" gone; "Allowed plans" read-only |
| N7 | Every app and person | Models, workflows and new revisions added after the cut-over are not usable until the owner grants them (the "Who can use it?" step preselects plans with offer new on). Today the built-in default and plans without model lists include them at once |

## 9. Red-team resolution

### 9.1 Round 1 (v1), with round 2's status

Round 2 rated B1, M1, M2, M6, m2 and m3 partial or open in v2; the v3 text
below says what closes each. Round 2 rated M3, M4, M5, M7, M8, M9 and m1
resolved at design level, B3 sound at the inspected seam, and accepted B2's
rejection.

| ID | Finding | Resolution | Where |
|---|---|---|---|
| B1 | The gate authorizes a service stem, not the executed graph; unconfigured gate passes | **Applied.** v2: every pass case denies; negative tests; code confirms `category_enforcement.py:43-44`, `:135`, `:153-173`. v3 replaces the v2 contract (closure digest and phases) with `evaluate` plus one Permit checked again at every point of effect (RT2 B1, M1, M2) | §2.13, §2.8, §5.2 |
| B2 | Direct engine access as a boolean loses backend and operation bounds | **Applied in part.** Bounded grant (exact engines and operations; empty grants nothing); engine is the category subject; session identities denied; migration enumerates engines; tests for ungranted engine, operation, category and Azizam management routes. Rejected: "only checkable native adapters" and keeping engine-side management out of grants, because both need a parser per engine API (AGENTS.md binding principle; operation scopes are generic, `policy.py:326-366`). Azizam's own management routes stay never grantable (501); engine-side management is disclosed in the editor | §2.7, §4.3, §5.3 |
| B3 | Moving service apps to a person can make keys act as that person | **Applied** with existing fields: `apps.owner_user_id` is the policy and spending subject only; `person_for_app_credential` unchanged and still needs kind `personal` (`admin_runtime.py:1562-1572`); migrated apps keep kind `service`; missing owner row denies. A new `policy_account_id` field is not needed | §2.4, D7 |
| M1 | Spend ledger keyed by plan id; migration omitted it | **Applied.** Ledger by person, app, provider, attempt; reconcile first. Code `providers.py:303-316`, `:1060-1073`. v3: the ledger is in the full backup; history keeps one row each, unmatched rows are unattributed or owner-assigned (RT2 M3); no rollback after accept (RT2 B2) | §2.11, §4.2, §4.3, D8 |
| M2 | Per-request cap, periods, reservation lifecycle missing | **Applied.** `usd_per_request` kept per paid attempt; rolling day and UTC month kept with honest copy; reconciliation kept; `spending_unavailable` separate. v3 adds attempt identity, one reservation owner and the state table (RT2 M1) | §2.11, §3.3 |
| M3 | Provider ACL removal must translate the intersection | **Applied.** D2 matrix with absent/null/empty semantics; display-name wording fixed | §1.4, §4.3, D2 |
| M4 | Plan shape cannot express v3 rules or old axes | **Applied.** Normative schema with `op`, `values`, `waive_missing`; capability id table; field-by-field conversion; unrepresentable values block | §2.2, §3.1, §4.3 |
| M5 | Key-level and multi-plan collapse; richer oracle | **Applied.** Key groups per app need a decision; oracle over routes, engines, settings and reasons; scope stated as known subjects only | §2.4, D1, D3 |
| M6 | No cross-store commit protocol; approval defaults to accept | **Applied, simplified in v3.** Rows start `pending` (kept). v2's staged swap is replaced by stop, full backup, migrate in place, verify, owner accept; restore only before accept; crash table (RT2 B2, M4) | §4.1, §4.2, §5.6 |
| M7 | Session apps exist; a global Azizam app is wrong | **Applied.** Reuse each surface's existing app; no global app; triggers kept | §2.4 |
| M8 | Keyless LAN should not be an owner decision; finish the cut-over | **Applied.** Removal mandatory; the v1 keyless option is gone from the owner decisions; all five `legacy-` keys are rotated or revoked and apply enforces it | §2.8, §4.5 |
| M9 | Non-grant settings have no composition rule | **Applied.** Composition table; metadata becomes a bound (listed as N5) | §2.6 |
| m1 | Reason order differs from the kernel | **Applied.** Total step order across both plans; kernel loop change named (`category_policy.py:421-443`) | §2.5 |
| m2 | No-plan app states and cap copy incomplete | **Applied.** No operation clears an assignment (the clear path `admin_store.py:1337-1354` is removed); member errors name the next step; cap copy built from scope and period. v3 corrects the v2 claim "cannot be created": a crash between app row and assignment leaves an app that denies and shows "Choose a plan" (RT2 M5) | §2.4, §3.1, §3.3 |
| m3 | Purge scope and string-ban static guard | **Applied.** Purge removed (decision 4 v1 deleted); rotation done through Apps, not migration code. v3: the AST guard also catches string-key use of `allowed_plans` (RT2 m2); history is safe because restore is refused after accept (RT2 B2) | §4.5, §4.6, §5.3 |
| — | v1 said unlisted identities skip the gate | **Corrected**: both must be unlisted (`category_enforcement.py:116-117`) | §1.3 |
| — | v1 cut for simplicity | Per-person and per-app cap pairs collapsed to one cap set; purge operation, global Azizam app and keyless option removed; no operation leaves an app without an assignment | §2.11, §2.4, §2.8 |

### 9.2 Round 2 (v2)

| ID | Finding | Resolution | Where |
|---|---|---|---|
| RT2 B1 | The contract cannot tell apart actions that need different decisions | **Applied, minimal.** The Permit binds subjects (person, app, auth kind) and action (capability, members, `METHOD /path`, route); `check` compares it with the real effect at engine and provider boundaries; the provider boundary recomputes the cost bound from the body (`providers.py:790-815`). No engine parser; scopes stay generic (`policy.py:326-366`) | §2.13, §2.7, §5.2 |
| RT2 B2 | Post-commit rollback can erase spending, keys, revocations and audit | **Applied by removal.** Restore only before the owner accepts, when no traffic has run; after accept the tool refuses and §4.2 lists what a manual restore loses. The backup is all of `data/`, so receipts cannot outlive the restored stores | §4.2, §5.6 |
| RT2 M1 | Attempt ownership and Permit consumption undefined | **Applied.** Server-numbered attempts; `UNIQUE (request_id, attempt)` in the cap transaction; `authorize` is the only reserver (TypeSafe `providers.py:848-873`, OpenAI callers `:1754-1767`); re-authorization keeps the row; state table. Per-attempt `usd_per_request` kept (`:406-408`). **Rejected:** retry parentage; no counter or decision reads it | §2.11, §2.13, §5.4 |
| RT2 M2 | Permits survive revocation and disable | **Applied through the same freshness check.** `access_epoch`, bumped by triggers on every column step 1 reads (trigger pattern exists, `admin_store.py:177-188`), plus `not_after`; `check` runs after capacity waits, right before each send; linearization point stated | §2.13, §5.2 |
| RT2 M3 | Historic spend fallback undercounts and does not fit one row | **Applied.** One row per charge, never copied. App from the receipt join, person = that app's accountable person; no match = unattributed (provider and global only), or owner-assigned via a `pending` row when inside the current day or month. Never inferred from today's plan holders | §4.3, D8, §5.6 |
| RT2 M4 | Crash protocol lacks durability | **Applied by replacing the protocol.** Durability is needed at two points only: a fsynced, hash-listed backup and an atomically written marker before the first live change; all changes fsynced before `verified`. Interruption table covers restore, torn marker and bad backups | §4.2, §5.6 |
| RT2 M5 | Assignment-first creation races orphan cleanup | **Applied by removal.** App row first, then assignment; no cleanup exists. No code deletes app rows (no `DELETE FROM apps` in `gateway/`, `proxy.py`, `scripts/`), so the existence check cannot go stale | §2.4, §5.3 |
| RT2 m1 | A7 claims paperless is owner-independent | **Applied.** A7: owner's plan ∩ Chat | §8 |
| RT2 m2 | AST guard misses the string-key ACL | **Applied.** String-key use of `"allowed_plans"` fails; `providers.py:1288-1290` is the failing fixture; one exempt file | §5.3 |
| RT2 notes | Placeholder code; tie-break; opaque-engine pin; `evaluate` apart from admission; D3 pairs; MCP discovery; output check is not authentication; A3 "exactly" | **Applied** in §2.8, §2.5, §2.7, §2.13, D3, §5.3, §7 and §8. Owner decisions unchanged; decision 2's wording changed only because "today's rules exactly" was false for new items (N7) | as listed |

## 10. What we cut (v2 → v3)

| Removed from v2 | Replaced by |
|---|---|
| Staged `before/` and `after/` copies, per-file rename swap | Migrate the stopped gateway in place after one full backup |
| Manifest states `staged`, `committed`, `rolled_back` and the `recover` command that compared digests | One marker (`migrating`, `verified`, `accepted`) and `restore` |
| Rollback after commit (`before/` plus the previous release) | None. Restore only before accept; forward fixes after |
| `data/.access-migration.lock` held only by the tool | The gateway's own lock, taken by the tool too |
| Four phases (`list`, `dispatch`, `attempt`, `output`) as kinds of authorization request | Pure `evaluate` for everything that does not spend; `authorize` only before an effect |
| `closure_digest` and its canonical hashing | The member list, which the output check needs anyway (a digest cannot be re-evaluated against current plans) |
| Assignment-first creation and orphan cleanup on apply | App row first; nothing to clean up |
| Historic spend charged to every holder of the old plan | One row each; receipt join, else unattributed or owner-assigned |
| Exhaustive person × app oracle | Valid owner/app pairs plus fixed wrong-owner fixtures |
| Reservations inside provider helpers and in callers | One reserver: `authorize` |

Added, each because a finding needs it and nothing smaller covers it: the
`access_epoch` row and its triggers; `UNIQUE (request_id, attempt)`; the
gateway lock; the `accept` step.

## Appendix: research to decision trace

| Decision | Research |
|---|---|
| One plan per person and per app; intersection | R§20 Rank 1; OpenRouter R§11; v3 A10 |
| No runtime default; four meanings of default | R§22; Tailscale R§4; LiteLLM R§12; Jellyfin R§18 |
| Category rules inside plans, catalog beside | R§21; Jellyfin R§18; Tailscale Aperture R§4; Plex R§17 |
| Deny wins; unknown blocks | v3; AWS R§1; Jellyfin R§18 |
| Remove LAN bypass and path tokens | R§29; Plex NT4; Home Assistant NT2; Tailscale NT1 |
| Invite default is a future-only preselection | Slack R§7; Figma R§10; Notion R§9 |
| Spend by person and app, never by plan | OpenRouter R§11; R§23 |
| Preview, apply with revision, restore | Tailscale R§4; Cloudflare R§5; v3 |
| One-time migration, verify then remove | R§24; Microsoft R§3; AWS R§1 |
| No protocol restrictions | OpenRouter R§11; LiteLLM R§12; Open WebUI R§13 |

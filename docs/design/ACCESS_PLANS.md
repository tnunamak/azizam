# Access plans (design v4)

Status: proposed design, not implemented. 2026-10-11. Three independent
red-teams found blockers. Every blocker in rounds 2 and 3 came from one
mechanism: a Permit decided early and checked again later. v4 deletes that
mechanism. The deciding authorization now happens at the moment of each
external effect, from the current key or session, person, app and plan, and
the same step reserves spending (§2.13). §10 lists what v4 cut; §9 maps every finding.
A go decision on this version is still required before any build starts
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
- Red-team of v3: `~/Documents/azizam/ux-findings/research-results/azizam-redteam3-access-plans-b81a9a5-r0.md`.
  Cited as [R3-B1] … [R3-m1].
- Owner visibility: `design/owner-visibility:docs/design/OWNER_VISIBILITY.md`
  (v2, `81ce3204`). Cited as [OV §n]. It owns the shared ownership model
  (personal and service apps, the accountable person, the app-binding
  triggers). This design uses that model and does not restate it. OV is
  being revised in parallel; section numbers are v2's.
- Owner data preview: `~/Documents/azizam/ux-findings/reports/access-plan-migration-preview-2026-10-11.md`
  (read-only copy of `admin.db`). Cited as [P§n].
- Divan `local-ai-gateway/`, 2026-10-10: the categories v3 evidence.
- In-flight branch `fix/access-hygiene`, which this design assumes has landed:
  Try keys bound to the sign-in session and expiring with it; the OIDC client
  secret in the secret store; plan deletion refused while keys, apps, people,
  invitations or mappings use the plan; `legacy_identity_profile_grants`
  dropped. This design does not redesign those.
- Code: `file:line` references are at `e20b3882` (the same code as
  `b81a9a5c`), except the v4 store and lock references in §4.2, which are at
  `8a3147f0`.

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

Two more fallbacks make an empty plan mean "everything" (`profiles.py:365-371`;
`runtime_auth.py:283-302`). 12 active keys on 5 apps resolve to the default
[P§4]. The default (`DEFAULT_PROFILE`, `gateway/policy.py:280`) grants every
local model, workflow and capability, may load models, and has no paid
providers, no direct engine access, basic receipts and no cap [P§1]. The
repository config turns LAN bypass on for 127/8, ::1, 192.168/16, 10/8 and
100.64/10 (`proxy_config.yml:1432-1434`); the deployed config is
**UNVERIFIED**.

### 1.3 Two systems called "plan"

1. **Access plans**: `profiles` table, `AppProfile`, `gateway/policy.py:373`.
2. **Category plans**: a snapshot in `categories.db`, evaluated by
   `gateway/category_policy.py:375`, attached in `PolicyVersion.people/apps`.

The category gate only tightens an allowed decision, and it passes when: no
gate is configured (`gateway/category_enforcement.py:153-162`); the store
file was never opened (`:68-70`); there is no active snapshot (`:87-90`,
`:112-113`); both the person and the app are unlisted (`:116-117`; the LAN
identity has neither). `visible()` passes with no gate (`:171-173`). The
adapter resolves the service by its stem without a revision (`:43-44`,
`:135`) and never passes phase, output or metadata completeness
(`category_policy.py:482`, `:497`) [RT B1]. Whether an active snapshot
exists on the instance is **UNVERIFIED**.

### 1.4 Two provider allow-lists

A paid provider call passes only if both the plan's `remote.providers`
(`gateway/policy.py:517-534`) and the provider's `allowed_plans`
(`gateway/providers.py:1281-1302`) agree [RT M3]. For `allowed_plans`, an
absent key means no restriction; `null`, an empty list or an invalid value
denies; a non-mapping policy counts as absent (`:1278`). Membership
compares the plan's JSON `id`, not its row id. On this instance both
providers list `["owner-local"]` [P§2]. Renaming a plan keeps the JSON id
(`gateway/admin_store.py:1082-1092`); changing the id or merging plans
would drop provider access.

### 1.5 Spending today

Plan caps add up `provider_spend` rows by `profile_id`
(`gateway/providers.py:303-316`, `:1060-1073`). The ledger has no person or
app column, so every app on one plan shares one total [RT M1]. The day is a
rolling 24 hours (`:425`, `:1131`); the month is the UTC calendar month
(`:1091-1093`). A per-request ceiling exists (`gateway/policy.py:74`,
`providers.py:406-408`). Unknown costs become `needs_reconciliation`, which
blocks new paid requests in scope until reconciled (`providers.py:352-376`,
`:564-570`, `:1032-1044`, `:613-649`). No stored plan sets a cap [P§2].
Provider and global caps add up by provider only (`:1000-1012`,
`:1066-1075`).

### 1.6 Other legacy items (P11)

Five `legacy-` keys and the `legacy-key-import` service account that owns
daisy, cap and open-webui [P§6]; `runtime_profile_mappings` (2 rows,
disabled) and the YAML bootstrap maps (`proxy.py:1939-1958`);
`ModelAccessPolicy` (`gateway/policy.py:137-141`, `:170-173`); the `default`
and `default-local` ids (`gateway/admin_store.py:473`, `:2001`); first-run
seeding of live plans (`gateway/default_profiles.py`); the category
kernel's legacy mode (`category_policy.py:256-257`, `:306`, `:327`); the
"Let people create their own apps" toggle changing which plan an app gets
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
  person**, which is a stored choice, not a fallback. No operation clears
  an assignment; to stop an app, disable it [RT m2].
- **App creation writes the app row first, then the assignment** [RT2 M5].
  The app row is one `admin.db` transaction (`admin_store.py:1181-1221`;
  session apps `:1268-1303`). The assignment is a compare-and-swap in the
  access settings, accepted only if the app row exists. No code deletes
  app rows (only `disabled_at` is set, `:1373-1388`), so there is no orphan
  to clean up. If the second write fails, the app exists without an
  assignment: step 3 denies it (`no_access_plan`, app side) and Apps shows
  "Choose a plan". `ensure_session_app_for_user` writes a missing "Same as
  its person" at the next sign-in. An invitation accept that stops after
  its rows commit leaves a person and "My first app" with no plan; People
  shows "Choose a plan". Plan deletion and assignment change the same
  document, so compare-and-swap orders them.
- **Keys have no plan.** A key acts for its app; `credentials.profile_id`
  goes away. If an app's active keys have different effective plans today,
  the migration requires a choice per app: split, narrow or revoke (§4.4)
  [RT M5].
- Every app has an **accountable person**, `apps.owner_user_id`, in the
  ownership model of [OV §3.1, §3.5]: a `personal` app's owner never
  changes; a `service` app's accountable person changes only while it has
  no unrevoked key; `admin.db` triggers enforce both and allow the
  migration's first assignment from a service account. `create_app`
  changes with them, since today it requires a service account for service
  apps (`admin_store.py:1193-1198`). The person must exist and be enabled;
  a missing row denies (today it is accepted,
  `gateway/admin_runtime.py:1694-1701`) [RT B3].
- A request runs only if **both** the accountable person's plan and the
  app's assignment allow it (v3 A10; §2.6).
- For plans, the accountable person is the policy and spending subject;
  [OV §3.1] also lets them see what a service app made. It never makes a
  key act as that person for personal data: `person_for_app_credential`
  returns a person only for a `personal` app whose key its owner created
  (`admin_runtime.py:1562-1572`). Migrated apps stay `service`, so moving
  daisy to Tim does not give daisy's key Tim's saved prompts [RT B3]. Its
  consumers are **UNVERIFIED**; the build traces them first.
- A role (Owner, Admin, Member) controls management screens, never
  generation. The admin API does not depend on plans, so the owner can
  always repair a plan that locks them out [R§20 item 5].
- **Browser surfaces** keep their identities [RT M7]: Make uses the
  per-person session app (`gateway/admin_store.py:139-188`,
  `:1268-1303`), Home the person's personal app (`home_api.py:49-65`), Try
  the person's Try app with session-bound keys (fix/access-hygiene). Each
  is an app with an assignment; the migration sets "Same as its person",
  except the owner's own (decision 1, §7). Which app Library uses is
  **UNVERIFIED**.

One plan per side makes "which plan applies?" one answer [R§20 Rank 1].

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
| 5 | Grants: person's plan, then the app's plan. Capability and item revision; each paid route in Paid providers; each fallback route needs fallback on; direct engine scope. For a model that is not running, May load models on both sides (§2.13, effect `load`) | 403 `not_in_access_plan` (side named); 403 `cannot_start_model` | access plan |
| 6 | Must match rows: person's plan, then the app's plan | 403 `not_reviewed` (missing value), `blocked_by_category` (known mismatch) | access plan |
| 7 | Capacity admission (`HEADLESS_AND_RESOURCES.md`, Release A) | as designed there | gateway / engine |
| 8 | Paid routes only: per-request ceiling, then one atomic reservation against every applicable cap (§2.11) | 429 `cap_reached`; 503 `spending_unavailable` | spending |

When both sides hold the same plan, it is evaluated once and the
explanation says "your access plan". The kernel today checks grants and
requirements per plan in one loop (`category_policy.py:421-443`); the build
changes it to the order above. Within one step, rows are checked in document
order and closure members in graph order, so the first reason is stable.

Steps 2-6 are one pure function, `evaluate` (§2.13). Discovery, Check
access, the change preview and the migration oracle call only `evaluate`.
The request handler runs steps 1-6 at entry, so a denied request fails
before it waits for capacity (step 7). That answer is only early. The
decision that lets an effect happen is `authorize`, which runs steps 1-6
again and then step 8 at the moment of the effect, after any wait. A
capacity slot taken at step 7 is released if `authorize` denies.

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
- Azizam does not parse an engine's own API (AGENTS.md), so it cannot see
  which model a direct call uses. The **engine** is the closure member that
  category rules check; the owner's tag on an engine covers everything
  behind its API. A direct call pins the engine, not a model revision
  [RT2 note on B2].
- The effect that `authorize` checks for a direct call is the engine id plus
  the canonical `METHOD /path` of the request about to be sent, so
  `GET /props` and `POST /delete` on one engine are different effects
  (§2.13) [RT2 B1].
- `"*"` can include the engine's own model management. Azizam cannot tell
  which engine paths manage models without engine knowledge in code, so
  the owner narrows with operation scopes, and the editor says: "This app
  can do anything {engine}'s own API allows, including loading or deleting
  models." Azizam's own management routes stay 501.
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
| Signed creation links | Governed by [OV §3.4]: signature plus a grant (`g`), decided from the app and maker that the creation records. Access plans govern effects, not stored files; v4 removes v3's re-check of a file against current plans (§2.13) | `proxy.py:23631-23670` |
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
blocked items; plans without the row ignore the tag. A block stops new
generation only: files already made stay visible to whoever could see them
before [OV §3.1]. The review screen says so: "Files already made stay in
Library."

### 2.11 Spending

Resolves [RT M1, M2; RT2 M1, M3; R3-B2].

- **Ledger**: one row per paid external call (an **attempt**), with the
  person, app, provider, request id and `attempt_id`, but no plan. The
  `person_id` is the spending subject (the accountable person), not the
  data person that [OV §3.1] reads from receipts and creations. Plan
  changes, renames and reassignments never reset a counter. Counters are
  sums over the ledger by person, app, provider or all, per period; each
  row counts once in each counter it belongs to and is never copied.
- **Caps** on one paid attempt: the person plan's caps on the person
  counter, the app plan's caps on the app counter, provider caps, global
  caps. With "Same as its person" the same amounts cap both counters; the
  person counter already includes the app's spend, so nothing is charged
  twice.
- **Periods** stay: day = rolling 24 hours; month = UTC calendar month;
  `usd_per_request` per attempt. Copy shows the real reset: "You can spend
  again from 14:32"; "Resets on 1 November at 01:00" (reader's time zone).
- **One row per send.** Each paid helper, retry and fallback is its own
  `authorize` and its own row, created in the cap transaction
  (`BEGIN IMMEDIATE`, today `providers.py:417-531`). `attempt_id` is a
  random 128-bit id made there, unique in the ledger, never sent to a
  client. The request id moves into the insert (today it is attached
  after, `:579-589`). No retry links are kept; no counter reads them.
- **Single use** [R3-B2]. Immediately before the send, `dispatch` claims
  the row: `UPDATE provider_spend SET status = 'sending' WHERE
  attempt_id = ? AND status = 'reserved'`, and sends only if one row
  changed. A second claim
  changes nothing and sends nothing. The claim commits durably first
  (`synchronous=FULL` on the ledger; today's setting is **UNVERIFIED**).
- **One reserver**: `authorize`. TypeSafe stops reserving inside
  `call_typesafe_systemone` (`providers.py:848-873`) and the OpenAI-shaped
  callers around `call_openai_json` (`:1754-1767`).
- **States**:

  | From | Event | To |
  |---|---|---|
  | — | `authorize` allows a paid effect | `reserved` |
  | `reserved` | claim succeeds | `sending` |
  | `reserved` | `dispatch` stops before the claim, or restart | `released` (never sent) |
  | `sending` | sent, cost known | `settled` |
  | `sending` | connection failed before any request byte, or a rejection today's code treats as unbilled (provider auth failure, rate limit; `providers.py:900-902`) | `released` |
  | `sending` | any other failure, unknown cost, or restart | `needs_reconciliation` |
  | `needs_reconciliation` | reconciliation | `settled` |

  Each transition is a conditional update on the expected status (settle
  requires `sending`; today `reserved`, `providers.py:566`). Caps count
  `reserved`, `sending`, `settled` and `needs_reconciliation` (today the
  list lacks `sending`, `:1069`, `:1211`). A `reserved` row past its
  `expires_at` (seconds; a reservation never waits) is released by
  recovery, in process as well as at restart, because it was never sent.
  Recovery (`recover_provider_spend`, `:352-376`) turns `sending` rows of a
  dead process into `needs_reconciliation`; today it does that to every
  open row.
  `needs_reconciliation` blocks paid requests in every capped scope that
  contains the row until reconciled (`:1026-1044`).
- **Unavailable**: if the ledger cannot be read or written, paid routes
  fail with 503 `spending_unavailable`. Local routes still run.

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
replaced by `person_id`, `app_id` and `attempt_id` (all three nullable for
history only, §4.3); `attempt_id` is unique; the `sending` state is added.
The access settings stay in the categories store beside the admin
database (`proxy.py:15588-15592`).

Creating a person or app writes the `admin.db` row, then the assignment
(§2.4). An invitation's plan was previewed when sent, so accept commits it
without another review. `delete_plan`'s "in an open invitation" check reads
`admin.db` and can race a new invitation; the race fails closed: accept's
compare-and-swap fails for a deleted plan, and the person shows "Choose a
plan".

| v3 text | Change |
|---|---|
| Migration R3: legacy mode with any-tag OR | Removed. The kernel drops `mode` and `legacy_tags`. No stored plan sets tags [P§2] |
| Plans live apart from access plans | The snapshot's plans are the access plans |
| Adapter: no gate, no snapshot, unlisted identities pass | Removed (§2.8) |
| Several plans per identity | The store allows one per side |
| Waiver as `missing: {row: allow}` on the plan | Kept in the kernel; the API puts it on the row as `waive_missing` |

### 2.13 Authorization at the moment of effect

Resolves [RT B1; RT2 B1, M1, M2; R3-B1, B2, M1, M2]. v2 and v3 decided
early and carried the decision (a digest, then a Permit) to the effect,
where it had to be proved fresh, equal to the effect and used once. Each
round broke that proof. v4 carries nothing: it decides at the effect, from
current state.

```
evaluate(settings, subjects, effect) -> Allow | Denial     pure; steps 2-6
authorize(auth, effect) -> Allow(attempt?) | Denial        steps 1-6 and 8

auth      AppKey(key_hash)
          | Session(person_id, session_id, expires_at, surface)
          server-held; a session's fields come from its signed cookie
subjects  person_id, app_id, auth_kind; resolved from auth
effect    effect_of(outbound): derived from the request about to be sent:
  kind         generate | load | direct
  item         the granted model or workflow, with revision (direct: none)
  member       the route, helper or engine called, with revision
  capability   the item's capability (direct: direct_engine)
  closure      every member of the item's pinned graph
  route        local or a provider id; fallback yes or no
  operation    direct only: canonical "METHOD /path"
  bounded_usd  paid only: from the body about to be sent (providers.py:790-815)
```

**One dispatch path.** Every engine send, provider send, model load and
direct call goes through `dispatch(auth, outbound)`. `outbound` is a typed
request: target member, destination, method and path, body. `dispatch`
derives the effect from it with `effect_of`, runs `authorize`, claims a
paid attempt (§2.11), and sends that same object. A caller cannot declare
an effect that differs from what is sent [R3-M1]. Nothing outlives the
call, so nothing can be replayed. The route inventory and an AST check
(§5.2, §5.3) prove no other path reaches an engine or provider; a fixture
per call site proves its `outbound` yields the expected effect.

**`authorize`**, in order:
- (a) One `SELECT` on `admin.db` resolves the identity with every state
  column of §2.5 step 1. For a key: the credential by key hash, its app
  and the accountable person. For a session: `now < expires_at` from the
  signed cookie; no `account_session_revocations` row for the session id;
  the person's `account_access` row exists and is enabled; if it has
  `require_registered_session`, a live `account_sessions` row; and the
  person's surface app. One statement is one snapshot. The runtime
  connection is in autocommit (`admin_store.py:575-581`), so this must
  stay one statement [R3-B1]. A revoked key or signed-out session fails
  even when another key of the app or session of the person works. A
  missing `account_access` row denies (today `session_allowed` accepts it,
  `accounts.py:149-167`); D1 lists people without one, and apply creates
  their rows as approved rows.
- (b) Read the active access settings revision (cached by `PRAGMA
  data_version`, `category_enforcement.py:47-55`).
- (c) Run `evaluate`. A denial ends here, without touching the ledger.
- (d) Paid only: `BEGIN IMMEDIATE` on the ledger (it serializes reservers,
  as today's cap transaction does); per-request ceiling,
  `needs_reconciliation` block, caps; insert the `reserved` row with a new
  `attempt_id`; commit.

A session stays tied to its signed-in person. Its app is that person's
surface app (§2.4), a `personal` app whose owner the [OV §3.5] triggers
fix; if the owner differs, `authorize` denies.

**Freshness.** Every change committed before (a) is seen: revocation,
sign-out, expiry, a disabled app or person, a new accountable person, a
plan or tag change. No epoch or trigger is needed [R3-B1]. The stores are
read separately, but each read returns that store's latest commit, and the
one cross-store link (an app's assignment) denies when missing. The
**linearization point** is (a): a change after it does not stop this
send; it stops the next `dispatch`.

**Waits and jobs.** Capacity admission (step 7) comes before `dispatch`,
so `authorize` always follows any wait. A job stores its `auth` with the
job: the key hash (stored today, `media_jobs.py:187-190`) or, new, the
session's person id, session id and expiry. It dispatches each external
call with it. A revocation, sign-out or session expiry stops it at its
next call.

**Every dimension is checked directly** [R3-M1]. No earlier action has to
match the effect. `evaluate` checks: the grant for (the item's capability,
item revision) on both sides; that `member` is in the item's pinned graph
at that revision (a helper needs no grant of its own: the owner granted
the pinned graph); the route against Paid providers and
fallback; for `direct`, engine, `METHOD /path` and `app_key` (§2.7); every
`closure` member against category rules. Loading is its own effect,
`load`: the item granted and May load models on both sides (today
`may_activate` versus `existing_only`, `policy.py:113-134`). Otherwise
`cannot_start_model`.

**Files are not re-authorized** [R3-M2]. v3 re-ran `evaluate` on a
creation at download. That needed the capability, route and fallback of
every effect behind the file, which no creation records: creations are
content-addressed and their sidecar holds only owner principals and makers
(`proxy.py:7006-7055`). It also guarded bytes the client already had.
Access to a stored file is [OV §3.1, §3.4], from the recorded app and
maker.

Other rules:
- Discovery calls `evaluate` per item and never reserves.
- Deny if: the gate is not initialized; no valid active revision; the
  credential or session is unknown, revoked or expired; the app or person
  is disabled or missing; closure metadata is missing; an effect cannot be
  built before it happens.
- [OV §3.5] item 3 says a service-app transfer "invalidates in-flight
  permits" through `access_epoch`. v4 has neither; the revoked keys fail
  at their next `authorize`. The OV revision should drop the reference.

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
| `cannot_start_model` | {model name} isn't running, and your access plan can't start it. Try again when it's running, or ask {owner name}. |
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

1. **Dry run**, from the predecessor release (§4.2), so the old evaluator
   computes "today" and the new release ships none of it [RT m3]. It reads
   the store manifest (§4.2) and copies of every store:
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

Resolves [RT M6; RT2 B2, M4; R3-M3, M4, m1]. The stores are separate
files, so no transaction covers them. Apply migrates the stopped gateway
in place after a verified backup; the only recovery is to restore that
backup, before any traffic.

**Store manifest** [R3-M4]. Stores need not be in `data/`: admin
(`AZIZAM_ADMIN_DB` or config `admin.db_path`, `bootstrap_config.py:28`),
ledger (`AZIZAM_PROVIDER_LEDGER_DB`, `providers.py:35`), receipts
(`AZIZAM_RECEIPTS_DB`), jobs (`AZIZAM_MEDIA_JOBS_DB`), config
(`AZIZAM_CONFIG_PATH`, `bootstrap_config.py:15-16`), and the categories
store beside the admin database (`proxy.py:15588-15592`). The tool
resolves each with the gateway's own resolver, then `realpath`, and lists
every SQLite store the gateway opens plus the config: configured path,
real path, size, SHA-256. Media files are not in it; apply does not write
them. Apply refuses before any change if two resolvers disagree about one
store (today they can: `proxy.py:12956` recovers
`ADMIN_DB_PATH.parent / "provider_spend.db"` while the ledger follows
`AZIZAM_PROVIDER_LEDGER_DB`, and `providers.py:1259-1261` derives the
admin database from the ledger's directory; the build makes one resolver
per store), if two entries share a real path, or if a store the gateway
opens is missing from the list.

**Lock and control record** [R3-M3]. The gateway holds an exclusive
`flock` on `azizam.lock` for its whole life; apply, restore and accept hold
it to the end. Per-store `.lock` files (`sqlite_startup.py`) are not
enough: they are held only while a connection is open, and the ledger and
categories store do not use them (at `8a3147f0`). The lock and the control
record `access-migration.json` live in the admin database's real
directory. Restore replaces files, never directories, so it cannot remove
either.

**Predecessor release** (the last before the cut-over): the gateway lock,
the dry-run tool, and two startup checks [R3-m1]. It reads the schema
version before any DDL and refuses a newer one without writing (today
`executescript(_SCHEMA)` runs first, `admin_store.py:82-188`, `:284-297`),
and it refuses to start while the control record says `migrating`,
`verified` or `restoring`.

`scripts/access-migrate apply`:

1. **Preconditions**, under the lock: every `approval.yaml` row decided
   with a matching digest; inputs equal the dry run's; the manifest checks
   pass; no active key keeps a `legacy-` prefix (§4.5); no ledger row is
   `reserved`, `sending` or `needs_reconciliation`; enough free space
   (D10).
2. **Backup**. `PRAGMA wal_checkpoint(TRUNCATE)` on every store. Copy
   every manifest file to `access-backup-<id>/` in the admin database's
   directory; fsync files and directory. Write `backup.json` (manifest
   plus each copy's SHA-256) atomically: temporary file, fsync, rename,
   directory fsync. A backup without a matching `backup.json` is never
   used.
3. **Control record** `migrating`, with the backup id and the digest of
   `backup.json`, written the same atomic way before any store changes.
4. **Migrate in place**. `admin.db` first; its first transaction sets
   schema version 6, which the predecessor refuses. Then the access
   settings, the ledger, the config. Each store change is one transaction
   with `synchronous=FULL`; the config is written atomically.
5. **Verify**, gateway stopped: new schema; no removed table, column,
   setting, config key or plan name; the §4.4 oracle, run with `evaluate`,
   equals the approved matrix; provider and global ledger totals per
   period unchanged; approved revocations and disables applied; every app
   has an assignment; unmigrated stores (receipts, jobs) still match their
   backup hashes; the new release's startup checks pass read-only, without
   a listener. Fsync, then set `verified`. On failure the state stays
   `migrating` and the tool prints the restore command.
6. **Report**: "Verified. Nothing has run on the new rules yet. To start:
   `access-migrate accept`. To go back: `access-migrate restore` (allowed
   until you accept)." `access-migrate check …` runs `evaluate` offline.

`accept` takes the lock and sets `accepted`.

**`restore`** runs in `migrating`, `verified` or `restoring`, under the
lock. It checks every backup file against `backup.json` and refuses,
changing nothing, on a mismatch. It sets `restoring`. A destination is
**restored** when its SHA-256 equals the backup's and no `-wal` or `-shm`
file is beside it. For each manifest entry that is not restored, in this
order, each step skipped if already done:
1. Copy the backup to `<real path>.access-restoring`; fsync it.
2. Rename `-wal`, then `-shm`, to `<name>.access-failed-<id>` (kept,
   P19). A stale WAL beside a restored database would be replayed into
   it; moving them first means a crash never leaves a restored main file
   beside an old WAL.
3. Rename the destination to `<name>.access-failed-<id>`, unless that name
   exists already (an earlier run moved it).
4. Rename the copy into place; fsync the directory.

If an `.access-failed-<id>` name exists and its source does too, something
outside the lock wrote the store; restore stops and names the file.
Rerunning `restore` repairs any crash. Only when every destination is
restored does it set `restored`; until then both releases refuse to start
[R3-M3]. Then the owner runs the predecessor release. Apply may run again
after `restored`, with a new backup id.

**Start rules.** The predecessor starts with no control record or with
`restored`; its schema check refuses migrated stores. The new release
starts only with `accepted`, or on a fresh install whose `admin.db` it
creates. A migrated `admin.db` without a control record refuses to start:
"The access migration record is missing."

**After accept there is no tool rollback** [RT2 B2]; `restore` refuses.
A manual restore would delete every charge (caps undercount), key,
revocation (a revoked key works again), audit event, receipt and job since
accept. Fixes after accept are forward, through the API.

| Stopped during | Found next | Result |
|---|---|---|
| Backup, before `backup.json` | no record | Stores unchanged; apply deletes the incomplete backup and starts again |
| After `backup.json`, before the record | no record | Stores unchanged; apply starts again |
| Record write | old or new record (atomic rename) | As for that state |
| Migrate or verify | `migrating` | Both releases refuse to start; restore, then apply again |
| After `verified` | `verified` | Accept or restore |
| Restore, any step | `restoring` | Both releases refuse to start; restore again finishes |
| Record unreadable | — | Everything refuses; the owner uses the backup path printed and logged at step 2 |

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
| Category snapshot plans | If the active snapshot governs anyone, each attached plan's rows must map into one access plan per identity, or apply refuses | **UNVERIFIED** (categories store on the instance) |
| Ledger `profile_id` | One row each, `attempt_id` null [RT2 M3]. `app_id` from the receipt with the same request id; `person_id` = that app's accountable person after the move (D7). No receipt match (`request_id` may be null, `providers.py:303-316`): **unattributed**, counted in provider and global totals only; inside the current day or month it is a `pending` approval row where the owner names a person and app or accepts "unattributed". Never inferred from today's plan holders | **UNVERIFIED** (ledger on the instance); no stored plan sets a cap [P§2], so today no person or app counter depends on these rows |

### 4.4 Checks the dry run must report

| # | Check | Why |
|---|---|---|
| D1 | Every person, app and active key with today's effective plan; each app's active keys grouped by effective plan; each key's `created_by` against its app's accountable person after the move | An app with more than one group needs a split, narrow or revoke decision [RT M5]. Keys whose maker is not the accountable person are the ones [OV §3.5] item 4 revokes later; the owner replaces them once |
| D2 | Provider matrix: each old plan × provider, plan side, provider side (absent, null, empty, string, list), effective result; duplicate JSON ids | Paid access comes from the intersection [RT M3] |
| D3 | The oracle: for every **valid** (app, its accountable person) pair × capability × item revision × route (local, each paid provider, fallback) and each engine × direct operation, old decision and reason against new; plus per pair: receipt detail, metadata, model loading, caps. Results are reused per plan pair. Wrong-owner pairs are covered by fixed adversarial fixtures (§5.6), not by the full product [RT2 note] | Every difference must be a row of §8 or apply refuses. Covers known subjects only, not future traffic |
| D4 | Receipts in the last 90 days by app and for `api_key_name = "lan-bypass"` (written today, `proxy.py:2489`, `:29844`) by path and capability | Keyless traffic that will stop. Receipts have no client IP (`gateway/receipt_store.py:67-92`), so the owner must name the devices |
| D5 | Whether LiteLLM is on; delegated `sk-` traffic | Clients that need Azizam keys. Whether delegated requests write receipts is **UNVERIFIED** |
| D6 | The deployed config's `auth` block, `public_paths` and `ollama_compat_discovery` | Confirms LAN bypass, auth-off and public discovery state; flags public entries that match protected routes |
| D7 | Each app's accountable person after the move, that person's plan, and the app's effective access (person ∩ app); apps whose person would be missing or disabled | The owner approves the owner column of §8; a missing person blocks apply [RT B3] |
| D8 | Ledger rows in the current day and month with their attribution (§4.3); rows with no receipt match; any `reserved` or `needs_reconciliation` rows; provider and global totals per period | Reconcile before apply; unattributed in-window rows become `pending` approval rows; apply verifies the totals are unchanged [RT M1; RT2 M3] |
| D9 | Stored values listed as unrepresentable in §4.3 | Blocks apply |
| D10 | The store manifest (§4.2): configured and real path, size and filesystem of every store and the config; resolver disagreements; free space on each filesystem | The backup needs the manifest's total size in the admin database's directory; a restore needs, on each filesystem, the size of the files restored there. Apply refuses without it. Layout and sizes on this instance are **UNVERIFIED** |

### 4.5 Prerequisites on this instance

Done by the owner through the normal Apps and Providers flows before apply
[RT M8, m3]. Revocations and disables listed in §8 are approved rows that
apply performs; these steps are the ones that need a client changed:
1. `fix/access-hygiene` has landed (it revokes unbound Try keys once).
2. Create new keys for **daisy** and **open-webui** in Apps, as the person
   who becomes their accountable person (Tim), update those clients, and
   revoke their two `legacy-` keys. This uses today's key rules; it must
   happen before [OV]'s switch, which allows key creation only by the
   accountable person [OV §3.5]. The other three `legacy-`
   keys (simon, librechat, cap) were never used; apply revokes them as
   approved rows. Apply refuses if any active key would still have a
   `legacy-` prefix after the approved revocations, so no legacy string stays
   in live data (P11). Receipts and audit events keep the old names as
   history (P19).
3. Give every LAN device found in D4 an app and a key.
4. Give every client found in D5 an Azizam app key.
5. Reconcile any ledger row that D8 lists.
6. Deploy the predecessor release (§4.2). It carries the dry-run tool, the
   gateway lock and the early startup checks, so the gateway that apply
   stops already takes the lock.

**Order with owner visibility.** This cut-over is accepted before [OV]'s
privacy switch. OV's Accountable basis needs the people that §8 assigns to
service apps; before this lands it matches nothing [OV §12].

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

### 5.2 Authorization at the moment of effect (§2.13)

- One engine, one plan granting only `GET /props`: `POST /delete` through
  `dispatch` is denied; the engine is not called [RT2 B1].
- An `outbound` whose target member is not in the granted revision's
  graph, or is at another revision: denied. Per call site (TypeSafe,
  OpenAI-shaped, native, engine dispatch), a fixture `outbound` yields
  exactly the expected capability, member, route and operation [R3-M1].
- Generation granted, May load models off on one side, model not running:
  `cannot_start_model`, nothing loaded. Same with the model running:
  allowed, no `load` effect [R3-M1].
- A session identity on a direct engine route: denied.
- Two keys on one app: revoke key A while a request on A waits for
  capacity; A's next `dispatch` is denied and sends nothing; key B keeps
  working. The same with two sessions of one person and sign-out or expiry
  of session A [R3-B1].
- Revoke, expire, disable the app, disable or remove the person, or sign
  out, each committed after entry and before `dispatch`: zero sends, no
  ledger row.
- A change committed between the `admin.db` read and the send: this send
  happens; the next `dispatch` of the same job is denied (the stated
  linearization point).
- Change a service app's accountable person between two calls of one job
  (keys revoked first, as [OV §3.5] requires): the next call is denied.
  Change the plan of the accountable person instead: the next call uses
  the new plan.
- Gate not initialized, store file missing, no active revision, no
  assignment for the person or the app: deny.
- `evaluate` never reserves; discovery output passed to an engine route is
  not an authorization.
- Static: no engine or provider client is called outside `dispatch` (an
  AST check like §5.3's guard).

### 5.3 Server denial tests (every path)

Extend the route inventory test so every protected route (HTTP, browser
Home, Make, Try and Library, MCP, signed links, async jobs, native) shows
that each of its effects goes through `dispatch` (§2.13), and that
discovery never stands in for it [R3 route coverage]. A mock engine fails
the test if called.

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
on both sides charged once; unknown cost;
reconciliation; ledger unavailable gives `spending_unavailable` while local
routes run; rolling-day and UTC-month boundaries; per-request ceiling.

Attempts [RT2 M1; R3-B2]: a workflow with two paid helpers writes two
rows; a retry and a fallback each add one row; two concurrent claims of
one `attempt_id` send once (the loser changes zero rows and sends
nothing); a crash after the insert and before the claim leaves `released`
after restart; a crash after the claim leaves `needs_reconciliation`,
never `released`; a connection refused before any request byte leaves
`released`; a provider rate limit or auth failure leaves `released`; a
`sending` row counts toward caps; a `reserved` row past `expires_at` in a
live process is released; settle on a row not in `sending` changes
nothing; TypeSafe
and an OpenAI-shaped route both reserve only through `authorize`. Each
case: exactly one row per actual send, and provider and global totals
count each row once.

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
- Crash injected before and after each §4.2 step and after each restore
  step for each manifest entry, including after `-wal` moved and before
  the main file did, and after the databases are restored and before the
  config is, a torn control record, and a backup
  with a missing or changed file: each interruption row holds; running
  restore again finishes it; the result is every manifest file migrated
  and verified, or every one byte-equal to the backup; neither release
  starts in between [RT2 M4; R3-M3].
- Layout fixtures [R3-M4]: the admin database and the ledger each outside
  `data/`, a store reached through a symlink, and the two ledger resolvers
  disagreeing. External stores are backed up and restored at their real
  paths; a disagreement refuses before any change. A stale `-wal` beside a
  restored database is moved aside, not replayed.
- Store inventory: every SQLite store the gateway opens is in the
  manifest.
- Serve traffic after accept (a paid charge, a new key, a revocation, a
  reconciliation, an audit event), then run restore: it refuses and changes
  nothing [RT2 B2].
- The new release with a migrated `admin.db` and no control record
  refuses to start.
- The predecessor release started against a migrated `admin.db` refuses
  it, and the file's SHA-256 is unchanged afterwards; started while the
  control record says `migrating`, `verified` or `restoring`, it refuses
  [R3-m1].
- Unattributed ledger rows [RT2 M3], with and without a receipt match:
  in-window unmatched rows are `pending`; totals unchanged; no row
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
| `authorize` runs at every external effect: one `admin.db` `SELECT`, a cached settings lookup and, for paid calls, the ledger transaction | Same cost order as today's per-request auth and cap check; a workflow pays it per helper. Measure before release |
| A browser-started job stops after sign-out or session expiry (§2.13) | Expected for revocation. If session lifetime cuts real jobs short, the job page says why; measured in the build |
| The owner restores the backup by hand after accept | Restore after accept is refused by the tool; §4.2 lists exactly what a manual restore loses |
| The backup of every store is large (receipts) | D10 reports sizes and free space per filesystem; apply refuses without the space |
| A crash between app row and assignment | The app denies and shows "Choose a plan" (§2.4); never a widening |

## 7. Owner decisions

Each row has a recommended default; everything else is decided above. The
defaults add no paid or direct engine access to automated apps. Decision 1
adds paid models to your own browser use; decisions 3 and 4 remove access
from apps that have not used it (Paid provider proof: confirm its one use
was a test). §8 lists every gain and loss.

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
| P1 | Tim Nunamaker | no plan → built-in default | Everything | Tim's person plan now bounds Tim's apps (they all fit inside Everything) |
| P2 | Simon Agent | no plan → built-in default | Local only | None |
| P3 | Tim ODL | Assistant | Chat | None |

Apps. "Owner after" is the accountable person. D7 confirms rows marked
**UNVERIFIED** and prints person ∩ app; a different owner that narrows
access shows in D3 as an unapproved difference. N7 applies to every row.
Approve each row or pick its alternative.

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
| N4 | Files already made and their signed links | No change from this design; [OV §3.1, §3.4] governs them. A later block or plan change does not hide them (v3 did; v4 does not, §2.13) |
| N5 | Any request that asks for more file metadata than its plans allow | Gets the plan's level (today a request may override) |
| N6 | Settings and Providers pages | "Default plan" gone; "Allowed plans" read-only |
| N7 | Every app and person | Models, workflows and new revisions added after the cut-over are not usable until the owner grants them (the "Who can use it?" step preselects plans with offer new on). Today the built-in default and plans without model lists include them at once |

## 9. Red-team resolution

**Rounds 1 and 2.** Round 3 rated these resolved at design level: RT2 B2
(no rollback after accept), RT2 M3 (one historic row per charge), RT2 M5
(app row first), RT2 m1, RT2 m2, and the round-1 findings they carry
(B2 bounded direct access, B3 accountable person, M3-M5, M7-M9, m1-m3).
It rated four partial; v4 closes each through a round-3 row: RT2 B1
(action binding) and RT2 M2 (revocation) through R3-B1 and R3-M1; RT2 M1
(attempt consumption) through R3-B2; RT2 M4 (crash durability) through
R3-M3 and R3-M4. Where each lives: plans and composition §2.1-§2.6; direct
access §2.7; deny by default §2.8; spending §2.11; migration §4; tests §5.
The per-finding text of earlier rounds is in git history (`e6e4965b`,
`b81a9a5c`).

**Round 3** [R3]:

| ID | Finding | Resolution | Where |
|---|---|---|---|
| R3-B1 | Re-authorization cannot revalidate the original key or session; reads are not one snapshot | **By removal**: no re-authorization, no epoch. Each effect resolves the exact key hash or session (id, expiry, revocation, registration) in one `SELECT`. Sessions never switch person | §2.13, §5.2 |
| R3-B2 | A unique index does not make a send single-use | One row per `authorize`, random `attempt_id`, conditional `reserved → sending` claim; uncertain sends reconcile | §2.11, §5.4 |
| R3-M1 | "Same action" omits capability; activation uncovered | **By removal** of the comparison: the effect is derived from the typed outbound request and checked directly; `load` is its own effect | §2.13, §5.2 |
| R3-M2 | Output re-evaluation lacks provenance | **By removal**: plans govern effects; stored files follow [OV §3.1, §3.4]; disclosed in §2.10 and N4 | §2.13, §8 |
| R3-M3 | Restore destroys its recovery marker | Control record beside the gateway lock; per-file idempotent restore, WAL moved first; start rules | §4.2, §5.6 |
| R3-M4 | `data/` backup misses external stores | Manifest at resolved real paths; resolver disagreement refuses (`proxy.py:12956` against `providers.py:35`); space per filesystem | §4.2, D10, §5.6 |
| R3-m1 | Predecessor writes schema before its version check | Version check before DDL; file unchanged on refusal | §4.2, §5.6 |
| Routes, ownership | One boundary for every route; constructors and triggers change together | One `dispatch` path with inventory and AST checks; [OV §3.5] triggers with `create_app`; order with OV stated | §2.13, §5.3, §2.4, §4.5 |

## 10. What we cut in v4

| Removed from v3 | Why it could go |
|---|---|
| The Permit and `check` | Nothing is carried from decision to effect |
| `access_epoch` and its triggers | Current rows are read at each effect |
| Re-authorization of an attempt; `UNIQUE (request_id, attempt)` | One `authorize` and one row per send, with a single-use claim |
| Cost-bound recomputation against `reserved_usd` | The reservation is made from the body about to be sent |
| Re-checking files at download, and v3's D9 | [OV] governs stored files; the check needed provenance that does not exist |
| Replacing `data/` as a directory | Per-file restore at real paths |

Added, each because a finding needs it: the `sending` status and claim;
the store manifest; control states `restoring` and `restored`; the
predecessor's two startup checks; the typed outbound request. v3's cuts
(staged swap, post-accept rollback, closure digest, phases, orphan
cleanup, copied spend) stay cut.

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

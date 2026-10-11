# Access plans (design v1)

Status: proposed design, not implemented. 2026-10-11. Needs an independent
red-team before any build starts (P20).

This design replaces the built-in default access, the "Legacy …" plans and the
separate category-rule plans with one kind of thing: an **access plan** that
the owner names and assigns. It amends `CATEGORIES_AND_PLANS.md` (v3) in the
places listed in §2.10. Everything else in v3 stays.

Inputs:
- Research: `~/Documents/azizam/ux-findings/research-results/azizam-research-access-plan-composition-20261011-b.md`
  (AWS IAM, Google Workspace, Microsoft 365, Tailscale, Cloudflare Access,
  Discord, Slack, GitHub, Notion, Figma, OpenRouter, LiteLLM, Open WebUI,
  Family Link, Screen Time, Microsoft Family Safety, Plex, Jellyfin; §29
  network-location trust: Tailscale, Home Assistant, Plex, Jellyfin). Cited
  below as [R§n].
- Owner data preview: `~/Documents/azizam/ux-findings/reports/access-plan-migration-preview-2026-10-11.md`
  (read-only copy of `admin.db`). Cited as [P§n].
- Backlog D1: PF-D101 to PF-D107 in `principles-fix-backlog-2026-10-11.md`.
- Divan `local-ai-gateway/`, 2026-10-10: the categories v3 evidence and
  red-teams.
- Code at `origin/release/r17` (`0061d150`). All `file:line` references
  below are at that commit. I read them myself unless marked [P].

## 1. Problem

### 1.1 The owner's questions, answered from the code

| Owner, 2026-10-11 | Answer |
|---|---|
| "I don't want legacy anything so why do we still have them vs mapping old keys to modern stuff? And why do we have so many?" | Five plans named "Legacy …" came from a one-time import of `proxy_api_keys.yml` on 2026-05-22 [P§6]. Nothing mapped them to current plans afterwards. The settings picker hides two of them by name (`web/src/app/settings/page.tsx:99-101`), so the other three still show. The instance has 9 plans for 15 live apps. With one rule change (§2.1, plans do not restrict by API protocol) they reduce to 4 (§4.3). |
| "'Use the default (None — built-in local access)' is None as an access plan supported by divan?" | No. Categories v3 (rule A10) says "A missing identity, an empty plan list or a failed lookup denies. There is no implicitly unrestricted app." The "None" option (`settings/page.tsx:954`) and its copy "Choosing none does not block AI access" (`:986`) contradict the validated design. |
| "personally I don't want the default to be anything local in my own instance" | In this design there is no runtime default anywhere. A person or app with no plan cannot generate. The invite form can preselect a plan you choose, and its initial setting is "Ask each time" (§2.6). |
| "merge category rules into access plans" | Yes. v3 already put category rules inside its plans. The implementation then stored those plans in a second store, apart from the access plans, so the gateway now has two things called "plan". This design keeps one (§2.2). |
| "tag models or workflows … as NSFW … have a default access plan filter out NSFW things? And maybe some other category?" | You define a category (for example "Audience": Everyone, Teens, Adults only), tag models and workflows with it, and add "Block: Audience is Adults only" to a plan. Untagged items are blocked by that plan until you tag them. Walkthrough in §2.7. |
| "Invite someone … Choose an access plan < what are the options and specifically what is the default" | Today the options are all 9 plans, including the 5 Legacy ones (`web/src/app/people/page.tsx:72-99`). Nothing is preselected (`:953`), and the server requires a plan (`gateway/accounts_api.py:62-63`). The Settings "Default plan" does not feed the invite form; it is a runtime fallback for people with no plan. After this design: the options are your current plans, each with a one-line summary. The preselection is whatever you set in Settings, initially none (§3.4). |

### 1.2 How a request gets its plan today

`authenticate_request` (`proxy.py:2461-2496`) checks, in this order:

| Step | Condition | Plan used | Code |
|---|---|---|---|
| 0 | Auth disabled, or a public path | Built-in default (`NO_AUTH_CONTEXT`) | `proxy.py:2468-2469`, `:13406-13409` |
| 1 | An app key | Key plan → app plan → the app owner's person plan (only if self-service apps are on and the owner is human) → **built-in default** | `gateway/admin_runtime.py:1682-1720` (default at `:1703`); `proxy.py:1997-2007` |
| 1a | Person plan | `account_access.profile_id`, then `legacy_identity_profile_grants`, then an active `runtime_profile_mappings` row (all users, email, domain, role, OIDC group), then nothing | `gateway/accounts.py:140-149`; `proxy.py:1919-1975` (`_default_plan_for_user`, `_effective_plan_for_person`) |
| 2 | No key, client IP in `lan_bypass_cidrs` | **Built-in default**, plus full media metadata | `proxy.py:2487-2489`, `:2385-2402` |
| 3 | No key, path starts with a configured path token | **Built-in default** | `proxy.py:2491-2494` |
| — | Browser sessions (Home, Make, catalog) | Person plan, else `"default-local"` → built-in default | `proxy.py:1992-1994`, `:15884`, `:16026`; `gateway/user_runtime.py:110-116`, `:229`; `gateway/home_api.py:49-65` |

Two more fallbacks make an empty plan mean "everything":
- `load_profile(None or {})` returns the built-in default (`gateway/profiles.py:365-371`).
- `evaluate_and_apply` uses the default when the identity is missing (`gateway/runtime_auth.py:283-302`).

On this instance no mapping is active, so every person without an assigned
plan falls to the default. 12 active keys on 5 apps resolve to it [P§4].

### 1.3 What the built-in default grants

`DEFAULT_PROFILE = AppProfile()` (`gateway/policy.py:280`). Every allow-list
is `None`, which means "no opinion" (`policy.py:177-185`). In effect: every
local model, workflow, capability and protocol; it may load models; no remote
providers (`policy.py:517-523`); no direct engine access; basic receipt
detail; no spending cap [P§1]. The settings page calls this "None — built-in
local access".

### 1.4 Keyless requests

The repository config turns LAN bypass on for 127/8, ::1, 192.168/16, 10/8
and 100.64/10 (`proxy_config.yml:1432-1434`), although the comment above it
says "all OFF by default" (`:1420`). Whether the deployed config matches is
**UNVERIFIED** (I did not read the live config). Any device on those networks
that sends no key runs with the built-in default. A list of placeholder keys
(`gateway/lan_auth_policy.py:43-54`) lets LAN clients send a dummy key and
still get the bypass.

Path tokens are configured empty (`proxy_config.yml:1450`), and the config
comment says no public route accepts a token prefix (`:1446-1448`). The code
path still exists (`proxy.py:2374-2384`, `:30212-30214`).

A third path has no plan at all. When a key is not an Azizam key but starts
with `sk-`, many routes forward it to LiteLLM with an empty Azizam identity
and skip Azizam's route policy (`proxy.py:2498-2535`; for example `:25490`).
LiteLLM then decides alone. Whether LiteLLM is enabled on the instance is
**UNVERIFIED**.

Signed creation links check only the signature and expiry
(`proxy.py:23631-23670`), not whether the creator may still use the work.

### 1.5 Two systems called "plan"

1. **Access plans** (`profiles` table, `AppProfile`, evaluated by
   `gateway/policy.py:373`). These are what the People, Apps, Invite and
   Settings pages show.
2. **Category plans** (inside a snapshot in `categories.db`, evaluated by
   `gateway/category_policy.py:375`, attached to people and apps in
   `PolicyVersion.people/apps`, `:459-466`). These have grants, require/deny
   rows and a `legacy` mode (`:256-257`).

The category gate only tightens an allowed decision. It does nothing with no
active snapshot (`gateway/category_enforcement.py:116-118`). It skips any
person or app that the snapshot does not list (`:122-123`). The Categories
page says "Nothing is enforced by this page yet"
(`web/src/app/categories/page.tsx:273`). The v3 header says "not
implemented", but the kernel, store, API (`gateway/category_api.py:389-396`)
and gate exist. Whether an active snapshot exists on the instance is
**UNVERIFIED** (the store is in `data/`).

### 1.6 Two provider allow-lists

A remote provider call passes only if both lists agree:
- the plan's `remote.providers` (`gateway/policy.py:517-523`), and
- the provider's `allowed_plans` in provider settings, when that key exists
  (`gateway/providers.py:1281-1297`; discovery repeats it at
  `proxy.py:13947-13948`).

`allowed_plans` stores the plan's JSON `id` (`owner-local`), not its row id
[P§2]. Renaming or merging that plan would silently remove provider access.
The Providers page shows only the second list (PF-D107).

### 1.7 Other legacy items (P11)

- `legacy_identity_profile_grants` table, 0 rows, still read in 4 places
  (`gateway/accounts.py:148`, `:380`, `:422`, `:427`, `:491`).
- Five imported keys with a `legacy-…` prefix, a `legacy-key-import` service
  account that owns 3 apps, and the `proxy_api_keys.yml` import path
  (`gateway/runtime_auth.py`) [P§6].
- `runtime_profile_mappings` (2 rows, both disabled) and the YAML bootstrap
  maps `role_profile_ids`, `domain_profile_ids`, `email_profile_ids`,
  `default_profile_id` (`proxy.py:1939-1958`).
- `ModelAccessPolicy`, documented as a "Backward-compatible alias for older
  LLM-only profile configs" (`gateway/policy.py:138-140`).
- Built-in pseudo-plans `default` and `default-local` accepted as ids without
  rows (`gateway/admin_store.py:473`, `:2001`; `proxy.py:1684`, `:1766`).
- The "Let people create their own apps" toggle changes which plan an app
  gets: when it is off, apps without a plan use the built-in default
  instead of the person's plan (`settings/page.tsx:1060`; `proxy.py:1998`).

### 1.8 What the owner sees

- Settings offers "None — built-in local access", "Default local (built-in)"
  and "Default (built-in)" (`settings/page.tsx:953-963`).
- Apps says "Each app gets a key with your account's access plan"
  (`web/src/app/apps/page.tsx:337`), which is false for apps with their own
  plan and, when self-service is off, for apps without one (PF-D102).
- Plan summaries say "Capabilities follow gateway defaults"
  (`web/src/app/people/people-api.ts:134`, `:143`) (PF-D104).
- A provider chip says "Local only by default" for any plan without paid
  access (`web/src/app/providers/page.tsx:1856`) (PF-D105).

## 2. The model

### 2.1 Unit of permission: one access plan

An **access plan** is a named, reusable object. Many people and apps can use
the same plan. It has these parts:

| Part | UI label | Meaning |
|---|---|---|
| Grants | **Can use** | Per capability, the exact models and workflows allowed. Nothing is allowed unless it is listed. |
| Providers | **Paid providers** | The providers a granted model may route to. An empty list means engines on this gateway only. |
| Provider fallback | **Use a paid provider when the engine is unavailable** | Off by default. A fallback route must also pass every other rule (§2.4). |
| Category rules | **Blocked categories**, **Must match** | Deny rows and require rows over owner-defined categories (v3 semantics, unchanged). |
| Caps | **Spending caps** | Per person and per app, per day and per month, for paid routes (§2.8). |
| Direct engine access | **Direct engine access** | Lets an app call an engine's own API through the gateway (today's native passthrough). Audited. |
| Other settings | under **Details** | May load models that aren't running; receipt detail (basic, standard, full); metadata in delivered files. |

Capabilities are the glossary list (`VOICE.md`): Chat, Images, Video, Voice,
Transcription, Music, Sound effects, Embeddings, plus Direct engine access.
Decisions (`decision.systemone`) also needs a capability name; the glossary
does not list one yet (**UNVERIFIED** which word the UI uses today). Model
discovery (`models.list`, `models.show` and similar) is not a capability. Any
identity that may generate can list models, and the list is filtered by the
same evaluator. Model management through the Ollama API (`models.pull`,
`push`, `create`, `copy`, `delete`) returns 501 today (`proxy.py:27215-27281`)
and is never grantable.

**Plans stop restricting by API protocol** (OpenAI, Anthropic, Ollama). A
protocol is the shape of a request, not what the request may use: the same
model is reachable through each. No product in the research restricts access
by wire protocol: OpenRouter guardrails restrict models and providers [R§11],
LiteLLM restricts model groups [R§12], Open WebUI restricts features and
models [R§13]. Protocol rules today act as capability rules in two places,
and the migration converts them: `openai.audio` only becomes Voice,
Transcription, Music and Sound effects; `provider-native` becomes Direct
engine access. Risk: if some capability is reachable only through one
protocol, dropping the rule widens a plan. The migration dry-run must list
every intent each protocol reaches (§4.2, check M4). **UNVERIFIED** until
that check runs.

Grants are exact (v3 rule A9). A plan never matches "every model" by
pattern. To keep a household plan easy to maintain, each capability in a plan
has an authoring flag **Offer new models and workflows** (stored as
`offer_new`). When the owner adds a model or workflow, the add flow ends with
a step "Who can use it?". That step preselects every plan whose flag is on
for that capability and shows the gain preview. The kernel still sees only
exact grants. Nothing widens without that previewed owner step (v3
principle 5). This is the "currently reviewed items versus future automatic
membership" distinction in [R§21], and Plex's warning that "All Libraries"
silently includes new libraries [R§17].

### 2.2 Category rules belong inside the plan

Decision: category **rules** (Blocked categories, Must match) are part of
each access plan. Category **definitions** and the **tags** on models,
workflows and provider routes are a shared catalog, edited once and used by
every plan.

Evidence:
- v3 already designed it this way: its plan has `grants`, `require` and
  `deny` together (`CATEGORIES_AND_PLANS.md`, Plans). The split into two
  stores happened in the implementation, not the design.
- Jellyfin keeps blocked tags, required tags and maximum rating in the same
  per-user policy that grants libraries, and checks blocked tags first
  [R§18]. OpenRouter keeps model allow-lists and privacy rules in one
  guardrail object [R§11]. Plex keeps rating and label restrictions on the
  share itself [R§17].
- Tailscale Aperture shows what happens when labels sit beside grants
  without rules: "Additive and allow-only", so a broad grant still allows a
  labelled resource [R§4]. A label must be referenced by a rule to restrict.
- A second engine beside the plans needs its own "who does it govern" rule.
  Today that rule is "unlisted identities skip it"
  (`category_enforcement.py:122-123`), which is the gap D1 must close.
- P16: one name per thing. Two objects both called "access plan" fail the
  check by construction.

v3 constraints kept: deny-wins; unknown deny input blocks
(`DENY_INPUT_UNKNOWN`); `in` passes only when every accepted value is in the
row; bundles (every member of the selected closure must pass); waivers only
on one require row's missing value; outside metadata is evidence, never a
grant; one evaluator for runtime, preview and "why denied".

Not adopted: an instance-wide "always block" bound beside plans [R§21
mentions it as optional]. An owner who wants it adds the same Blocked row to
each plan; the change preview shows every plan it touches. Revisit if an
owner runs many plans.

### 2.3 Assignment

- Every **person** has exactly one access plan, or none.
- Every **app** has exactly one access plan, or **Same as its person**, or
  none. "Same as its person" is a stored choice on the app, not a fallback.
  It follows the person's plan when that plan changes.
- **Keys have no plan.** A key acts for its app. Today's key-level plans
  (`credentials.profile_id`) go away.
- Every app belongs to one person, who is accountable for it. Service
  accounts no longer own runtime apps. The migration moves the
  `legacy-key-import` apps to the owner (§4.3). Research says not to invent a
  person-less exemption [R§20, item 6].
- A request runs only if **both** the person's plan and the app's plan allow
  it (intersection, v3 A10). With "Same as its person" the two are the same
  plan. Attaching a plan can only narrow access, never widen it [R§20,
  Rank 1; R§26].
- A role (Owner, Admin, Member) controls management screens. It never grants
  generation. The owner needs a plan like anyone else [R§20, item 5]. The
  admin API does not depend on access plans, so an owner can always fix a
  plan that locks them out.
- Browser surfaces (Home, Make, Try, Library) run as the signed-in person and
  a built-in app named **Azizam** with "Same as its person". It is shown in
  Apps so the owner can narrow web use. **UNVERIFIED**: which app id each
  browser surface uses today; Home uses the person's app and credential
  (`gateway/home_api.py:49-65`), and Try mints keys on a "Try" app
  (`web/src/app/try/page.tsx:1242`).

Why one plan per side: it makes "which plan applies?" one answer, matches the
research's first-ranked option, and keeps the v3 intersection [R§20]. The
kernel keeps its list of plans per identity (`PolicyVersion.people`), and the
store allows exactly one entry. Several plans per side, or additive grant
packages, are deferred until an owner needs them [R§20, Rank 2].

### 2.4 Evaluation order

One evaluator answers runtime requests, discovery, "Check access" and the
change preview. Every step must pass. The order fixes which reason is shown
first, never the outcome.

| # | Check | Fails with | Layer (VOICE rule 4) |
|---|---|---|---|
| 1 | The key, session or token resolves an enabled app and an enabled person | 401 `key_required` or `key_invalid`; 403 `app_disabled`, `person_disabled` | your key / your app |
| 2 | The access settings load (one valid active snapshot) | 503 `access_settings_unavailable` (v3 `POLICY_UNAVAILABLE`) | gateway |
| 3 | The person has a plan; the app has a plan or "Same as its person" | 403 `no_access_plan` (says which side) | access plan |
| 4 | For each of the two plans: no Blocked row matches any member of the selected closure, and none is unknown | 403 `blocked_by_category`, `not_reviewed` | access plan |
| 5 | For each plan: the capability and item are granted; each provider route in the closure is in Paid providers; fallback routes need fallback on | 403 `not_in_access_plan` (says which side) | access plan |
| 6 | For each plan: every Must match row passes | 403 `not_reviewed` (missing value) or `blocked_by_category` (known mismatch) | access plan |
| 7 | For paid routes: every cap that applies has room (§2.8) | 429 `cap_reached` | spending |
| 8 | Capacity admission (`HEADLESS_AND_RESOURCES.md`, Release A) | as designed there | gateway / engine |

Explicit deny: the only deny in a plan is a category rule. To keep one item
out, leave it out of Can use. A deny always beats a grant, and step 4 runs
before step 5 so the reason names the block, not the grant.

Re-checks: v3 enforcement (R4, A17) stays. The evaluator runs after alias and
fallback resolution, on every route or revision change and retry, at job
dispatch, and at output download and share.

### 2.5 Deny by default on every path

| Path | After this design | Code to remove or change |
|---|---|---|
| App key | Steps 1-8 | `admin_runtime.py:1703` default; key-level plan |
| Browser session | Steps 1-8 as person + Azizam app | `user_runtime.py:110-116`, `:229`; `proxy.py:15884`, `:16026`; `home_api.py:53` |
| LAN request without a key | **Removed.** 401 `key_required`: "This gateway needs an app key. Create one in Apps." | `proxy.py:2187-2230` (LAN part), `:2385-2402`, `:2487-2489`; `lan_auth_policy.py`; config keys `lan_bypass_enabled`, `lan_bypass_cidrs` |
| Path token | **Removed.** Not routed today and none are configured | `proxy.py:2234`, `:2374-2384`, `:2491-2494`, `:30212-30214`; config key `path_tokens` |
| Auth disabled | **Removed.** Authentication is always on | `AUTH_ENABLED` branch at `proxy.py:2468`; config key `auth.enabled` |
| Public paths (`/health`, showcase pages and examples) | Kept. They never call the evaluator and never dispatch, list models or return creations. `public_paths` is config data (`proxy.py:1574`) and accepts `*` prefixes, so startup rejects any entry that matches a protected route in the route inventory | `is_public_path` (`proxy.py:2431-2444`) |
| Public Ollama discovery (`ollama_compat_discovery`) | **Removed.** With no identity there is no plan to filter the list. Off today (`proxy_config.yml:1418`) | `proxy.py:2172-2175`, `:2436-2437` |
| `?api_key=` | Kept (it is a key). Off by default | — |
| Unknown `sk-` key forwarded to LiteLLM ("delegation") | **Removed.** Today any `sk-` key that is not an Azizam key runs with no Azizam identity, and Azizam's route policy is skipped. Only LiteLLM checks it. Models served through LiteLLM become catalog items that plans grant like any other | `_is_delegatable_litellm_key` and `authenticate_runtime_or_litellm_request` (`proxy.py:2498-2535`); the `delegated_to_litellm` skips, e.g. `proxy.py:25490`; `allow_litellm_delegation=True` call sites (`:25485`, `:26439`, `:26897`, `:26957`, `:27019`, `:27058`, `:27097`, `:27292`) |
| Signed creation links (`/v1/assets/{id}/content`, `/v1/images/{id}/content`) | Kept as links, but each creation records its person and app. Download checks that their current plans still allow the recorded bundle (v3 R4). A link cannot outlive a disabled person or app, or a plan that no longer allows the work. A link never grants generation | `_signed_asset_content_response` checks signature and expiry only (`proxy.py:23631-23670`); the "URL without md predates the control" branch (`:23655-23656`) also goes (P11) |
| Missing identity anywhere | Deny; never `DEFAULT_PROFILE` | `runtime_auth.py:292`; `proxy.py:13406-13409`, `:29875`; `profiles.py:365-371`; `DEFAULT_PROFILE` itself |
| First-run setup network (`setup_allowed_cidrs`) | Kept as its own setting. It falls back to the LAN list today (`proxy.py:2370-2371`); it must not after the LAN list is removed | — |

Why remove LAN bypass instead of mapping it to a plan: a source address is
not an identity. Plex documents that apps allowed in without auth on its LAN
list "will be treated as the admin/owner" [R§29, NT4]. Home Assistant's
trusted networks still pick a real user and default `allow_bypass_login` to
false [NT2]. Tailscale separates network admission from application grants
[NT1]. Mapping all LAN traffic to a shared plan would create a new implicit
audience and lose who made each request [R§29, item 3]. The alternative, a
named app for keyless home devices with its own plan, is owner decision 1.

### 2.6 What "default" means

The research separates four meanings that must not share one control [R§22].

| Meaning | Behavior | Owner sees |
|---|---|---|
| No plan | Cannot generate. Can sign in, see their account and ask for access. | "No access plan" |
| New invitation | The invite form preselects a plan you chose in Settings. The invitation stores the plan id when you send it. Changing the setting later does not change sent invitations. Initial setting: **Ask each time** (nothing preselected). | "Access plan for new invitations" |
| New app | The creator chooses. Members get "Same as my access plan" with no picker. Owners and admins see a picker with "Same as {person}'s access plan" first. The server rejects an app with no choice. | "Access plan" field in New app |
| Starting templates | Bundled, read-only, inert until copied (v3 Bundled defaults). Copying creates a normal plan. A template update never changes a copied plan. | "Use as a starting point" |

The existing "Default plan" setting (an `all_users` mapping, `admin_store.py:1994-2024`)
is deleted, with every other runtime mapping. OIDC sign-in without an
invitation creates a person with no plan; People shows "Needs an access
plan". Group-to-plan rules for large schools are deferred. If added, they
must write an assignment at first sign-in, shown as "Assigned from group
{name}", never act as a live fallback [R§22; Slack and Figma default-team
precedents, R§7, R§10].

Invitation accept stops copying the plan id onto the new app and key
(`gateway/accounts.py:337-345`). It writes the person's plan and creates "My
first app" with "Same as its person".

### 2.7 Tagging and a plan that blocks a category

Example: the owner wants a "Family" plan with no NSFW content.

1. **Define the category** (once). Categories → New category. Name
   "Audience". Values: "Everyone", "Teens", "Adults only". Applies to models,
   workflows and provider routes. Azizam ships "Audience" with these values
   as an editable starting category (v3 Bundled defaults); the gateway code
   never knows what any value means (P12, `ENGINE_KNOWLEDGE_BOUNDARY.md`).
   v3 supports set categories only (A14), so "NSFW yes/no" is a set with two
   values.
2. **Tag**. On a model or workflow page, the Categories section has one
   picker per category. In Models and Workflows lists, select several rows,
   then "Set category". A tag is an owner assertion with who and when (v3
   Evidence). Outside evidence shows as a suggestion with its source and
   date, for example "Hugging Face lists this as not-for-all-audiences. Set
   Audience to Adults only?" Only the owner's choice makes it a tag.
3. **Add the rule**. Access plans → Family → Blocked categories → Add →
   "Audience is Adults only".
4. **Review and apply**. Tag edits and plan edits collect as pending changes
   ("4 access changes · Review"). The review shows, for known people, apps
   and items: who gains, who loses, and **Not tagged yet** (items the plan
   now blocks because their Audience is unknown). "Apply changes" activates
   them together (v3 compare-and-swap).

Results under v3 semantics:
- An item tagged Adults only is blocked for Family.
- An untagged item is blocked for Family (unknown deny input). Family members
  see "This hasn't been reviewed for your access plan yet."
- An item tagged both Everyone and Adults only is blocked (any match).
- A workflow is blocked if any model or route in its closure is Adults only
  or untagged.
- Discovery hides blocked items from Family members (same evaluator,
  `category_enforcement.visible`).
- Plans without the rule ignore the Audience tag.

Other categories work the same way. Examples from v3 Bundled defaults:
"Licence use" (block non-commercial for a team plan), "Data handling" (must
match "No training on our data"), "Runs where" (block cloud for a kids plan).
"Runs where" overlaps with Paid providers. Keep it only if the owner wants a
category for route location beyond the provider list (not an owner decision;
leave it out of the starting set).

### 2.8 Spending caps

- **Plan caps**: amount per day and per month, per person and per app. Each
  person and each app has its own counter; sharing a plan never shares money
  [R§11 OpenRouter, R§23].
- **Provider caps** (existing provider settings, for example the global
  USD 2 per day): a shared pool for the whole gateway, checked as well.
- Every cap that applies must have room. The request reserves an upper bound
  before the paid call and records the measured cost after.
- If spend accounting is unavailable, paid routes deny (`cap_reached` is not
  used for that case; it is `access_settings_unavailable` for the spend
  layer). Local routes are not affected.
- Changing a plan never resets a counter. Counters belong to the person or
  app and the period, not to the plan.
- **UNVERIFIED**: how the existing `remote.budget_usd_per_day` and
  `budget_usd_per_month` fields (`gateway/policy.py:72-77`) are counted today
  (per key, per plan or per app). No stored plan sets them [P§2], so the
  migration moves no values. The implementation must confirm the counter
  subject before it reuses the fields.

### 2.9 Storage

One versioned document, the **access settings**, holds categories, tags,
plans and assignments (person → plan, app → plan or "same as person"). It
uses the existing snapshot store (`gateway/category_store.py`): immutable
revisions, compare-and-swap activation, history, restore as a new revision.
People, apps and keys stay in `admin.db` and are referenced by id.

Removed from `admin.db`: the `profiles` table, `account_access.profile_id`,
`apps.default_profile_id`, `credentials.profile_id`,
`account_links.profile_id` (replaced by a plan id inside the invitation
record that the evaluator checks at accept), `runtime_profile_mappings`,
`legacy_identity_profile_grants`, and the `user_access_defaults` setting.
Removed from provider settings: `allowed_plans`.

Plans are referenced only by an opaque, immutable plan id. The display name
can change freely. There is no second id inside the plan document, so the
`owner-local` coupling (§1.6) cannot recur.

Creating a person or app adds an assignment through the same store. An
invitation's plan was chosen and previewed by the owner when they sent it,
so accept commits it without another review. A concurrent change makes the
compare-and-swap retry.

### 2.10 Changes to categories v3

| v3 text | Change |
|---|---|
| Migration (R3): "Existing plans run in an explicitly named legacy mode that keeps any-tag OR semantics" | Removed (P11). The kernel drops `mode` and `legacy_tags` (`category_policy.py:256-257`, `:306`, `:327`). No stored plan sets tags today [P§2], so nothing needs the mode. This also removes the empty-tag oracle disagreement the research found [R§19]. |
| Plans live in the policy snapshot, apart from access plans | The snapshot's plans are the access plans. `AppProfile` fields that remain (providers, fallback, direct engine access, caps, settings) move into the plan document. |
| Enforcement adapter: ungoverned identities and no active snapshot pass | Removed. No active snapshot denies (v3 Preview: "With no valid committed snapshot, protected access denies"). Every identity is governed. |
| Several plans per identity | The store allows one per side. The kernel keeps its list type. |
| Status header "not implemented" | Update when this design lands: the kernel, store, API and gate exist. |

Everything else in v3 stands: evidence and assertions, bundles, waivers,
re-authorization points, preview-as, change preview, compare-and-swap,
restore, layered read-only defaults, no timers.

## 3. Interfaces

API first (P21). The UI, CLI and MCP call the same server operations. None of
them decides policy.

### 3.1 Owner API

Pattern from the Tailscale policy file: read with a revision, preview, write
with `If-Match` [R§4, T7].

| Operation | Endpoint | Notes |
|---|---|---|
| Read access settings | `GET /admin/api/access` | Returns categories, tags, plans, assignments, `revision`; `ETag: "<revision>"` |
| Preview a change | `POST /admin/api/access/preview` | Body: `{"base_revision": n, "changes": [...]}`. Pure. Returns gains, losses, reason changes, `not_tagged`, and the finite scope it covered |
| Apply a change | `POST /admin/api/access` with `If-Match` | Same body. 409 `stale_base` if the revision moved. Returns the new revision |
| History | `GET /admin/api/access/history` | Revisions with actor and time |
| Restore | `POST /admin/api/access/revisions/{n}/restore` | Returns a preview of a new candidate; apply with the call above |
| Check access | `POST /admin/api/access/check` | Body: `{"person", "app", "capability", "model" or "workflow"}`. Returns the decision, the side and rule that failed, and the closure member |

Change operations: `create_plan`, `update_plan`, `rename_plan`,
`delete_plan` (rejected while assigned or in an open invitation; the error
lists who uses it), `assign_person`, `assign_app` (plan id or `"person"`),
`create_category`, `update_category`, `tag` (subject, category, values),
`untag`, `accept_evidence`.

Plan document (API shape):

```json
{
  "id": "plan_7Hq2",
  "name": "Family",
  "can_use": {
    "chat":   {"models": ["family-chat", "homework-helper"], "offer_new": true},
    "images": {"workflows": ["storybook-pictures"], "offer_new": false},
    "voice":  {"models": ["reader-voice"], "offer_new": true}
  },
  "paid_providers": [],
  "provider_fallback": false,
  "blocked": [{"id": "family-no-adult", "category": "audience", "values": ["adults-only"]}],
  "must_match": [],
  "caps": {"per_person": {"usd_per_day": null, "usd_per_month": null},
           "per_app":    {"usd_per_day": null, "usd_per_month": null}},
  "direct_engine_access": false,
  "can_load_models": true,
  "receipt_detail": "basic",
  "media_metadata": "none"
}
```

A missing field is a validation error, not "no opinion". A `null` cap means
no cap for that period. Model and workflow names are the public model names
(`VOICE.md`), resolved to pinned revisions inside the snapshot.

Removed endpoints, with no redirect (P11): `/admin/api/profiles*`,
`/admin/api/runtime-profile-mappings*`, the user-access-defaults setting,
the provider `allowed_plans` endpoint (`PUT /{provider_id}/allowed-plans`, `gateway/provider_admin.py:563-575`),
and `/admin/api/categories/*` (folded into `/admin/api/access`).

People, apps and invitations keep their endpoints. Their bodies change:
- `POST /admin/api/invites`: `access_plan` (required plan id) replaces
  `profile_id`.
- `POST /admin/api/apps` and the member app endpoint: `access_plan` (plan id
  or `"person"`) is required; members may send only `"person"`.
- `PATCH /admin/api/people/{id}`: `access_plan` (plan id or `null`).

### 3.2 Member API

`GET /me/access`: the plan name, a summary computed from the plan
("Chat, Images and Voice. No paid models."), caps with remaining amount and
reset time, and, per app, whether the app narrows it. Discovery
(`/v1/models`, MCP `capabilities`) already lists only what the evaluator
allows.

### 3.3 Errors on the data plane

OpenAI error shape, `type: "access_error"`, with these `code` values and
messages. Members see the plain sentence; the `details` object carries the
plan, rule and closure member for builders and owners.

| Code | Message |
|---|---|
| `key_required` | This gateway needs an app key. Create one in Apps. |
| `key_invalid` | This key isn't valid. It may have been revoked. Create a new key in Apps. |
| `no_access_plan` (person) | You don't have an access plan yet. Ask {owner name} to choose one. |
| `no_access_plan` (app) | This app has no access plan. Choose one in Apps. |
| `not_in_access_plan` (person) | {model name} isn't in your access plan. |
| `not_in_access_plan` (app) | Your access plan allows {model name}, but this app's access plan doesn't. |
| `blocked_by_category` | {model name} is blocked for your access plan. |
| `not_reviewed` | {model name} hasn't been reviewed for your access plan yet. |
| `cap_reached` | Your monthly cap for paid models is reached (USD 5.00). It resets on 1 November. |
| `access_settings_unavailable` | Access settings couldn't be loaded, so nothing can run. Ask {owner name} to check the gateway. |

### 3.4 UI copy

Follows `VOICE.md`. Owner surfaces may name rules; member surfaces never show
rule ids, category ids or "policy".

**Invite someone** (People):
- Field label "Access plan". Placeholder "Choose an access plan". No "None"
  option.
- Each option: plan name, then a summary from the plan data, for example
  "Family — Chat, Images and Voice. No paid models. Blocks Adults only."
- Below the picker, the chosen plan in full: "They can use: …", "Paid
  models: up to USD 5.00 per month" or "No paid models" (PF-D103).
- Preselected plan: the Settings choice, else nothing.
- No plans yet: "Create an access plan before inviting someone." with
  "Create access plan" (`people/page.tsx:252` already has the title).
- Plans failed to load: "Access plans couldn't be loaded. Try again."
- Send is disabled until a plan is chosen.

**New app** (Apps):
- Member: no picker. "Access plan: Family (yours)". Replaces "Each app gets a
  key with your account's access plan" (`apps/page.tsx:337`).
- Owner or admin: "Access plan" picker. First option "Same as {person}'s
  access plan ({plan name})", then each plan with its summary. Helper: "This
  app can use only what both its access plan and {person}'s access plan
  allow."
- App row: "Access plan: {name}" or "Access plan: same as {person} ({name})".
- App with no plan (only possible if a plan was deleted mid-flight or after
  migration hold): "No access plan. This app can't make requests." Action
  "Choose a plan".

**Settings → People and access**:
- "Access plan for new invitations". Options: "Ask each time" (initial),
  then each plan. Helper: "Preselected when you invite someone. People
  already invited keep the plan in their invitation."
- Removed: "Default plan", "None — built-in local access", "Default local
  (built-in)", "Default (built-in)" and the consequence sentence at
  `settings/page.tsx:986`.
- "Let people create their own apps" help text loses its sentence about
  built-in local access (`:1060`). The toggle no longer affects plans.

**People**:
- Person with no plan: badge "No access plan", action "Choose a plan".
- A member with no plan sees on Home: "You can't use models yet. Ask {owner
  name} to choose an access plan for you."

**Access plans** (People → Access plans):
- List row: name, computed summary, "Used by 3 people and 5 apps".
- Empty: "No access plans yet. Start from a template or create one."
- Plan editor sections in this order: Can use, Paid providers, Blocked
  categories, Spending caps, Details (Must match, Direct engine access,
  model loading, receipt detail, file metadata).
- Summaries come from plan data only. "Capabilities follow gateway
  defaults" and "Model locations follow gateway defaults"
  (`people-api.ts:134`, `:143`) are removed: nothing follows a default.
- Pending changes bar: "{n} access changes · Review". Review page: "{a}
  people and {b} apps gain access · {c} lose access · {d} items not tagged
  yet", then "Apply changes".
- Delete a plan in use: "{name} is used by 3 people and 2 apps. Move them to
  another plan first." with the list.
- Settings unavailable: "Access settings couldn't be loaded. Requests are
  stopped until this is fixed." with "Try again" and "View history".

**Providers**: the "Allowed plans" control is replaced by a read-only line
"Access plans that can use {provider}: Everything", linking to each plan.
With none: "No access plan includes {provider}." The chip "Local only by
default" (`providers/page.tsx:1856`) is removed.

**Check access** (owner): pick a person and an app, see every model and
workflow each can use, and for each denied one the first failing check from
§2.4. Replaces "Preview as" on the Categories page.

### 3.5 CLI and MCP

The CLI from `HEADLESS_AND_RESOURCES.md` Release B gains, using the same
endpoints and an owner credential:

```
azizam access show [--json]
azizam access check --person tim --app open-webui --capability chat --model family-chat
azizam access preview --changes changes.json
azizam access apply --changes changes.json --base-revision 42
azizam access history
azizam access restore 41
azizam plans list
azizam people set-plan <person> <plan>
azizam apps set-plan <app> <plan|person>
azizam categories tag <model-or-workflow> audience=adults-only
```

MCP: the data-plane tools (`capabilities`, `run`, `job`) return the error
codes of §3.3. Owner MCP tools stay deferred, as
`HEADLESS_AND_RESOURCES.md` already says.

## 4. Owner data migration

### 4.1 Shape

A one-time, reviewed operation. There is no compatibility layer and no
period where old and new rules both run [R§24 item 9; Tailscale's indefinite
coexistence rejected, R§4].

1. **Dry run** on copies: `scripts/access-migrate dry-run --admin-db <copy>
   --categories-db <copy> --receipts-db <copy> --config <proxy_config.yml>
   --out <dir>`. It opens every input read-only and writes only to `--out`:
   `report.md` (for the owner), `report.json`, and `approval.yaml` (every
   proposed row, each with `decision: accept` and its listed alternatives).
2. **Owner review**: the owner edits `approval.yaml`. A row that is not
   accepted or changed to a listed alternative blocks apply.
3. **Apply**, during a planned stop of the gateway:
   `scripts/access-migrate apply --approval approval.yaml --admin-db ...
   --categories-db ... --config ...`. It refuses if a fingerprint of the
   input rows differs from the dry run, writes a `.backup` of each database
   first, runs one transaction per database, activates the first access
   settings revision, and edits the config to remove deleted keys.
4. **Verify**: the script runs Check access for every person × app ×
   capability × item and compares with the approved preview. Any difference
   restores the backups and exits non-zero.
5. The new gateway code refuses to start until the migration has been
   applied (schema version check with an instruction). This is a guard, not
   a compatibility path.

Rollback is restoring the backups together with the previous release. There
is no partial undo.

### 4.2 Checks the dry run must report

| # | Check | Why |
|---|---|---|
| M1 | Every person, app and active key, with effective rules today and after | The approval is per row |
| M2 | Receipts in the last 90 days by app and by the synthetic LAN identity (`api_key_name = "lan-bypass"`), grouped by path and capability | Which apps and how much keyless traffic will stop. Receipts have no client IP column (`gateway/receipt_store.py:67-92`), so **the dry run cannot name LAN devices**; the owner must list them. Whether LAN receipts are stored at all after the v2 receipts change is **UNVERIFIED** |
| M3 | The active category snapshot, if any: every identity it governs and every category plan | If it governs anyone, apply refuses until each category plan's rows are mapped to one access plan in `approval.yaml` |
| M4 | For each protocol that a plan restricts today, every intent reachable through it | Confirms that dropping protocol rules (§2.1) widens nothing beyond what the report lists |
| M5 | Credentials whose `profile_id` points at a plan being deleted, including revoked keys | `delete_profile` does not check them [P§2] |
| M6 | Apps owned by service accounts | They move to the owner |
| M7 | Disabled apps and revoked keys | Candidates for owner decision 4 |
| M8 | The deployed config's `auth` block | Shows whether LAN bypass is really on |
| M9 | Whether LiteLLM is enabled, and receipts or LiteLLM logs of delegated `sk-` requests | Which clients use LiteLLM virtual keys directly and will need Azizam keys. Whether delegated requests write receipts is **UNVERIFIED** |
| M10 | Creations without a recorded person and app | Their signed links stop working after the cut-over unless the owner assigns them (owner keeps the files; only the links change) |

### 4.3 Proposed mapping for this instance

Source: [P§2-§7], `admin.db` copy of 2026-10-11. Final rows come from the
dry run. Plan rows:

| Today | After | Rules after |
|---|---|---|
| Owner (local) | **Everything** (same plan id, renamed) | All capabilities, all local models and workflows (offer new: on), Paid providers OpenRouter and TypeSafe, Direct engine access **on** (new, owner decision 2), receipt detail full |
| LibreChat vivid.fish full runtime | **Local with direct engine access** (renamed) | All capabilities, local only, Direct engine access on, receipt detail full |
| Legacy daisy | merged into Local with direct engine access | — |
| *(built-in default)* | **Local only** (new plan, explicit copy of the default's rules) | All capabilities, local only, no Direct engine access, receipt detail basic |
| Legacy open-webui | merged into Local only | — |
| Assistant (chat + RAG) | **Chat** (renamed; "RAG" is jargon, P15) | Chat and Embeddings, local only, receipt detail basic |
| Legacy simon, Legacy librechat, Legacy cap | deleted (only never-used keys refer to them) | — |
| Media creator | deleted (nothing refers to it) | — |

9 plans become 4: Everything, Local with direct engine access, Local only,
Chat. Provider access lives only in Everything's Paid providers; the
providers' `allowed_plans: ["owner-local"]` is deleted.

Assignments and data changes:

| Person or app | Today | After |
|---|---|---|
| Tim Nunamaker (person) | no plan → built-in default | Everything |
| Simon Agent (person) | no plan → built-in default | Local only |
| Tim ODL (person) | Assistant | Chat |
| t3-pi-agent | Owner (local) | Same as its person (Everything) |
| Try (Tim) | default | Same as its person (Everything) |
| Try (Simon) | default | Same as its person (Local only) |
| OpenWhispr | default | Local only |
| open-webui | Legacy open-webui; owner `legacy-key-import` | Local only; owner Tim Nunamaker |
| daisy | Legacy daisy; owner `legacy-key-import` | Local with direct engine access; owner Tim Nunamaker |
| librechat.vivid.fish | LibreChat full runtime (key-level) | Local with direct engine access (app-level) |
| paperless, My first app (Tim ODL) | Assistant (app and key) | Same as its person (Chat) |
| Anna's Museum App | Owner (local), key never used | Local only (owner decision 3) |
| Tim personal access, Simon personal access | no app plan | Same as its person |
| cap | Legacy cap; key never used | disabled, key revoked; owner Tim Nunamaker |
| waspflow-dsh-probe | default; last used 2026-08-14 | disabled, key revoked |
| Paid provider proof | Owner (local); key made and used 2026-10-11 15:41 | disabled, key revoked (owner confirms the key was a test) |
| Azizam (browser surfaces) | — | new app, Same as its person |
| 7 disabled test apps (Temporary empathy Jev triage ×3, copy-triage ×4) | disabled, keys revoked | owner decision 4 |

Keys revoked: "Imported legacy simon key", "Imported legacy librechat key",
"Imported legacy cap key", "Testing" (Tim personal access), all 8 "Try
session" keys, "dsh-probe", the Paid provider proof "Default key". Revoked
keys that still name a Legacy plan (2× "Paid proof … denied plan", 2×
"TEMP-diagnosis-verify-DELETE-ME") lose that reference with the column.

The daisy and open-webui imported keys stay active; only their names lose
"Imported legacy" (they become "daisy key" and "open-webui key"). Their
`legacy-` prefix is part of the stored key prefix; it cannot change without
rotating the key. **Rotation is recommended** so no `legacy-` string remains
(P11), but it needs the owner to update daisy and Open WebUI, so apply does
not do it.

Removed data and code (P11): `legacy_identity_profile_grants` and its 4 read
sites; the `legacy-key-import` service account; `runtime_profile_mappings`;
the YAML bootstrap maps; the `proxy_api_keys.yml` import; `ModelAccessPolicy`;
the `default`/`default-local` ids; `DEFAULT_PROFILE`; `LEGACY_ACCESS_PLAN_NAMES`
(`settings/page.tsx:99-101`); the category kernel's legacy mode; LAN bypass,
LAN placeholder keys, path tokens, the auth-off switch and public Ollama
discovery; LiteLLM key delegation. Audit events and receipts are kept unchanged (P19): history
records what happened under the old names.

### 4.4 Behavior changes for owner approval

Relative to today's rules, not traffic. Traffic per key is **UNVERIFIED**
until check M2 runs.

| # | Who | Change |
|---|---|---|
| 1 | Any device on the LAN that sends no key (or a placeholder key) | **Loses all access** (401 `key_required`). Which devices: unknown until the owner lists them (M2). Each needs an app and key, or owner decision 1 |
| 2 | Tim Nunamaker in the browser (Home, Make, Library) | Gains OpenRouter and TypeSafe within caps, full receipt detail, Direct engine access (unused by those pages) |
| 3 | Try (Tim) | Gains OpenRouter and TypeSafe within caps and full receipt detail. Its 7 session keys are revoked; Try mints a new key when next opened (**UNVERIFIED** that an open Try tab recovers without a reload) |
| 4 | t3-pi-agent | Gains Direct engine access (audited). Nothing else changes |
| 5 | daisy | Gains full receipt detail (was standard) and Decisions if a local Decisions engine exists (**UNVERIFIED**; the TypeSafe route stays denied because the plan has no paid providers) |
| 6 | open-webui | Gains the Anthropic API shape for the same local models. No new models |
| 7 | librechat.vivid.fish | None. Its never-used legacy key is revoked |
| 8 | Anna's Museum App | Loses OpenRouter and TypeSafe and full receipt detail. Its key was never used |
| 9 | cap | Disabled. Its key was never used |
| 10 | waspflow-dsh-probe | Disabled. Last used 2026-08-14 |
| 11 | Paid provider proof | Disabled. Its key was created and used on 2026-10-11 15:41 |
| 12 | Simon personal access, Tim personal access | None in practice. Their only keys were never used and are revoked |
| 13 | OpenWhispr, Try (Simon), Simon Agent, Tim ODL, paperless, My first app | None |
| 14 | Providers page | "Allowed plans" becomes a read-only list. The set of plans with provider access is unchanged |
| 15 | Settings | "Default plan" is gone; "Access plan for new invitations" starts at "Ask each time" |
| 16 | Any person created later without an invitation (OIDC) | Cannot generate until the owner chooses a plan (today: built-in default) |
| 17 | Any client that sends a LiteLLM virtual key (`sk-…`) to Azizam | **Loses access** (401 `key_invalid`). It needs an Azizam app key. Which clients: check M9 |
| 18 | Signed creation links already shared | Stop working if the creating person or app is disabled, or its plan no longer allows the work. Links to creations with no recorded person and app follow check M10 |

## 5. Test plan

### 5.1 Evaluator (unit, pure)

- Truth table over: no plan on person; no plan on app; "same as person";
  person allows and app does not, and the reverse; both allow.
- Deny wins over grant; unknown deny input blocks; mixed values {Everyone,
  Adults only} match a block; `in` with no accepted value fails; a waiver
  never overrides a block or a known mismatch (v3 scenarios in
  `categories_model/` carry over).
- Closure: a workflow with an Adults-only helper is blocked; a fallback route
  to a provider not in Paid providers is blocked; fallback with fallback off
  is blocked.
- A missing plan field fails validation; an empty `can_use` grants nothing.
- No active snapshot and a corrupt snapshot both deny with
  `access_settings_unavailable`.

### 5.2 Server denial tests (every path)

Extend the existing route inventory test so every protected route is listed
with its enforcing boundary. For each route, with a mock engine that fails
the test if called:

| Case | Expected |
|---|---|
| No key from 127.0.0.1, ::1, 192.168.1.50, 10.0.0.5, 100.64.1.1, an IPv6 ULA and a public address | 401 `key_required`; engine not called |
| Placeholder key from a LAN address | 401 `key_invalid` |
| Former path-token prefix `/tok_x/v1/chat/completions` | 401; no token stripping |
| Unknown `sk-` key on every former delegation route, with LiteLLM enabled | 401 `key_invalid`; LiteLLM not called |
| Signed creation link after the creator's app is disabled, or its plan stops allowing the work | 403; file not returned |
| Forged `X-Forwarded-For: 192.168.1.5` from an untrusted peer | 401 |
| Config still containing `lan_bypass_enabled`, `path_tokens` or `auth.enabled: false` | Startup fails with a message naming the removed key |
| Revoked key; key of a disabled app; key of an app whose person is disabled | 401 / 403 |
| App with no plan; person with no plan | 403 `no_access_plan`, side named |
| Person plan allows, app plan does not | 403 `not_in_access_plan` (app) |
| Browser session of a person with no plan on Home, Make, Try, Library, catalog | 403 / empty list; engine not called |
| `/health` and showcase pages | 200; never dispatch, list models or return creations |
| Config `public_paths` containing `/v1/*` or any protected route | Startup fails naming the entry |
| `/v1/models`, Ollama `/api/tags`, MCP `capabilities` | Only allowed items, equal to Check access |
| Output download and share after the plan is changed | Current plan checked (v3 R4) |
| Static guard | `DEFAULT_PROFILE`, `default-local`, `lan_bypass`, `path_tokens`, `allowed_plans`, `legacy_identity`, `LEGACY_ACCESS_PLAN`, `delegated_litellm` do not appear in `gateway/`, `proxy.py` or `web/src` (like `tests/test_engine_knowledge_boundary.py`) |

### 5.3 Category exclusion

With "Family" blocking "Audience is Adults only" and models A (Everyone), B
(Adults only), C (untagged), workflow W (Everyone, helper B):
- A runs; B returns `blocked_by_category`; C returns `not_reviewed`; W
  returns `blocked_by_category` naming B in `details`.
- `/v1/models` for a Family app lists A only.
- Tagging C as Everyone has no effect until "Apply changes"; after apply, C
  runs on the next request (cache invalidation, v3 A22).
- Restore the previous revision: C is blocked again.
- An "Everything" person and app run A, B, C and W.
- A Family person with an app on "Everything": B is blocked (intersection).

### 5.4 Migration

Fixture: a synthetic `admin.db` and `categories.db` with the same shapes as
the owner's data (9 plans with the same rules, the 22 apps, keys with fake
hashes, the disabled mappings, the empty legacy grants table, the
`legacy-key-import` service account), built by a test helper. No secrets, no
copy of `data/`.

- Dry run writes nothing: input file hashes are unchanged, and only `--out`
  has new files.
- Dry-run report matches a golden file, including every row of §4.4.
- Apply with an unedited `approval.yaml` that contains a blocked row
  refuses.
- Apply on the fixture produces exactly the approved Check access matrix.
- Apply twice: the second run reports nothing to do.
- Input changed after the dry run (one key revoked): apply refuses.
- A fault injected mid-apply leaves both databases byte-identical to the
  backups.
- After apply: no table, column, setting, config key or plan name from
  §4.3's removed list exists.
- Rehearsal on a `.backup` copy of the owner's real `admin.db` in `~/.tmp`
  (never `data/`), report reviewed by the owner before the real apply.

### 5.5 UI

- Invite: no "None"; Send disabled without a plan; summary shown; 390 px.
- Settings: no default-plan control; "Access plan for new invitations" shows
  "Ask each time" first.
- Apps: member sees no picker; owner sees "Same as …" first.
- Review page counts equal the API preview for the same change.

## 6. Risks

| Risk | Mitigation |
|---|---|
| The owner locks themselves out of generation by narrowing their own plan | The review warns "You will lose access to …"; the admin API never depends on plans, so the owner can always repair it |
| Clients that use LiteLLM virtual keys directly stop working | Check M9 before apply; each gets an Azizam app key |
| Unknown LAN devices stop working after the cut-over | Check M2 shows keyless traffic volume; the owner lists devices before apply; owner decision 1 offers a named app |
| Untagged items vanish for blocked plans, which may surprise a household | The review counts "Not tagged yet" before apply; Categories shows an "Untagged" list with bulk tagging |
| Exact grants make plans tedious as models change | "Offer new models and workflows" and the "Who can use it?" step; revision re-pins show in the preview |
| Dropping protocol rules widens a plan in a way nobody listed | Check M4 must pass before apply; the behavior table lists every known widening |
| The category gate becomes the only authority on the hot path | It already caches by `PRAGMA data_version` (`category_enforcement.py:47-55`); measure request overhead before release |
| Browser surfaces use apps in ways this design has not traced | Trace each surface's app identity before build (UNVERIFIED in §2.3) |
| Spend counters' current subject is unknown | Confirm before reusing the fields (§2.8); no stored plan sets them today |
| One snapshot store for people and app assignments creates write contention on invites and app creation | Compare-and-swap with retry; assignment-only changes are small |
| This design was written by one agent from one research pass | Independent red-team before build (P20); the owner's decisions below |

## 7. Owner decisions

At most five. Each has a recommended default; everything else above is
decided.

1. **Keyless LAN access.** Recommended: **remove it**. Every device uses an
   app key. Alternative: keep one named app, "Home network devices", owned
   by you, with its own plan (for example Local only) and an "only from these
   networks" condition. It is visible and revocable, but every LAN device
   shares one identity in receipts.
2. **What your own apps inherit.** Recommended: Everything gains Direct
   engine access, and t3-pi-agent and Try (Tim) use "Same as its person"
   (rows 2-4 of §4.4). Alternative: keep a fifth plan, "Local and paid
   models" (today's Owner (local) exactly), for t3-pi-agent and Try.
3. **Anna's Museum App.** Recommended: **Local only** (it loses paid
   providers; its key was never used). Alternatives: Chat; disable it; keep
   paid providers.
4. **Disabled test apps and revoked keys.** Recommended: **delete the rows**
   of the 7 disabled test apps (plus cap, waspflow-dsh-probe and Paid
   provider proof after they are disabled) and their revoked keys, and keep
   their audit events and receipts. This needs a new purge operation with a
   preview (P19); none exists today [P§5]. Alternative: keep them disabled.
5. **Access plan for new invitations.** Recommended: **Ask each time**.
   Alternative: preselect one plan, for example Chat.

## Appendix: research to decision trace

| Decision | Research |
|---|---|
| One named plan per person and per app; intersection | R§20 Rank 1; OpenRouter member and key guardrails R§11; v3 A10 |
| No runtime default; four meanings of "default" kept apart | R§22; Tailscale allow-all bootstrap R§4; LiteLLM empty list means all R§12; Jellyfin all-libraries flag R§18 |
| Category rules inside plans, catalog beside | R§21; Jellyfin R§18; Tailscale Aperture R§4; Plex additive labels rejected R§17 |
| Deny wins; unknown blocks | v3; AWS explicit deny R§1; Jellyfin blocked tags first R§18 |
| Remove LAN bypass and path tokens | R§29; Plex LAN treated as owner NT4; Home Assistant NT2; Tailscale NT1 |
| Invite default is a preselection, future-only | Slack default channels R§7; Figma default teams R§10; Notion retroactive defaults rejected R§9 |
| Per-person and per-app cap counters | OpenRouter R§11; R§23 |
| Preview, apply with revision, restore | Tailscale policy file and tests R§4; Cloudflare reusable policies R§5; v3 |
| One-time migration, no coexistence | R§24; Microsoft verify-then-remove R§3; AWS inline to managed R§1; Tailscale coexistence rejected R§4 |
| No protocol restrictions in plans | OpenRouter R§11, LiteLLM R§12, Open WebUI R§13 restrict models and features, not wire protocols |

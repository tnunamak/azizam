# Owner visibility: private member work, an operational owner view

Status: design, v2, 2026-10-11. Not built. P20 requires a recorded go
decision on this version before the first implementation commit. v2 resolves
the red-team of v1 (`4a020412`, §11) and adds per-person storage (§6).
Baseline: `release/r18` at `8a3147f0`.

Owner direction: "Just follow best practices made clear by the many prior arts
like immich". Azizam is unreleased, so this design adds no compatibility shims
(P11).

## 0. Inputs

- **R**: research, `~/Documents/azizam/ux-findings/research-results/azizam-research-owner-visibility-20261011.md`
  (14 products, access matrix, metadata allowlist, sharing, break-glass,
  moderation, wording, acceptance criteria). R §n means its product section n.
- **RT**: red-team of v1, `…/azizam-redteam-owner-visibility-4a02041-r0.md`
  (2 blockers, 8 major, 1 minor; three owner defaults). RT n means finding n.
- **SR**: supervision research, `…/azizam-research-supervised-accounts-20261011-r0.md`
  (12 products and legal guidance; §13 "Minimal Azizam shape").
- **ST**: storage research, `…/azizam-research-storage-quota-ux-20261011-r0.md`
  (10 products; §12 "Recommended minimum design").
- **R18**: release 18 security review, `…/azizam-review-r18-security-8a3147f-r0.md`
  (finding 2: the owner's Library broadens to everyone without a choice).
- **AP**: `design/access-plans:docs/design/ACCESS_PLANS.md` v3 (plans, the
  accountable person, `access_epoch`, the migration protocol).
- **UB**: `design/ux-batch1:docs/design/UX_BATCH1.md` (§2.3 Recent, §3 account
  recovery, §4 "Don't save this prompt").
- **HR**: `docs/design/HEADLESS_AND_RESOURCES.md` (principle 4: members see
  shared capacity coarsely; owners see attribution, never prompt content).
- **PM**: `docs/design/PROMPT_MEMORY.md` (prompts are already maker-only).
- Principles P1-P22 (`docs/owner-principles`), `VOICE.md`,
  `docs/ops/DATA_RETENTION.md`, the code-quality canon.
- Code references. v1's inventory was made by a read-only sub-agent and
  spot-checked. For v2 a second read-only sub-agent checked every red-team
  citation and answered eight evidence questions. I re-read
  `ownership.py:1-102`, `proxy.py:25808-25828`, `custom_voices.py:1-136`,
  `me_media.py:483-500`, `:655-665`, `:735-742` and
  `resource_decisions.py:79-109`. Other line numbers come from the
  sub-agents and may be off by a few lines.

## 1. Current state

### 1.1 What is already private

- **Saved prompts and workflows.** `asset_prompts` is keyed by
  `(asset_id, person_id)` (`gateway/prompt_memory.py:31-41`). Every reader
  takes one person. The Library reads the **viewer's** prompts
  (`me_media.py:656`, `:676-677`), so another person's item shows
  `prompt: null` and "Picture by {maker}" (`_media_title`, `:1196-1206`).
  `md=full` delivery requires the maker's own kept prompt
  (`proxy.py:23633-23647`).
- **Saved chats.** A Responses row needs the same person **and** the same App
  (or the same appless key) (`_responses_row_visible`,
  `proxy.py:25808-25817`). There is no owner bypass.
- **Custom voices.** One folder per principal key
  (`custom_voices.py:59-62`), plus legacy owner folders (`:135-136`). There is
  no owner bypass.
- **Key API.** `/gateway/v1/*`, `/v1/videos/*` and MCP go through
  `owns_record` (`record_ownership.py:11-22`), `_asset_access_allowed`
  (`proxy.py:6930`) or `_video_job_access_allowed` (`proxy.py:9352`). None has
  an owner bypass.
- **Receipts store no bodies.** `receipt_store._assert_safe` (`:191-204`)
  rejects prompt, input, messages, body and bytes keys. Its own comment says
  the check is structural: it does not prove that values are safe (RT 4).
- **Logs are not clean.** v1 said "the gateway prints no prompts or bodies".
  That is false (RT 6). `mcp_server.py:529-530` prints `{exc}` for any tool
  failure, and 74 `print(f"…{exc}")` lines exist in `proxy.py` and
  `gateway/*.py`. Exception text can contain input or a signed URL. uvicorn
  access logging is off (`proxy.py:30203-30208`). §3.6 fixes the sinks
  this design depends on.

### 1.2 Where the owner sees member content or content-bearing metadata today

| # | Surface | Path | What the owner gets | How scope is decided |
|---|---|---|---|---|
| L1 | Library list and detail | `GET /me/api/media`, `/media/{id}` (`me_media.py:162-219`) | Every member's creations: kind, size, model, workflow, seed, maker name, `download_url` | `scope=everyone` or `person=all\|local-network\|<id>` (`_media_scope`, `me_media.py:461-481`). The web sends `scope=everyone` by default (R18 finding 2) |
| L2 | Download, posters | `GET /me/api/media/{id}/download` (`me_media.py:271-307`) | A signed URL for any member's creation | Same `_media_scope`. A poster is a separate asset (`poster_asset_id`, `media_ingestion.py:329`, `me_media.py:716`) fetched through the same route (`library/page.tsx:944-948`) |
| L3 | Signed URLs | `/v1/assets/{id}/content` (`proxy.py:23468-23560` → `:23563`) | Bytes, to anyone with the URL | HMAC over `asset_id:expires[:md]` only (`proxy.py:6843-6865`). No person and no grant. TTL 3600 s (`proxy.py:808`). Public handlers pass no `auth_context`, so the ownership check at `:23617` never runs on the wire |
| L4 | Receipt detail | `GET /me/api/receipts/{id}` (`proxy.py:16017-16038` → `user_runtime.py:373-428`) | Any member's receipt (client audience) | Owner role, implicit |
| L5 | Home, status, catalog and workflow feeds | `/me/api/activity`, `/me/api/home`, `/me/api/status`, `/me/api/models/catalog`, `/me/api/workflows` | Every member's recent receipts and jobs | `app_ids=None if owner` (`user_runtime.py:350-361`). Not audited |
| L6 | Usage | `GET /me/api/usage/summary` (`me_media.py:309-328`, `:1230-1430`) | Every member's receipts and totals, plus the people list | `person=all\|local-network\|<id>`. The web defaults the owner to `all` |
| L7 | Owner request log | `GET /admin/api/activity/requests` (`activity_api.py:19-127`) | Every receipt in the **admin audience**, plus a `source` envelope with `client_hint` (`:112-127`) | `"owner" in ctx.roles` (`:25`) |
| L8 | Status | `/v1/status`, `/status`, `/status/raw` | Live video jobs with `key_name` and `error` text; file names and paths of the newest creations (`status_endpoint.py:2469-2540`, `:2981-3031`) | `ADMIN_AUTH`, any admin |
| L9 | Keys on a member's App | `POST /admin/api/apps/{app_id}/keys` (`admin_api.py:2251`) | A key that reads that App's creations, receipts and jobs. `created_by` is the admin (`admin_runtime.py:585`), so `person_for_app_credential` gives it no person and no prompts (`:1604-1614`) | Any admin, any App. **This bypasses any UI-only policy** |
| L10 | Password reset | `POST /admin/api/people/{id}/reset` (`accounts_api.py:239-245`, `accounts.py:245-273`) | A link to sign in as the member. Default life 7 days (`accounts.py:246`) | Owner. The link row records the issuer (`account_links.created_by`, `accounts.py:42`, `:268-270`). The member gets no notice |
| L11 | Host files | `data/*.db`, `cache/images/*`, job spools, logs | Everything | Host authority. Outside product control |

Not an exposure today:
- `/me/api/jobs`, `/me/api/make/jobs` and notification polling are self-only,
  owners included (`me_media.py:122-160`; `me_make.py:178-224`).
- Library DELETE is self-scoped: `_member_media_scope(actor.user_id)`
  (`me_media.py:221-266`). It removes bytes only when the file has one owner
  and one maker (`:416`).
- People, Apps, credentials and config export carry no content.
- There is no content export or backup endpoint and no CLI in this repo.

### 1.3 Identity facts the core depends on

These are today's facts. §3.1 keeps them.

| Fact | Code |
|---|---|
| Records store the App principal `app:<id>`, not the key that wrote them. Every key of an App reads what the App made | `ownership.py:46-55`, `:75-77`, `:85-94` |
| Revoked keys stay **read aliases** for their principal. Revocation ends authentication only | `ownership.py:3-4`, `:85-94` |
| A key has a person only if its App is `personal` **and** the key's `created_by` is the App's owner | `person_for_app_credential`, `admin_runtime.py:1604-1614` |
| A browser session has a person directly, through its session App | `runtime_identity_for_session_app`, `admin_runtime.py:1690` |
| A creation's makers are `maker_person_ids` in its sidecar. Files are content-addressed, so two people who make identical bytes share one file with two makers | `proxy.py:7028-7040`, `:7048-7051`; `me_media.py:691-694` |
| App `kind` is `personal` or `service` and is never updated. No code changes `owner_user_id` | `admin_store.py:57`, `:1236`; only `INSERT` writes `kind` or `owner_user_id` (`admin_store.py:1249`, `:1338`; `accounts.py:344`) |
| Every credential records `created_by` (NULL or `""` when unknown). A back-fill sets `""` when the audit trail does not prove the owner made it | `admin_store.py:162`, `:346-387` |
| `admin.db` already enforces identity rules with triggers | `admin_store.py:176`, `:182-185` |

### 1.4 Authorization is spread out

There is no single authorization core. `owner` means `{owner, superadmin}` in
`me_media.py:77`, `:447` and `user_runtime.py:356`. It means `owner` only in
`activity_api.py:25` and `accounts_api.py:112`. No code grants `superadmin`;
a row can exist only from old data. The same ownership rule is written five
times: `owns_record`, `_asset_access_allowed`, `_video_job_access_allowed`,
`_responses_row_visible`, `_receipt_in_scope` / `_job_in_scope`
(`me_media.py:925-942`). The rules differ on purpose by resource (§1.3): App
for creations, person for prompts, person and App for chats.

### 1.5 Tests that encode owner-sees-all (to rewrite)

- `tests/test_me_media.py:459-482`, `:536-551`, `:617`, `:704-711`, `:877-892`,
  `:1043-1054`
- `tests/test_home_api.py:121-163`
- `tests/test_workflows_catalog.py:753-790`
- `tests/test_activity_api.py:44-107`
- `tests/test_prompt_memory_identity_r2.py:152`
- `web/e2e/live-data-scoping.spec.ts:37-65`, `web/e2e/live-data-audio.spec.ts:11`
- `web/e2e/library.spec.ts:293`, `:392`, `:408`, `:418`
- `web/e2e/prompt-memory.household.ts:216-240`, `:368-375`
- `web/src/app/library/page.spec.tsx:685-700`
- `web/src/app/library/prompt-visibility.spec.tsx:228-237`
- `web/tests/components/home-library-polish.spec.tsx:340`
- `web/e2e/activity.spec.ts:51-92`

## 2. Policy

**Member work is private by default, everywhere: UI, REST, key API, MCP and
CLI. The owner gets an operational view. Content leaves its maker only when
the maker shares it.** v1 has no "owner sees all" setting.

### 2.1 The access matrix (adopts R "Recommended Azizam access matrix")

| Data | Maker | Share recipient | Owner (operational view) | Admin (not owner) |
|---|---|---|---|---|
| Creation bytes, posters, signed URLs, inputs | Yes | The shared creation and its poster only | No | No |
| Prompt, workflow, saved chat, custom voice | Yes | Prompt only if the maker chose "Include prompt" | No | No |
| Receipts and jobs | Full own view, at the plan's receipt detail | No | The §3.3 projection only | No |
| Storage | Own counts and bytes (§6.2) | No | Per person: counts and bytes by kind; disk health (§6.1) | No |
| Accounts, plans, keys | Own; keys only for Apps they are accountable for (§3.5) | No | Invite, plan, disable, revoke, reset; keys only for Apps the owner is accountable for | As today, except keys |
| Audit | Own sharing list; Security events, including owner-issued resets (§5) | No | Configuration and access records, with no content | As today |

Anyone who is the accountable person of a **service** App also sees what that
App made (basis "Accountable", §3.1). That is App content, not member
content, and the Privacy page says so (§7).

"Owner" is one role, `owner`. Live `superadmin` rows are listed in the
build preview and become `owner` or `admin` there, with no alias kept (P11).

Attributed operations are **owner-only**. Admins keep engine and capacity
status with person and key attribution removed (§3.3).

### 2.2 Why there is no "owner sees all" setting in v1

1. **The closest precedents keep personal work out of the admin's view.**
   - Immich gives each account "their own library", and server administration
     is a separate plane (R §1).
   - Bitwarden and Vaultwarden admin portals "do not provide access to the
     protected data" (R §5, §6).
   - Nextcloud's admin works with accounts and quotas; Files shows only the
     current account's content (R §11).
   - SharePoint admins "don't have automatic access to all sites and each
     user's OneDrive" (R §14).

   PhotoPrism, Jellyfin and Plex share one operator collection; Home
   Assistant shares one household state. A creation made from a member's own
   prompt is personal work, not a shared collection (R "Cross-product
   conclusions" 1).
2. **A toggle makes every promise conditional.** Open WebUI defaults
   `ENABLE_ADMIN_CHAT_ACCESS` to true, and its own docs say the per-user flags
   "do not establish a security boundary against the admin" (R §9). With a
   toggle, member copy must say "unless the owner turned it on", plus a
   notice when it changes.
3. **The owner's real needs are operational.** Cost, abuse control, capacity
   and storage all work from attribution and quantities (R "Moderation" 2).
   HR principle 4 already says "never prompt content".

### 2.3 Supervised accounts (children): deferred, with a fixed direction

A household with children may want oversight of a child. v1 gives
prevention: access plans provide capability grants, blocked categories,
caps and disable (AP §2.1). v1 does not build supervision.

SR now gives the future shape. It is recorded here so that nothing in v1
blocks it:

- **Four separate concepts** (SR §13.A): member identity, access plan,
  **supervisor relationship**, individual sharing. A supervisor is a named
  adult linked to one member. The server owner is not a guardian by role,
  and a guardian is not an administrator by role.
- **The relationship is its own layer.** It is a record
  (supervisor, member, shared fields, start, end) read by the operational
  projection (§3.3). It is **not** a basis in `content_access`. It never
  widens a plan (SR §13.D).
- **First release shares coarse data only**: usage counts by broad kind and
  spend against allowance, the same view shown to the member, from the start
  date forward (SR §13.B). Plus a member-initiated "Ask {name} for help",
  which is a share (§4) with a preview.
- **Member copy when true**: "Supervised by {name}. They can see your usage
  and spending. Your prompts and creations stay private unless you share
  them." (SR §13.B).
- **Deferred beyond that**: content visibility (only as a separately labeled
  supervised workspace, prospective, never retroactive, SR §13.F) and
  automated safety alerts (SR §13.E).
- **Before building**: the SR §13.H evidence gates, plus the tension between
  P19 (keep data) and children's data-minimization duties (SR §13.G).

## 3. Enforcement: one core with typed resources

### 3.1 The module

`gateway/content_access.py` replaces the five ownership checks in §1.4 and
the owner branches in L1-L7. v1 proposed one universal "Own" rule. RT 1
showed that this drops real distinctions: an App key that may read a
creation must not read its maker's prompt or chat, and a share must not
satisfy delete. v2 keeps **one module and one decision function**, but the
decision takes the resource kind and the action. Each kind keeps today's
identity rule (§1.3).

```python
@dataclass(frozen=True)
class Viewer:                     # built once per request from trusted auth
    person_id: str | None         # session person; for a key, person_for_app_credential()
    app_id: str | None            # the App the request authenticates as
    key_hash: str | None          # set only for an appless key (chat rule)
    owner_keys: frozenset[str]    # readable principals (ownership.readable_owner_keys);
                                  # for a signed-in person, the principals of the Apps they own
    is_owner: bool                # role == "owner"; read only by operational()

Kind   = Literal["creation", "prompt", "chat", "voice", "receipt", "job"]
Action = Literal["read", "delete", "reuse", "share", "link", "cancel"]
Access = Own | Accountable | Shared(share_id, include_prompt) | Deny

def decide(viewer: Viewer, record: ContentRecord, action: Action) -> Access
def require(viewer, record, action) -> Access    # raises 404 not_found on Deny
def operational(viewer) -> bool                   # viewer.is_owner
def project(record: ReceiptRecord | JobRecord | StorageRecord) -> OwnerView   # §3.3
def shared_view(record: CreationRecord, share: Share) -> SharedView          # §4.2
```

`ContentRecord` is a tagged union. Each kind carries only the identity
fields it already stores:

| Kind | Identity fields | Source |
|---|---|---|
| `creation` (bytes, poster, sidecar facts) | `asset_id`, `owner_keys`, `maker_person_ids`, App | sidecar (`proxy.py:6924-6927`, `:7048-7051`) |
| `prompt` (saved prompt, workflow) | `asset_id`, `person_id` | `asset_prompts` key |
| `chat` (stored Responses) | `person_id`, `app_id` or `owner_key_hash` | `proxy.py:25808-25828` |
| `voice` (custom voice, consent) | principal folder | `custom_voices.py:59-62`, `:135-136` |
| `receipt` | App principal, `person_id` at submission | receipt row |
| `job` (and its events) | `owner_key_hash` / `owner_scope`, App | `media_jobs.py:187-190` |

**Bases.** Each is one predicate:
- **Own**: the record's principals intersect `viewer.owner_keys`. Today's
  App-level rule.
- **Self**: `record.person_id == viewer.person_id`, both non-null. An App
  never satisfies Self.
- **Self+App**: today's chat rule, unchanged.
- **Maker**: Own, **and** `maker_person_ids == [viewer.person_id]` (exactly
  one maker).
- **Accountable**: the record has no person, its App has kind `service`, and
  the App's `owner_user_id` is `viewer.person_id`. This **amends AP §2.4**,
  where the accountable person is "only the policy and spending subject". A
  record with no person still needs someone who can find and delete it in
  the product (P19). Member copy discloses it (§7).
- **Shared**: an active `shares` row for this `asset_id` (or its
  `poster_asset_id`) and `viewer.person_id` (§4).

**The action matrix.** A cell lists the bases that allow the action. A blank
cell is Deny. This table is the whole policy. The test in §9.2 item 1 is
generated from it.

| Kind | read | delete | reuse (input, "Use again", workflow download, `md=full`) | share | link (mint a signed URL) | cancel |
|---|---|---|---|---|---|---|
| creation | Own, Accountable, Shared | Own, Accountable | Own | Maker | Own, Accountable → `g=o`; Shared → `g=s` | |
| prompt | Self; Shared with `include_prompt` reads the **share's maker's** row | Self | Self (`md=full` also needs Self) | | | |
| chat | Self+App | Self+App | Self+App | | | |
| voice | Own | Own | Own | | | |
| receipt | Own, Accountable | | | | | |
| job | Own, Accountable | | | | | Own, Accountable |

Rules that follow from the matrix:
- **Role is never an input.** `is_owner` feeds only `operational()`.
- **Shared never satisfies a mutation**, `reuse`, `share` or an `o` link.
- **Prompts and chats never follow the App.** A key on a member's App reads
  that App's creations (Own) but not the member's prompts (Self) or chats
  (Self+App). Today's code already behaves this way: an admin-made key has
  no person (§1.3).
- **Receipt deletion is not an action.** Retention is an owner setting with
  preview (P19, DATA_RETENTION).
- Requests with no identity get Deny.

**How identity enters `Viewer`.**
- Browser session: `person_id` is the session person; `app_id` is the
  surface App (AP §2.4: session App, Home's personal App, Try's App).
- App key: `app_id` is the key's App; `person_id` comes from
  `person_for_app_credential`, so only a personal App's key made by its owner
  has a person.
- Maker: the request's person at write time, stored in `maker_person_ids`. A
  creation made through a key with no person has no maker and cannot be
  shared.
- Person-less App: kind `service`. A personal App key made by someone else is
  also person-less today; the switch revokes those keys (§3.5).
- Auth-disabled mode (`proxy.py:14162`, `unrestricted=True`) is removed by
  AP §2.8. Until AP lands, startup refuses auth-disabled config once the
  visibility switch has run (§8).

This is a consolidation, not new machinery. The stored data shape is
unchanged. The only new input is the `shares` table.

### 3.2 Every read path uses it

| Path (from §1.2) | Change |
|---|---|
| L1 `/me/api/media`, `/media/{id}` | Always the caller's Own, Accountable and Shared items. `view=mine` (default) or `view=shared-with-me`. `scope`, `owner` and `person` are removed (§3.7). `_MediaScope.app_ids=None` and `audit_everyone` are deleted |
| L2 download, posters | `require(viewer, creation, "link")`, then mint a URL with that access (§3.4) |
| L3 signed URLs | §3.4 |
| L4 `/me/api/receipts/{id}` | Own and Accountable only; 404 for every other role. The owner reads others at `GET /admin/api/activity/requests/{id}` as the projection. `receipt.detail.instance` audit is deleted |
| L5 activity, home, status, catalog, workflows | `/me/*` is always the caller's own. The owner branch at `user_runtime.py:350-361` and the `"scope": "instance"` label are deleted |
| L6 Usage | `/me/api/usage/summary` is own only. The owner's per-person usage and storage move to `GET /admin/api/usage` and `GET /admin/api/storage`, in the projection |
| L7 request log | Built entirely by `project()` (§3.3), including the envelope. New `GET /admin/api/activity/requests/{id}` |
| L8 status | Delete `recent_videos`, `recent_images` and `recent_renders` (`status_endpoint.py:3960-3963`). `_live_video_jobs` uses the job projection. `key_name` on live jobs, `auth_keys`, per-key request rates and `recent_events` are owner-only |
| L9 keys on a member's App | Refused at creation; existing ones revoked by the switch (§3.5) |
| Job events | Own only, on every surface. The `admin` job audience (`media_jobs.py:276-287`, a denylist that still includes `assets` and `error`) and the admin event audience (`:1142-1157`) are deleted. No owner event view exists in v1 (RT 5). Per-job event sequence numbers stay; household-wide row ids stay hidden (`:1164`) |
| `/gateway/v1/*`, `/v1/videos/*`, `/v1/responses/*`, voices | Call `decide` with the kind and action. Behavior stays the same, by construction: each kind's basis is today's rule |
| MCP (`/mcp`) | Every tool and sub-operation calls the same core through the REST path it dispatches to (`mcp_server.py:255-261`): `capabilities`, `run` per target kind, `job` poll, wait, content and cancel (`:454-503`) |

**Inventory (RT 5).** One test lists every entry point and fails CI for an
unclassified one:
- every FastAPI method and route, including HEAD;
- every MCP tool and sub-operation, and each internal dispatch callback
  (`McpDeps.call`);
- every job event type, which must be in a closed set (`media_jobs.py:1023-1026`
  accepts any non-empty string today).

Each entry is `content:<kind>:<action>`, `operational`, `own-account` or
`none`. Classification is a guardrail. The matrix test (§9.2) proves
behavior.

**The owner's Library "Everyone" view is removed, not narrowed.** The owner's
Library is the same member Library (P8): their own work, what their
accountable Apps made, and "Shared with me". This also resolves R18 finding
2. The owner's storage question moves to Usage (§6.1).

### 3.3 The operational projection (a typed, recursive contract)

`project()` builds a **new** value from a typed schema. It never copies or
filters a record. RT 4 showed that a shallow allowlist is not enough:
`cost` and `resource_decision` are nested maps, and the envelope and
aggregate helpers add `client_hint`, `source_label` and `requested_model`
back (`activity_api.py:112-127`, `receipt_store.py:349-369`,
`me_media.py:1540-1547`). So the rule is:

1. **Every leaf has a type**: number, boolean, timestamp, opaque id, closed
   enum, or a display name looked up from accounts or the catalog. No other
   string reaches an owner view of someone else's record.
2. **The whole response is projected**: items, envelope, filters, source
   lists, aggregate group keys and labels. The current activity and usage
   envelopes are not reused.
3. **Names come from trusted ids.** A model or workflow label comes from the
   catalog id the gateway resolved. If there is none, the label is "Unknown
   model". The free-text `requested_model` is never a fallback.
4. **Disclosed on purpose**: person names, App names and model names. They
   are account and catalog metadata that the owner already manages. The
   Privacy page says the owner sees them (§7).

| Kind | Field | Type |
|---|---|---|
| Receipt | `request_id`, `job_id` | opaque id (correlation only; grants nothing, §3.1) |
| | `attempt` | integer |
| | `created_at`; `queued_at`, `started_at`, `first_output_at`, `finished_at` | timestamps |
| | `person` | `{id, name}` of the **spending subject at submission** (AP ledger `person_id`), else `unknown`. Never the App's current owner (RT 11; `activity_api.py:82-88` does that today) |
| | `app` | `{id, name}` |
| | `source_kind` | enum (`app_key`, `session`) |
| | `protocol` | enum; `stream`: boolean |
| | `capability` | enum (AP §2.2 capability ids) |
| | `model`, `workflow` | `{catalog_id, name}` or `{null, "Unknown model"}` |
| | `route` | enum `local`, `provider`; `provider`: catalog id |
| | `status` | enum; `status_code`: integer |
| | `terminal_reason` | enum (`receipts.py:185-187`) |
| | `layer` | enum `request`, `plan`, `gateway`, `engine`, `provider`, `unknown`, derived from `terminal_reason` and `status_code` |
| | `latency_ms` and phase durations | integers |
| | `usage` | fixed numeric keys only: input and output tokens, images, audio seconds, video seconds, characters |
| | `cost` | `{amount_usd: decimal or null, quality: measured\|estimated\|unknown, source: provider\|price_table\|none, reconciled: boolean}` |
| | `refusal` | `{code: enum}` |
| | `policy` | `{decision: allow\|deny, reason: enum}` (AP denial codes) |
| | `resource_decision` | list of `{code, effect, resource_class}`, all enums, as `project_decision(…, "client")` builds them (`resource_decisions.py:79-96`). The owner audience's `rule` and `observed_at` are dropped |
| Receipt list envelope | `coverage_start`, `aggregate_complete`, `unknown_paid_count`, `history_note` | timestamp, boolean, integer, enum (kept from `me_media.py:1389-1409`, RT 11) |
| Job | `job_id`, `capability`, `model`, `workflow`, `status`, `phase`, `attempt`, `created_at`, `updated_at`, `latency_ms` | as above |
| | output quantities | integers: count, width, height, duration seconds, bytes |
| Storage | §6.3 | integers, timestamps, enums |

Dropped, for example: `path`, `requested_model`, `accepted_inputs`,
`rejected_inputs`, `ignored_inputs`, `warnings`, `fallback_reasons`,
`client_hint`, `source_label`, `request_shape`, LiteLLM ids, job `error`
text, `assets`, `seed`, `input_hash`, `metadata`.

Support without raw errors (RT 11): the owner sees `layer`, `status_code`
and `terminal_reason`. When `layer` is `unknown`, the owner request detail
shows "Ask {Name} to share the details." The member can share a creation
(§4) or send their own error text out of band. There is no owner bypass for
raw errors.

The plan's receipt detail setting (AP §2.6) controls only how much a caller
sees of **their own** receipts. It never widens the projection.

### 3.4 Signed URLs carry a grant

The signed message becomes `asset_id:expires:md:g`. **Both `md` and `g` are
required.** `g` is `o` (Own or Accountable) or `s.<share_id>`.

Delivery (`_signed_asset_content_response`, `proxy.py:23563`) runs these
checks **before it reads any bytes** (today the read is at `:23629`):
1. Verify the signature. A URL without `md` or `g` fails with 403, the same
   message as a bad signature. No shim (P11).
2. If `g=s.<id>`: the share is active, and the asset is the share's
   `asset_id` or its `poster_asset_id` (RT 7: the poster is a separate
   asset).
3. A share grant never allows `md=full`.
4. A share grant never serves a container that has no metadata stripper.
   Today that is GIF (`media_metadata.py:62`). Sharing refuses such items
   (§4.1).

Then delivery continues as today: Range slicing (`media_delivery.py:194-234`)
and metadata stripping (`:174-191`) run after the checks. Cache rules
(RT 7):
- There is no ETag, 304 or server-side byte cache today (none found). Any
  later one must sit after these checks; the inventory test lists HEAD
  routes, so a new HEAD handler cannot skip them.
- `Cache-Control` stays `private, max-age=3600` for `g=o`
  (`media_delivery.py:208-209`). `private` keeps shared caches out. For
  `g=s` it is `private, no-store`, so a stopped share does not keep
  rendering from the recipient's browser cache.

**What "lose access now" means** (copy §7): the next fetch fails. Bytes
already downloaded, cached or in transit cannot be recalled. A `g=o` URL is a
bearer capability until it expires (at most 1 hour, `proxy.py:808`), and it
is minted only for Own or Accountable access.

**Metadata stripping is tested, not assumed.** Stored bytes are stripped on
write (`proxy.py:7027`) and stripped again on delivery unless `md=full`. The
test puts a canary in the metadata of every container the stripper handles
(`media_metadata.py:467-500`): PNG, JPEG, WebP, WAV, FLAC, MP3, MP4, WebM,
MKV, OGG and M4A. It asserts that a `g=s` fetch does not contain the canary.

Every minting site takes an `Access`, so a URL cannot be minted without one:
- `build_signed_asset_url` (`proxy.py:7172`) and `build_signed_image_url`
  (`:7186`);
- MCP results (`_mcp_store_asset`, `:14636`);
- generation responses on `/v1/images/*` and `/v1/videos/*` (the caller is
  the maker: `o`);
- the Library download route, after `require(…, "link")`; a poster is minted
  with its parent's access.

### 3.5 Who can create a key, and what can change an App

A key reads everything its App made (basis Own). Creating a key is therefore
a content grant. RT 2 showed that key creation is not the only way to get an
App's principal: changing an App's owner or kind would do it too. v2 closes
both.

**1. Only the App's accountable person creates keys for it.** These are
every issuance path (sub-agent inventory; rotate and import do not exist):

| Path | Today | v2 |
|---|---|---|
| `POST /admin/api/apps/{id}/keys` (`admin_api.py:2251`) | Any admin, any App | `403 not_your_app` unless the caller is the App's `owner_user_id` |
| `POST /me/api/apps/{id}/keys` (`proxy.py:16097`) | Own App only (`user_runtime.py:140`) | Unchanged |
| `POST /me/api/try-key` (`proxy.py:16129`) | Own Try App, session-bound | Unchanged |
| Invite acceptance (`accounts.py:346`) | The new person's own App | Unchanged |
| `POST /setup/first-app` (`proxy.py:17783`) | Person branch uses the own-App path; other branch `ADMIN_STORE.create_credential` (`:17853`) | Both must target an App the caller owns. Which App the other branch uses is UNVERIFIED; the build checks it |
| `POST /admin/api/admin-keys` (`admin_api.py:2280`), `generate-admin-key` (`proxy.py:2716`) | Control-plane `service_account` credentials | Unchanged. Their principal `service_account:<id>` owns no content. With role `owner` they get `operational()` only |

**2. The App binding is fixed by the database.** New `admin.db` triggers,
in the existing trigger style (`admin_store.py:176-185`):
- refuse any `UPDATE` of `apps.kind`;
- refuse any `UPDATE` of `owner_user_id` on a `personal` App;
- on a `service` App, refuse a change from one person to another while the
  App has an unrevoked key. A first assignment from NULL is allowed (AP
  migration moves service Apps from service accounts to people).

No code changes these columns today (§1.3), so the triggers cost nothing now.
They stop a future route from moving a personal App, and its history, to
someone else.

**3. Service App transfer is a content transfer.** v1 has no transfer route.
When one is built, it is one operation that:
- revokes every key of the App (trigger 2 requires it); `access_epoch` then
  invalidates in-flight permits (AP §2.13);
- shows the new person, before confirming: "You'll see everything {App}
  made, including before today.";
- is audited, and keeps past spend with the person who was the spending
  subject at the time (§3.3, AP RT2 M3).

**4. Keys of unknown or other provenance fail closed.** The build preview
lists every unrevoked key whose `created_by` is not its App's
`owner_user_id`, including NULL and `""`. The switch revokes all of them.
Nothing infers provenance from receipts. The App's accountable person makes
a replacement key before the switch (§10.1).

**5. Revocation ends authentication.** Historical read aliases
(`ownership.py:85-94`) stay. They map old records to the same principal and
give nothing to a revoked key. Deleting them is neither needed nor enough
(RT 2).

### 3.6 Logs and side stores

- **MCP tool failures** (`mcp_server.py:529-530`): print the exception class
  and a correlation id, never `{exc}`. The client already gets a generic
  error.
- **The other 74 `print(f"…{exc}")` lines.** The build lists them. It fixes
  the ones on request paths that carry member input: provider adapters, job
  runners and MCP. A ratchet test fails CI if the count rises. Logs are host
  files (L11), so the honest-limits sentence covers them. This step only
  stops avoidable copies.
- **Canaries reach the exception paths.** The §9.2 canary test includes a
  failing MCP call and a provider error that echoes the input, and it scans
  stdout and stderr.
- **LiteLLM**: pin `litellm_settings.turn_off_message_logging: true` and
  `general_settings.store_prompts_in_spend_logs: false` in the generated
  config (`gateway/litellm_substrate.py:75-89`). Its behavior is a
  deployment gate, UNVERIFIED until tested on the deployed version.
- **Reverse proxy (Traefik)** may log `sig=` query strings. That is outside
  this repo and a deployment gate. The ops note says to log paths without
  query strings for `/v1/assets/` and `/v1/images/`.
- `cache/key_activity.jsonl` (`firehose_metrics.py:69`) already strips prompt
  fields. It stays operational.

### 3.7 Removed query parameters fail closed

Today `_scope_requested` checks truthy values (`me_media.py:483-485`), so
`?scope=` passes as "not requested". Removing the FastAPI argument alone
would ignore the key rather than reject it (RT 10). One validator, applied
to every `/me/*` route, rejects the **presence** of `scope`, `owner` or
`person` in any form: empty, repeated, or alongside other values. It returns
`400 param_not_supported`.

## 4. Sharing v1

**Smallest useful version: share one creation, read-only, with one or more
people. The owner is an ordinary recipient. The prompt is not included unless
the maker chooses it. The maker can stop sharing.** This follows Immich
albums and partner sharing (R §1), and Nextcloud and OneDrive "private until
shared" (R §11, §14).

### 4.1 Data

New table `shares` in `media_jobs.db`, next to `asset_prompts`:

```
share_id TEXT PRIMARY KEY, asset_id TEXT, poster_asset_id TEXT NULL,
maker_person_id TEXT, recipient_person_id TEXT, include_prompt INTEGER,
created_at INTEGER, revoked_at INTEGER NULL, revoked_by TEXT NULL
CREATE UNIQUE INDEX shares_active ON shares(asset_id, recipient_person_id)
  WHERE revoked_at IS NULL
```

- **Only the sole maker can share** (basis Maker). An item with more than one
  maker (identical bytes made by two people, §1.3) returns
  `409 share_not_allowed` with reason `several_makers` (RT 8). An item with
  no maker (a service App's work) returns reason `no_maker`. An item whose
  container has no metadata stripper (GIF) returns reason `file_type`.
- **The row's maker is authoritative.** Prompt reads, updates and revocation
  use `maker_person_id`. `PUT` updates only rows whose maker is the caller.
  It never touches another maker's row.
- **The prompt is read live.** With `include_prompt`, the recipient reads the
  row maker's current `asset_prompts` row. If the maker erases it or never
  saved it, the recipient sees "Prompt not saved". There is no copy, so PM
  erasure keeps working.
- Sharing counts no extra storage. Bytes stay with the maker (§6.3).
- The maker stops sharing, or the recipient removes it from their own
  Library. Both set `revoked_at`. Revoked rows are kept as history:
  DATA_RETENTION class (a), with no automatic expiry.
- **Deletion.** When the maker deletes the creation from their Library,
  their shares of it are revoked. Bytes may stay on disk for other owners
  (`me_media.py:416`); those owners' access does not change.
- Every share write also bumps `shares_revision` (§8, restore guard).

### 4.2 Recipient view: its own projection

v1 said a share adds the recipient to the access check. Returning today's
Library item would then leak `workflow`, `seed`, `job_id`, `request_id`,
`poster_asset_id`, `workflow_download_url`, provenance and a prompt-derived
title (`me_media.py:695-723`, `:748-788`, `:1156-1206`; RT 3). So a shared item
is built by `shared_view()`, a typed projection like §3.3:

| Field | Value |
|---|---|
| `share_id`, `asset_id` | opaque ids (the asset id is the shared item itself) |
| `kind`, `mime` | enums |
| `size_bytes`, `width`, `height`, `duration_s` | integers |
| `model` | `{catalog_id, name}` or "Unknown model" |
| `maker` | `{id, name}` |
| `shared_at` | timestamp |
| `title` | "{Picture\|Video\|Audio} from {maker}". Never derived from the prompt |
| `download_url`, `poster_url` | minted with `g=s.<share_id>`, `md=none` |
| `prompt` | the live prompt if `include_prompt`, else absent |

- The "Shared with me" list is built from the `shares` table joined to the
  sidecar, not from the own-receipt filters (`me_media.py:660-663`,
  `:739-740`). So cache-only shared items are not dropped.
- `shared_view()` runs **before** title, search and filters. `q` matches the
  maker's name, the model name and, only if included, the live prompt.
- No workflow download, no `md=full`, no receipt, no reuse, no re-sharing.

### 4.3 Notice

A share adds a Recent entry for the recipient: "{Name} shared a picture with
you." This changes UB §2.3, where a Recent entry is "one recipient's record
of one job reaching its terminal state". It becomes "… or one creation shared
with them", read by the same `GET /me/api/recent`. If Recent ships later than
sharing, "Shared with me" in the Library is the only signal.

### 4.4 Deferred explicitly

Each needs its own research and design: groups and albums; public or link
sharing; edit or contribute rights, and re-sharing; sharing chats, prompts
alone, receipts or workflows; share expiry (§10.2 question 3); partner-style
whole-library sharing; copying a shared creation into the recipient's
Library; notice when a recipient opens a share; sharing items with several
makers.

## 5. Support, break-glass and moderation

**Decision: v1 has member-initiated sharing only. It adds no new product
path that lets the owner open member content.** One path already exists: an
owner-issued password reset lets the owner sign in as the member (L10). v1
said there was "no product path"; that was wrong (RT 9). v2 states it on
the Privacy page (§7) and keeps it visible after the fact.

Why no break-glass:
- A break-glass path needs separate approval, a reason, expiry and a notice
  (R "Support and break-glass" 2-4). In a one-owner household nobody can
  approve it except the owner. R: "dual approval may not be possible".
- The owner keeps host authority (L11). A product break-glass adds code but
  does not change what the owner can do.
- Support works by sharing. The member shares the failing creation and can
  include the prompt. For a failure with no creation, the owner's projection
  shows `layer`, `status_code`, `terminal_reason`, model and timing (§3.3).

**The reset record.** The issuer is already stored: `account_links.created_by`
(`accounts.py:42`, `:268-270`). UB §3.4 shows "Password changed {time}." in
Account → Security at the next sign-in. v2 amends it:
- When the redeemed link was issued by someone else, the event reads
  "Password changed {time} with a reset link from {Owner}." It is joined from
  the redeemed link's `created_by`. The redemption audit
  (`accounts.py:357-373`) does not record who redeemed the link, so the copy
  never says who signed in.
- The owner cannot remove the event in the product. This makes a reset
  visible afterward. It does not prevent it.

Rejected for v1: recovery delivered to the member through a verified
channel, with notices to the prior channel. Azizam sends no mail (UB §3.1),
so no independent channel exists. The CRM mail module can add a notice to
the member's address when it ships. Until then the owner's ability to take
over an account is a stated trust limit.

Moderation in v1 does not browse content (R "Moderation" 1-2):
- **Prevent** with plans: grants, blocked categories, caps (AP).
- **Stop** by disabling the person or revoking keys.
- **Report** by a member sharing a creation with the owner.

Owner-initiated cancel of someone else's running job, without seeing its
content, belongs to the owner "Now" view (HR Release C item 2).

## 6. Storage

ST recommends per-person storage inside Usage, a private cleanup view for
each member, no quota by default, and a protective pause on new work when the
disk is nearly full (ST §12). v2 takes the smallest version of each. Nothing
counts storage per person today. The only free-space check guards the
speech-to-text spool (`stt_chunking.py:1428-1433`, `:573-575`).

### 6.1 Owner: Storage in Usage (owner section)

- **Disk.** One line per filesystem that holds Azizam data (the creations
  folder and `data/`), merged when they are the same device: free bytes,
  total, percent used, measured time, and state Healthy, Low, Paused or
  Unknown (§6.4).
- **People.** One row per person: name, stored bytes, item count, sorted by
  bytes. Tapping a row shows the split by kind: pictures, videos, audio,
  voices, other. No limit column (§6.5).
- **Not linked to a person.** Service App work, by App name (the accountable
  person can open it in their Library), and bytes no person or App accounts
  for. These are never spread across people (ST §12).
- **Gateway data.** Databases and caches, as one separate line.
- No file names, thumbnails, single-item dates or links. It is the storage
  projection (§3.3).

### 6.2 Member: their own storage

- Usage gains a Storage row: "{12.4 GB} stored" and "Review storage". Its
  detail says "No personal limit. Everyone on this server shares its space."
- "Review storage" opens the Library sorted largest first (new sort
  `sort=size`), with the existing kind filter. Selecting items shows
  "{n} selected · frees about {x}". The estimate counts only files where the
  member is the only owner and maker, because other files stay on disk
  (`me_media.py:416`). If a selected item is shared, the confirmation adds
  "{n} people lose access." Deletion is today's Library delete. There is no
  trash in v1.
- After deletion the number is measured again, not computed.

### 6.3 Accounting

- **Person's stored bytes** (logical): the sizes of creation files whose
  sidecar lists one of the person's App principals, plus their voice folders
  (`custom_voices.py:103`). Sharing adds nothing.
- **Files with several owners** count in full for each owner. The server
  disk line counts them once. Reclaim estimates exclude them (§6.2).
- **How it is measured.** Sidecars have no size field, so a scan reads file
  sizes, caches the result and returns `measured_at`. A result older than
  15 minutes is shown as stale (P18). The scan cost on the live cache is
  UNVERIFIED; if it is slow, the build adds `size_bytes` to sidecars at
  write time instead.

### 6.4 Pause new work when the disk is nearly full

ST: "stop admitting uploads/generation jobs that need affected storage
BEFORE invoking billable providers", and never delete to make room. The
speech-to-text spool already does this with `min_free_bytes`, so this
extends an existing check rather than adding one.

- **One check, at admission.** Before any job that writes creations or
  uploads (image, video, audio, voice) is admitted, and before any engine or
  paid provider call: if free space on the target filesystem is below the
  reserve, refuse with `503 storage_paused` (layer gateway).
- **Reserve.** Owner setting `storage.reserve_percent`, default 5% of the
  filesystem (ST's proposed 95% pause). Warning (state Low) at 10% free.
  Resume only when free space exceeds the reserve by 1% of the filesystem,
  so the state does not flap.
- **Unknown fails closed.** If free space cannot be read, new work pauses and
  the disk line shows Unknown.
- **Running jobs finish.** If a write fails for lack of space, the job ends
  with terminal reason `storage_full`, not success.
- **What still works:** sign-in, browsing, downloads and deletion.
- **No reservation ledger in v1.** Concurrent jobs can race for the last
  bytes; the reserve is meant to absorb that. Whether 5% is enough for the
  largest video output at the configured concurrency is UNVERIFIED.
- Nothing is ever deleted automatically (P19).

### 6.5 No quotas in v1

ST's default is no personal quota, and Immich's quota is empty (unlimited)
by default. A quota is a ceiling, not free space, so it would not prevent a
full disk; §6.4 does that. The existing voice limit stays
(`custom_voices.py:47-52`). An optional per-person hard cap is ST's next
step (warning at 90%, refuse new storage at 100%, never delete). It is
deferred until a household asks for it.

## 7. Member wording

The copy follows VOICE.md: fact first, one next action, plain words, each
fact once per screen. `{Owner}` is the owner's display name.

| Where | Exact copy |
|---|---|
| Home, first sign-in for a member; once for existing members after the switch | Title "Your work is private". Body "{Owner} sees your usage, spend and storage, not your creations or prompts." Link "What {Owner} can see" (Account > Privacy). Button "OK" |
| Account > Privacy | Heading "Privacy". Rows: "Private to you: creations, prompts and saved chats." / "{Owner} sees: when you made something, the model, the app you used, how long it took, what it cost and your storage." / "Shared apps: work made through an app that several people use, like a chat bot, belongs to that app. The person responsible for the app can see it." / "Shared: only what you share, with the people you choose. Prompts only if you include them." → "Manage sharing". Then the honest-limits sentence |
| **Honest limits** (Account > Privacy; "What is saved?" in Try) | "{Owner} runs this gateway and can read the files it stores. {Owner} can also reset your password and sign in as you; Account > Security shows when a reset happened. Azizam keeps your work out of the owner's views, but it isn't end-to-end encrypted. The engine or provider that makes your work receives your prompt." |
| Account > Security, after a reset someone else issued (amends UB §3.4) | "Password changed {time} with a reset link from {Owner}." |
| Library detail | Button "Share" |
| Share sheet | Title "Share this {picture\|video\|audio}". List "With", with each person's name; the owner's row reads "{Name} (owner)". Switch "Include prompt", off by default. Note when off: "They see the {result} and its model. Not your prompt." When on: "They also see: “{prompt}”". With no saved prompt the switch is disabled: "Prompt not saved, so it can't be shared." Footer: "They can download it." Primary "Share with {Name}" / "Share with {n} people" |
| Share refused | `several_makers`: "Someone else made the same file, so it can't be shared." `file_type`: "GIF files can't be shared yet." `no_maker`: "Work made by a shared app can't be shared." |
| After sharing | The sheet closes. The detail shows the badge "Shared with {Name}" or "Shared with {n} people" (no toast, P2); tap opens the list |
| Stop sharing | "Stop sharing with {Name}? They can't open it after this. Copies they downloaded stay with them." Buttons "Stop sharing" / "Cancel" |
| Recipient | Library filter "Shared with me". Caption "From {Name}". Action "Remove from my Library", note "{Name} still has it." Empty state "Nothing is shared with you yet." Stale open "{Name} stopped sharing this." |
| Recent | "{Name} shared a {picture} with you." |
| Owner Usage, per-person view (once, under the person picker) | "Creations and prompts stay private to each person." |
| Owner request detail, someone else's request | Row "Result" with value "Private to {Name}". When the layer is unknown: "Ask {Name} to share the details." |
| Invite sheet (owner) | "{Name} gets a private Library. You see their usage, spend and storage." |
| Member Usage, Storage | "{12.4 GB} stored" / "Review storage". Detail: "No personal limit. Everyone on this server shares its space." |
| Member, new work refused (`storage_paused`) | "The server is low on space, so new creations are paused. Your saved creations are still available." Next action: "Ask {Owner} to free space." |
| Owner Usage, disk Low | "Disk {91}% full. New creations pause at {95}%." |
| Owner Usage, disk Paused | "Disk nearly full. New creations are paused until you free space or add storage." |
| Owner Usage, disk Unknown | "Couldn't read free space, so new creations are paused." Action "Try again" |

"Stop sharing" no longer says "They lose access now": "now" overstated what
revocation can do (RT 7).

### 7.1 Changes to "Don't save this prompt" (UB §4.4)

| UB state | Old copy | New copy |
|---|---|---|
| Switch off (saving), note | "Saved with the {result}. Only you see it in your Library." | "Saved privately with the {result} in your Library." |
| Disclosure body | "… This doesn't hide your activity from the owner. …" | "Azizam uses your prompt to make this, then doesn't save it. The {result} and a record of the request (time, model, cost) stay in your account. {Owner} sees that record, not the {result}. The engine or provider that makes it follows its own rules. The {result} itself may contain words from your prompt." |

The UB §4.2 sentence "the owner and the engine or provider are outside this
promise" becomes: "the owner sees the request record, not the prompt; the
engine or provider is outside this promise". PM's viewer table gains a row,
"Share recipient: prompt only if included; never workflow download". PM's
line "Library visibility uses App access" becomes "Library visibility uses
`content_access`".

## 8. Data and migration

- **No content moves.** Visibility is computed by the core, so existing
  creations, prompts, chats, receipts and jobs are private to their maker (or
  the accountable person) from the first start. The `shares` table starts
  empty.
- **Removed** (P11, no aliases): the `scope`, `owner` and `person` parameters
  on `/me/*`; `"scope": "instance"`; `_audit_everyone` and its four actions;
  `receipt.detail.instance`; `recent_videos`, `recent_images`,
  `recent_renders`; the `admin` job and event audiences; the Library person
  picker and the Usage person picker outside the owner section; the
  `scope=everyone` web calls (`library/page.tsx:244-245`, `:468`, `:483`,
  `:948`, `:997`, `:1624`, `:1647`, `:2274`); signed URLs without `md` or `g`.
  Audit rows already written keep their labels
  (`web/src/lib/audit-description.ts:4-5`).

**The switch** uses AP's migration protocol (AP §4.2: stop, full backup,
migrate in place, verify, owner accept, one marker). It adds no second
protocol (RT 10).
1. **Preview** (read-only, on the stopped gateway's copy). It lists:
   - unrevoked keys whose `created_by` is not their App's `owner_user_id`,
     including NULL and `""` (§3.5);
   - records with no person and no App, in the three classes of §10.2
     question 2;
   - `superadmin` role rows;
   - creations and bytes per person, and free disk space against the
     reserve (§6.4).
2. **Owner approves.**
3. **Apply**: revoke the listed keys; map `superadmin` rows; attach or hold
   unattributed records (question 2); install the §3.5 triggers; create
   `shares`; write `visibility_version = 1`.
4. **Verify**: run the preview queries again and require zero listed keys
   and zero `superadmin` rows.

**Fail-closed start.** The gateway refuses to start in these cases:
- the marker shows the switch started but did not verify;
- `visibility_version = 1` and auth is disabled in config (until AP §2.8
  removes that mode).

Old signed URLs fail on their own: the signed message changed. Browser
sessions keep their person.

**Restore guard (RT 10).** This instance restores databases from file copies
(the main checkout has `data/media_jobs.db.bak.pre-r15` to `…pre-r18`). A
restored `media_jobs.db` would bring back shares that were stopped later.
- Every share write bumps `shares_revision` in `media_jobs.db` first, then in
  `admin.db`.
- At start, if the two differ, the gateway revokes every active share,
  writes the audit event `shares.revoked_after_restore`, and sets both
  revisions equal. Members share again.
- A crash between the two writes also triggers this. That is a false
  positive, and it fails closed.
- A restore of the whole `data/` folder restores both stores consistently.
  AP §4.2 governs that: restore is refused after accept, and a manual
  restore loses later changes. There is no separate revocation ledger
  (RT 10: "Do not create a complex ledger solely for household v1").

## 9. API, CLI and MCP first; then tests

### 9.1 Shape

| Operation | REST | CLI (HR Release B item 4) | MCP |
|---|---|---|---|
| List own or shared creations | `GET /me/api/media?view=mine\|shared-with-me&sort=recent\|size` | `azizam library list [--shared-with-me] [--sort size] --json` | Not supported. Explicit error per HR "Support matrix" |
| Share or update | `PUT /me/api/media/{asset_id}/shares/{person_id}` `{include_prompt}`. Idempotent; 201 or 200 | `azizam share add <asset> <person> [--include-prompt]` | Not supported |
| Stop (maker) or remove (recipient) | `DELETE /me/api/shares/{share_id}` | `azizam share rm <share_id>` | Not supported |
| List shares | `GET /me/api/shares?direction=by-me\|with-me` | `azizam share list` | Not supported |
| Share targets | `GET /me/api/people/share-targets`: enabled people, `id` and display name, owner flagged | (used by `share add`) | — |
| Own storage | `GET /me/api/storage` | `azizam storage` | Not supported |
| Owner usage | `GET /admin/api/usage?person=all\|<id>&from&to` (projection) | `azizam admin usage …` | Owner MCP deferred (HR "Later") |
| Owner request log | `GET /admin/api/activity/requests[/{id}]` (projection) | `azizam admin activity …` | Deferred |
| Owner storage | `GET /admin/api/storage` | `azizam admin storage` | Deferred |
| Storage reserve | `PUT /admin/api/settings/storage {reserve_percent}` | `azizam admin storage reserve <percent>` | Deferred |

Errors:
- `400 param_not_supported` for a removed parameter, in any form (§3.7).
- `404 not_found` for a record the caller has no access to. Existence is not
  revealed.
- `403 not_your_app` (§3.5).
- `409 share_not_allowed` with reason `several_makers`, `no_maker` or
  `file_type`.
- `403` "Invalid or expired signature" for a stopped, ungranted or old-format
  URL.
- `503 storage_paused` (layer gateway) when the disk is below the reserve.

There is no CLI in this repo today. The CLI rows depend on HR Release B
item 4.

### 9.2 Tests (the oracle)

1. **Action matrix, generated from §3.1.** For every kind × action × basis
   cell, a fixture viewer and record. It asserts allow or 404 and, for a
   mutation, that nothing changed (RT 5). Required negative fixtures (RT 1):
   - same App, different person: prompt and chat denied, creation allowed;
   - same person, different App: chat denied;
   - Shared: delete, reuse, share, `g=o` link and `md=full` denied;
   - Accountable: prompt and chat denied;
   - an App key with no person: prompt denied;
   - owner and admin roles: no content cell allowed by role alone.
2. **Paths.** Every entry the inventory classifies `content:*` or
   `operational` is called as: maker, another member, owner, admin (not
   owner), share recipient, recipient after stop, the maker's personal key,
   a service-App key, the service App's accountable person, signed-URL
   holder, and no auth. That covers L1-L8, `/gateway/v1/*`, `/v1/videos/*`,
   `/v1/responses/*`, voices, and every MCP tool and sub-operation. Each
   cell asserts 200 with full data, 200 with the projection, or 404. A
   non-owner `job cancel` leaves the job running.
3. **Inventory.** Every route (with HEAD), MCP sub-operation, dispatch
   callback and job event type is classified. Event types are a closed set.
4. **No-leak canary.** A member makes an image, a video, a chat with
   `store:true`, a failing MCP call, and a failing request whose provider
   error echoes the input. The canary `CANARY-7f3a` is in the prompt, the
   input file name, a direct engine access path, the provider error, file
   metadata, and the nested `cost` and `resource_decision` values where a
   fixture can place it. As owner and as admin, call every `operational` and
   `own-account` entry. No response, envelope, filter list or aggregate label
   contains the canary, the member's asset ids, `sig=` or a `/v1/assets/`
   URL. Also scan stdout, stderr and `cache/key_activity.jsonl`.
5. **Projection schema.** A recursive walk of every owner response checks
   that each leaf is a number, boolean, timestamp, opaque id, enum member or
   known display name. A new receipt field is absent until the schema lists
   it. A receipt whose only model is `requested_model` shows "Unknown model".
6. **Signed URLs.** The owner cannot mint a URL for a member's creation.
   Stopping a share makes its URL and its poster URL return 403 on the next
   fetch, including a Range request. A share URL for a different asset
   fails. Tampering with `g` breaks the signature. A share with `md=full`
   fails. URLs without `md` or `g`, and old-format URLs, fail. Stop then
   share again gives a new share id, and the old URL stays dead. A `g=s`
   response has `Cache-Control: private, no-store`. Every minting site
   needs an `Access` (a type check and one test per site). Metadata canary
   per container (§3.4).
7. **Sharing.** With `include_prompt=false`, no response to the recipient
   (list, detail, download, `q` search) contains the prompt, for both
   job-backed and cache-only items. After `include_prompt` goes from true to
   false, the prompt is gone on the next read. After the maker erases
   prompts, the recipient sees "Prompt not saved". A two-maker item returns
   `several_makers`. A GIF returns `file_type`. A `PUT` by a non-maker
   changes nothing. When the maker deletes, their shares are revoked and
   other owners keep the file.
8. **Keys and binding.** `403 not_your_app` for anyone but the accountable
   person, on personal and service Apps, on every issuance path in §3.5. The
   triggers refuse a `kind` change, a personal App owner change, and a
   service App owner change while a key is active. The switch revokes the
   keys the preview listed, and only those.
9. **Switch and restore.** Separate cases: first start; a switch
   interrupted after apply and before verify (start refused); auth-disabled
   config after the switch (start refused); `media_jobs.db` restored alone
   (all active shares revoked, audit written); `admin.db` restored alone
   (same). `?scope=`, `?scope=a&scope=b`, `?owner=` and `?person=` each
   return 400.
10. **Storage.** Per-person bytes match a fixture of single-owner,
    two-owner and service-App files. The owner response has no asset ids or
    names. Below the reserve, a generation request returns `503
    storage_paused` before any engine or provider call (a recording test
    double asserts no call). Downloads and deletion still work. An
    unreadable disk pauses new work. Hysteresis: no flapping at the
    threshold.
11. **Reset event.** A reset issued by the owner shows "with a reset link
    from {Owner}". A self-issued or unknown-issuer reset shows UB's plain
    event.
12. **Web (390 px, P4).** The owner's Library has no person picker. The share
    flow, stop sharing and "Shared with me" work by touch. The owner's Usage
    shows no thumbnails. "Review storage" selects and deletes by touch. The
    member first-use notice shows once.

Rewrite the §1.5 specs to these expectations. Do not delete them.

## 10. Risks and open questions

### 10.1 Risks

| Risk | Mitigation |
|---|---|
| Members read "private" as protection from the host | The honest-limits sentence names host files and resets, one step from every private claim. It never says "only you" |
| Metadata still reveals behavior (models, times, frequency) | Disclosed in Privacy. The projection keeps typed quantities only |
| A route or MCP operation added later skips the core | The inventory test fails CI for any unclassified entry |
| A future route moves a personal App and its history | `admin.db` triggers (§3.5) |
| Live bearer URLs after a share stops | `g` is checked on every fetch; downloaded copies cannot be recalled, and the copy says so |
| An owner who relied on Everyone loses visibility of a child's work | Plans prevent; supervision is deferred with a fixed direction (§2.3) |
| Logs hold exception text | §3.6 fixes the request-path sinks and ratchets the rest; logs are host files (L11) |
| Reverse-proxy logs hold signed URLs; LiteLLM logging defaults | Deployment gates, UNVERIFIED |
| A restore brings back stopped shares | Restore guard (§8) |
| Revoking the listed keys breaks an app that uses one | The preview names each key and its App. The accountable person makes a replacement before the switch |
| The disk pause starts on deploy | The preview reports free space against the reserve (§10.2 question 4) |
| The storage scan is slow on a large cache | `measured_at` and stale state; fall back to sizes in sidecars (§6.3) |
| Wide change: about 25 routes, MCP and three web surfaces | Build order (§12): core first with no behavior change, then the switch with the matrix test as gate |

### 10.2 Open owner questions (each has a default)

1. **Supervised accounts for children.** **Default: defer.** Plans prevent
   rather than watch. When needed, build the §2.3 direction: a separate,
   disclosed supervisor relationship that shares usage and spend, not
   content. RT agrees.
2. **Creations with no recorded person or App.** **Default (refined by
   RT):** the preview sorts them into three classes, and only the first is
   attached to anyone.
   - **Proven unbound**: no person, no App, no credential hash that matches
     any credential, and written through the keyless LAN path. Attached to
     one new service App, "No person recorded", with you as its accountable
     person. They appear in your Library, where you can keep or delete each
     one (P19).
   - **Resolvable**: a stored owner key or `maker_person_ids` maps them to
     an App or person through today's aliases (`ownership.py:46-55`). Not a
     new assignment; the core already reads them that way.
   - **Unresolved**: everything else. Nobody is granted access, nothing is
     deleted, and the bytes show under "Not linked to a person" in Storage
     (§6.1). They stay listed until you resolve them.
3. **Share expiry.** **Default: no expiry.** Ordinary shares (Immich,
   Nextcloud, OneDrive) do not expire. The badge keeps a share visible, and
   the maker can stop it at any time. RT agrees.
4. **Disk reserve (new).** **Default: pause new creations below 5% free.**
   The assignment says this disk is 97% full; that is UNVERIFIED. If it is
   true, new creations pause as soon as step 3 (§12) deploys. Free space
   first, or lower `storage.reserve_percent` knowingly.

## 11. Red-team resolution

Resolutions of RT (v1 at `4a020412`). "Applied" means the design text
changed; code evidence is in the cited section.

| RT | Finding | Resolution | Where |
|---|---|---|---|
| 1 (blocker) | The one core replaces resource-specific identity and action rules with an App intersection | **Applied.** Typed `ContentRecord` kinds and an action matrix inside the one module. Each kind keeps today's rule: App for creations (`ownership.py:46-55`), person for prompts (`me_media.py:656`), person and App for chats (`proxy.py:25808-25817`), principal folder for voices. Shared never satisfies a mutation. Identity entry for sessions, personal keys (`admin_runtime.py:1604-1614`), makers and person-less Apps stated. Negative tests added | §3.1, §9.2 item 1 |
| 2 (blocker) | Key issuance is not a complete boundary: App ownership can change; key provenance unknown | **Applied.** All issuance paths enumerated (rotate and import do not exist). Triggers fix App kind and personal ownership; a service App owner change needs all keys revoked. Transfer is a content transfer with disclosure. Keys with `created_by` not equal to the owner, NULL or `""` are revoked by the switch. Read aliases stay; revocation ends authentication. No current code changes App ownership (§1.3), so no exploit exists today | §3.5, §8, §9.2 item 8 |
| 3 | Shared output needs its own serializer | **Applied.** `shared_view()` typed projection, applied before title, search and filter; list built from `shares`, not the own-receipt filters; title never from the prompt | §4.2, §9.2 item 7 |
| 4 | The allowlist is not recursive; envelopes add fields back | **Applied.** Typed schema for every leaf, nested `cost` and `resource_decision` included; whole response projected; catalog names only, no `requested_model` fallback; recursive schema test and nested canaries | §3.3, §9.2 items 4-5 |
| 5 | Job events and MCP not covered by a route inventory | **Applied.** Inventory covers HEAD, MCP tools and sub-operations, dispatch callbacks and event types (closed set). Job events Own-only; admin job and event audiences deleted. Tests assert no side effects | §3.2, §9.2 items 1-3 |
| 6 | "No prompts in logs" is contradicted by an exception sink | **Applied.** Claim withdrawn (74 `{exc}` prints found, not only MCP). MCP sink fixed; request-path sinks fixed; ratchet on the rest; exception-path canaries; LiteLLM and Traefik are deployment gates | §1.1, §3.6 |
| 7 | Signed grants need a byte, derivative and cache contract | **Applied.** Checks before any read and before Range; poster bound to the share; `private, no-store` for shares; GIF refused; metadata canary per container; "now" scoped to the next fetch and the copy changed. Code facts: no HEAD route, ETag, 304 or server cache exists today (`media_delivery.py:194-234`) | §3.4, §7, §9.2 item 6 |
| 8 | Several makers, partial deletion and the share key do not compose | **Applied, simplest option.** Sharing refuses items with more than one maker; the row's maker is authoritative for prompt, update and stop; deletion revokes that maker's shares and keeps other owners' bytes | §4.1, §9.2 item 7 |
| 9 | Password reset is an existing impersonation path | **Applied.** Privacy page names it; the Security event joins the existing `account_links.created_by` (no new field). **Rejected for now:** verified-channel delivery, because Azizam has no mail; the CRM mail module can add a notice | §5, §7 |
| 10 | Migration and restore need executable fail-closed gates | **Applied.** One presence-based validator for removed parameters; the switch reuses AP §4.2 with a verify step; start refused on a partial switch or auth-disabled config; `shares_revision` in two stores revokes all active shares after a single-store restore. No revocation ledger | §3.7, §8, §9.2 item 9 |
| 11 (minor) | Keep honest accounting and safe failure evidence | **Applied.** Projection keeps the submission-time spending subject, cost quality, source and reconciliation, coverage fields and attempt facts; unknown layer has a next action; no raw-error bypass | §3.3 |
| Default 1 | Supervised accounts: defer | Kept. SR's minimal shape recorded as the direction | §2.3, §10.2 |
| Default 2 | Unattributed creations: conditional | **Refined**: three preview classes; only proven-unbound records are attached; unresolved ones stay ungranted | §10.2 |
| Default 3 | Share expiry: none | Kept | §10.2 |

## 12. Build order

Each step ships on its own, keeps the full test suite green, and makes no
privacy claim before its enforcement exists. Steps 1-2 also need AP's
migration tool (AP §4.2); if AP has not landed, step 2 adds only the marker
and verify steps it uses.

| Step | Ships | Gate |
|---|---|---|
| **1. One core, no behavior change** | `content_access` with kinds, the action matrix and `Viewer`. The five checks in §1.4 call it. The projection (§3.3) and its schema test exist but nothing serves them yet. The inventory test, with today's routes classified | The current suite passes unchanged (behavior preserved). The matrix test passes for every cell that matches today's behavior |
| **2. Private by default** | Every read path uses the core and the projection (§3.2), including MCP and job events. Removed parameters fail (§3.7). Signed URLs carry `g` (§3.4). Key and App-binding rules, triggers (§3.5). Log sinks (§3.6). The switch with preview, verify and fail-closed start (§8). The web Library loses Everyone and the person picker; the owner's per-person usage moves to the Usage owner section. Member notice and Privacy page (§7) | The full matrix test, inventory, canary and schema tests. Owner approves the preview. LiteLLM and reverse-proxy logging checked on the deployment |
| **3. Storage** | Owner storage in Usage (§6.1), member storage and "Review storage" (§6.2), accounting (§6.3), the disk reserve pause (§6.4) | Storage tests (§9.2 item 10); 390 px journeys. Owner answers question 4 |
| **4. Sharing v1** | `shares` table, `shared_view`, share sheet, "Shared with me", Recent entry, restore guard (§4, §8) | Sharing, signed-URL and restore tests (§9.2 items 6, 7, 9) |
| **5. Documents** | PM, UB §2.3, §3.4 and §4, DATA_RETENTION (shares class (a)), and the AP §2.4 amendment | Cross-references checked |

Notes:
- The Accountable basis reads a service App's `owner_user_id`. Today service
  Apps belong to service accounts; AP's migration gives them a person. Before
  AP lands, Accountable matches nothing, and service App work stays visible
  only through its keys, as today.
- The §6.4 disk pause does not depend on steps 1-2. It can ship first if
  the disk needs it now.
- Until step 2 ships, R18 finding 2 can be closed by a one-line web change:
  the owner's Library starts at "mine". That is optional; step 2 removes
  the view.
- Step 2 is the largest. It is one step because each half alone is false:
  hiding the Library while keys still read member Apps would make the notice
  untrue.

## 13. Before building (P20)

- [x] Non-maker red-team of v1 (RT). Resolutions in §11.
- [ ] Red-team v2's changed areas: §3.1 matrix, §3.5 triggers, §3.4
      delivery checks, §6, §8 restore guard.
- [ ] Owner answers or accepts the defaults in §10.2.
- [ ] Record the go decision against the reviewed version.
- [ ] Follow §12.

## Change log

- v2, 2026-10-11. Resolves RT (§11). Also:
  - Added §6 Storage from ST: per-person storage in the owner's Usage, the
    member's own storage and largest-first review, no quotas, a disk reserve
    pause. New open question 4.
  - §2.3 records SR's direction: a separate supervisor relationship layer,
    coarse usage and spend, no content.
  - Corrected v1 facts: logs (§1.1); the reset issuer is already recorded
    (`account_links.created_by`), so no `issued_by` field is added; posters
    are separate assets; reset links last 7 days today.
  - Sections renumbered: Storage is §6; wording §7; data §8; API and tests
    §9; risks §10; red-team §11; build order §12.
- v1, 2026-10-11. First draft, then one non-maker review pass (11 findings
  applied: App-level rule 1 with L9 closed at key creation; accountable-only
  key creation; rule 2 as an AP §2.4 amendment; status sections hidden from
  non-owner admins; `terminal_reason` kept and `path` dropped; reset notice
  folded into UB §3.4; required `md` and `g`; cross-references; VOICE copy;
  cuts).

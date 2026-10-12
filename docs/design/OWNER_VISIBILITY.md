# Owner visibility: private member work, an operational owner view

Status: design, v3, 2026-10-11. Not built. P20 requires a recorded go
decision on this version before the first implementation commit. v2 resolved
the red-team of v1 (`4a020412`, §11.1) and added per-person storage (§6). v3
resolves the second red-team of v2 (`81ce3204`, §11.2), mostly by removing
machinery: one App-ownership rule instead of two bases, no two-store restore
guard, and an absolute disk reserve instead of a percentage. §2.4 states the
App-ownership model that this design and AP share.
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
- **RT2**: red-team of v2, `…/azizam-redteam2-owner-visibility-81ce320-r0.md`
  (2 blockers, 6 major, 1 minor). RT2 Bn, Mn, N1 means its finding.
- **TD**: authorization test design of v2,
  `…/azizam-testdesign-owner-visibility-81ce320.md`. Adopted as the test
  section: `docs/design/OWNER_VISIBILITY_TESTS.md`, adjusted for v3 (§9.2).
- **AP**: `design/access-plans:docs/design/ACCESS_PLANS.md` v3 at `b81a9a5c`
  (plans, the accountable person, the migration protocol §4.2). AP is being
  revised in parallel to remove its per-request permit machinery
  (`access_epoch`, Permit, `check`). v3 of this design depends on none of
  it. Both designs cite §2.4 here for App ownership.
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
  sub-agents and may be off by a few lines. For v3 I re-read every RT2
  citation at `8a3147f0`: `admin_store.py:117-188`, `:343-387`, `:584-592`,
  `:1225-1260`, `:1469-1477`; `admin_runtime.py:333-367`, `:567-601`,
  `:1595-1720`; `mcp_server.py:89-103`, `:214-243`, `:312-330`, `:373-411`,
  `:454-531`; `proxy.py:6930-6937`, `:14612-14668`, `:23625-23660`;
  `me_media.py:221-269`, `:397-426`, `:440-505`, `:819-837`, `:861-877`;
  `stt_chunking.py:560-595`; `accounts.py:340-350`; `user_runtime.py:453-459`.
- Disk, measured 2026-10-11 with `df -B1` on the filesystem that holds the
  checkout (`/dev/nvme0n1p5`, mounted at `/`): 1,451,030,417,408 bytes,
  79,473,270,784 available, 95% used. Whether `data/` and the creations
  folder are on this filesystem is UNVERIFIED (not inspected, by rule).

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
| Every credential records `created_by` (NULL or `""` when unknown). A back-fill sets `""` when the audit trail does not prove the owner made it. It runs at every start | `admin_store.py:162`, `:343`, `:346-387` |
| `admin.db` already enforces identity rules with triggers | `admin_store.py:176`, `:182-185` |
| A service App must be owned by a service account, and an App cannot have both owners. So a service App with a person as owner cannot be built today | `admin_store.create_app`, `admin_store.py:1238-1243` |
| The admin create route turns any App with an `owner_user_id` into a `personal` App | `admin_runtime.py:344-355` |
| "Apps a person owns" selects on `owner_user_id` for both kinds. Eight call sites use it, including reuse (`me_make.py:449`) and the Library scope (`me_media.py:453-458`) | `admin_store.py:1469-1477` |
| Admin key creation sets `created_by` to the acting person, or `""` for a service-account actor. Self-service, Try and invite keys set it to the App's owner | `admin_runtime.py:585`; `user_runtime.py:459`; `accounts.py:346-347` |
| Authentication keeps no state between requests: every request reads the credential and its App again, in autocommit | `admin_store.py:584-592`; `admin_runtime.py:1640-1676` |
| MCP reads input assets and stores result bytes through two direct callbacks, not through a REST route | `mcp_server.py:89-99`; `proxy.py:14636-14652` |
| Library delete hides the item for the deleting person (an audit tombstone) and removes bytes only when the file has one owner and at most one maker. It never removes an owner from the sidecar, so a file that two people made stays after both delete it | `me_media.py:251-266`, `:397-426`, `:861-877` |

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
- `web/e2e/global-setup.ts:60-68`, `tests/e2e/contracts.py:144-145` (key
  minting by an admin automation key, §3.5)

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
| Accounts, plans, keys | Own; keys only for Apps they are accountable for (§2.4 O4) | No | Invite, plan, disable, revoke, reset; keys only for Apps the owner is accountable for | As today, except keys |
| Audit | Own sharing list; Security events, including owner-issued resets (§5) | No | Configuration and access records, with no content | As today |

The accountable person of a **service** App owns what that App made, as they
own what their personal Apps made (§2.4 O6). That is App content, not member
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

### 2.4 App ownership: the shared model (cited by AP)

This section is the single statement of App ownership for this design and
for AP. Each rule has an id so that both designs can cite it. `admin.db`
triggers enforce the data shape of O1-O4 (§3.5); the issuance routes enforce
who may call them (§3.5 item 1); `person_for_app_credential` computes O5.
The ownership cut-over (§8.1) makes existing data obey them before any
trigger is installed; a new database gets the triggers with its schema.

| Rule | Statement |
|---|---|
| **O1** One accountable person | Every App has exactly one accountable person, `apps.owner_user_id`, for both kinds. It is never NULL after the cut-over. Service accounts own no App: `owner_service_account_id` is always NULL (AP may drop the column). A service account is a control-plane identity only |
| **O2** Fixed kind | `kind` is `personal` or `service`, chosen at creation, and never changes. Session Apps and Apps a member makes for themselves are `personal` (`admin_store.py:1305`, `user_runtime.py:560-566`); the Try App's kind is UNVERIFIED and the cut-over preview lists it. A personal App's accountable person never changes |
| **O3** Transfer | A service App's accountable person changes only through a transfer. A transfer needs zero unrevoked keys on the App, and it shows the new person that they will see everything the App made, including before today. v1 has no transfer route |
| **O4** Issuance | Only the accountable person issues an App's keys. At insert, a key's `created_by` equals its App's `owner_user_id`. A key's App, subject and `created_by` never change |
| **O5** Key identity | A key acts for its App. It carries a person only when its App is `personal` and its creator is the owner (`person_for_app_credential`, unchanged). By O4 that is every live personal key. A service App key never carries a person |
| **O6** Content | A person owns (basis Own, §3.1) what every App they are accountable for made, of both kinds. A key owns what its own App made. Prompts and saved chats follow the person who wrote them (Self), never the App |
| **O7** Policy and spending (AP) | A request runs only if both the accountable person's plan and the App's assignment allow it. Spend is attributed to the accountable person **at submission** and is never rewritten by a later transfer (AP §2.4, §2.11) |
| **O8** Missing person | If the accountable person is missing or disabled, the App's requests are denied (AP §2.4) |

O6 amends AP §2.4, which says the accountable person is "only the policy
and spending subject". It still does not make a key act as that person (O5):
daisy's key does not read Tim's prompts. The amendment runs the other way:
Tim, as daisy's accountable person, can find, use and delete what daisy
made. Someone must be able to do that in the product (P19), and the
accountable person already controls the App's keys (O4), which read all of
it.

**Linearization.** Authorization is decided per request from the rows it
reads; nothing is cached across requests (`admin_store.py:584-592`). A
change to keys or ownership applies to every request that starts after it
commits. A request already running finishes under the rows it read. Long
operations re-authenticate at each step: every MCP poll is a new internal
request (`proxy.py:14620-14626`), and each job-event reconnect is a new
request. No per-request permit or epoch is needed, so this design does not
depend on AP's `access_epoch`.

## 3. Enforcement: one core with typed resources

### 3.1 The module

`gateway/content_access.py` replaces the five ownership checks in §1.4 and
the owner branches in L1-L7. v1 proposed one universal "Own" rule. RT 1
showed that this drops real distinctions: an App key that may read a
creation must not read its maker's prompt or chat, and a share must not
satisfy delete. v2 kept **one module and one decision function**, but the
decision takes the resource kind and the action. Each kind keeps today's
identity rule (§1.3). v3 removes v2's separate "Accountable" basis (RT2 M1,
below).

```python
@dataclass(frozen=True)
class Viewer:                     # built once per request from trusted auth
    person_id: str | None         # session person; for a key, person_for_app_credential()
    app_id: str | None            # the App the request authenticates as
    key_hash: str | None          # set only for an appless key (chat rule)
    owner_keys: frozenset[str]    # readable principals (ownership.readable_owner_keys) of:
                                  #   a key: its own App;
                                  #   a session: every App the person is accountable for (§2.4 O6),
                                  #   plus user:<person_id>, as today (me_media.py:826)
    is_owner: bool                # role == "owner"; read only by operational()

Kind   = Literal["creation", "prompt", "chat", "voice", "receipt", "job"]
Action = Literal["read", "delete", "reuse", "share", "link", "cancel"]
Access = Own | Shared(share_id, include_prompt) | Deny

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
  App-level rule, with §2.4 O6 saying which Apps a session owns.
- **Self**: `record.person_id == viewer.person_id`, both non-null. An App
  never satisfies Self.
- **Self+App**: today's chat rule, unchanged.
- **Maker**: Own, **and** `maker_person_ids == [viewer.person_id]` (exactly
  one maker).
- **Shared**: an active `shares` row for this `asset_id` (or its
  `poster_asset_id`) and `viewer.person_id` (§4).

**Why there is no "Accountable" basis (RT2 M1).** v2 gave a service App's
accountable person a narrower basis than Own (no reuse). But the session
Viewer already included every App the person owns, so the accountable person
also matched Own, and the narrower basis did nothing. Two fixes were
possible: separate the two sets in `Viewer`, or state one rule. v3 states one
rule (§2.4 O6). The narrower basis protected nothing: Accountable could read
and download the bytes, so denying reuse only forced a download and
re-upload. Prompts and chats never follow the App, so the rule gives the
accountable person no prompt or chat of anyone (Self, Self+App). A service
App's records carry no maker, because its keys carry no person (O5) and
sessions act only through personal Apps (AP §2.4). Compared with v2, the
accountable person gains reuse, voices and job events of the service App.
Member copy discloses it (§7).

**Precedence.** `decide` tries Own first, then Shared. Own is a superset of
Shared for every creation action, so a viewer who has both gets Own and the
own serializer. A share never selects a prompt: prompt reads are a separate
`prompt` decision (Self, or the share's maker row when `include_prompt`).

**The action matrix.** A cell lists the bases that allow the action. A blank
cell is Deny. This table is the whole policy. The test oracle in §9.2 is
written from it independently; it does not call `decide`.

| Kind | read | delete | reuse (input, "Use again", workflow download, `md=full`) | share | link (mint a signed URL) | cancel |
|---|---|---|---|---|---|---|
| creation | Own, Shared | Own | Own | Maker | Own → `g=o`; Shared → `g=s` | |
| prompt | Self; Shared with `include_prompt` reads the **share's maker's** row | Self | Self (`md=full` also needs Self, §3.4) | | | |
| chat | Self+App | Self+App | Self+App | | | |
| voice | Own | Own | Own | | | |
| receipt | Own | | | | | |
| job (and its events) | Own | | | | | Own |

Rules that follow from the matrix:
- **Role is never an input.** `is_owner` feeds only `operational()`.
- **Shared never satisfies a mutation**, `reuse`, `share` or an `o` link.
- **Prompts and chats never follow the App.** A key on a member's App reads
  that App's creations (Own) but not the member's prompts (Self) or chats
  (Self+App). Today's code already behaves this way: an admin-made key has
  no person (§1.3).
- **Receipt deletion is not an action.** Retention is an owner setting with
  preview (P19, DATA_RETENTION).
- **Share refusal order.** A viewer who cannot read the creation gets
  `404 not_found` before any share diagnostic, so a refusal never reveals
  that an item exists. A viewer who can read it gets `409
  share_not_allowed` with its reason (§4.1).
- Requests with no identity get Deny.

**How identity enters `Viewer`.**
- Browser session: `person_id` is the session person; `app_id` is the
  surface App (AP §2.4: session App, Home's personal App, Try's App).
  `owner_keys` comes from one query of the Apps whose `owner_user_id` is the
  person, plus the `user:<person_id>` principal that today's scope already
  includes (`me_media.py:819-837`). That replaces the `list_apps_for_user`
  callers that decide content (`me_media.py:453-458`, `me_make.py:449`,
  `home_api.py:351`, `user_runtime.py:319`), so they cannot drift apart. The
  other callers (`proxy.py:2018`, `:17098`; `user_runtime.py:281`, `:502`)
  list Apps for management and will also list service Apps after the
  cut-over; that is intended (O1).
- App key: `app_id` is the key's App; `person_id` comes from
  `person_for_app_credential`, so only a personal App's key made by its owner
  has a person (O5). The credential and its App are read in two queries
  (`admin_store.py:1663-1672`, then `admin_runtime.py:1675`). That is safe
  without a shared snapshot: a key's App and creator never change (O4), a
  personal App's kind and person never change (O2), and a service App key
  carries no person, so no interleaving of commits can give a request a
  person or an App it did not have at one instant.
- Maker: the request's person at write time, stored in `maker_person_ids`. A
  creation made through a key with no person has no maker and cannot be
  shared.
- Control-plane `service_account` credentials own no content and never reach
  a content route (runtime auth rejects them, `admin_runtime.py:1648-1649`).
  The cut-over preview counts records whose principal is `service_account:…`;
  any it finds are Unresolved (§10.2 question 2), granted to nobody.
- Auth-disabled mode (`proxy.py:14162`, `unrestricted=True`) is removed by
  AP §2.8. Until AP lands, startup refuses auth-disabled config once the
  visibility switch has run (§8.2).

This is a consolidation, not new machinery. The stored data shape is
unchanged. The only new input is the `shares` table.

### 3.2 Every read path uses it

| Path (from §1.2) | Change |
|---|---|
| L1 `/me/api/media`, `/media/{id}` | Always the caller's Own and Shared items. `view=mine` (default) or `view=shared-with-me`. `scope`, `owner` and `person` are removed (§3.7). `_MediaScope.app_ids=None` and `audit_everyone` are deleted |
| L2 download, posters | `require(viewer, creation, "link")`, then mint a URL with that access (§3.4) |
| L3 signed URLs | §3.4 |
| L4 `/me/api/receipts/{id}` | Own only; 404 for every other role. The owner reads others at `GET /admin/api/activity/requests/{id}` as the projection. `receipt.detail.instance` audit is deleted |
| L5 activity, home, status, catalog, workflows | `/me/*` is always the caller's own. The owner branch at `user_runtime.py:350-361` and the `"scope": "instance"` label are deleted |
| L6 Usage | `/me/api/usage/summary` is own only. The owner's per-person usage and storage move to `GET /admin/api/usage` and `GET /admin/api/storage`, in the projection |
| L7 request log | Built entirely by `project()` (§3.3), including the envelope. New `GET /admin/api/activity/requests/{id}` |
| L8 status | Delete `recent_videos`, `recent_images` and `recent_renders` (`status_endpoint.py:3960-3963`). `_live_video_jobs` uses the job projection. `key_name` on live jobs, `auth_keys`, per-key request rates and `recent_events` are owner-only |
| L9 keys on a member's App | Refused at creation, by the route and by the database (§2.4 O4, §3.5); existing ones revoked by the cut-over (§8.1) |
| Job events | Own, as for the job itself (RT2 M1 removed the conflict), on every surface. The `admin` job audience (`media_jobs.py:276-287`, a denylist that still includes `assets` and `error`) and the admin event audience (`:1142-1157`) are deleted. No owner event view exists in v1 (RT 5). Per-job event sequence numbers stay; household-wide row ids stay hidden (`:1164`) |
| `/gateway/v1/*`, `/v1/videos/*`, `/v1/responses/*`, voices | Call `decide` with the kind and action. Behavior stays the same, by construction: each kind's basis is today's rule |
| Job content (`/gateway/v1/media/jobs/{id}/content`, `/v1/videos/{id}/content`) | `require(job, "read")`, **and** `require(creation, "link")` for each output before its bytes or URL. An owned job does not vouch for an asset it names |
| MCP (`/mcp`) | Table below: every `McpDeps` callback, not only `call` (RT2 B2) |

**MCP callbacks (RT2 B2).** v2 said every MCP operation goes through REST.
That is false: two callbacks touch assets directly, and two read the catalog
directly (`mcp_server.py:89-99`, wired at `proxy.py:14658-14668`). Each
callback has one treatment:

| `McpDeps` callback | Today | v3 |
|---|---|---|
| `authenticate` | `authenticate_request` (`proxy.py:14659`) | Builds the `Viewer`. Every other callback receives that `Viewer`, never a raw `auth` that may be `None` |
| `call` | In-process REST with the caller's headers (`proxy.py:14620-14626`) | Unchanged. Each call re-authenticates, so the REST route's `decide` applies. Covers `capabilities` speech voices (`mcp_server.py:324`), `run` submit, `job` poll, wait, content and cancel |
| `read_asset` (run inputs, `workflow_call.py:131-148`) | `_asset_access_allowed`: App-owner intersection; with no owners it allows a caller with no identity (`proxy.py:6930-6937`, `:14647-14652`) | `require(viewer, creation, "reuse")` before the file is opened. Reuse is Own only, so a share, a guessed id or another person's asset is refused with the same answer as a missing asset. The no-identity branch is deleted |
| `store_asset` (results, `mcp_server.py:227`, `:242`) | Stores with the caller's principal, mints with `build_signed_asset_url` (`proxy.py:14636-14644`) | Stores with the caller's principal and maker. Mints with `Access=Own`, which holds by construction: the caller's principal was just written. Same minting rule as every other site (§3.4) |
| `catalog`, `aliases` (discovery, unknown-target suggestions) | Workflow registry and alias config (`proxy.py:14629-14633`) | Classified `none`: they hold operator configuration, not member content. The two-world test (§9.2) proves that discovery output does not change when private data changes. Whether the registry can ever hold a member-made workflow is UNVERIFIED; if it can, discovery moves to `call` |
| `public_base_url`, `allowed_origins` | Config | `none` |

The result URLs that `_assets_from_response` forwards from a core response
(`mcp_server.py:229-231`) are minted by the core route, which needs an
`Access` (§3.4). The adapter mints nothing on that branch, and a forged URL
fails its signature.

**MCP cancel (RT2 N1).** `job` with `cancel` polls after cancelling, and a
successful poll fetches content (`mcp_server.py:494-500`). v3 returns the
cancel result on its own: `{status: "cancel_requested", job_id}` when the
cancel call succeeds. A later poll or content failure does not turn a
successful cancel into an error.

**Inventory (RT 5).** One test lists every entry point and fails CI for an
unclassified one:
- every FastAPI method and route, including HEAD;
- every MCP tool and sub-operation, and **every `McpDeps` callback**;
- every job event type, which must be in a closed set (`media_jobs.py:1023-1026`
  accepts any non-empty string today).

Each entry is `content:<kind>:<action>`, `operational`, `own-account` or
`none`. Classification is a guardrail. The matrix test (§9.2) proves
behavior.

**The owner's Library "Everyone" view is removed, not narrowed.** The owner's
Library is the same member Library (P8): what their Apps made, personal and
service (O6), and "Shared with me". This also resolves R18 finding
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
required.** `g` is `o` (Own) or `s.<share_id>`.

Delivery (`_signed_asset_content_response`, `proxy.py:23563`) runs these
checks **before it reads any bytes** (today the read is at `:23629`):
1. Verify the signature. A URL without `md` or `g` fails with 403, the same
   message as a bad signature. No shim (P11).
2. If `g=s.<id>`: the share is active, and the asset is the share's
   `asset_id` or its `poster_asset_id` (RT 7: the poster is a separate
   asset).
3. A share grant never allows `md=full`. For `g=o`, `md=full` is not a
   bearer capability: delivery authenticates the **fetcher** and restores
   that person's own kept prompt (`proxy.py:23630-23647`, today's rule,
   kept). Two makers of one file each get their own prompt, and a fetcher
   with no person gets 403 `prompt_not_accessible`. The signature carries no
   person and needs none.
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
is minted only for Own access. Revoking a key does not revoke a `g=o` URL
already minted with it; the URL expires within the hour.

**Metadata stripping is tested, not assumed.** Stored bytes are stripped on
write (`proxy.py:7027`) and stripped again on delivery unless `md=full`. The
test puts a canary in the metadata of every container the stripper handles
(`media_metadata.py:467-500`): PNG, JPEG, WebP, WAV, FLAC, MP3, MP4, WebM,
MKV, OGG and M4A. It asserts that a `g=s` fetch does not contain the canary.

Every minting site takes an `Access`, so a URL cannot be minted without one:
- `build_signed_asset_url` (`proxy.py:7172`) and `build_signed_image_url`
  (`:7186`);
- MCP results (`_mcp_store_asset`, `:14636`), with `Access=Own` (§3.2);
- generation responses on `/v1/images/*` and `/v1/videos/*` (the caller is
  the maker: `o`);
- the Library download route, after `require(…, "link")`; a poster is minted
  with its parent's access.

### 3.5 Who can create a key, and what can change an App

A key reads everything its App made (basis Own). Creating a key is therefore
a content grant. RT 2 showed that key creation is not the only way to get an
App's principal: changing an App's owner or kind would do it too. RT2 M2
showed that v2's triggers had gaps (A→NULL→B, an unfenced first
assignment, and a route check that is not atomic with the insert). v3
enforces §2.4 O1-O4 in `admin.db`, so no route can skip them.

**1. Only the App's accountable person creates keys for it (O4).** These are
every issuance path (sub-agent inventory; rotate and import do not exist):

| Path | Today | v3 |
|---|---|---|
| `POST /admin/api/apps/{id}/keys` (`admin_api.py:2251`) | Any admin, any App; `created_by` is the actor, or `""` for a service-account actor (`admin_runtime.py:585`) | `403 not_your_app` unless the caller is the App's `owner_user_id`. Service-account actors cannot issue App keys |
| `POST /me/api/apps/{id}/keys` (`proxy.py:16097`) | Own App only (`user_runtime.py:140`, `created_by` at `:459`) | Unchanged. After the cut-over "own App" includes service Apps the person is accountable for |
| `POST /me/api/try-key` (`proxy.py:16129`) | Own Try App, session-bound | Unchanged |
| Invite acceptance (`accounts.py:346-347`) | The new person's own App, `created_by` the person | Unchanged |
| `POST /setup/first-app` (`proxy.py:17783`) | Person branch uses the own-App path; other branch creates a new service App and its key (`:17849-17856`) | The new App's accountable person is the caller (O1), so the key passes the O4 trigger |
| Test setup: `web/e2e/global-setup.ts:60-68`, `tests/e2e/contracts.py:144-145` | Mint keys on a personal App with an admin automation key (`created_by` `""`) | Refused. The setups sign in as the App's person and use the self-service path; added to the §1.5 rewrite list |
| `POST /admin/api/admin-keys` (`admin_api.py:2280`), `generate-admin-key` (`proxy.py:2716`) | Control-plane `service_account` credentials (`admin_runtime.py:621`, `:1773`) | Unchanged. Their principal `service_account:<id>` owns no content. With role `owner` they get `operational()` only |

**2. App creation obeys O1 and O2.** Both create paths change with the
cut-over (§8.1):
- `admin_store.create_app` requires `owner_user_id` for both kinds and
  refuses `owner_service_account_id` (today a service App requires a service
  account, `admin_store.py:1238-1243`).
- `admin_runtime.create_app` keeps the requested kind. It no longer turns an
  App with an owner into a personal App (`admin_runtime.py:344-355`). A
  service App's accountable person is the caller unless the owner names
  another person.

**3. The database enforces the data shape of O1-O4.** Triggers in the
existing style (`admin_store.py:176-188`). They cannot know who called: a
route that writes the owner's id as `created_by` passes, so the route check
in item 1 is the gate for "who issues", and the trigger guarantees that no
key exists whose recorded creator is not its App's person. Every comparison
is NULL-safe (`IS`, `IS NOT`), because `!=` is never true when one side is
NULL. App ids are read as `COALESCE(NULLIF(app_id, ''), subject_id)`, as the
runtime does (`admin_store.py:1670`). The sketch is pseudo-SQL:

```sql
-- O1: an App always has a person and never a service-account owner.
BEFORE INSERT ON apps WHEN NEW.owner_user_id IS NULL
                        OR NEW.owner_service_account_id IS NOT NULL      -> ABORT
BEFORE UPDATE OF owner_user_id, owner_service_account_id ON apps
  WHEN NEW.owner_user_id IS NULL
    OR NEW.owner_service_account_id IS NOT NULL                          -> ABORT
-- O2: kind never changes; a personal App's person never changes.
BEFORE UPDATE OF kind ON apps WHEN NEW.kind IS NOT OLD.kind              -> ABORT
BEFORE UPDATE OF owner_user_id ON apps
  WHEN OLD.kind = 'personal' AND NEW.owner_user_id IS NOT OLD.owner_user_id -> ABORT
-- O3: a service App changes person only with no unrevoked key.
BEFORE UPDATE OF owner_user_id ON apps
  WHEN NEW.owner_user_id IS NOT OLD.owner_user_id
   AND EXISTS (SELECT 1 FROM credentials
               WHERE COALESCE(NULLIF(app_id, ''), subject_id) = OLD.id
                 AND subject_kind = 'app' AND revoked_at IS NULL)        -> ABORT
-- O4: a key is issued by its App's person, and its binding never changes.
BEFORE INSERT ON credentials WHEN NEW.subject_kind = 'app' AND (
    NEW.created_by IS NULL
 OR NEW.created_by IS NOT (SELECT owner_user_id FROM apps
                           WHERE id = COALESCE(NULLIF(NEW.app_id, ''), NEW.subject_id))) -> ABORT
BEFORE UPDATE OF subject_kind, subject_id, app_id, created_by ON credentials
  WHEN NEW.subject_kind IS NOT OLD.subject_kind OR NEW.subject_id IS NOT OLD.subject_id
    OR NEW.app_id IS NOT OLD.app_id OR NEW.created_by IS NOT OLD.created_by -> ABORT
-- No App row is ever deleted (AP §2.4), so delete-and-reinsert cannot reset O2 or O3.
BEFORE DELETE ON apps                                                    -> ABORT
```

Consequences:
- **No A→NULL→B path**: NULL is refused on every write (O1).
- **First assignment is fenced**: after the triggers exist, no App has a
  NULL person, so there is no "first assignment" left. The only first
  assignment is the reviewed cut-over (§8.1), which runs before the triggers
  are installed and shows each person what they will own.
- **Issuance is atomic**: the O4 trigger runs inside the `INSERT`, so the
  check and the write cannot be split by a concurrent change. An expired key
  that is not revoked still counts as live for O3, so a transfer revokes it
  first.
- **The `created_by` back-fill is deleted** (`admin_store.py:343-387`). The
  cut-over runs it one last time, before it revokes keys, so provenance is
  decided on the same evidence as today. After that the O4 update trigger
  would refuse it.
- The cut-over installs the triggers with `DROP TRIGGER IF EXISTS` then
  `CREATE TRIGGER`, so an older definition is replaced, and its verify step
  compares the stored SQL text in `sqlite_master`.
- **A new database** gets the triggers from its base schema and writes
  `ownership_version = 1` at creation, while `apps` is empty. The cut-over
  is only for databases that already hold Apps; test databases start
  enforced.

**4. Service App transfer is a content transfer (O3).** v1 has no transfer
route. When one is built, it is one `admin.db` transaction that revokes every
key of the App and then changes `owner_user_id`. Before confirming it shows
the new person: "You'll see everything {App} made, including before today."
It is audited. Past spend stays with the person who was the spending subject
at submission (§3.3, O7). Requests that started before the commit finish
(§2.4 Linearization); every later request sees the new person.

**5. Keys of unknown or other provenance fail closed.** The cut-over (§8.1)
revokes every unrevoked App key whose `created_by IS NOT` its App's new
`owner_user_id`. That includes NULL, `""`, and keys an admin made on a
member's App (L9). Nothing infers provenance from receipts. An App that has
been used recently and would lose all its keys blocks apply until the owner
makes a replacement key or approves that the App stops (§8.1).

**6. Revocation ends authentication.** Historical read aliases
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
  their shares of it are revoked in the same step (§6.3, "Deletion releases
  a claim"). Bytes may stay on disk for other owners; those owners' access
  does not change.
- **One store, one transaction.** A share write is one SQLite transaction in
  `media_jobs.db`. The `shares_active` unique index serializes two writers
  for the same pair. v2's second revision counter in `admin.db` is removed
  (§8.3, RT2 M4).

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
- **It ships with the privacy switch (§12 step 3), not with UB (RT2 M6).**
  No Security event exists today: "Password changed" is in neither
  `web/src` nor `gateway`. The honest-limits sentence points to it, so the
  event must be live when the sentence is. Step 3 adds the smallest form:
  `GET /me/api/security/events` returns the caller's redeemed reset links
  (`account_links` rows with `kind='reset'`, `used_at` set, `user_id` the
  caller), each with its time and, when `created_by` is someone else, that
  person's display name. Account > Security lists them. UB §3.4 later adds
  its other events to the same list.

Rejected for v1: recovery delivered to the member through a verified
channel, with notices to the prior channel. Azizam sends no mail (UB §3.1),
so no independent channel exists. The CRM mail module can add a notice to
the member's address when it ships. Until then the owner's ability to take
over an account is a stated trust limit.

Moderation in v1 does not browse content (R "Moderation" 1-2):
- **Prevent** with plans: grants, blocked categories, caps (AP).
- **Stop** by disabling the person or revoking keys.
- **Report** by a member sharing a creation with the owner.

**Limit in v1 (RT2 N1): the owner cannot cancel someone else's job.** Job
cancel is Own (§3.1). An owner-initiated cancel without content is an
operational action with its own audit reason; it belongs to the owner "Now"
view (HR Release C item 2), which this design lists as a dependency, not a
deliverable. Until then the owner stops a runaway job's source by disabling
the person or revoking the key; whether that also stops a job already
running is UNVERIFIED. The fix is never to give the owner job content.

## 6. Storage

ST recommends per-person storage inside Usage, a private cleanup view for
each member, no quota by default, and a protective pause on new work when the
disk is nearly full (ST §12). This design takes the smallest version of
each. Nothing counts storage per person today. The only free-space check
guards the speech-to-text spool (`stt_chunking.py:560-595`).

### 6.1 Owner: Storage in Usage (owner section)

- **Disk.** One line per filesystem that holds Azizam data (the creations
  folder, `data/` and job spools), merged when they are the same device:
  free bytes, total, percent used (description only), the reserve in bytes,
  measured time, and state Healthy, Low, Paused or Unknown (§6.4).
- **People.** One row per person: name, stored bytes, item count, sorted by
  bytes. Tapping a row shows the split by kind: pictures, videos, audio,
  voices, other. No limit column (§6.5).
- **Service Apps.** One row per service App: App name, its accountable
  person, bytes. They are not added to the person's row (ST §12).
- **Not linked to a person.** Bytes no App accounts for (Unresolved, §10.2
  question 2), and bytes kept only by a legacy owner key that maps to no App.
  Never spread across people.
- **Gateway data.** Databases and caches, as one separate line.
- No file names, thumbnails, single-item dates or links. It is the storage
  projection (§3.3).

### 6.2 Member: their own storage

- Usage gains a Storage row: "{12.4 GB} stored" and "Review storage". Its
  detail says "No personal limit. Everyone on this server shares its space."
- "Review storage" opens the Library sorted largest first (new sort
  `sort=size`), with the existing kind filter. Selecting items shows
  "{n} selected · frees about {x}". The estimate counts the files that would
  have no owner left after this member's claim is released (§6.3); files
  someone else also owns stay on disk. If a selected item is shared, the
  confirmation adds
  "{n} people lose access." Deletion is today's Library delete. There is no
  trash in v1.
- After deletion the number is measured again, not computed.

### 6.3 Accounting

- **Person's stored bytes** (logical): the sizes of creation files whose
  sidecar lists one of the person's App principals, plus their voice folders
  (`custom_voices.py:103`). Sharing adds nothing.
- **Files with several owners** count in full for each owner. The server
  disk line counts them once. Reclaim estimates exclude them (§6.2).
- **Deletion releases a claim (RT2 M5).** Today delete hides the item for
  the deleting person but never removes them from the sidecar, so a file
  two people made stays on disk after both delete it, still counted, and
  "Review storage" could not free it (§1.3). v3: under the existing
  `CACHE_MUTATION_LOCK` (`me_media.py:405-409`), delete removes the
  viewer's principals (`viewer.owner_keys`) from `owner_key_hashes` and the
  viewer from `maker_person_ids`, revokes the viewer's shares of the item,
  and removes the bytes and sidecar only when no owner is left. The audit
  tombstone stays as history. It hides an item only while it is newer than
  the person's claim: if the same bytes are made again later, the new claim
  wins, so the item shows again and counts again (today the tombstone hides
  it for ever, `me_media.py:861-877`). Step 4 also releases the claims of
  every existing `media.asset.delete` event, so items deleted before v3 stop
  counting and can be freed.
  Historical provenance (who made it) lives in the audit event, not in the
  sidecar. A side effect closes a gap: today a deleted item stays readable
  through the person's App keys; after v3 it is not, because their App no
  longer owns it. The asset store writes bytes, owners and makers under the same lock
  (`proxy.py:7037-7052`), so a concurrent identical generation either keeps
  the file or writes it again.
- **How it is measured.** Sidecars have no size field, so a scan reads file
  sizes, caches the result and returns `measured_at`. A result older than
  15 minutes is shown as stale (P18). The scan cost on the live cache is
  UNVERIFIED; if it is slow, the build adds `size_bytes` to sidecars at
  write time instead.

### 6.4 Pause new work when the disk is nearly full

ST: "stop admitting uploads/generation jobs that need affected storage
BEFORE invoking billable providers", and never delete to make room. v2 used
a percentage of the filesystem. RT2 M3 showed that this is wrong for this
server: the disk is shared with other software and is large, so a percentage
says nothing about Azizam's need. Measured on 2026-10-11 (§0): 1.45 TB
total, 79.5 GB available, 95% used. v2's 5% reserve is 72.6 GB, so v2 would
have shown Low on deploy and paused after about 7 GB of growth by other
software, while Azizam needs a few GB. v3 reserves **bytes**, sized from the
work Azizam admits.

The speech-to-text spool already does this: it holds an in-process byte
reservation per admitted request and admits only while free bytes minus
reserved bytes cover the new request plus a floor (`stt_chunking.py:560-595`).
v3 applies the same rule to every storage-writing job. It is a counter in
memory, not a persistent ledger.

**Definitions**, per filesystem *d* that Azizam writes (the creations
folder, `data/`, job spools; merged when they are the same device):
- `peak_bytes(job)`: the most bytes one job can add to *d* before it ends:
  stored upload or input copy, output, temporary and conversion copies,
  derivatives (posters), and its database and WAL growth. Each
  storage-writing job kind (image, video, audio, speech, voice, upload)
  declares a bound in config, with a code default taken from its existing
  limits (for example the voice upload limit, `custom_voices.py:50`, and the
  512 MiB MCP result limit, `mcp_server.py:71`). The inventory test (§9.2)
  fails CI for a storage-writing kind with no bound, so no unbounded kind
  ships. The bound for each video workflow is UNVERIFIED; the build measures
  the largest stored output of each kind and sets the default above it.
- `W_max`: the largest `peak_bytes` of any kind the server admits.
- `leased(d)`: the sum of `peak_bytes` of the admitted jobs that have not
  ended. A job takes its lease at admission and returns it when it ends
  (success, failure or cancel). A job that resumes after a restart takes a
  new lease before it runs.
- `reserve_bytes`: owner setting `storage.reserve_bytes`. Default:
  `W_max + 2 GiB`. The 2 GiB margin covers database and log growth and
  gateway recovery. The setting refuses a value below `W_max` with `400
  reserve_below_largest_job`, because then the largest job would not be
  budgeted.

**The rule.** Admit a storage-writing job only if, for every filesystem it
writes, `free_bytes(d) − leased(d) ≥ reserve_bytes`. Otherwise refuse with
`503 storage_paused` (layer gateway). The check runs before an upload body is
read (it uses the declared length) and before any engine or paid provider
call. Because `reserve_bytes ≥ W_max ≥ peak_bytes(job)`, an admitted job has
its own peak covered and still leaves the margin; concurrent jobs are
covered by their leases, so no separate limit on the number of jobs is
needed. Free space that running jobs have already used is counted twice (in
`free_bytes` and in their leases). That errs toward pausing early, by up to
`leased(d)`.

- **Check and lease are one step.** One process-wide lock (a
  `threading.Lock`, because runners use threads) covers reading free space,
  comparing, and adding the lease. The STT gate relies on one event loop
  (`stt_chunking.py:545-595`); this gate does not.
- **Leases cannot leak.** A lease is released in a `finally` on every exit
  of the job's run, including cancel and error. Leases live in memory, so a
  restart clears them all; a job resumed after a restart takes a new lease
  first, and if the rule refuses it, the job stays queued (not failed) until
  the state is Healthy or Low again.
- **Admission follows the state.** New work is admitted only while the state
  is Healthy or Low, so the resume margin also applies to admission and the
  boundary does not flap.

- **States.** Paused: the rule fails. Resume when `free − leased ≥
  reserve_bytes + 1 GiB`, so the state does not flap. Low: `free − leased <
  reserve_bytes + 10 GiB`. Healthy otherwise. Both margins are bytes and are
  settings (`storage.resume_margin_bytes`, `storage.warn_margin_bytes`).
- **Percent is description only.** The disk line shows "95% used" as a
  fact. No state, threshold or copy depends on it.
- **Unknown fails closed.** If free space cannot be read, new
  storage-writing work pauses and the disk line shows Unknown.
- **The promise is "budgeted", not "guaranteed".** Other software can fill
  the disk after admission. A write that fails for lack of space ends the
  job with terminal reason `storage_full`, never success.
- **What still works:** sign-in, browsing, downloads and deletion. Chat and
  other work that stores no file is not paused.
- Nothing is ever deleted automatically (P19).

On this server, as an illustration only: if `W_max` were 1 GiB, the reserve
would be 3 GiB, against 79.5 GB available, so the state on deploy would be
Healthy. The real `W_max` is UNVERIFIED until the build measures it.

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

**Each row ships with the step that makes it true (RT2 M6).** The "Step"
column names the build step (§12). A row never renders before its step; a
row with two variants changes when the later step ships. Navigation follows
the same rule: "Manage sharing" and "Review storage" do not exist before
their steps.

| Where | Step | Exact copy |
|---|---|---|
| Home, first sign-in for a member; once for existing members after the switch | 3 | Title "Your work is private". Body, step 3: "{Owner} sees your usage and spend, not your creations or prompts. Work in shared apps belongs to the app." From step 4: "{Owner} sees your usage, spend and storage, not your creations or prompts. Work in shared apps belongs to the app." Link "What {Owner} can see" (Account > Privacy). Button "OK" |
| Account > Privacy | 3 | Heading "Privacy". Rows: "Private to you: creations, prompts and saved chats." / "{Owner} sees: when you made something, the model, the app you used, how long it took and what it cost." (from step 4, ending "…what it cost and your storage.") / "Shared apps: work made through an app that several people use, like a chat bot, belongs to that app. The person responsible for the app can see and use it." / from step 5 only: "Shared: only what you share, with the people you choose. Prompts only if you include them." → "Manage sharing". Then the honest-limits sentence |
| **Honest limits** (Account > Privacy; "What is saved?" in Try) | 3 | "{Owner} runs this gateway and can read the files it stores. {Owner} can also reset your password and sign in as you; Account > Security shows when a reset happened. Azizam keeps your work out of the owner's views, but it isn't end-to-end encrypted. The engine or provider that makes your work receives your prompt." |
| Account > Security, after a reset someone else issued (§5) | 3 | "Password changed {time} with a reset link from {Owner}." A self-issued reset: "Password changed {time}." |
| Library detail | 5 | Button "Share" |
| Share sheet | 5 | Title "Share this {picture\|video\|audio}". List "With", with each person's name; the owner's row reads "{Name} (owner)". Switch "Include prompt", off by default. Note when off: "They see the {result} and its model. Not your prompt." When on: "They also see: “{prompt}”". With no saved prompt the switch is disabled: "Prompt not saved, so it can't be shared." Footer: "They can download it." Primary "Share with {Name}" / "Share with {n} people" |
| Share refused | 5 | `several_makers`: "Someone else made the same file, so it can't be shared." `file_type`: "GIF files can't be shared yet." `no_maker`: "Work made by a shared app can't be shared." |
| After sharing | 5 | The sheet closes. The detail shows the badge "Shared with {Name}" or "Shared with {n} people" (no toast, P2); tap opens the list |
| Stop sharing | 5 | "Stop sharing with {Name}? They can't open it after this. Copies they downloaded stay with them." Buttons "Stop sharing" / "Cancel" |
| Recipient | 5 | Library filter "Shared with me". Caption "From {Name}". Action "Remove from my Library", note "{Name} still has it." Empty state "Nothing is shared with you yet." Stale open "{Name} stopped sharing this." |
| Recent | 5 | "{Name} shared a {picture} with you." |
| Owner Usage, per-person view (once, under the person picker) | 3 | "Creations and prompts stay private to each person." |
| Owner request detail, someone else's request | 3 | Row "Result" with value "Private to {Name}". When the layer is unknown: "Ask {Name} to share the details." |
| Invite sheet (owner) | 3 | "{Name} gets a private Library. You see their usage and spend." (from step 4: "…their usage, spend and storage.") |
| Member Usage, Storage | 4 | "{12.4 GB} stored" / "Review storage". Detail: "No personal limit. Everyone on this server shares its space." |
| Member, new work refused (`storage_paused`) | 4 | "The server is low on space, so new creations are paused. Your saved creations are still available." Next action: "Ask {Owner} to free space." |
| Owner Usage, disk line | 4 | "{79.5 GB} free of {1.45 TB} ({95}% used). New creations pause when less than {3.0 GB} is left beyond what running jobs may still use." |
| Owner Usage, disk Low | 4 | "Free space is low: {8.2 GB}. New creations pause near {3.0 GB}." |
| Owner Usage, disk Paused | 4 | "New creations are paused: {2.9 GB} free, {3.0 GB} needed. Free space or add storage." |
| Owner Usage, disk Unknown | 4 | "Couldn't read free space, so new creations are paused." Action "Try again" |

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
  creations, prompts, chats, receipts and jobs are private to the people who
  own them (§2.4 O6) from the first start.
- **Removed** (P11, no aliases): the `scope`, `owner` and `person` parameters
  on `/me/*`; `"scope": "instance"`; `_audit_everyone` and its four actions;
  `receipt.detail.instance`; `recent_videos`, `recent_images`,
  `recent_renders`; the `admin` job and event audiences; the Library person
  picker and the Usage person picker outside the owner section; the
  `scope=everyone` web calls (`library/page.tsx:244-245`, `:468`, `:483`,
  `:948`, `:997`, `:1624`, `:1647`, `:2274`); signed URLs without `md` or `g`;
  the `created_by` back-fill (`admin_store.py:343-387`); service-account
  ownership of Apps and the dashboard's default App service account
  (`admin_runtime.py:356-359`). Audit rows already written keep their labels
  (`web/src/lib/audit-description.ts:4-5`).

**One protocol, two units.** Both units run in AP's migration tool and
protocol (AP §4.2: gateway lock, full backup, marker before any live change,
migrate in place, verify, owner accept; restore only before accept). There
is no second migration framework (RT 10, RT2 complexity note). The tool
ships with whichever design is built first. Each unit writes its own version
row in `admin.db`. A unit whose version row exists is **verify only**: it
runs its verify step and changes nothing. So the ownership unit runs once,
whether AP or this design ships first, and AP's plan migration runs it, or
verifies it, before its own steps.

### 8.1 Unit 1: the ownership cut-over (§2.4; RT2 B1)

v2 assumed AP would give service Apps a person, and said service work stayed
reachable through keys if AP had not landed. RT2 B1 showed that this was
not enough: today a service App cannot have a person (`admin_store.py:1238-1243`),
the admin route turns an App with an owner into a personal App
(`admin_runtime.py:344-355`), and revoking keys by comparison with an absent
owner proves nothing. So the ownership cut-over is a hard prerequisite of
the privacy switch: it is §12 step 2, the switch is step 3, and it is
complete in itself.

1. **Preview** (read-only, on the stopped gateway's copy):
   - Every App: id, name, kind, current owner (person or service account),
     proposed accountable person, last use of any key. An App owned by a
     service account gets an approval row with **no default**; the owner
     names a person. (AP §8 expects the owner to name Tim for daisy,
     open-webui, cap and waspflow-dsh-probe; that is an expectation, not a
     pre-filled value.) A missing approval blocks
     apply. Nothing is inferred from receipts, names or plans, and no App
     becomes personal because it has a person.
   - Every personal App, other than a session App, whose `app.create` audit
     actor is not its owner, because the dashboard may have turned an
     intended service App into a personal one (§1.3). Its approval row
     defaults to "keep personal"; the alternative is "service", with its
     current `owner_user_id` as the accountable person (the preview shows
     that person; v1 has no transfer). Only personal → service is offered:
     it narrows (the App's keys lose their person, O5). Service → personal is
     never offered.
   - The new service App "No person recorded", with the owner as its
     accountable person, if §10.2 question 2 finds proven-unbound records.
   - Every unrevoked App key whose `created_by IS NOT` its App's proposed
     person (NULL, `""`, an admin's key on a member's App), computed as apply
     will compute it: with the proposed persons set first and the back-fill
     run against them. Each listed key is revoked by apply; the preview also
     lists the keys that survive.
   - Every App with a key whose `last_used_at` is in the last 30 days and
     whose keys would all be revoked. (Session Apps hold no keys and are not
     in this list.)
     Apply refuses until the owner makes a replacement key (today's admin
     route allows it before the cut-over, with `created_by` set to the
     owner, `admin_runtime.py:585`) or approves "this App stops working".
   - The count of records whose principal is `service_account:…`. They go
     to Unresolved (§10.2 question 2), granted to nobody.
2. **Owner approves** every row.
3. **Apply**, in one `admin.db` transaction, in this order: set
   `owner_user_id`; set `owner_service_account_id` to NULL; apply approved
   kind changes; run the `created_by` back-fill one last time, now that
   every App has its person (it resolves a creator only to the App's person,
   `admin_store.py:370-372`; it has already run at every start, so only keys
   still NULL can change, and keys made through the admin route already
   carry their maker, `admin_runtime.py:585`); revoke
   the listed keys; create "No person recorded"; install the triggers
   (§3.5); write `ownership_version = 1`. The new create paths (§3.5 item 2)
   ship in the same release.
4. **Verify**: zero Apps with a NULL person or a service-account owner;
   zero unrevoked App keys with `created_by IS NOT owner_user_id`; every
   App's kind equals the preview plus approved changes; every App whose key
   was used in the last 30 days has a live key or an approved stop row; trigger SQL in
   `sqlite_master` equals the release's text; and a self-test inside a
   savepoint that is rolled back: set an owner to NULL, change a kind, move
   a personal App, move a service App with a live key, insert a key whose
   `created_by` is not the owner, change a key's `created_by`. Each must
   abort.

### 8.2 Unit 2: the visibility switch

Runs after unit 1 in the same tool run, or later.

1. **Preview**: `superadmin` role rows; records with no person and no App,
   in the three classes of §10.2 question 2; creations and bytes per person;
   free bytes against the reserve (§6.4).
2. **Owner approves.**
3. **Apply**: map `superadmin` rows to `owner` or `admin`; attach the
   proven-unbound records to "No person recorded"; leave Unresolved records
   ungranted; write `visibility_version = 1`.
4. **Verify**: zero `superadmin` rows; the attached records are owned by
   "No person recorded"; `ownership_version = 1`.

The `shares` table is not part of the switch. It is new, holds no existing
data, and is created by the sharing release (§12 step 5) with its normal
schema code. Before that release the core has no Shared basis, so a `g=s`
URL is refused like any unknown grant.

**Fail-closed start.** The gateway refuses to start when:
- the marker says a unit started but was not accepted (AP §4.2);
- the release needs `ownership_version = 1` or `visibility_version = 1` and
  the row is missing;
- `visibility_version = 1` and auth is disabled in config (until AP §2.8
  removes that mode).

Old signed URLs fail on their own: the signed message changed. Browser
sessions keep their person.

### 8.3 Restore (RT2 M4: the two-store guard is removed)

v2 kept a share revision in `media_jobs.db` and in `admin.db` and revoked all
shares when they differed. RT2 M4 showed that it needed a runtime failure
protocol (what the running process does when the second write fails, how
writers are serialized against reconciliation), and that it still could not
detect a consistent restore of both stores. v3 removes it. Shares live in one
store, and each write is one transaction (§4.1).

What remains is a stated limit, the same one AP §4.2 states for keys:
- The supported restore is AP §4.2 `restore`, which runs only before
  accept, before any traffic. No share exists in that window that a
  restore could bring back.
- Restoring a database file by hand (this instance keeps copies such as
  `media_jobs.db.bak.pre-r18`) is a host action (L11). It brings back what
  the copy held: shares stopped later, keys revoked later (AP §4.2). The ops
  restore note says so and gives the step to run before starting the
  gateway: stop every active share
  (`UPDATE shares SET revoked_at = <now>, revoked_by = 'restore' WHERE revoked_at IS NULL`),
  then revoke again the keys revoked since the copy. Members share again.

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
| Storage reserve | `PUT /admin/api/settings/storage {reserve_bytes, warn_margin_bytes, resume_margin_bytes}` | `azizam admin storage reserve <bytes>` | Deferred |
| Own Security events | `GET /me/api/security/events` (§5) | `azizam account security` | Not supported |

Errors:
- `400 param_not_supported` for a removed parameter, in any form (§3.7).
- `404 not_found` for a record the caller has no access to. Existence is not
  revealed.
- `403 not_your_app` (§3.5).
- `409 share_not_allowed` with reason `several_makers`, `no_maker` or
  `file_type`.
- `403` "Invalid or expired signature" for a stopped, ungranted or old-format
  URL.
- `503 storage_paused` (layer gateway) when free bytes minus leased bytes are
  below the reserve (§6.4).
- `400 reserve_below_largest_job` when `reserve_bytes` is below `W_max`.

There is no CLI in this repo today. The CLI rows depend on HR Release B
item 4.

### 9.2 Tests (the oracle)

The test section is `docs/design/OWNER_VISIBILITY_TESTS.md`: TD, adopted.
Lines that v3 changed start with **[v3]**; the rest is TD unchanged. It
defines the actors (A, B, O, D, S, N, Rk, Rs, Q, F, C, H, U), the resource
states, an oracle written independently of `decide`, the assertion helpers,
the path matrix P01-P20, the signed-grant and metadata suite, the canary and
two-world noninterference tests, the identity, trigger, migration and
storage regressions, the pytest layout, and its UNVERIFIED register. Where
it and this design disagree, this design wins, and the test file is fixed.

Normative rules taken from TD:
- Every resource denial is exactly `404 not_found`, the same body and
  headers as a nonexistent id.
- A denial happens before any side effect: no file read, no mint, no
  provider call, no event sequence repair, no store write (spies assert it).
- Tests call the production `Viewer` builder; a hand-built `Viewer` is
  allowed only in pure core tests.
- No member notice or Privacy copy ships because tests were designed; it
  ships when the step's gate passes on an implementation SHA (§12).

v3 additions that TD does not cover:
1. **MCP callbacks** (RT2 B2), in `test_mcp_boundaries.py`, each through
   the real bound `McpDeps`: a guessed other-person asset id as a `run`
   input; a Shared-only asset as input; a personless service key reusing its
   own asset (allowed) and another App's (404); `store_asset` mints only
   with `Access=Own`; discovery output identical across two worlds that
   differ only in private data; MCP cancel result independent of the poll.
2. **Ownership cut-over** (§8.1): the rows of TD §7 "B1", plus the verify
   self-test.
3. **Reset event** (§5): a reset issued by the owner shows "with a reset
   link from {Owner}"; a self-issued reset shows the plain event; another
   person's events are never returned.
4. **Web (390 px, P4)**: the owner's Library has no person picker; the share
   flow, stop sharing and "Shared with me" work by touch; the owner's Usage
   shows no thumbnails; "Review storage" selects and deletes by touch; the
   member first-use notice shows once; no row of §7 renders before its step.

Rewrite the §1.5 specs to these expectations. Do not delete them.

## 10. Risks and open questions

### 10.1 Risks

| Risk | Mitigation |
|---|---|
| Members read "private" as protection from the host | The honest-limits sentence names host files and resets, one step from every private claim. It never says "only you" |
| Metadata still reveals behavior (models, times, frequency) | Disclosed in Privacy. The projection keeps typed quantities only |
| A route or MCP operation added later skips the core | The inventory test fails CI for any unclassified entry |
| A future route moves a personal App and its history | `admin.db` triggers (§3.5); NULL-safe, so no route can pass through NULL |
| The ownership cut-over picks the wrong person for a service App | No default: the owner names each person in the preview (§8.1). A transfer later needs all keys revoked (O3) |
| Live bearer URLs after a share stops | `g` is checked on every fetch; downloaded copies cannot be recalled, and the copy says so |
| An owner who relied on Everyone loses visibility of a child's work | Plans prevent; supervision is deferred with a fixed direction (§2.3) |
| Logs hold exception text | §3.6 fixes the request-path sinks and ratchets the rest; logs are host files (L11) |
| Reverse-proxy logs hold signed URLs; LiteLLM logging defaults | Deployment gates, UNVERIFIED |
| A hand restore of a database brings back stopped shares | Stated limit, as for keys in AP §4.2; the ops restore note stops every share before start (§8.3) |
| Revoking the listed keys breaks an app that uses one | The preview names each key and its App; an App used in the last 30 days that would lose every key blocks apply until it has a replacement or the owner approves that it stops (§8.1) |
| The disk pause starts on deploy | The reserve is bytes sized from Azizam's own largest job (§6.4), not a share of a large shared disk. The preview reports free bytes against it |
| Another program fills the disk after a job is admitted | The promise is "budgeted", not "guaranteed"; the job ends `storage_full` (§6.4) |
| A person who is accountable for service Apps is removed | Removal disables their Apps and revokes the keys (`accounts.py:430-436`); O8 then denies the Apps. With no transfer route in v1, those Apps' work stays on disk under a removed person and nobody can open or free it in the product. Known limit; the transfer route (O3) is its fix. Until then, remove such a person only after deciding about their Apps |
| `peak_bytes` for a kind is too low | The margin in `reserve_bytes` absorbs a small error; the build sets defaults above the largest stored output of each kind (§6.4) |
| The storage scan is slow on a large cache | `measured_at` and stale state; fall back to sizes in sidecars (§6.3) |
| Wide change: about 25 routes, MCP and three web surfaces | Build order (§12): core first with no behavior change, then the ownership cut-over, then the switch with the matrix test as gate |

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
4. **Disk reserve.** **Default: `reserve_bytes = W_max + 2 GiB`**, an
   absolute amount (§6.4), not v2's 5%. With 79.5 GB available today
   (measured, §0), this does not pause on deploy unless `W_max` is above
   about 77 GB. Change it only if you want a larger margin for the other
   software on this disk.
5. **Who is accountable for each App owned by a service account.** **No
   default, by design** (§8.1): the cut-over preview asks you to name a
   person for each one. AP §8 expects you for daisy, open-webui, cap and
   waspflow-dsh-probe. You also see personal Apps that the dashboard may
   have made personal by mistake; their default is "keep personal".

## 11. Red-team resolution

### 11.1 Round 1 (v1 at `4a020412`)

Resolutions as applied in v2. "Applied" means the design text changed; code
evidence is in the cited section. v2's "§9.2 item n" references now point to
the adopted test file (§9.2). Where round 2 changed a resolution, the last
column says so.

| RT | Finding | Resolution (v2) | Where | Changed by round 2 |
|---|---|---|---|---|
| 1 (blocker) | The one core replaces resource-specific identity and action rules with an App intersection | **Applied.** Typed `ContentRecord` kinds and an action matrix inside the one module. Each kind keeps today's rule: App for creations (`ownership.py:46-55`), person for prompts (`me_media.py:656`), person and App for chats (`proxy.py:25808-25817`), principal folder for voices. Shared never satisfies a mutation. Identity entry for sessions, personal keys (`admin_runtime.py:1604-1614`), makers and person-less Apps stated. Negative tests added | §3.1, §9.2 | M1: the Accountable basis is removed; one App-ownership rule (O6) |
| 2 (blocker) | Key issuance is not a complete boundary: App ownership can change; key provenance unknown | **Applied.** All issuance paths enumerated (rotate and import do not exist). Triggers fix App kind and personal ownership; a service App owner change needs all keys revoked. Transfer is a content transfer with disclosure. Keys with `created_by` not equal to the owner, NULL or `""` are revoked. Read aliases stay; revocation ends authentication | §3.5, §8 | B1, M2: ownership cut-over (§8.1), NULL-safe triggers, issuance in the database |
| 3 | Shared output needs its own serializer | **Applied.** `shared_view()` typed projection, applied before title, search and filter; list built from `shares`, not the own-receipt filters; title never from the prompt | §4.2, §9.2 | — |
| 4 | The allowlist is not recursive; envelopes add fields back | **Applied.** Typed schema for every leaf, nested `cost` and `resource_decision` included; whole response projected; catalog names only, no `requested_model` fallback; recursive schema test and nested canaries | §3.3, §9.2 | — |
| 5 | Job events and MCP not covered by a route inventory | **Applied.** Inventory covers HEAD, MCP tools and sub-operations, dispatch callbacks and event types (closed set). Admin job and event audiences deleted. Tests assert no side effects | §3.2, §9.2 | B2: every `McpDeps` callback; events follow job read |
| 6 | "No prompts in logs" is contradicted by an exception sink | **Applied.** Claim withdrawn (74 `{exc}` prints found, not only MCP). MCP sink fixed; request-path sinks fixed; ratchet on the rest; exception-path canaries; LiteLLM and Traefik are deployment gates | §1.1, §3.6 | — |
| 7 | Signed grants need a byte, derivative and cache contract | **Applied.** Checks before any read and before Range; poster bound to the share; `private, no-store` for shares; GIF refused; metadata canary per container; "now" scoped to the next fetch and the copy changed. Code facts: no HEAD route, ETag, 304 or server cache exists today (`media_delivery.py:194-234`) | §3.4, §7, §9.2 | `md=full` binding to the fetcher stated (§3.4) |
| 8 | Several makers, partial deletion and the share key do not compose | **Applied, simplest option.** Sharing refuses items with more than one maker; the row's maker is authoritative for prompt, update and stop; deletion revokes that maker's shares and keeps other owners' bytes | §4.1, §9.2 | M5: deletion releases the deleter's claim (§6.3) |
| 9 | Password reset is an existing impersonation path | **Applied.** Privacy page names it; the Security event joins the existing `account_links.created_by` (no new field). **Rejected for now:** verified-channel delivery, because Azizam has no mail | §5, §7 | M6: the event ships with the switch (§5) |
| 10 | Migration and restore need executable fail-closed gates | **Applied.** One presence-based validator for removed parameters; the switch reuses AP §4.2 with a verify step; start refused on a partial switch or auth-disabled config | §3.7, §8 | M4: two-store guard removed (§8.3); both units use the full AP §4.2 protocol, whose marker is written before any live change |
| 11 (minor) | Keep honest accounting and safe failure evidence | **Applied.** Projection keeps the submission-time spending subject, cost quality, source and reconciliation, coverage fields and attempt facts; unknown layer has a next action; no raw-error bypass | §3.3 | — |
| Default 1 | Supervised accounts: defer | Kept. SR's minimal shape recorded as the direction | §2.3, §10.2 | — |
| Default 2 | Unattributed creations: conditional | **Refined**: three preview classes; only proven-unbound records are attached; unresolved ones stay ungranted | §10.2 | `service_account:` principals go to Unresolved |
| Default 3 | Share expiry: none | Kept | §10.2 | — |

### 11.2 Round 2 (v2 at `81ce3204`)

Every citation was re-read at `8a3147f0` (§0). "Applied" means the design
text changed as RT2 asked; "Simplified" means a smaller change that removes
the problem; "Rejected" means RT2's change was not taken, with the evidence.

| RT2 | Finding | Resolution | Where |
|---|---|---|---|
| B1 (blocker) | Step 2 needs the service-App identity migration, not only AP's marker and verify | **Applied.** Confirmed: a service App cannot have a person (`admin_store.py:1238-1243`), and the admin route turns an App with an owner into a personal App (`admin_runtime.py:344-355`). The ownership model is now stated once (§2.4 O1-O8) and migrated once by the ownership cut-over (§8.1), a hard prerequisite of the switch (§12 step 2). The preview has no default person for a service-account App; no App becomes personal because it has a person; only personal → service is offered; both create paths change; the back-fill runs one last time before NULL-safe revocation; an App in use that would lose every key blocks apply. One tool and protocol (AP §4.2) whichever design ships first | §2.4, §3.5, §8.1, §12 |
| B2 (blocker) | The MCP inventory misses direct asset callbacks | **Applied.** Confirmed: `read_asset` and `store_asset` bypass REST (`mcp_server.py:89-99`, `proxy.py:14636-14652`), and `read_asset` allows a caller with no identity on an ownerless asset (`proxy.py:6934-6936`). Every `McpDeps` callback now has one treatment: `read_asset` is `require(…, "reuse")`; `store_asset` mints with `Access=Own`; `catalog` and `aliases` are classified `none` and proven by the two-world test; job content checks each output. Negative tests for guessed, shared and foreign inputs, personless keys, minting and discovery. The notice waits for them (§12 step 3 gate) | §3.2, §9.2 |
| M1 | Session principal expansion makes Accountable ineffective | **Simplified.** Confirmed: "Apps a person owns" selects both kinds (`admin_store.py:1469-1477`), so after the cut-over the accountable person matched Own. v3 takes RT2's second option: one explicit rule (O6), the Accountable basis removed, matrix and copy amended ("can see and use it"). The narrower basis protected nothing, because Accountable could already read and download the bytes. Prompts and chats stay Self and Self+App. Precedence: Own, then Shared; prompts are a separate decision. Events follow job read, which removes the event conflict | §2.4, §3.1, §7 |
| M2 | Ownership triggers need NULL-safe transitions and first-assignment fencing | **Applied.** Triggers use `IS` / `IS NOT`; NULL is refused on every App write, so A→NULL→B cannot happen; the only first assignment is the reviewed cut-over before the triggers exist; the recorded creator is checked inside the credential `INSERT` (atomic), and the route checks the caller; a credential's App, subject and `created_by` are immutable; triggers are replaced with DROP then CREATE and verified by SQL text and a rolled-back self-test. Captured auth: there is none to invalidate. Each request reads the rows again (`admin_store.py:584-592`); the credential and App are read in two queries, which is safe because the fields that could combine wrongly are immutable (§3.1). So no `access_epoch` is needed, which matches AP's removal of it | §2.4, §3.1, §3.5, §8.1 |
| M3 | Replace the percentage reserve with an absolute budget | **Applied, with leases instead of a concurrency bound.** Measured: 1.45 TB, 79.5 GB available, 95% used; v2's 5% would have paused after about 7 GB of growth by other software. Rule: `free − leased ≥ reserve_bytes`, `reserve_bytes = W_max + 2 GiB` by default, configurable, refused below `W_max`; in-process per-job leases as the STT spool already does (`stt_chunking.py:560-595`), so no global concurrency limit is assumed; byte hysteresis and warning; every storage-writing kind declares `peak_bytes` or fails CI; unknown fails closed; "budgeted, not guaranteed"; percent is description only | §6.4, §7, §9.1 |
| M4 | Two-store share revisions need a runtime failure protocol | **Simplified: the guard is removed.** A share write is one transaction in one store. Hand restore is a host action with a stated limit, the same as AP §4.2 states for revoked keys; the ops note stops every share before start. The supported restore (AP §4.2) runs only before any traffic. RT2 itself noted that the guard could not detect a consistent restore of both stores | §4.1, §8.3 |
| M5 | Storage cleanup inherits a deduplicated-file reclamation dead end | **Applied.** Confirmed: delete never removes an owner from the sidecar (`me_media.py:397-426`). Delete now releases the deleter's principals and maker entry under the existing cache lock, revokes their shares, and removes bytes when no owner is left. The audit tombstone stays as history. Reclaim estimates count files that would have no owner left. As a side effect, a deleted item is no longer readable through the deleter's App keys | §6.2, §6.3 |
| M6 | Step-2 copy promises later features and an unassigned recovery event | **Applied.** Confirmed: no "Password changed" event exists in `web/src` or `gateway`. The reset event ships with the switch (§12 step 3). Every copy row has a step; storage and sharing rows and links render only from their steps. Document amendments that define a live promise ship with the step that makes it true | §5, §7, §12 |
| N1 (minor) | Keep owner control without reopening content | **Applied as a stated limit.** The owner cannot cancel someone else's job in v1; it is an HR Release C dependency with its own audit reason, never a content grant. MCP cancel returns its own result, independent of the later poll | §3.2, §5 |
| Note: `md=full` | A signed `g=o, md=full` URL carries no person | **No change needed; stated.** Delivery authenticates the fetcher and restores that person's own prompt (`proxy.py:23630-23647`); two makers each get their own; no person in the signature | §3.4 |
| Note: `service_account` principals | Control-plane principals could own content | Runtime auth rejects them (`admin_runtime.py:1648-1649`). The cut-over preview counts records with such a principal; any it finds are Unresolved | §3.1, §8.1 |
| Note: step 1 | "Five checks call the final matrix" may change differing rules | The owner-everyone branches and the no-identity branch stay at the call sites in step 1, named for deletion in step 3, so step 1 changes no behavior | §12 |
| Note: AP permits | The design leaned on `access_epoch` | Removed; per-request authorization is enough (§2.4 Linearization) | §2.4, §3.5 |
| Consistency check of v3 (Sonnet, before commit) | 5 major, 9 minor; no blocker | Applied: cut-over sets persons before the last back-fill and computes the preview the same way; new databases get the triggers from the base schema; tombstones yield to a newer claim and step 4 releases pre-v3 deletions; `user:` principals stay in the session `Viewer`; §6.4 states its error bound, an atomic check-and-lease lock, leases released in `finally`, and admission following the state; database claims reworded (shape, not caller), `NULLIF` app ids, `BEFORE DELETE ON apps`; the two-query auth read argued safe instead of "one query"; test setups that mint keys as a service account added to §1.5; transfer tests deferred; ops restore note moved to step 2; removed-person limit stated (§10.1); Home copy names shared apps | §2.4, §3.1, §3.5, §6.3, §6.4, §7, §8.1, §10.1, §12 |

## 12. Build order

Each step ships on its own, keeps the full test suite green, and makes no
privacy claim before its enforcement exists. Copy and navigation ship with
the step that makes them true (§7). A document amendment that defines a
live promise ships with its step, not in a later documents step.

| Step | Ships | Gate |
|---|---|---|
| **1. One core, no behavior change** | `content_access` with kinds, the action matrix and `Viewer`. The five checks in §1.4 call it. The branches that differ from the matrix stay at their call sites, named for deletion in step 3: the owner-everyone scopes (`app_ids=None`, `me_media.py:461-480`, `user_runtime.py:350-361`) and the no-identity branch (`proxy.py:6934-6936`). The projection (§3.3) and its schema test exist but nothing serves them yet. The inventory test, with today's routes, MCP callbacks and event types classified | The current suite passes unchanged. The pure matrix test passes for every cell |
| **2. Ownership cut-over** (§2.4, §3.5, §8.1) | Both App create paths; owner-only issuance on every path (`403 not_your_app`); the triggers, also in the base schema; the back-fill removed; the ownership unit of the migration tool; the ops restore note (§8.3), because revocations start here. If AP's cut-over has already run, this step is verify only. AP §2.4 cites §2.4 here | TD §7 "B1" and "M2" tests, the trigger transition matrix and the verify self-test. Owner approves the preview; no service-account App without a named person |
| **3. Private by default** | Every read path uses the core and the projection (§3.2), including every MCP callback, job content per output, and job events. Removed parameters fail (§3.7). Signed URLs carry `g` (§3.4). Log sinks (§3.6). The visibility switch (§8.2) and fail-closed start. The reset event (§5). The web Library loses Everyone and the person picker; the owner's per-person usage moves to the Usage owner section. The step-3 rows of §7. PM's viewer table and UB §4.4 copy (§7.1) | TD in full except storage and sharing, plus the §9.2 MCP and reset-event additions. Owner approves the preview. LiteLLM and reverse-proxy logging checked on the deployment |
| **4. Storage** | Owner storage in Usage (§6.1), member storage and "Review storage" (§6.2), accounting and claim release on delete (§6.3), the absolute disk reserve (§6.4). The step-4 rows of §7 | TD P18 and "M3", 390 px journeys |
| **5. Sharing v1** | `shares` table, `shared_view`, share sheet, "Shared with me", Recent entry, the share line of the ops restore note (§4, §8.3). The step-5 rows of §7. UB §2.3 and DATA_RETENTION (shares class (a)) | TD P14, §5 (signed grants and metadata) and the sharing rows of P01-P06 |
| **6. Cross-references** | Links between this design, AP, PM, UB and HR checked | Cross-references checked |

Notes:
- Step 2 comes before step 3 because the privacy promise needs a person on
  every App (O6) and proven provenance on every live key (O4).
- The §6.4 disk pause does not depend on steps 2-3. It can ship right
  after step 1 (whose inventory test it uses) if the disk needs it.
- Until step 3 ships, R18 finding 2 can be closed by a one-line web change:
  the owner's Library starts at "mine". That is optional; step 3 removes
  the view.
- Step 3 is the largest. It is one step because each half alone is false:
  hiding the Library while keys still read member Apps would make the notice
  untrue.

## 13. Before building (P20)

- [x] Non-maker red-team of v1 (RT). Resolutions in §11.1.
- [x] Non-maker red-team of v2 (RT2) and a test design (TD). Resolutions in
      §11.2; TD adopted (§9.2).
- [ ] Red-team v3's changed areas: §2.4, §3.1 (one ownership rule), §3.2
      MCP callbacks, §3.5 triggers, §6.3 claim release, §6.4, §8.
- [ ] AP's revision cites §2.4 and agrees with O1-O8.
- [ ] Owner answers or accepts the defaults in §10.2.
- [ ] Record the go decision against the reviewed version.
- [ ] Follow §12.

## Change log

- v3, 2026-10-11. Resolves RT2 (§11.2). Also:
  - New §2.4: the App-ownership model shared with AP (O1-O8), and the
    per-request linearization rule that replaces any dependence on AP's
    `access_epoch`.
  - The Accountable basis is removed: a person owns what every App they are
    accountable for made (O6).
  - §3.5: NULL-safe triggers, issuance checked in the database, both create
    paths changed. §8 split into the ownership cut-over (§8.1), the
    visibility switch (§8.2) and restore (§8.3, two-store guard removed).
  - §6.3: delete releases the deleter's claim. §6.4: absolute byte reserve
    with per-job leases; measured disk facts in §0.
  - §7: every copy row has a build step. §9.2: TD adopted as
    `OWNER_VISIBILITY_TESTS.md`. §12: new step 2 (ownership cut-over);
    sharing is step 5.
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

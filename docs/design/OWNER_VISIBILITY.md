# Owner visibility: private member work, an operational owner view

Status: design, v5, 2026-10-20. Not built. Tim go on v3 2026-10-12;
v4 edits per round 3; v5 applies the design owner’s round-4 decisions (§11.4)
and v4.2 origin correction (§11.5, D52 addendum).
Round 4 (Claude Opus): steps 1 and 2 can be built; step 3 needs these edits.
D50 makes request history on by default, with incognito. D51 confirms the
section 2 policy: maker-private content, an operational owner view, and
maker shares only. Section 2's access policy is unchanged; §2.4's binding
mechanics are extended as round 3 requires.
Code baseline for every file:line below: `origin/main` at `52408b26`
(`52408b268215585dab40299f2ebc2bd920ab6bff`). Historical review commits
identify review inputs, not the source revision of code citations.

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
  section: `docs/design/OWNER_VISIBILITY_TESTS.md`, adjusted through v5 (§9.2).
- **AP**: `origin/design/access-plans:docs/design/ACCESS_PLANS.md` v4 at
  `545726a7` (plans and migration protocol §4.2). The design remains a
  cross-reference; shipped schema and behavior are checked against main.
  AP's schema 6→7 migration already gives service Apps to the gateway owner
  (`gateway/access_plan_migration.py:13-19`,
  `gateway/access_plan_migration.py:527-531`). This does not close O2-O4.
- **RR**: `docs/design/REQUEST_RETENTION.md` on the code baseline; D50 and
  D51 in `~/Documents/azizam/engine-contract/DECISIONS.md` override its open
  questions about defaults or reader widening. RR slice 5 uses this core.
- **D52**: `~/Documents/azizam/engine-contract/DECISIONS.md`, "Delegated
  credentials and person-less rows under D51", including the 2026-10-20
  addendum: MCP own outputs are scoped to the creating `connection_id`,
  not the shared session App. Applied in §3.1 and §11.5.
- **RT3**: the supplied non-maker Claude Opus round-3 report, checked
  against main; every finding has a resolution and build step in §11.3.

- **UB**: `design/ux-batch1:docs/design/UX_BATCH1.md` (§2.3 Recent, §3 account
  recovery, §4 "Don't save this prompt").
- **HR**: `docs/design/HEADLESS_AND_RESOURCES.md` (principle 4: members see
  shared capacity coarsely; owners see attribution, never prompt content).
- **PM**: `docs/design/PROMPT_MEMORY.md` (prompts are already maker-only).
- Principles P1-P22 (`docs/owner-principles`), `VOICE.md`,
  `docs/ops/DATA_RETENTION.md`, the code-quality canon.
- Code references use full repository paths on the baseline above. The
  old v2/v3 line offsets are replaced, including historical resolution
  tables and the test document's source map. Static source inspection is
  evidence of current behavior, not proof of future implementation.
- Disk, measured 2026-10-20 with `df -B1` on this worktree's filesystem
  (`/dev/nvme0n1p5`, `/`): 1,451,030,417,408 bytes total,
  101,558,423,552 available, 93% used. This does not establish the live
  configured data/cache/scratch devices. Step 4 must re-measure every
  resolved data, creation, spool and private-temp path immediately before
  selecting `W_max` and applying the reserve (§6.4).

## 1. Current state

### 1.1 What is already private

- **Saved prompts and workflows.** `asset_prompts` is keyed by
  `(asset_id, person_id)` (`gateway/prompt_memory.py:38-48`). Every prompt reader
  takes one person. The Library reads the **viewer's** prompts
  (`gateway/me_media.py:1026`, `gateway/me_media.py:1038-1047`), so another person's item shows
  `prompt: null` and "Picture by {maker}" (`_media_title`, `gateway/me_media.py:1658-1668`).
  `md=full` delivery requires the maker's own kept prompt
  (`proxy.py:23751-23765`).
- **Saved chats.** A Responses row needs the same person **and** the same App
  (or the same appless key) (`_responses_row_visible`,
  `proxy.py:25886-25888`, `gateway/retention.py:472-484`). There is no
  owner bypass. Current scope code also permits a matching no-person
  App/credential row; this is a characterization fact, not permission to
  give a person access to no-person request content (§3.1).
- **Custom voices.** One folder per principal key
  (`gateway/custom_voices.py:58-61`), plus legacy owner folders (`gateway/custom_voices.py:135-136`). There is
  no owner bypass.
- **Key API.** `/gateway/v1/*`, `/v1/videos/*` and MCP go through
  `owns_record` (`gateway/record_ownership.py:11-19`), `_asset_access_allowed`
  (`proxy.py:6969`) or `_video_job_access_allowed` (`proxy.py:9374`). None has
  an owner bypass.
- **Receipts store no bodies.** `receipt_store._assert_safe` (`gateway/receipt_store.py:213-233`)
  rejects prompt, input, messages, body and bytes keys. Its own comment says
  the check is structural: it does not prove that values are safe (RT 4).
- **Logs are not clean.** v1 said "the gateway prints no prompts or bodies".
  That is false (RT 6). `gateway/mcp_server.py:687-688` prints `{exc}` for any tool
  failure, and 17 `print(f"…{exc}")` lines exist in `proxy.py` and
  `gateway/*.py`. Exception text can contain input or a signed URL. uvicorn
  access logging is off (`proxy.py:31208-31213`). §3.6 fixes the sinks
  this design depends on.

### 1.2 Where the owner sees member content or content-bearing metadata today

| # | Surface | Path | What the owner gets | How scope is decided |
|---|---|---|---|---|
| L1 | Library list and detail | `GET /me/api/media`, `/media/{id}` (`gateway/me_media.py:167-231`) | Every member's creations: kind, size, model, workflow, seed, maker name, `download_url` | `scope=everyone` or `person=all\|local-network\|<id>` (`_media_scope`, `gateway/me_media.py:737-755`). The web sends `scope=everyone` by default (R18 finding 2) |
| L2 | Download, posters | `GET /me/api/media/{id}/download` (`gateway/me_media.py:386-422`) | A signed URL for any member's creation | Same `_media_scope`. A poster is a separate asset (`poster_asset_id`, `gateway/media_ingestion.py:329`, `gateway/me_media.py:1157`) fetched through the same route (`web/src/app/library/page.tsx:1044-1049`) |
| L3 | Signed URLs | `/v1/assets/{id}/content` (`proxy.py:23556-23678` → `proxy.py:23681`) | Bytes, to anyone with the URL | HMAC over `asset_id:expires[:md]` only (`proxy.py:6867-6889`). No person and no grant. TTL 3600 s (`proxy.py:947`). Public handlers pass no `auth_context`, so the ownership check at `proxy.py:23735` never runs on the wire |
| L4 | Receipt detail | `GET /me/api/receipts/{id}` (`proxy.py:16519-16541` → `gateway/user_runtime.py:324-387`) | Any member's receipt (client audience) | Owner role, implicit |
| L5 | Home, status, catalog and workflow feeds | `/me/api/activity`, `/me/api/home`, `/me/api/status`, `/me/api/models/catalog`, `/me/api/workflows` | Every member's recent receipts and jobs | `app_ids=None if owner` (`gateway/user_runtime.py:305-312`). Not audited |
| L6 | Usage | `GET /me/api/usage/summary` (`gateway/me_media.py:424-443`, `gateway/me_media.py:1700-1926`) | Every member's receipts and totals, plus the people list | `person=all\|local-network\|<id>`. The web defaults the owner to `all` |
| L7 | Owner request log | `GET /admin/api/activity/requests` (`gateway/activity_api.py:20-107`) | Every receipt in the **admin audience**, plus a `source` envelope with `client_hint` (`gateway/receipt_context.py:20-35`) | `"owner" in ctx.roles` (`gateway/activity_api.py:26`) |
| L8 | Status | `/v1/status`, `/status`, `/status/raw` | Live video jobs with `key_name` and `error` text; file names and paths of the newest creations (`status_endpoint.py:2492-2563`, `status_endpoint.py:2999-3049`) | `ADMIN_AUTH`, any admin |
| L9 | Keys on a member's App | `POST /admin/api/apps/{app_id}/keys` (`gateway/admin_runtime.py:620-634`) | A key that reads the App's creations, receipts and jobs; an owner admin key records `created_by=""` | Owner or accountable person (`gateway/admin_runtime.py:358-370`); the owner exception leaves L9 open |
| L10 | Password reset | `POST /admin/api/people/{user_id}/reset` (`gateway/accounts_api.py:302-308`) | A sign-in link; route default 1 hour (`gateway/accounts_api.py:72-73`), store helper default 7 days (`gateway/accounts.py:473-475`) | Owner; issuer stored (`gateway/admin_store.py:335-338`, `gateway/accounts.py:503-506`), no member Security notice |
| L11 | Host files | `data/*.db`, `cache/images/*`, job spools, logs | Everything | Host authority. Outside product control |
| L12 | Trusted addresses on any App | `POST /admin/api/apps/{app_id}/trusted-addresses` (`gateway/admin_api.py:2096-2117`) | Keyless App access, with no person; owns `app:<id>` content (`gateway/admin_runtime.py:2457-2482`) | Owner can add to any non-session App; store checks no creator/accountable match (`gateway/admin_store.py:2065-2098`) |
| L13 | Per-request retention ledger | `GET /admin/api/requests/{id}/retention/ledger` (`gateway/api.py:965-973`); MCP `requests` (`gateway/mcp_server.py:654-669`) | Backend, resolved and free-text requested model, request timestamps and existence; members cannot read their own through this admin route | `_require_admin` with no request ownership (`proxy.py:14047-14076`); MCP checks admin grant (`proxy.py:14006-14010`) |

Not an exposure today:
- `/me/api/jobs`, `/me/api/make/jobs` and notification polling are self-only,
  owners included (`gateway/me_media.py:127-165`; `gateway/me_make.py:181-227`).
- Library DELETE is self-scoped: `_member_media_scope(actor.user_id)`
  (`gateway/me_media.py:233-278`). It removes bytes only when the file has one owner
  and one maker (`gateway/me_media.py:695`).
- People, Apps, credentials and config export carry no content.
- There is no content export or backup endpoint and no CLI in this repo.

### 1.3 Identity facts the core depends on

These are today's facts. §3.1 keeps them.

| Fact | Code |
|---|---|
| Records store the App principal `app:<id>`, not the key that wrote them. Every key of an App reads what the App made | `gateway/ownership.py:46-55`, `gateway/ownership.py:75-77`, `gateway/ownership.py:85-94` |
| Revoked keys stay **read aliases** for their principal. Revocation ends authentication only | `gateway/ownership.py:3-4`, `gateway/ownership.py:85-94` |
| A key has a person only if its App is `personal` **and** the key's `created_by` is the App's owner | `person_for_app_credential`, `gateway/admin_runtime.py:2312-2322` |
| A browser session has a person directly, through its session App | `runtime_identity_for_session_app`, `gateway/admin_runtime.py:2436` |
| A creation's makers are `maker_person_ids` in its sidecar. Files are content-addressed, so two people who make identical bytes share one file with two makers | `proxy.py:7088-7102`, `proxy.py:7110-7113`; `gateway/me_media.py:1131-1135` |
| Apps already have `owner_user_id TEXT NOT NULL` with a users FK; `owner_service_account_id` is gone | `gateway/admin_store.py:143-154` |
| AP v4 assigns service Apps to the gateway owner during schema 6→7, and writes accountable people during migration | `gateway/access_plan_migration.py:13-19`, `gateway/access_plan_migration.py:527-531` |
| Admin App creation defaults to service, keeps the requested kind, requires an access-plan assignment, and defaults the accountable person to the actor or gateway owner | `gateway/admin_runtime.py:372-401` |
| Every key records `created_by`; legacy recovery still runs at startup. Unknown stays NULL without audit evidence; a proven foreign actor becomes empty string | `gateway/admin_store.py:550`, `gateway/admin_store.py:612-655` |
| Session Apps never hold credentials; these existing triggers do not enforce O2-O4 | `gateway/admin_store.py:183-200` |
| A trusted-address request carries only its App principal and `person_id=None`, even on a personal App | `gateway/admin_runtime.py:2457-2482` |
| "Apps a person owns" selects on `owner_user_id` for both kinds. Call sites use it, including reuse (`gateway/me_make.py:452`) and the Library scope (`gateway/me_media.py:729-734`) | `gateway/admin_store.py:2144-2152` |
| Admin key creation sets `created_by` to the acting person, or `""` for a service-account actor. Self-service, Try and invite keys set it to the App's owner | `gateway/admin_runtime.py:634`; `gateway/user_runtime.py:417`; `gateway/accounts.py:585-586` |
| Authentication resolves the key and its App in one SELECT on each request, then checks active runtime use | `gateway/admin_store.py:2463-2485`; `gateway/admin_runtime.py:2403-2433` |
| MCP reads input assets and stores result bytes through two direct callbacks, not through a REST route | `gateway/mcp_server.py:109-119`; `proxy.py:14653-14669` |
| Library delete hides the item for the deleting person (an audit tombstone) and removes bytes only when the file has one owner and at most one maker. It never removes an owner from the sidecar, so a file that two people made stays after both delete it | `gateway/me_media.py:263-278`, `gateway/me_media.py:676-707`, `gateway/me_media.py:1299-1322` |

### 1.4 Authorization is spread out

There is no single authorization core. `owner` means `{owner, superadmin}` in
`gateway/me_media.py:81`, `gateway/me_media.py:723` and `gateway/user_runtime.py:306`. It means `owner` only in
`gateway/activity_api.py:26` and `gateway/accounts_api.py:132`. No code grants `superadmin`;
a row can exist only from old data. The same ownership rule is written five
times: `owns_record`, `_asset_access_allowed`, `_video_job_access_allowed`,
`_responses_row_visible`, `_receipt_in_scope` / `_job_in_scope`
(`gateway/me_media.py:1370-1387`). The rules differ on purpose by resource (§1.3): App
for creations, person for prompts, person and App for chats.

### 1.5 Tests that encode owner-sees-all (step 3: rewrite)

- `tests/test_retention_ledger_headless.py:43-73`, `tests/test_retention_ledger_headless.py:78-95`: replace admin ledger access with maker read, owner projection, non-owner-admin 404 and missing-id equivalence; add maker-key/MCP permission cases.
- `tests/test_me_media.py:480-503`, `tests/test_me_media.py:557-569`, `tests/test_me_media.py:633`, `tests/test_me_media.py:720-727`, `tests/test_me_media.py:892-905`,
  `tests/test_me_media.py:1130-1141`
- `tests/test_home_api.py:130-172`
- `tests/test_workflows_catalog.py:753-789`
- `tests/test_activity_api.py:45-108`
- `tests/test_prompt_memory_identity_r2.py:158`
- `web/e2e/live-data-scoping.spec.ts:40-68`, `web/e2e/live-data-audio.spec.ts:12`
- `web/e2e/library.spec.ts:314`, `web/e2e/library.spec.ts:411`, `web/e2e/library.spec.ts:427`, `web/e2e/library.spec.ts:437`
- `web/e2e/prompt-memory.household.ts:218-242`, `web/e2e/prompt-memory.household.ts:370-377`
- `web/src/app/library/page.spec.tsx:860-875`
- `web/src/app/library/prompt-visibility.spec.tsx:228-236`
- `web/tests/components/home-library-polish.spec.tsx:340`
- `web/e2e/activity.spec.ts:6-60`
- `web/e2e/global-setup.ts:95-103`, `tests/e2e/contracts.py:144-145` (key
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
| Accounts, plans, keys, trusted addresses | Own; keys and trusted addresses only for Apps they are accountable for (§2.4 O4) | No | Invite, plan, disable, revoke, reset; keys and trusted addresses only for Apps the owner is accountable for | As today, except keys and trusted addresses |
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
triggers enforce the data shape of O2-O4 (§3.5); the issuance routes enforce
who may call them (§3.5 Caller check); `person_for_app_credential` computes O5.
The ownership cut-over (§8.1) makes existing data obey them before any
trigger is installed. Fresh and upgraded databases install them after all
migrations, including AP, and before serving traffic (§3.5).

| Rule | Statement |
|---|---|
| **O1** One accountable person | Every App has exactly one accountable person, `apps.owner_user_id`, for both kinds. AP v4 already enforces NOT NULL and a users FK; no service-account owner column remains (`gateway/admin_store.py:143-154`). A service account is a control-plane identity only |
| **O2** Fixed kind | `kind` is `personal` or `service`, chosen at creation, and never changes after the reviewed cut-over. Session Apps and self-service Apps are personal. Try creates one through `create_user_app` (`gateway/user_runtime.py:472`, `gateway/user_runtime.py:499-536`). A personal App's accountable person never changes |
| **O3** Transfer | A service App's accountable person changes only with zero bindings: no unrevoked App key (expired keys count until revoked) and no trusted address. This transfers all existing App content, with disclosure. v1 has no transfer route |
| **O4** App bindings | Only the accountable person adds a key or trusted address. At INSERT, `created_by IS apps.owner_user_id`. App key subject, App and creator are immutable; revoked App keys never become live again. Trusted-address App, address and creator are immutable; replacement is delete plus accountable-person add. Removal/revocation may narrow access; it never proves issuance authority |
| **O4b** Retention settings | Per-App or per-key retention settings are writable only by the accountable person. The gateway owner can only narrow household retention through `never_keep`; owner role never grants per-member retention writes or reader widening. Steps 2 (binding authority) and 3 with RR slice 5 (retention controls) |
| **O5** Key identity | A key acts for its App. It carries a person only when its App is `personal` and its creator is the owner (`person_for_app_credential`, unchanged). By O4 that is every live personal key. A service App key never carries a person |
| **O6** Content | A person owns (basis Own, §3.1) what every App they are accountable for made, of both kinds. A key owns what its own App made. Person-bearing prompts and saved chats follow §3.1; no-person content is App-only |
| **O7** Policy and spending (AP) | A request runs only if both the accountable person's plan and the App's assignment allow it. Spend is attributed to the accountable person **at submission** and is never rewritten by a later transfer (AP §2.4, §2.11) |
| **O8** Missing person | If the accountable person is missing or disabled, the App's requests are denied (AP §2.4) |

AP v4 §2.4 already references OV for service-App content ownership (O6)
as well as policy and spending. It does not make a key act as that person (O5):
daisy's key does not read Tim's prompts. The amendment runs the other way:
Tim, as daisy's accountable person, can find, use and delete what daisy
made. Someone must be able to do that in the product (P19), and the
accountable person already controls the App's keys (O4), which read all of
it.

**Linearization (steps 1 and 3; RT3 N5).** Authorization is decided per
request from current rows (`gateway/admin_store.py:2463-2485`). A revoke or
ownership change applies to requests starting after commit. An already
running response finishes under the rows it read. Every MCP poll is a new
internal request (`proxy.py:14607-14615`); an event reconnect re-authenticates.
An open job-event stream continues after revocation until it disconnects;
there is no per-event revoke guarantee. Test both that limit and denial of
the next reconnect/poll. No permit or epoch is added.

## 3. Enforcement: one core with typed resources

### 3.1 The module (step 1; enforced in step 3)

`gateway/content_access.py` replaces the five ownership checks in §1.4 and
the owner branches in L1-L7. v1 proposed one universal "Own" rule. RT 1
showed that content kinds need distinct rules: creation ownership alone
must not expose another maker’s prompt or chat, and a share must not
satisfy delete. Keep **one module and one decision function**, with the
resource kind and action as inputs. Round 4 fixes the delegated and
no-person content rules below; current readers are not the target oracle.
v3 removes v2’s separate "Accountable" basis (RT2 M1, below).

```python
@dataclass(frozen=True)
class Viewer:                     # built once per request from trusted auth
    person_id: str | None         # session person; for a key, person_for_app_credential()
    app_id: str | None            # the App the request authenticates as
    connection_id: str | None     # authenticated MCP grant connection
    credential_id: str | None     # authenticated App credential
    key_hash: str | None          # set only for an appless key (chat rule)
    owner_keys: frozenset[str]    # readable principals (ownership.readable_owner_keys) of:
                                  #   a key: its own App;
                                  #   a session: every App the person is accountable for (§2.4 O6),
                                  #   plus user:<person_id>, as today (gateway/me_media.py:1264)
    trusted_address: str | None  # validated runtime identity, never request body
    auth_kind: Literal["session", "personal_key", "app_key", "trusted_address", "mcp", "control_plane"]
    privacy_permission: bool      # session: true; delegated key/grant: explicit maker consent
    is_owner: bool                # role == "owner"; read only by operational()

Kind   = Literal["creation", "prompt", "chat", "request_history", "debug_capture",
                 "voice", "receipt", "job"]
Action = Literal["read", "delete", "reuse", "share", "link", "cancel"]
Access = Own | Shared(share_id, include_prompt) | Deny

def decide(viewer: Viewer, record: ContentRecord, action: Action) -> Access
def require(viewer, record, action) -> Access    # raises 404 not_found on Deny
def operational(viewer) -> bool                   # viewer.is_owner
def project(record: ReceiptRecord | JobRecord | StorageRecord) -> OwnerView   # §3.3
def shared_view(record: CreationRecord, share: Share) -> SharedView          # §4.2
```

`ContentRecord` is a tagged union. Each kind carries only the identity
fields it stores today, plus originating App and credential/connection
provenance required in step 3:

| Kind | Identity fields | Source |
|---|---|---|
| `creation` (bytes, poster, sidecar facts) | `asset_id`, `owner_keys`, `maker_person_ids`, App | sidecar (`proxy.py:6963-6966`, `proxy.py:7110-7113`) |
| `prompt` (saved prompt, workflow) | `asset_id`, `person_id`, originating `app_id` and credential/connection origin | Current `asset_prompts` key is asset/person (`gateway/prompt_memory.py:38-48`); step 3 must persist/resolve trusted App and credential/connection origin for ContentRead, never infer it from today’s accountable person |
| `chat` (stored Responses) | `person_id`, `app_id` or `owner_key_hash`, credential/connection origin | `proxy.py:25886-25899` |
| `request_history` | `person_id`, originating `app_id` and credential/connection origin for permission and narrowing policy; no-person App-only exception below | RR §4 (`docs/design/REQUEST_RETENTION.md:427-504`); future slice 5 store |
| `debug_capture` | Non-null `person_id`, originating App/key/grant for permission and narrowing policy | RR §4 (`docs/design/REQUEST_RETENTION.md:427-504`); future Debug store |
| `voice` (custom voice, consent) | principal folder | `gateway/custom_voices.py:58-61`, `gateway/custom_voices.py:135-136` |
| `receipt` | App principal, `person_id` at submission | receipt row |
| `job` (and its events) | `owner_key_hash` / `owner_scope`, App | `gateway/media_jobs.py:203-206` |

**Bases.** Each is one predicate:
- **Own**: the record's principals intersect `viewer.owner_keys`. Today's
  App-level rule, with §2.4 O6 saying which Apps a session owns.
- **Self**: `record.person_id == viewer.person_id`, both non-null. An App
  never satisfies Self.
- **Self+App**: non-null Self and the same non-null App (legacy appless rows require the same credential). Used for chat delete/reuse, not the read rule.
- **OwnOutput**: for an App key, an attributed row created by its same
  non-null App; for an MCP grant, a row created through its exact same
  non-null `connection_id`. Matching the session App alone never qualifies
  an MCP grant. Legacy rows without origin attribution count as
  browser-created and cannot satisfy this exemption.
- **ContentRead** (prompt, chat, request_history): for a person-bearing row,
  Self plus either a browser session, OwnOutput, or explicit
  `privacy_permission`. For a row with no person, only an App credential
  or trusted-address identity with no person and the exact same non-null
  `app_id` qualifies. A session or other
  person never reads a no-person row, even if accountable for its App.
  Neither null person equality nor null App equality grants access.
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
re-upload. ContentRead separately protects person-bearing prompts, chats and
history. No-person rows are made by the App and readable only through that
App's credentials, never its accountable person's session. O6 does not
change that rule. Compared with v2, the accountable person gains reuse,
voices and job events of the service App. Member copy discloses it (§7).

**Precedence.** `decide` tries Own first, then Shared. Own is a superset of
Shared for every creation action, so a viewer who has both gets Own and the
own serializer. A share never selects a prompt: prompt reads are a separate
`prompt` decision (ContentRead, or the share’s maker row when `include_prompt`).

**Delegated reads and retention (round 4 and v4.2, steps 1 and 3).**
D52's 2026-10-20 addendum defines "own outputs" by the creating credential:
an App key reads attributed chats, prompts and request_history created by
its App; an MCP grant reads rows created through its `connection_id`.
These reads need no `privacy_permission`, including stored-Response
continuation with `previous_response_id`. Browser sessions and MCP grants
share a session App, so matching `app_id` alone cannot give a grant access
to browser history. A grant reading browser-session rows, another
connection's rows or unattributed legacy rows needs `privacy_permission`
and non-null Self. Legacy rows without origin attribution count as
browser-created (fail closed). Browser sessions never need the flag.

Step-3 write paths record trusted origin on every new content row:
`connection_id` for MCP, credential id for App keys, and an explicit browser
session origin for browser writes. Retain originating `app_id` for the
App-key rule. Never accept origin claims from request data or infer an MCP
origin from the session App. This adds provenance; it does not back-fill
legacy rows as a grant's own outputs.

Debug capture remains non-null Self plus explicit permission for delegated
read/delete; it has no reuse action.

Store `privacy_permission` as a column on credentials and on MCP grants.
Only the App's accountable person, authenticated through a browser session,
may write it. No credential or grant can grant itself permission. New and
existing keys default off. The unit-2 preview lists keys that read cross-App
content in the last 30 days, so the accountable person can turn it on per
key; do not silently grandfather them (§8.2, like the binding-use block in
§8.1). Ordinary MCP consent is not this permission.

`retention.reader_allowed(record, viewer, policy)` calls
`content_access.decide` first, then narrows for policy, expiry and erasure.
`readers=maker_signed_in` requires a browser session even when delegated
permission exists. Lists, search, direct reads, export, continuation, Run
again and content links use the same core. RR slice 5 adds no independent
reader (`docs/design/REQUEST_RETENTION.md:427-504`,
`docs/design/REQUEST_RETENTION.md:1712`). Owner policy may only narrow.

**Rows with no person (round 4).** The maker is the App. Existing rows
(open-webui, daisy) stay readable only by their exact same non-null App,
under normal retention. A service-key or trusted-address `store:true`
request after step 3 is accepted and stored for that App's own continuation
only. Its receipt states that scope. API reads by that App's credentials
work; no person's UI or history ever shows the content, and the accountable
person is never assigned as maker. Same-App `previous_response_id` works;
any other App or person gets the same 404 as a missing id. Without explicit
`store:true`, no-person requests keep only structural receipts. Incognito,
expiry, erasure and household restrictions still narrow storage. Replace
the current loose no-person exception (`gateway/retention.py:459-468`) with
this explicit non-null App scope, not a blanket refusal.

**Step-3 deletions.** Remove the independent/looser reader rules in
`conversation_row_visible` (`gateway/retention.py:472-484`),
`_responses_row_visible` (`proxy.py:25886-25888`), the `md=full` prompt
restore for a key fetcher (`proxy.py:23748-23765`), and key-API prompt reads
(e.g. `proxy.py:9518-9523`). Replace each with the typed core decision;
person equality alone must not bypass cross-App permission.

**The action matrix.** A cell lists the bases that allow the action. A blank
cell is Deny. This table is the whole policy. The test oracle in §9.2 is
written from it independently; it does not call `decide`.

| Kind | read | delete | reuse (input, "Use again", workflow download, `md=full`) | share | link (mint a signed URL) | cancel |
|---|---|---|---|---|---|---|
| creation | Own, Shared | Own | Own | Maker | Own → `g=o`; Shared → `g=s` | |
| prompt | ContentRead; Shared with `include_prompt` reads the **share's maker's** row | Self | Self plus ContentRead for delegated `md=full` (§3.4) | | | |
| chat | ContentRead | Self+App | Self+App | | | |
| request_history | ContentRead | Self + privacy permission for delegated credentials | Self + privacy permission for delegated credentials | | | |
| debug_capture | Self + privacy permission for delegated credentials | Self + same permission | | | | |
| voice | Own | Own | Own | | | |
| receipt | Own | | | | | |
| job (and its events) | Own | | | | | Own |

**Own-output read cells (v4.2; prompt/chat/request_history).** All
person-bearing rows below require non-null Self and remain subject to
retention narrowing. "Allowed" is the read cell, not a mutation grant.

| Viewer / row origin | privacy_permission off | privacy_permission on |
|---|---|---|
| App key / attributed row created by that App | Allowed | Allowed |
| App key / other App or browser-session row | Deny | Allowed |
| MCP grant / own connection_id | Allowed | Allowed |
| MCP grant / browser-session row on the same session App | Deny | Allowed |
| MCP grant / another connection on the same session App | Deny | Allowed |
| MCP grant / unattributed legacy row | Deny | Allowed |
| App key / unattributed legacy row | Deny | Allowed |
| Browser session / own person's row, any origin | Allowed | Allowed |

Rules that follow from the matrix:
- **Role is never an input.** `is_owner` feeds only `operational()`.
- **Shared never satisfies a mutation**, `reuse`, `share` or an `o` link.
- **ContentRead is distinct from O6.** OwnOutput reads need no permission;
  delegated reads outside that origin need Self and permission. App-key
  ownership is App-scoped; MCP ownership is connection-scoped.
  No-person rows require the exact same non-null App and no viewer person.
  Foreign-created personal keys are revoked in step 2.
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
  includes (`gateway/me_media.py:1257-1275`). That replaces the `list_apps_for_user`
  callers that decide content (`gateway/me_media.py:729-734`, `gateway/me_make.py:452`,
  `gateway/home_api.py:367-368`, `gateway/user_runtime.py:274`), so they cannot drift apart. The
  other callers (`proxy.py:1999`, `proxy.py:17022`; `gateway/user_runtime.py:220-229`, `gateway/user_runtime.py:465`)
  list Apps for management and already include service Apps after AP v4;
  that is intended (O1).
- App key: `app_id` is the key's App; `person_id` comes from
  `person_for_app_credential`, so only a personal App's key made by its owner
  has a person (O5). Current main resolves the key and App in one SELECT
  (`gateway/admin_store.py:2463-2485`). O2/O4 prevent rebinding fields;
  revoked credentials cannot authenticate.
- Trusted address (step 1 identity, step 2 binding): `app_id` is the matched
  App, `person_id=None`, `key_hash=None`, and `owner_keys` contains only
  `app:<id>` and its read aliases. Record `trusted_address` from trusted
  runtime auth, never the body or an unchecked forwarded header
  (`gateway/admin_runtime.py:2457-2482`). It satisfies Own, never Self.
- MCP OAuth grant (steps 1 and 3): person and session App come from the
  grant; readable App principals are the person's Apps, as for a session.
  Carry its authenticated `connection_id`; sharing the session App does
  not make browser rows its own. Carry privacy permission for content
  outside that connection and for Debug reads; ordinary MCP consent is
  not that permission (`proxy.py:2425-2453`).

- Maker: the request's person at write time, stored in `maker_person_ids`. A
  creation made through a key with no person has no maker and cannot be
  shared.
- Control-plane `service_account` credentials own no content and never reach
  a content route (runtime auth rejects them, `gateway/admin_runtime.py:2411-2416`).
  The cut-over preview counts records whose principal is `service_account:…`;
  any it finds are Unresolved (§10.2 question 2), granted to nobody.
- Auth is already mandatory: main rejects the removed auth-disabled setting
  (`proxy.py:2114`). Fail-closed migration/version checks remain (§8.2).

Existing identity remains authoritative; step 3 records App and
credential/connection origin for ContentRead. Missing or ambiguous origin
counts as browser-created and cannot qualify for OwnOutput. The separate
no-person exact-App rule is unchanged. Sharing rows and retention kinds/
permission join the same decision function with RR slice 5. No second
content reader is added.

### 3.2 Every read path uses it (step 3; inventory in step 1)

| Path (from §1.2) | Change |
|---|---|
| L1 `/me/api/media`, `/media/{id}` | Always the caller's Own and Shared items. `view=mine` (default) or `view=shared-with-me`. `scope`, `owner` and `person` are removed (§3.7). `_MediaScope.app_ids=None` and `audit_everyone` are deleted |
| L2 download, posters | `require(viewer, creation, "link")`, then mint a URL with that access (§3.4) |
| L3 signed URLs | §3.4 |
| L4 `/me/api/receipts/{id}` | Own only; 404 for every other role. The owner reads others at `GET /admin/api/activity/requests/{id}` as the projection. `receipt.detail.instance` audit is deleted |
| L5 activity, home, status, catalog, workflows | `/me/*` is always the caller's own. The owner branch at `gateway/user_runtime.py:305-312` and the `"scope": "instance"` label are deleted |
| L6 Usage | `/me/api/usage/summary` is own only. The owner's per-person usage and storage move to `GET /admin/api/usage` and `GET /admin/api/storage`, in the projection |
| L7 request log | Built entirely by `project()` (§3.3), including the envelope. New `GET /admin/api/activity/requests/{id}` |
| L8 status | Delete `recent_videos`, `recent_images` and `recent_renders` (`status_endpoint.py:3978-3981`). `_live_video_jobs` uses the job projection. `key_name` on live jobs, `auth_keys`, per-key request rates and `recent_events` are owner-only |
| L9/L12 keys and trusted addresses | Step 2 refuses additions by anyone except the accountable person, at the route and database; cut-over revokes/removes unproven bindings (§3.5, §8.1) |
| L13 per-request ledger | `GET /gateway/v1/requests/{id}/retention/ledger` (and `/me` UI adapter) is `content:receipt:read` with Own from the core, checked before ledger lookup. Full ledger follows own receipt detail; owner role adds nothing. Owner operational route returns only §3.3 projection, never `observed.requested_model`, reason or deadline. Non-owner admins get `404 not_found` on that operational route, exactly as for a missing id; their own maker route still works. Replace the old admin path with that owner projection; no alias to full content |
| Retention fact ledgers | `/admin/api/settings/retention/ledger` and `/admin/api/engines/{id}/retention/ledger` are `operational`: facts with no request content, owner and admin read (`gateway/retention_fact_api.py:59-66`) |
| No-request-id ledger | `none`: current destination/config facts only, no receipt lookup or observed/request fields; keep the management/discovery gate and prove two-world invariance |
| History and Debug maker routes | `content:request_history:*` or `content:debug_capture:*` plus retention narrowing (§3.1), with ContentRead for history and Self/permission for Debug; RR slice 5 gates their delivery |
| Job events | Own, as for the job itself (RT2 M1 removed the conflict), on every surface. The `admin` job audience (`gateway/media_jobs.py:300-311`, an allowlist that still includes `assets` and `error`) and the admin event audience (`gateway/media_jobs.py:1382-1397`) are deleted. No owner event view exists in v1 (RT 5). Per-job event sequence numbers stay; household-wide row ids stay hidden (`gateway/media_jobs.py:1398-1400`) |
| `/gateway/v1/*`, `/v1/videos/*`, `/v1/responses/*`, voices | Call `decide` with the kind and action. Replace legacy readers with §3.1, including App-key same-App and MCP same-connection continuation and scoped no-person storage |
| Job content (`/gateway/v1/media/jobs/{id}/content`, `/v1/videos/{id}/content`) | `require(job, "read")`, **and** `require(creation, "link")` for each output before its bytes or URL. An owned job does not vouch for an asset it names |
| MCP (`/mcp`) | Table below: every `McpDeps` callback, not only `call` (RT2 B2) |

**MCP callbacks (RT2 B2).** v2 said every MCP operation goes through REST.
That is false: two callbacks touch assets directly, and two read the catalog
directly (`gateway/mcp_server.py:109-119`, wired at `proxy.py:14777-14800`). Each
callback has one treatment:

| `McpDeps` callback | Today | v4 (step 3) |
|---|---|---|
| `authenticate` | `authenticate_request` (`proxy.py:14783`) | Builds the `Viewer`. Every other callback receives that `Viewer`, never a raw `auth` that may be `None` |
| `call` | In-process REST with the caller's headers (`proxy.py:14599-14604`) | Unchanged. Each call re-authenticates, so the REST route's `decide` applies. Covers `capabilities` speech voices (`gateway/mcp_server.py:428`), `run` submit, `job` poll, wait, content and cancel |
| `read_asset` (run inputs, `gateway/workflow_call.py:131-148`) | `_asset_access_allowed`: App-owner intersection; with no owners it allows a caller with no identity (`proxy.py:6969-6976`, `proxy.py:14664-14669`) | `require(viewer, creation, "reuse")` before the file is opened. Reuse is Own only, so a share, a guessed id or another person's asset is refused with the same answer as a missing asset. The no-identity branch is deleted |
| `store_asset` (results, `gateway/mcp_server.py:325`, `gateway/mcp_server.py:340`) | Stores with the caller's principal, mints with `build_signed_asset_url` (`proxy.py:14653-14661`) | Stores with the caller's principal and maker. Mints with `Access=Own`, which holds by construction: the caller's principal was just written. Same minting rule as every other site (§3.4) |
| `catalog`, `aliases` (discovery, unknown-target suggestions) | Workflow registry and alias config (`proxy.py:14646-14650`) | Classified `none`: they hold operator configuration, not member content. The two-world test (§9.2) proves that discovery output does not change when private data changes. Whether the registry can ever hold a member-made workflow is UNVERIFIED; if it can, discovery moves to `call` |
| `retention_ledger` | Direct provider; admin-grant gate (`gateway/mcp_server.py:654-669`, `proxy.py:14006-14010`) | Change `retention_visible` (`proxy.py:14006-14010`, `gateway/mcp_server.py:656`) to any person-bearing identity for the Own ledger. With request id: same Own receipt decision/maker ledger or owner projection as L13. Without id: `none`, current facts only. No raw-admin bypass and no existence oracle |
| `stt_measurement` | Operator callback (`gateway/mcp_server.py:134`, `proxy.py:14785`) | `operational`; preserve operator authorization and typed output; no member content |
| `rename` (`rename_app`, `rename_key`) | Grant-scoped person mutation (`proxy.py:14618-14643`) | `own-account`; ownership checked before rename, never content authority |
| `visible`, `owner_visible`, `retention_visible` | Discovery gates (`gateway/mcp_server.py:129-141`) | `none`; whether a tool is listed never authorizes a later content read |
| `authorization_servers`, `scopes` | OAuth discovery (`gateway/mcp_server.py:132-133`) | `none`; no private request observations |
| `public_base_url`, `allowed_origins`, `server_version`, `poll_interval` | Config | `none`; include non-callback McpDeps fields in inventory too |

The result URLs that `_assets_from_response` forwards from a core response
(`gateway/mcp_server.py:327-329`) are minted by the core route, which needs an
`Access` (§3.4). The adapter mints nothing on that branch, and a forged URL
fails its signature.

**MCP cancel (RT2 N1).** `job` with `cancel` polls after cancelling, and a
successful poll fetches content (`gateway/mcp_server.py:579-581`,
`gateway/mcp_server.py:603-609`). v4 returns the
cancel result on its own: `{status: "cancel_requested", job_id}` when the
cancel call succeeds. A later poll or content failure does not turn a
successful cancel into an error.

**Inventory (RT 5).** One test lists every entry point and fails CI for an
unclassified one:
- every FastAPI method and route, including HEAD;
- every MCP tool and sub-operation, and **every `McpDeps` callback**;
- every job event type, which must be in a closed set (`gateway/media_jobs.py:1263-1266`
  accepts any non-empty string today).

Each entry is `content:<kind>:<action>`, `operational`, `own-account` or
`none`. Classification is a guardrail. The matrix test (§9.2) proves
behavior.

**The owner's Library "Everyone" view is removed, not narrowed.** The owner's
Library is the same member Library (P8): what their Apps made, personal and
service (O6), and "Shared with me". This also resolves R18 finding
2. The owner's storage question moves to Usage (§6.1).

### 3.3 The operational projection (step 1 schema; step 3 delivery)

`project()` builds a **new** value from a typed schema. It never copies or
filters a record. RT 4 showed that a shallow allowlist is not enough:
`cost` and `resource_decision` are nested maps, and the envelope and
aggregate helpers add `client_hint`, `source_label` and `requested_model`
back (`gateway/receipt_context.py:20-35`, `gateway/receipt_store.py:386-406`,
`gateway/me_media.py:2058-2065`). So the rule is:

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
| | `person` | `{id, name}` of the **spending subject at submission** (AP ledger `person_id`), else `unknown`. Never the App's current owner (RT 11; `gateway/receipt_context.py:50-55` does that today) |
| | `app` | `{id, name}` |
| | `source_kind` | enum (`app_key`, `session`, `trusted_address`, `mcp`) |
| | `protocol` | enum; `stream`: boolean |
| | `capability` | enum (AP §2.2 capability ids) |
| | `model`, `workflow` | `{catalog_id, name}` or `{null, "Unknown model"}` |
| | `route` | enum `local`, `provider`; `provider`: catalog id |
| | `status` | enum; `status_code`: integer |
| | `terminal_reason` | enum (`gateway/receipts.py:294-296`) |
| | `layer` | enum `request`, `plan`, `gateway`, `engine`, `provider`, `unknown`, derived from `terminal_reason` and `status_code` |
| | `latency_ms` and phase durations | integers |
| | `usage` | fixed numeric keys only: input and output tokens, images, audio seconds, video seconds, characters |
| | `cost` | `{amount_usd: decimal or null, quality: measured\|estimated\|unknown, source: provider\|price_table\|none, reconciled: boolean}` |
| | `refusal` | `{code: enum}` |
| | `policy` | `{decision: allow\|deny, reason: enum}` (AP denial codes) |
| | `resource_decision` | list of `{code, effect, resource_class}`, all enums, derived from the reasons of `project_decision(…, "client")` (`gateway/resource_decisions.py:79-96`). The message, actions and owner-only `rule`, `observed_at` and `quality` are dropped |
| | `retention` | list of `{kind: enum, decision: kept\|not_kept\|failed}` only; no reason, deadline, parts or policy limit. Kind enum expands with RR to include `request_history` and `debug_capture` (`gateway/receipts.py:93-107`) |
| Receipt list envelope | `coverage_start`, `aggregate_complete`, `unknown_paid_count`, `history_note` | timestamp, boolean, integer, enum (kept from `gateway/me_media.py:1889-1909`, RT 11) |
| Job | `job_id`, `capability`, `model`, `workflow`, `status`, `phase`, `attempt`, `created_at`, `updated_at`, `latency_ms` | as above |
| | output quantities | integers: count, width, height, duration seconds, bytes |
| Storage | §6.3 | integers, timestamps, enums |

Dropped, for example: `path`, `requested_model`, `accepted_inputs`,
`rejected_inputs`, `ignored_inputs`, `warnings`, `fallback_reasons`,
`client_hint`, `source_label`, `client_address`, `api_key_name`, `request_shape`, LiteLLM ids, job `error`
text, `assets`, `seed`, `input_hash`, `metadata`. The current admin receipt
allowlist includes `client_address` and `api_key_name`
(`gateway/receipts.py:72-81`); they are deliberately absent here. Ledger
observations pass through this projection too, never a nested pass-through
`observed` object (`proxy.py:14074-14076`).

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

Delivery (`_signed_asset_content_response`, `proxy.py:23681`) runs these
checks **before it reads any bytes** (today the read is at `proxy.py:23747`):
1. Verify the signature. A URL without `md` or `g` fails with 403, the same
   message as a bad signature. No shim (P11).
2. If `g=s.<id>`: the share is active, and the asset is the share's
   `asset_id` or its `poster_asset_id` (RT 7: the poster is a separate
   asset).
3. A share grant never allows `md=full`. For `g=o`, `md=full` is not a
   bearer capability: delivery authenticates the **fetcher** and restores
   that person’s kept prompt after ContentRead; key fetchers need
   permission for cross-App/session prompts. Replace the loose person-only
   restore (`proxy.py:23748-23765`) in step 3. Two makers of one file each get their own prompt, and a fetcher
   with no person gets 403 `prompt_not_accessible`. The signature carries no
   person and needs none.
4. A share grant never serves a container that has no metadata stripper.
   Today that is GIF (`gateway/media_metadata.py:62`). Sharing refuses such items
   (§4.1).

Then delivery continues as today: Range slicing (`gateway/media_delivery.py:194-234`)
and metadata stripping (`gateway/media_delivery.py:174-191`) run after the checks. Cache rules
(RT 7):
- There is no ETag, 304 or server-side byte cache today (none found). Any
  later one must sit after these checks; the inventory test lists HEAD
  routes, so a new HEAD handler cannot skip them.
- `Cache-Control` stays `private, max-age=3600` for `g=o`
  (`gateway/media_delivery.py:194-209`). `private` keeps shared caches out. For
  `g=s` it is `private, no-store`, so a stopped share does not keep
  rendering from the recipient's browser cache.

**What "lose access now" means** (copy §7): the next fetch fails. Bytes
already downloaded, cached or in transit cannot be recalled. A `g=o` URL is a
bearer capability until it expires (at most 1 hour, `proxy.py:947`), and it
is minted only for Own access. Revoking a key does not revoke a `g=o` URL
already minted with it; the URL expires within the hour.

**Metadata stripping is tested, not assumed.** Stored bytes are stripped on
write (`proxy.py:7094`) and stripped again on delivery unless `md=full`. The
test puts a canary in the metadata of every container the stripper handles
(`gateway/media_metadata.py:498-531`): PNG, JPEG, WebP, WAV, FLAC, MP3, MP4, WebM,
MKV, OGG and M4A. It asserts that a `g=s` fetch does not contain the canary.

Every minting site takes an `Access`, so a URL cannot be minted without one:
- `build_signed_asset_url` (`proxy.py:7257`) and `build_signed_image_url`
  (`proxy.py:7277`);
- MCP results (`_mcp_store_asset`, `proxy.py:14653`), with `Access=Own` (§3.2);
- generation responses on `/v1/images/*` and `/v1/videos/*` (the caller is
  the maker: `o`);
- the Library download route, after `require(…, "link")`; a poster is minted
  with its parent's access.

### 3.5 App bindings and immutable identity (step 2; RT3 B1, B2, M6)

An App key or trusted address grants App content access. Both must obey O4.
Main still permits owner-or-accountable key issuance
(`gateway/admin_runtime.py:358-370`, `gateway/admin_runtime.py:620-634`)
and owner-only address issuance on any non-session App
(`gateway/admin_api.py:2096-2117`, `gateway/admin_store.py:2065-2098`).

**Caller check.** Binding additions require an authenticated person whose
id equals `owner_user_id`, returning `403 not_your_app` otherwise. Remove
the owner-role exception from binding issuance at
`gateway/admin_runtime.py:366`; retain owner operational powers for plans
and disabling Apps. If the existing helper remains for those powers, key
and address additions use a separate accountable-only check. Never write
an accountable id on behalf of a control-plane key to pass a trigger.

| Path | Current-main evidence | Step-2 requirement |
|---|---|---|
| Admin App keys | `gateway/admin_runtime.py:620-634` | Accountable person only; owner automation key cannot mint |
| Own App keys and Try | `gateway/user_runtime.py:391-427`, `gateway/user_runtime.py:472-495` | Preserve accountable caller, session and key provenance checks |
| Invite acceptance | `gateway/accounts.py:583-586` | New person's own App and key; retain explicit provenance |
| First App setup | `proxy.py:17610-17681` | Session branch requires accountable-person issuance. Control-plane-key branch (`proxy.py:17671-17680`) becomes 403; use owner sign-in or the documented owner CLI bootstrap below |
| Trusted-address add | `gateway/admin_api.py:2096-2117`, `gateway/admin_store.py:2065-2098` | Replace owner-role gate with accountable-person gate; add a `/me` adapter so a non-admin accountable person can add their own address |
| Revocation/address removal | `gateway/admin_store.py:2101-2110` | Existing management authority may narrow access; it cannot add a replacement binding |
| Admin-key creation/adoption | `gateway/admin_runtime.py:650-682`, `gateway/signin_admin.py:485-499` | Control-plane only; do not apply App credential immutability to these rows |
| Test setup minting | `web/e2e/global-setup.ts:95-103`, `tests/e2e/contracts.py:144-145` | Sign in as the App's accountable person instead of using an owner automation key |

**Headless first run.** Step 2 documents an **owner CLI bootstrap** for a
host operator to establish the owner and issue a binding with that owner's
explicit identity and provenance. It is an owner bootstrap, never an
App/control-plane credential borrowing the owner. The alternatives are
owner sign-in or that documented CLI path; the removed control-plane-key
setup branch returns 403. No CLI implementation is claimed to exist today.

**Schema.** AP already supplies O1 (`gateway/admin_store.py:143-154`). No
second accountable-person migration or removed column is added. The
following pseudo-SQL defines the remaining triggers; comparisons are
NULL-safe and credential App ids use `COALESCE(NULLIF(app_id, ''), subject_id)`
as main does (`gateway/admin_store.py:2475`).

```sql
-- O2: fixed kind and fixed personal-App person after cut-over.
BEFORE UPDATE OF kind ON apps WHEN NEW.kind IS NOT OLD.kind -> ABORT
BEFORE UPDATE OF owner_user_id ON apps
  WHEN OLD.kind = 'personal' AND NEW.owner_user_id IS NOT OLD.owner_user_id -> ABORT
-- O3: no transfer with any binding, including an expired but unrevoked key.
BEFORE UPDATE OF owner_user_id ON apps
  WHEN NEW.owner_user_id IS NOT OLD.owner_user_id AND (
    EXISTS (SELECT 1 FROM credentials WHERE subject_kind = 'app'
      AND COALESCE(NULLIF(app_id, ''), subject_id) = OLD.id AND revoked_at IS NULL)
    OR EXISTS (SELECT 1 FROM app_trusted_addresses WHERE app_id = OLD.id)) -> ABORT
-- O4: accountable provenance for additions.
BEFORE INSERT ON credentials WHEN NEW.subject_kind = 'app' AND (
  NEW.created_by IS NULL OR NEW.created_by IS NOT
    (SELECT owner_user_id FROM apps
     WHERE id = COALESCE(NULLIF(NEW.app_id, ''), NEW.subject_id))) -> ABORT
BEFORE INSERT ON app_trusted_addresses WHEN NEW.created_by IS NULL OR
  NEW.created_by IS NOT (SELECT owner_user_id FROM apps WHERE id = NEW.app_id) -> ABORT
BEFORE UPDATE OF subject_kind, subject_id, app_id, created_by ON credentials
  WHEN OLD.subject_kind = 'app' AND (
    NEW.subject_kind IS NOT OLD.subject_kind OR NEW.subject_id IS NOT OLD.subject_id
    OR NEW.app_id IS NOT OLD.app_id OR NEW.created_by IS NOT OLD.created_by) -> ABORT
-- App-key revocation is final; expiry alone does not count as revocation.
BEFORE UPDATE OF revoked_at ON credentials
  WHEN OLD.subject_kind = 'app' AND OLD.revoked_at IS NOT NULL
    AND NEW.revoked_at IS NOT OLD.revoked_at -> ABORT
BEFORE UPDATE OF app_id, address, created_by ON app_trusted_addresses
  WHEN NEW.app_id IS NOT OLD.app_id OR NEW.address IS NOT OLD.address
    OR NEW.created_by IS NOT OLD.created_by -> ABORT
BEFORE DELETE ON apps -> ABORT
```

The route proves the caller; the INSERT trigger proves stored provenance
atomically. Session Apps continue refusing both forms of binding. Direct
SQL may not turn a control-plane credential into an App credential without
the same insert provenance and non-session checks; implementations must
close that UPDATE path too, or refuse that subject transition entirely.

**Install last.** Delete the startup creator back-fill and its call
(`gateway/admin_store.py:550`, `gateway/admin_store.py:612-655`); do not run
it one final time or infer missing provenance from receipts. Unit 1 revokes
unproven keys and deletes unproven addresses (§8.1). Run all schema
migrations, including AP's owner updates
(`gateway/access_plan_migration.py:527-531`), before installing triggers.
Fresh databases use the same post-migration installation path. Replace
old trigger definitions with DROP then CREATE, compare `sqlite_master`
SQL, and run rolled-back forbidden-write self-tests before traffic. Test
schema v6→current as well as current→enforced and fresh initialization.

The `OLD.subject_kind='app'` scope is essential: sign-in administration
legitimately adopts control-plane keys by updating `created_by`
(`gateway/signin_admin.py:485-499`, `gateway/signin_admin.py:549`).

A future service-App transfer first revokes every key and deletes every
trusted address in one transaction, then changes the person and discloses
that all past App content follows. v1 has no transfer route. Past spend
remains with its submission subject; open responses follow §2.4.
Revoked hashes remain read aliases (`gateway/ownership.py:85-94`), never
live authentication. Reissuing requires a new key, not unrevocation.

### 3.6 Logs and side stores

- **MCP tool failures** (`gateway/mcp_server.py:687-688`): print the exception class
  and a correlation id, never `{exc}`. The client already gets a generic
  error.
- **The 17 current `print(f"…{exc}")` lines.** The build lists them. It fixes
  the ones on request paths that carry member input: provider adapters, job
  runners and MCP. A ratchet test fails CI if the count rises. Logs are host
  files (L11), so the honest-limits sentence covers them. This step only
  stops avoidable copies.
- **Canaries reach the exception paths.** The §9.2 canary test includes a
  failing MCP call and a provider error that echoes the input, and it scans
  stdout and stderr.
- **LiteLLM**: pin `litellm_settings.turn_off_message_logging: true` and
  `general_settings.store_prompts_in_spend_logs: false` in the generated
  config (`gateway/litellm_substrate.py:104-118`). Its behavior is a
  deployment gate, UNVERIFIED until tested on the deployed version.
- **Reverse proxy (Traefik)** may log `sig=` query strings. That is outside
  this repo and a deployment gate. The ops note says to log paths without
  query strings for `/v1/assets/` and `/v1/images/`.
- `cache/key_activity.jsonl` (`firehose_metrics.py:72-86`) already strips prompt
  fields. It stays operational.

### 3.7 Removed query parameters fail closed

Today `_scope_requested` checks truthy values (`gateway/me_media.py:757-759`), so
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
title (`gateway/me_media.py:1131-1163`, `gateway/me_media.py:1168-1225`, `gateway/me_media.py:1618-1668`; RT 3). So a shared item
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
  sidecar, not from the own-receipt filters (`gateway/me_media.py:993-999`,
  `gateway/me_media.py:1072-1073`). So cache-only shared items are not dropped.
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
(`gateway/admin_store.py:335-338`, `gateway/accounts.py:503-505`). UB §3.4 shows "Password changed {time}." in
Account → Security at the next sign-in. v2 amends it:
- When the redeemed link was issued by someone else, the event reads
  "Password changed {time} with a reset link from {Owner}." It is joined from
  the redeemed link's `created_by`. The redemption audit
  (`gateway/accounts.py:596-612`) does not record who redeemed the link, so the copy
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

## 6. Storage (step 4)

ST recommends per-person storage inside Usage, a private cleanup view for
each member, no quota by default, and a protective pause on new work when the
disk is nearly full (ST §12). This design takes the smallest version of
each. Nothing counts storage per person today. The speech-to-text
free-space check guards its spool (`gateway/stt_chunking.py:784-865`).

### 6.1 Owner: Storage in Usage (owner section)

- **Disk.** One line per filesystem that holds Azizam data (the creations
  folder, `data/`, job spools and private temp), merged when they are the same device:
  free bytes, total, percent used (description only), the reserve in bytes,
  measured time, and state Healthy, Low, Paused or Unknown (§6.4).
- **People.** One row per person: name, stored bytes, item count, sorted by
  bytes. Tapping a row shows the split by kind: pictures, videos, audio,
  voices, other; kept request history has a separate bytes-only row. No limit column (§6.5).
- **Service Apps.** One row per service App: App name, its accountable
  person, bytes. They are not added to the person's row (ST §12).
- **Not linked to a person.** Bytes no App accounts for (Unresolved, §10.2
  question 2), and bytes kept only by a legacy owner key that maps to no App.
  Never spread across people.
- **Gateway data.** Databases and caches, as one separate line.
- **Kept request history (RT3 M4).** Count retained history bytes per maker,
  separately from creations and shared App bytes. The owner sees bytes
  only: no history count, item id, model, title, date or link. This row
  waits for RR slice 5 as well as step 4.
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
- **Kept request history (with RR slice 5).** The member's own "Review
  storage" includes their retained history, largest first, with byte size
  and Delete using the same maker authorization and erasure fences. Owner
  storage offers no history drill-down or deletion of another person's item.
- After deletion the number is measured again, not computed.

### 6.3 Accounting

- **Person's stored bytes** (logical): the sizes of creation files whose
  sidecar lists one of the person's App principals, plus their voice folders
  (`gateway/custom_voices.py:103`), plus the maker's kept-history bytes
  when RR slice 5 exists. Its owner projection is bytes only. Sharing adds
  nothing.
- **Files with several owners** count in full for each owner. The server
  disk line counts them once. Reclaim estimates exclude them (§6.2).
- **Deletion releases a claim (RT3 M8).** Main keeps actor/asset-id audit
  tombstones (`gateway/me_media.py:1299-1322`); sidecars store no claim
  timestamp (`proxy.py:7088-7115`). Do not compare claim and deletion
  times. Under `CACHE_MUTATION_LOCK` (`gateway/me_media.py:684-704`), remove
  the caller's intersecting readable principals from `owner_key_hashes`
  and the caller from `maker_person_ids`, revoke their shares, and remove
  bytes/sidecar only if no owner remains. After release the removed
  principal itself hides the item; drop only audit-tombstone-based list/detail hiding.
  Audit events remain history and never suppress a future new claim to
  identical bytes. A fresh generation adds its principals under the same
  lock (`proxy.py:7088-7115`), so the item can reappear without a timestamp.
  The deleter's App keys then lose read/reuse while surviving co-owners keep
  access. If a service App principal is removed, confirmation adds
  **"This also removes it from {App}."** List each affected service App;
  a session's `owner_keys` includes both App kinds (O6).
- **Job-scoped hides stay unchanged (round 4 M8).** Keep
  `media_hidden_assets(person_id, asset_id, job_id)` and its writer/readers
  (`gateway/prompt_memory.py:960`, `gateway/media_jobs.py:800-810`,
  `gateway/me_media.py:356`, `gateway/me_media.py:1028`). The same bytes from
  another job are a new item. Claim release replaces audit-tombstone hiding
  only; it does not replace these per-job hides.
- **One-time pre-v3 cleanup (RT3 M8).** Preview existing
  `media.asset.delete` events by actor and asset. Release only that actor's
  personal-App principals and their historical aliases, plus `user:<id>`;
  do not replay the actor's current full session `owner_keys`, which also
  contains service Apps (`gateway/admin_store.py:2144-2152`,
  `gateway/me_media.py:1257-1275`). Keep service-App and other owners'
  claims. Apply on stopped state with an idempotent manifest/version, then
  never replay old tombstones against newly created claims. With no claim
  time, cleanup cannot distinguish a later pre-cut-over identical
  generation; the concrete preview must show every claim it releases.
- **Library/history cascade (RT3 M2; RR §3.1).** Library delete removes the
  deleting maker's Library prompt and linked request-history records in
  one `media_jobs.db` transaction, using RR erasure cutoff/fence checks
  (`docs/design/REQUEST_RETENTION.md:126-131`,
  `docs/design/REQUEST_RETENTION.md:398-405`,
  `docs/design/REQUEST_RETENTION.md:1267-1271`). Other makers' prompts,
  history and bytes are preserved. Claim release and file mutation are
  serialized under the cache lock; they are not falsely described as part
  of the SQLite transaction. Use a durable idempotent deletion intent
  written with the content-row cascade, then apply sidecar/file release
  and mark complete; replay unfinished intents before accepting new claims
  after restart. The RR writer fence (`docs/design/REQUEST_RETENTION.md:1267-1271`)
  checks both the durable deletion intent (the released claim) and the
  job-scoped hide, preventing a concurrent history/prompt write from
  resurrecting deleted content. A creation delete cannot leave
  a second kept copy of the same maker's words behind. This cascade waits
  for RR slice 5; the implementation gate tests its crash boundaries.

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
says nothing about Azizam's need. The v3 worktree measurement on 2026-10-11 was 1.45 TB
total, 79.5 GB available, 95% used; v4 measures 101.6 GB available (§0). v2's 5% reserve is 72.6 GB, so v2 would
have shown Low on deploy and paused after about 7 GB of growth by other
software, while Azizam needs a few GB. v3 reserves **bytes**, sized from the
work Azizam admits.

**One byte ledger per process and device (RT3 M5).** Main's
`SttResourceGate` has a separate `_reserved_bytes`, assumes one event loop
and probes `spool_dir` only (`gateway/stt_chunking.py:762-868`), with a
64 MiB floor (`gateway/stt_chunking.py:369`). Step 4 replaces its disk
counter with the same shared per-device lease ledger used by every other
writer. Keep STT concurrency policy separate, but never account its bytes
in a second independent disk ledger.

STT upload, decoded workspace, segment/conversion and replay reservations
all count in `leased(d)`, including top-ups after probing. STT obeys
`reserve_bytes` and the shared Healthy/Low/Paused state; its additional
configured floor may only narrow admission (use the stricter floor), not
bypass the reserve. Check-and-lease/top-up/release are under the same
process-wide `threading.Lock`, including thread and event-loop callers.
A reservation is counted once, even if STT and a job refer to the same
workspace.

Measure the private temp directory as well as `data/`, creations and
explicit job/STT spools. Relative STT spool lives under private temp
(`gateway/stt_chunking.py:318-333`). Bare-metal default private temp is
beside admin.db (`gateway/private_tmp.py:35-42`); Docker explicitly places
it on image storage (`Dockerfile:26-27`), which may differ from the data
volume. Resolve paths and merge only by device id; do not infer devices
from the checkout measurement. Re-measure each before step 4.

**Definitions**, per filesystem *d* that Azizam writes (the creations
folder, `data/`, job spools and private temp; merged when they are the same device):
- `peak_bytes(job)`: the most bytes one job can add to *d* before it ends:
  stored upload or input copy, output, temporary and conversion copies,
  derivatives (posters), and its database and WAL growth. Each
  storage-writing job kind (image, video, audio, speech, voice, upload, kept history)
  declares a bound in config, with a code default taken from its existing
  limits (for example the voice upload limit, `gateway/custom_voices.py:49`, and the
  512 MiB MCP result limit, `gateway/mcp_server.py:91`). The inventory test (§9.2)
  fails CI for a storage-writing kind with no bound, so no unbounded kind
  ships. The bound for each video workflow is UNVERIFIED; the build measures
  the largest stored output of each kind and sets the default above it.
- `W_max`: the largest `peak_bytes` of any kind the server admits.
- `leased(d)`: the sum of `peak_bytes` of the admitted jobs that have not
  ended, including STT upload/workspace/replay leases. A job takes its lease at admission and returns it when it ends
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
  comparing, and adding, extending or releasing every device lease. STT
  uses this same lock and ledger; it cannot admit against an independent
  counter. Acquire all device leases atomically or none.
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
- **What still works (RT3 M4):** sign-in, browsing, downloads, deletion and
  requests whose only optional write is kept history. When Paused/Unknown,
  skip that history write with receipt reason `storage_unavailable`;
  return the model response normally. Do not fail or refuse the request
  just because default-on history cannot be saved. This is an optional
  retention write, not admission of a required upload/creation/STT spool.
  No queue or background retry may later save that skipped content.
- Nothing is ever deleted automatically (P19).

On this server, as an illustration only: if `W_max` were 1 GiB, the reserve
would be 3 GiB, against 101.6 GB available at the v4 worktree measurement, so the state on deploy would be
Healthy. The real `W_max` is UNVERIFIED until the build measures it.

### 6.5 No quotas in v1

ST's default is no personal quota, and Immich's quota is empty (unlimited)
by default. A quota is a ceiling, not free space, so it would not prevent a
full disk; §6.4 does that. The existing voice limit stays
(`gateway/custom_voices.py:46-51`). An optional per-person hard cap is ST's next
step (warning at 90%, refuse new storage at 100%, never delete). It is
deferred until a household asks for it.

## 7. Member wording

The copy follows VOICE.md: fact first, one next action, plain words, each
fact once per screen. `{Owner}` is the owner's display name.

**Each row ships with the step that makes it true (RT2 M6).** The "Step"
column names the build step (§12). A row never renders before its step; a
row with two variants changes when the later step ships.

History wording in Home, Privacy, honest limits and owner Usage is the
RR slice-5 variant: render the words about saved request history only when
step 3 AND RR slice 5 enforce it. Before then use the same rows with that
phrase omitted. Storage and sharing still wait for steps 4 and 5.
Navigation follows the same rule: "Manage sharing" and "Review storage" do not exist before
their steps.

| Where | Step | Exact copy |
|---|---|---|
| Home, first sign-in for a member; once for existing members after the switch | 3 | Title "Your work is private". Body, step 3: "{Owner} sees your usage and spend, not your creations, prompts or saved request history. Shared apps without your person identity keep request content only when explicitly asked, for that app’s own continuation; it never appears in a person’s history." From step 4: "{Owner} sees your usage, spend and storage, not your creations, prompts or saved request history. Shared apps without your person identity keep request content only when explicitly asked, for that app’s own continuation; it never appears in a person’s history." Link "What {Owner} can see" (Account > Privacy). Button "OK" |
| Account > Privacy | 3 | Heading "Privacy". Rows: "Private to you: creations, prompts, saved chats and saved request history." / "{Owner} sees: when you made something, the model, the app you used, how long it took and what it cost." (from step 4, ending "…what it cost and your storage.") / "Shared apps: creations belong to the app, and its responsible person can see and use them. Without your person identity, explicitly stored request content stays available only through that app’s credentials, never in a person’s history." / from step 5 only: "Shared: only what you share, with the people you choose. Prompts only if you include them." → "Manage sharing". Then the honest-limits sentence |
| **Honest limits** (Account > Privacy; "What is saved?" in Try) | 3 | "{Owner} runs this gateway and can read the files it stores, including saved request history. {Owner} can also reset your password and sign in as you; Account > Security shows when a reset happened. Azizam keeps your work out of the owner's views, but it isn't end-to-end encrypted. The engine or provider that makes your work receives your prompt." |
| Account > Security, after a reset someone else issued (§5) | 3 | "Password changed {time} with a reset link from {Owner}." A self-issued reset: "Password changed {time}." |
| Library detail | 5 | Button "Share" |
| Share sheet | 5 | Title "Share this {picture\|video\|audio}". List "With", with each person's name; the owner's row reads "{Name} (owner)". Switch "Include prompt", off by default. Note when off: "They see the {result} and its model. Not your prompt." When on: "They also see: “{prompt}”". With no saved prompt the switch is disabled: "Prompt not saved, so it can't be shared." Footer: "They can download it." Primary "Share with {Name}" / "Share with {n} people" |
| Share refused | 5 | `several_makers`: "Someone else made the same file, so it can't be shared." `file_type`: "GIF files can't be shared yet." `no_maker`: "Work made by a shared app can't be shared." |
| After sharing | 5 | The sheet closes. The detail shows the badge "Shared with {Name}" or "Shared with {n} people" (no toast, P2); tap opens the list |
| Stop sharing | 5 | "Stop sharing with {Name}? They can't open it after this. Copies they downloaded stay with them." Buttons "Stop sharing" / "Cancel" |
| Recipient | 5 | Library filter "Shared with me". Caption "From {Name}". Action "Remove from my Library", note "{Name} still has it." Empty state "Nothing is shared with you yet." Stale open "{Name} stopped sharing this." |
| Recent | 5 | "{Name} shared a {picture} with you." |
| Owner Usage, per-person view (once, under the person picker) | 3 | "Creations, prompts and saved request history stay private to each person." |
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

### 7.1 Incognito: one control (step 3 with RR slice 5; RT3 M3)

D50 replaces "Don't save this prompt" with **Incognito**, for a request or
a whole chat. Extend Try's existing `forget` state and sessionStorage
control (`web/src/app/try/page.tsx:1325`,
`web/src/app/try/page.tsx:1413-1414`) rather than add a second switch.
One choice suppresses `library_prompt`, `request_history` and
`conversation` writes; the response still reaches the caller and only the
structural receipt remains for the request. Explicit Debug capture must
also obey incognito and never restore a suppressed content copy.

| Surface/state | Exact copy (RR slice-5 gate) |
|---|---|
| Try/chat switch | "Incognito" |
| Off | "Request history is saved privately. You can review or delete it." |
| On | "This request's prompt, history and conversation won't be saved. A request record with time, model and cost remains." |
| Whole chat | "Incognito is on for this chat. Its prompts, history and conversation won't be saved." |
| App/key setting | "Use Incognito for requests from this app/key." Only the accountable person edits it (O4b) |
| What is saved? | "Incognito stops Azizam saving request content. The engine or provider still receives it and follows its own rules. Your request record remains. Returned results can contain words from your prompt." Then honest limits (§7) |

The same semantic choice applies to Try, chat, per-App/per-key defaults
and `extensions.com.azizam.prompts: {"mode":"forget"}` on each data-plane
request. A request may narrow a saving default to incognito; it cannot
undo a stricter App/key or household `never_keep`. Household controls may
only narrow. No owner/admin override can save content for someone else.

Update UB §4's prompt-only wording when this behavior ships, not earlier.
PM's viewer table gains "Share recipient: prompt only if included; never
workflow download"; its App-access statement points to `content_access`.
RR's default for request history is on (D50), subject to a person and all
narrowing/failure checks; explicit no-person App-only continuation follows
§3.1 and is never added to a person’s history. There is no independent RR reader or reader
widening setting (D51).

## 8. Data and migration

- **No content moves.** Visibility is computed by the core, so existing
  creations, prompts, chats, receipts and jobs are private to the people who
  own them (§2.4 O6) from the first start.
- **Removed** (P11, no aliases): the `scope`, `owner` and `person` parameters
  on `/me/*`; `"scope": "instance"`; `_audit_everyone` and its four actions;
  `receipt.detail.instance`; `recent_videos`, `recent_images`,
  `recent_renders`; the `admin` job and event audiences; the Library person
  picker and the Usage person picker outside the owner section; the
  `scope=everyone` web calls (`web/src/app/library/page.tsx:278-279`,
  `web/src/app/library/page.tsx:723`, `web/src/app/library/page.tsx:537`, `web/src/app/library/page.tsx:552`,
  `web/src/app/library/page.tsx:1049`, `web/src/app/library/page.tsx:1103`, `web/src/app/library/page.tsx:1814`, `web/src/app/library/page.tsx:1837`, `web/src/app/library/page.tsx:2481`); signed URLs without `md` or `g`;
  the `created_by` startup back-fill (`gateway/admin_store.py:550`,
  `gateway/admin_store.py:612-655`). AP already removed service-account
  ownership of Apps; this release does not recreate or migrate that column.
  Audit rows already written keep their labels
  (`web/src/lib/audit-description.ts:4-5`).

**One protocol, two units.** Extend AP v4's §4.2 stop/backup/marker,
migrate/verify/accept protocol; do not introduce another framework. Main's
AP schema migration already exists (§1.3). OV adds separate version rows
for its two units; AP's schema/version alone proves neither is complete.
For an accepted OV version, a repeat run is verify-only. An interrupted
unit resumes from its durable manifest while the started-but-unaccepted
marker continues blocking traffic. Backups cover affected sidecars as
well as databases (§8.2).

### 8.1 Unit 1: enforce App bindings on AP v4 (step 2; RT3 B1, B2, M6)

Main already has accountable people, the users FK, explicit plan
assignments and service-default App creation (§1.3). This unit enforces
O2/O3/O4; it does not reassign service Apps or ask for a new person approval.
AP being installed is not a verify-only shortcut: this unit has its own
version and enforcement checks.

1. **Preview**, on the stopped gateway's copy:
   - Every App: id, name, kind, existing accountable person, plan assignment,
     every key and trusted address with recorded creator and last use.
   - Keep the row for a personal App created by someone other than its
     owner (excluding session Apps). Default is "keep personal"; an explicit
     personal→service correction keeps the same person and is applied
     before fixed-kind triggers. Never offer service→personal.
   - "No person recorded", only for proven-unbound records (§10.2 Q2):
     `kind=service`, gateway owner accountable, `access_plan="person"`
     (required by `gateway/admin_runtime.py:399-401`). Run the same
     proven-unbound classification shown in unit 2 before creating this
     App. No binding is added.
   - Every unrevoked key and every address whose `created_by IS NOT
     owner_user_id`, including NULL and empty string; list revocations and
     address removals separately, and show surviving bindings. Do not use
     a last back-fill or infer creators from usage.
   - Block apply for any App used in the last 30 days that would lose all
     bindings. Use key `last_used_at` and trusted-address request evidence;
     if address-use history is unavailable, block rather than call it
     unused. The accountable person supplies a proven replacement before
     the stopped apply, or the owner explicitly approves that the App stops.
   - `service_account:…` content principals remain Unresolved, granted to
     nobody. AP has already removed service-account ownership of Apps.
2. **Owner accepts** the concrete revocation/correction preview. There are
   no service-account-to-person approval rows (RT3 B2; Q5 removed).
3. **Apply**, after all existing migrations, in one `admin.db` transaction:
   approved personal→service corrections; revoke unproven App keys; remove
   unproven addresses; create "No person recorded" if needed with the
   explicit plan assignment; install §3.5 triggers; write
   `ownership_version=1`. Delete the startup back-fill in this release.
4. **Verify**: FK/NOT NULL and plan checks; zero unrevoked App keys or
   addresses with unproven provenance; exact surviving bindings and kind
   corrections; recently used Apps have a live binding or an approved stop;
   trigger SQL matches the release. In a rolled-back savepoint, refuse a
   NULL person, kind/personal-owner change, service-owner change with a key
   or address, bad-provenance INSERT, App-key identity rewrite, unrevocation
   and App deletion. Prove control-plane adoption still works. Exercise
   fresh, current and schema-v6 upgrade paths before marking verified.

### 8.2 Unit 2: the visibility switch (step 3; RT3 N3)

Runs after unit 1 in the same tool run, or later.

1. **Preview**: `superadmin` role rows; records with no person and no App,
   in the three classes of §10.2 question 2; creations and bytes per person;
   free bytes against the reserve (§6.4). Also list each key that read
   cross-App content in the last 30 days, with its App, accountable person
   and permission-off post-state; the accountable person can enable
   `privacy_permission` per key through a browser session. No automatic
   grant for recent use; unavailable evidence must be named, not treated
   as proof of no use.
2. **Owner approves.**
3. **Apply**: map `superadmin` rows to `owner` or `admin`. Attachment is
   an idempotent, re-runnable sidecar operation, outside the `admin.db`
   transaction: persist the accepted asset-id manifest with pre/post owner
   sets before writing, atomically replace each affected sidecar, and skip
   an already-correct post-state on resume. Refuse an unexpected state
   instead of overwriting it. AP §4.2's backup includes every affected
   sidecar so restore reverses both stores before traffic. Leave Unresolved
   records ungranted. Only after all attachments verify, write
   `visibility_version=1`; acceptance remains blocked until then.
4. **Verify**: zero `superadmin` rows; every manifest sidecar matches its
   accepted post-state and is owned by "No person recorded"; unresolved
   records unchanged; `ownership_version=1`. Test crash before/after each
   atomic file replacement, replay, unexpected sidecar edits and restore
   of sidecars plus database. Neither a version row nor an SQLite commit
   alone proves the attachments finished.

The `shares` table is not part of the switch. It is new, holds no existing
data, and is created by the sharing release (§12 step 5) with its normal
schema code. Before that release the core has no Shared basis, so a `g=s`
URL is refused like any unknown grant.

**Fail-closed start.** The gateway refuses to start when:
- the marker says a unit started but was not accepted (AP §4.2);
- the release needs `ownership_version = 1` or `visibility_version = 1` and
  the row is missing;
- a removed auth-disabled setting is supplied; current main already rejects
  it (`proxy.py:2114`), so this is not a future AP dependency.

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
| Own retention ledger (step 3) | `GET /gateway/v1/requests/{id}/retention/ledger`; `/me` UI adapter | Request ledger command in HR CLI, when built | `requests {action:"ledger", request_id}`: same content:receipt:read |
| Owner retention ledger (step 3) | Existing `/admin/api/requests/{id}/retention/ledger`, projection only | Same projection | `requests` owner operational branch uses projection |
| Current ledger, no id (step 3 inventory) | Existing `/admin/api/me/retention/ledger`, config facts only | Current-facts command when built | `requests {action:"ledger"}`, `none` |
| History/Debug (step 3 with RR) | RR maker routes under `/gateway/v1/requests`, §3.1 and narrowing | RR CLI when built | RR requests operations use the same core and privacy permission |
| Own App address add (step 2) | `POST /me/api/apps/{id}/trusted-addresses`; admin adapter has same accountable check | Own-App binding command when built | Not supported |
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
  below the reserve for required storage (§6.4). Optional history skips
  its write with `storage_unavailable`, without failing the request.
- `400 reserve_below_largest_job` when `reserve_bytes` is below `W_max`.

There is no CLI in this repo today. The CLI rows depend on HR Release B
item 4.

### 9.2 Tests (the oracle)

The test section is `docs/design/OWNER_VISIBILITY_TESTS.md`: TD, adopted
and revised through v4. `[v3]` and `[v4]` mark revision requirements. Its
current-main source map is §10; old review offsets no longer apply. It
defines actors A, B, O, D, S, T (trusted address), N, Rk, Rs, Q, F, C, H
and U, the states, eight kinds and six actions, an independent oracle,
whole-response assertions, paths P01-P21, canary tests, and migration,
STT/storage, history and incognito regressions. This design is authoritative
if the two files disagree; fix the test design before implementation.

Normative rules taken from TD:
- Every resource denial is exactly `404 not_found`, the same body and
  headers as a nonexistent id.
- A denial happens before any side effect: no file read, no mint, no
  provider call, no event sequence repair, no store write (spies assert it).
- Tests call the production `Viewer` builder; a hand-built `Viewer` is
  allowed only in pure core tests.
- No member notice or Privacy copy ships because tests were designed; it
  ships when the step's gate passes on an implementation SHA (§12).

Required implementation additions (included in TD v4):
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
   member first-use notice shows once; no row of §7 renders before its step
   or its RR slice-5 variant gate.
5. **Round 3**: TD P07/P15/P19/P21 and §7 cover trusted bindings, ledger
   ownership/projection, retention permissions, incognito/O4b, shared STT
   leases, optional history failure, claim release and sidecar replay.

Rewrite the §1.5 specs to these expectations. Do not delete them.

## 10. Risks and open questions

### 10.1 Risks

| Risk | Mitigation |
|---|---|
| Members read "private" as protection from the host | The honest-limits sentence names host files and resets, one step from every private claim. It never says "only you" |
| Metadata still reveals behavior (models, times, frequency) | Disclosed in Privacy. The projection keeps typed quantities only |
| A route or MCP operation added later skips the core | The inventory test fails CI for any unclassified entry |
| A future route moves a personal App and its history | `admin.db` triggers (§3.5); NULL-safe, so no route can pass through NULL |
| Existing binding provenance is unproven | Unit 1 revokes keys and removes addresses without accountable-person provenance; recent use blocks apply if no binding survives (§8.1). Transfer needs zero bindings (O3) |
| Live bearer URLs after a share stops | `g` is checked on every fetch; downloaded copies cannot be recalled, and the copy says so |
| An owner who relied on Everyone loses visibility of a child's work | Plans prevent; supervision is deferred with a fixed direction (§2.3) |
| Logs hold exception text | §3.6 fixes the request-path sinks and ratchets the rest; logs are host files (L11) |
| Reverse-proxy logs hold signed URLs; LiteLLM logging defaults | Deployment gates, UNVERIFIED |
| A hand restore of a database brings back stopped shares | Stated limit, as for keys in AP §4.2; the ops restore note stops every share before start (§8.3) |
| Removing the listed bindings breaks an app that uses one | The preview names each key/address and its App; an App used in the last 30 days that would lose every binding blocks apply until it has a replacement or the owner approves that it stops (§8.1) |
| The disk pause starts on deploy | The reserve is bytes sized from Azizam's own largest job (§6.4), not a share of a large shared disk. The preview reports free bytes against it |
| Another program fills the disk after a job is admitted | The promise is "budgeted", not "guaranteed"; the job ends `storage_full` (§6.4) |
| A person who is accountable for service Apps is removed | Removal disables their Apps and revokes the keys (`gateway/accounts.py:674-680`); O8 then denies the Apps. With no transfer route in v1, those Apps' work stays on disk under a removed person and nobody can open or free it in the product. Known limit; the transfer route (O3) is its fix. Until then, remove such a person only after deciding about their Apps |
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
     person and `access_plan="person"` (AP v4;
     `gateway/admin_runtime.py:399-401`). They appear in your Library, where you can keep or delete each
     one (P19).
   - **Resolvable**: a stored owner key or `maker_person_ids` maps them to
     an App or person through today's aliases (`gateway/ownership.py:46-55`). Not a
     new assignment; the core already reads them that way.
   - **Unresolved**: everything else. Nobody is granted access, nothing is
     deleted, and the bytes show under "Not linked to a person" in Storage
     (§6.1). They stay listed until you resolve them.
3. **Share expiry.** **Default: no expiry.** Ordinary shares (Immich,
   Nextcloud, OneDrive) do not expire. The badge keeps a share visible, and
   the maker can stop it at any time. RT agrees.
4. **Disk reserve (re-measure before step 4).** **Default: `reserve_bytes = W_max + 2 GiB`**, an
   absolute amount (§6.4), not v2's 5%. With 101.6 GB available at the v4 worktree measurement
   (measured, §0), this does not pause on deploy unless `W_max` is above
   about 99 GB on that same device, before concurrent leases. This is not a
   deploy prediction: the live data/scratch devices and `W_max` still need
   measurement. Change it only if you want a larger margin for the other
   software on this disk.

## 11. Red-team resolution

### 11.1 Round 1 (v1 at `4a020412`)

Resolutions as applied in v2. "Applied" means the design text changed; code
evidence is in the cited section. v2's "§9.2 item n" references now point to
the adopted test file (§9.2). Where round 2 changed a resolution, the last
column says so.

| RT | Finding | Resolution (v2) | Where | Changed by round 2 |
|---|---|---|---|---|
| 1 (blocker) | The one core replaces resource-specific identity and action rules with an App intersection | **Applied.** Typed `ContentRecord` kinds and an action matrix inside the one module. Each kind keeps today's rule: App for creations (`gateway/ownership.py:46-55`), person for prompts (`gateway/me_media.py:1026`), person and App for chats (`proxy.py:25886-25888`), principal folder for voices. Shared never satisfies a mutation. Identity entry for sessions, personal keys (`gateway/admin_runtime.py:2312-2322`), makers and person-less Apps stated. Negative tests added | §3.1, §9.2 | M1: the Accountable basis is removed; one App-ownership rule (O6) |
| 2 (blocker) | Key issuance is not a complete boundary: App ownership can change; key provenance unknown | **Applied.** All issuance paths enumerated (rotate and import do not exist). Triggers fix App kind and personal ownership; a service App owner change needs zero bindings (RT3 B1). Transfer is a content transfer with disclosure. Keys with `created_by` not equal to the owner, NULL or `""` are revoked. Read aliases stay; revocation ends authentication | §3.5, §8 | B1, M2: ownership cut-over (§8.1), NULL-safe triggers, issuance in the database |
| 3 | Shared output needs its own serializer | **Applied.** `shared_view()` typed projection, applied before title, search and filter; list built from `shares`, not the own-receipt filters; title never from the prompt | §4.2, §9.2 | — |
| 4 | The allowlist is not recursive; envelopes add fields back | **Applied.** Typed schema for every leaf, nested `cost` and `resource_decision` included; whole response projected; catalog names only, no `requested_model` fallback; recursive schema test and nested canaries | §3.3, §9.2 | — |
| 5 | Job events and MCP not covered by a route inventory | **Applied.** Inventory covers HEAD, MCP tools and sub-operations, dispatch callbacks and event types (closed set). Admin job and event audiences deleted. Tests assert no side effects | §3.2, §9.2 | B2: every `McpDeps` callback; events follow job read |
| 6 | "No prompts in logs" is contradicted by an exception sink | **Applied.** Claim withdrawn (74 at the original review; 17 on current main, §1.1). MCP sink fixed; request-path sinks fixed; ratchet on the rest; exception-path canaries; LiteLLM and Traefik are deployment gates | §1.1, §3.6 | — |
| 7 | Signed grants need a byte, derivative and cache contract | **Applied.** Checks before any read and before Range; poster bound to the share; `private, no-store` for shares; GIF refused; metadata canary per container; "now" scoped to the next fetch and the copy changed. Current range helper has no HEAD/ETag/304/cache handling; full route inventory remains the implementation gate (`gateway/media_delivery.py:194-234`) | §3.4, §7, §9.2 | `md=full` binding to the fetcher stated (§3.4) |
| 8 | Several makers, partial deletion and the share key do not compose | **Applied, simplest option.** Sharing refuses items with more than one maker; the row's maker is authoritative for prompt, update and stop; deletion revokes that maker's shares and keeps other owners' bytes | §4.1, §9.2 | M5: deletion releases the deleter's claim (§6.3) |
| 9 | Password reset is an existing impersonation path | **Applied.** Privacy page names it; the Security event joins the existing `account_links.created_by` (no new field). **Rejected for now:** verified-channel delivery, because Azizam has no mail | §5, §7 | M6: the event ships with the switch (§5) |
| 10 | Migration and restore need executable fail-closed gates | **Applied.** One presence-based validator for removed parameters; the switch reuses AP §4.2 with a verify step; start refused on a partial switch or auth-disabled config | §3.7, §8 | M4: two-store guard removed (§8.3); both units use the full AP §4.2 protocol, whose marker is written before any live change |
| 11 (minor) | Keep honest accounting and safe failure evidence | **Applied.** Projection keeps the submission-time spending subject, cost quality, source and reconciliation, coverage fields and attempt facts; unknown layer has a next action; no raw-error bypass | §3.3 | — |
| Default 1 | Supervised accounts: defer | Kept. SR's minimal shape recorded as the direction | §2.3, §10.2 | — |
| Default 2 | Unattributed creations: conditional | **Refined**: three preview classes; only proven-unbound records are attached; unresolved ones stay ungranted | §10.2 | `service_account:` principals go to Unresolved |
| Default 3 | Share expiry: none | Kept | §10.2 | — |

### 11.2 Round 2 (v2 at `81ce3204`)

This table records the v3 decisions; code citations now point to main
`52408b26`. RT3 corrections in §11.3 override superseded mechanics. "Applied" means the design
text changed as RT2 asked; "Simplified" means a smaller change that removes
the problem; "Rejected" means RT2's change was not taken, with the evidence.

| RT2 | Finding | Resolution | Where |
|---|---|---|---|
| B1 (blocker) | Step 2 needs service-App identity and provenance | **Applied in v3; superseded by RT3 B2.** AP v4 now supplies accountable people (`gateway/admin_store.py:143-154`, `gateway/access_plan_migration.py:527-531`). Step 2 still enforces bindings and provenance; it does not re-run person assignment or the creator back-fill | §2.4, §3.5, §8.1, §12 |
| B2 (blocker) | The MCP inventory misses direct asset callbacks | **Applied.** Confirmed: `read_asset` and `store_asset` bypass REST (`gateway/mcp_server.py:109-119`, `proxy.py:14653-14669`), and `read_asset` allows a caller with no identity on an ownerless asset (`proxy.py:6973-6975`). Every `McpDeps` callback now has one treatment: `read_asset` is `require(…, "reuse")`; `store_asset` mints with `Access=Own`; `catalog` and `aliases` are classified `none` and proven by the two-world test; job content checks each output. Negative tests for guessed, shared and foreign inputs, personless keys, minting and discovery. The notice waits for them (§12 step 3 gate) | §3.2, §9.2 |
| M1 | Session principal expansion makes Accountable ineffective | **Simplified.** Confirmed: "Apps a person owns" selects both kinds (`gateway/admin_store.py:2144-2152`), so after the cut-over the accountable person matched Own. v3 takes RT2's second option: one explicit rule (O6), the Accountable basis removed, matrix and copy amended ("can see and use it"). The narrower basis protected nothing, because Accountable could already read and download the bytes. Historical v3 read rule superseded by round-4 ContentRead (§11.4). Precedence: Own, then Shared; prompts are a separate decision. Events follow job read, which removes the event conflict | §2.4, §3.1, §7 |
| M2 | Ownership triggers need NULL-safe transitions and first-assignment fencing | **Applied in v3; corrected by RT3 M6.** Current auth resolves key and App in one SELECT (`gateway/admin_store.py:2463-2485`). Triggers are scoped to App keys, make revocation final and install after migrations; no epoch is required | §2.4, §3.5, §8.1 |
| M3 | Replace the percentage reserve with an absolute budget | **Applied, with leases instead of a concurrency bound.** Measured: 1.45 TB, 79.5 GB available, 95% used; v2's 5% would have paused after about 7 GB of growth by other software. Rule: `free − leased ≥ reserve_bytes`, `reserve_bytes = W_max + 2 GiB` by default, configurable, refused below `W_max`; in-process per-job leases as the STT spool already does (`gateway/stt_chunking.py:784-865`), so no global concurrency limit is assumed; byte hysteresis and warning; every storage-writing kind declares `peak_bytes` or fails CI; unknown fails closed; "budgeted, not guaranteed"; percent is description only | §6.4, §7, §9.1 |
| M4 | Two-store share revisions need a runtime failure protocol | **Simplified: the guard is removed.** A share write is one transaction in one store. Hand restore is a host action with a stated limit, the same as AP §4.2 states for revoked keys; the ops note stops every share before start. The supported restore (AP §4.2) runs only before any traffic. RT2 itself noted that the guard could not detect a consistent restore of both stores | §4.1, §8.3 |
| M5 | Storage cleanup inherits a deduplicated-file reclamation dead end | **Applied.** Confirmed: delete never removes an owner from the sidecar (`gateway/me_media.py:676-707`). Delete now releases the deleter's principals and maker entry under the existing cache lock, revokes their shares, and removes bytes when no owner is left. The audit tombstone stays as history. Reclaim estimates count files that would have no owner left. As a side effect, a deleted item is no longer readable through the deleter's App keys | §6.2, §6.3 |
| M6 | Step-2 copy promises later features and an unassigned recovery event | **Applied.** Confirmed: no "Password changed" event exists in `web/src` or `gateway`. The reset event ships with the switch (§12 step 3). Every copy row has a step; storage and sharing rows and links render only from their steps. Document amendments that define a live promise ship with the step that makes it true | §5, §7, §12 |
| N1 (minor) | Keep owner control without reopening content | **Applied as a stated limit.** The owner cannot cancel someone else's job in v1; it is an HR Release C dependency with its own audit reason, never a content grant. MCP cancel returns its own result, independent of the later poll | §3.2, §5 |
| Note: `md=full` | A signed `g=o, md=full` URL carries no person | **Corrected in round 4 (§11.4).** Key fetchers must pass ContentRead; delivery authenticates the fetcher and restores that person's own prompt (`proxy.py:23748-23765`); two makers each get their own; no person in the signature | §3.4 |
| Note: `service_account` principals | Control-plane principals could own content | Runtime auth rejects them (`gateway/admin_runtime.py:2411-2416`). The cut-over preview counts records with such a principal; any it finds are Unresolved | §3.1, §8.1 |
| Note: step 1 | "Five checks call the final matrix" may change differing rules | The owner-everyone branches and the no-identity branch stay at the call sites in step 1, named for deletion in step 3, so step 1 changes no behavior | §12 |
| Note: AP permits | The design leaned on `access_epoch` | Removed; per-request authorization is enough (§2.4 Linearization) | §2.4, §3.5 |
| Consistency check of v3 (Sonnet, before commit) | 5 major, 9 minor; no blocker | Historical v3: cut-over set persons before a final back-fill (superseded by RT3 B2: AP supplies persons; no final back-fill); new databases need triggers and step 4 releases pre-v3 deletions (both corrected by RT3 M6/M8); `user:` principals stay in the session `Viewer`; §6.4 states its error bound, an atomic check-and-lease lock, leases released in `finally`, and admission following the state; database claims reworded (shape, not caller), `NULLIF` app ids, `BEFORE DELETE ON apps`; v3 argued two-query auth safe (superseded by main's single SELECT); test setups that mint keys as a service account added to §1.5; transfer tests deferred; ops restore note moved to step 2; removed-person limit stated (§10.1); Home copy names shared apps | §2.4, §3.1, §3.5, §6.3, §6.4, §7, §8.1, §10.1, §12 |

### 11.3 Round 3 resolution

Non-maker: Claude Opus, v3 at `782d763f`. Verdict: **BUILD AFTER THESE
DESIGN EDITS**. All 15 findings are applied; none is rejected. Evidence
below was re-read against `origin/main` `52408b26`. "Applied" means the
design and test requirements changed, not that implementation has shipped.

| Finding | Applied or rejected, with reason and current-main evidence | Section | Step |
|---|---|---|---|
| B1: trusted addresses grant content | **Applied.** Owner can add to any non-session App (`gateway/admin_api.py:2096-2117`, `gateway/admin_store.py:2065-2098`); runtime acts as `app:<id>` without a person (`gateway/admin_runtime.py:2457-2482`). O4 covers keys and addresses; accountable-only add, INSERT provenance trigger, zero-binding transfer and recent-use removal block | §1.2 L12, §2.4 O3/O4, §3.1, §3.5, §8.1; TD P19/§7 | 1 identity/inventory; 2 enforcement |
| B2: AP v4 already supplies identity schema | **Applied.** NOT NULL/FK person, no service-account owner column (`gateway/admin_store.py:143-154`); migration assigns persons (`gateway/access_plan_migration.py:13-19`, `gateway/access_plan_migration.py:527-531`); service default (`gateway/admin_runtime.py:372-401`). L9 owner exception and empty automation creator remain (`gateway/admin_runtime.py:358-370`, `gateway/admin_runtime.py:620-634`); back-fill still runs (`gateway/admin_store.py:550`, `gateway/admin_store.py:612-655`). Unit 1 now enforces bindings, revokes/removes unproven provenance and deletes back-fill. Keep foreign-created personal-App row; drop obsolete person-approval row and Q5 | §1.3, §3.5, §8.1, §10.2, §13; TD §7 | 2 |
| M1: retention ledger bypass/existence oracle | **Applied.** REST uses admin auth with no ownership and copies requested_model (`gateway/api.py:965-973`, `proxy.py:14047-14076`); MCP repeats the role gate (`gateway/mcp_server.py:654-669`, `proxy.py:14006-14010`). Add maker content:receipt:read route, owner projection without observed.requested_model, non-owner-admin 404 on operational route and missing-id equivalence. No-id ledger is none. Rewrite leak-encoding tests (`tests/test_retention_ledger_headless.py:43-73`, `tests/test_retention_ledger_headless.py:78-95`) | §1.2 L13, §1.5, §3.2/§3.3; TD P07/P15 | 1 classify; 3 enforce |
| M2: retained-content kinds need one core | **Applied.** RR defines person-scoped kinds and narrowing (`docs/design/REQUEST_RETENTION.md:427-504`); the decision returns no_person (`gateway/retention.py:439-447`), but explicit conversation store still has a no-person exception (`gateway/retention.py:459-468`). Historical round-3 read rule superseded by §11.4: ContentRead for history, Self/permission read/delete only for Debug; retention.reader_allowed calls decide then narrows. RR slice 5 has no independent reader. Library cascade joins maker deletion (`docs/design/REQUEST_RETENTION.md:126-131`) | §3.1, §6.3; TD P21/P18 | 1 kinds; 3 reader with RR slice 5; 4 cleanup |
| M3: D50 incognito and retention authority/copy | **Applied.** Extend existing Try forget/sessionStorage (`web/src/app/try/page.tsx:1325`, `web/src/app/try/page.tsx:1413-1414`), one switch across prompt/history/conversation; O4b accountable-only App/key writes and household never_keep narrowing. Privacy, Home, owner Usage and honest limits name history, gated on RR slice 5 | §2.4 O4b, §7/§7.1; TD P21 | 2 binding authority; 3 controls/copy with RR slice 5 |
| M4: kept-history bytes and paused write | **Applied.** RR adds kept requests in media_jobs.db (`docs/design/REQUEST_RETENTION.md:398-405`); main has storage_unavailable reason (`gateway/receipts.py:97-102`). Count per-maker bytes, owner bytes only, maker review/delete; Paused/Unknown skips optional history and never fails the request | §6.1-§6.4; TD P18/§7 | 4 with RR slice 5 |
| M5: competing STT disk counter | **Applied.** STT owns separate reserved bytes, loop-only admission and spool probe (`gateway/stt_chunking.py:762-868`), floor 64 MiB (`gateway/stt_chunking.py:369`). Use one locked per-device ledger including STT upload/top-up/replay; STT obeys reserve. Measure private temp: data-adjacent default (`gateway/private_tmp.py:35-42`), relative STT (`gateway/stt_chunking.py:318-333`), Docker image disk (`Dockerfile:26-27`) | §0, §6.4, §10.2 Q4; TD §7 | 4 |
| M6: trigger scope, revocation and upgrade order | **Applied.** Control-plane adoption updates created_by (`gateway/signin_admin.py:485-499`, `gateway/signin_admin.py:549`); AP updates App persons (`gateway/access_plan_migration.py:527-531`). Scope immutable credentials to OLD App rows, make App-key revocation final, install after every migration and test v6→current plus fresh/current initialization | §3.5, §8.1; TD §7 | 2 |
| M7: new MCP fields and OAuth Viewer | **Applied.** Classify every field (`gateway/mcp_server.py:109-141`), wired at `proxy.py:14777-14800`: retention per M1, STT operational, rename own-account, gates/config none. OAuth is a person/session-App identity (`proxy.py:2425-2453`); its Viewer includes the person's Apps and privacy permission | §3.1/§3.2; TD P15/P21 | 1 inventory/identity; 3 enforcement |
| M8: untimestamped claims/tombstone replay | **Applied.** Sidecar writes have no claim time (`proxy.py:7088-7115`); tombstones use actor/asset id (`gateway/me_media.py:1299-1322`), session principals include both App kinds (`gateway/admin_store.py:2144-2152`). Remove principals, stop audit-tombstone hiding only (job-scoped hides preserved per §11.4), replay pre-v3 cleanup once using personal-App aliases plus user only; service-App removal confirmation is explicit | §6.3; TD P18/§7 | 4 |
| N1: stale facts and measurements | **Applied.** Reset route 1 hour versus helper 7 days (`gateway/accounts_api.py:72-73`, `gateway/accounts.py:473-475`); Try is personal (`gateway/user_runtime.py:472`, `gateway/user_runtime.py:499-536`); cancel still polls/fetches (`gateway/mcp_server.py:603-609`, `gateway/mcp_server.py:579-581`). Drop client_address/api_key_name (`gateway/receipts.py:72-81`); re-measure disk before step 4 | §0, §1.2, §2.4 O2, §3.2/§3.3, §6.4; TD §10 | 1 citations; 3 paths; 4 measurement |
| N2: retention projection | **Applied.** Current retention vocabulary is closed (`gateway/receipts.py:93-107`). Owner projection adds only retention [{kind: enum, decision: enum}], never reason/deadline | §3.3; TD §6 | 1 schema; 3 delivery |
| N3: sidecars outside admin.db transaction | **Applied.** Sidecars are file writes (`proxy.py:7110-7115`), not SQLite rows. Unit 2 uses an accepted pre/post manifest, atomic idempotent replacements, re-run/verification before version and acceptance, plus sidecars in AP backup/restore | §8.2; TD §7 | 3 |
| N4: unbound App plan | **Applied.** App creation requires an explicit plan (`gateway/admin_runtime.py:399-401`). "No person recorded" is service, accountable to gateway owner, access_plan="person", no automatic binding | §8.1, §10.2 Q2; TD §7 | 2 create; 3 attach |
| N5: open-stream revoke boundary | **Applied.** Runtime rows are per-request (`gateway/admin_store.py:2463-2485`); MCP polls dispatch fresh requests (`proxy.py:14607-14615`). Main event endpoint is JSON polling (`gateway/api.py:764-818`); the contract for an open event stream allows it to continue until disconnect, then reconnect re-authenticates. No per-event revocation guarantee | §2.4 Linearization; TD P12/U4 | 1 contract; 3 enforcement |
| Open questions | **Applied.** Keep Q1 supervision, Q2 unbound creations (with N4), Q3 expiry and Q4 reserve (with M5/re-measure). Q5 is obsolete because AP already assigned service Apps (`gateway/access_plan_migration.py:13-19`) | §10.2 | 2 preview; 4 measurement |

### 11.4 Round 4 resolution

Input: Claude Opus round-4 check of v4 at `e27a7ebb`. Steps 1 and 2 can
be built; step 3 needs the corrections below. The design owner decided
each point. All are applied in v5; none is deferred or rejected. This table
supersedes conflicting historical round-2/round-3 wording. Citations were
re-read against `origin/main` `52408b268215585dab40299f2ebc2bd920ab6bff`.
These are design/test requirements, not executed implementation evidence.

| Finding | Resolution and current-main evidence | Sections / tests | Build step |
|---|---|---|---|
| Delegated content reads and Debug actions | **Applied.** Debug has read/delete only; reuse cell is blank. App keys read attributed same-App outputs and MCP grants read their own connection’s chats, prompts and request_history without permission (v4.2 correction, §11.5); same-person content outside OwnOutput requires permission; sessions never need it. Delete the loose `conversation_row_visible` (`gateway/retention.py:472-484`), `_responses_row_visible` (`proxy.py:25886-25888`), key-fetcher `md=full` restore (`proxy.py:23748-23765`) and key-API prompt reads (e.g. `proxy.py:9518-9523`); use ContentRead and trusted App/credential/connection origin | §3.1/§3.4/§12; TD §2/P06/P21 | 1 oracle; 3 readers |
| Permission storage and migration | **Applied.** Columns on credentials and MCP grants; only the App's accountable person's browser session writes them; no self-grant. New/existing keys default off. Unit-2 preview lists keys with cross-App reads in the last 30 days for explicit per-key enablement, following §8.1's binding-use pattern; no silent grant | §3.1/§8.2/§12; TD §2/P21 | 3 |
| Rows with no person | **Applied.** Maker is the App; only exact same non-null App credentials read these rows, never a person or null-person match. Existing open-webui/daisy rows retain App-only access under normal retention. Accept service-key/trusted-address `store:true` for App-only continuation, state that scope in the receipt, and omit content from every person's UI/history. Same-App previous_response_id works; other App/person gets missing-id-equivalent 404. Replace the loose writer exception (`gateway/retention.py:459-468`) with explicit App scope | §3.1/§12; TD §2/P06/P21 | 1 oracle; 3 storage/readers |
| M8 job-scoped hides and writer fence | **Applied.** Keep `media_hidden_assets` as is (`gateway/prompt_memory.py:960`, `gateway/media_jobs.py:800-810`, `gateway/me_media.py:356`, `gateway/me_media.py:1028`): another job's identical bytes are a new item. Claim release replaces only audit-tombstone hiding. RR writer fence (`docs/design/REQUEST_RETENTION.md:1267-1271`) checks both deletion intent/released claim and job-scoped hide | §6.3; TD P18/§7 | 4 with RR slice 5 |
| MCP Own-ledger gate and fact-ledger classification | **Applied.** Change retention_visible to any person-bearing identity (`proxy.py:14006-14010`, `gateway/mcp_server.py:656`), then enforce Own per request. `/admin/api/settings/retention/ledger` and `/admin/api/engines/{id}/retention/ledger` are operational fact data with no request content, owner and admin read (`gateway/retention_fact_api.py:59-66`); the per-request owner projection remains separate | §3.2; TD P07/P15/§10 | 1 classify; 3 enforce |
| Trusted addresses, headless setup and unbound classification | **Applied.** Add trusted addresses to §2.1. Control-plane-key first-App setup (`proxy.py:17671-17680`) becomes 403; replacement is owner sign-in or documented owner CLI bootstrap with owner provenance. Unit 1 runs the same proven-unbound classification as unit 2 when creating "No person recorded" | §2.1/§3.5/§8.1/§12; TD P19/§7/§10 | 2 |

### 11.5 Round 4.2 resolution

Lead decision, D52 addendum (2026-10-20) in
`~/Documents/azizam/engine-contract/DECISIONS.md`. This corrects §11.4's
App-only description of the delegated own-output exemption.

| Finding | Resolution | Sections / tests | Build step |
|---|---|---|---|
| MCP grants share the browser session App | **Applied.** App keys match attributed outputs of their App; MCP grants match the creating connection_id. Browser-session and unattributed legacy rows require privacy_permission for a grant; missing origin counts as browser-created. Step-3 writes record connection_id or credential id, with explicit browser origin. Matrix and test cells cover own-connection allow, browser-row deny/allow with flag, other-connection isolation and unattributed-row deny without flag | §3.1; TD §2/P06/P21 | 1 oracle/Viewer; 3 origin writes/readers |

## 12. Build order

Each step ships on its own, keeps the full test suite green, and makes no
privacy claim before its enforcement exists. Copy and navigation ship with
the step that makes them true (§7). A document amendment that defines a
live promise ships with its step, not in a later documents step.

| Step | Ships | Gate |
|---|---|---|
| **1. One core, no behavior change** | Eight kinds and action matrix, Viewer with trusted address, OAuth identity and privacy_permission; typed operational retention projection. Classify every route, ledger variant, MCP field/tool and event. Keep legacy broad/no-identity branches at call sites until step 3 | Current suite unchanged; independent eight-kind/six-action oracle, inventory completeness, identity and recursive schema tests |
| **2. App-binding enforcement on AP v4** (§3.5, §8.1) | Accountable-only keys and trusted addresses; O2/O3/O4 triggers, App-scoped immutability and final revoke, installed after all migrations; startup back-fill removed; revoke/remove unproven bindings, recent-use stop/replacement block. Run unit 2’s proven-unbound classification when creating the unbound App with person plan. Replace control-plane first-App setup with 403; document owner sign-in/owner CLI bootstrap. Ops restore note. AP installed is not a verify-only shortcut | Actual schema-v6→current, current and fresh paths; trigger/provenance/race/self-tests; control-plane adoption still works; accepted concrete preview |
| **3. Private by default** | Core/projection on every path and MCP callback, maker ledger and projected owner ledger, unknown-id equivalence; parameter rejection, signed grants, log sinks, reset event and idempotent Unit-2 attachment/restore. Remove Everyone/person picker. Delete the loose conversation, Responses, key-fetcher md=full and key-API prompt readers (§3.1). Add permission columns on credentials/MCP grants and browser-session-only accountable writes, default off, with unit-2 recent cross-App-use preview. Record connection_id or credential id on new content rows plus browser origin. Preserve App-key same-App and MCP same-connection OwnOutput reads; treat unattributed legacy rows as browser-created. Preserve App-only no-person store:true/continuation. With RR slice 5: request_history/debug_capture reader integration (Debug read/delete only), O4b settings and single D50 incognito; history copy waits for that slice | TD P01-P17/P19-P21 except step-5 shares; no owner/admin bypass in real bound callbacks; sidecar crash/replay/restore; history and copy gates run on implementation SHA; deployment logging checked |
| **4. Storage** | Owner/member storage, history bytes and maker review/delete (with RR slice 5); claim release, one-time personal-only pre-v3 cleanup and service-App removal copy; Library/history cascade and deletion intents. One per-device locked ledger across STT and other writers, scratch devices included; optional history skips while paused | Re-measure every resolved filesystem and W_max; TD P18/§7 device/thread/async/replay tests; normal response under history storage failure; 390 px cleanup journeys |
| **5. Sharing v1** | `shares` table, `shared_view`, share sheet, "Shared with me", Recent entry, the share line of the ops restore note (§4, §8.3). The step-5 rows of §7. UB §2.3 and DATA_RETENTION (shares class (a)) | TD P14, §5 (signed grants and metadata) and the sharing rows of P01-P06 |
| **6. Cross-references** | AP v4, RR, PM, UB and HR references/copy aligned with this design and D50/D51. AP key-only transfer wording points to O3 zero bindings; no reader widening | References and notices match shipped step, no stale AP schema or second reader |

Notes:
- Step 2 comes before step 3 because the privacy promise needs a person on
  every App (O6) and proven provenance on every live key and trusted address (O4).
- The required-storage disk pause can ship after step 1 if needed, but
  must include STT in the same ledger. History accounting/skip behavior
  and cascade wait for the RR slice-5 writer/core integration.
- Until step 3 ships, R18 finding 2 can be closed by a one-line web change:
  the owner's Library starts at "mine". That is optional; step 3 removes
  the view.
- Step 3 is the largest. It is one step because each half alone is false:
  hiding the Library while foreign-created bindings still read member Apps would make the notice
  untrue.

## 13. Before building (P20)

- [x] Non-maker red-team of v1 (RT), §11.1.
- [x] Non-maker red-team of v2 (RT2) and TD, §11.2/§9.2.
- [x] Non-maker red-team of v3 (Claude Opus): BUILD AFTER THESE DESIGN
      EDITS. Round-3 changes applied in v4, §11.3; no new review started.
- [x] Apply round-4 design-owner decisions in v5 (§11.4). Steps 1 and 2
      can be built; step 3 uses the corrected contracts. No new review started.
- [x] Re-check AP v4 (`545726a7`) and current-main AP schema (`52408b26`).
      AP §2.4 cites OV's ownership model and preserves O5/O6; its old OV
      section numbers and key-only transfer wording still need step-6
      cross-reference updates. AP does not supply O2/O3/O4 enforcement;
      step 2 is required. Do not report full cross-document agreement yet.
- [x] Record decision: **Tim go on v3 2026-10-12; v4 edits per round 3**.
      D50/D51 are fixed inputs, not open policy questions.
- [ ] Apply or explicitly accept Q1-Q4 defaults in the concrete build
      preview (§10.2); Q5 removed. Re-measure data/scratch before step 4.
- [ ] Follow §12 and record implementation/test evidence before notices.

## Change log

- v4.2 follow-up to v5, 2026-10-20. Applies D52’s origin addendum (§11.5):
  App-key own outputs stay App-scoped; MCP own outputs are connection-scoped.
  New rows record origin; unattributed legacy rows count as browser-created.

- v5, 2026-10-20. Applies the design owner’s round-4 decisions (§11.4):
  delegated own-output/other-origin reads, App-only no-person continuation,
  permission storage and preview, retained job-scoped hides, MCP Own-ledger
  gate, operational fact ledgers and headless bootstrap/classification.
  Re-cited against unchanged `origin/main` `52408b26`; no build or review run.

- v4, 2026-10-20. Resolves round 3 (§11.3) against main `52408b26`.
  - Re-cites both files, rebases Unit 1 on shipped AP v4, includes trusted
    addresses and trigger upgrade order, final revocation and provenance.
  - Integrates maker ledger/history/Debug reads, MCP fields and permission,
    D50 incognito and D51’s maker-private policy; no-person rules were
    corrected by round 4 (§11.4).
  - Counts history bytes, skips optional retention while paused, shares
    STT/device leases, replaces claim-time comparisons with principal
    release and makes sidecar attachment/restore replayable.
  - Records step ownership, copy/RR gates, Q1-Q4 and Tim's v3 go.

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
    are separate assets; the store helper defaults to 7 days (the route
    defaults to 1 hour on the v4 baseline, §1.2).
  - Sections renumbered: Storage is §6; wording §7; data §8; API and tests
    §9; risks §10; red-team §11; build order §12.
- v1, 2026-10-11. First draft, then one non-maker review pass (11 findings
  applied: App-level rule 1 with L9 closed at key creation; accountable-only
  key creation; rule 2 as an AP §2.4 amendment; status sections hidden from
  non-owner admins; `terminal_reason` kept and `path` dropped; reset notice
  folded into UB §3.4; required `md` and `g`; cross-references; VOICE copy;
  cuts).

# Owner visibility: private member work, an operational owner view

Status: design, v1, 2026-10-11. Not built. P20 requires a red-team and a
recorded go decision before the first implementation commit.
Baseline: `release/r18` at `8a3147f0`.

Owner direction: "Just follow best practices made clear by the many prior arts
like immich". Azizam is unreleased, so this design adds no compatibility shims
(P11).

## 0. Inputs

- **R**: research, `~/Documents/azizam/ux-findings/research-results/azizam-research-owner-visibility-20261011.md`
  (14 products, access matrix, metadata allowlist, sharing, break-glass,
  moderation, wording, acceptance criteria). R §n means its product section n.
- **AP**: `design/access-plans:docs/design/ACCESS_PLANS.md` v3 (plans, the
  accountable person, receipt detail).
- **UB**: `design/ux-batch1:docs/design/UX_BATCH1.md` (§4 "Don't save this
  prompt", §3 Recent).
- **HR**: `docs/design/HEADLESS_AND_RESOURCES.md` (principle 4: members see
  shared capacity coarsely; owners see attribution, never prompt content).
- **PM**: `docs/design/PROMPT_MEMORY.md` (prompts are already maker-only).
- Principles P1-P22 (`docs/owner-principles`), `VOICE.md`,
  `docs/ops/DATA_RETENTION.md`, the code-quality canon.
- The code inventory in §1 was made by a read-only sub-agent. I spot-checked
  the lines this design depends on: `me_media.py:461-480`, `:271-307`,
  `:1418-1430`; `user_runtime.py:324-428`; `proxy.py:6842-6862`, `:7172-7183`,
  `:23563-23640`; `activity_api.py:21-97`; `admin_api.py:2251-2278`;
  `admin_runtime.py:1604-1614`; `record_ownership.py:11-22`;
  `status_endpoint.py:2469-2540`, `:2981-3031`. I did not check other line
  numbers one by one.

## 1. Current state

### 1.1 What is already private

- **Saved prompts and workflows.** `asset_prompts` is keyed by
  `(asset_id, person_id)` (`gateway/prompt_memory.py:31-41`). Every reader
  takes one person (`get` `:392`, `get_person_prompts` `:412`). The Library
  passes the signed-in viewer even under `person=<id>` (`me_media.py:473`).
  Another person's item shows `prompt: null` and "Picture by {maker}"
  (`me_media.py:656`, `_media_title` `:1196`). `md=full` delivery requires the
  maker's own kept prompt (`proxy.py:23618-23640`).
- **Saved chats.** Responses rows need the same person and App
  (`_responses_row_visible`, `proxy.py:25808-25817`). There is no owner bypass.
- **Custom voices.** Stored in per-principal folders (`custom_voices.py:59`).
  There is no owner bypass.
- **Key API.** `/gateway/v1/*`, `/v1/videos/*` and MCP go through
  `owns_record` (`record_ownership.py:11-22`), `_asset_access_allowed`
  (`proxy.py:6930`) or `_video_job_access_allowed` (`proxy.py:9352`). None has
  an owner bypass. The only `unrestricted=True` caller is the auth-disabled
  mode (`proxy.py:14162`).
- **Receipts store no bodies.** `receipt_store._assert_safe` (`:191`) rejects
  prompt, input, messages, body and bytes keys (`gateway/redaction.py`).
- **Logs.** The gateway prints no prompts or bodies. uvicorn access logging is
  off (`proxy.py:30203-30208`), so signed query strings are not logged by the
  gateway.

### 1.2 Where the owner sees member content or content-bearing metadata today

| # | Surface | Path | What the owner gets | How scope is decided |
|---|---|---|---|---|
| L1 | Library list and detail | `GET /me/api/media`, `/media/{id}` (`me_media.py:162-219`) | Every member's creations: kind, size, model, workflow, seed, maker name, `download_url` | `scope=everyone` or `person=all\|local-network\|<id>` (`_media_scope`, `me_media.py:461-480`). `person=<id>` is **not audited** (`:473`) |
| L2 | Download, posters | `GET /me/api/media/{id}/download` (`me_media.py:271-307`) | A signed URL for any member's creation | Same `_media_scope`. Posters use the same route (`web/src/app/library/page.tsx:468`, `:483`, `:948`, `:997`) |
| L3 | Signed URLs | `/v1/assets/{id}/content` (`proxy.py:23467`, `:23487`, `:23551` → `:23563`) | Bytes, to anyone with the URL | HMAC over `asset:expires:md` only (`proxy.py:6842-6847`). No person and no grant. TTL 3600 s (`proxy.py:808`). No ownership check without `auth_context` (`:23617`) |
| L4 | Receipt detail | `GET /me/api/receipts/{id}` (`proxy.py:16017-16038` → `user_runtime.py:373-428`) | Any member's receipt (client audience) | Owner role, implicit. No parameter. Audited `receipt.detail.instance`. The web sends `?scope=everyone` (`library/page.tsx:1647`); the server ignores it |
| L5 | Home, status, catalog and workflow feeds | `/me/api/activity` (`proxy.py:15990`), `/me/api/home` (`home_api.py:378`), `/me/api/status` (`home_api.py:392-418`), `/me/api/models/catalog` (`proxy.py:15800`), `/me/api/workflows` (`proxy.py:15949-15960`) | Every member's recent receipts and jobs | `build_user_activity_snapshot`: `app_ids=None if owner` (`user_runtime.py:350-361`). The owner gets this by default. Not audited |
| L6 | Usage | `GET /me/api/usage/summary` (`me_media.py:309-328`, `:1230-1430`) | Every member's receipts and totals, plus the people list | `person=all\|local-network\|<id>` (`_usage_app_scope`, `:1418`). `person=<id>` is not audited (`:1386`). The web defaults the owner to `all` (`usage/page.tsx:158-168`, `:903-908`) |
| L7 | Owner request log | `GET /admin/api/activity/requests` (`activity_api.py:19-97`) | Every receipt in the **admin audience**: backend, `api_key_name`, `request_shape` counts, policy, litellm ids | `"owner" in ctx.roles` (`:21-28`). Defaults to everyone. Not audited |
| L8 | Status | `/v1/status`, `/status`, `/status/raw` (`proxy.py:25603`, `:25735`, `:25743`) | Live video jobs with `key_name`, alias, size, and `error` text up to 200 characters (`status_endpoint.py:2981-3031`). File names and absolute paths of the 20 newest videos and 10 newest images in the engine's output folder, regardless of maker (`:2469`, `:2510`) | `ADMIN_AUTH`, so any admin, not only the owner. Whether these routes are owner-only is UNVERIFIED; the comment at `proxy.py:25641` says "owner-guarded" |
| L9 | Keys on a member's App | `POST /admin/api/apps/{app_id}/keys` (`admin_api.py:2251-2278`) | A new key on a member's personal App. It has no person (`admin_runtime.py:1604-1614`), so no prompts, but it has App access (`app:<id>`). It reads that App's creations, receipts and jobs through `/gateway/v1/*` and MCP | Any admin, any App. **This bypasses any UI-only policy** |
| L10 | Password reset | `POST /admin/api/people/{id}/reset` (`accounts_api.py:239`, `accounts.py:357-373`) | A link to sign in as the member | Owner. Audited (`account.reset.create`, `account.password.reset`). The member gets no notice; they find their old password no longer works |
| L11 | Host files | `data/*.db`, `cache/images/*`, `data/image_media_jobs/<job>.json` (full request body until the job ends, mode 0600, `proxy.py:834`, `:20593`) | Everything | Host authority. Outside product control (PM: "The owner can read databases or issue a reset link to sign in as a member") |

Not an exposure today:
- `/me/api/jobs`, `/me/api/make/jobs` and notification polling are self-only,
  owners included (`me_media.py:122-160`; `me_make.py:178-224`;
  `job-notices.tsx:160`).
- Library DELETE is self-only (`me_media.py:221-269`).
- People, Apps, credentials and config export carry no content
  (`accounts_api.py:213`; `admin_api.py:1803`, `:1905`, `:1915`, `:2327`;
  `proxy.py:18356`).
- There is no content export or backup endpoint and no CLI in this repo.

### 1.3 Authorization is spread out

There is no single authorization core. `owner` means `{owner, superadmin}` in
`me_media.py:76` and `user_runtime.py:350-356`. It means `owner` only in
`activity_api.py:25`. `accounts.py:393` displays `superadmin` as "admin". The
same ownership rule is written five times:
- `owns_record`
- `_asset_access_allowed`
- `_video_job_access_allowed`
- `_responses_row_visible`
- `_receipt_in_scope` / `_job_in_scope` (`me_media.py:925-942`)

The web defaults the owner to Everyone in Library (`library/page.tsx:2154`,
`:2454`) and Usage, but the API defaults to "me".

### 1.4 Tests that encode owner-sees-all (to rewrite)

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
| Creation bytes, posters, signed URLs, inputs | Yes | The shared creation only | No | No |
| Prompt, workflow, saved chat, custom voice | Yes | Prompt only if the maker chose "Include prompt" | No | No |
| Receipts and jobs | Full own view, at the plan's receipt detail | No | The §3.3 projection only | No |
| Storage | Own counts and bytes | No | Per person: counts and bytes by kind, plus server totals | No |
| Accounts, plans, keys | Own; keys only for Apps they are accountable for (§3.5) | No | Invite, plan, disable, revoke, reset; keys only for Apps the owner is accountable for (§3.5) | As today, except keys (§3.5) |
| Audit | Own sharing list; Security events, including owner-issued resets (§5) | No | Configuration and access records, with no content | As today |

Anyone, owner or not, who is the accountable person of a service App also
sees what that App made (rule 2, §3.1). That is App content, not member
content, and the Privacy page says so (§6).

"Owner" is one role, `owner`. The core has one predicate (§3.1). Live
`superadmin` rows are listed in the build preview and become `owner` or
`admin` there, with no alias kept (P11). Whether any exist is UNVERIFIED.

Attributed operations are **owner-only**. This matches today's request log
(`activity_api.py:25`). Admins keep engine and capacity status with person
and key attribution removed (§3.3). For teams, R recommends separating
operational, moderation and export capabilities. One role does that until a
team asks for more.

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

   The products that show everything to the admin are a different model.
   PhotoPrism, Jellyfin and Plex share one operator collection; Home
   Assistant shares one household state. A creation made from a member's own
   prompt is personal work, not a shared collection (R "Cross-product
   conclusions" 1).
2. **A toggle makes every promise conditional.** Open WebUI defaults
   `ENABLE_ADMIN_CHAT_ACCESS` to true. Its own docs say the per-user flags "do
   not establish a security boundary against the admin". Its exceptions also
   continue past the flag: internal chats by ID, export, and per-ID routes
   (R §9). With a toggle, member copy must say "unless the owner turned it
   on", plus a notice when it changes (R "Support and break-glass" 5). That
   adds a state, a notice flow and a historical-reach rule to protect a
   capability nobody needs routinely.
3. **The owner's real needs are operational.** Cost, abuse control, capacity
   and storage all work from attribution and quantities (R "Moderation" 2).
   HR principle 4 already says "never prompt content".

### 2.3 Supervised accounts (children, schools): deferred

A household with children may want to see a child's creations. That is a
**supervision** product: Google Family Link, Apple Screen Time and school
device management. R does not cover those products, so P14 and P10 forbid
designing it from memory. R gives the constraint: a supervised service "should
be explicitly configured and disclosed" and is "a different experience from
the member-private default" (R "Moderation" last item).

v1 gives prevention, not surveillance. Access plans already provide
capability grants, blocked categories ("Adults only"), caps, and disable
(AP §2.1). If the owner wants supervision (open question 1), it needs its
own research and design. Fixed now: it is per person, disclosed to that
person, reaches forward only, and is a grant kind in the core (§3.1), never
an owner bypass.

## 3. Enforcement: one core

### 3.1 The module

`gateway/content_access.py` replaces the five ownership checks in §1.3 and
the owner branches in L1-L7. It is one deep module with a small interface:

```python
@dataclass(frozen=True)
class Viewer:
    person_id: str | None        # signed-in person, or the person of a personal key
    owner_keys: frozenset[str]   # readable principal keys (gateway/ownership.py)
    accountable_app_ids: frozenset[str]  # person-less Apps this person is accountable for (AP §2.4)
    is_owner: bool               # role == "owner"; the only owner predicate in the codebase

Grant = Own | Shared(share_id, include_prompt) | None_

def content_grant(viewer: Viewer, record: ContentRecord) -> Grant
def require_content(viewer, record) -> Grant          # raises NotFound (404) on None_
def operational(viewer) -> bool                        # viewer.is_owner
def project(kind: Literal["receipt", "job", "storage"], record) -> dict   # §3.3 allowlist
```

Rules inside `content_grant`, in order. The `is_owner` field feeds only
`operational`.
1. **Own.** The record's principal keys intersect `viewer.owner_keys`. This
   is today's rule. Records store the App principal `app:<id>`, not the key
   that wrote them (`ownership.py:48-49`, `:75-77`). Every key of an App
   therefore reads everything the App made (`credential_owner_keys`,
   `:85-94`). The core keeps this, because a per-key rule cannot work on
   existing records. L9 is closed at key creation instead (§3.5).
2. **Own (accountable).** The record has no person, and its App is in
   `viewer.accountable_app_ids`. Example: the owner sees what the `daisy`
   service App made. This **amends AP §2.4**, where the accountable person is
   "only the policy and spending subject". The reason: a record with no person
   still needs someone who can find and delete it in the product (P19). Member
   copy discloses it (§6). Records with no App and no person are settled once
   in the build preview, not by a runtime branch (open question 2).
3. **Shared.** An active `shares` row for `(asset_id, viewer.person_id)`
   (§4). This applies to creations only.
4. Otherwise `None_`. **Role is not an input to rules 1-3.**

Requests with no identity get `None_`. Auth-disabled mode (`proxy.py:14162`)
is out of scope. Whether production can run with auth disabled is
UNVERIFIED; the access-plans build removes keyless access (AP §2.8).

This is a consolidation, not new machinery. The data shape is unchanged:
sidecar `owner_key_hashes`, receipt `app_id`, and job `owner_key_hash`. The
only new input is the `shares` table.

### 3.2 Every read path uses it

| Path (from §1) | Change |
|---|---|
| L1 `/me/api/media`, `/media/{id}` | Always the caller's Own, Accountable and Shared items. The `scope`, `owner` and `person` parameters are removed: `400 param_not_supported`. Old callers fail loudly, so they are not silently narrowed. New filter `view=mine` (default) or `view=shared-with-me`. `_MediaScope.app_ids=None` and `audit_everyone` are deleted |
| L2 download, posters | `require_content`, then mint a URL with that grant (L3) |
| L3 signed URLs | §3.4 |
| L4 `/me/api/receipts/{id}` | Own only. Other people's receipts return 404 for every role. The owner reads them at `GET /admin/api/activity/requests/{id}` as the projection. `receipt.detail.instance` audit is deleted, because the owner's access is now operational, not content |
| L5 activity, home, status, catalog, workflows | `/me/*` is always the caller's own. The owner branch at `user_runtime.py:350-361` is deleted, as is the `"scope": "instance"` label |
| L6 Usage | `/me/api/usage/summary` is own only; `person` is removed (`400 param_not_supported`). The owner's per-person usage moves to `GET /admin/api/usage?person=all\|<id>&from=&to=`, in the projection |
| L7 request log | Returns `project("receipt", …)`, not the admin audience. New `GET /admin/api/activity/requests/{id}` |
| L8 status | Delete `recent_videos`, `recent_images` and the `recent_renders` alias (`status_endpoint.py:3960-3963`). They are engine-folder paths to anyone's creations, and the Library already covers own work. `_live_video_jobs` uses the job projection. Attributed sections are owner-only and omitted for other admins: `key_name` on live jobs, `auth_keys` and per-key request rates (`status_endpoint.py:3333-3357`, `:3681`), and `recent_events`, which carry key name and path (`firehose_metrics.py:119-124`) |
| L9 keys on a member's App | Refused at creation (§3.5); existing ones revoked by the switch (§7) |
| `/gateway/v1/*`, `/v1/videos/*`, `/v1/responses/*`, voices, MCP | Call `content_grant` instead of their own checks. Behavior stays the same. L9 keys are revoked at the switch (§3.5, §7) |

**Route inventory.** A test lists every route in the FastAPI app. Each route
is classified as `content:<kind>`, `operational`, `own-account` or `none`. An
unclassified route fails CI. This follows the category route inventory that
HR "Support matrix" refers to.

**The owner's Library "Everyone" view is removed, not narrowed.** The owner's
Library is the same member Library (P8): their own work, what their
accountable Apps made, and "Shared with me". The owner's storage question
moves to Usage (§3.3). A "shared items only" Everyone view would duplicate
"Shared with me" (P2).

### 3.3 The operational projection (explicit allowlist)

`project()` builds a new dict from named fields. It never copies the record
and deletes fields from it. A field that is not listed here never reaches an
owner view of someone else's record. This adopts R "Operational metadata: an
allowlist", fitted to the receipt fields that exist (`receipts.py:27-58`,
`:448-461`).

| Kind | Kept | Dropped (examples present today) |
|---|---|---|
| Receipt | `request_id`; `created_at` and phase timestamps; person id and label; App label; `source_kind`; `protocol`, `stream`; `intent` (capability); `resolved_model`, `resolved_workflow`; `remote` (local or provider); `status`, `status_code`; `terminal_reason` (a closed enum, `receipts.py:185-187`); `latency_ms`; `usage` quantities; `cost` with quality (measured, estimated, unknown); `refusal` codes; policy decision codes; `resource_decision`; `attempt_number`; `job_id` | `path` (direct engine access records a caller-chosen path, `proxy.py:29964`; speech-to-text records `request.url.path`, `:4945`); `requested_model` (free text a member typed); `accepted_inputs`, `rejected_inputs`, `ignored_inputs`, `warnings`, `fallback_reasons` (free text that can echo input); `client_hint`; `request_shape`; litellm ids; `source_label` free text |
| Job | `job_id`, `intent`, model, workflow, `status`, phase, `created_at`, `updated_at`, latency; output quantities (count, width, height, duration, bytes) | `error` text (up to 500 characters, `proxy.py:21525`, `:22190`); `assets` and asset ids; `seed`; `input_hash`; `metadata` |
| Storage | Per person and per kind (image, video, audio, inputs, voices): item count and logical bytes. Server totals separately: physical bytes, and bytes that no person or App accounts for | Asset ids, names, dates of single items |

Model names and exact times can still reveal something; a model's name can
suggest what it makes. App and key names are account metadata that the owner
already manages in Apps. Member copy says the owner sees the model, the time
and the App used (§6). Receipts have no error layer field today. P3's layer
is derived from `terminal_reason` and `status_code` where it can be, and is
shown as unknown otherwise. This design adds no new free-text error field.
The plan's receipt detail setting (AP §2.6, `status_visibility.level`) controls only how
much a caller sees of **their own** receipts. It never widens the owner
projection.

### 3.4 Signed URLs carry a grant

The signed message becomes `asset_id:expires:md:g`. **Both `md` and `g` are
required.** Today `md` is optional in the message (`proxy.py:6846`), and a URL
without it is still served (`:23585`). `g` is `o` (own) or `s.<share_id>`.
Delivery (`_signed_asset_content_response`):
1. Verify the signature.
2. If `g=s.<id>`, the share must be active and must name this asset. Revoking
   a share therefore stops its live URLs at once, not up to an hour later.
3. A share grant never allows `md=full`.
4. A URL without `md` or `g` fails with 403, the same message as a bad
   signature. No shim (P11).

Every minting site takes a `Grant`, so a URL cannot be minted without one:
- `build_signed_asset_url` (`proxy.py:7172`) and `build_signed_image_url`
  (`:7186`)
- MCP results (`_mcp_store_asset`, `:14636`)
- generation responses on `/v1/images/*` and `/v1/videos/*`. The caller is the
  maker, so these mint with `o`.
- the Library download route, after `require_content`

The owner can no longer mint a URL for a member's creation (L2). The URL is
still a bearer token: whoever holds it can fetch bytes until it expires or the share ends. That is the
OpenAI image-URL contract (PM "OpenAI image URLs are bearer capabilities").

### 3.5 Who can create a key

A key reads everything its App made (rule 1). Creating a key is therefore a
content grant. **Only the App's accountable person can create keys for it**
(AP §2.4: `apps.owner_user_id`), for personal and service Apps alike.
`POST /admin/api/apps/{id}/keys` (`admin_api.py:2251-2278`, any admin today)
returns `403 not_your_app` for anyone else.

- The owner can still revoke and disable any key, and can change a service
  App's accountable person. That change moves who sees the App's work. It is
  audited and shown to the new person.
- Keys that someone other than the accountable person created are listed in
  the build preview. The switch revokes them once the owner approves (§7).
  This is the only way to close L9 for existing keys, because records do not
  say which key made them.

### 3.6 Logs and side stores

- Pin LiteLLM `litellm_settings.turn_off_message_logging: true` and
  `general_settings.store_prompts_in_spend_logs: false` in the generated
  config (`gateway/litellm_substrate.py:75-89`). Today LiteLLM defaults apply;
  its behavior is UNVERIFIED.
- `cache/key_activity.jsonl` (`firehose_metrics.py:69`) already strips
  prompt fields. It stays operational.
- The reverse proxy (Traefik) may log `sig=` query strings. That is UNVERIFIED
  and outside this repo. The ops note says to log paths without query strings
  for `/v1/assets/` and `/v1/images/`.

## 4. Sharing v1

**Smallest useful version: share one creation, read-only, with one or more
people. The owner is an ordinary recipient. The prompt is not included unless
the maker chooses it. The maker can revoke.** This follows Immich albums and
partner sharing (R §1), and Nextcloud and OneDrive "private until shared"
(R §11, §14). "Share with owner for help" is R's recommended alternative to an
admin backdoor ("Sharing model" 2).

### 4.1 Data

New table `shares` in `media_jobs.db`, next to `asset_prompts`:

```
share_id TEXT PRIMARY KEY, asset_id TEXT, maker_person_id TEXT,
recipient_person_id TEXT, include_prompt INTEGER, created_at INTEGER,
revoked_at INTEGER NULL, revoked_by TEXT NULL
CREATE UNIQUE INDEX shares_active ON shares(asset_id, recipient_person_id)
  WHERE revoked_at IS NULL
```

- Only the maker can share. The viewer's person must be in the creation's
  `maker_person_ids` (asset sidecar). Having a key to the App (rule 1) is
  not enough. An accountable-App item (rule 2) cannot be shared in v1,
  because it has no maker.
- **The prompt is read live.** With `include_prompt`, the recipient reads the
  maker's current `asset_prompts` row. If the maker erases or never saved the
  prompt, the recipient sees "Prompt not saved". There is no copy, so PM
  erasure keeps working.
- Sharing counts no extra storage. Bytes stay attributed to the maker
  (R "Storage and usage attribution" 1).
- The maker revokes, or the recipient removes it from their own Library. Both
  set `revoked_at`. Revoked rows are kept as history: DATA_RETENTION class
  (a), with no automatic expiry. Deleting the creation, or permanently
  removing either person, deletes their share rows.

### 4.2 Recipient view

- The shared creation, its kind, size, duration and model, and "From {maker}".
  The prompt only if included.
- No workflow download, no `md=full`, no receipt, no reuse ("Use again"),
  no re-sharing.

### 4.3 Notice

A share adds a Recent entry for the recipient: "{Name} shared a picture with
you." This changes UB §2.3, where a Recent entry is defined as "one
recipient's record of one job reaching its terminal state". It becomes "… or
one creation shared with them", read by the same `GET /me/api/recent`. There
is no toast backlog (UB §2.3). If Recent ships later than sharing, "Shared
with me" in the Library is the only signal.

### 4.4 Deferred explicitly

Each needs its own research and design:
- groups and albums
- public or link sharing
- edit or contribute rights, and re-sharing
- sharing chats, prompts alone, receipts or workflows
- share expiry (open question 3)
- partner-style whole-library sharing
- copying a shared creation into the recipient's Library
- notice when a recipient opens a share

## 5. Support, break-glass and moderation

**Decision: v1 has member-initiated sharing only. It adds no new product
path that lets the owner open member content.** The one elevation that
already exists, the password reset (L10), is already recorded. This design
makes the member's record of it name who issued the link.

Why:
- A break-glass path needs separate approval, a reason, expiry and a notice
  (R "Support and break-glass" 2-4). In a one-owner household nobody can
  approve it except the owner, so it would be ceremony with no real control.
  R: "dual approval may not be possible".
- The owner keeps host authority over the files (L11) whether or not the
  product has a path. A product break-glass adds code and UI but does not
  change what the owner can actually do. The honest-limits sentence (§6) is
  the true statement.
- Support works by sharing. The member shares the failing creation with the
  owner and can include the prompt. For a failure with no creation, the
  owner's projection shows the receipt's status, `status_code`,
  `terminal_reason`, model and timing (§3.3). That is the diagnostic R allows
  by default. It is less than a raw error; the member can say more by sharing.

The reset record: UB §3.4 already shows "Password changed {time}." as a
Security event in Account at the next sign-in. This design amends it:
- When the owner issued the link, the event reads "Password changed {time}
  with a reset link from {Owner}." This needs `issued_by` on the reset
  record, written by UB's redemption core. Today's `account.password.reset`
  audit row (`accounts.py:373`) does not say who issued the link, so the
  copy must not guess.
- The event stays in the member's Security list. The owner cannot remove it
  in the product. If the owner redeems the link and signs in as the member,
  the member still finds the event, and their old password no longer works.
  This makes the reset visible; it does not prevent it.

Moderation in v1 does not browse content (R "Moderation" 1-2):
- **Prevent** with plans: grants, blocked categories, caps (AP).
- **Stop** by disabling the person or revoking keys (existing actions).
- **Report** by a member sharing a creation with the owner.

There is no public sharing, so there is no public content to moderate.
Owner-initiated cancel of someone else's running job, without seeing its
content, belongs to the owner "Now" view (HR Release C item 2). It is not
added here.

## 6. Member wording

The copy follows VOICE.md: fact first, one next action, plain words, each
fact once per screen. `{Owner}` is the owner's display name
(`admin_store.owner_display_name`). Avoid "Only you can see this" and
"end-to-end encrypted" as unqualified claims (R "Proposed member wording").

| Where | Exact copy |
|---|---|
| Home, first sign-in for a member; once for existing members after the upgrade | Title "Your work is private". Body "{Owner} sees your usage, spend and storage, not your creations or prompts." Link "What {Owner} can see" (Account > Privacy). Button "OK" |
| Account > Privacy | Heading "Privacy". Rows: "Private to you: creations, prompts and saved chats." / "{Owner} sees: when you made something, the model, the app you used, how long it took, what it cost and your storage." / "Shared apps: work made through an app that several people use, like a chat bot, belongs to that app. The person responsible for the app can see it." / "Shared: only what you share, with the people you choose. Prompts only if you include them." → "Manage sharing". Then the honest-limits sentence |
| **Honest limits** (Account > Privacy; "What is saved?" in Try) | "{Owner} runs this gateway and can read the files it stores. Azizam keeps your work out of the owner's views, but it isn't end-to-end encrypted. The engine or provider that makes your work receives your prompt." |
| Account > Security, after an owner-issued reset (amends UB §3.4) | "Password changed {time} with a reset link from {Owner}." |
| Library detail | Button "Share" |
| Share sheet | Title "Share this {picture\|video\|audio}". List "With", with each person's name; the owner's row reads "{Name} (owner)" (ban list 7: no "A · B" strings). Switch "Include prompt", off by default. Note when off: "They see the {result} and its model. Not your prompt." When on: "They also see: “{prompt}”". With no saved prompt the switch is disabled: "Prompt not saved, so it can't be shared." Footer: "They can download it." Primary "Share with {Name}" / "Share with {n} people" |
| After sharing | The sheet closes. The detail shows the badge "Shared with {Name}" or "Shared with {n} people" as the confirmation (no toast, P2); tap opens the list |
| Stop sharing | "Stop sharing with {Name}? They lose access now. Copies they downloaded stay with them." Buttons "Stop sharing" / "Cancel" |
| Recipient | Library filter "Shared with me". Caption "From {Name}". Action "Remove from my Library", note "{Name} still has it." Empty state "Nothing is shared with you yet." Stale open "{Name} stopped sharing this." |
| Recent | "{Name} shared a {picture} with you." |
| Owner section of Usage, per-person view (once, under the person picker) | "Creations and prompts stay private to each person." |
| Owner request detail, someone else's request | Row "Result" with value "Private to {Name}" in place of "Open result" |
| Invite sheet (owner) | "{Name} gets a private Library. You see their usage, spend and storage." |

The owner gets no separate upgrade notice. The removed person picker in
Library and the Usage caption carry the change.

### 6.1 Changes to "Don't save this prompt" (UB §4.4)

| UB state | Old copy | New copy |
|---|---|---|
| Switch off (saving), note | "Saved with the {result}. Only you see it in your Library." | "Saved privately with the {result} in your Library." |
| Disclosure body | "… The {result} and a record of the request (time, model, cost) stay in your account. This doesn't hide your activity from the owner. The engine or provider that makes it follows its own rules. …" | "Azizam uses your prompt to make this, then doesn't save it. The {result} and a record of the request (time, model, cost) stay in your account. {Owner} sees that record, not the {result}. The engine or provider that makes it follows its own rules. The {result} itself may contain words from your prompt." |

The UB §4.2 sentence "the owner and the engine or provider are outside this
promise" becomes: "the owner sees the request record, not the prompt; the
engine or provider is outside this promise". PM's viewer table gains a row,
"Share recipient: prompt only if included; never workflow download". PM's
line "Library visibility uses App access" becomes "Library visibility uses
`content_access`".

## 7. Data and migration

- **No content moves, and no grants are created.** Visibility is computed by
  the core, so all existing creations, prompts, chats, receipts and jobs are
  private to their maker (or the accountable person) from the first start.
  The `shares` table starts empty.
- **Removed** (P11, no aliases):
  - the `scope`, `owner` and `person` parameters on `/me/*`
  - the `"scope": "instance"` responses
  - `_audit_everyone` and its four actions
  - `receipt.detail.instance`
  - `recent_videos`, `recent_images` and `recent_renders` in status
  - the Library person picker, and the Usage person picker outside the owner
    section
  - the `scope=everyone` web calls (`buildMediaPath`, `library/page.tsx:244-245`;
    `:468`, `:483`, `:948`, `:997`, `:1624`, `:1647`, `:2274`)
  - signed URLs without `md` or `g`

  Audit rows already written keep their labels in
  `web/src/lib/audit-description.ts:4-5`. They are a historical record, not
  live behavior.
- **Build preview** (read-only, run before the switch, using the stopped
  gateway's copy as AP §4 does). It lists:
  - keys created by someone other than their App's accountable person
  - creations and receipts with no person and no App (open question 2)
  - `superadmin` role rows
  - the count of creations per person

  The owner approves the preview. The switch then revokes the listed keys,
  maps `superadmin` rows to `owner` or `admin`, and applies the open
  question 2 choice. Nothing else changes.
- **Backups.** Ops note: restoring an older `media_jobs.db` can bring back
  revoked shares; re-apply current revocations after a restore.

## 8. API, CLI and MCP first; then tests

### 8.1 Shape

| Operation | REST | CLI (HR Release B item 4) | MCP |
|---|---|---|---|
| List own or shared creations | `GET /me/api/media?view=mine\|shared-with-me` | `azizam library list [--shared-with-me] --json` | Not supported. Explicit error per HR "Support matrix" |
| Share or update | `PUT /me/api/media/{asset_id}/shares/{person_id}` `{include_prompt}`. Idempotent; 201 or 200 | `azizam share add <asset> <person> [--include-prompt]` | Not supported |
| Revoke (maker) or remove (recipient) | `DELETE /me/api/shares/{share_id}` | `azizam share rm <share_id>` | Not supported |
| List shares | `GET /me/api/shares?direction=by-me\|with-me` | `azizam share list` | Not supported |
| Share targets | `GET /me/api/people/share-targets`: enabled people, `id` and display name, owner flagged | (used by `share add`) | — |
| Owner usage | `GET /admin/api/usage?person=all\|<id>&from&to` (projection) | `azizam admin usage …` | Owner MCP deferred (HR "Later") |
| Owner request log | `GET /admin/api/activity/requests[/{id}]` (projection) | `azizam admin activity …` | Deferred |
| Owner storage | `GET /admin/api/storage` | `azizam admin storage` | Deferred |

Errors:
- `400 param_not_supported` for a removed parameter.
- `404 not_found` for a record the caller has no grant to. Existence is not
  revealed.
- `403 not_your_app` (§3.5).
- `409 share_not_allowed` when the item has no maker or its prompt
  visibility cannot be decided.
- `403` "Invalid or expired signature" for a revoked or ungranted URL.

There is no CLI in this repo today. The CLI rows depend on HR Release B
item 4. The Privacy page is static copy, so it has no API.

### 8.2 Tests (the oracle)

1. **Authorization matrix, per read path.** Viewers:
   - maker
   - another member
   - owner
   - admin (not owner)
   - share recipient
   - recipient after revoke
   - the maker's personal key
   - a key created by someone other than the App's accountable person
     (refused at creation; an existing one is revoked by the switch)
   - a service-App key
   - the service App's accountable person, and another admin
   - signed-URL holder
   - no auth

   Paths: every route the inventory test classifies `content:*` or
   `operational`. This includes all L1-L8 routes, `/gateway/v1/*`,
   `/v1/videos/*`, `/v1/responses/*`, voices, and MCP `run` and `job`. Each
   cell asserts 200 with full data, 200 with the projection, or 404. The test
   is table-driven and fixed in one file, so a reviewer can read it.
2. **Route inventory.** Every route is classified, and every `content:*`
   route calls `content_access` (checked with a recording test double).
3. **No-leak canary.** A member makes an image, a video, a chat with
   `store:true`, and a failing request whose provider error echoes the input.
   Each carries the canary `CANARY-7f3a` in the prompt, the input file name,
   a direct engine access path and the provider error. App and key names are
   account metadata (§3.3), so the canary is not put there. As the owner and
   as an admin, call every route the
   inventory marks `operational` or `own-account`. Assert that no response
   contains:
   - the canary
   - the member's asset ids
   - any `sig=`
   - any `/v1/assets/` URL

   Also scan captured stdout and stderr, `cache/key_activity.jsonl` and the
   generated LiteLLM config flags.
4. **Signed URLs.**
   - The owner cannot mint a URL for a member's creation.
   - Revoking a share makes its URL return 403 on the next fetch.
   - Changing `g` breaks the signature.
   - A share grant with `md=full` fails.
   - A URL without `md` or without `g` fails.
   - Every minting site in §3.4 needs a `Grant` (a type check and one test
     per site).
5. **Sharing.**
   - With `include_prompt=false`, no response to the recipient (list,
     detail, download, search with `q`) contains the prompt.
   - With `include_prompt=true`, after the maker erases prompts, the
     recipient sees `prompt: null` and "Prompt not saved".
   - Deleting the creation removes its shares.
6. **Projection.** Feeding a receipt and a job with every known field gives
   exactly the §3.3 keys. A new field added to receipts is absent until it is
   listed.
7. **Keys.**
   - `403 not_your_app` for anyone but the App's accountable person, on
     personal and service Apps.
   - The switch revokes the keys the preview listed, and only those.
8. **Reset event.** An owner-issued reset shows "with a reset link from
   {Owner}". A reset with no recorded issuer shows UB's plain event.
9. **Web (390 px, P4).**
   - The owner's Library has no person picker.
   - The share flow, stop sharing, and "Shared with me" work by touch.
   - The owner's Usage per-person view shows no thumbnails.
   - The member first-use notice shows once.

   Rewrite the §1.4 specs to these expectations. Do not delete them.

## 9. Risks and open questions

### 9.1 Risks

| Risk | Mitigation |
|---|---|
| Members read "private" as protection from the host | The honest-limits sentence sits one step from every private claim. It never says "only you" |
| Metadata still reveals behavior (models, times, frequency) | Disclosed in Privacy. The projection drops free text and keeps quantities. Timing side channels in a small household are acknowledged, not denied (HR) |
| A route added later skips the core | The route inventory test fails CI for any unclassified route |
| Live bearer URLs after revoke | `g` grant is checked on every fetch. Downloaded copies cannot be recalled; the copy says so |
| An owner who relied on Everyone loses visibility of a child's work | Plans prevent; supervision is deferred with fixed constraints (§2.3, question 1) |
| Reverse-proxy logs hold signed URLs; LiteLLM logging defaults | Ops note; config pin; both UNVERIFIED |
| A backup restore brings back revoked shares | Ops note (§7) |
| Shared-App work is visible to the App's accountable person, who may not be the person who asked for it | Disclosed on the Privacy page. Key creation is limited to that person (§3.5). Attributing shared-App work to the person who started it needs identity from the app, which is deferred |
| Revoking the keys in the preview breaks an app that uses one | The preview names each key and its App. The accountable person creates a replacement key before the switch |
| Wide change: about 25 routes and the web Library, Usage and Home | Land the core and the projection first with no behavior change, proven by the current tests. Then switch reads in one change, with the matrix test as the gate |

### 9.2 Open owner questions (genuine forks; each has a default)

1. **Supervised accounts for children.** Do you need a supervised mode now,
   where you can see a specific person's creations with their knowledge?
   **Default: defer.** Plans (blocked categories, caps, capability grants)
   prevent rather than watch. Supervision gets its own research (Family
   Link, Screen Time) under the §2.3 constraints.
2. **Creations with no recorded person or App.** These are older keyless-LAN
   records. No member was ever bound to them, and access plans stop new ones.
   The build preview counts them, and the switch settles them once, with no
   runtime branch. **Default: attach them to one new service App, "No person
   recorded", with you as its accountable person.** Rule 2 then applies:
   they appear in your Library, where you can keep or delete each one (P19).
   The alternative is deletion after a preview. R warns against assigning
   such records "to a convenient current member", so neither option assigns
   them to a member.
3. **Share expiry.** Should a share to the owner for help end by itself, for
   example after 7 days? Support-access precedents (Customer Lockbox,
   Bitwarden emergency access) are time-limited; ordinary shares (Immich,
   Nextcloud, OneDrive) are not. **Default: no expiry.** A help share is an
   ordinary share, the badge keeps it visible, and the maker can stop it at
   any time.

## 10. Before building (P20)

- [ ] Red-team this design with a non-maker panel. One non-maker review
      pass already ran before this commit; its 11 findings are applied (see
      the change log). Remaining areas: grant rule 2 and the AP §2.4
      amendment, projection completeness against live receipts, URL grants,
      wording against VOICE and R.
- [ ] Owner answers or accepts the defaults in §9.2.
- [ ] Record the go decision against the reviewed version.
- [ ] Sequence:
      1. core and projection, no behavior change
      2. switch reads, route inventory test and matrix test
      3. sharing
      4. wording
      5. update PM, UB §4 and DATA_RETENTION

## Change log

- v1, 2026-10-11. First draft, then one non-maker review pass. Applied:
  - Rule 1 keeps the App-level rule, because records store `app:<id>`, not
    the key. L9 is closed at key creation and by revoking listed keys.
  - Only the accountable person can create keys, on service Apps too.
  - Rule 2 is declared an AP §2.4 amendment and disclosed to members.
  - Records with no person and no App are settled in the build preview, with
    no runtime role branch.
  - Status: `recent_videos` is deleted too, and `auth_keys`, per-key rates
    and `recent_events` are hidden from non-owner admins.
  - The projection keeps `terminal_reason` and drops `path`. It claims no
    error code or layer field that does not exist.
  - The reset notice is folded into UB §3.4's Security event, with the
    issuer recorded.
  - `md` and `g` are required on signed URLs, and the minting sites are
    listed.
  - Cross-references corrected: UB §2.3, HR Release B item 4, HR Release C
    item 2.
  - Copy follows VOICE: no heading repeat, "spend", no toast duplicating the
    badge.
  - Cut: the owner upgrade notice, the second revoke route,
    `/me/api/privacy`, the long supervision constraint list and the restore
    rule.

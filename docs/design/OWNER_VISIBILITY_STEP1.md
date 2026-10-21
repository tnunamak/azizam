# Owner visibility step 1: implementation and current-main differences

Approved policy: v3 at `782d763f`, D51 (2026-10-12, confirmed 2026-10-20).
Implementation base: `origin/main` at `52408b268215585dab40299f2ebc2bd920ab6bff`.
This step consolidates existing checks. It does not switch visibility, issue
privacy permissions, change saved records, or serve the owner projection.

## Core and the lead's amendment

`gateway/content_access.py` contains immutable Viewer/resource/access types and
the pure decision function. Roles affect only `operational()`. Principal aliases
are resolved by the existing trusted callers, outside the pure core.

| Kind | Read | Delete | Reuse | Share | Link | Cancel |
|---|---|---|---|---|---|---|
| creation | Own or Shared | Own | Own | Own and sole Maker | Own or Shared | Deny |
| prompt | Self/App-maker (R4), or included share | Self/App-maker (R4) | Self/App-maker (R4) | Deny | Deny | Deny |
| chat | Self/App-maker (R4) | Self/App-maker (R4) | Self/App-maker (R4) | Deny | Deny | Deny |
| voice | Own | Own | Own | Deny | Deny | Deny |
| receipt | Own | Deny | Deny | Deny | Deny | Deny |
| job | Own | Deny | Deny | Deny | Deny | Own |
| request_history | Self/App-maker (R4) | Self/App-maker (R4) | Self/App-maker (R4) | Deny | Deny | Deny |
| debug_capture | Self/App-maker (R4) | Self/App-maker (R4) | Deny | Deny | Deny | Deny |

This includes the lead's round-4 refinement on 2026-10-20, which supersedes the
earlier blanket permission requirement and v3's chat Self+App restriction. The
creating-credential refinement for v4.2 distinguishes MCP from browser origins:

- A session reads its own person's content across Apps without a privacy flag.
- An App key reads its person's content made by its exact non-null App without
  the flag. Keys within an App share that authority; key-level equality is not
  needed. Other App origins require privacy_permission.
- An MCP grant reads its person's content made through its exact non-null
  connection_id without the flag. Browser rows on the shared session App, other
  connections, and unattributed legacy rows require privacy_permission. Null or
  empty connection ids never establish creating-credential equality.
- The same identity rule applies to allowed delete/reuse actions; debug_capture
  never permits reuse.
- A row with no person belongs to its creator App. Only a caller with no person
  and the exact same non-null app_id satisfies App-maker; no person, including
  an accountable owner or a holder of privacy permission, may read that row.
- Included prompt shares remain read-only. A delegated recipient needs privacy
  permission; a recipient session does not.

Creator app_id and connection_id are trusted submission attribution, never an
inference from current App accountability or an intersection of readable owner
keys. Viewer.delegation distinguishes App keys from MCP grants, including a grant
whose connection id is missing. Step 3 must record connection/credential origin
on new content writes and normalize legacy missing origins as browser-created
for MCP access. This step adds typed attribution only; it changes no write path.
O6 still applies only to creations, voices, jobs and receipts. The new delegation/permission fields
have no credential storage or issuance surface in this step; no history/debug
capture route is added.
Shares are typed, supplied evidence in pure tests; there is no share store or
production share resolver yet. Own precedes Shared. An unavailable resource is
Denied even if a stale share exists. A prompt share selects its maker's row only.

## Trusted identity sources for the eventual Viewer builder

| Source | Current main authority | Viewer identity and principal set | Privacy permission |
|---|---|---|---|
| Browser session | `admin_runtime.runtime_identity_for_session_app`; `me_media._member_media_scope` | Session person, surface App; every accountable App plus `user:<person>` and historical aliases | Not needed (`delegation=None`) |
| App key | `admin_runtime.person_for_app_credential`, `runtime_identity_for_app_credential_hash`; `ownership.readable_owner_keys` | Its App only; a person only for a personal App whose key was made by its accountable person | Only cross-App/session person content needs it (`delegation="app_key"`) |
| Appless key | `proxy._responses_scope`; `ownership.readable_owner_keys` | Trusted credential hash; no App. Person content requires Self and explicit privacy permission | Only cross-App/session person content needs it (`delegation="app_key"`) |
| Trusted address | `admin_runtime.runtime_identity_for_trusted_address` (2457-2482 on main) | `app:<id>`, no person; never the shared network as a person | False; exact-App App-maker rows only |
| MCP OAuth | `proxy._authenticate_mcp_access_token` (2425-2453), `_mcp_identity_for_person` | Grant person through that person's session App; session-like accountable principals. `connection_id` identifies the grant at the auth boundary | Only exact creating-connection rows bypass the flag; browser, other-connection and legacy rows need it (`delegation="mcp_grant"`) |
| Control-plane service account | Runtime authentication rejects it on content routes | No content Viewer | False |
| Missing identity | No trusted person, App, credential or principal | No content basis | False |

These are normalization requirements, not a new auth system. Existing call-site
scopes remain in place. In particular, current MCP asset callbacks resolve their
runtime App's principal, whereas the target session-like Viewer includes all the
person's accountable Apps. Step 3 must reconcile that difference explicitly.
Client body/query identity assertions must never build a Viewer.

## Preserved exceptions and step-3 work

Search production code for `OWNER_VISIBILITY_STEP3`. These markers name the
exceptions rather than moving them into the matrix:

1. `me_media._media_scope` still accepts Everyone and other-person selectors;
   `_record_in_scope` still accepts `app_ids=None`. `user_runtime.build_user_activity_snapshot`
   still requests all Apps for owners. Receipt detail retains its owner bypass.
2. `proxy._asset_access_allowed` and `_video_job_access_allowed` still permit
   absent ownership for a caller with no identity. The video exception was also
   present in r18, but the step-1 build note mentioned only the asset exception.
3. `retention.conversation_row_visible` retains today's narrow App/legacy-key
   scope before calling the core. Its appless null-person/key match remains a
   legacy exception; exact-App App-maker access is now part of the R4 matrix.
   The old scope carries no delegation permission, so its existing delegated
   reads keep a temporary grant at the call site. `_responses_row_visible`
   explicitly names this delegation. Key-API prompt reads in
   `_tracked_video_fields`, `proxy_graph_video_content`, and the key-fetcher
   md=full restore in `_signed_asset_content_response` have their own markers.
   Browser Library prompt reads remain session reads.
4. REST and MCP retention ledgers still accept today's management-role access.
   A request-specific ledger is classified `content:receipt:read`; the ledger
   without a request id is `none`. Step 3 must require Own before reading a
   request-specific ledger, including its observed/requested-model envelope.
5. `media_jobs.append_event` still accepts any nonempty trusted event type.
   Its SQL sink, literal writers, dynamic writers and resolved current types are
   frozen by the inventory. Runtime type restriction remains a step-3 change.

## Current main differs from v3's r18 assumptions

The [citation audit](OWNER_VISIBILITY_CITATION_AUDIT.md) compares 225 historical
ranges in 45 files. The following semantic differences matter to later steps;
line references here name current-main symbols rather than stale v3 offsets.

| Area | Current main | Consequence for step 3 / later steps |
|---|---|---|
| Library scope | `_media_scope` moved from 461-480 to 737-759; `_member_media_scope` starts at 761; receipt/job filters moved to 1370/1381 before this branch | Remove the actual Everyone, person and local-network branches; do not edit the old line ranges |
| Activity scope | `user_runtime.build_user_activity_snapshot` uses `app_ids=None if owner else owned` at 311 and still returns `scope=instance` at 320 | Owner feed and scope label still need removal |
| Asset/video checks | `_asset_access_allowed` moved to 6969; `_video_job_access_allowed` to 9374 | Preserve signed-delivery checks and both no-identity exceptions until the switch |
| Responses | `_responses_row_visible` at 25886 delegates to `retention.conversation_row_visible` at 472 | There is already one shared Responses retention/access predicate; change that predicate, not a duplicate in proxy. Appless null-person/key equality is a separate legacy exception; cross-App session/granted reads are part of the R4 target but are not enabled at the old call site |
| Receipt/job ownership | `record_ownership.owns_record` uses immutable App attribution first and then the stored key hash; plans, names, parent receipt ids and output asset references are not authority | Preserve explicit-App precedence and historical aliases. Do not let an owned parent vouch for a foreign output |
| App ownership | Schema 7 and `access_plan_migration.py` already give service Apps a human accountable person; `admin_store.create_app` at 1735 accepts service+person; service-account App ownership column is gone | v3's assertion that service+person cannot exist is obsolete. Do not rerun its old assignment procedure blindly |
| App creation/constraints | `admin_runtime` no longer turns every human-owned App into personal kind. Existing schema triggers protect session Apps and plan assignments, but v3's complete O2-O4 immutability/issuance trigger set and `ownership_version=1` are absent | Step 2 is not wholly complete merely because AP landed. Verify the current schema and migration before adding the remaining constraints |
| Creator provenance | `_backfill_credential_creators` moved to 612 and still runs at initialization; unknown creators stay NULL rather than being uniformly rewritten to an empty string. Admin issuance still records the actor | Preserve NULL-safe handling. Key issuance and creator enforcement remain a prerequisite to the switch |
| Key identity | `person_for_app_credential` moved to 2312; personal owner-created keys still get a person, service/personless keys do not. RuntimeIdentity now separates `person_id` from `accountable_person_id` | Never substitute the spending/accountable person for Self |
| Trusted addresses | Keyless traffic can now authenticate as a configured App with no person, rather than all keyless traffic being absent identity | Keep that App's Own access; never give it prompt/chat/history/debug Self access |
| MCP OAuth | OAuth grants and browser work share the person's session App; the grant has a `connection_id`. Newer callback fields and tool visibility gates exist | Same app_id does not prove that a grant created a row. Resolve privacy permission from the grant and compare trusted creating connection_id; absent/legacy origins require the flag |
| Sensitive creating-credential attribution | Current `asset_prompts` rows are keyed by asset/person and have no creator app_id. Older chat rows may use only a legacy key hash; future history/debug rows need creator attribution too | Step 3 write paths must persist trusted app_id and connection/credential origin. App keys compare App identity; MCP grants compare creating connection_id. Unattributed legacy rows count as browser-created for MCP, even on its session App |
| Receipt retention/D48 | `receipt_store._filter_for_audience` compares `maker_person_id` with `viewer_person_id`; non-makers get only retention kind/decision, even in admin audience. Receipt serialization now has closed retention kinds/decisions/reasons/parts | Keep this masking. The new owner projection uses the current closed retention vocabulary, without reason/deadline/parts |
| Receipt admin fields | Admin receipts now include `client_address`, `api_key_name`, `connection_id`, `retention_facts`, and sanitized STT/decision metadata | Drop address/key-name and arbitrary metadata from the owner projection. They must not reappear through an envelope or nested map |
| Retention ledger | REST current/request-specific ledgers, service/provider/system facts and MCP `requests(action=ledger)` did not exist in v3 | Current global facts are `none`; request-specific evidence requires Own. Management-role access is preserved only as a named exception |
| MCP callbacks/tools | There are 18 McpDeps fields (15 callable), seven tools, STT's six actions, renaming and retention-ledger operations | The old eight-callback table is incomplete. The fixture records every field, tool schema, dispatch fingerprint and explicit suboperation |
| Multipart/transient files | `request_forms.read_form` owns multipart spill cleanup, including interrupted parsing. `private_tmp` validates ancestors, owns private temporary storage and worker lifetime, and is used by voice/STT paths | Authorization must precede prohibited byte reads without bypassing these cleanup/lifetime boundaries. Do not restore the old raw form/spool code |
| STT admission | `stt_chunking.py` has grown far beyond the old 560-595 spool check: staged reservations, bounded WAV preparation, whole-file verification, sample-exact cuts, replay storage and cancellation cleanup now exist | Reuse current resource/disk admission. Do not reintroduce a second decode, unbounded spool, or a new independent reservation system |
| STT operator surfaces | Measurement/settings REST routes and MCP `stt_measurement` are new, with durable jobs, public sample rights/provenance and cancellation | Classified operational per the lead. Keep their public-sample contract; they are not a grant to member content |
| Voices | `voices_api` now has consent, rename/update and sample routes, using private_tmp/request_forms and guarded reference processing | Include consent/sample paths, not only the old principal-folder check, when step 3 connects the voice kind |
| Status/logs | `status_endpoint.py` still returns recent media and live jobs, now around 3640-3981. MCP still prints generic-exception text; new STT/retention error paths also exist | The original line list and exception count are stale. Re-audit current sinks and all status aliases before making a privacy claim |
| Delete/storage | `me_media` deletion moved under the same cache lock, still refusing physical deletion for multiple owners/makers and retaining audit tombstones | v3's claim-release/storage accounting work is still needed. Do not equate current deletion with released principal claims |
| Existing action mismatches | Video DELETE still uses job-read ownership; MCP video cancel dispatches to that DELETE. Client event append also uses job ownership; chat cancel is an unsupported route | The inventory names these current contracts. Do not infer new job-delete/append or chat-cancel permission from the final matrix; resolve them at the switch |
| Projection infrastructure | The existing management `projection.py`/`projection_types.py` is for configuration/secret redaction, not the member-content owner view | `owner_projection.py` is a separate, unserved schema. It builds nested values and envelopes anew using trusted account/catalog names |
| Web/defaults | Main's Library still defaults owners to Everyone. Branch A (`fix/library-owner-default-mine`, `f12fa877`) separately changes default/list/detail/poster/download scope and labels to Mine | Branch B intentionally starts from main and does not include A. Step 3 still removes Everyone/person controls and moves owner usage |
| Browser fixtures/tests | Current Vitest/Playwright specs and state fixtures differ from r18; the Library page/default tests are at later lines | Update current specs, not historical offsets. Keep UI_E2E_STATE_ROOT outside the checkout and restore tracked web state before committing |

## Projection and inventory limits

`owner_projection.py` builds receipt, job, storage and receipt-list values with
recursive closed schemas. Person labels use the submission person's trusted id;
model/workflow labels come from supplied catalog lookups, with no requested-model
fallback. Protocol identifiers come from the existing protocol catalog data.
The schema test rejects unknown nested fields and untyped strings, except names
whose lookup provenance is tested separately. Raw data bags never pass through.
No endpoint or MCP callback serves this projection in step 1.

The registration inventory has 450 route-method entries, including registered
HEAD and non-HTTP entries, all McpDeps fields, tool schemas/suboperations, dispatch
fingerprints and production event writers (including the SQL sink). Its 12 event
types are today's emitted/validated types, not a claim that append_event already
rejects all others. Classifications state the relevant core checks; they do not
prove that every current route enforces them. Existing route graph and headless
access tests remain separate behavior gates.

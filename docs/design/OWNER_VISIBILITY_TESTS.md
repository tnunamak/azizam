# Authorization test design: Owner visibility v4

Adopted from the v2 test-design pass and revised through v3 and round 3.
`[v3]` marks an earlier revision; `[v4]` marks a round-3 requirement.
OV means `docs/design/OWNER_VISIBILITY.md`; its §2 policy is unchanged.
Code evidence throughout this file is `origin/main` at
`52408b268215585dab40299f2ebc2bd920ab6bff` (2026-10-20).
The source map in §10 replaces all old file:line offsets and source links.
D46-D51 were read in `~/Documents/azizam/engine-contract/DECISIONS.md`.
This file specifies future tests. No implementation tests were run and no
notice readiness is claimed. Historical `CURRENT VERIFIED` labels below
mean source-inspected behavior, never an executed pass. Sections 1-9 give
target assertions; §10 identifies current-main adapters and limitations.

## 1. Blocking conclusions and test-oracle authority

The v4 design is not built. Source-confirmed current behavior must not become the privacy oracle:
- Library owner/superadmin broadening remains, including a local-network detail/download bypass. Regression tests must require the target denial rather than bless this behavior.
- MCP has direct asset read/store callbacks in addition to REST dispatch. Route authorization tests alone cannot cover input reuse, returned bytes, signed-link minting, or discovery content.
- Receipt storage forbids sensitive key names but not sensitive values. Whole-envelope recursive typed projection, not the existing structural guard, is the target owner boundary.
[v4] - AP v4's accountable-person schema is already on main. Step 2 still enforces O2/O3/O4 on keys AND trusted addresses. Test the real schema-v6→current upgrade before post-migration trigger installation, current→enforced and fresh initialization; do not treat AP being present as verify-only completion. See §10 current-main evidence.
[v3] - RESOLVED in v3 (OV §3.1): a session Viewer intentionally includes every App the person is accountable for, of both kinds (OV §2.4 O6). There is no Accountable basis. Test that the production Viewer builder includes service Apps for the session and only its own App for a key; prompts and chats stay Self and Self+App.
- Trigger SQL, migration machinery, content_access, share storage/serialization, signed delivery, metadata stripping, route registry, full log pipeline, actual export handlers and storage admission implementation are UNVERIFIED under this allow-list.
[v3] - RESOLVED in v3 (OV §6.4): the disk pause is an absolute byte rule, free_bytes - leased_bytes >= reserve_bytes, with reserve_bytes defaulting to W_max + 2 GiB and in-process per-job leases. Percent is display only.

Normative ordering for these proposed tests: authenticated identity first; removed-parameter validation on authenticated /me requests; resource existence/authorization core; audience-specific serializer; side effect/bytes/provider boundary. Authentication error status and unsupported-MCP wire encoding are UNVERIFIED; lock their established transport contract after permitted implementation inspection rather than inventing 401/403 mappings here. Every resource-level denial below is exactly HTTP 404 with error code not_found, equivalent in semantic status/code through MCP dispatch, unless a more specific explicit error below applies.

No notice may ship because these tests were designed. Notice gate requires implemented full path coverage, executed success evidence, resolution of the design conflicts and deployment log checks.

## 2. Fixture dimensions and complete cross-product rule

Parameterize every applicable PathSpec over every actor below and every state below, plus kind/action. Do not sample only representative actors. The policy function below defines every resulting cell; a transport applicability adapter distinguishes an unsupported protocol from a content denial. State labels alone never grant authority: compute the independently seeded record fields and grants.

Actors (identity rows step 1; binding enforcement step 2):
A: member A session, plus A personal owner-created key as a companion transport.
B: member B session/key, with no relationship to A.
O: owner person session/control-plane credential. Also seed O's genuine own records; owner role never changes a content result.
D: admin-not-owner person session/control-plane credential. Also seed D's genuine own records; admin role never changes a content result.
[v3] S: active service-App key for app S; person_id=None; canonical principal app:S plus its historical key aliases. This is Own for S records, never Self.
[v4] T: trusted-address request with canonical app:T, person_id=None, trusted address from runtime auth, no credential hash. Test personal and service Apps; no caller-supplied forwarding/body identity may add a person. Address creator provenance controls issuance, never Self.
N: active personless personal-App key (created_by is another person, NULL, or empty string), and active appless/service-account credential variants. Before migration, its same-App creation Own behavior is explicit; after switch the bad-provenance personal key must no longer authenticate. An unrelated control-plane principal owns no member content.
Rk: revoked key, with the exact same principal and historical resource as a live replacement key.
Rs: revoked/expired session for each person, and disabled person with stale session.
Q: current recipient of A's single-creation share, include_prompt false/true; repeat with B/O/D as recipient. Sharing is a relation, not a role.
F: former recipient after maker stop, recipient remove, include_prompt downgrade for prompt only, and delete-triggered revocation; repeat with B/O/D.
[v3] C: accountable person of service S, accessed through a session without authenticating as S. Under v3, C's session is Own for S records (OV §2.4 O6). Essential additional actor, even when C is O or A.
H: unauthenticated bearer URL holder; essential signed-delivery actor.
U: unauthenticated without a bearer URL.

[v4] For A/B/O/D repeat browser session, personal key with and without privacy_permission, and MCP grant with and without that permission. MCP grant Viewer has the person's session App and all accountable Apps, like a session; a personal key still has only its own App. Seed auth_kind and trusted_address from production builders. A valid service/trusted-address identity has no person and can never obtain privacy content through permission alone.

Seed all actor identities from trusted auth fixtures. Request JSON/query parameters claiming person_id, app_id, owner_keys, is_owner, privacy_permission, auth_kind, trusted_address, maker or basis must not alter the Viewer. Personal-key created_by matching owner is independently checked. Record/account names must be safe controlled strings distinct from canaries.

Resources/states:
own: canonical principal matches actor for creation/voice/receipt/job; Self matches prompt; Self+App matches chat. For N/S own does NOT mean a prompt gains a person.
other: A's record, no principal/person/basis match.
shared: A's existing sole-maker creation and separate poster shared to Q; optional live prompt row; other kinds get NO authority merely because associated creation is shared.
unshared: same resources before share, plus another asset by the same maker and siblings/inputs/workflow/receipt/job. A grant is not transitive.
deleted: (a) missing record/bytes, (b) deleted by A while bytes survive for co-owner B, (c) orphan poster, (d) dangling/revoked share. A/F lose access; B retains only B's surviving Own association.
unattributed: (a) legacy hash resolvable to an App, (b) maker metadata plus resolver evidence, (c) proven unbound migrated to designated service App, (d) unresolved or contradictory attribution. Only evidence-backed resolution/migration grants; unresolved denies everyone including owner. Operational quantities may include unresolved storage without revealing individual assets.

[v3] For each state seed fresh independent DB/file fixtures. A share to Q must not accidentally also put A's App principal in Q.owner_keys. Own+Shared collisions are a separate precedence test: decide returns Own, the own serializer is used, and prompt reads stay a separate prompt decision. After a share stops, independently held Own authority remains. Do not assert that stopping a share revokes unrelated ownership.

Resource/action oracle, generated exhaustively for all eight kinds x all six actions (step 1; enforcement step 3):
[v3] creation read = Own OR active Shared for that exact asset/poster.
[v3] creation delete = Own.
creation reuse = Own only.
creation share = Maker (Own AND exactly one non-null maker equal to Viewer.person_id); then supported container checks.
[v3] creation link = Own mints g=o; Shared-only mints g=s.<share_id>. Shared-only cannot request g=o.
prompt read = Self OR exact active share with include_prompt true, reading its authoritative maker's live prompt row.
prompt delete/reuse = Self only. Share does not authorize md=full or workflow reuse.
chat read/delete/reuse = non-null Self AND same app_id; appless variant requires the same owner_key_hash. Neither matching person alone nor matching App alone suffices.
[v4] request_history/debug_capture read/delete/reuse = non-null Self, plus privacy_permission for a personal key or MCP grant; browser session qualifies directly. Apply retention readers narrowing, expiry and erasure after the core floor. All other actions deny; App Own/role/share never grants these kinds. A personal key with permission but different person denies. Maker_signed_in denies delegated reads even with permission.
voice read/delete/reuse = Own.
[v3] receipt read = Own; receipt delete/reuse/share/link/cancel denied as core actions.
[v3] job read/cancel = Own; other actions denied.
All omitted cells = Deny. Owner/admin role is not an extra basis.
[v3] Job EVENT streams follow job read: Own. Shared never gets jobs/events.
[v3] A valid no-person service key can Own/reuse its creations but cannot Self-read any saved prompt or chat. A session that owns a service App reads none of its prompts or chats (its records have no maker person, OV §3.1). Null == null is never Self.
No identity = Deny, except validated signed delivery is a separate bearer-capability path.
Deleted current resource = not_found even if an old principal/share used to match.
Share creation refusal: an authorized Own creator with multiple makers gets 409 share_not_allowed/reason several_makers; zero makers gets no_maker; unsupported GIF gets file_type. An unrelated caller gets generic 404 before maker/container diagnostics, to avoid existence leakage.
[v3] Share refusal precedence is locked (OV §3.1): a caller who cannot read the creation gets 404 not_found before any share diagnostic; a caller who can read it gets 409 share_not_allowed with its reason (several_makers, no_maker, file_type).

Concrete actor rows against A's private creation/prompt/chat/receipt/job:
A own: creation read/delete/reuse/link allowed; share allowed if sole maker/supported; own prompt Self; own chat requires same surface App; receipt full at plan detail; job read/cancel; events Own.
B/O/D: content all denied; list excludes A. O alone may obtain operational projection through owner endpoints. D retains unattributed engine/capacity status only.
S/N: matching A App principal (explicit same-App fixture) permits creation Own and receipt/job Own, but not A prompt/chat. A different App principal denies all. S/no-maker share fails no_maker.
Q: exact shared creation/poster only, read+g=s link; prompt only if included; no reuse/delete/share/md=full/receipt/job/events/workflow/chat/voice.
F: same as B once revoked, unless independently Own.
Rk/Rs: reject authentication before any content/operational path, independent of role or previous share. An independently valid bearer g=o URL is still a bearer grant until expiry; revoking a key alone does not revoke that URL under v2.
[v3] C against S work (no maker person): Own, so creation read/delete/reuse/g=o link, receipt/job read, job cancel and events; no share (no maker), no prompt or chat. C authenticating with C's own personal key is not Own for S. Against another person's personal App, C has nothing.
H: valid signed URL delivers its exact granted asset only; no discovery, JSON detail, minting, reuse, prompt or arbitrary ID access.
U: auth failure for authenticated endpoints, 403 Invalid or expired signature for invalid/missing signed grants.

## 3. Uniform assertion helpers and denial invariants

assert_not_found(response): exact semantic status=404/code=not_found; compare normalized body and exposed headers to a nonexistent ID response; no resource ID/title/maker/prompt/path/size or signed grant. Do not assert timing equivalence with a flaky wall-clock threshold; separately ensure storage/read/provider spies were not called before authorization.
assert_auth_rejected(response): established auth failure wire shape, no downgrade to unscoped/no-person user, dispatch/read/mint/cancel/store/provider call counts all zero.
assert_full_own(response, expected): exact documented own fields at the plan's receipt detail; expected owner IDs present; no unrelated record.
assert_shared(response, expected, include_prompt): exact allowed field-path set from §4.2, expected title Picture|Video|Audio from <maker>, exact grant and maker, no workflow/job/request/seed/provenance/internal poster_asset_id. Prompt absent when excluded; when included equals authoritative maker's live prompt only.
assert_owner_projection(response): exact recursive allowlist/schema for whole response, including envelopes, aggregates, filter/source labels. No dictionary pass-through or unknown additionalProperties.
[v3] assert_no_effects(before, after, spies): receipts/jobs/assets/share rows, owner and maker lists, prompt rows, files, credential bindings, audit sensitive payloads unchanged by denied action; no provider or denied-target byte-read calls. Compare logical state, not SQLite file bytes. A documented valid-auth last-used timestamp may update; isolate that field. Revoked/expired auth must not reach the inspected last-used update. Expected failed-auth generic security audit is permitted but must be separately schema-checked.
assert_list_exact(response, expected_ids): sorted/set equality plus total/count/cursor facets derived only from allowed set; test first/last/multiple pages and empty result. No invisible marker influences query matches, sort position, suggestions, group labels or counts.
assert_equivalent_denial(known_forbidden, unknown): same public code/body/header class without relying on identical request correlation IDs.
assert_grant(response, asset_id, md, grant): parse without logging secret URL; assert authorized exact resource and access basis; signatures verified by test server, not recomputed by copying production sign implementation.
Capture evidence safely: test failures should identify sentinel name and JSON path, never print an actual production credential. Synthetic secrets only.

## 4. Path x outcome/assertion matrix

Apply all actor/resource rows in §2 to each path row; the stated kind/action and serialization determine the exact expected cell. Existence not found and denied resource both use not_found; owner operational results never turn correlation job/request IDs into usable content capabilities.

P01 Library list GET /me/api/media:
[v3] authenticated 200, exact Own set for default/view=mine; exact active-share set for view=shared-with-me. Other/unshared/deleted/unresolved excluded. Q results serialized Shared, never raw own serializer. Repeat job-backed and cache-only resources. Verify q/facets/sort=size/recent/pagination against expected allowed set. O's default list identical to ordinary member logic, not household.
CURRENT VERIFIED route in me_media; target views/sharing unimplemented here.

P02 Library detail GET /me/api/media/{asset_id}:
[v3] creation read oracle; 200 Own or Shared serializer, otherwise 404. Repeated forged scope/owner/person handled P16 before lookup. Metadata outside allowed audience absent even when bytes are shareable.
CURRENT VERIFIED.

P03 Download GET /me/api/media/{asset_id}/download:
[v3] creation link oracle; 200 signed-link response only after Access. Own g=o, Shared g=s.id/md=none. Invalid caller 404 and signer never called for the denied target. If lookup builds an authorized collection, unrelated allowed items may invoke the signer; inspect target IDs rather than asserting zero total calls. Owner role never mints member link. md=full on g=o is bound to the authenticated fetcher's own prompt at delivery (OV §3.4); a two-maker file gives each fetcher their own prompt, an unauthenticated holder gets 403.
CURRENT VERIFIED route; actual signer/delivery implementation UNVERIFIED.

P04 Thumbnail/poster:
same creation decision as parent; exact poster binding, no sibling/other creation access, no raw internal asset path. Test direct guessed poster ID and parent-provided URL. Read/download parent and poster after revoke/delete; partial deletion preserves another genuine owner's bytes. Thumbnail route and thumbnail generation pipeline UNVERIFIED; poster metadata references VERIFIED in me_media.

P05 Signed bytes GET /v1/assets/{id}/content and image aliases:
valid H grant: 200 full bytes for no Range; valid satisfiable Range: 206 with exact permitted slice/Content-Range and correct length; no Range may silently skip authorization. Missing/bad/expired signature, missing md or g, old URL, changed md/g/asset/expiry, nonactive share, cross-asset grant, g=s with md=full all exactly 403 "Invalid or expired signature". Stop then re-share gives new share_id; old URL remains 403. All signature/grant checks before read/decode/strip/range/conditional delivery. Test Range bytes=0-0, prefix, suffix, invalid/unsatisfiable/multiple range according to supported delivery contract; invalid grant must return 403 even when range itself would otherwise fail. Valid range error semantics UNVERIFIED; do not invent a range parser contract.
g=s response Cache-Control private, no-store; g=o private,max-age=3600 (normalization for whitespace allowed) and expiry <=1 hour. URL is bearer authority, not viewer/session-bound. g=o survives share/key revocation until expiry by design, but absent bytes still not found. A stolen valid URL's scope is limited to its signed resource, not a claim of person authentication.
GET/HEAD/conditional route inventory: no current HEAD/ETag/304 implementation confirmed in allowed code. UNVERIFIED. If registered, all must perform grant checks before bodyless 200/206/304 and leak no content-bearing metadata after revoke. No new handler gets a waiver.
All signed delivery handlers UNVERIFIED here; source exists only as design references.

P06 Prompt/workflow/md=full:
Self for own saved prompt/workflow; exact share-maker prompt read only when included; never include workflow download or md=full for Shared. Same App/different person -> 404 prompt; same person/different App -> prompt allowed but chat denied. Include true->false immediately removes prompt from detail/list/q results and any shared title; erasing saved prompt shows "Prompt not saved", no stale cached value. Full-metadata endpoint handlers UNVERIFIED; viewer-specific prompt selection VERIFIED in me_media.

P07 (step 3; step-1 classification) Receipt detail GET /me/api/receipts/{id} and key/MCP receipt lookup:
receipt read oracle; full own view constrained by plan detail, no owner role bypass; share does not grant receipt access; unrelated 404. Owner counterpart GET /admin/api/activity/requests/{id}: 200 projection only when O, including for O's own receipt on this operational endpoint; D/B must be denied by role and disclose no item. Owner role-denial wire status is UNVERIFIED. receipt store methods VERIFIED; HTTP adapters outside allow-list UNVERIFIED.

[v4] Ledger adapters: maker GET /gateway/v1/requests/{id}/retention/ledger and /me UI adapter require content:receipt:read Own before receipt/fact lookup. Maker with allowed receipt detail sees full ledger. The old /admin path becomes owner-only operational projection; O gets no observed.requested_model, private reason/deadline or raw facts envelope. D gets 404 there, including for a real id; D acting as maker on the maker route still works. Foreign known id and unknown id have equal 404 code/body/header class on each forbidden branch. Test the real REST provider and bound MCP requests callback, not just mocked admin auth. Rewrite tests/test_retention_ledger_headless.py expectations (see §10).
Without request_id, REST/MCP ledger is classified none: current config facts only, no receipt fetch and no observed/request-specific field. The existing management/discovery gate remains; two worlds differing only in receipts and content produce identical output. A permissive retention_visible mock cannot bypass per-request authority.

P08 Usage GET /me/api/usage/summary:
200 own-only quantities and source lists; owner cannot request member breakdown here. No shared resources create usage authority; S/N key endpoint applicability UNVERIFIED, never silently treat as owner/all.
GET /admin/api/usage: O gets projected household/person quantities; no asset IDs, prompt-derived labels, arbitrary requested_model, names not resolved from trusted catalog/accounts. Admin-not-owner denied attributed usage. A share recipient sees no sharer's spend. Submission-time spending person remains fixed after service ownership transfer.
Current /me usage builder VERIFIED; new /admin usage handler UNVERIFIED.

P09 Activity/home/status/catalog/workflows/recent:
GET /me/api/activity, /home, /status, /models/catalog, /workflows: own-account audience, no instance scope/other receipt/job/resource snippets, including O. Public model catalog data may remain common, but recent/example/private-resource fields must respect content authorization. Shared Recent entry only permitted sanitized sharing notification, not sharer's receipt/job. GET /admin/api/activity/requests (list/detail): O whole-response projection, non-owner no attributed record access. /v1/status,/status,/status/raw: remove recent images/videos/renders; O projected live jobs only; D engine/capacity without people/key attribution. Every alias queried separately, not just one handler unit test. Actual handlers UNVERIFIED; usage-source builder evidence in allowed files does not prove route protection.

P10 Jobs GET /me/api/jobs, /me/api/jobs/{id}, key /gateway/v1 jobs and /v1/videos jobs; MCP job poll/wait:
[v3] job read oracle, full own job through content route; no owner/admin audience bypass. List exact IDs. Shared creation gives no job access. Deny before jobs.get/receipt lookup that may reveal error/assets; for wait, deny before any wait/poll/stream observation, not just final output. Target owner status projects job separately.
me_media adapters, MCP adapters and media_jobs storage VERIFIED; other HTTP routers UNVERIFIED.

P11 Job content/download:
first require job read AND creation link/read for each output before bytes/URL; do not use an owned job as a confused deputy for a foreign asset seeded into its output. MCP image/audio inline output and video URLs separately tested. Foreign/deleted/unresolved output must be rejected or omitted under a specified response contract without copying its bytes/URL; exact partial-success wire behavior UNVERIFIED and a blocker for that branch. Parent job authorization alone is insufficient.

P12 Job events:
Own-only target; Q/F/O/D without Own get 404 before events() called; include SSE/poll/cursor/reconnect/replay variants when registered. Closed set of permitted event types; all payloads typed/audience-safe. Assert contiguous per-job seq and absence of household-wide row IDs. Forbidden event request must not repair event sequencing or mutate storage. [v4] Per-request linearization: an already-open stream continues under its original rows until disconnect; there is no per-event revalidation promise. Revoke while streaming, confirm that limit, then assert the next reconnect and MCP poll deny. Do not require that revocation recalls already emitted/in-flight frames. Actual transport registrations UNVERIFIED.

P13 Cancel/delete/reuse/share:
[v3] denied job cancel leaves status/phase/cancel_requested/result/events unchanged and cancel callback uncalled. Allowed Own job cancel follows existing accepted/cancelled contract; exact HTTP success status UNVERIFIED. MCP job cancel returns {status: cancel_requested, job_id} when the cancel call succeeds; a later poll or content failure does not turn it into an error (OV §3.2).
Allowed own Library DELETE verifies response success and exact detach of caller's owner/maker/prompt; physical bytes removed only after no owner remains; revoke that maker's shares. Shared-only delete returns 404 and does not mutate; recipient removing share is P14, not content delete.
[v3] Run/reuse foreign/shared-only asset ->404 before read_asset, store_asset, job admission or paid calls. Own reuse allowed, including a session reusing a service App asset it is accountable for; no person prompt access.
All six actions across all eight kinds must be tested directly at core as well as path adapter, including methods not exposed (unsupported HTTP method does not prove core Deny).

P14 Sharing new target APIs:
PUT /me/api/media/{asset}/shares/{person}: sole Maker with valid target yields 201 first create, 200 idempotent/update, exactly one active pair; include_prompt default false; title/source not copied from prompt. Other people/owner/admin cannot grant on A's behalf. Source prompt/maker row authoritative even in corrupt competing-row fixture.
DELETE /me/api/shares/{id}: maker or exact recipient revokes active row, inaccessible other caller gets 404/no mutation. Repeated delete semantics UNVERIFIED (choose idempotent target contract explicitly). GET /me/api/shares?direction=by-me|with-me returns exact authorized rows. GET share-targets includes enabled people's id/display-name/owner flag only; no private fields.
Sharing list/create/remove unsupported on MCP per design; explicit unsupported error, never empty-success disguised as capability. Entire share API implementation UNVERIFIED.

P15 MCP (step 3; full McpDeps inventory step 1):
capabilities, run each supported target kind, job poll/wait/content/cancel, direct read_asset/store_asset and each returned signed-link minting path. Use detailed appendix for actual operation names. Keep authenticated context unchanged through dispatch; rejected or missing identity cannot default to privileged context.
Capabilities/discovery may enumerate public capability descriptions but cannot contain a private receipt, resource count, prompt example, filename, dynamic asset or signed URL. Parameterize O,D,S,N,Q,F,Rk,Rs and seed foreign data in dependencies; public fixture yields exact stable result regardless of private resources.
Run submits own new work only after authorization of EVERY nested asset reference; combine owned+foreign input, list duplicates, poster refs, alternate MIME/asset ID/URL representations supported by schema. Shared link cannot become input permission. Reader receives trusted Access for reuse, not arbitrary caller-supplied basis.
Direct returned asset/link: only authorized current output, deny a backend stub returning foreign asset ID/URL; store/mint callbacks require explicit authenticated principal/Access. Check async wait/poll returns and image/audio/video result branches.
MCP adapters VERIFIED; proxy callback implementations UNVERIFIED. Do not assert full protection based only on route mocks.

[v4] New McpDeps fields: retention_ledger follows P07, stt_measurement is operational, rename_app/rename_key are own-account and grant-scoped. visible, owner_visible, retention_visible, authorization_servers and scopes are none (gates/config), as are public_base_url, allowed_origins, server_version and poll_interval. Classify callback and tool operation separately: a discovery gate never substitutes for content authorization. Test every field in gateway/mcp_server.py:109-141 and actual proxy binding; two-world discovery must hold across grants/roles without exposing request data.

P16 Removed parameters and identity injection:
every /me registered route with scope,owner,person keys rejects presence with 400 param_not_supported. Cases empty, repeated same/mixed values, scope=a&scope=b, percent-encoded key, extra allowed query alongside it, all/everyone/local-network/another user. No broad query performed; responses have no private markers. In particular current person=local-network detail/download must not work. FastAPI simply ignoring an obsolete argument is a failure. Unknown nonremoved parameters follow existing router policy, UNVERIFIED.
No owner/superadmin legacy role can bypass this. Migration must eliminate superadmin and no alias remain. Unauthenticated ordering is separate auth contract.

P17 Exports:
No content export/backup/CLI implementation was confirmed; the design says none exists. Mark REST content export, bulk archive, CLI and MCP export as UNVERIFIED/unsupported, not tested-safe. Inventory any existing account/config/people/App/key exports as own-account/operational: no creation blobs, prompt/chat/error body, filenames, URLs, nested content fields or extra owner keys. Owner metadata export never yields content grant. For future content export apply per-item kind/action, exact authorized item list, safe archive filenames/manifests, no foreign embedded bytes, prompts only Self, Shared export no workflow/receipts and only explicit share fields/bytes. Export code and registry require additional scope in implementation phase; no guessed live endpoints probed.

P18 Storage (step 4; history rows with RR slice 5):
[v3] GET /me/api/storage own logical counts/bytes; GET /admin/api/storage O projection only; no asset IDs/names/thumbnails/item dates/links. Two-owner bytes count fully to each person and once physically; sharing adds zero; service-App bytes are listed per service App with its accountable person, not added to the person row; unresolved remains aggregate Not linked to a person. Deletion releases a claim (OV §6.3): two makers delete in turn -> bytes and sidecar removed after the second; first deleter's App key can no longer read or reuse it; concurrent identical generation under the cache lock keeps or rewrites the file. Reclaim estimate = files with no owner left after release; rescan after delete. Timestamp age >15 min stale; separate target devices merged only when device identity equal.

[v4] M4 kept-history accounting: real retained byte lengths count per maker; O sees bytes only (no history count/id/title/model/date/link), A's Review storage lists and deletes A's history under Self and retention narrowing. Deleting a Library item transactionally cascades A's linked prompt/history only, preserving B's rows on a shared physical file. Crash between the content-row cascade and sidecar release leaves a durable deletion intent; replay before new claims, with erasure fencing against concurrent writes.
[v4] M8 claim release: no claim timestamp exists, so do not test timestamp precedence. Release the intersecting caller principals and maker entry, retain audit as history, and stop using tombstones as permanent list/detail filters. After A deletes, A cannot read through an old App alias; B still can. A later identical generation creates a new visible claim. Pre-v3 cleanup runs once from manifest/version and removes only actor personal-App aliases plus user:<id>, never accountable service-App claims. Seed both kinds in a production session Viewer to catch accidental full-owner_keys release. Repeat cleanup/restart without affecting later claims. The preview exposes every affected pre-cut-over claim because an old deletion cannot be ordered against an untimestamped claim. Current service-App claim removal confirms "This also removes it from {App}." for each affected App.

P19 Key issuance and service binding:
POST /admin/api/apps/{id}/keys and /me/api/apps/{id}/keys: only App accountable person, for personal and service Apps; unrelated A/B/O/D -> exactly 403 not_your_app, no credential row/token/audit of issued secret. Allowed issuance gets the established success shape and exactly the intended App principal; never grants another App. /me/api/try-key, invite acceptance and both /setup/first-app branches must obey own-App binding; exact router bodies and setup's nonperson branch UNVERIFIED. Control-plane /admin/api/admin-keys and generate-admin-key retain operational roles but service_account principal grants no member content. Old bad-provenance keys must fail auth after switch even if caller is owner. App names/profile collisions confer nothing. Trigger and migration tests are prerequisite, not a substitute for each issuance path.
[v4] POST /admin/api/apps/{id}/trusted-addresses and the own-App /me adapter: same accountable-only add rule as keys; O cannot add on A's App, even on a personal App. SQL INSERT refuses foreign/NULL/empty created_by. No denied add mutates bindings or records a secret. Removal may narrow access; re-add requires the accountable person. Trusted-address identity T owns only its matched App; after removal its next request/reconnect cannot authenticate through the old address.

P20 Job event writes and idempotency:
[v3] Event append is NOT one of the six content_access actions in v2. Keep Own-only existing write authority as a proposed extension requiring explicit inventory classification; Own read/cancel of a job never implicitly grants event append. Client note/progress source and bounded payload validation must be enforced even for owner/admin; lifecycle event types or phase/status claims rejected before mutation. For same request_id, a different owner_scope may not replay or replace another job. Same canonical principal with rotated live key may replay only exact input digest; missing legacy scope/digest cannot prove replay. No denied request allocates seq/event row, mutates progress, consumes reservation or dispatches work. Actual event HTTP routes are UNVERIFIED; storage behavior is confirmed in media_jobs.

P21 [v4] Request history, Debug and incognito (core step 1; step 3 with RR slice 5):
Run the eight-kind oracle through actual production Viewer builders and retention.reader_allowed: the latter must call content_access.decide, then narrow, not implement another reader. Test list/search/direct read/export/Run again/receipt link adapters for Self and delegated privacy_permission. Owner/admin/accountable-App/Shared never adds authority. Same person with permission on personal key or MCP grant qualifies only while policy permits; maker_signed_in narrows to browser. Attempted reader widening is rejected.
Default person-attributed history is on under D50. One incognito switch spans Try, chat, whole chat, App/key defaults and per-request extensions.com.azizam.prompts mode=forget. Assert zero prompt/history/conversation/debug content writes, one structural receipt and normal response delivery. Service Apps and trusted-address requests without person produce no_person and only structural receipts, never content assigned to the accountable person. Existing creation ownership stays OV §2. A request cannot override a stricter App/key or household never_keep.
Only accountable person edits per-App/per-key retention (O4b); O may narrow household never_keep but cannot edit A's App/key content-retention choice or widen readers. Verify allowed edits, denied writes/no effects and concurrent narrowing at the write fence.
Gate incognito and history wording on step 3 AND RR slice 5. Home notice, Privacy, honest limits and owner Usage render their history variants only then; saved history is named among host-readable files. Try uses the existing forget/sessionStorage state, with no second switch. Keep the privacy notice once-only and test 390 px.

## 5. Signed metadata and grant abuse suite

Use synthetic known fixtures with marker embedded in PNG/JPEG/WebP/WAV/FLAC/MP3/MP4/WebM/MKV/OGG/M4A metadata and distinct visible-pixel/audio markers. For Shared md=none, decode container metadata and scan raw returned bytes for metadata sentinel; do not demand removal of marker intentionally present in the image/audio content. Include poster metadata separately. GIF share is 409 file_type; no stripper/error fallback may return unstripped bytes. For corrupt/unsupported container, fail closed without exposing original data and log only safe error schema; exact error status needs implementation contract.
Pair job-backed/cache-only, no-saved-prompt/saved prompt, include true/false, Range after revocation, recipient remove, maker delete, restore revocation and new-share IDs. Before read spies on open/read_bytes/stream preparation, not merely final response. Grant tampering matrix includes asset, parent/poster mismatch, expiry boundary frozen clock, md missing/empty/full, g missing/empty/unknown/s.other and old signature versions.
A cache or CDN assertion in unit tests does not prove deployment cache behavior; deployment headers/logging are separate gates.

## 6. Planted-marker noninterference and recursive schema gate

Canary fixture produces unique synthetic values per leak channel, e.g. CANARY_<run>_prompt, _workflow, _chat, _filename, _path, _error, _metadata, _cost, _decision, _source, _requested_model. Use distinctly seeded private asset IDs, poster IDs, file basenames, engine paths and fake signed links. Trusted account/App/catalog display names use safe fixtures so a test does not conflate intentionally disclosed names with private fields.

Plant markers in:
request_history/debug_capture bodies, ledger observed.requested_model, retention reason/deadline, client_address and api_key_name; prompt and workflow rows; saved chat messages/tool output; job error/assets/result metadata/event nested fields; creation filename/sidecar metadata/seed/input_hash/provenance; exception str/repr/args/chain and provider error echo; allowed-key receipt scalar values such as requested_model/client_hint/source_label; nested cost and resource_decision unknown strings/maps/lists; request source/envelope, grouping/facets/filter options, sort labels, cursor or pagination metadata; direct MCP dependency responses and content callback results.
Do not plant only forbidden receipt keys: _assert_safe might reject those before the owner projection is exercised. Direct projection fixtures may contain future extra fields; separate store tests assert structural rejection for prompt/input/messages/body/bytes keys.

For every inventoried operational and own-account route as O and D, and every corresponding MIME/HEAD/error/stream variant:
1. Capture complete status/headers/cookies/body, redirects including Location, JSON/text/SSE frames, MCP content and structuredContent and error.data; include base64 only where encoding is declared.
2. Assert no private canary or forbidden private asset/poster ID, signed query marker sig=, /v1/assets/ or equivalent private download URL, filesystem path. Known own/shared content tests are separate: it is incorrect to prohibit intentionally included shared bytes/prompt in O acting as ordinary authorized recipient.
3. Recursively validate exact schema at every JSON leaf. Opaque id fields must match expected fixture IDs and grammar, not any arbitrary string; enum values from closed sets, timestamps valid, numerics finite/ranged, booleans actual booleans, known names resolved by ID. An asset ID cannot be smuggled into request_id by satisfying generic 'string'.
4. Compare response for two fixtures differing only in forbidden content. Normalize authorized timestamps/correlation IDs if needed; responses, visible counts/facets/group keys must be identical. This catches content-dependent aggregate labels even when the sentinel is transformed.
5. [v4] Retention is exactly [{kind: closed enum, decision: kept|not_kept|failed}], never reason/deadline/parts/limited_by. Keep valid operational facts: submission person, App, resolved model/workflow, status/layer/terminal reason, cost quality/source/reconciliation, attempt and coverage fields. A requested_model-only receipt must show Unknown model, not canary. Unknown layer presents safe next action, not provider error.
6. Add new arbitrary receipt/job field and nested new cost/decision field: projection omits it until schema intentionally expanded. No source envelope can add it back.

Capture caplog records (message, args, exception info/rendered trace), capsys stdout/stderr, cache/key_activity.jsonl every JSONL line, task/background worker logs and fake provider adapter failures. Scan from before request through awaited test-background completion so late error logs are included. Assert line-by-line no canaries/assets/signed links and safe logs include exception class plus allowed correlation ID, never arbitrary exception text. Test generic Exception AND MCP CallError (message/details branch), job failures, malformed input, cancellation, timeout, storage_full. No full traceback containing private locals/error str. Pure exception-count ratchet is supplementary, not proof.
Full proxy/provider/runner/LiteLLM/Traefik sinks are outside scope and UNVERIFIED. Require deployed LiteLLM message/spend prompt logging disabled and reverse-proxy signed query suppression verified on deployed version before notice. Do not scan or expose production secrets in this design.

## 7. Identity, migration, trigger and storage budget regressions

[v4] B1/B2 binding prerequisite (step 2; identity fixtures step 1):
Start from schema v6 with a service-account-owned App, personal Apps, aliases and spend rows. Run the actual AP migration: service App remains service, now accountable to the gateway owner, explicit plan assignment retained. Then run OV Unit 1. Current-main fixture starts with NOT NULL/FK owner_user_id and no owner_service_account_id. Fresh fixture also installs enforcement only after all migrations. No new person-approval row or Q5 exists; a personal App created by someone else still has a keep-personal/correct-to-service row.
Seed live/expired/revoked keys and trusted addresses with accountable, foreign, NULL and empty created_by. Preview and apply exactly revoke bad keys and delete bad addresses, without a final back-fill or inference from receipts. Preserve matching bindings and historical alias reads. A recently used App losing every binding blocks apply, including an App used only by address; unknown address-use provenance blocks rather than counting unused. A replacement from the accountable person or explicit stop approval clears it. Verify repeat-run idempotence and version checks.
Create "No person recorded" only for proven-unbound records with kind=service, gateway owner and access_plan="person"; no key/address is silently added. Unresolved/service_account principals stay ungranted. Historical spend is unchanged. Do not mock away the actual migration.

M1 session provenance:
[v3] C session owns personal App P and is accountable for service S. v3 policy (OV §2.4 O6): the session Viewer includes both P and S principals. Assert decide(C_session,S_asset,read|delete|reuse|link)=Own, share=Deny (no maker); decide(C_session,S_prompt or chat)=Deny; decide(C_personal_key,S_asset,*)=Deny; decide(S_key,S_asset,reuse)=Own. Repeat with App name collisions. (Transfer cases are deferred until a transfer route exists; v1 has none, OV §2.4 O3.)

[v4] M2/M6 NULL-safe binding and upgrade order (step 2):
Against real installed triggers, test same/different/NULL/empty/foreign owners and kinds. NOT NULL/FK enforce O1; O2 refuses kind and personal-owner changes after cut-over. O3 refuses service-owner change with any unrevoked App key (including expired) OR any trusted address. With zero bindings, SQL satisfies the transfer precondition; no v1 public transfer route exists. App deletion aborts.
O4 refuses credential/address INSERT unless created_by IS accountable person. App-key subject/App/creator updates abort only for OLD.subject_kind='app'; control-plane adoption through signin_admin.adopt_admin_keys_in must succeed. Reject promotion of a control-plane row to App authority without the same provenance/session checks. Address App/address/creator updates abort; replacement must be delete plus new authenticated add. App-key revocation is final: revoked_at cannot be cleared or changed, and revoked aliases never authenticate. Test atomic insert versus transfer, same-value no-ops, rollback with original rows intact and trigger replacement with SQL comparison.
Run schema v6→AP→current→OV with actual production migration APIs: AP's owner updates must succeed before O2/O3/O4 installation. Remove both startup back-fill call and implementation; restarting cannot repair or rewrite unproven creators. Fresh initialization follows the same final installation order. Savepoint self-tests exercise forbidden transitions and permitted control-plane adoption before verification.

Switch/restore:
[v3] Preview/apply/verify every crash boundary of both units (ownership cut-over, then visibility switch) under the AP 4.2 protocol; unaccepted marker refuses start; missing ownership_version or visibility_version refuses start for a release that needs it; auth-disabled config after switch refuses start; unknown/partial state never sets notice-ready. Superadmin mapping leaves no alias; removed params reject. Ownership cut-over: AP already assigns service Apps; only approved personal->service corrections precede triggers; an App used in the last 30 days that would lose all bindings blocks apply; the verify self-test aborts every forbidden write inside a rolled-back savepoint. Restore: the two-store revision guard is removed (OV §8.3); test that a share write is one transaction, and that the ops-note SQL stops every active share so old g=s URLs return 403.

[v3] M3 absolute reserve (OV §6.4, the selected contract):
[v3] Per filesystem d: admit a storage-writing job only if free_bytes(d) - leased(d) >= reserve_bytes, checked before any upload body is read and before engine/provider calls. reserve_bytes defaults to W_max + 2 GiB; setting it below W_max returns 400 reserve_below_largest_job. Each admitted job leases its peak_bytes until it ends; a resumed job re-leases before running. Paused when the rule fails; resume at reserve_bytes + resume_margin_bytes; Low below reserve_bytes + warn_margin_bytes. Every storage-writing kind declares peak_bytes, including retained history; an undeclared kind fails the inventory test.
[v3] Tests use deterministic disk and admission spies: free - leased at reserve-1/reserve/reserve+1 and at the resume margin; giant filesystem with high percent used but enough absolute bytes stays Healthy (the earlier 1.45 TB / 79.5 GB case and the v4 101.6 GB worktree measurement); small filesystem with low percent used but too few bytes pauses; percent never changes a state; unknown disk pauses; separate data/cache/spool/private-temp devices each checked; aliases of one device counted once; N concurrent barrier starts are admitted only while leases fit; leases released on success, failure and cancel; restart-resume re-lease; worst-case video+upload+voice; write ENOSPC gives storage_full; no auto-delete.
[v3] Refusal exactly 503 storage_paused with layer gateway before upload persistence/job enqueue/engine/provider; upload with no declared length is refused before reading the body. Existing downloads/signin/browse/delete continue, and chat that stores no file is not paused; running job failure storage_full, no success; logs no path/private content. Byte hysteresis and recovery after cleanup.

[v4] M5 shared device ledger: STT acquire/top-up/replay/release and ordinary image/video/upload/history writers use one process-wide per-device counter under threading.Lock. Run barrier-started thread and asyncio callers together; prove they cannot each admit against the same unleased bytes. STT obeys reserve_bytes/state and may apply a stricter configured minimum; its 64 MiB default cannot bypass the household reserve. Count a workspace once, merge data/private-temp only on equal device id, and include Docker image scratch plus an absolute STT spool on another device. Atomic multi-device admission takes all leases or none; finally releases on every error/cancel/top-up failure and restart-resume re-leases.
[v4] M4 Paused/Unknown history: send a request whose only optional write is request_history. Return the normal successful/streaming response and one receipt with not_kept/storage_unavailable; no kept row, content buffer persisted, delayed retry or provider refusal. Required creation/upload/STT spool work still refuses before body/provider while paused. Force an ENOSPC after the optional writer check: response still succeeds and receipt truthfully reports storage_unavailable. Verify the member's not-saved explanation without exposing its reason in O's projection.
[v4] N3 Unit-2 attachments: backup all affected sidecars with the database; manifest contains accepted pre/post ownership sets; attachment is atomic replacement and idempotent. Crash before and after each sidecar write, replay, unexpected edits, final DB version write and restore. Version/acceptance cannot precede full file verification. Only proven-unbound assets attach; all unresolved files remain ungranted. No test may pretend an admin.db rollback undoes a sidecar write.

## 8. Proposed pytest layout, fixture interfaces and coverage gate

All modules below are NEW DESIGN names, not claims that files exist. Keep narrow unit tests plus real app integration tests. No module was created or executed.

tests/privacy/conftest.py
  seeded_world(tmp_path, frozen_clock) -> PrivacyWorld
  actor_factory(kind, *, person_id=None, app_id=None, roles=(), created_by=None, revoked=False) -> Actor
  resource_factory(kind, *, owner_principals=(), person_id=None, app_id=None, maker_ids=(), state='active', markers=None) -> Resource
  share_factory(asset, recipient, *, maker, include_prompt=False, active=True, poster=None) -> Share
  viewer_for(actor, *, surface_app=None) -> Viewer  # production builder, not handcrafted in integration tests
  app_client(actor) -> authenticated test client
  mcp_harness(actor, *, deps_spies, in_process=True) -> MCP client
  synthetic_media(container, *, metadata_marker, visible_marker=None) -> bytes
  projection_schema_registry() -> exact JSON schemas
  disk_budget_fixture(devices, concurrency_bounds, job_bounds) -> FakeDisk
  side_effect_spies() -> Reads/Mints/Provider/Store/Cancel/Event/DB recorder
  leak_markers(world) -> MarkerSet
  sink_capture(caplog, capsys, tmp_path) -> CapturedSinks

tests/privacy/oracle.py
[v3]   expected_basis(actor, resource, action, state) -> Own|Shared|Deny
  expected_response(path: PathSpec, actor, resource, state) -> Expected
  expand_cases(paths, actors, resource_states, kinds, actions) -> Iterable[Case]
Keep oracle independently expressed from the reviewed action table; do not call production decide to compute expected results. Code-level tests compare production to this independent oracle.

tests/privacy/assertions.py
  assert_not_found(response, *, nonexistent_response=None) -> None
  assert_auth_rejected(response, *, spies) -> None
  assert_no_effects(before: Snapshot, after: Snapshot, *, spies) -> None
  assert_list_exact(response, *, expected_ids, expected_count) -> None
  assert_shared(response, *, share, expected_prompt=ABSENT) -> None
  assert_owner_projection(value, *, schema, world) -> None
  assert_no_private_markers(payloads, *, markers, forbidden_ids=()) -> None
  assert_safe_logs(capture, *, markers, allowed_correlation_ids) -> None
  assert_signed_delivery(response, *, expected_bytes, range_spec=None, grant_kind) -> None
  assert_case(case, response, *, world, before, after, spies) -> None

test_content_access_matrix.py:
[v4]   test_history_debug_self_privacy_permission; test_retention_reader_calls_core_then_narrows; test_all_eight_kinds_six_actions.
  test_every_kind_action_basis_cell; test_actor_state_cross_product; test_roles_never_add_content_basis; test_null_self_never_matches; test_same_app_different_person; test_same_person_different_app; test_share_not_transitive; test_owned_plus_shared_revocation_keeps_own; test_deleted_and_unresolved_deny.
test_identity_provenance.py:
[v3]   test_owner_created_personal_key_self; test_personless_key_no_prompt_chat; test_revoked_auth_live_alias; test_service_migration_prerequisite; test_session_owns_accountable_service_app; test_personal_key_not_own_for_other_apps; test_trusted_viewer_ignores_body_identity.
test_library_paths.py:
  test_list_actor_state_matrix; test_detail_actor_state_matrix; test_download_requires_access; test_cache_only_vs_job_backed; test_poster_bound_parent; test_query_filters_noninterference; test_local_network_scope_not_detail_escape; test_removed_parameter_presence_all_me_routes.
test_sharing.py:
  test_sole_maker_share; test_no_maker_multi_maker_gif; test_idempotent_pair; test_live_prompt_update_erasure; test_shared_serializer_exact; test_recipient_remove_vs_asset_delete; test_delete_revokes_only_makers_shares; test_share_lists_and_targets.
test_signed_grants.py:
  test_every_mint_site_requires_access; test_grant_tamper_matrix; test_next_fetch_revoke_range; test_reshare_old_url_dead; test_poster_cross_asset_binding; test_metadata_containers; test_cache_headers; test_no_legacy_grants; test_byte_read_after_auth; test_bearer_o_lifetime.
test_receipts_usage_activity.py:
[v4]   test_retention_kind_decision_only; test_request_ledger_real_provider_owner_admin_maker_matrix; test_current_ledger_never_reads_receipts.
  test_own_receipt_access_plan; test_operational_projection_actor_matrix; test_recursive_schema_and_future_fields; test_submission_person_immutable; test_unknown_model_no_requested_fallback; test_envelope_group_filter_leaks; test_storage_aggregate_only.
test_jobs_events.py:
  test_job_status_actor_matrix; test_cancel_no_effect_on_deny; test_content_each_asset_authorized; test_events_actor_matrix; test_event_type_closed_set; test_per_job_seq_no_household_ids; test_denied_events_no_repair_write; test_wait_denied_before_poll.
test_mcp_boundaries.py:
[v4]   test_mcp_grant_viewer_and_privacy_permission; test_all_mcpdeps_fields_classified; test_ledger_maker_projection_none_and_no_existence_oracle; test_rename_own_account; test_stt_operational_gates_none.
  test_tools_and_subops_registry; test_dispatch_context_preserved; test_capabilities_no_private_data; test_run_nested_asset_reuse_matrix; test_direct_reader_personless_shared_foreign; test_output_store_and_link_access; test_job_poll_wait_content_cancel; test_error_callerror_and_exception_safe.
test_owner_leak_canaries.py:
  test_every_owner_and_admin_surface_canary; test_two_world_noninterference; test_stdout_stderr_jsonl_and_background; test_error_path_markers; test_new_field_not_implicitly_projected.
test_migration_bindings_restore.py:
[v4]   test_v6_upgrade_installs_triggers_last; test_trusted_address_provenance_and_zero_bindings; test_revocation_final_admin_adoption_allowed; test_pre_v3_release_personal_principals_only_once; test_unit2_sidecar_manifest_replay_restore; test_claim_delete_intent_crash_fence.
[v3]   test_switch_verified_before_notice; test_ownership_cutover_preview_apply_verify; test_bad_provenance_exact_revocation; test_null_safe_trigger_transition_matrix; test_issuance_trigger_atomic; test_fresh_schema_has_triggers; test_share_write_single_transaction; test_ops_restore_sql_stops_shares; test_auth_disabled_start_denied; test_reset_event_issuer.
test_storage_admission_budget.py:
[v4]   test_stt_and_job_shared_ledger_thread_async_race; test_stt_reserve_and_stricter_floor; test_private_temp_device_and_docker_split; test_optional_history_paused_keeps_response; test_history_bytes_owner_projection_member_delete.
  test_absolute_budget_boundaries; test_small_large_filesystem_counterexamples; test_concurrent_working_set_bound; test_per_device_accounting; test_unknown_pauses; test_no_provider_before_admission; test_running_enospc_not_success; test_cleanup_and_download_still_work.
test_request_history_incognito.py:
[v4]   test_d50_default_person_history; test_no_person_structural_only; test_single_incognito_all_surfaces; test_o4b_accountable_settings_household_never_keep_only; test_rr_slice5_copy_gate; test_no_reader_widening.
test_path_inventory_exports.py:
  test_all_route_methods_classified; test_all_mcp_callbacks_subops_classified; test_closed_job_event_types; test_no_unclassified_export_or_head; test_unsupported_cli_export_claims_absent; test_notification_privacy_gate_requires_evidence.

PathSpec fields: id, protocol, method_or_tool, template, operation, resource_kind, action, audience, auth_supported, resolver, serializer, callback_chain, output_kinds, success_shape, denial_shape, evidence_state, evidence_anchor. Every registered FastAPI route+method (including HEAD), MCP tool/suboperation/callback and event type must appear exactly once in a reviewed inventory or explicitly justified composition. Use a registration diff test, not a filename grep alone. New entry fails CI until classified and given an actor/state test adapter. Public/static none paths are classified but still checked for accidentally added private fields.

Execution order for future implementer: independent core oracle; production identity builder/migration; transport matrix; callback output/input boundaries; whole-envelope schemas; canary/error sinks; signed bytes/container/range; mutation negatives; restore and storage concurrency; actual deployment logging/metadata/cache evidence. Do not hide target failures in broad xfail; current characterization tests can document old behavior only in a clearly separate suite that cannot satisfy notice readiness.

## 9. Unresolved/UNVERIFIED register and required completion evidence

U1 content_access, actual route inventory, proxy asset delivery/mint callbacks, prompt/chat/voice/export transport code: outside read scope; must be inspected and implemented before proof.
[v4] U2 AP schema is source-confirmed on main; binding provenance enforcement remains a step-2 implementation gate. Real v6 upgrade, trusted addresses and trigger order must be executed, not replaced by a mock.
[v3] U3 RESOLVED (OV §2.4 O6): one App-ownership rule; no Accountable basis.
[v3] U4 RESOLVED: job events follow job read (Own); share refusal precedence locked (404 before 409). In-flight revocation boundary: per request (OV §2.4 Linearization); each new MCP poll or stream reconnect re-authenticates; an already-open event stream continues until disconnect after a revoke.
[v4] U5 target specified (OV §3.5): AP NOT NULL/FK already enforce O1; O2/O3/O4 still need post-migration installation, accountable-only binding INSERT, App-scoped immutability and final revocation.
[v3] U6 RESOLVED (OV §6.4): absolute byte rule with per-job leases; W_max per filesystem remains UNVERIFIED until the build measures it.
U7 all serializers/migration/triggers/storage APIs not present in reviewed evidence; claimed contract remains intended.
U8 exact authentication errors, unsupported MCP errors, cancellation success status, content-partial-output handling and repeated share-delete semantics: transport contract unverified; do not infer a test pass from generic 2xx or 4xx.
U9 metadata stripper/container support, HEAD/ETag/range edge contract, reverse-proxy and LiteLLM log behavior: deployment/implementation gates unverified.
U10 config/accounts/key export and absence of content exports/CLI is documented but not independently proven from an allowed route registry.
U11 host filesystem and owner-issued reset remain explicitly disclosed trust limits. This suite proves product views only; it does not claim end-to-end encryption or prevent host/operator access.

Required future evidence: executed tests tied to implementation SHA, generated actor/state/path case manifest with no missing classified entry, mutation snapshots and sink assertions, signed-grant container/range results, migration/trigger/restore receipts, concurrency-budget evidence, and deployment log checks. No execution count or pass count is asserted in this report.

## 10. Current-main evidence and concrete adapters

Every anchor below is against `origin/main` `52408b26`, not the original
v2 evidence revision. Source inspection proves the current mechanism only.
New core, migration, projection and storage tests remain implementation work.

| Adapter | Current-main evidence | Consequence for the test |
|---|---|---|
| Library routes | `gateway/me_media.py:127-278`, `gateway/me_media.py:386-443` | Mount the real router with actor, mutation, metadata, resolver and signer dependencies; separately integrate actual reuse wiring |
| Broad owner scope and parameter presence | `gateway/me_media.py:737-758` | Delete owner-everyone scope in step 3; reject even empty removed keys before lookup |
| Batched job/cache listing | `gateway/me_media.py:979-1095` | Seed contradictory receipt/job/sidecar attribution, hidden rows and cache-only items; test each authority before serialization |
| Own item serializers and titles | `gateway/me_media.py:1098-1225`, `gateway/me_media.py:1658-1667` | Shared view has its own field schema and runs before prompt-derived title/search |
| Delete and audit hiding | `gateway/me_media.py:233-278`, `gateway/me_media.py:676-705`, `gateway/me_media.py:1299-1322` | Step 4 releases claims, stops permanent tombstone hiding, preserves other owners and tests re-generation of identical bytes |
| Session principal expansion | `gateway/me_media.py:1257-1275`, `gateway/admin_store.py:2144-2152` | Sessions own both App kinds, but pre-v3 tombstone release removes only the actor's personal-App aliases and user principal |
| Usage and envelopes | `gateway/me_media.py:1700-1926`, `gateway/receipt_context.py:20-55` | Use actual temporary SQLite, not fake SQL success; project labels/groups and immutable submission person |
| McpDeps and production binding | `gateway/mcp_server.py:109-141`, `proxy.py:14777-14800` | Every callback/config field classified, including visibility gates, OAuth, retention, rename and STT measurement |
| MCP data and job paths | `gateway/mcp_server.py:301-347`, `gateway/mcp_server.py:558-609`, `proxy.py:14653-14669` | Actual direct asset reader/writer plus recording deps; test inline/base64/URL results and independent cancel result |
| MCP tools and errors | `gateway/mcp_server.py:652-690` | Scan content, structuredContent and links; test CallError and generic exceptions, including raw exception print at present |
| MCP OAuth identity | `proxy.py:2425-2453` | Grant Viewer acts like session person/App with that person's Apps, plus privacy permission for history/debug |
| MCP rename | `proxy.py:14618-14643` | Own-account mutation; grant scope required; label change grants no content |
| Ledger REST and provider | `gateway/api.py:954-974`, `proxy.py:14047-14076` | Current admin gate lacks request ownership and observed contains requested_model; maker route and owner projection need distinct tests |
| Ledger MCP | `gateway/mcp_server.py:291-299`, `gateway/mcp_server.py:654-669`, `proxy.py:14006-14010` | Requests without id = none; with id = content:receipt:read or owner projection, never admin bypass |
| Ledger regressions to rewrite | `tests/test_retention_ledger_headless.py:43-73`, `tests/test_retention_ledger_headless.py:78-95` | Replace admin access expectations with maker success, masked owner and forbidden/missing equivalence |
| Receipt structural guard and source envelope | `gateway/receipt_store.py:213-233`, `gateway/receipt_store.py:401-427`, `gateway/receipt_context.py:20-55` | Audience serialization is not ownership; scalar canaries and empty-vs-unscoped selectors must be tested |
| Receipt retention vocabulary | `gateway/receipts.py:72-107` | Add request_history/debug_capture enums with RR; owner gets kind/decision only, never reason/deadline/client_address/api_key_name |
| Principal aliases | `gateway/ownership.py:17-102` | Revoked hashes remain readable aliases; they do not authenticate or reassign resources |
| AP schema and migration | `gateway/admin_store.py:143-154`, `gateway/access_plan_migration.py:13-19`, `gateway/access_plan_migration.py:527-531` | Use real v6 upgrade; accountable people already exist; triggers install after AP owner writes |
| App creation and key issuance | `gateway/admin_runtime.py:358-401`, `gateway/admin_runtime.py:620-634` | Service default already exists; remove binding issuer owner exception; reject automation provenance substitution |
| Key/runtime identity | `gateway/admin_runtime.py:2312-2322`, `gateway/admin_runtime.py:2403-2433`, `gateway/admin_store.py:2463-2485` | Single SELECT resolves current key/App; only personal accountable-created keys have a person |
| Session and trusted-address identity | `gateway/admin_runtime.py:2436-2482` | Trusted address has App-only Own; never inherit a person merely because App is personal |
| Trusted-address issuance | `gateway/admin_api.py:2096-2117`, `gateway/admin_store.py:2065-2098` | Test accountable add through admin and /me adapters, direct store INSERT trigger, session-App refusal and zero-binding transfer |
| Startup creator recovery | `gateway/admin_store.py:550`, `gateway/admin_store.py:612-655` | Delete rather than rerun recovery before revocation; test restart and unknown creators |
| Control-plane key adoption | `gateway/signin_admin.py:485-499`, `gateway/signin_admin.py:549` | OLD App-key scope cannot abort legitimate adoption |
| Try and incognito state | `gateway/user_runtime.py:472`, `gateway/user_runtime.py:499-536`, `web/src/app/try/page.tsx:1325`, `web/src/app/try/page.tsx:1413-1414` | Try is personal; extend its existing sessionStorage forget state to the single D50 incognito control |
| Reset lifetime/issuer | `gateway/accounts_api.py:72-73`, `gateway/accounts.py:473-475`, `gateway/accounts.py:503-506` | Route default 1 hour versus helper 7 days; Security event joins recorded issuer, never invents redeemer identity |
| STT admission | `gateway/stt_chunking.py:762-868`, `gateway/stt_chunking.py:369` | Replace independent byte counter with shared locked per-device leases; keep concurrency separate; test replay/top-up/finally |
| Scratch filesystems | `gateway/stt_chunking.py:318-333`, `gateway/private_tmp.py:35-42`, `Dockerfile:26-27` | Bare-metal data default and Docker image scratch may differ; test resolved devices, including absolute STT spool |
| Existing conversation reader | `proxy.py:25886-25888`, `gateway/retention.py:472-484` | Consolidate into one core; current scope behavior is characterization, not an owner-role grant or permission to widen D51 |
| RR contract to integrate | `docs/design/REQUEST_RETENTION.md:126-131`, `docs/design/REQUEST_RETENTION.md:427-504`, `docs/design/REQUEST_RETENTION.md:1712` | Slice 5 uses core decisions, narrowing-only retention policy and atomic Library/history cascade |

For media fixtures use real `img_`/`vid_`/`aud_` plus unique 64-hex IDs.
Use synthetic databases/files and an isolated test server. No live ports,
provider calls, secret files or deployed state are needed for this design.
Fixed mocks prove plumbing only; bound-reader and actual migration cases
must establish authority. Future tests must pin the actual implementation
SHA and record executed evidence before any privacy notice ships.

### Suggested attribution conflict fixture expansion
Use job-backed and cache-only items with: own receipt/foreign job; foreign receipt/own job; missing request receipt/present parent receipt; conflicting first and parent receipt; embedded cache receipt contradicting sidecar; maker-only metadata; no ownership fields; resolvable revoked alias; orphan unmapped hash; shared exact parent and separate poster; multi-owner same bytes; physically retained but logically deleted-by-A. Assert final policy comes from each typed resource's documented authority and never broad owner scope. Unresolved attribution conflicts fail closed rather than selecting whichever identifier benefits the caller.

## Completion statement
This is a complete proposed actor x resource x path test design within the permitted evidence boundary. UNVERIFIED entries are explicitly release-blocking where necessary; none are represented as passing. No tests were executed or counted, no implementation was changed, and no member privacy notice readiness is claimed.

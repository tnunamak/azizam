# Authorization test design: Owner visibility (adopted as the test section of OWNER_VISIBILITY v3)

> **Adoption note (v3).** This file is the dot test-design pass of v2 (`azizam-testdesign-owner-visibility-81ce320`), copied unchanged except where a line starts with **[v3]**. Those lines were rewritten because OWNER_VISIBILITY v3 changed the behaviour they test: one App-ownership rule instead of Own plus Accountable (OV §2.4 O6, §3.1), NULL-safe triggers and the ownership cut-over (OV §3.5, §8.1), deletion releases a claim (OV §6.3), the absolute disk reserve with leases (OV §6.4), and no two-store restore guard (OV §8.3). Where this file and OV v3 disagree, OV v3 wins and the disagreement is a defect to fix here. Line references below are to the original v2 file, offset by the three lines of this note.

Repository: tnunamak/azizam
Exact reviewed commit: 81ce3204c454658383e0b82d8e42920ff6d885d3
Assignment: azizam-testdesign-owner-visibility-81ce320-r0
Deliverable status: TEST DESIGN COMPLETE; implementation and validation NOT performed.

Evidence boundary: only docs/design/OWNER_VISIBILITY.md, gateway/me_media.py, gateway/mcp_server.py, gateway/ownership.py, gateway/admin_runtime.py, gateway/media_jobs.py, and gateway/receipt_store.py were inspected through GitHub at this commit. No bridge workspace was read. No tests, probes, provider requests, edits, publication, Work/Codex tasks, or model API calls were performed. This report contains proposed tests and assertions, not test results. Source links use https://github.com/tnunamak/azizam/blob/81ce3204c454658383e0b82d8e42920ff6d885d3/<path>#L<n>. Routes or handlers outside these seven files are UNVERIFIED even when the design document cites them.

## 1. Blocking conclusions and test-oracle authority

The design file explicitly says v2 is not built. Source-confirmed current behavior must not become the privacy oracle:
- Library owner/superadmin broadening remains, including a local-network detail/download bypass. Regression tests must require the target denial rather than bless this behavior.
- MCP has direct asset read/store callbacks in addition to REST dispatch. Route authorization tests alone cannot cover input reuse, returned bytes, signed-link minting, or discovery content.
- Receipt storage forbids sensitive key names but not sensitive values. Whole-envelope recursive typed projection, not the existing structural guard, is the target owner boundary.
[v3] - Service-App accountable identity is a prerequisite for the visibility switch. Current create_app converts a supplied owner_user_id to personal kind. v3 makes the ownership cut-over (OV §8.1) a hard prerequisite with its own preview, apply and verify; tests must run the real cut-over, and a fake service+human-owner fixture must not conceal a missing production migration.
[v3] - RESOLVED in v3 (OV §3.1): a session Viewer intentionally includes every App the person is accountable for, of both kinds (OV §2.4 O6). There is no Accountable basis. Test that the production Viewer builder includes service Apps for the session and only its own App for a key; prompts and chats stay Self and Self+App.
- Trigger SQL, migration machinery, content_access, share storage/serialization, signed delivery, metadata stripping, route registry, full log pipeline, actual export handlers and storage admission implementation are UNVERIFIED under this allow-list.
[v3] - RESOLVED in v3 (OV §6.4): the disk pause is an absolute byte rule, free_bytes - leased_bytes >= reserve_bytes, with reserve_bytes defaulting to W_max + 2 GiB and in-process per-job leases. Percent is display only.

Normative ordering for these proposed tests: authenticated identity first; removed-parameter validation on authenticated /me requests; resource existence/authorization core; audience-specific serializer; side effect/bytes/provider boundary. Authentication error status and unsupported-MCP wire encoding are UNVERIFIED; lock their established transport contract after permitted implementation inspection rather than inventing 401/403 mappings here. Every resource-level denial below is exactly HTTP 404 with error code not_found, equivalent in semantic status/code through MCP dispatch, unless a more specific explicit error below applies.

No notice may ship because these tests were designed. Notice gate requires implemented full path coverage, executed success evidence, resolution of the design conflicts and deployment log checks.

## 2. Fixture dimensions and complete cross-product rule

Parameterize every applicable PathSpec over every actor below and every state below, plus kind/action. Do not sample only representative actors. The policy function below defines every resulting cell; a transport applicability adapter distinguishes an unsupported protocol from a content denial. State labels alone never grant authority: compute the independently seeded record fields and grants.

Actors:
A: member A session, plus A personal owner-created key as a companion transport.
B: member B session/key, with no relationship to A.
O: owner person session/control-plane credential. Also seed O's genuine own records; owner role never changes a content result.
D: admin-not-owner person session/control-plane credential. Also seed D's genuine own records; admin role never changes a content result.
[v3] S: active service-App key for app S; person_id=None; canonical principal app:S plus its historical key aliases. This is Own for S records, never Self.
N: active personless personal-App key (created_by is another person, NULL, or empty string), and active appless/service-account credential variants. Before migration, its same-App creation Own behavior is explicit; after switch the bad-provenance personal key must no longer authenticate. An unrelated control-plane principal owns no member content.
Rk: revoked key, with the exact same principal and historical resource as a live replacement key.
Rs: revoked/expired session for each person, and disabled person with stale session.
Q: current recipient of A's single-creation share, include_prompt false/true; repeat with B/O/D as recipient. Sharing is a relation, not a role.
F: former recipient after maker stop, recipient remove, include_prompt downgrade for prompt only, and delete-triggered revocation; repeat with B/O/D.
[v3] C: accountable person of service S, accessed through a session without authenticating as S. Under v3, C's session is Own for S records (OV §2.4 O6). Essential additional actor, even when C is O or A.
H: unauthenticated bearer URL holder; essential signed-delivery actor.
U: unauthenticated without a bearer URL.

Seed all actor identities from trusted auth fixtures. Request JSON/query parameters claiming person_id, app_id, owner_keys, is_owner, maker or basis must not alter the Viewer. Personal-key created_by matching owner is independently checked. Record/account names must be safe controlled strings distinct from canaries.

Resources/states:
own: canonical principal matches actor for creation/voice/receipt/job; Self matches prompt; Self+App matches chat. For N/S own does NOT mean a prompt gains a person.
other: A's record, no principal/person/basis match.
shared: A's existing sole-maker creation and separate poster shared to Q; optional live prompt row; other kinds get NO authority merely because associated creation is shared.
unshared: same resources before share, plus another asset by the same maker and siblings/inputs/workflow/receipt/job. A grant is not transitive.
deleted: (a) missing record/bytes, (b) deleted by A while bytes survive for co-owner B, (c) orphan poster, (d) dangling/revoked share. A/F lose access; B retains only B's surviving Own association.
unattributed: (a) legacy hash resolvable to an App, (b) maker metadata plus resolver evidence, (c) proven unbound migrated to designated service App, (d) unresolved or contradictory attribution. Only evidence-backed resolution/migration grants; unresolved denies everyone including owner. Operational quantities may include unresolved storage without revealing individual assets.

[v3] For each state seed fresh independent DB/file fixtures. A share to Q must not accidentally also put A's App principal in Q.owner_keys. Own+Shared collisions are a separate precedence test: decide returns Own, the own serializer is used, and prompt reads stay a separate prompt decision. After a share stops, independently held Own authority remains. Do not assert that stopping a share revokes unrelated ownership.

Resource/action oracle, generated exhaustively for all six kinds x all six actions:
[v3] creation read = Own OR active Shared for that exact asset/poster.
[v3] creation delete = Own.
creation reuse = Own only.
creation share = Maker (Own AND exactly one non-null maker equal to Viewer.person_id); then supported container checks.
[v3] creation link = Own mints g=o; Shared-only mints g=s.<share_id>. Shared-only cannot request g=o.
prompt read = Self OR exact active share with include_prompt true, reading its authoritative maker's live prompt row.
prompt delete/reuse = Self only. Share does not authorize md=full or workflow reuse.
chat read/delete/reuse = non-null Self AND same app_id; appless variant requires the same owner_key_hash. Neither matching person alone nor matching App alone suffices.
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

P07 Receipt detail GET /me/api/receipts/{id} and key/MCP receipt lookup:
receipt read oracle; full own view constrained by plan detail, no owner role bypass; share does not grant receipt access; unrelated 404. Owner counterpart GET /admin/api/activity/requests/{id}: 200 projection only when O, including for O's own receipt on this operational endpoint; D/B must be denied by role and disclose no item. Owner role-denial wire status is UNVERIFIED. receipt store methods VERIFIED; HTTP adapters outside allow-list UNVERIFIED.

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
Own-only target; Q/F/O/D without Own get 404 before events() called; include SSE/poll/cursor/reconnect/replay variants when registered. Closed set of permitted event types; all payloads typed/audience-safe. Assert contiguous per-job seq and absence of household-wide row IDs. Forbidden event request must not repair event sequencing or mutate storage. Midstream revocation semantics need explicit boundary: proposed revalidate each delivered event/reconnect; current design does not specify an in-flight guarantee, so mark unresolved rather than claim it. Read-only snapshot checks cannot prove live-stream revoke. Actual transport registrations UNVERIFIED.

P13 Cancel/delete/reuse/share:
[v3] denied job cancel leaves status/phase/cancel_requested/result/events unchanged and cancel callback uncalled. Allowed Own job cancel follows existing accepted/cancelled contract; exact HTTP success status UNVERIFIED. MCP job cancel returns {status: cancel_requested, job_id} when the cancel call succeeds; a later poll or content failure does not turn it into an error (OV §3.2).
Allowed own Library DELETE verifies response success and exact detach of caller's owner/maker/prompt; physical bytes removed only when sole owner and maker; revoke that maker's shares. Shared-only delete returns 404 and does not mutate; recipient removing share is P14, not content delete.
[v3] Run/reuse foreign/shared-only asset ->404 before read_asset, store_asset, job admission or paid calls. Own reuse allowed, including a session reusing a service App asset it is accountable for; no person prompt access.
All six actions must be tested directly at core as well as path adapter, including methods not exposed (unsupported HTTP method does not prove core Deny).

P14 Sharing new target APIs:
PUT /me/api/media/{asset}/shares/{person}: sole Maker with valid target yields 201 first create, 200 idempotent/update, exactly one active pair; include_prompt default false; title/source not copied from prompt. Other people/owner/admin cannot grant on A's behalf. Source prompt/maker row authoritative even in corrupt competing-row fixture.
DELETE /me/api/shares/{id}: maker or exact recipient revokes active row, inaccessible other caller gets 404/no mutation. Repeated delete semantics UNVERIFIED (choose idempotent target contract explicitly). GET /me/api/shares?direction=by-me|with-me returns exact authorized rows. GET share-targets includes enabled people's id/display-name/owner flag only; no private fields.
Sharing list/create/remove unsupported on MCP per design; explicit unsupported error, never empty-success disguised as capability. Entire share API implementation UNVERIFIED.

P15 MCP:
capabilities, run each supported target kind, job poll/wait/content/cancel, direct read_asset/store_asset and each returned signed-link minting path. Use detailed appendix for actual operation names. Keep authenticated context unchanged through dispatch; rejected or missing identity cannot default to privileged context.
Capabilities/discovery may enumerate public capability descriptions but cannot contain a private receipt, resource count, prompt example, filename, dynamic asset or signed URL. Parameterize O,D,S,N,Q,F,Rk,Rs and seed foreign data in dependencies; public fixture yields exact stable result regardless of private resources.
Run submits own new work only after authorization of EVERY nested asset reference; combine owned+foreign input, list duplicates, poster refs, alternate MIME/asset ID/URL representations supported by schema. Shared link cannot become input permission. Reader receives trusted Access for reuse, not arbitrary caller-supplied basis.
Direct returned asset/link: only authorized current output, deny a backend stub returning foreign asset ID/URL; store/mint callbacks require explicit authenticated principal/Access. Check async wait/poll returns and image/audio/video result branches.
MCP adapters VERIFIED; proxy callback implementations UNVERIFIED. Do not assert full protection based only on route mocks.

P16 Removed parameters and identity injection:
every /me registered route with scope,owner,person keys rejects presence with 400 param_not_supported. Cases empty, repeated same/mixed values, scope=a&scope=b, percent-encoded key, extra allowed query alongside it, all/everyone/local-network/another user. No broad query performed; responses have no private markers. In particular current person=local-network detail/download must not work. FastAPI simply ignoring an obsolete argument is a failure. Unknown nonremoved parameters follow existing router policy, UNVERIFIED.
No owner/superadmin legacy role can bypass this. Migration must eliminate superadmin and no alias remain. Unauthenticated ordering is separate auth contract.

P17 Exports:
No content export/backup/CLI implementation was confirmed; the design says none exists. Mark REST content export, bulk archive, CLI and MCP export as UNVERIFIED/unsupported, not tested-safe. Inventory any existing account/config/people/App/key exports as own-account/operational: no creation blobs, prompt/chat/error body, filenames, URLs, nested content fields or extra owner keys. Owner metadata export never yields content grant. For future content export apply per-item kind/action, exact authorized item list, safe archive filenames/manifests, no foreign embedded bytes, prompts only Self, Shared export no workflow/receipts and only explicit share fields/bytes. Export code and registry require additional scope in implementation phase; no guessed live endpoints probed.

P18 Storage:
[v3] GET /me/api/storage own logical counts/bytes; GET /admin/api/storage O projection only; no asset IDs/names/thumbnails/item dates/links. Two-owner bytes count fully to each person and once physically; sharing adds zero; service-App bytes are listed per service App with its accountable person, not added to the person row; unresolved remains aggregate Not linked to a person. Deletion releases a claim (OV §6.3): two makers delete in turn -> bytes and sidecar removed after the second; first deleter's App key can no longer read or reuse it; concurrent identical generation under the cache lock keeps or rewrites the file. Reclaim estimate = files with no owner left after release; rescan after delete. Timestamp age >15 min stale; separate target devices merged only when device identity equal.

P19 Key issuance and service binding:
POST /admin/api/apps/{id}/keys and /me/api/apps/{id}/keys: only App accountable person, for personal and service Apps; unrelated A/B/O/D -> exactly 403 not_your_app, no credential row/token/audit of issued secret. Allowed issuance gets the established success shape and exactly the intended App principal; never grants another App. /me/api/try-key, invite acceptance and both /setup/first-app branches must obey own-App binding; exact router bodies and setup's nonperson branch UNVERIFIED. Control-plane /admin/api/admin-keys and generate-admin-key retain operational roles but service_account principal grants no member content. Old bad-provenance keys must fail auth after switch even if caller is owner. App names/profile collisions confer nothing. Trigger and migration tests are prerequisite, not a substitute for each issuance path.
Only admin_runtime credential/App primitives were read; HTTP issuance endpoints and actual store constraints UNVERIFIED.

P20 Job event writes and idempotency:
[v3] Event append is NOT one of the six content_access actions in v2. Keep Own-only existing write authority as a proposed extension requiring explicit inventory classification; Own read/cancel of a job never implicitly grants event append. Client note/progress source and bounded payload validation must be enforced even for owner/admin; lifecycle event types or phase/status claims rejected before mutation. For same request_id, a different owner_scope may not replay or replace another job. Same canonical principal with rotated live key may replay only exact input digest; missing legacy scope/digest cannot prove replay. No denied request allocates seq/event row, mutates progress, consumes reservation or dispatches work. Actual event HTTP routes are UNVERIFIED; storage behavior is confirmed in media_jobs.

## 5. Signed metadata and grant abuse suite

Use synthetic known fixtures with marker embedded in PNG/JPEG/WebP/WAV/FLAC/MP3/MP4/WebM/MKV/OGG/M4A metadata and distinct visible-pixel/audio markers. For Shared md=none, decode container metadata and scan raw returned bytes for metadata sentinel; do not demand removal of marker intentionally present in the image/audio content. Include poster metadata separately. GIF share is 409 file_type; no stripper/error fallback may return unstripped bytes. For corrupt/unsupported container, fail closed without exposing original data and log only safe error schema; exact error status needs implementation contract.
Pair job-backed/cache-only, no-saved-prompt/saved prompt, include true/false, Range after revocation, recipient remove, maker delete, restore revocation and new-share IDs. Before read spies on open/read_bytes/stream preparation, not merely final response. Grant tampering matrix includes asset, parent/poster mismatch, expiry boundary frozen clock, md missing/empty/full, g missing/empty/unknown/s.other and old signature versions.
A cache or CDN assertion in unit tests does not prove deployment cache behavior; deployment headers/logging are separate gates.

## 6. Planted-marker noninterference and recursive schema gate

Canary fixture produces unique synthetic values per leak channel, e.g. CANARY_<run>_prompt, _workflow, _chat, _filename, _path, _error, _metadata, _cost, _decision, _source, _requested_model. Use distinctly seeded private asset IDs, poster IDs, file basenames, engine paths and fake signed links. Trusted account/App/catalog display names use safe fixtures so a test does not conflate intentionally disclosed names with private fields.

Plant markers in:
prompt and workflow rows; saved chat messages/tool output; job error/assets/result metadata/event nested fields; creation filename/sidecar metadata/seed/input_hash/provenance; exception str/repr/args/chain and provider error echo; allowed-key receipt scalar values such as requested_model/client_hint/source_label; nested cost and resource_decision unknown strings/maps/lists; request source/envelope, grouping/facets/filter options, sort labels, cursor or pagination metadata; direct MCP dependency responses and content callback results.
Do not plant only forbidden receipt keys: _assert_safe might reject those before the owner projection is exercised. Direct projection fixtures may contain future extra fields; separate store tests assert structural rejection for prompt/input/messages/body/bytes keys.

For every inventoried operational and own-account route as O and D, and every corresponding MIME/HEAD/error/stream variant:
1. Capture complete status/headers/cookies/body, redirects including Location, JSON/text/SSE frames, MCP content and structuredContent and error.data; include base64 only where encoding is declared.
2. Assert no private canary or forbidden private asset/poster ID, signed query marker sig=, /v1/assets/ or equivalent private download URL, filesystem path. Known own/shared content tests are separate: it is incorrect to prohibit intentionally included shared bytes/prompt in O acting as ordinary authorized recipient.
3. Recursively validate exact schema at every JSON leaf. Opaque id fields must match expected fixture IDs and grammar, not any arbitrary string; enum values from closed sets, timestamps valid, numerics finite/ranged, booleans actual booleans, known names resolved by ID. An asset ID cannot be smuggled into request_id by satisfying generic 'string'.
4. Compare response for two fixtures differing only in forbidden content. Normalize authorized timestamps/correlation IDs if needed; responses, visible counts/facets/group keys must be identical. This catches content-dependent aggregate labels even when the sentinel is transformed.
5. Keep valid operational facts: submission person, App, resolved model/workflow, status/layer/terminal reason, cost quality/source/reconciliation, attempt and coverage fields. A requested_model-only receipt must show Unknown model, not canary. Unknown layer presents safe next action, not provider error.
6. Add new arbitrary receipt/job field and nested new cost/decision field: projection omits it until schema intentionally expanded. No source envelope can add it back.

Capture caplog records (message, args, exception info/rendered trace), capsys stdout/stderr, cache/key_activity.jsonl every JSONL line, task/background worker logs and fake provider adapter failures. Scan from before request through awaited test-background completion so late error logs are included. Assert line-by-line no canaries/assets/signed links and safe logs include exception class plus allowed correlation ID, never arbitrary exception text. Test generic Exception AND MCP CallError (message/details branch), job failures, malformed input, cancellation, timeout, storage_full. No full traceback containing private locals/error str. Pure exception-count ratchet is supplementary, not proof.
Full proxy/provider/runner/LiteLLM/Traefik sinks are outside scope and UNVERIFIED. Require deployed LiteLLM message/spend prompt logging disabled and reverse-proxy signed query suppression verified on deployed version before notice. Do not scan or expose production secrets in this design.

## 7. Identity, migration, trigger and storage budget regressions

B1 service identity prerequisite:
[v3] Seed legacy service App owned by service_account, plus existing personal Apps, known principal hashes, histories and submission-spend attribution. Migration preview must independently identify missing human accountable binding, not invent it from receipt history. Apply explicit mapping to actual service App without converting kind to personal or changing canonical app principal, preserves personless historical creators, preserves record owners and spend subject, and gives the accountable person Own for that App only (OV §2.4 O6). Unresolved mapping prevents notice/switch verification. Active keys with created_by IS NOT the accountable person including NULL/empty are listed and revoked exactly; replacement owner-created credentials map to same App. Both pre-migration denied and post-migration Own positive tests use actual migration APIs; a direct SQL seed alone is only a core fixture.
Verify no wrong keys revoked; revoked keys fail authentication; live replacement reads historical alias-owned records. No current service-account owner can be mistaken for person. AP migration implementation UNVERIFIED.

M1 session provenance:
[v3] C session owns personal App P and is accountable for service S. v3 policy (OV §2.4 O6): the session Viewer includes both P and S principals. Assert decide(C_session,S_asset,read|delete|reuse|link)=Own, share=Deny (no maker); decide(C_session,S_prompt or chat)=Deny; decide(C_personal_key,S_asset,*)=Deny; decide(S_key,S_asset,reuse)=Own. Repeat with App name collisions. (Transfer cases are deferred until a transfer route exists; v1 has none, OV §2.4 O3.)

M2 NULL-safe binding:
[v3] Against real schema trigger installation (OV §3.5), parameterize old/new kind and owner including same value, different value, NULL, empty string, valid A/B and service-account legacy IDs. Any write that leaves owner_user_id NULL or owner_service_account_id non-NULL aborts (insert and update), so A->NULL->B is impossible. Kind change aborts (IS NOT). Personal owner change aborts. Service owner change aborts while any credential with revoked_at IS NULL exists (expired-but-unrevoked counts as live). Credential insert aborts unless created_by IS the App's owner_user_id (NULL created_by aborts). Credential subject_kind/subject_id/app_id/created_by updates abort. Same-value updates are no-ops and pass. The only first assignment is the cut-over, which runs before the triggers exist.
[v3] Assert transaction abort, original row/keys unchanged, no transfer leaked on rollback. Deferred until a transfer route exists (v1 has none): zero active keys permits a service transfer only through the transfer operation (revoke all keys, then change owner, in one transaction); afterward old C loses Own, new C gains; past spend remains old subject. In v1, test only that the triggers refuse every owner change with a live key, and that BEFORE DELETE ON apps aborts; a fresh database has the triggers and ownership_version=1 from its base schema. Revoked historical aliases are not live auth. Triggers are installed with DROP then CREATE; verify compares sqlite_master SQL text.

Switch/restore:
[v3] Preview/apply/verify every crash boundary of both units (ownership cut-over, then visibility switch) under the AP 4.2 protocol; unaccepted marker refuses start; missing ownership_version or visibility_version refuses start for a release that needs it; auth-disabled config after switch refuses start; unknown/partial state never sets notice-ready. Superadmin mapping leaves no alias; removed params reject. Ownership cut-over: an App owned by a service account with no approved person blocks apply; no App becomes personal because it has a person; only personal->service kind changes are offered; an App used in the last 30 days that would lose all keys blocks apply; the verify self-test aborts every forbidden write inside a rolled-back savepoint. Restore: the two-store revision guard is removed (OV §8.3); test that a share write is one transaction, and that the ops-note SQL stops every active share so old g=s URLs return 403.

[v3] M3 absolute reserve (OV §6.4, the selected contract):
[v3] Per filesystem d: admit a storage-writing job only if free_bytes(d) - leased(d) >= reserve_bytes, checked before any upload body is read and before engine/provider calls. reserve_bytes defaults to W_max + 2 GiB; setting it below W_max returns 400 reserve_below_largest_job. Each admitted job leases its peak_bytes until it ends; a resumed job re-leases before running. Paused when the rule fails; resume at reserve_bytes + resume_margin_bytes; Low below reserve_bytes + warn_margin_bytes. Every storage-writing kind declares peak_bytes; an undeclared kind fails the inventory test.
[v3] Tests use deterministic disk and admission spies: free - leased at reserve-1/reserve/reserve+1 and at the resume margin; giant filesystem with high percent used but enough absolute bytes stays Healthy (the measured 1.45 TB / 79.5 GB case); small filesystem with low percent used but too few bytes pauses; percent never changes a state; unknown disk pauses; separate data/cache/spool devices each checked; aliases of one device counted once; N concurrent barrier starts are admitted only while leases fit; leases released on success, failure and cancel; restart-resume re-lease; worst-case video+upload+voice; write ENOSPC gives storage_full; no auto-delete.
[v3] Refusal exactly 503 storage_paused with layer gateway before upload persistence/job enqueue/engine/provider; upload with no declared length is refused before reading the body. Existing downloads/signin/browse/delete continue, and chat that stores no file is not paused; running job failure storage_full, no success; logs no path/private content. Byte hysteresis and recovery after cleanup.

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
  test_own_receipt_access_plan; test_operational_projection_actor_matrix; test_recursive_schema_and_future_fields; test_submission_person_immutable; test_unknown_model_no_requested_fallback; test_envelope_group_filter_leaks; test_storage_aggregate_only.
test_jobs_events.py:
  test_job_status_actor_matrix; test_cancel_no_effect_on_deny; test_content_each_asset_authorized; test_events_actor_matrix; test_event_type_closed_set; test_per_job_seq_no_household_ids; test_denied_events_no_repair_write; test_wait_denied_before_poll.
test_mcp_boundaries.py:
  test_tools_and_subops_registry; test_dispatch_context_preserved; test_capabilities_no_private_data; test_run_nested_asset_reuse_matrix; test_direct_reader_personless_shared_foreign; test_output_store_and_link_access; test_job_poll_wait_content_cancel; test_error_callerror_and_exception_safe.
test_owner_leak_canaries.py:
  test_every_owner_and_admin_surface_canary; test_two_world_noninterference; test_stdout_stderr_jsonl_and_background; test_error_path_markers; test_new_field_not_implicitly_projected.
test_migration_bindings_restore.py:
[v3]   test_switch_verified_before_notice; test_ownership_cutover_preview_apply_verify; test_bad_provenance_exact_revocation; test_null_safe_trigger_transition_matrix; test_issuance_trigger_atomic; test_fresh_schema_has_triggers; test_share_write_single_transaction; test_ops_restore_sql_stops_shares; test_auth_disabled_start_denied; test_reset_event_issuer.
test_storage_admission_budget.py:
  test_absolute_budget_boundaries; test_small_large_filesystem_counterexamples; test_concurrent_working_set_bound; test_per_device_accounting; test_unknown_pauses; test_no_provider_before_admission; test_running_enospc_not_success; test_cleanup_and_download_still_work.
test_path_inventory_exports.py:
  test_all_route_methods_classified; test_all_mcp_callbacks_subops_classified; test_closed_job_event_types; test_no_unclassified_export_or_head; test_unsupported_cli_export_claims_absent; test_notification_privacy_gate_requires_evidence.

PathSpec fields: id, protocol, method_or_tool, template, operation, resource_kind, action, audience, auth_supported, resolver, serializer, callback_chain, output_kinds, success_shape, denial_shape, evidence_state, evidence_anchor. Every registered FastAPI route+method (including HEAD), MCP tool/suboperation/callback and event type must appear exactly once in a reviewed inventory or explicitly justified composition. Use a registration diff test, not a filename grep alone. New entry fails CI until classified and given an actor/state test adapter. Public/static none paths are classified but still checked for accidentally added private fields.

Execution order for future implementer: independent core oracle; production identity builder/migration; transport matrix; callback output/input boundaries; whole-envelope schemas; canary/error sinks; signed bytes/container/range; mutation negatives; restore and storage concurrency; actual deployment logging/metadata/cache evidence. Do not hide target failures in broad xfail; current characterization tests can document old behavior only in a clearly separate suite that cannot satisfy notice readiness.

## 9. Unresolved/UNVERIFIED register and required completion evidence

U1 content_access, actual route inventory, proxy asset delivery/mint callbacks, prompt/chat/voice/export transport code: outside read scope; must be inspected and implemented before proof.
U2 service-App human accountable migration/AP integration and trust provenance: blocker B1; direct mocked post-migration fixture insufficient.
[v3] U3 RESOLVED (OV §2.4 O6): one App-ownership rule; no Accountable basis.
[v3] U4 RESOLVED: job events follow job read (Own); share refusal precedence locked (404 before 409). In-flight revocation boundary: per request (OV §2.4 Linearization); a live event stream or MCP wait re-authenticates on each poll or reconnect, and a stream already open finishes its current response.
[v3] U5 RESOLVED (OV §3.5): NULL refused on every App write; first assignment only in the reviewed cut-over before triggers exist; issuance checked inside the INSERT.
[v3] U6 RESOLVED (OV §6.4): absolute byte rule with per-job leases; W_max per filesystem remains UNVERIFIED until the build measures it.
U7 all serializers/migration/triggers/storage APIs not present in reviewed evidence; claimed contract remains intended.
U8 exact authentication errors, unsupported MCP errors, cancellation success status, content-partial-output handling and repeated share-delete semantics: transport contract unverified; do not infer a test pass from generic 2xx or 4xx.
U9 metadata stripper/container support, HEAD/ETag/range edge contract, reverse-proxy and LiteLLM log behavior: deployment/implementation gates unverified.
U10 config/accounts/key export and absence of content exports/CLI is documented but not independently proven from an allowed route registry.
U11 host filesystem and owner-issued reset remain explicitly disclosed trust limits. This suite proves product views only; it does not claim end-to-end encryption or prevent host/operator access.

Required future evidence: executed tests tied to implementation SHA, generated actor/state/path case manifest with no missing classified entry, mutation snapshots and sink assertions, signed-grant container/range results, migration/trigger/restore receipts, concurrency-budget evidence, and deployment log checks. No execution count or pass count is asserted in this report.

## 10. Source-grounded implementation map and concrete adapters

These are static observations at the reviewed SHA, not executed evidence. They distinguish the target matrix from current behavior and identify where to bind future tests.

### me_media.py
Source: https://github.com/tnunamak/azizam/blob/81ce3204c454658383e0b82d8e42920ff6d885d3/gateway/me_media.py
- Routes are under /me/api (L95): list_jobs L122-147; job_detail L149-160; list_media L162-188; media_detail L190-219; delete_media L221-269; media_download L271-307; usage_summary L309-327.
- Current denied/missing job detail uses 404 job_not_found. Current media lookup uses 404 asset_not_found; unavailable file is 404 asset_unavailable. Current download metadata=full without own workflow URL is 403 prompt_not_accessible; invalid metadata value is 400 invalid_metadata. These are CURRENT compatibility facts, not v2's normalized 404 not_found target. Tests for new contract must choose the documented new envelope deliberately rather than mix old and new errors.
- Owner flag includes superadmin at L75-77; role refresh L358-365/L440-450. Scope broadening L461-480 and truthy-parameter test L483-485 are current bypass surfaces. List-only local-network filtering L520/L535-536 does not constrain direct detail/download.
- receipt/job scope helpers L925-942 treat app_ids=None as unrestricted. Restricted scope gives truthy app_id priority over owner hash. Job-backed media L644-723 uses first existing request_id/parent_request_id receipt before job ownership; cache branch L724-788 uses owner-key intersection plus receipt scope if present. maker_person_ids only relabels source at L691-694. Thus mixed attribution fixtures must include contradictory job, receipt and sidecar ownership and must not silently accept the current precedence as final typed-core contract.
- owned_library_asset L562-594 is member-scoped and wired to make routes L98-120. It is a critical independent reuse boundary if list/detail gains Shared. Delete remains member-scoped, and conservative_asset_deleter L397-426 only physically removes sole-owner/sole-maker files. Per-person deletion history L861-877 must be preserved.
- Scope credential expansion L819-837 includes returned credential hashes without local revoked filtering. ownership.py independently confirms historical alias expansion; do not confuse this with auth.
- Usage SQL and final filtering L1253-1279, counts L1328-1373, client receipt feed L1374-1385, coverage L1389-1409 and returned data L1413-1414 require separate assertions. Even owner default-own usage currently calls _usage_sources with app_ids=None at L1242 and returns people; own-account envelope needs target scoping, not merely filtered receipt items.
- Router unit fixture: mount create_me_media_router in a FastAPI app; inject session_provider, mutation_validator, signed URL recorder, metadata reader, resolver/deleter and catalog. Stubbing me_make wiring is acceptable for narrow route units only; actual owned_library_asset wiring must have integration coverage. For SQL aggregation, use real temporary SQLite or explicitly label a fake-connect test as mock-only. Use valid img_/vid_/aud_ plus unique 64-hex IDs and stable UTC month/time. Avoid real ffprobe by stubbing embedded provenance in adapter unit tests; metadata-container integration is separate.

### mcp_server.py
Source: https://github.com/tnunamak/azizam/blob/81ce3204c454658383e0b82d8e42920ff6d885d3/gateway/mcp_server.py
- McpDeps L89-103 includes authenticate, call, read_asset, store_asset. Direct asset callbacks receive request AND auth; actual implementations are outside scope.
- POST /mcp L605-646 authenticates before parsing/dispatch; batch entries share that request auth. handle_message L563-602 dispatches tools/list, capabilities, run, job. Test supported modern/legacy forms and mixed batches; no response/callback may pick up another concurrent request's auth.
- capabilities/resolve/_describe L289-359 call zero-argument catalog/aliases; unknown targets can expose suggestions. Speech detail also GETs /v1/audio/voices. tools/list is static TOOLS. Test capability list, query, intent, full/short target, alias, speech alias, unknown-target suggestions; no private schema defaults or examples. Fixed catalog mocks prove only plumbing, not real catalog safety.
- run L373-411 passes read_asset/request/auth into build_request for sync and async. Test actual bound reader directly AND through build_request; read permission is insufficient for reuse. Supported asset reference syntax/build_request schema is outside read scope, so adapter schema setup remains UNVERIFIED until implementation inspection.
- _speech L413-422 and _send L424-435 dispatch caller-preserving core calls. _assets_from_response L214-243 stores binary/data[].b64_json using request/auth, but forwards existing url/content_url without local ownership validation and wraps generic JSON. Output tests must exercise binary, base64, URL, content_url and generic result separately.
- Concrete job call adapters: video IDs GET /v1/videos/{id}, GET /v1/videos/{id}/content, DELETE /v1/videos/{id}; nonvideo GET /gateway/v1/media/jobs/{id}, GET /gateway/v1/media/jobs/{id}/content, POST /gateway/v1/media/jobs/{id}/cancel. _poll/_await/_job_assets/job L454-503 can automatically retrieve content after successful poll, including post-cancel. Poll denied must stop content; content denied must not emit any asset link; cancel denied stops later calls; allowed cancel does not waive independent poll/content auth.
- _normalize_job L264-273 and _await L479 propagate errors. tool_result L506-519 repeats output as JSON text, structuredContent and resource_link.uri. Scan all three. call_tool L522-531 forwards CallError message/details, while generic exception at L530 prints raw str(exc). Test both failure branches and exception-producing dependencies, previous_attempts/progress fields, not only provider HTTP error.
- Auth unit fixture may make authenticate return explicit 401/403 responses and assert zero catalog/core/read/store calls; production revoked-auth exact status remains to be pinned. Verify resource-metadata challenge header only on 401 according to this adapter's supported auth response contract.
- A recording McpDeps plus real-bound-deps fixture are both necessary. Recorder asserts exact request/auth objects, verb/path/body and ordering; real-bound tests establish policy. Mocking read_asset to deny does NOT test the production reader.

### receipt_store.py
Source: https://github.com/tnunamak/azizam/blob/81ce3204c454658383e0b82d8e42920ff6d885d3/gateway/receipt_store.py
- _IMMUTABLE_ACCEPTED_FIELDS L65-73 and finalize L287-340 preserve accepted attribution/source fields; _coerce_to_internal_dict L177-188 normalizes owner key; _assert_safe L191-204 says its structural guard is not complete sanitization.
- _filter_for_audience/_with_source L343-368 passes source_label, api_key_name, client_hint to imported serializer. get has no actor ACL parameter. recent L515 onward combines App/hash matches with OR; explicit empty filters return no rows unless include_null_app_id is requested; both omitted are unscoped. These primitives need authorized adapter inputs, not assumed audience-based ACL.
- record L392-428 uses INSERT OR REPLACE, distinct from begin/finalize immutability. Test public callers cannot replace B's request_id with A attribution; scope authorization must precede record replacement.
- Test record, begin/finalize, update_cost, get/recent plus all intended audience serializers. Separate private value leak assertions from acceptance/rejection of structurally forbidden keys. The full internal storage schema may legitimately hold approved internal fields; the absolute no-canary contract in this report is owner/admin operational responses and logs, not a claim that arbitrary safe internal diagnostic fields are already sanitized by this file.
- Empty scope [],[] must never turn into unfiltered None,None. Colliding profile/api_key_name must not create ownership. Accepted person/App/source values do not rebind on finalize; terminal duplicate writes and submission-time attribution are checked separately from owner projection.

### ownership.py and admin_runtime.py
Sources:
https://github.com/tnunamak/azizam/blob/81ce3204c454658383e0b82d8e42920ff6d885d3/gateway/ownership.py
https://github.com/tnunamak/azizam/blob/81ce3204c454658383e0b82d8e42920ff6d885d3/gateway/admin_runtime.py
- ownership principal kinds app/user/service_account L17-28; read-only credential lookup without revocation filter L31-43; stored_owner_key L46-55 gives explicit App precedence and keeps unmapped hashes opaque; runtime app identity L58-82; readable aliases L85-102 expands one principal plus historical hashes, NOT a human's owned service Apps.
- admin_runtime create_app L333-367 coerces owner_user_id to personal; update_app L381-417 changes only name/profile. Therefore B1 is a real model/migration prerequisite.
- credential activity checks L1595-1614 and runtime identity L1632-1687 reject inactive/non-App/session-App credentials; personal owner-created key alone gains person. Valid auth may update last-used L1654-1667.
- session identity L1690-1721 requires matching person plus session App and returns that App's identity. M1 is prospective design expansion risk, not demonstrated present implementation in these allowed files.
- revocation callback L1313-1337 actual binding effects remain UNVERIFIED. Keep resources/history after revoke and verify inactive authentication separately.
- Add test_owner_read_does_not_initialize_or_mutate_store (missing file remains absent), test_alias_set_includes_revoked_hash_not_other_app, test_unmapped_hash_no_role_reassignment, test_session_rejects_foreign_non_session_app. Trigger upgrade must replace an old unsafe definition on existing DB, not merely CREATE TRIGGER IF NOT EXISTS. No App ownership trigger body was inspected.

### media_jobs.py
Source: https://github.com/tnunamak/azizam/blob/81ce3204c454658383e0b82d8e42920ff6d885d3/gateway/media_jobs.py
- get/get_by_request_id/recent L731-843 are actor-agnostic; recent optional App/hash scopes union. update_status/request_cancel L851-957 likewise depend on calling authorization.
- Queued cancel settles canceled; running cancel records cancel_requested_at while retaining running state; terminal cancel raises. Test exact current internal state transitions, while public response statuses remain UNVERIFIED.
- events L1066-1136 may repair and commit missing per-job sequences at L1105-1112. Deny BEFORE store call, not after serializing or filtering.
- audience fields L109-144/L269-287 are serialization, not authorization; client event projection L1142-1167 removes global id and allows progress/step/total/phase/status. Target removes admin event/job audience bypass.
- append validation L1048-1063 permits client note/progress but denies lifecycle types/top-level phase/status. Add invalid payload/client source/lifecycle tests independent of actor role.
- event sequence trigger L376-382 already uses NEW.sequence IS NULL. M2 does NOT allege that this particular trigger is defective; the App ownership trigger remains uninspected.
- Idempotent job creation L563-566 cannot prove replay with missing legacy scope/digest. Different owner_scope/input must conflict without leaking or replacing existing job; same canonical live replacement principal/exact digest may replay.
[v3] - MediaJob has app_id/owner_key_hash/owner_scope but no person_id. Service-App fixtures need no person: Own comes from the App principal. Self-based fixtures must supply person_id through an authoritative resolver, not invent a constructor field.

### Suggested attribution conflict fixture expansion
Use job-backed and cache-only items with: own receipt/foreign job; foreign receipt/own job; missing request receipt/present parent receipt; conflicting first and parent receipt; embedded cache receipt contradicting sidecar; maker-only metadata; no ownership fields; resolvable revoked alias; orphan unmapped hash; shared exact parent and separate poster; multi-owner same bytes; physically retained but logically deleted-by-A. Assert final policy comes from each typed resource's documented authority and never broad owner scope. Unresolved attribution conflicts fail closed rather than selecting whichever identifier benefits the caller.

## Completion statement
This is a complete proposed actor x resource x path test design within the permitted evidence boundary. UNVERIFIED entries are explicitly release-blocking where necessary; none are represented as passing. No tests were executed or counted, no implementation was changed, and no member privacy notice readiness is claimed.

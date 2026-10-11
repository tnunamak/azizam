# UX batch 1: names, completion notices, account recovery

Status: design draft, 2026-10-11. **Not approved for implementation.**
Source baseline: release 17, commit `0061d150`. File:line references below
refer to that commit. This document proposes behavior; it does not change it.

## Handoff at the quota stop

The three supplied research reports were read in full. Current Try, model-name,
Library, notice and recovery paths were inspected. Recovery evidence received a
separate read-only source pass; the author checked its principal claims and
corrected its incomplete reading of the reset dialog (which already shows expiry).
The design below records decisions, contracts, copy, migration rules and test
oracles. **No product code, live services, deployment data or credentials changed.**

Stopped before independent design red-team, full citation/link validation, prose
lint and visual/usability validation. All proposed behavior tests are UNVERIFIED;
none were executed. Claude should first review the cross-design contracts against
P1–P22, especially preset identity, notification races and sole-owner recovery,
then resolve findings in this file. Review must precede implementation (P20).
No new research or implementation is needed merely to finish that review.

## Evidence and common rules

Read with [PRINCIPLES.md](PRINCIPLES.md), obtained from
`docs/owner-principles:docs/design/PRINCIPLES.md`, [VOICE.md](VOICE.md),
[HEADLESS_AND_RESOURCES.md](HEADLESS_AND_RESOURCES.md), obtained from
`design/headless-resources:docs/design/HEADLESS_AND_RESOURCES.md`,
[ENGINE_KNOWLEDGE_BOUNDARY.md](ENGINE_KNOWLEDGE_BOUNDARY.md),
[CATEGORIES_AND_PLANS.md](CATEGORIES_AND_PLANS.md),
[PROMPT_MEMORY.md](PROMPT_MEMORY.md) and [DESIGN.md](DESIGN.md).
The headless and categories documents contain proposed contracts, not proof
that those contracts are implemented.

Research references used throughout:

- **M:** `~/Documents/azizam/ux-findings/research-results/azizam-research-model-naming-ia-20261011.md`.
- **N:** `~/Documents/azizam/ux-findings/research-results/azizam-research-notifications-2026-20261011.md`.
- **R:** `~/Documents/azizam/ux-findings/research-results/azizam-research-self-hosted-password-reset-20261011.md`.

These are supplied documentary surveys, not observed competitor interactions or
Azizam usability tests. Section references distinguish their evidence from our
choices. Timer lengths, default labels and API additions below are design choices.
Use existing visual tokens and member shell. Do not rename Try to Create in this
batch. Phone acceptance width is 390 px, with keyboard and screen-reader checks.

All proposed adapters call one server core for identity, authorization, resolution,
state changes and audience filtering. Publish supported HTTP operations in OpenAPI;
use the existing error envelope `{error:{code,message}}`, scoped opaque cursors
and epoch-second timestamps. Cookie mutations require the applicable CSRF/origin
protection. CLI calls use an explicitly selected identity, never an ambient owner
key. Runtime App keys do not gain account-management rights. A new general
operation registry or one MCP tool per endpoint is out of scope.

## 1. Model naming

### Problem and current behavior

Owner: “Why are there different model names? Model ACE-Step 1.5 XL Turbo …
yue2-song … audio.generate.yue2-song” and “is it confusing how models are mixed
with model aliases [in Try]”.

Try fetches `/me/api/models/catalog` and `/me/api/workflows` separately
(`web/src/app/try/page.tsx:1354`). `groupCatalog` concatenates aliases and models,
keeps alias rows first, and collapses targets named by alias `use_names`
(`:644`, `:658`, `:702`). Duplicate labels gain provider and prettified identifier
text (`:710`). Workflows have a separate option builder (`:745`), but music and
sound-effect capability links lead to the workflow tab (`:279`). The picker is
labelled “Model”, including its loading state (`:2880`). It already tracks
loading, ready, empty and error (`:1423`); do not claim loading is absent.

The catalog returns `aliases` and `models`, with caller policy applied before
building rows (`gateway/models_catalog.py:134`, `:199`). Models combines these
for lookup, leads with aliases/secondary profiles and offers an expanded exact
model list (`web/src/app/models/models-client.tsx:946`, `:981`, `:998`, `:1018`).
Workflows reads declared inputs and outputs and renders “Needs” and “Makes”
(`web/src/app/workflows/page.tsx:251`, `:281`). These are useful distinctions to
retain rather than flatten.

Release 17's `modelDisplayName` first matches the exact catalog ID, then a workflow
reference, then capability metadata; importantly, capability fallback matches only
`use_name` or `model`, **not pooled `use_names`** (`web/src/lib/model-name.ts:90`).
Grouped names indicate a shared capability, not common model identity. The helper
also shortens file paths and makes IDs readable (`:26`, `:44`, `:59`). Preserve
that fix; formatting an identifier is not evidence that two things are identical.

Library chooses `resolved_model`, then `requested_model`, then the asset model,
and resolves a display name using current catalog data
(`web/src/app/library/page.tsx:1490`, `:2189`). It displays “Model” in facts and
“Model name” in Details (`:1756`, `:1827`). Receipts already have requested/resolved
model and workflow fields, manifest version/hash, parent request and fallback
reasons (`gateway/receipts.py:238`). That is useful provenance, but it does not
establish an immutable historical display-name or complete execution-stage
snapshot. The owner's particular three names cannot be proven to describe the
same execution without deployment evidence: **UNVERIFIED**, deliberately not
queried here.

### Decision and alternatives

Separate task, preset, model, workflow, voice and accepted API name. Use task-first
Try with a task-specific preset control; concrete models remain directly selectable
through “Choose a model”. A workflow is selected as a workflow, never presented as
a model because its API happens to accept a `model` field. A result gets one title
and one Details disclosure containing selection and execution evidence.

Grounding: M §2 Together separates readable names from API strings; OpenRouter
separates requested routing from selected models; M §3 ElevenLabs separates voice
and model, and Home Assistant names a composition independently of its stages.
M §1 Gemini and §5D support per-result details. These support the distinction,
not a claim that our exact layout has passed a usability test.

Reject a flat alias/model/workflow list because it implies equal semantics.
Reject replacing every name with an API ID because it moves builder detail into
member decisions. Reject hiding model choice entirely because builders need
explicit selection and receipts. Reject treating every alias as a preset: a pure
alternate spelling adds no separate saved behavior. Reject inferred equivalence
from a common capability, a prettified string or today's route configuration.

### Entities and headless contract first

| Entity | Meaning and name | Identity and relation |
|---|---|---|
| Task | What a person wants to do; label “Task” in Try | Existing capability key with an operator-editable display label. A task is not a runnable model. |
| Preset | Saved task-specific selection/settings; label “Preset” | Stable ID plus immutable revision; fixed target, moving default or declared routing policy. Reuse the service revision/dependency closure in Categories and Plans; do not create a second permission object. |
| Alias | Another accepted API spelling; builder-only term | Accepted string maps to a typed target. A pure synonym gets no picker row. A saved configuration behind an alias becomes a preset, with the alias pointing to it. |
| Model | The AI model or evidenced release | Stable identity, display name and optional revision/digest/packing metadata; multiple deployments do not imply different weights. Unknown release stays unknown. |
| Workflow | Configured process with inputs/outputs | Workflow ID and revision, declared stages and model references; one or several stages. |
| Voice | Selected speech identity | Voice ID/revision and readable name, filtered by declared compatibility; never parsed from a model ID. |
| API name | Exact accepted request string | UI label remains **Model name** under “Use in an app”, per VOICE. In a workflow example use **Workflow ID**. `api_name` is a contract field, not a member label. |
| Creation / Receipt | Output / record of a request | Existing asset/request IDs plus a historical selection snapshot and execution evidence. |

All engine/model/workflow/voice names and recommended defaults come from owner
editable registry/manifest data. Generic entity kinds and capability semantics
can be code. No named-engine branches or brand-aware parsers (P12).

Extend the existing catalog projection with `schema_version: 2`, typed `presets`,
`models`, `workflows`, `voices`, `api_names` and task defaults. Keep workflow payloads
in `/me/api/workflows`; catalog workflow references link to those same objects.
Each selectable row has `id`, `revision`, `display_name`, `task_ids`, `availability`,
`use` (method/path/body or multipart schema), and permission-safe relationship
references. A preset also has `resolution_mode`, target references, effective
settings and an allowed-change summary. `api_names` maps accepted names to
`{target_kind,target_id}`; it is not a list of extra UI options.

`availability` separates eligibility, readiness and lifecycle, with stable reason
codes and dated evidence. Preserve the established Ready/Busy/Degraded/Down/
Blocked/Unknown/Off presentation in DESIGN.md; loading catalog data is a separate
UI state. Omit undiscoverable targets before pagination. Admission rechecks
permissions and availability on execution; seeing a row never reserves capacity.

Existing run routes accept existing API strings. Native `run` gains an optional
`selection:{kind,id,revision}` envelope resolved by the same core. Mutually
inconsistent explicit selection and API name return `400 selection_conflict`.
A stale explicit revision returns `409 selection_changed`, not a silently updated
run. Parameter precedence is explicit allowed request overrides, preset defaults,
then target defaults; locked settings reject overrides. Fallback remains inside
the authorized dependency closure and consented cost/data destination boundaries.

Extend the existing receipt read with:

```text
selection: {kind, id, revision, display_name_at_request, requested_api_name}
execution: {resolution_reason, stages:[
  {stage_id, display_name, model_id, model_name_at_run, model_revision,
   workflow_revision, voice_id, provider_or_engine_ref, remote,
   evidence_kind, observed_at, output_ids}
]}
```

Evidence kind is `gateway_observed`, `provider_reported`, `configured` or `unknown`.
A returned model string is not automatically a verified weights digest. Do not
infer identity from generated text. Route identifiers and host paths receive
server-side audience filtering; a UI scrub is insufficient. Keep existing
OpenAI-compatible response `model` semantics until compatibility tests establish
that changing them is safe; receipt extensions carry the richer record.

CLI: `azizam capabilities --task <key> --json`, `azizam run --selection <kind:id@rev>`
and `azizam receipts get <id> --json` are additions to the proposed headless CLI,
not existing commands. MCP keeps `capabilities`, `run`, `job`; extend their existing
results with typed choices and receipt references. No new MCP tool is required.
The same accepted API names must work from generated examples and direct clients.

### Screens, exact copy and states

Try retains its existing shell and task navigation. For the current task, show
“Preset” with the declared default, then “Choose a model” and “Choose a workflow”
only where those alternatives exist. Each opens a separate labelled list, never a
mixed select. Explicit model mode says “Model”; workflow mode says “Workflow”.
“Use a preset” returns to presets. For speech, “Voice” is a separate control.
One available option appears as a named value with “Details”; it is not an empty
selector. Do not auto-switch to a paid or remote option when a choice disappears.

Rows have one display name and only decision-relevant cost/locality/readiness.
A routing preset says “Chooses from models your access plan allows.” A fixed preset
says “Uses {model}.” In expanded details, disclose permitted fallback destinations
before submitting. Use existing paid-run confirmation; no preset grants consent.
Search matches names and accepted API strings but returns each typed entity once.
An alias match can show “Matched model name” inside the search result's details.
Display-name collisions require an owner-authored qualifier; IDs stay in Details.

Library's primary title stays its user/content title, subject to prompt-memory
policy; otherwise use “{creation kind}, {date and time}”. Do not add three engine
names below it. The single “Details” disclosure contains “Selected”, “Model used”,
“Workflow”, “Voice”, “Where it ran”, “Cost” and measured timing, omitting inapplicable
rows. A multi-stage run has a stage list with output relationships, not one invented
model. “Technical details” within it holds exact API name, request ID, revisions
and evidence source. A meaningful deviation surfaces “A different model was used”
with a Details link; the disclosure explains why. Missing evidence reads “Model
not recorded” or “Version not reported”, never today's default.

Models shows concrete models first, with a distinct “Presets” section for saved
choices. Model detail has “Use in an app” with copyable **Model name** and request
example, supported tasks and observed availability. Workflows shows readable
workflow name, “Needs”, “Makes”, availability and “Try workflow”; exact workflow
ID/revision and component models live in Details. The same typed identity drives
links between pages. Do not relabel a workflow as a model on Models.

| State | Exact copy / behavior |
|---|---|
| Loading choices | “Loading presets…” / “Loading models…” / “Loading workflows…” / “Loading voices…”; disabled selection, `aria-busy`, retain draft inputs. |
| Successful empty list | “No presets are available for this task.” Offer “Choose a model” only if available; otherwise “Ask the owner to make this task available.” |
| Failed catalog | “The gateway couldn’t load your choices.” Action “Try again”; do not replace a failure with an empty state. |
| Selection removed | “This choice is no longer available.” Action “Choose another”; preserve input, require a new choice. |
| Cold model | “Loads when you start.” Only when declared startable; no invented ETA. Actual loading says “Loading model…”. |
| Access blocked | “Your access plan doesn’t include this.” Show only for a target the person may discover. |
| Incompatible voice/input | “This voice isn’t available with this model.” or a server-declared input reason; require a choice, never substitute silently. |
| Details loading/failure | “Loading details…” / “The gateway couldn’t load these details.” Action “Try again”. The creation remains usable. |
| Missing historical identity | “Model not recorded.” Preserve recorded identifiers in permitted technical details without asserting resolution. |

### Data and migration

Build an inventory of accepted API strings and historical receipt fields before
migration. Map synonyms into `api_names`; map saved behavior into presets; retain
model/workflow identity separately. Keep current accepted names operational through
this normal mapping, with no parallel old catalog or “legacy” section. An owner
can later retire an accepted name only after an impact preview. A display rename
alone never changes routing or client strings (P11, P19).

Backfill display names only from historical evidence that actually identifies the
record. Uncertain mappings remain `unknown`; retain raw historical identifiers as
source evidence. Never relabel a past run with today's preset target. Snapshot
new selections and actual stages at execution, including fallback attempts. Keep
packing/version differences in model metadata, not discarded as harmless cosmetic
suffixes. Freeze historical display labels; current catalog labels describe current
objects. Changing a preset target creates a new reviewed revision and follows the
existing access-plan grant rules.

Migration must be idempotent and preserve receipt IDs, counts, timestamps, asset
links, ownership, amounts and accepted API behavior. Preview unresolved mappings
for the owner. Remove replaced UI projection code after cutover; do not keep two
resolvers. Retain release-17 path privacy and pooled-name regression tests until
equivalent server-backed tests replace them.

### Oracle tests

| Level | Fixture and required result |
|---|---|
| Component | Alias, preset and concrete model share a task: preset picker shows only presets; direct picker shows models once. Two unrelated music workflows in pooled `use_names` never acquire the recommended model's name. Test loading, empty, fetch error, removed selection and duplicate display names. |
| API | Resolve every accepted name before/after migration and compare target/settings/access decisions. An unauthorized candidate cannot appear or run through a preset. Old receipt is byte-stable in historical fields after a preset rename/retarget; unknown provenance stays unknown. Member payload contains no host paths. |
| API / adapters | Equivalent HTTP, CLI and MCP requests produce the same selected revision and receipt; conflicting or stale selection fails before dispatch. Explicit direct-model selection cannot silently become preset routing. |
| Browser | At 390 px, choose a task, generate using a fixture, open Library, identify selected versus actual model, copy the usable API name and run the generated example against an isolated server. Repeat for voice, workflow and fallback. Keyboard focus and draft survive errors. |
| Migration | Run twice against copied fixture databases; compare identities, ownership, receipt counts, costs and output links. Unknown mappings are reported, not guessed or deleted. |

### Owner decisions and risks

1. **Default preset vocabulary:** recommended default is preserve owner-authored names
   and require a description where behavior is unclear; do not seed “Best” without
   evidence. Whether households prefer “Preset” needs a phone comprehension test.
2. **Historical renames:** recommended default is freeze labels at request time;
   show the current object's name only after following its catalog link.
3. **Direct model control:** recommended default is one disclosure from Try, with
   the same access-plan limits as presets. No new member permission bypass.

## 2. Completion notifications

### Problem and current behavior

Owner: “Picture is ready Open result seems persistent even after I follow the
link. What is the 2026 best practice?”

Release 17 already calls `onDismiss` when Open result is activated
(`web/src/components/jobs/job-notices.tsx:237`). Library viewer mount marks the
asset opened (`web/src/app/library/page.tsx:1480`). The opened-asset helper uses
person-scoped `sessionStorage` and a same-window event
(`web/src/components/jobs/opened-creations.ts:1`). Dismissed job IDs use another
session store, capped at 200 (`job-notices.tsx:185`). This is not account-level
read state or cross-device synchronization.

The observer polls `/me/api/jobs?watch=1`, paginates before advancing its watermark,
and backs off (`:144`). It suppresses canceled jobs and completions predating the
session unless they changed during it (`:113`). Notices form an accumulating list
inside a polite live region (`:197`); there is no timer or burst aggregation there.
`jobLink` points to an asset or job detail in Library (`:44`). Do not replace these
stable destinations with signed media URLs.

### Decision and alternatives

Keep close-on-Open. Add **Recent**, a durable account-owned list of terminal work,
with server-owned read state. Toast presentation, read state and record retention
are separate. Keep Library as the home for creations and receipts as request
records; Recent is an index of outcomes, including failures without media.

Grounding: N “Ranked recommendations” 1–7; Google action-dismiss [S2], W3C untimed
alternative [S26], GitHub synced inbox [S9], and Slack/Notion read-versus-clear
[S14/S35]. The survey does not establish a universal toast timer or cross-device
OS tray guarantee. Use an eight-second toast and two-second burst window as
initial testable choices, not a “2026 standard”.

Reject permanently stacked success banners because they cover phone work after
being acted on. Reject toast-only delivery because results must remain findable.
Reject marking read on timeout/dismiss because neither proves viewing. Reject
session storage as authoritative read state. Defer web push and native Live
Activities: in-app completion must work without permission, installation or email.

### Entities and headless contract first

A **Recent entry** is one recipient's record of a job attempt reaching a terminal
revision. Its identity is unique on `(recipient_id, job_id, terminal_revision)`.
Fields: `id`, `job_id`, `receipt_id`, `terminal_revision`, `outcome`, `finished_at`,
`task_label`, safe title, output references, `read_at`, `state_revision`.
Recipient comes from authenticated ownership, never a client-supplied arbitrary
person. Personal work appears for that person; unattributed service-App jobs stay
in App job/receipt history and do not flood the owner's personal Recent. No prompt
text is copied into notice storage. Titles are composed from permitted current
content or kind/date; prompt removal must not leave another retained copy here.

Use proposed `GET /me/api/recent?cursor=...&read=all|unread` with entries,
`next_cursor`, `unread_count`, and a per-person snapshot boundary. Ownership is
applied before pagination/counting. `PUT /me/api/recent/{id}/read` is idempotent;
server sets `read_at`. No mark-unread operation initially, making read monotonic.
`POST /me/api/recent/read-through {through_cursor}` marks only entries in the
server-issued snapshot, so racing completions remain unread. Return updated count
and revision. Wrong-owner IDs have the same not-found response as absent IDs.

UI/CLI use the same reads and mutations. Proposed CLI adds `recent list --json`,
`recent read <id>` and `recent read-through <cursor>` to the headless CLI design.
MCP's existing `job` result exposes `recent_entry_id` and receipt/output links;
list and mark-read operations are initially HTTP/CLI only. No recovery or owner
privilege is added to inference MCP. The support matrix must say this explicitly.

Keep polling initially, using the existing job watch transport where practical.
Add scoped revisions for read changes and reconcile on focus, reconnect and sign-in.
Same-browser tabs coordinate one foreground presenter with an expiring lease and
BroadcastChannel; the server remains authoritative. A per-browser delivered-event
cursor suppresses replay after reload; it carries opaque IDs only and is cleared
on account switch. On a new device, initialize at the current event watermark;
show the unread backlog in Recent, never replay it as fresh toasts. A connected
second device may show the same newly completed event; account-wide exactly-once
visual presentation is not promised. Viewing it clears attention on other devices
when they next reconcile. Offline clients cannot guarantee immediate sync.

### Lifecycle, screens and exact copy

Recent is a named link in the shared member shell, including on phones; it is not
hidden in owner navigation. Use `/recent/` in the static app, through the existing
`/ui/` deployment prefix. The page has “Recent”, “All” / “Unread”, and “Mark all
read”. Rows show safe title, terminal outcome, time, New marker and Open result or
See details. Opening the list does not mark its rows read. Read rows remain in All.

| Event | Toast | Account read state | History/output |
|---|---|---|---|
| Terminal event elsewhere | Show once locally; aggregate bursts | Unread | Retain entry and output separately |
| Relevant terminal result visibly rendered | Suppress/hide | Mark read | Retain |
| Open result activated | Hide immediately | Unchanged until authorized detail renders visibly | Retain even if navigation fails |
| Background-tab open | Hide initiating toast | Unread until destination becomes visible | Retain |
| Close or timeout | Hide | Unchanged | Retain |
| Recent list opens | No backlog toast | Unchanged | Read and unread rows remain |
| Mark all read | Hide matching attention | Read through captured boundary | Retain |
| Another device reads | Hide on reconciliation | Read | Retain |
| Output deleted separately | No new success toast | Unchanged until detail viewed | Show unavailable output and retained receipt |

“Rendered visibly” means the authorized terminal detail and outcome are mounted
in a visible document for the relevant job/attempt. It does not require playing
an audio/video file. A 401, failed detail fetch or hidden tab is not read. A
successfully rendered tombstone (“This file is unavailable”) or failure detail
counts as read because the outcome was shown. Match by job/attempt, not asset ID
alone: identical content can belong to different jobs and people.

One floating toast maximum. Coalesce completions for two seconds, then announce
one summary: “Picture is ready” + “Open result”; “3 creations are ready” + “View
Recent”; mixed batch “2 ready, 1 needs attention” + “View Recent”. Keep individual
entries and links. Failed job: “Your video couldn’t finish” + “See details”. The
persistent detail names the known failing layer and one next action; otherwise
“We couldn’t tell where this stopped.” Canceled jobs appear as “Canceled” in
Recent without a toast by default. Retry opens a run form with cost/consent;
it must not dispatch another paid generation from a notice click.

Additional completions update the current aggregate without resetting its original
eight-second timer indefinitely. After it closes, suppress further popups from
that continuous burst until a quiet interval; Recent still updates. Exact quiet
interval default: two seconds without a terminal event. Count only outcomes
represented in the aggregate; never label failures as ready.

| State | Exact copy / action |
|---|---|
| Initial list load | “Loading Recent…”; skeleton, `aria-busy`, no fake zero badge. |
| Empty All | “Finished work will appear here.” |
| Empty Unread | “You’re up to date.” |
| List error | “The gateway couldn’t load Recent.” / “Try again”. Retain any cached rows with “Last updated {time}”. |
| Read write failed | “The gateway couldn’t save this as read.” / “Try again”. Keep local toast hidden, expose pending state in Recent. |
| Result load failed | “The gateway couldn’t open this result.” / “Try again”. Recent remains the recovery path. |
| File unavailable | “This file is unavailable.” Preserve receipt link; state a more exact cause only when known. |

Use a pre-existing `role=status`, `aria-live=polite`, atomic announcement region;
announce an outcome once per local aggregate, not on every poll. No focus theft.
Pause timeout on pointer interaction, keyboard focus or hidden document. Close
has accessible name “Dismiss notification”. If remote read arrives while a toast
control holds focus, update state immediately but defer removal until focus leaves.
Explicit dismissal restores the prior sensible focus target; navigation focuses
the destination heading. Provide 44 px touch targets, reduced-motion support,
non-color New/state cues, and a durable equivalent of every toast action.

### Data and migration

Persist entries and read state in server storage, with transactional terminal-event
creation or an idempotent durable outbox. A restart between job completion and
projection must not lose the entry. Keep entries indefinitely by default (P19).
An owner retention change requires an affected-count preview; deleting Recent
history never deletes receipts or media. Account closure/privacy erasure is a
separate explicit operation, not toast dismissal.

Backfill terminal jobs with known ownership into the same entry schema. Mark
pre-cutover entries read with `read_source=migration`, explicitly meaning no
historical read evidence was available; do not invent `viewed_at`. New terminal
events are unread. This avoids an upgrade flood while retaining history. Rows
without defensible person attribution stay in their existing App history.

Do not convert session dismissals to account reads. After a safe cutover, remove
both session-scoped acknowledgment stores and their obsolete readers; discard
those transient hints, not valuable job data. Existing sessions initialize their
presentation watermark at cutover. Late events for already viewed results reconcile
against read state. Offline writes are idempotent, keyed by account/event, and
must never replay after logout under a different account. Revoke access before
serving cached private content.

### Oracle tests

| Level | Fixture and required result |
|---|---|
| Component | Fake timer: toast closes after eight active seconds, pauses with focus, click hides immediately, dismiss/timeout does not call read. Ten events including failures produce truthful aggregate and one polite announcement. |
| API | Duplicate/reordered events and crash/retry yield one entry per recipient/job/revision. Read is idempotent and monotonic; mark-all snapshot excludes a racing event. Cross-person reads, counts and cursors reveal nothing. |
| Browser | Two tabs and two isolated browser contexts: viewing one result updates other active unread counts on refresh/poll; reconnect does not re-toast backlog. Background tabs do not mark read. Account switch clears prior content and pending writes. |
| Browser / accessibility | Keyboard, VoiceOver/Safari and TalkBack/Chrome can reach Recent after toast expiry; typing focus stays put during bursts. Remote acknowledgment does not remove a focused control. Denied notification permission changes nothing. |
| Retention/migration | Backfill twice yields equal counts; no output/receipt deletion. Deleted output has a readable tombstone. Two jobs sharing an asset do not acknowledge each other merely because one asset ID matches. |

### Owner decisions and risks

1. **Toast timing:** recommended default eight active seconds, two-second coalescing;
   adjust only from phone/accessibility testing, not competitor folklore.
2. **Retention:** recommended default keep Recent indefinitely, matching valuable
   receipt history; owner-selected expiry requires a deletion preview.
3. **Automation volume:** recommended default no personal Recent for unattributed
   service-App work. Add explicit subscriptions only after a demonstrated need.

## 3. Account recovery

### Problem and current behavior

Owner: “Forgot your password? Ask the person who invited you for a reset link.
< Isn't the usual thing to have a self service link?”

The current sign-in page displays that sentence as text, not a recovery link
(`web/src/app/sign-in/page.tsx:247`). It uses `/auth/api/options` for built-in/OIDC
choices and changes to a reset form when a fragment token is present (`:42`,
`:90`, `:111`). It currently says “Use at least 12 characters” (`:122`).

`POST /admin/api/people/{user_id}/reset` requires the owner role, issues a link and
returns its expiry (`gateway/accounts_api.py:108`, `:236`). Default validity is
one hour (`:68`). The underlying link bounds are one minute to 30 days, and a new
reset link revokes unused predecessors (`gateway/accounts.py:243`, `:265`).
The People dialog already shows a private copy field and relative expiry
(`web/src/app/people/page.tsx:791`, `:830`); cancellation is not offered there.

`POST /auth/api/password-reset` checks origin, throttles, resets, clears the cookie
and returns to sign-in (`gateway/accounts_api.py:200`). The transaction rechecks
the link and disabled/removed state, changes the verifier, requires registered
sessions, deletes the person's sessions and consumes the link
(`gateway/accounts.py:355`). **Browser sessions are already revoked.** App keys
are not rotated (`docs/PEOPLE_AND_ACCOUNTS.md:96`).

Built-in signup does not verify mailbox ownership (`docs/PEOPLE_AND_ACCOUNTS.md:27`).
Email delivery and passkeys are expressly absent/deferred (`:176`). The documented
host recovery route is `generate-admin-key owner-cli`, leading to privileged
access through `/setup` (`docs/FIRST_RUN_SETUP.md:243`, `:265`, `:275`). That is a
broad administration credential, not a narrow password reset. End-to-end recovery
of the existing sole owner through that route is **UNVERIFIED** here; no command
was run and no credential generated.

### Decision and alternatives

Always offer a real “Forgot password?” entry point for built-in sign-in. Its page
is capability-aware: email self-service only with verified recovery addresses and
working recovery delivery; otherwise owner-assisted reset with honest copy. Keep
external identity recovery with the configured provider. Deliver a documented,
tested, mail-independent sole-owner console route before calling recovery complete.

Grounding: R “Answer”, “Recovery entry-point states” and ranked recommendations
1–6; Gitea explicitly gates reset on mail, Nextcloud requires functioning delivery,
and Home Assistant/Immich retain administrator recovery. R “Security baseline”
and “Proposed security and operator contract” support non-enumeration, restricted
proof, atomic redemption and session revocation. These are design requirements,
not an assertion of security certification.

Reject a fake Send email action without delivery. Reject inviter-only wording:
the inviter may not manage accounts and the sole owner cannot ask themselves.
Reject publishing reset links or temporary passwords in an unauthenticated
response. Reject account recreation/database deletion because ownership, history
and access plans must survive. Defer magic-link sign-in and new passkeys; neither
solves the immediate no-mail recovery problem. Do not infer passkey support from
OIDC support or from a browser password manager.

### Entities and headless contract first

**Recovery options** describe installation capabilities without an account lookup.
**Recovery address** is a private verified delivery destination tied to a person,
not merely the account's email string. **Reset link** grants only replacement of
that person's password, once, before expiry. **Owner recovery** requires host
control or another explicitly supported strong method; it is not ordinary member
self-service. Keep invitation, reset link, browser session and App key distinct.

Extend `GET /auth/api/options` with:

```text
recovery: {
  password_reset: true|false,
  email_delivery: ready|off|unavailable|unknown,
  owner_assisted: true|false,
  owner_help_url: <same-origin static runbook>,
  external: [{label, sign_in_url, recovery_help_url}],
  passkey_sign_in: false
}
```

No email, account existence, role or individual eligibility is returned publicly.
`ready` requires enabled recovery delivery, a validated public reset origin and a
successful delivery test, with no known current transport failure. Configuration
alone is insufficient; successful handoff still cannot promise inbox arrival.
Expose dated diagnostics to owners, not mail jargon to members. Public options
can report a global outage without reporting recipient-specific failures.

Proposed `POST /auth/api/password-reset/request {email}` returns the same `202`
shape and copy for existing, absent, unverified, external-only or ineligible
accounts when global recovery delivery is ready. Only a local, eligible person
with a verified recovery address receives a message. Use timing-safe queued work,
per-address and per-origin/IP abuse controls; do not lock accounts on requests.
A known global outage returns `503 recovery_delivery_unavailable` uniformly.
No fallback ever reveals a link to the anonymous requester.

Keep `POST /auth/api/password-reset {token,password}` as the redemption core.
Unify mail and owner handover issuance around the existing scoped link model.
Proposed reset default: 30 minutes, maximum 24 hours, independent of invitation
lifetimes. Opening/previewing a link does not consume it. A successful reset
atomically consumes proof, revokes outstanding reset proofs and all browser
sessions, and records an audit event; it does not create a signed-in session,
enable a disabled person, change a role/access plan or cancel work they own.
Return to normal sign-in. Password strength policy changes are a separate review;
this design preserves the current advertised minimum rather than silently changing
it during recovery work.

Owner-assisted issuance keeps the existing owner-only route; require recent owner
authentication and an explicit target. Return a non-secret `link_id` alongside the
one-time URL and expiry; add `DELETE /admin/api/people/{id}/reset-links/{link_id}`
for cancellation through the same authorization. The target must be an eligible
built-in account. Do not widen this permission to admins as a side effect. CLI
uses the same endpoint under an explicitly selected owner credential and supports
private output to a mode-0600 file rather than logging the bearer link.

For email setup, proposed owner API reads/updates recovery-delivery settings with
revision preconditions and offers a test delivery with a confirmation challenge.
Authenticated members can add/change a recovery address only after fresh proof;
`POST /me/api/recovery-address/verification` starts verification and
`POST /me/api/recovery-address/verify {token}` completes it. Pending addresses
cannot receive resets. Keep a previous verified address until replacement is
verified; notify it of the change. Clear verification on any unverified address
edit. Shared recovery mailboxes are not silently treated as distinct people:
require a unique recovery address in the first implementation and retain assisted
recovery for members who share an inbox.

Proposed host-only command `azizam account recover-owner --person <id>` requires
local deployment authority, resolves the configured real database and displays
its non-secret path and target before confirmation. It issues a short-lived,
reset-only proof through the same core; it must not require a functioning mail
service or existing browser login. Use private interactive output, or explicit
protected-file output for automation; never command-line password arguments.
An OIDC-only sole owner needs a distinct, explicit local recovery-authenticator
binding to the **existing person ID**, not a new owner account or changed external
password. The runbook must distinguish these cases and audit the host-authorized
binding. This is proposed, not an existing command. Retain documented host admin
recovery until its replacement passes the deployment tests; remove obsolete
instructions only after cutover.

HTTP and CLI support recovery; inference MCP explicitly does not. No account-reset
operation belongs on an agent's ordinary inference connection. A future dedicated
owner MCP is outside this batch. Public recovery options remain readable headlessly.

### Exact UI copy and states

“Forgot password?” sits beside the built-in password form and links to
`/sign-in/recover/` under the app base path. Mixed sign-in offers the configured
provider as a separate method; never reveal “that email uses provider X”. All
recovery screens have “Back to sign in”. No-mail recovery requires no email entry.

| State | Exact copy / action |
|---|---|
| Options loading | “Checking recovery options…” |
| Options unavailable | “The gateway couldn’t load recovery options.” / “Try again”; keep a static “Owner recovery instructions” link. |
| No mail | Title “Reset your password”. Body “This Azizam doesn’t send password reset emails. Ask the person who manages it for a reset link.” Link “Are you the owner?” |
| Email ready | “Enter your recovery email address.” Field “Email address”; action “Send reset link”; busy “Sending request…”. |
| Request accepted | “If this address can recover an account here, we’ll send a reset link.” Do not say a message was definitely sent. |
| Delivery outage | “Password reset email is unavailable. Ask the person who manages this Azizam for a reset link.” |
| Rate limit | “Too many recovery requests. Try again later.” Use server retry timing accessibly when available. |
| Provider-only | “Use {provider} to recover your sign-in.” / “Continue with {provider}”; name comes from trusted configuration. |
| Owner help | Title “Recover owner access”. “Use the server recovery instructions if you can access the computer that runs this Azizam.” / “Open instructions”. No claim that saved codes exist. |
| Link redemption | Title “Choose a new password”; fields “New password”, “Confirm new password”; action “Save new password”; busy “Saving password…”. Allow paste and password managers. |
| Invalid/expired/used proof | “This reset link no longer works.” / “Get a new link”. Do not diagnose which secret check failed. |
| Success | “Your password has changed. Sign in with your new password.” Add “Your other browser sessions have ended.” only after revocation commits. |

Owner handover dialog: “Create a reset link for {name}?” Body “Confirm you’re
helping the right person. Anyone with this link can choose a new password for
this account.” Action “Create link”. After creation: “Share this link privately
with {name}.” Actions “Copy link”, “Cancel link”. Show expiry under Details to
respect P15; never put technical link lifetime copy in the ordinary sign-in flow.
An expired copy field becomes disabled with “Create a new link”. Copy failure:
“Couldn’t copy the link. Select it to copy.” Cancel failure preserves the warning
that the link remains usable and offers “Try again”.

For ordinary forgotten-password recovery, retain App keys and, if introduced later,
independently enrolled passkeys. State their treatment in Account → Security:
“Resetting your password ends browser sessions. App keys keep working.” A separate
compromise path must explicitly review/revoke keys and authenticators; do not
promise password reset stops all spending. No MFA bypass is implied. There are
no current native passkeys, so show no passkey button now. If added later, a working
“Sign in with a passkey” is offered before reset, with tested RP-ID/origin and
multiple-credential support; passwordless sign-in and MFA are distinct.

### Data and migration

Reuse the person ID and link/audit/session records. Add recovery-address
verification fields and delivery capability data; existing email strings start
**unverified**. Never bulk mark them verified from invitation acceptance. Bind
verification proof to person, normalized address, purpose and expiry. OIDC claims
may establish an external sign-in fact, but do not silently turn an external-only
identity into a local password account or grant recovery authority by email match.

Retain receipts, creations, ownership, plans and active jobs. Preserve existing
reset links until their recorded expiry unless the owner explicitly revokes them;
apply the shorter policy to new links. Remove replaced copy and routes after
cutover, mapping existing fragment links through the same redemption core rather
than introducing a second password reset implementation (P11/P19).

Reset tokens stay hashed at rest, redacted from logs/telemetry, and absent from
analytics/referrers. Continue fragment transport for browser links, clear sensitive
URL state after capture, and test preview/scanner behavior. Mail failures must not
cause an insecure development fallback. Record recovery events without passwords
or raw proofs; notify a pre-established verified channel when available and show
an account security event at next sign-in on mail-free installations. The latter
is not equivalent to out-of-band breach notification.

### Oracle tests and sole-owner release gate

| Level | Fixture and required result |
|---|---|
| Component | Ready/off/outage/unknown email, built-in/OIDC/mixed and no-passkey fixtures produce only supported actions. Every wait/error is distinct; reset proof never appears in error text or analytics. Password managers and paste work. |
| API | Existing/nonexistent/unverified/disabled/external identities have indistinguishable public request responses and no unauthorized mail. Unknown/off transport never exposes a reset proof. Expired, replayed, wrong-purpose and concurrent redemption fail safely; exactly one concurrent redemption changes the password. |
| API / authorization | Members, inference keys and admin-without-owner role cannot issue owner reset links. Missing freshness/CSRF fails without side effects. Reset after account disable cannot reactivate it. New issuance revokes the prior proof; cancellation invalidates only its target link. |
| Browser | Request via mail-sink fixture, open link on a second device, choose password and sign in. Old password fails and every pre-reset browser session is rejected. App keys retain precisely their prior authority. Repeat no-mail assisted path and outage fallback at 390 px. |
| Deployment / sole owner | With a disposable copy of each supported installation, sole-owner browser sessions absent and mail unavailable: follow only the shipped runbook, recover the existing local owner, then repeat for OIDC outage. Verify same person ID, same roles/plans/assets/receipts, revoked browser sessions and preserved App-key policy. No fresh database, SQL editing, new recovery port or surviving broad emergency key. Restart the fixture and repeat login. |

The last row is a release blocker, not a test completed by this design task.
UNVERIFIED: actual deployment-path discovery, OIDC-only binding implementation,
cryptographic relationships between passwords and stored content, and complete
session rejection across every authentication adapter. Implementation must prove
these against disposable real-backend fixtures; do not use the live gateway.

### Owner decisions and risks

1. **Shared recovery inboxes:** recommended default one verified address per person;
   households sharing an inbox retain assisted recovery until a non-enumerating,
   explicit account-selection design is reviewed.
2. **Reset scope:** recommended default revoke all browser sessions and reset proofs,
   retain App keys and future independent passkeys for ordinary forgotten passwords;
   provide separate compromise containment. This leaves stolen App keys usable until
   explicitly revoked and must be stated plainly.
3. **Owner recovery:** recommended default host-authorized narrow recovery for the
   existing owner, including explicit local-authenticator binding during OIDC outage.
   No public contact details or saved-code promise until those features exist.

## Completion evidence required before implementation

An independent red-team must check this draft against the supplied research and
current code, with a finding/resolution record linked here. Then validate catalog
migration on fixtures, notification state/race oracles, and sole-owner recovery in
each supported deployment. Run phone and assistive-technology journeys against
an isolated implementation. Confidence is high in the three conceptual decisions,
moderate in the proposed adapter/data shapes, and **UNVERIFIED** for implementation,
security behavior and usability. The quota stop prevented those later checks.

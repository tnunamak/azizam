# UX batch 1: names and voices, completion notices, account recovery, prompt saving

Status: design revision r1, 2026-10-11. **Not approved for implementation.**
r0 (`ccb42202`) was red-teamed; every finding is resolved below (table at the
end). Source baseline: release 17, commit `0061d150`. File:line references are
to that commit. This document proposes behavior; it changes nothing.

## Evidence and common rules

Read with [PRINCIPLES.md](PRINCIPLES.md) (from `docs/owner-principles`),
[VOICE.md](VOICE.md), [PROMPT_MEMORY.md](PROMPT_MEMORY.md),
[ENGINE_KNOWLEDGE_BOUNDARY.md](ENGINE_KNOWLEDGE_BOUNDARY.md),
[CATEGORIES_AND_PLANS.md](CATEGORIES_AND_PLANS.md),
`design/headless-resources:docs/design/HEADLESS_AND_RESOURCES.md` and
`design/connect-root-mail:docs/design/CONNECT_ROOT_MAIL.md` (**CRM**). The two
branch documents are proposals, not proof of implemented contracts.

Research (all `~/Documents/azizam/ux-findings/research-results/`, 2026-10-11):

- **M** `azizam-research-model-naming-ia-20261011.md`
- **N** `azizam-research-notifications-2026-20261011.md`
- **R** `azizam-research-self-hosted-password-reset-20261011.md`
- **PM** `azizam-research-private-mode-framing-20261011.md`
- **RT** `azizam-redteam-ux-batch1-ccb4220-r0.md` (red-team of r0)

These are documentary surveys, not observed usability tests. Timer lengths and
default labels below are design choices to test, not industry standards.

**One core, headless first (P21).** Every action below is one server rule. UI,
HTTP, CLI and MCP are adapters over it, with the same authorization, validation,
outcomes and errors. HTTP operations go into OpenAPI with the existing error
envelope `{error:{code,message}}`, opaque cursors and epoch-second times. Cookie
mutations need CSRF/origin checks. CLI calls use an explicitly selected identity.
App keys never gain account-management rights. The support matrix is at the end.

**What survives a rename (P11).** Azizam is unreleased. Only external contracts
are kept. Everything Azizam owns moves to the new shape at cutover, with no
alias, shim, redirect or frozen label.

| Name or format | Owner | Rule at cutover |
|---|---|---|
| OpenAI, Anthropic, Ollama, A1111 request/response shapes, including response `model` and Responses `store:false` | External | Keep, through adapter data |
| OpenAI voice names (`alloy` … `verse`) sent to `/v1/audio/speech` | External | Keep as accepted spellings in each engine's `voice_map` (§1.3) |
| `/v1/audio/voices` `id`/`name` fields that Open WebUI reads | External, de facto | Keep the field shape; content comes from voice data |
| MCP tool names `capabilities`, `run`, `job` | Azizam, in client configs | Unchanged by this batch |
| Model names the owner authored (`image`, `tts-quality`) | Owner data | Current data. A rename is a data edit with no alias |
| Capability/route strings never authored as model names (`audio.generate.yue2-song`) | Azizam | Not accepted as model names; shown only in Technical details |
| Old voice aliases (`cheerful_female`, `neutral_female` …), the `voxtral-voices` transform id, `voice_transform` | Azizam | Removed |
| Prompt extension `mode:"forget"` | Azizam | Replaced by `save:false` (§4) |
| `/ui/...` pages and `/ui/sign-in/#reset=` links | Azizam | Replaced by root paths (CRM §2). Old links stop working |
| Session-storage notice stores (`opened-creations`, dismissed job IDs) | Azizam | Removed |

Records are never deleted to modernize them (P19). Receipt and job IDs, raw
recorded strings, ownership, money and media links stay. Only their
presentation maps to current concepts.

**Cutover preview.** Before cutover the core lists every accepted model name and
voice spelling that will stop working, with the Apps that sent each one in the
last 30 days of receipts. The owner updates those Apps and approves. Whether
receipts record the requested voice today is **UNVERIFIED**; if not, the
preview says "Voice use not recorded" for voices.

## 1. Model and voice naming

### 1.1 Problem and current behavior

Owner: "Why are there different model names? Model ACE-Step 1.5 XL Turbo …
yue2-song … audio.generate.yue2-song" and "is it confusing how models are mixed
with model aliases [in Try]" (P16).

Try fetches `/me/api/models/catalog` and `/me/api/workflows` separately
(`web/src/app/try/page.tsx:1354`). `groupCatalog` concatenates aliases and
models, puts alias rows first, and collapses targets named by alias `use_names`
(`:644`, `:658`, `:702`). Duplicate labels gain provider and prettified-ID text
(`:710`). Music and sound-effect links lead to the workflow tab (`:279`). The
picker already tracks loading, ready, empty and error (`:1423`).

Library chooses `resolved_model`, then `requested_model`, then the asset model,
and names it from the current catalog (`web/src/app/library/page.tsx:1490`,
`:2189`). Receipts already record requested/resolved model and workflow,
manifest version/hash, parent request and fallback reasons
(`gateway/receipts.py:238`). `modelDisplayName` does not match pooled
`use_names` (`web/src/lib/model-name.ts:90`); keep that fix until a server test
replaces it. Whether the owner's three names describe one execution is
**UNVERIFIED** (no deployment query).

**Voices (release 17).** `speech_voice_catalog` builds picker rows from the
`voice_map` keys, then its values, then the default, and names each by
`value.replace("_", " ").title()` (`gateway/speech_voices.py:13-33`). It never
reads the engine's `voices` list. On the owner's config the Kokoro engine
declares 28 voices (`proxy_config.yml:1245`) and maps 13 OpenAI names to 13 of
them (`:1248`). The picker therefore shows 26 rows: 13 OpenAI names plus 13
native IDs such as "Af Heart". Fifteen real voices are missing and "Shimmer"
and "Af Heart" are the same voice.

Voice knowledge is also code: `DEFAULT_TTS_VOICE = "alloy"`,
`CANONICAL_TTS_VOICES` with gender and description, and
`LEGACY_TTS_VOICE_ALIASES` (`gateway/transforms.py:976-1027`). `_engine_voice`
replaces an unknown voice with the engine default (`proxy.py:3955-3963`). The
picker replaces a missing choice with the default or first row
(`web/src/app/try/voice-picker.tsx:8-18`) and says a default exists even when
none is declared (`:67-70`). P12, P16 and P18 all fail here.

### 1.2 Decision

Separate **capability, preset, model, workflow, voice and model name**. Try keeps
its capability tabs (no new "Task" concept). Within a capability, show the
presets, then "Choose a model" and "Choose a workflow" where those exist. A
workflow is never presented as a model. A voice has one record with one name.
A result has one title and one Details disclosure.

Grounding: M §2 (Together separates readable names from API strings; OpenRouter
separates requested routing from the selected model), M §3 (ElevenLabs separates
voice from model), M §1 and §5D (per-result details).

Rejected: one flat alias/model/workflow list (implies equal meaning); API IDs as
names (builder detail in member decisions); hiding model choice (builders need
it); a preset per alias (a pure spelling is not saved behavior); equivalence
inferred from a shared capability or a prettified string; title-casing IDs into
names.

### 1.3 Entities and headless contract

| Entity | Member label | Identity and rule |
|---|---|---|
| Capability | Existing tab names (Chat, Images, Voice …) | Existing capability key. Not runnable. |
| Preset | "Preset" | ID plus revision. A fixed target or a declared routing policy over models the access plan allows. Uses the Categories and Plans revision closure, not a new permission object. |
| Model name | "Model name" (builder) | An accepted request string mapped to one typed target. A pure spelling gets no picker row. |
| Model | "Model" | ID, display name, optional revision/packing metadata. Unknown release stays unknown. |
| Workflow | "Workflow" | ID, revision, declared inputs/outputs, one or more steps. |
| Voice | "Voice" | Record inside one engine's data: `{id, name, language?, description?}`. Identity is `(engine, id)`. |
| Creation / Receipt | "Creation" / "Receipt" | Existing IDs plus selection and execution evidence. |

Glossary change, applied to VOICE.md with the implementation: Workflow becomes
"A configured process with declared inputs and outputs, in one or more steps".
Add **Preset**: "A saved choice of model and settings for one capability".

All names and defaults come from owner-editable data (P12). Code knows entity
kinds and capability semantics only.

**Catalog.** `/me/api/models/catalog` returns typed `presets`, `models`,
`model_names` (`{name → {kind, id}}`, not picker rows) and capability defaults.
Workflow rows link to `/me/api/workflows` objects. Every selectable row has `id`,
`revision`, `display_name`, `capabilities`, `availability` and `use`
(method/path/body schema). Speech-capable rows carry `voices` (below).
`availability` keeps the DESIGN.md set (Ready/Busy/Degraded/Down/Blocked/
Unknown/Off) with reason codes and dated evidence. The server filters by access
plan before pagination and rechecks at execution. No `schema_version` field:
the shape changes in place (P11).

**Run.** Run routes accept model names. Native `run` also accepts
`selection:{kind,id,revision}`. Selection and name that disagree return
`400 selection_conflict`. A stale revision returns `409 selection_changed`;
the gateway never runs an updated target silently. Settings precedence:
allowed request overrides, then preset defaults, then target defaults. Locked
settings reject overrides.

**Voice data.** Each speech engine's record declares:

```yaml
kokoro:
  voices:                       # complete list; one picker row each
    - {id: af_heart, name: Heart, language: en-US}
    - {id: af_alloy, name: Alloy, language: en-US}
    # … 26 more
  default_voice: af_heart       # must be an id in voices
  voice_map:                    # accepted request spellings → voice id
    {alloy: af_alloy, shimmer: af_heart, …}
```

Rules, enforced by the core and the service-record validator:

1. `voices` is the complete list. Picker rows are exactly these records.
   `voice_map` keys are accepted spellings and never become rows.
2. Every `voice_map` value and `default_voice` must be an `id` in `voices`.
   Otherwise the engine record is invalid; the owner sees which entry is wrong.
3. Two voices in one engine may not share a `name`; the owner adds a qualifier.
   The same `id` in two engines is two voices.
4. A voice without `name` is a validation warning to the owner. The picker then
   shows its `id` exactly as written, never title-cased.
5. An engine with no `voices` list takes a typed voice ID ("open" entry).
6. One mechanism: per-engine `voice_map`. Remove `voice_transform`, the
   `voxtral-voices` registry entry, `DEFAULT_TTS_VOICE`, `CANONICAL_TTS_VOICES`
   and `LEGACY_TTS_VOICE_ALIASES`. The 13 shared clips become `voices` records
   in the seed data of the engines that use them. Descriptions become per-voice
   `description` data. Old aliases are dropped (P11).
7. User-created voices (`custom_voices: true` engines) keep their stored record
   and name, and list in a "Your voices" group of the same picker.

Seed data names the current engines' voices: Kokoro by its vendor name without
the prefix (`af_heart` → "Heart", with `language`); the 13 shared clips by their
OpenAI names; Supertonic `F1`–`M5` by the vendor code itself (Owner decision 4).

**Voice resolution at dispatch.** The core resolves `voice` per engine: an
accepted spelling resolves on any engine that maps it; an `id` resolves on any
engine whose `voices` contains it. A preset that falls back skips an engine that
cannot resolve the voice. If none can, the request fails with layer `engine`,
code `voice_not_available`. An unknown voice is `400 voice_not_available`
(`param: voice`), as OpenAI also rejects an invalid voice. A voice is omitted
only when the engine declares `default_voice`; that default then applies and is
recorded. `/v1/audio/voices` lists the records of the default speech model, or
of `?model=`, in its current `id`/`name` shape.

**Receipt read.** Extend it with:

```text
selection: {kind, id, revision, requested_name}
execution: {resolution_reason, stages: [
  {model_id, model_revision, workflow_revision, engine_or_provider, remote,
   voice: {engine, id, requested}?, evidence, observed_at, output_ids}
]}
```

`evidence` is `gateway_observed`, `provider_reported`, `configured` or
`unknown`. `requested_name` is raw provenance, not a label. Display names come
from the current catalog by identity; nothing is frozen. A voice has no revision
of its own: the engine record revision already in receipts covers it. The
server filters route IDs and host paths by audience. The OpenAI response `model`
field keeps its meaning (external contract).

**CLI and MCP.** CLI: `azizam capabilities --capability <key> --json`,
`azizam run --selection <kind:id@rev> [--voice <v>]`, `azizam receipts get <id>
--json` (additions to the proposed headless CLI). MCP keeps `capabilities`,
`run`, `job`; `capabilities` results gain typed choices and voices, `run`
accepts `selection` and `voice`. No new MCP tool.

### 1.4 Screens, exact copy and states

Try: per capability, "Preset" with the declared default, then "Choose a model"
and "Choose a workflow" only where alternatives exist. Each opens its own
labelled list. "Use a preset" returns. Speech adds a separate "Voice" control.
One available option shows as a named value with "Details", not a selector.
Never auto-switch to a paid or remote option when a choice disappears.

Rows show one name plus only decision-relevant cost, locality and readiness.
Routing preset: "Chooses from models your access plan allows." Fixed preset:
"Uses {model}." Details lists permitted fallback destinations before submit.
Paid runs use the existing confirmation. Search matches names and model names
but returns each entity once; a model-name match shows "Matched model name" in
the result's Details.

Library title: the creation's title under prompt-memory rules, else "{creation
kind}, {date and time}". One "Details" disclosure holds "Selected", "Model used",
"Workflow", "Voice", "Where it ran", "Cost" and measured timing, omitting
inapplicable rows. Multi-step runs list steps. "Technical details" inside it
holds the model name sent, request ID, revisions and evidence source. A
deviation shows "A different model was used" with a Details link. A model that
is no longer installed shows "Removed model"; its recorded ID is in Technical
details. Missing evidence reads "Model not recorded" or "Version not reported".

Models: concrete models first, then a "Presets" section. Model detail has "Use
in an app" with a copyable **Model name** and an example. Workflows keeps
"Needs", "Makes", availability and "Try workflow"; IDs live in Details.

| State | Exact copy / behavior |
|---|---|
| Loading | "Loading presets…" / "Loading models…" / "Loading workflows…" / "Loading voices…". Disabled control, `aria-busy`, draft kept. |
| Empty presets | "No presets are available here." Offer "Choose a model" and/or "Choose a workflow" for each that has rows; if neither, "Ask the owner to turn this on." |
| Catalog failed | "The gateway couldn’t load your choices." / "Try again". Never shown as empty. |
| Selection removed or changed | "This choice is no longer available." / "Choose another". Draft kept; a new choice is required. |
| Cold model | "Loads when you start." only when declared startable. While loading: "Loading model…". |
| Access blocked | "Your access plan doesn’t include this." only for a discoverable target. |
| Details | "Loading details…" / "The gateway couldn’t load these details." / "Try again". The creation stays usable. |

**Voice control states.** The control holds one of: loading, failed,
unselected, valid, removed, open entry. Submit sends the catalog revision; the
server revalidates the voice against it.

| State | Exact copy / behavior |
|---|---|
| Loading | "Loading voices…"; Run disabled. |
| Failed | "The gateway couldn’t load voices." / "Try again"; Run disabled. |
| Default declared | Preselect it; no extra text. |
| No default, list present | Unselected, placeholder "Choose a voice"; Run disabled until chosen. |
| Previous choice not in the new list (model switch, refresh) | "{voice} isn’t available with this model." / "Choose a voice". Never substitute. |
| Removed between render and submit (`409`) | "This voice is no longer available." / "Choose another". Text kept. |
| Open entry (no list declared) | Field "Voice ID", hint "This model doesn’t list its voices." |
| Group label for user-created voices | "Your voices" |

### 1.5 Data and migration

Inventory accepted model names, voice spellings and receipt fields. Classify
each by the P11 table. Map owner-authored names into `model_names`; saved
behavior into presets. Old Azizam spellings are not migrated; the cutover
preview shows their recent users. Seed `voices` records for current engines.
A preset retarget creates a new revision under the access-plan grant rules and
never rewrites what ran. Unknown provenance stays unknown.

Migration is idempotent and keeps receipt IDs, counts, timestamps, asset links,
ownership and amounts. Remove `groupCatalog`'s alias collapsing,
`speech_voice_catalog`, `selectedVoice`'s fallback and the code voice tables
after cutover: one resolver, no parallel path.

### 1.6 Oracle tests

| Level | Fixture and required result |
|---|---|
| Unit (core) | **The real bug as data:** Kokoro-shaped engine, 28 voices, 13-key map. Catalog has 28 rows; `alloy` and `af_alloy` resolve to one row named "Alloy"; `shimmer` resolves to "Heart"; dispatch sends `af_heart`. No engine name in code (boundary test passes). |
| Unit (core) | Map value not in `voices` → invalid record; duplicate names → invalid; same `id` in two engines stays two voices; missing name shows the raw `id`. Unknown voice → `400 voice_not_available`, never the default. Fallback skips an engine that cannot resolve the voice. |
| Component | Model switch, catalog refresh, removal between render and submit, failed catalog, empty list, open entry, default not in list: each shows its state; no request leaves with a substituted voice. Pooled `use_names` workflows never take the recommended model's name. |
| API | Unauthorized candidates never appear or run through a preset. After a preset retarget, an old receipt still names the model that ran. Member payload has no host paths. Removed Azizam spellings fail with `model_not_found` / `voice_not_available`. |
| Adapters | Equal HTTP, CLI and MCP requests produce the same selection revision, voice resolution and receipt. Stale selection fails before dispatch. |
| Browser (390 px) | Pick a capability, preset, voice; generate on a fixture; open Library; tell selected from actual model; copy the model name; run the example against an isolated gateway. Repeat for workflow and fallback. Focus and draft survive errors. |
| Migration | Run twice on copied fixture databases; identities, counts, costs and links equal. |

## 2. Completion notices

### 2.1 Problem and current behavior

Owner: "Picture is ready Open result seems persistent even after I follow the
link. What is the 2026 best practice?"

Release 17 calls `onDismiss` on Open result (`web/src/components/jobs/job-notices.tsx:237`).
Library marks the asset opened on mount (`web/src/app/library/page.tsx:1480`) in
person-scoped `sessionStorage` (`web/src/components/jobs/opened-creations.ts:1`).
Dismissed job IDs use another session store (`job-notices.tsx:185`). Notices poll
`/me/api/jobs?watch=1` (`:144`), stack in a polite live region (`:197`) and
link by asset or job (`:44`). Acknowledgment is per tab session and by asset
alone (RT 5). Jobs are already terminal-final: a late result after a terminal
state is dropped (`gateway/me_make.py:525`, `:533`, `:545`).

### 2.2 Decision

Keep close-on-Open. Add **Recent**: an account-owned list of finished
background work, with server-owned read state. Toast, read state and retention
are separate. Library stays the home of creations; receipts stay the record of
requests.

**Scope (RT 7).** Recent covers work that runs as a **job**: the request returns
before the result exists. This is a declared execution mode, not a list of
capability names, so every capability that runs as a job is covered equally
(P17). Synchronous results (chat, speech, transcription, embeddings, decisions)
show inline where they were requested and never toast. Coverage today:

| Work | Runs as job | In Recent |
|---|---|---|
| Images from Try and `/me` make routes | Yes (`runImageAsJob`) | Yes |
| Video, music, sound effects, workflows | Yes | Yes |
| Speech, chat, transcription, embeddings, decisions | No | No; result is inline |
| A job from an App key that belongs to a person | Yes | That person's Recent |
| A job from a service App with no person | Yes | App history only |

Whether every row above matches the server today is **UNVERIFIED**; the
implementation's first test enumerates job intents and fails on any mismatch.

Grounding: N ranked recommendations 1–7; Google action-dismiss [S2], W3C
untimed alternative [S26], GitHub synced inbox [S9], Slack/Notion
read-versus-clear [S14/S35].

Rejected: stacked success banners (cover phone work); toast-only delivery
(results must stay findable); read on timeout or dismiss (no proof of viewing);
session storage as truth; a durable outbox (atomic creation is simpler, below);
web push and email (CRM decision M3: not in v1).

### 2.3 Entities and headless contract

A **Recent entry** is one recipient's record of one job reaching its terminal
state. Identity: `(recipient_id, job_id)`. A job reaches a terminal state once,
so one entry per job. Retry creates a new job with `retry_of`, so it gets its
own entry. Metadata updates never create entries.

Fields: `job_id`, `receipt_id`, `outcome` (`succeeded|failed|canceled`),
`finished_at`, `capability`, safe title, `output_ids[]`, `read_at`. No prompt
text: the title comes from permitted current content or kind and date.

**Atomic creation (RT 6).** The entry is written in the same SQLite transaction
as the terminal transition, in the jobs database. No terminal job exists
without its entry, so a person cannot see a result before its entry exists.

| Operation | HTTP | Rule |
|---|---|---|
| List | `GET /me/api/recent?cursor=&read=all\|unread` | `entries`, `next_cursor`, `unread_count`, `as_of`. Ownership filtered before paging and counting. |
| Mark read | `PUT /me/api/recent/{job_id}/read` | Idempotent; server sets `read_at`; monotonic (no unread operation). Wrong owner = not found. |
| Mark all read | `POST /me/api/recent/read {as_of}` | Marks only entries finished at or before the `as_of` the list returned; racing completions stay unread. |
| Job detail | existing job read | Returns the entry's `job_id` and `read_at`. |

CLI: `azizam recent list --json`, `azizam recent read <job_id>`,
`azizam recent read --all --as-of <t>`. MCP: the `job` result includes `read_at`
and output links; MCP does not mark read or list Recent (an agent reading a job
is not a person viewing it).

**Destinations (RT 5).** Every notice and Recent row links to
`/library/?job={job_id}`, a stable app page, never a signed media URL. It shows
all outputs, or the failure. Rendering it visibly marks that job read. Opening a
creation from Library browsing marks read only when exactly one of the person's
unread entries lists that asset; with two or more, it marks none.

**Delivery.** Keep polling the existing job watch. Each browser keeps one
`localStorage` set of toasted job IDs (capped at 500, cleared on account
switch), so a second tab or a reload never toasts the same job twice. A new
device starts at the current time: backlog stays in Recent, never as toasts. Two
signed-in devices may each toast once; exactly-once across devices is not
promised. Read state reaches other devices at their next poll.

### 2.4 Lifecycle, screens and exact copy

Recent is a link in the shared member shell, including phones, at `/recent/`
(root path, CRM §2). The page has "Recent", "All" / "Unread" and "Mark all
read". Rows: title, outcome, time, a "New" marker and "Open result" or "See
details". Opening the list does not mark rows read.

| Event | Toast | Read state |
|---|---|---|
| Job finishes while the person is elsewhere | One toast per burst | Unread |
| Result rendered visibly on `/library/?job=` | Hidden | Read |
| "Open result" pressed | Hidden at once | Read when the destination renders visibly |
| Destination opened in a background tab | Hidden | Unread until visible |
| Close or timeout | Hidden | Unchanged |
| Mark all read | Matching toasts hidden | Read through `as_of` |
| Read on another device | Hidden at next poll | Read |
| Output deleted later | No toast | Unchanged; detail shows "This file is unavailable" |

"Rendered visibly" means the authorized job detail and its outcome are mounted
in a visible document. Playing media is not required. A 401, failed fetch or
hidden tab is not read. A rendered failure or "This file is unavailable" counts.

**Toast.** One floating toast at most. Collect completions for two seconds, then
show: "Picture is ready" + "Open result"; "3 creations are ready" + "View
Recent"; "2 ready, 1 needs attention" + "View Recent". Failure: "Your video
couldn’t finish" + "See details". The detail names the failing layer and one
next action, or "We couldn’t tell where this stopped." Canceled jobs show
"Canceled" in Recent with no toast. "Retry" opens the run form with cost and
consent; a notice never dispatches paid work.

The toast stays eight active seconds. Later completions update its count without
restarting the timer. After it closes, further completions in the same burst
(no two-second gap) go to Recent only. Counts include only outcomes they name.

**Announcements (RT 10).** A `role=status`, `aria-live=polite` region exists
before any toast. It announces the first summary once. If a failure joins later,
it announces only that: "1 needs attention". Successes that join later update
the visible count silently. At most two announcements per burst. No focus theft.
The timer pauses on hover, keyboard focus or a hidden document. Close is named
"Dismiss notification". A remote read while a toast control holds focus waits
until focus leaves. Targets are 44 px; reduced motion is respected; "New" and
outcome are not color-only.

| State | Exact copy / action |
|---|---|
| Loading | "Loading Recent…"; skeleton, `aria-busy`, no zero badge. |
| Empty All | "Background work appears here when it finishes." |
| Empty Unread | "You’re up to date." |
| List failed | "The gateway couldn’t load Recent." / "Try again". Cached rows stay, with "Last updated {time}". |
| Read failed | "The gateway couldn’t mark this as read." / "Try again". Retried at next poll. |
| Result failed to open | "The gateway couldn’t open this result." / "Try again". |
| File unavailable | "This file is unavailable." Receipt link stays. |

### 2.5 Data and migration

Entries live in the jobs database and are kept indefinitely (P19, settled).
Deleting Recent entries never deletes receipts or media; any owner retention
change needs the P19 preview and approval. Account erasure is a separate
operation.

Backfill terminal jobs with known ownership, marked read with
`read_source=migration` (no `viewed_at` invented), so the upgrade does not flood.
Jobs without a person stay in App history. Remove both session stores and their
readers at cutover.

### 2.6 Oracle tests

| Level | Fixture and required result |
|---|---|
| Unit (core) | Terminal transition and entry commit together; a crash between them leaves neither. Duplicate or reordered completions give one entry per job. A late result after terminal changes nothing. Retry creates a second entry. |
| API | Read is idempotent and monotonic. Mark-all excludes a job that finished after `as_of`. Cross-person reads, counts and cursors reveal nothing. Two jobs sharing an asset: opening the asset marks neither; opening `?job=` marks only that job. |
| Race schedules | (a) View result → leave → poll arrives: no toast, already read. (b) Read on device A, completion poll on device B: B shows no toast after its next poll. (c) Gateway restart mid-job: one entry after resume. (d) Sign-out with a pending read write: the write is dropped, never sent as another account. |
| Component | Fake timer: eight active seconds; pause on focus; Open hides at once; dismiss/timeout do not mark read. Success-first then failure-later: two announcements, second "1 needs attention". Continuous burst: one toast. |
| Browser / a11y | Two tabs: one toast. Keyboard, VoiceOver/Safari and TalkBack/Chrome reach Recent after expiry. Typing focus stays put. Denied notification permission changes nothing. |
| Coverage | Every job intent the server can create appears in Recent; no synchronous route creates an entry. |

## 3. Account recovery without mail

### 3.1 Problem and current behavior

Owner: "Forgot your password? Ask the person who invited you for a reset link.
< Isn't the usual thing to have a self service link?" (P14)

The sign-in page shows that sentence as plain text (`web/src/app/sign-in/page.tsx:247`).
`POST /admin/api/people/{id}/reset` is owner-only and returns a one-hour link
(`gateway/accounts_api.py:108`, `:236`, `:68`); a new link revokes older ones
(`gateway/accounts.py:265`). The People dialog shows a copy field and expiry
(`web/src/app/people/page.tsx:791`, `:830`) but no cancel.
`POST /auth/api/password-reset` checks origin, throttles, changes the verifier,
deletes all sessions and consumes the link in one transaction
(`gateway/accounts_api.py:200`, `gateway/accounts.py:355`). App keys are not
rotated (`docs/PEOPLE_AND_ACCOUNTS.md:96`). The gateway sends no mail
(`docs/PEOPLE_AND_ACCOUNTS.md:176`).

Defect (CRM §3.1): redemption throttles on the literal key `"reset"`
(`gateway/accounts_api.py:203`), so all clients share 10 attempts per 15
minutes. Anyone can block every reset cheaply.

Sole-owner recovery today is `generate-admin-key owner-cli` on the host, which
mints a broad admin key (`docs/FIRST_RUN_SETUP.md:243`, `:265`). That is not a
narrow password reset. It was not run here.

### 3.2 Decision

This batch delivers recovery that works **completely without mail** (RT 2):

1. A real "Trouble signing in?" entry on every sign-in page.
2. Owner-assisted reset links, with cancel.
3. A host command that recovers the sole owner, local or provider-only.

Mail is a separate optional capability owned by CRM §3. This design does not
define recovery addresses, delivery, request-by-email or mail messages. It reads
one value, `recovery.email_delivery`, that the mail module supplies. Without the
mail module the value is `off` and nothing here changes. When it is `ready`, the
recover page shows CRM's email form (CRM §3.5) above the no-mail path. Mail
reuses this design's redemption core; it adds no second one.

Grounding: R "Answer", "Recovery entry-point states", ranked 1–6 (Gitea gates
reset on mail; Nextcloud needs working delivery; Home Assistant and Immich keep
administrator recovery); R "Security baseline" for non-enumeration, atomic
redemption and session revocation.

Rejected: a fake "Send email" button; inviter wording (the inviter may not
manage accounts; a sole owner cannot ask themselves); links or temporary
passwords in an unauthenticated response; account recreation or database
editing; magic links and passkeys (neither solves no-mail recovery now).

### 3.3 Headless contract

**Options.** `GET /auth/api/options` gains:

```text
recovery: {
  email_delivery: "off"|"ready"|"unavailable"|"unknown",   # from CRM; "off" without mail
  owner_help_url: "/sign-in/recover/owner/",
  external: [{label, sign_in_url}]                          # from trusted config
}
```

No account, email, role or eligibility is public.

**Redemption core.** Keep `POST /auth/api/password-reset {token, password}`.
Opening a link does not consume it. Success consumes the proof, revokes other
reset proofs and all browser sessions, and writes an audit event. It does not
sign in, enable a disabled person, change a role or plan, or cancel jobs. The
current password minimum stays. Fix the throttle: one bucket per client IP and
one per link (keyed by the stored link hash); remove the shared `"reset"` key.

**Owner-assisted links.** The existing owner-only route stays, with three
changes. The owner must have signed in within 10 minutes (the same freshness
rule CRM uses). The response adds a non-secret `link_id`. A new
`DELETE /admin/api/people/{id}/reset-links/{link_id}` cancels it. Default
lifetime stays one hour; the maximum drops from 30 days to 24 hours. Only
built-in-password people are eligible. Admins do not gain this right. CLI:
`azizam people reset-link <person> [--output FILE]` (file mode 0600; never
logged).

**Host recovery (RT 8).** New command, run on the gateway host as the service
user. It needs no browser session, no mail and no prior knowledge of any
internal ID.

```text
systemd install:  ./venv/bin/python -m gateway.cli account recover-owner
container:        docker compose exec azizam azizam account recover-owner
```

1. It reads the same configuration the service reads and prints the data
   directory and `admin.db` path it will use. A missing or unreadable database
   stops with that path and no changes.
2. It lists owners: number, display name, sign-in methods ("Password",
   "{provider label}"), email as entered, last sign-in. Nothing secret.
3. One owner: the operator confirms by typing `recover`. Several: the operator
   picks a number, then confirms. Non-interactive: `--owner <email>` must match
   exactly one owner, else it exits non-zero without changes.
4. **Owner with a password:** it issues a 15-minute reset link through the
   redemption core.
5. **Provider-only owner:** it issues a 15-minute "set a password" link on the
   **same person ID**. Redeeming it adds a password sign-in to that account. No
   new account, no change at the provider. Afterwards Account → Security shows
   "Password: on" with "Remove password", allowed only while another sign-in
   method exists (`DELETE /me/api/password`).
6. The link prints to the terminal only, or to `--output FILE` (0600). It uses
   the configured public address (`instance_presentation.base_url`); if none is
   set, it prints the path and says "Open this on your gateway's address."
7. It writes an audit event `account.owner_recovered` and nothing else.

Password sign-in is accepted for any person who has a password, even when the
sign-in page leads with a provider. The page then shows the provider button
first and "Use a password" under it. Whether built-in sign-in can be fully
disabled today, and how that interacts with this rule, is **UNVERIFIED**.

The recovery runbook stops using `generate-admin-key`. Whether that command
stays for headless automation is a HEADLESS_AND_RESOURCES decision, outside this
batch. MCP supports no recovery operation (inference connection).

### 3.4 Screens and exact copy

"Trouble signing in?" appears under every sign-in method, including a
provider-only page, and opens `/sign-in/recover/`. All recovery pages have
"Back to sign in". The owner page `/sign-in/recover/owner/` is static, so it
loads even when sign-in is broken.

| State | Exact copy / action |
|---|---|
| Options loading | "Checking recovery options…" |
| Options failed | "The gateway couldn’t load recovery options." / "Try again". The "Are you the owner?" link stays. |
| No mail (`off`, `unknown`, `unavailable`) | Title "Reset your password". Body "Ask the owner of this gateway for a reset link." Link "Are you the owner?" |
| Mail `ready` | CRM §3.5 email form above the no-mail body. |
| Provider sign-in configured | Extra row: "Signed in with {provider}? Recover your account there." / "Continue with {provider}". Never says which method an email uses. |
| Owner page | Title "Recover owner access". "If you can use the computer that runs this gateway, run this command there. It makes a one-time link." Then the two commands from §3.3 and "It works without email and keeps your account and history." |
| Link redemption | Title "Choose a new password"; "New password", "Confirm new password"; "Save new password"; busy "Saving password…". Paste and password managers work. |
| Set-password link (provider-only owner) | Title "Add a password"; "Save password". |
| Invalid, expired or used link | "This reset link no longer works." / "Get a new link" (to `/sign-in/recover/`). No diagnosis. |
| Success | "Your password has changed. Sign in with your new password." Plus "Your other browser sessions have ended." after revocation commits. |
| Next sign-in after a reset | Account → Security event: "Password changed {time}." |

**Owner handover dialog.** "Create a reset link for {name}?" / "Confirm you’re
helping the right person. Anyone with this link can choose a new password for
this account." / "Create link". After: "Share this link privately with {name}."
/ "Copy link", "Cancel link". Expiry sits under Details (P15). Expired: field
disabled, "Create a new link". Copy failed: "Couldn’t copy the link. Select it
to copy." Cancel failed: "The link still works. Try again."

Account → Security: "Resetting your password ends browser sessions. App keys
keep working." A suspected compromise is a separate review of keys; no copy
promises that a reset stops spending.

### 3.5 Data and migration

Reuse person IDs, link, audit and session records. Add the set-password link
purpose. Retain receipts, creations, ownership, plans and jobs. At cutover,
revoke all outstanding reset links (short-lived; the owner reissues). `/ui/`
fragment links stop working with the root move (P11). Reset proofs stay hashed,
out of logs, analytics and referrers; the fragment is cleared after capture.

### 3.6 Oracle tests and sole-owner release gate

| Level | Fixture and required result |
|---|---|
| Component | Mail off/ready/unavailable/unknown, built-in/provider/mixed: only supported actions show; "Trouble signing in?" is on every variant. Proofs never appear in errors or analytics. |
| API | Expired, replayed, wrong-purpose and concurrent redemption fail; exactly one concurrent redemption wins. Throttle: one client's failures do not block another IP's valid reset. |
| Authorization | Members, App keys and admins cannot issue links. Stale owner sign-in fails without side effects. Reset cannot re-enable a disabled person. Cancel invalidates only its link. |
| Browser (390 px) | Owner creates a link, member opens it on a second context, sets a password, signs in. Old password fails; every earlier session is rejected; App keys keep their authority. |
| Host command | Starting with no internal IDs, only the shipped owner page: (a) local sole owner; (b) provider-only owner while the provider is down; (c) two owners, explicit pick; (d) wrong `--owner` → no change. Each keeps the same person ID, roles, plans, creations and receipts; sessions revoked; no broad key created; restart, then sign in again. |

The host-command row is a **release blocker**, run on disposable copies of each
supported install type, never the live gateway. **UNVERIFIED** until then:
config discovery in each install type, the container command path, set-password
binding, and session rejection across every authentication adapter.

## 4. Prompt saving ("Don't save this prompt")

### 4.1 Problem and current behavior

Owner: "How does this relate to e.g. incognito which is how real products frame
stuff like this" (P14) and "why does this only show on some types of
generations?" (P17).

Try shows "Don’t keep this prompt" only on the image, speech and workflow tabs
(`MEMORY_TABS`, `web/src/app/try/page.tsx:180`). The choice persists for the
browser session (`:182`). The request carries
`extensions["com.azizam.prompts"].mode = "forget"` (PROMPT_MEMORY.md "Request
and API contracts"). A forgotten async job lives only in memory and cannot
resume after a gateway restart (PROMPT_MEMORY.md "Code challenges"). The
current "Only you can see kept prompts." is true of the app, not of a host
administrator.

### 4.2 Decision

Keep a per-request control and name exactly what it does: **"Don't save this
prompt"**. Show it on every capability with the same rules (P17). State the
three consequences before submit: the result is still saved, a restarted job
cannot resume, and the owner and the engine or provider are outside this
promise.

Grounding: PM executive finding and §6.1–6.4. Browsers keep downloads in
private modes (Chrome, Firefox); creative products separate output storage from
publication (Midjourney Stealth); AI products split memory, history, training
and retention (ChatGPT, Claude, Gemini, Copilot). Admin visibility is documented
by Claude, ChatGPT Enterprise and Copilot. "Save" pairs with "saved to Library"
(PM §6.1).

Rejected: "Incognito", "Private mode", "Anonymous", "Zero retention" (each
promises more than Azizam does; PM §5, §7); a mask icon or dark theme as the
only signal (PM §6.7); retroactive deletion from a toggle; silent fallback that
saves the prompt to allow resume.

### 4.3 Headless contract

Request: `extensions["com.azizam.prompts"] = {"save": false}`, replacing
`mode:"forget"` with no alias (P11). OpenAI Responses `store:false` has the same
effect (external contract). The policy is fixed at acceptance; a later settings
change never alters an accepted request.

The catalog declares, per capability, `prompt_saving: "choice" | "never"`,
computed by the core from the execution path, never from capability or engine
names. A path that cannot honor `save:false` is a defect: the core rejects the
request with `400 prompt_save_unsupported` rather than saving.

Receipt and job read gain `prompt_saved: bool` and, for jobs, `resumable: bool`.
CLI: `azizam run … --no-save-prompt`. MCP: `run` accepts `save_prompt: false`.
The effective account and household settings stay at the existing
`/me/api/prompt-memory` and `/admin/api/settings/prompt-memory`.

**Per-capability matrix.** Each value must be confirmed by the copy-path audit
in §4.6 before its copy ships; all are **UNVERIFIED** today.

| Capability | Saves the prompt now | Runs as job | Control |
|---|---|---|---|
| Images, video, music, sound effects, workflows | Yes (with the creation) | Yes | Switch; restart line shown |
| Speech | Yes (text with the audio) | No | Switch; no restart line |
| Chat | Only through Responses `store` | No | Switch if the core reports `choice`, else fixed row |
| Transcription, embeddings, decisions | Believed no | No | Fixed row "Azizam doesn’t save these requests." |

### 4.4 Screens and exact copy

One row above Run on every capability tab. `{result}` is the declared output
kind ("image", "audio", "video") or "result" for mixed output.

| State | Exact copy |
|---|---|
| Switch off (saving) | Label "Don’t save this prompt". Note "Saved with the {result}. Only you see it in your Library." |
| Switch on, job capability | Note "Your {result} still goes to Library. If the gateway restarts before it finishes, it can’t resume." |
| Switch on, other capability | Note "Your {result} still goes to Library." |
| Disclosure link | "What is saved?" |
| Disclosure body | "Azizam uses your prompt to make this, then doesn’t save it. The {result} and a record of the request (time, model, cost) stay in your account. This doesn’t hide your activity from the owner. The engine or provider that makes it follows its own rules. The {result} itself may contain words from your prompt." |
| You turned saving off | Fixed "Prompts aren’t saved". Note "You turned this off. Change" (to Account). |
| Owner turned it off for everyone | Fixed "Prompts aren’t saved". Owner: "You turned this off for everyone. Settings". Member: "The owner turned this off." |
| Settings failed to load | Fixed "This prompt won’t be saved". Note "The gateway couldn’t load your prompt settings." / "Try again". |
| Capability never saves | Fixed "Azizam doesn’t save these requests." |
| Library item | "Prompt not saved" in Details. Reuse disabled: "Prompt not saved, so it can’t be reused." |
| Job lost on restart | Title "Couldn’t resume after a restart". Body "This prompt wasn’t saved. Enter it again to start a new one." / "Start again" (opens the form with cost and consent; never resubmits). |

The switch state is text plus the switch, never icon or color alone. Screen
readers get the label and note together. The session memory of the switch stays
and is visible on every tab.

The owner line names only what is verified. If the copy-path audit confirms
exactly what the owner can see, replace "This doesn’t hide your activity from
the owner." with that fact (for example "The owner can see that you made this
and what it cost."), and never write "Only you can see this" about the request.

### 4.5 Data and migration

No new storage. Rename the extension field; update Try, CLI and MCP in the same
change. Replace `MEMORY_TABS` with the catalog's `prompt_saving`.

### 4.6 Oracle tests

| Level | Fixture and required result |
|---|---|
| Copy-path audit | For each capability with `save:false`: no prompt bytes in the gateway's data and cache directories, logs, job store, receipts or Library search (extends the existing no-bytes test). Results decide each matrix row. |
| API | `save:false` → `prompt_saved:false` on receipt; job `resumable:false`. Restart mid-job → "interrupted" state with no prompt. Unsupported path → `400 prompt_save_unsupported`. `mode:"forget"` is rejected as unknown. |
| Adapters | Equal UI, HTTP, CLI and MCP requests give equal receipts. |
| Component (P17) | Every capability tab renders the row in the state the catalog declares; none is hidden. |
| Browser (390 px) | A household member can say what stays, who may see it and what a restart changes, after reading only the row and disclosure (P4; usability check, not automatable). |

## Support matrix (P21)

| Action | UI | HTTP | CLI | MCP |
|---|---|---|---|---|
| List choices, voices, prompt-saving mode | Try, Models | catalog | `capabilities` | `capabilities` |
| Run with selection, voice, `save:false` | Try | run routes | `run` | `run` |
| Read receipt with selection/execution | Library Details | receipt read | `receipts get` | via `job` links |
| List Recent / mark read / mark all | Recent | `/me/api/recent` | `recent` | Not supported: agents are not viewers |
| Owner reset link / cancel | People | `/admin/api/people/...` | `people reset-link` | Not supported: inference connection |
| Request reset / redeem | Recover pages | `/auth/api/...` | — (browser link) | Not supported |
| Sole-owner recovery | Owner page (instructions) | — | `account recover-owner` (host) | Not supported |
| Remove password | Account → Security | `DELETE /me/api/password` | Restish | Not supported |

Unsupported surfaces return an explicit error, not 404.

## Owner decisions

| # | Decision | Recommended default | Why |
|---|---|---|---|
| 1 | Approve the cutover preview of retired names | Retire every Azizam-owned model name and voice spelling in the preview after updating the Apps it lists | P11 settles "no aliases"; the preview is the only place an App of yours can break. |
| 2 | Recent covers background jobs only | Yes; synchronous chat and speech never enter Recent | Their result is already on screen; adding them would flood Recent and toasts. |
| 3 | Provider-only owner recovery adds a password to the same account | Yes, removable later in Account → Security | The only no-mail path that keeps your identity and history during a provider outage. |
| 4 | Names for voices whose vendor gives only codes (Supertonic F1–M5) | Use the vendor code as the name until you author better ones | Invented names would be guesses; the code is honest and editable as data. |
| 5 | Reset link lifetimes | Owner link 1 hour (max 24 hours); host link 15 minutes | Keeps today's default; caps a forgotten link's exposure. |
| 6 | Member label "Preset" | Use "Preset", then test on a phone with a household member | Distinct from "Model"; the comprehension test decides, not taste. |

## Red-team resolution

RT = `azizam-redteam-ux-batch1-ccb4220-r0.md`; D-lines refer to r0.

| Finding | Resolution | Where |
|---|---|---|
| B1 Voice contract does not remove the two-name picker | Applied. Voice records with one name, `voice_map` as spelling data only, validation, no title-casing, the Kokoro 28/13 case as a unit test, cross-engine IDs kept distinct, voice in execution evidence. **Sub-point rejected:** a separate voice revision; the engine record revision in receipts already covers it. | §1.1, §1.3, §1.6 |
| B2 No-mail batch contains a mail subsystem | Applied. All addresses, delivery, request-by-email and mail tests removed; recovery reads only `email_delivery` from CRM. | §3.2, §3.3 |
| M3 Old names and formats preserved as if external | Applied. External-versus-Azizam table; no frozen labels, no byte-stable names; old reset links and `/ui/` paths stop working; raw strings kept only as provenance. r0 owner decision "freeze historical labels" deleted. | Common rules, §1.3, §1.5, §3.5 |
| M4 Voice dispatch boundary and false default | Applied. Six control states, submit-time revalidation, no client or server substitution, omission only with a declared default. | §1.3, §1.4 |
| M5 Attempt identity lost in asset-only links | Applied, simplified. Jobs are already terminal-final (`me_make.py:525`), and retry creates a new job, so identity is `(recipient, job)`. All links carry `job_id`; asset opens mark read only when unambiguous. | §2.3 |
| M6 Read-before-projection race | Applied with the first option: entry created atomically with the terminal transition; outbox removed. Race schedules added. | §2.3, §2.6 |
| M7 "Terminal work" overpromises coverage | Applied. Scope is declared job execution, with a coverage table and a test enumerating job intents; empty copy says "Background work". | §2.2, §2.6 |
| M8 Sole-owner bootstrap and provider-only entry missing | Applied. Discovery, owner listing, confirmation, non-interactive rule, set-password binding with removal, "Trouble signing in?" on provider-only pages, test starts with no internal IDs. Release-blocker label kept. | §3.3, §3.4, §3.6 |
| m9 Copy contradicts glossary; workflow-only empty state | Applied. "Task" dropped for existing "Capability"; Workflow glossary amended; "gateway" not "server"; empty state offers models and/or workflows. | §1.3, §1.4, §3.4 |
| m10 Burst announcements undefined | Applied. First summary once, a later failure announced alone, at most two per burst. | §2.4 |
| RT settled decisions 1–5 | Applied. r0 decisions on label freezing, retention and toast timing removed from owner decisions. | Owner decisions |
| CRM amendment 1: public verify endpoint | Accepted; it now lives wholly in CRM because this design no longer defines recovery addresses. | §3.2 |
| CRM amendment 2: root paths | Accepted: `/recent/`, `/sign-in/recover/`, `/library/?job=`. | §2.3, §3.4 |
| CRM note: shared `"reset"` throttle bucket | Accepted; fixed in the first recovery change. | §3.3 |

Also cut from r0 as over-engineered: catalog `schema_version`, the
BroadcastChannel presenter lease, the offline acknowledgment queue,
`terminal_revision`, public `password_reset`/`owner_assisted`/`passkey_sign_in`
flags, future passkey copy, and a changed reset-link default.

## Before implementation (P20)

An independent review of this revision against RT, the research and current
code; then a recorded go decision on this version. Confidence: high in the four
conceptual decisions; moderate in the data shapes; **UNVERIFIED** for the
per-capability prompt-saving matrix, job-intent coverage, host-command paths in
each install type, and all usability and assistive-technology results.

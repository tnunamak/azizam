# Request retention

Status: proposed design, not implemented. 2026-10-19. Base: `main` at
`a372c303`. Owner: the Azizam owner thread. Three choices in this area are
reserved for Tim. Section 18 lists them with a recommendation for each, apart
from the design. The design works for every answer.

## Sources and how claims are marked

Each design claim names its ground. A claim with no ground says
**UNGROUNDED**.

- **[handoff]**: divan `local-ai-gateway/request-body-retention-recommendation-fits-identity-architecture-but-five-items-need-changes-and-owner-decides-default.md`. It holds the owner tasks, Tim's reserved decisions and two judged reviews (findings F1-F12, I1-I13).
- **[research]**: divan `local-ai-gateway/request-body-retention-has-no-class-aware-contract-...md` at divan `911fa8b` (owner task 10 applied: 26 products). Claim ids such as GW-L2, PT-W1, L14 and OS1, and recommendation ids R1-R10, refer to that file.
- **[reach]**: divan `local-ai-gateway/administrator-content-reach-differs-across-14-products-...md` (2026-10-11), synthesis "Bottom line", "Sharing" and "Support and break-glass".
- **Dnn**: `~/Documents/azizam/engine-contract/DECISIONS.md` (D26-D46). This file is outside the repo.
- Repo paths with line numbers, at `a372c303`.

## 1. Summary

1. **One contract, four kinds.** Azizam keeps request content under one
   policy contract with one decision function, one write path, one reader
   rule, one erasure machine and one per-request control. The contract has
   four content kinds. Each kind has its own purpose sentence and its own
   defaults:
   - Library prompts and saved conversations already exist.
   - Request history and Debug captures are new.

   The default values are bundled registry data, not code constants.
2. **The reader floor is in core.** Only the person who made a request can
   read its content through Azizam. Data can narrow that set. Nothing in data
   can widen it. Widening is one marked extension point, and it stays closed
   until Tim decides.
3. **LiteLLM keeps nothing.** Azizam decides retention before a request
   reaches LiteLLM. The generated LiteLLM config and the launch environment
   turn off every LiteLLM place that can keep content. A test reads the
   generated artifact and fails when a content sink can be on.

   Today this is not true on the live host. LiteLLM there has a database
   and writes spend and error rows. Its default ERROR line printed an
   echoed prompt in a measured test. Slice 3 fixes this and ships first
   among the behaviour changes.
4. **Facts about other systems are data.** Provider retention facts are
   provider-catalog data with evidence dates. Engine-kept history is a fact
   on the engine's protocol, definition or record, shown in the ledger and in
   the "never save" disclosure. Azizam adds no purge code.
5. **"Never save" says only what is true.** The copy says what Azizam does
   not write. It also names what Azizam cannot control: swap, hibernation,
   crash dumps, the engine's own history and who can reach the engine's
   port.
6. **One contract, three layers of UI.** Presets, a guided form and full data
   editing all show the same contract.
7. **Debug window.** It is one step from the engine check. It captures the
   opener's own requests to one engine for a limited time, and it follows the
   same reader floor and secrets rules as everything else.
8. **Run again.** It makes a new request through the front door, with that
   request's permissions, cost checks and admission.
9. **Phases.** The build is nine small slices (section 17). Slice 1 changes
   no behaviour. It moves today's defaults into data and proves that every
   decision stays the same.

## 2. What exists today

The repo survey for this design (2026-10-19) found these facts. They are the
baseline that must not change silently.

| Store | Content | Default | Reader | Source |
|---|---|---|---|---|
| `asset_prompts` in `data/media_jobs.db` | Prompt text and original workflow metadata of Library media | kept; `retention_cap=never` | maker only, while unexpired | `gateway/prompt_memory.py:29-45,356,429`; `docs/design/PROMPT_MEMORY.md` |
| `responses` in `data/responses.db` | Full Responses `request_json` and `response_json` | `store` defaults to true when the request has a person | person plus App or credential scope | `proxy.py:26797-26804`; `responses_store.py` |
| Receipts in `data/receipts.db` | Structural facts only; key-name guard | kept forever unless the owner sets 30/90/365 days | audience projections | `gateway/receipts.py:29-77,526`; `gateway/receipt_store.py:52-54,211` |
| Execution spool `data/image_media_jobs/mj_*.json` | Full request body of an async media job | durable until the job ends; memory-only for forget, member-off or kill switch | runtime only | `proxy.py:20277-20419`; `docs/CORE_CONTRACTS.md` "Normal asynchronous jobs" |
| STT workspace `data/stt-spool/` | Uploaded audio and its segments | deleted when the request ends | runtime only | `gateway/stt_chunking.py:251,522,529`; `proxy.py:4751-4759` |
| Multipart spill | Any uploaded part over 1 MiB | a temporary file in the system temp directory, deleted at request end | runtime only | Starlette 0.50.0 `formparsers.py:126,209`; `proxy.py:8441,9045,10211,12374,23255` |
| Engine output files `data/processes/<id>.log` | What an engine the gateway starts prints (mode 0600; at most 64 KiB by default, up to 1 MiB) | on | host only | `gateway/engine_process.py:513-531`; D45 |
| LiteLLM subprocess (1.86.2) | Requests in memory. **On the live host it has a database** (`DATABASE_URL` from `.env.local`; Postgres on loopback), so spend rows and error rows are written. Error rows can hold the text of a provider error that echoes the prompt. At the default log level, its ERROR line printed an echoed prompt to stdout, which goes to the journal (measured). | the generated config sets no content switch; no callbacks, no cache | host only | `gateway/litellm_substrate.py:101-116`; `start.sh:9-12,92-148`; `gateway/provider_sidecar.py` (the child inherits the whole environment); LiteLLM survey for this design, 2026-10-19 |

Today the policy is `prompt_memory.decide` (`gateway/prompt_memory.py:136-159`).
Its defaults are code constants (`prompt_memory.py:29,32-33`). The same values
are repeated in `gateway/settings_api.py:19-24`,
`gateway/me_prompt_memory.py:18-20` and `web/src/lib/prompt-memory.ts`. The
per-request control is `extensions["com.azizam.prompts"].mode = "forget"`
(`prompt_memory.py:68-81`; `docs/EXTENSION_TARGETS.md:13`). Chat Completions,
Anthropic and Ollama requests keep no body today (the repo survey, section 6).
Prompt memory has no MCP or CLI surface.

## 3. The contract (owner tasks 1 and 4)

### 3.1 Four kinds, each with one purpose sentence

A **kind** is a reason Azizam keeps content. Core code defines the set of
kinds, because each kind needs a capture point in code. Data gives each kind
its values. A data file that names an unknown kind fails at startup and in CI,
as an unclassified field does under D28.

| Kind | Purpose (the sentence the UI shows) | Exists today |
|---|---|---|
| `library_prompt` | "Library keeps the words that made each thing you created, so you can find it and make it again." | yes: `asset_prompts` |
| `conversation` | "An app can ask Azizam to keep a conversation so it can continue it later." | yes: Responses `store` |
| `request_history` | "Request history keeps what your apps sent for a short time, so you can check it and run it again." | new |
| `debug_capture` | "A Debug window keeps what went to one engine and what came back while you set it up, then deletes it." | new |

The two existing kinds and the new request history serve different
purposes. That is why their defaults can differ without the simple path lying
about the deep one ([handoff] I1; Identity "Try and Library exist to test and
keep results", `docs/PRODUCT_STRATEGY.md:43`). One contract carries the
different values, so the UI shows one model ([handoff] F2 and I1, first fix).

**Overlap rule.** A media request can create both a Library prompt and a
request-history record. Then the two records link. When the maker deletes a
Library item, its linked request-history records go too. Deleting what you
made deletes every kept copy of the words that made it. This keeps the spirit
of "one prompt write path" (`PROMPT_MEMORY.md` "Code challenges"). A second
copy can exist, but only under the same policy and the same deletion.
UNGROUNDED: whether people expect this cascade. A live check of the Library
delete flow covers it (section 16.3).

### 3.2 Where each value lives

The contract has three layers. Each layer can only narrow the one above it.

1. **Bundled defaults: `registry/retention.yml`.** This is data shipped
   with the gateway. It has a closed schema, `registry/retention.schema.json`,
   and the owner can override every value. It holds each kind's purpose,
   default and choices. Core code has no default of its own. A missing file
   fails startup ([handoff] I7; `docs/EXTENSION_ARCHITECTURE.md:7-10` "Data
   owns ... policy defaults").
2. **Household policy.** The owner's values, in `admin.db`
   `operator_settings`, key `retention`. Today's `prompt_memory` key moves
   there in slice 1 (section 12). It holds `never_keep` (the kill switch,
   one for all kinds), and for each kind: `default_keep` for new members,
   `retention_cap`, `readers` and `parts`.
3. **Member choice.** One row per person and kind in a member preference
   table (today: `prompt_memory_preferences`, `gateway/admin_store.py:236`),
   with `keep` and `retention`.

The shape of `registry/retention.yml` (illustrative values; the
request-history default is Tim's decision 1):

```yaml
format_version: 1
retention_choices:          # shared vocabulary; seconds are data, not code
  never: null
  1_hour: 3600
  1_day: 86400
  7_days: 604800
  30_days: 2592000
  3_months: 7776000
  1_year: 31536000
kinds:
  library_prompt:
    purpose: Library keeps the words that made each thing you created, so you can find it and make it again.
    default_keep: true                # today's value (prompt_memory.py:33)
    choices: [never, 3_months, 1_year]
    default_retention: never          # today's value
    parts: {allowed: [accepted], default: [accepted]}
    readers: maker                    # see section 4
  conversation:
    purpose: An app can ask Azizam to keep a conversation so it can continue it later.
    default_keep: true                # today: Responses store defaults on with a person
    choices: [never]                  # until explicitly deleted, as today
    default_retention: never
    parts: {allowed: [accepted, received], default: [accepted, received]}
    readers: maker
  request_history:
    purpose: Request history keeps what your apps sent for a short time, so you can check it and run it again.
    default_keep: false               # TIM DECISION 1 (section 18)
    choices: [1_day, 7_days, 30_days]
    default_retention: 7_days
    parts: {allowed: [accepted, received], default: [accepted]}
    max_record_bytes: 1048576
    readers: maker
  debug_capture:
    purpose: A Debug window keeps what went to one engine and what came back while you set it up, then deletes it.
    default_keep: false               # only inside an open window
    choices: [1_hour, 1_day, 7_days]
    default_retention: 1_day          # counted from the window's end
    window_choices: [5_minutes, 15_minutes, 1_hour]
    parts: {allowed: [accepted, sent, received], default: [accepted, sent, received]}
    max_record_bytes: 4194304
    readers: maker
widening: none                        # TIM DECISION 3: the only legal value today
disclosure:
  never_save: retention.never_save    # copy key; text in the UI catalog
  content_private: retention.private  # TIM DECISION 2 changes this copy
```

The validator (core) refuses these:

- A `readers` value outside the core list (section 4).
- A `parts` default outside `allowed`.
- A `default_retention` outside `choices`.
- A choice name missing from `retention_choices`.
- `widening` other than `none`.

The owner's household policy goes through the same validator. Each refusal
names the rule it broke ([research] R3, last paragraph).

### 3.3 One decision, in a fixed order

`retention.decide(kind, request, now)` replaces `prompt_memory.decide`. It is
a pure function of the request's maker, the request's extension, the
household policy, the member's choice, the bundled defaults and any open
Debug windows. It returns `Decision(kind, keep, reason, kept_until, parts)`.
The order is the order today's code uses (`prompt_memory.py:142-159`), with
two kinds added:

1. Kill switch on: `server_disabled`.
2. The request forgets (`com.azizam.prompts.mode=forget`, or Responses
   `store:false` for `conversation`): `request_forget`.
3. No person: `no_person`. There is no maker to read the content
   (`PROMPT_MEMORY.md` "No-key LAN calls ..."). The one existing exception is
   kept: a no-person Responses request with a stable App or credential scope
   and an explicit `store:true` (`CORE_CONTRACTS.md:620-622`).
4. `debug_capture` only: no open window whose opener is this person and
   whose engine is the dispatched engine: `no_window`.
5. The member's `keep` is false: `member_disabled`. A member with no row
   gets the household `default_keep` for that kind, snapshotted at rollout
   or user creation, as today (`PROMPT_MEMORY.md` "Member preferences").
6. Otherwise keep. `kept_until` is the request time plus the shorter of the
   member's retention and the household cap. `parts` is the intersection of
   the household parts and the kind's allowed parts.

A decision is made before anything persists. That covers the database, the
execution spool, the STT workspace and anything sent to LiteLLM
(`PROMPT_MEMORY.md` "Code challenges": an off request is memory-only).

### 3.4 One per-request control (stable names)

The control stays `extensions["com.azizam.prompts"]`, and its one value stays
`mode: "forget"`. It now covers all four kinds: nothing about this request is
kept as content. The stable-names rule favours extending the existing control
over adding a second one ([handoff] I8; Identity opinionated item 5,
`PRODUCT_STRATEGY.md:25`).

- **Where it is read.** JSON bodies on every data-plane route, which extends
  today's media, speech and Responses routes (`EXTENSION_TARGETS.md:13`) to
  Chat Completions, completions, embeddings, rerank, and the Anthropic and
  Ollama frontends. Multipart requests use the existing form field
  `extensions[com.azizam.prompts][mode]` (`proxy.py:23260-23265`).
- **New: `X-Azizam-Prompts: forget`.** This is the one new name. It is a
  request header for bodies the gateway does not parse (the native lane). It
  is needed because those bodies cannot carry an extension. It sits in the
  gateway's own `X-Azizam-*` namespace (D45 "Headers"). The gateway strips it
  before dispatch.
- **It only reduces.** A request cannot turn keeping on, lengthen a deadline
  or add parts. Widening comes only from the maker's settings. Otherwise
  anyone who holds a personal key could store content against the maker's
  choice. The research proposal says the same: "per-request/member choices
  can reduce retention" ([research] R5). A `mode: "keep"` value was
  considered and rejected for the same reason.
- **Validation is unchanged.** Any other value is `400 invalid_prompt_mode`
  (`prompt_memory.py:68-81`). The extension never reaches the engine.

### 3.5 What a record can hold: parts and the secret floor

A record holds at most three parts. Each is captured at a boundary that
request-shape receipts already name (`gateway/backends/request_shape.py`:
`accepted`, `backend_egress`, `terminal`):

- `accepted` is the request as the gateway accepted it, after
  authentication and validation and before transforms.
- `sent` is the body the gateway sent to the engine, after transforms, plus
  the destination (D42's `destination`: scheme, host, port and path, with no
  credential). Headers appear as names only. A header that carries a stored
  credential shows as "set from the stored key" with no value.
  - For a local engine behind LiteLLM, `sent` is captured at the gateway's
    internal hop (`/internal/litellm/local/{backend}/v1/...`,
    `proxy.py:29440`). That is the true engine egress.
  - For a remote provider behind LiteLLM, the gateway sees only what it gave
    LiteLLM. The part says so: "before LiteLLM's translation for the
    provider".
- `received` is the reply's status, the allowlisted headers (D45
  "Headers") and the body, up to `max_record_bytes`. A streamed reply is
  assembled up to the same bound.

The secret floor is core code. Data cannot change it ([research] R3; [handoff]
I10):

1. **Content kinds exist only on data-plane routes.** The gateway's own
   security operations have no capture point at all: sign-in, invites,
   resets, keys, secrets, OAuth and MCP grants. This is a structural rule,
   not a list a scanner keeps.
2. **The gateway's own authentication material is never captured.** That is
   the header, query parameter or form field from which `authenticate_request`
   (`proxy.py:2325`) read the key. Request headers are kept only from a fixed
   allowlist of non-credential names (content type, accept, the
   `X-Azizam-*` request controls that carry no credential).
3. **Outbound credentials are never in a part.** `sent` is captured before
   `runtime_secrets` attaches stored credentials (D26, D38, D42: a request is
   built once and credentials are attached at the moment of use).
4. **URLs inside a body keep scheme, host and path.** User info and query
   values are replaced by `[removed]`, and query names are kept. A signed
   URL is a bearer credential ([research] R6; `PROMPT_MEMORY.md` "OpenAI
   image URLs are bearer capabilities"). The record marks this, so Run
   again knows the record is incomplete.
5. **Large media is not copied.** A `data:` URL, or a base64 string over
   64 KiB, becomes `{"omitted": "media", "media_type": ..., "bytes": N}`.
   Multipart file parts are listed by field name, media type and size only.
   There is no content hash: a hash is derived content ([research]
   "Proposed classification", row "Derived content"). Media that is kept
   lives in Library, under `library_prompt`. A blob store for request inputs
   ([research] R6) is not part of this design. Slice 9 lists it as a later
   need.
6. **Pasted secrets cannot be detected.** No rule can prove that a message
   holds no pasted secret. The UI says so, and offers forget and "never
   save" ([research] R3, second paragraph). Azizam ships no regex "secret
   detector", because a detector that misses would be a control that does
   less than it says.

**Classification is per body, not per field.** Rules 1-5 are generic. They
use no knowledge of an API shape, so any API shape, including one that
arrives tomorrow, is covered with no new data. Field-level selectors
(JSON pointers on a protocol description, [research] R2) are an extension
for later, if a real shape needs a field dropped. Building them now would be
machinery on speculation (`AGENTS.md:39`). This is a deliberate change from
R2's full classifier, made for simplicity. A reviewer should check it.

### 3.6 Storage and the one write path

- `library_prompt` and `conversation` keep their tables
  (`asset_prompts`, `responses`). Their schemas do not change, and no row
  moves.
- `request_history` and `debug_capture` use one new table, `kept_requests`,
  in a new store `data/kept_requests.db`. It opens through `sqlite_startup`,
  so it gets the shared lock, quarantine and corruption-only recovery
  (`docs/ops/DATA_RETENTION.md`, last sections), and `secure_delete`. Its
  columns:
  - `record_id`, `request_id`, `kind`, `person_id`, `app_id`
  - `engine_id`, `debug_window_id`
  - `created_at`, `kept_until`
  - `parts_json`, `omissions_json`, `policy_version`
  - `linked_asset_ids`
  - a small structural summary: route, model, status, latency, so that a
    record still reads well after its receipt is pruned
- There is **one writer** for all four kinds, `retention.write(decision,
  ...)`. It extends today's `retention_guard`
  (`prompt_memory.py:162`). In one write transaction it re-reads the kill
  switch, the member's choice and the erasure cutoffs under the
  `admin.db` write lock, and refuses a write that a later setting forbids.
  The existing `prompt_memory.put` and the Responses writes
  (`proxy.py:26875,26960`) call through it. New kinds are written once, at
  the request's end (when the receipt finalizes), so a cancelled or failed
  request writes at most one record and the check is done once, late.
- A write that fails never claims success. The receipt's `retention` field
  (section 13.3) is set from the writer's result, after the commit
  ([research] R1, second paragraph).

## 4. Reader scope (owner task 2)

**The core floor.** Content of every kind is projected only after this
equality check: `record.person_id == viewer.person_id`. The viewer's person
comes from one of three places:

- a browser session or OIDC sign-in;
- a personal App key minted by its owner (`admin_runtime.py:2294`,
  `person_for_app_credential`);
- an MCP grant made by that person (`proxy.py:2413`).

These have no person and can never read content: the owner role, admin and
automation keys, service Apps, keys minted for someone else, trusted
addresses and signed URLs. This is the existing prompt-memory rule, extended
to the new kinds (`CORE_CONTRACTS.md:610` "The owner role grants no prompt
access through these APIs"; `PROMPT_MEMORY.md` viewer table;
`HEADLESS_AND_RESOURCES.md` principle 4: "owners see attribution, never
prompt content"). The check runs in the shared read function, not in each
route. It covers:

- list, search and direct-id reads;
- export;
- Run again;
- MCP results;
- receipts' links to content.

[reach] "Enforce the rule in the shared authorization core" names the same
coverage.

**Data may narrow it.** `readers` takes values from a core list:

- `maker` (the floor, and the most it allows today);
- `maker_signed_in` (only a browser session or MCP grant; the maker's App
  keys cannot read the content).

A household or member value of `maker_signed_in` is a real narrowing.
Today, sharing a personal key shares the maker's prompts
(`CORE_CONTRACTS.md:601-603`).

**Widening is one marked extension point.** It is the function
`retention.reader_allowed(record, viewer, policy)` together with the data
value `widening`. Today the only legal value is `none`. The validator refuses
anything else, so no control in the UI does nothing. Tim's decision 3 selects
what, if anything, comes next (section 18). Each possible answer is one named
slice with its own code, because a widening rule needs a notice, an audit
trail and a revocation path that do not exist yet. Shipping a data value with
no code behind it would be a control that does nothing. This is the honest
limit of "any answer is a data change".

**Server authority stays visible.** The owner runs the machine and can read
its database files, or sign in as a member through a reset link. The UI says
this in the ledger and in the disclosure, as prompt memory does today
(`PROMPT_MEMORY.md` "The owner can read databases ..."; [reach] "Draft member
copy").

## 5. Where request content can land, and LiteLLM (owner task 3)

### 5.1 The inventory

Every place below is named in the "never save" disclosure and in the canary
test (section 16.2). "Azizam controls" means Azizam's code decides whether
the place gets a copy.

| # | Place | When | Azizam controls | Under forget / never save |
|---|---|---|---|---|
| 1 | `asset_prompts`, `responses`, `kept_requests` | per decision | yes | no write ([research] R4) |
| 2 | Receipts | always | yes | structural only; the guard extends to values (16.2) |
| 3 | Execution spool `data/image_media_jobs/` | async media jobs | yes | memory only, as today; the job cannot resume after a restart |
| 4 | STT workspace `data/stt-spool/` | long audio | yes | **today it writes to disk even under forget.** Slice 6 moves it to the gateway's private temp directory (row 5) and the copy names it. A memory-only STT path is a later choice: long audio would cost too much memory (UNGROUNDED size limit). |
| 5 | Multipart spill over 1 MiB | uploads | yes, through the temp directory | slice 6 sets the temp directory to a gateway-private directory (`TMPDIR`, mode 0700, emptied at start) and the unit sets `PrivateTmp=yes`. A deleted temp file can stay in free disk blocks; the copy says "a temporary file, deleted when the request ends". |
| 6 | Gateway logs (journal) | always | yes | D43/D44: no request or upstream text in logs. The canary test checks the journal capture. |
| 7 | Engine output files `data/processes/<id>.log` | engines the gateway starts | the file, yes; what the engine prints, no | the disclosure names it when the engine record declares a prompt-printing log level (an engine fact, section 6). `log_max_bytes` is existing record data. |
| 8 | LiteLLM process, its stdout and its database | chat-shaped requests | yes, through the generated config and environment | section 5.2. **Today it is not clean**: error rows and the ERROR log line can hold echoed prompts. |
| 9 | The engine's own history | engine-dependent | no | detect-and-advise (section 6) |
| 10 | Provider retention | remote providers | no | provider fact (section 6) |
| 11 | Swap, hibernation image | any memory | no (host) | the disclosure names it ([research] OS1) |
| 12 | Crash dumps | a crash | partly: the shipped unit | `LimitCORE=0` in the shipped `azizam.service`, for the gateway and its LiteLLM child. It is Azizam's own process, not a change to the owner's system. systemd-coredump host settings remain the owner's ([research] OS2). |
| 13 | Backups, WAL, file-system snapshots | host | no | as today: no forensic erasure promise (`PROMPT_MEMORY.md` "Database deletion uses SQLite secure_delete ...") |

### 5.2 LiteLLM keeps no content

LiteLLM is Azizam's own execution layer (`PRODUCT_STRATEGY.md:197`). Its
sinks are Azizam's sinks ([handoff] F1 and I4; [research] GW-L9). The policy
runs in the gateway before the request reaches LiteLLM. LiteLLM itself must
keep nothing, whatever the policy says. Then no retention choice depends on
LiteLLM's behaviour. "Forget" has nothing to switch off there, and LiteLLM
needs no per-request flag.

The facts below come from the LiteLLM survey for this design (2026-10-19).
It read the installed source (`litellm[proxy]==1.86.2`, `requirements.txt:11`;
`site-packages` paths are in the survey notes) and ran an in-process canary
harness with no ports and no database. The names of the live process's
environment variables were read; their values were not. Three facts drive the
design:

1. **The live LiteLLM has a database.** `DATABASE_URL` reaches it from
   `.env.local` through `start.sh:9-12`, and the sidecar passes the whole
   environment to the child (`gateway/provider_sidecar.py`). Azizam needs
   that database for LiteLLM virtual keys
   (`docs/LITELLM_SUBSTRATE_DECISION.md` "Current state: key ownership").
   So the design cannot simply remove it. It must turn off the content
   tables inside it. No Azizam code reads LiteLLM's spend logs (grep of
   `gateway/`, `proxy.py` and `web/src`, 2026-10-19).
2. **`turn_off_message_logging` does not stop process logs**, in 1.86.2 or
   by design. The `raw_request` fix is in 1.105.0 ([research] GW-L6,
   GW-L12). In the measured harness, a provider error that echoed the
   request put the canary in 4 stdout lines at the default level. Only
   `LITELLM_LOG=CRITICAL` removed them. `redact_messages_in_exceptions` did
   not.
3. **Database rows can override the YAML.** `LiteLLM_Config` rows apply when
   `store_model_in_db` is true, and the database value of
   `store_model_in_db` can itself force that on ([research] GW-L3).

The generated config (`compile_litellm_substrate_config`,
`gateway/litellm_substrate.py:101-116`) and the child's launch environment
state every switch explicitly, even where the default is already off. A later
upstream default change then cannot turn a sink on.

| LiteLLM sink (1.86.2) | What Azizam sets | Today | Ground |
|---|---|---|---|
| Spend rows: messages, response, `proxy_server_request` | `general_settings.store_prompts_in_spend_logs: false`; `disable_spend_logs: true` (key, user and team spend totals still update, so virtual-key budgets keep working; slice 3 tests this) | unset; spend rows written to the live database | GW-L2, GW-L10; survey |
| Error rows (`metadata.error_information`: provider error text, capped; only the keys `input`, `messages` and `prompt` are redacted) | `general_settings.disable_error_logs: true` | unset; written | GW-L2; survey |
| Database settings that override YAML | `general_settings.store_model_in_db: false`, plus the effective-settings check below | unset | GW-L3; survey |
| Callbacks (`callbacks`, `success_callback`, `failure_callback`, key and team callbacks) | `[]` for each; Azizam creates no key with callback metadata | unset (empty); no environment variable enables them in 1.86.2 | GW-L1, GW-L10; survey |
| Message content in logging objects | `litellm_settings.turn_off_message_logging: true`; `redact_messages_in_exceptions: true`; `redact_user_api_key_info: true` (defence in depth only) | unset | GW-L1, GW-L12 |
| Process stdout and stderr | `LITELLM_LOG=CRITICAL` in the child environment; never `--debug`, `--detailed_debug`, `DEBUG` or `DETAILED_DEBUG` | unset: ERROR lines can carry echoed prompts | GW-L11; survey (measured) |
| Per-request debug and no-log fields (`litellm_request_debug`, `no-log`, `success_callback` in a body) | nothing new: the gateway's body allowlist already drops them (`gateway/backends/llm_execution.py:38-124,245-268`); a test pins it | blocked | survey |
| Response cache and Responses polling cache | `litellm_settings.cache: false`; no `cache_params`; `router_settings.cache_responses: false` | unset (off; `litellm.cache` is `None`, measured) | GW-L8, GW-L10 |
| `.env` auto-load by LiteLLM | `LITELLM_MODE=PRODUCTION` | unset (`DEV` loads a `.env` found by walking up; none exists today) | survey |
| Admin UI and API docs | `DISABLE_ADMIN_UI=true`, `NO_DOCS=true`, `NO_REDOC=true` (the `/ui` files stay mounted on 127.0.0.1 behind the master key) | unset | GW-L3; survey |
| Uploads over 1 MiB | `TMPDIR` set to the gateway's private temp directory (slice 6) | system temp | Starlette `formparsers.py:126,209` |
| Image-URL conversion cache (10 entries, 600 s, memory) and other memory caches | none; disclosed under "memory" | n/a | GW-L7; survey |

**Launch environment.** The child gets an allowlisted environment instead of
the gateway's whole environment. The allowlist holds:

- what LiteLLM needs to run;
- the provider keys the sidecar injects;
- `DATABASE_URL` only while virtual keys need it;
- the values above.

Then a stray `STORE_PROMPTS_IN_SPEND_LOGS`, `LANGFUSE_*`, `OTEL_*`,
`LITELLM_LOG` or `DEBUG` in `.env.local` cannot reach LiteLLM.

**The cost of `LITELLM_LOG=CRITICAL`.** LiteLLM's own error lines leave the
journal. The gateway's own lines stay: the status, error class and origin of
each failure (D43 `describe_*`). So the gateway's log becomes the record of
LiteLLM failures. This is a deliberate trade: the journal is host-only (D45),
but "never save" must also be true there.

**Effective-settings check.** Database rows can override the YAML. So after
each LiteLLM start the gateway reads LiteLLM's effective general settings
through its master-key settings API, as the virtual-key admin client already
does for `/key/*` (`gateway/admin_api.py:291,2870`). It compares them with
the no-content profile.

- **On a mismatch** (or when the check cannot run), the engine page shows an
  attention item: "LiteLLM may keep request content: <setting>".
- The "never save" disclosure then adds a line: "LiteLLM, which runs chat
  for Azizam, may keep a copy."
- Chat keeps working.

This is honest without changing routing. Failing closed was considered: it
would stop all chat on a setting the owner may not know about. UNGROUNDED:
the exact LiteLLM 1.86.2 route that returns effective settings. Slice 3 finds
it in the installed source before it relies on it.

**What is already stored is unknown.** Rows that LiteLLM wrote to the live
database before slice 3 stay there. Turning switches off does not remove old
rows ([research] GW-L3, "no retroactive cleansing"). How many exist is not
known: counting them needs a read-only query of the live database, which
needs Tim's OK (section 19). Deleting them is the owner's choice, not a
migration step.

**Not request content, but identity leaves the machine.**
`forward_client_headers_to_llm_api: true` (set today for cancel correlation,
`litellm_substrate.py:108-115`) also sends the `x-azizam-app` and
`x-azizam-key-prefix` headers to remote providers (measured). The ledger
names this. Slice 3 proposes sending only `X-Azizam-Request-Id` to LiteLLM,
after it confirms the internal hop needs nothing more.

**Proof.**

1. A config test compiles the config from a fixture registry. It asserts the
   values above and a closed set of allowed keys in `litellm_settings`,
   `general_settings` and `router_settings`. A new sink key fails the test
   until it is classified, in the spirit of D28 and D34.
2. A launch-environment test gives the sidecar's environment builder a
   poisoned environment and asserts that none of those variables reach the
   child: `STORE_PROMPTS_IN_SPEND_LOGS`, `LANGFUSE_PUBLIC_KEY`,
   `OTEL_EXPORTER_OTLP_ENDPOINT`, `LITELLM_LOG=DEBUG`, `DEBUG=1`,
   `DATABASE_HOST`.
3. The in-process canary test is described in section 16.2.
4. The admin preview (`GET /admin/api/operator/litellm-config`, D30) shows
   the content switches and the last effective-settings check.

### 5.3 "Never save" copy that stays honest

The disclosure is data (`disclosure.never_save`). It is assembled from facts,
not written as one promise. This is the default text with no engine facts
added (draft; the UX review owns the final words):

> Azizam does not save this request: not in its database, files or logs.
> While the request runs, it is in this computer's memory. The computer can
> copy memory to its swap space, to a hibernation file or into a crash
> report. A large upload passes through a temporary file that is deleted
> when the request ends.

Facts are added for the engine and provider the request goes to (section 6):

> ComfyUI on this computer keeps its own history of up to 10,000 requests.
> Anything that can reach its port can read or clear that history. Azizam
> cannot delete it.

and

> OpenAI keeps prompts and outputs for up to 30 days to check for abuse
> (checked 2026-10-19).

Words such as "only you", "end-to-end" and "erased" never appear unless they
are true ([reach] "Draft member copy"; [handoff] F4).

## 6. Facts about other systems are data (owner tasks 5 and 6)

Both kinds of fact use one schema, `retention_fact`:

```yaml
- id: abuse-monitoring
  holds: prompts and outputs          # plain words; who/what is kept
  where: provider abuse-monitoring store
  how_long: up to 30 days             # or "unknown"; never a guess
  default: on                         # on | off | conditional
  condition: null                     # e.g. "when started with a request-recording setting"
  reach: provider staff on review     # who can read it, as stated by the source
  control: ask the provider for modified abuse monitoring
  evidence_url: https://...
  checked: 2026-10-19                 # the date someone read the source
  confidence: documented              # documented | inferred | unknown
```

### 6.1 Provider retention facts (task 5)

- **Where.** A new optional list `retention_facts` in each
  `providers/catalog/<id>.yml`. The loader in `gateway/provider_catalog.py`
  validates it against the schema above. Today the loader silently ignores
  unknown keys (repo survey, section 9). This key is validated, not ignored.
- **Who updates.** The bundled catalog is updated by a PR, like any catalog
  change. The owner can add, edit or remove facts for a configured provider
  in the provider's settings (DB). The ledger shows the owner's version and
  labels it "set by the owner". A removed bundled fact does not come back
  after an upgrade: an explicit empty list is kept ([research] R2 "No hidden
  vendor defaults that reappear after removal").
- **Staleness.** The ledger shows `checked`. A fact older than a
  data-defined age (`stale_after_days` in `registry/retention.yml`, default
  180) shows "this was last checked on <date>". Azizam never fetches the
  source automatically: no network calls on read ([research] R8 "Viewing the
  ledger must never resend the request").
- **History.** A receipt records the `id` and `checked` date of each fact in
  force when the request was sent. A later catalog change then does not
  rewrite what the ledger said for an old request ([research] R8 "preserve
  the evidence date and exact route/model/config used").
- **Seed.** The seed comes from the corrected research: PT-O1 for OpenAI,
  GW-O1 to GW-O3 for OpenRouter, PT-A* for Anthropic, PT-G* and PT-VX3 for
  Gemini through the OpenAI-compatible route. PT-VX3 records implicit
  caching on by default for supported Gemini models. Only claims marked
  HIGH are seeded. UNVERIFIED claims are seeded as `how_long: unknown`.

### 6.2 Engine-kept history (task 6)

A `retention_fact` can sit at three data levels. The most specific level
wins:

1. **The protocol description** (`registry/protocols/<id>.yml`), for
   history that comes with the API shape. `node-graph` gets:
   - `holds: submitted graphs, their inputs and outputs`
   - `how_long: until cleared; up to 10,000 entries`
   - `reach: anything that can reach the engine's port; no sign-in in a default install`
   - evidence L13 and L14

   The protocol already names this history (`graph.history`,
   `graph.delete_history`, `registry/protocols/node-graph.yml:22-30`).
2. **The engine definition** (`registry/engine_definitions.json`), for
   history that depends on a server's launch settings. One example is a
   server that records request bodies to temporary files when started with
   a request-recording setting (`default: off`, `condition: ...`;
   [research] addendum, Ollama `OLLAMA_DEBUG_LOG_REQUESTS`). The definition
   is matched by the existing detection rules, which name API shapes, not
   engines (`registry/engine_definitions.json` "notes").
3. **The engine record** (`config.retention_facts` on the service record).
   The owner edits it on the engine page, for example: "I start it with
   request recording on", or "my install has no history". An explicit empty
   list overrides the levels above it.

**Reach is part of the fact.** L14 shows that the history is readable and
clearable by anything that can reach the port. So the ledger joins the fact
with what Azizam already knows about the engine's address, from the engine
check's `is_this_computer` and destination checks
(`gateway/engine_check.py:147,191`):

- "on this computer only" when the address is a loopback address;
- "on your network" otherwise.

The engine's address is something Azizam knows. The engine's bind address is
not. So the text says "Azizam reaches it at <address>", not "only this
computer can reach it". UNGROUNDED: whether loopback-only reach can be
proven from the gateway side. It cannot be proven in general, so it is not
claimed.

**No purge code.** Azizam does not clear engine history. This is
detect-and-advise: Identity "a repairer of broken engines (it detects and
advises)", `PRODUCT_STRATEGY.md:44`; [handoff] I6. The advice is a fact
field, `control`, for example: "Clear it in ComfyUI, or start ComfyUI with
history kept in memory only". One existing behaviour is kept and not
widened: a maker's explicit video DELETE also asks the engine to drop that
one job's history entry, through the protocol's declared
`graph.delete_history` operation (`proxy.py:12320-12328`). It is data-driven
(a protocol operation, no engine name) and acts only on the maker's own job.

**The boundary guard stays green.** Facts are data files and DB fields. The
code that reads them names no engine. `tests/test_engine_knowledge_boundary.py`
and its ratchet do not move.

## 7. The ledger: who could see it

The ledger is one read-only view, with no network calls, built from data the
gateway already has ([research] R8). It appears in three places:

- next to each kept record;
- in the settings disclosure;
- in the "never save" text.

It answers, in plain words:

1. **Who can read it in Azizam.** "Only you", plus "your App keys" when
   `readers: maker`.
2. **Who runs the machine.** "The owner, <name>, runs this computer and can
   read its files."
3. **Where it went.** The engine or provider, by display name. It shows the
   destination's provider facts and engine facts with their checked dates
   and their reach.
4. **What Azizam keeps.** Each kind kept for this request, with its deadline.
   Omitted parts show with their reason.
5. **What Azizam cannot control.** Rows 9-13 of section 5.1.

It distinguishes what Azizam observed ("Azizam sent this to <engine> at
14:02") from what a source declares ("the provider says it keeps prompts for
up to 30 days") and from what is unknown. It never says that a provider
deleted something.

## 8. Layered UX: three views of one contract (owner task 7)

The UI has three layers. Each one reads and writes the same household policy
or member choice object (section 3.2). None has state of its own (Identity,
`PRODUCT_STRATEGY.md:53-56`).

1. **Presets** (registry picks). These are named policy objects in
   `registry/retention.yml` under `presets`:
   - **Keep nothing:** every kind off, the forget default for Try.
   - **Library only:** today's behaviour, and the default after migration.
   - **Library and 7 days of request history.**
   - **Library and saved conversations, no request history.**

   Picking a preset writes its values. When the stored values match no
   preset, the picker shows **Custom**. The UI never pretends a preset is
   still in force.
2. **Guided form.** One row per kind: its purpose sentence, an on/off
   switch, the retention choice and, for request history, "also keep
   replies". Under each row the form shows the ledger preview for a typical
   request (section 7) and the consequence of the change in plain words. For
   example: "Turning this off deletes 41 kept requests now. This cannot be
   undone." The owner's form adds the kill switch, the caps and
   `readers`. A member's form shows the owner's caps as limits, not as
   errors.
3. **Full data editing.** The policy object as YAML or JSON, with the
   schema and the validator's plain refusals. It is the same object the
   REST API returns.

The essential complexity is four kinds × (keep, retention, parts, readers) ×
three layers of authority. The UI hides it this way:

- A member sees two switches: Library and request history. Conversations
  are an app's choice. Debug captures exist only inside a window.
- The owner sees one form.
- Only the expert layer shows parts and readers.

## 9. Debug window (owner task 8)

**Purpose.** The owner sets up a new engine or API shape and needs to see
what Azizam sent and what came back (Identity "Can an owner support a new
engine, vendor API or wiring today?", `PRODUCT_STRATEGY.md:50-51`; [handoff]
I9).

**Where.** The engine check result on the Add engine and engine pages has a
button: **Watch the next requests** (`web/src/app/engines/*`). It sits next
to the real test (`gateway/engine_admin.py:1490,1508`). That is one step
from the check.

**What it is.** A row `debug_windows`, with these fields:

- `id`
- `opener_person_id`
- `engine_id`
- `opened_at`, `ends_at`, chosen from `window_choices`
- `parts`
- `closed_at`

While a window is open, decision step 4 keeps `debug_capture` records for
the opener's own requests to that engine. The records carry all three parts.
They expire `default_retention` after the window ends. A window can be
closed early. Closing a window or deleting its records uses the normal
erasure path.

**Rules (all in core):**

- **Openers.** Only a person with the engine-admin role (owner or
  administrator, the roles that may run the engine check:
  `owner_openapi_routes.json:458-466`) can open a window. The opener must be
  a person. Automation keys have no person and are refused
  (`409 debug_window_needs_person`).
- **Reader floor.** A window captures only requests whose maker is the
  opener: requests from Try, from the opener's personal App keys or from
  their MCP grant. It never captures another member's requests or no-person
  traffic, such as a household LibreChat on a service App. That traffic can
  carry other people's words (section 4). To debug a shared app, the owner
  points it at a personal key for the session. The UI says so in one
  sentence.
- **Secrets.** Parts follow the secret floor (section 3.5). The `received`
  part of an error reply follows the engine check's existing rule
  (`engine_check.guarded_fetch`, `gateway/engine_check.py:598`; D44
  "Limits"):
  - The owner sees the engine's words, with every spelling of a registered
    secret removed.
  - An administrator whose request went out with the owner's stored key
    sees status and shape only, with the body withheld.
  - Success replies are the trusted engine's product and show as sent
    (D45b).
  - The view is a management output, so it leaves through the one egress
    (D31). Part bodies are a new projection class `MakerContent`. It is
    emitted only to the record's maker after the equality check and is
    forbidden for every other audience (D28 default Forbidden).
- **Honesty.** While a window is open, the header of every page the opener
  sees shows "Debug window open for <engine>, 12 minutes left". The receipt
  of each captured request says `retention.kind: debug_capture`.

## 10. Surfaces: REST, MCP and CLI for every control (owner task 8)

Every control is in one support matrix. Supported surfaces get the same
semantic tests. A route inventory test fails when an operation is missing
from the matrix (`HEADLESS_AND_RESOURCES.md` "Support matrix, not a parity
ritual"; Identity item 6).

**Who may change what.** Any maker credential can reduce: turn off, shorten,
delete, close a window. Widening needs the person: turn on, lengthen, add
parts, open a window. The person can be a browser session, an MCP grant, or
a personal App key minted with **May change my privacy settings**.

That last option is the one new key property: a boolean chosen when the key
is minted. Minting already needs a recent sign-in (D36). It defaults to off.
Without it, a leaked personal key could turn history on, capture the maker's
requests from every other device, and read them later. The asymmetry is the
same as for the per-request control (section 3.4).

| Control | REST | MCP | CLI (Restish over OpenAPI) |
|---|---|---|---|
| Household policy: read, write, preset | `GET/PUT /admin/api/settings/retention` (`If-Match`) | tool `retention`, action `household_get` / `household_set` (owner person) | `restish azizam get-retention-settings` / `put-retention-settings` (owner key) |
| My choices | `GET/PUT /me/api/retention` (session); `GET/PUT /gateway/v1/me/retention` (personal key; PUT that widens needs the key property) | `retention` `mine_get` / `mine_set` | `restish azizam-app get-my-retention` / `put-my-retention` |
| My kept requests: list, read, delete, delete all | `GET /gateway/v1/requests`, `GET /gateway/v1/requests/{id}`, `DELETE ...{id}`, `DELETE /gateway/v1/requests` | tool `requests`, actions `list` / `get` / `delete` / `delete_all` | `restish azizam-app list-requests` etc. |
| Library prompt delete (existing) | `DELETE /me/api/prompt-memory/prompts` becomes `DELETE /gateway/v1/requests?kind=library_prompt` | `requests` `delete_all` with `kind` | same |
| Ledger | `GET /gateway/v1/requests/{id}/ledger`; `GET /gateway/v1/me/retention/ledger` | `requests` `ledger` | `restish azizam-app get-request-ledger` |
| Debug window: open, close, list, read | `POST /gateway/v1/debug-windows`, `DELETE .../{id}`, `GET ...`, `GET .../{id}/requests` | tool `debug_window` | `restish azizam-app open-debug-window --engine <name> --minutes 15` |
| Run again | `POST /gateway/v1/requests/{id}/run-again` (with `dry_run`) | `requests` `run_again` | `restish azizam-app run-again <id> [--dry-run]` |
| Forget one request | `extensions["com.azizam.prompts"]`, `X-Azizam-Prompts` | the `run` tool accepts `forget: true` | any HTTP client |
| Provider and engine facts | provider and engine record routes (existing), new field | read through `requests` `ledger` | existing record commands |

The new maker routes are under `/gateway/v1/`, next to receipts
(`gateway/api.py:550,585`). They are part of the OpenAPI document that
Restish reads (`docs/ops/HEADLESS_ACCESS.md`), so a personal key works from
the CLI. Today's `/admin/api/settings/prompt-memory` and
`/me/api/prompt-memory` are replaced, not shimmed. Azizam is unreleased, and
these are Azizam's own routes, not caller contracts. The caller-facing name,
`com.azizam.prompts`, stays. Three MCP tools are added (`retention`,
`requests`, `debug_window`). This is a deliberate change to "no new tools
yet" (`HEADLESS_AND_RESOURCES.md` Release B item 5). Parity for a privacy
control is the demonstrated need. Each tool groups actions instead of having
one tool per endpoint.

## 11. Run again (owner task 9)

UI name: **Run again**. API: `run-again`. Not "replay": in this repo,
"replay" already means request-id idempotent replay of media jobs
(`docs/ops/DATA_RETENTION.md` "Replay window"; `media_jobs.REPLAY_WINDOW_S`).

**Viewing is offline.** Opening a record, the ledger or the editor makes no
call to an engine, fetches no media and runs no graph. Stored content renders
inertly ([research] R7).

**Run again is a new request.** It sends a new request through the front
door's normal path, as the person who clicks it: a new `request_id`, with
`replay_of: <source request_id>` on its receipt. It does not resend stored
bytes past any check:

- **Permissions.** The caller must be able to read the record (section 4),
  and must be allowed the target now, under today's access plan and model
  aliases. Credentials come from current custody. A record never holds any
  (section 3.5).
- **Incomplete records.** A record with omitted media, removed URL parts or
  an expired blob is incomplete. The editor names what is missing and asks
  for it. It never fetches a stored URL again ([research] R6).
- **Cost.** Before sending, the confirm step shows the cost estimate from
  the same estimator dispatch uses (`providers.estimate_request_cost`,
  `gateway/providers.py:862`). Spend caps are enforced by the normal
  reservation (`providers.reserve_provider_spend`, `:414`). An alias that
  now points to a paid model is shown as such. The "never fall back from
  free to paid" rule holds (`PRODUCT_STRATEGY.md` "Architecture decisions").
  Cost unknown under a cap is refused, as today.
- **Scheduler and fair sharing** (Identity item 3). Run again goes through
  the one admission gate as any request (`HEADLESS_AND_RESOURCES.md` Release
  A item 5; `resource_admission.advise_resource_admission`,
  `gateway/resource_admission.py:158`). Before confirming, `dry_run`
  evaluates the same pure function and shows the result, labelled "not
  reserved". It can say:
  - "ready";
  - "will wake <engine>";
  - "will wait for <engine>, which is busy";
  - "will unload <other engine> (yours)";
  - "refused: <reason>".

  Run again never unloads another person's engine without the normal
  consent rule (`HEADLESS_AND_RESOURCES.md` Release A item 3). It runs at
  the caller's normal priority and counts toward the caller's concurrency
  and outstanding-job limits (`media_jobs.create(..., outstanding_limits=)`).
  There is no batch run: one record at a time, so history cannot become a
  bulk queue. Dispatch re-checks admission after the preview.
- **Output.** Run again is a new request, so its own content follows the
  current policy (section 3.3), not the source record's. Its output is not
  promised to match the original. Engines and models change, and a seed
  does not guarantee the same output ([research] R7).

## 12. Deletion, expiry, late writes and migration

**Deletion and expiry** reuse the prompt-memory machinery, extended to two
more kinds (`PROMPT_MEMORY.md` "Retention and upgrade"):

- the maintenance loop (`proxy.py:20228,20262`);
- durable erasure intents and cutoffs (`admin_store.py:3512-3560`);
- `secure_delete` with a WAL truncate.

The changes:

- **Cutoffs gain a kind.** A cutoff row becomes `(person_id, kind)`.
  Existing rows become the kinds they covered then: `library_prompt` and
  `conversation`, plus job hashes and spools as today. "Delete all" and the
  kill switch cover every kind.
- **Turning a kind off records an erasure intent for that kind.** It is
  written in the same transaction as the choice, and the kind's records are
  deleted, as memory-off does today. A failed intent write returns `503
  retry_required` and leaves the choice unchanged.
- **Shortening a retention shortens existing deadlines. Lengthening never
  extends them.** This is Azizam's own rule (`PROMPT_MEMORY.md:96`). It
  differs on purpose from LibreChat, which "does not rewrite existing
  deadlines" ([handoff] F6). A household cap change is applied by
  `tighten_retention` (`prompt_memory.py:408`), extended by kind.
- **Late writes.** New kinds are written once at the request's end, through
  the one writer that rechecks the setting and the cutoffs under the write
  lock (section 3.6). A request that started before a cutoff cannot write
  after it. A Debug window closed during a request makes that request's
  capture refused.
- **Expired is deleted.** An expired record is deleted by the loop, not
  hidden. Reads also check `kept_until`, so a record past its deadline is
  never shown, even if the loop is late.
- **Deleting a Library item deletes its linked request-history records**
  (section 3.1).
- **Removing a member erases every kind.** Disabling a member keeps them
  until their deadlines, as today.

**Migration: no silent change.** Slice 1 is a pure move:

1. `operator_settings.prompt_memory` becomes `operator_settings.retention`.
   - `default_remember` becomes `kinds.library_prompt.default_keep` and
     `kinds.conversation.default_keep`.
   - `retention_cap` becomes `kinds.library_prompt.retention_cap`.
   - `never_keep` becomes `never_keep`.
   - The revision counter carries over.
2. The member preference rows get `kind = library_prompt`. Each member also
   gets a `conversation` row with the same `remember` value. Today one
   preference governs both (`prompt_memory.decide` is called for Responses
   at `proxy.py:26730`).
3. `request_history` and `debug_capture` start with no member rows. A
   member with no row gets the household default for that kind (section
   3.3, step 5). So existing members see nothing new being kept unless the
   default keeps it. If Tim chooses a keep default (decision 1), the
   rollout instead snapshots `keep: false` for existing members and shows
   them a one-time notice with the switch. New members get the default.
   Either way, nobody already using Azizam starts having request bodies
   kept without being told.
4. No row in `asset_prompts` or `responses` changes. No `kept_until` moves.
   The proof is the migration test in section 16.1, run on a copy of the
   live `admin.db` and `media_jobs.db`. That is the same rehearsal rule D26
   used.

## 13. How it composes

### 13.1 Sessions and maker identity

The maker is exactly the person `authenticate_request` resolves
(`runtime_auth.RuntimeIdentity.person_id`, repo survey section 13). The
rules are those of prompt memory. Retention adds no new identity rule.

- **A signed-out session cannot widen.** A sign-out that commits before a
  widening write makes the write fail: the D41 pattern, with
  `session_guard.require_live` in the retention writer's transaction. The
  retention settings are not credentials, so D36 step-up is not required
  to change them. Opening a Debug window is not a credential action either.
  UNGROUNDED: whether a Debug window, which lasts at most an hour, should
  need a recent sign-in. Proposed: no, because it widens only to the
  opener's own content.
- **Session-family revocation** (D35, D35b) does not delete content. It ends
  the access path. Open Debug windows of a revoked family close.

### 13.2 Egress rules (D28, D31, D43-D45b)

- Content leaves only through the management egress (D31), as
  `MakerContent` (section 9). The projector builds that class only after
  the person-equality check. Every other audience gets Forbidden.
- Parts hold no gateway-attached credential by construction (section 3.5).
  The noninterference property (D32 e) is tested: two registries that
  differ only in secret bytes give byte-identical records and views.
- No engine words reach a non-owner through the Debug window (D45, D44
  "Limits"). Request history keeps only `accepted` by default, which is the
  caller's own words. When `received` is kept, it is the trusted engine's
  success product (D45b). Error replies keep the gateway's composed error
  (`client_errors`), not the engine's text.
- Logs never carry content (D43/D44). Retention adds no log line with
  record text. Its log lines use fixed event ids and counts.

### 13.3 Receipts

Each receipt gets one structural, client-visible field, `retention`. It is
added to `CLIENT_VISIBLE_FIELDS` (`gateway/receipts.py:29`):

```json
"retention": [
  {"kind": "request_history", "decision": "kept", "reason": "kept",
   "kept_until_ms": 1761523200000, "parts": ["accepted"]},
  {"kind": "library_prompt", "decision": "not_kept", "reason": "request_forget"}
]
```

- `decision` is one of `kept`, `not_kept`, `failed`.
- `reason` comes from the closed set in section 3.3, plus `write_failed` and
  `incomplete`.

A receipt also gets `replay_of` (Run again) and `retention_facts`: the fact
ids and checked dates in force (section 6.1). All values are enums, ids,
counts and times. The receipt never holds content ([research] R1;
`gateway/receipts.py:3-4`).

The receipt store's guard checks key names only (`receipt_store.py:211`;
`redaction.py:163,190`). Slice 2 adds a value check for these fields: each
must be in its enum or be a bounded id.

## 14. AGENTS.md and the extension rung (owner task 11)

`AGENTS.md:36-40` says: "Do **not** build new plugin machinery on
speculation; building a plugin system is itself the treadmill." The Identity
calls "a reviewed, permissioned extension package" the better rung
(`PRODUCT_STRATEGY.md:47-48`), and that machinery exists
(`docs/EXTENSION_ARCHITECTURE.md` "Implemented Installer Shape"). The two
are compatible: one forbids *new* machinery, the other names the existing
package system as the place for code that data cannot express. The proposed
wording makes that explicit. It replaces the last two sentences of the
"NO →" bullet:

> - NO → it's the bad kind. Cut it, or, if it is genuinely irreducible and
>   worth keeping, put it in a reviewed, permissioned extension package
>   (`docs/EXTENSION_ARCHITECTURE.md`). That package system is the one code
>   rung. For code that must stay in-tree, `register_builtin` in
>   `gateway/transforms.py` is the escape hatch, with a reviewed
>   justification. Do **not** build new plugin machinery: no new loader,
>   hook point or plugin kind until a present, concrete need shows that data
>   and the existing package system cannot express it. Building a plugin
>   system on speculation is itself the treadmill.

This design follows that wording. It needs no extension code: the
classification is generic (section 3.5), and every fact about an engine or
provider is data.

## 15. Rejected alternatives

| Alternative | Why rejected |
|---|---|
| Two stores, two contracts | Two defaults for the same words with no shared model ([handoff] F2, I1). |
| Per-field classifier for every API shape now ([research] R2) | Machinery ahead of need. Generic whole-body rules cover any shape (section 3.5). |
| A per-request `mode: "keep"` | Lets a key holder widen against the maker's choice (section 3.4). |
| Owner-editable reader scope | Crosses "who may do what" ([handoff] I2). |
| Purging engine history from the gateway | Per-engine remediation in code; Identity "detects and advises" ([handoff] I6). |
| Relying on `turn_off_message_logging` | The installed 1.86.2 predates the `raw_request` fix ([research] GW-L12), and the switch does not stop process logs (measured). |
| Removing LiteLLM's database | LiteLLM virtual keys need it (`LITELLM_SUBSTRATE_DECISION.md`). The content tables are turned off instead. Revisit when the virtual-key bypass is retired. |
| A memory-only Debug window | Lost on restart, and it would be a second storage path with its own rules. The one store with a short expiry is simpler. |
| `mlock` for "never save" | Not possible for a Python process as a whole, and hibernation ignores it ([research] OS1). The copy is honest instead. |

## 16. Test plan

The oracles are deterministic tests against the real gateway app, its real
SQLite stores and the pinned LiteLLM, not mocks of them (handoff standard).
Each slice lists its own subset in section 17.

### 16.1 Deterministic oracles

1. **Decision equivalence (slice 1 gate).**
   - Enumerate (kill switch × forget × `store` true/false/absent × person
     present × member keep × member retention × household cap × route
     kind). Assert that `retention.decide` equals today's
     `prompt_memory.decide` and the Responses store rule
     (`proxy.py:26797-26804`) on every case.
   - The old functions are kept in the test as the reference until slice 9.
2. **Migration.**
   - On a fixture and on a copy of the live `admin.db` and `media_jobs.db`:
     every `kept_until` is unchanged.
   - Every member's effective decision for the existing kinds is unchanged.
   - Running the migration twice is a no-op.
   - A partial failure rolls back.
3. **Data, not code.**
   - Deleting `registry/retention.yml` fails startup with a plain message.
   - A grep-based test finds no retention default literal in Python or the
     web source. This is the same ratchet style as the boundary guard.
   - Changing `request_history.default_keep` in the file changes the
     decision for a new member, with no code change.
4. **Reader floor.**
   - A cross-principal matrix of the owner, an administrator, another
     member, the maker's personal key, a service App key, an automation
     key, a trusted address and a signed URL.
   - Each is checked against every content route and MCP action: list,
     get, search, ledger, export, Run again, Debug window read.
   - Only the maker's credentials succeed. `readers: maker_signed_in`
     removes the personal key.
   - Ownership is applied before pagination (`HEADLESS_AND_RESOURCES.md`
     Release A item 4).
5. **Widening is closed.** Every `widening` value other than `none` is
   refused by the validator, through REST, MCP and the data file.
6. **Per-request control.**
   - `forget` (body, form field, header) produces no row in any store, on
     every data-plane route.
   - An invalid value returns 400.
   - The extension and the header never reach a fake engine. The existing
     fake-upstream harness asserts the received body and headers.
7. **Late writes and erasure.**
   - A request in flight while the member turns history off, closes a
     Debug window or deletes all, or while the owner enables the kill
     switch: its write is refused.
   - Restart during erasure resumes the intent.
   - Re-enabling does not bring back pre-cutoff rows.
   - Expiry shortens and never lengthens.
   - Deleting a Library item deletes linked records.
8. **Surfaces.**
   - The support-matrix inventory test: every route, MCP action and OpenAPI
     operation is in the matrix of section 10, and every matrix row is
     reachable.
   - The same semantic cases run over REST, MCP and the OpenAPI client.
   - A widening through a personal key without the key property returns
     403. With the property it succeeds.
9. **Receipts.**
   - `retention` is set only after the write commits.
   - A forced writer failure gives `failed`, never `kept`.
   - The value guard refuses free text in the new fields.
10. **Boundary guard.** `tests/test_engine_knowledge_boundary.py` passes,
    and the ratchet baseline does not rise.

### 16.2 Canary tests across every sink

These follow the D32 canary method. Plant distinct markers through the real
write APIs, then search every sink. Positive controls prove each search can
find a marker.

- **Content canaries.** Markers go in:
  - nested message text;
  - tool arguments and results;
  - a multipart filename and a part over 1 MiB;
  - a URL query in a body;
  - a streamed reply chunk;
  - a malformed body;
  - a cancelled request.
- **Credential canaries.** The caller key, a stored provider key, a stored
  header and URL credentials (D32 a).
- **Runs.** Each canary request runs under:
  - **forget**: no content marker may appear in any sink;
  - **kept**: content markers appear only in `kept_requests` (or the kind's
    table), and credential markers appear nowhere.
- **Sinks searched.** Raw bytes, with base64, URL-encoded and JSON-escaped
  forms (`log_scrub.forms`):
  - every file under the test's data, cache and private temp directories;
  - the SQLite files including WAL;
  - the captured gateway stdout and stderr;
  - the receipt rows;
  - the management and MCP responses of a non-maker;
  - LiteLLM's writable paths and captured stdout;
  - the engine output files of a fake engine that echoes its input to
    stdout.
- **The positive control for the engine file** is the reason row 7 of
  section 5.1 is disclosed rather than claimed clean.
- **LiteLLM.** The real pinned LiteLLM runs in-process: its proxy app under
  a test client, with the generated config, the allowlisted environment, and
  a private working directory and `TMPDIR`. It needs no ports. A stub
  upstream on an ephemeral loopback port answers 200, or answers 400 and
  echoes the request. The LiteLLM survey built and ran this harness; it is a
  seed for slice 3.
  - The test captures stdout and stderr and every file in those
    directories, and searches them for the canary.
  - It pins the measured results: with `LITELLM_LOG` unset, the echo path
    leaks; with `CRITICAL` and the profile, nothing leaks.
  - With a test database, it also asserts that no spend row and no error
    row is written.
  - Mutation tests prove the check works: `success_callback: ["log_file"]`,
    `cache: true`, `LITELLM_LOG=DEBUG` or `disable_error_logs: false` in the
    fixture must each fail it.
- **Noninterference** (D32 e): two configs that differ only in secret
  bytes give identical records, ledgers and Debug views.

### 16.3 Live end-to-end checks on this machine

These run against a scratch gateway instance on its own port and data
directory (`scripts/scratch_owner.py`; the fresh-instance harness), never the
live 5000 or 15000 services.

1. **Settings.** A member turns request history on in the browser, sends a
   chat request from a CLI with their personal key, and sees it in history.
   They read the ledger, then turn history off. The record is gone from the
   UI, the REST API and the database file.
2. **Owner and members.** The owner opens a Debug window from the engine
   check of a scratch engine. They send a request from Try, see all three
   parts, see a second member's request not captured, close the window and
   see the records expire.
3. **Run again.** Run again on a kept request to an engine that is asleep.
   The dry run says "will wake". Confirm. The new receipt has `replay_of`.
   With a paid alias, a capped plan refuses with the plain reason.
4. **Library delete.** Delete a Library item and confirm that its linked
   request-history record is gone.
5. **Never-save disclosure.** It names ComfyUI history and port reach for a
   node-graph engine, and the provider fact for a remote provider.
6. **Swap and core dumps.** `systemctl --user show` of a scratch unit
   reports `LimitCORE=0`. The swap and hibernation lines appear in the
   disclosure. Their behaviour is a host property and is not tested
   ([research] OS2: "application tests" do not "prove OS erasure").

## 17. Build plan

Each slice ships on its own, passes the gate, and gets an independent model
review before it merges (handoff standard). The acceptance tests refer to
section 16.

| # | Slice | Changes behaviour? | Acceptance |
|---|---|---|---|
| 1 | **Contract and data, no behaviour change.** `registry/retention.yml` and schema; `gateway/retention.py` with `decide`, `write` and the validator; `prompt_memory.decide` and the Responses rule call it; settings key migration; remove the three duplicated default copies (Python and web read the data through the settings API). | No | 16.1.1, 16.1.2, 16.1.3 (except the new-kind case), 16.1.10; live: existing prompt-memory flows unchanged |
| 2 | **Receipts `retention` field** for the existing kinds, with the value guard. | Adds a field | 16.1.9; client receipt shape snapshot updated deliberately |
| 3 | **LiteLLM keeps nothing.** Explicit config switches, allowlisted child environment, `LITELLM_LOG=CRITICAL`, the effective-settings check and its attention item, only `X-Azizam-Request-Id` forwarded (if the internal hop allows it), `LimitCORE=0` in the unit. Ships first among the behaviour changes, because live content is being written today. | LiteLLM config and environment; LiteLLM's own error lines leave the journal | the 16.2 LiteLLM canary and mutation tests; the config and environment tests of 5.2; live on a scratch instance with a scratch LiteLLM database: chat works, a virtual key's budget still meters, no spend or error row is written, and cancellation still correlates |
| 4 | **Request history**: kind, `kept_requests` store, capture of `accepted` and `received`, the secret floor, the per-request control on every data-plane route, member and household switches, deletion and expiry, maker routes under `/gateway/v1/`. Default from data (Tim's decision 1). | New kind; off unless data says otherwise | 16.1.3-16.1.7, the 16.2 content and credential canaries, live 16.3.1 and 16.3.4 |
| 5 | **Facts as data**: `retention_fact` schema; provider catalog seeds; protocol, definition and record levels; ledger view and the assembled "never save" copy. | Adds views | schema tests, a removed fact stays removed, the stale label, no network on read; live 16.3.5 |
| 6 | **Temporary files**: gateway-private `TMPDIR`, `PrivateTmp=yes`, STT workspace under it; disclosure rows 4-5. | Moves temporary files | 16.2 multipart and STT canaries; the STT suite unchanged |
| 7 | **Debug window**: windows table, decision step 4, `sent` part at the internal hop and the egress builder, the `MakerContent` projection, the engine-page button. | New kind | 16.1.4 (Debug reads), 16.2 with a window open, the admin-on-owner-key withholding case; live 16.3.2 |
| 8 | **Surfaces and UX layers**: the three MCP tools, the OpenAPI operations, the key property, presets, guided form and data editor. | New surfaces | 16.1.8; UX review of the three layers per the design-review gate |
| 9 | **Run again**: the dry-run admission preview, cost preview, re-auth, incomplete-record editor, `replay_of`. Then remove the old `prompt_memory.decide` reference used by 16.1.1. | New action | 16.3.3; access-plan and cap refusals through REST and MCP |

**Later, only on demonstrated need:** field-level selectors on protocol
descriptions; a blob store for request inputs ([research] R6); saved
templates ([research] R5 option 3); the widening slice Tim chooses
(section 18).

## 18. Tim's three decisions

The design does not depend on any answer. Each answer is a data value plus,
for decision 3, a named slice.

### Decision 1: the default for new request history

- **What each answer sets.**
  - Off: `kinds.request_history.default_keep: false`.
  - On: `true`, with `default_retention` as chosen. The rollout then
    snapshots `keep: false` for existing members and shows them a notice
    (section 12).
  - Per route kind: not offered. One switch per kind keeps the member's
    model to two switches.
- **Recommendation: off.** Request history stays structural-only until the
  maker turns it on. Reasons:
  1. The client apps keep their own history. Azizam is "not ... a
     replacement for the client apps that call it". Library already keeps
     results (`PRODUCT_STRATEGY.md:42-43`; [handoff] Q2 reason 1).
  2. A pasted secret cannot be detected (section 3.5). Bodies kept by
     default build up exposure on the owner's machine.
  3. Receipts already meet the honesty promise without bodies.
  4. Trust-first gateways and platforms default content capture off:
     OpenRouter (GW-O1), Kong (GW-K1), LiteLLM spend content (GW-L2),
     Bedrock invocation logging (PT-W1) and Vertex request-response logging
     (PT-VX1, PT-VX3). Observability products default it on.
  5. The setup need, seeing bodies, is met by the Debug window one step
     from the engine check (section 9).
- **The best argument against.** A member who wants to see what an app sent
  has to turn history on first. Mitigation: the receipt view links "keep
  requests like this for 7 days" to the member's switch.
- Confidence: moderate, the same as the judge's.

### Decision 2: does the Identity state that members' content is private, and from whom?

- **What each answer sets.** Yes: add one sentence to the Identity, and
  `disclosure.content_private` switches to the stated copy. No: the copy
  stays as today.
- **Recommendation: yes.** Add under opinionated item 1:

  > A person's request content is theirs: Azizam keeps it only as they
  > choose, and no role reads it through Azizam. The owner runs the machine
  > and can read its files, and Azizam says so.

  This states the existing behaviour. It adds no new rule:
  `CORE_CONTRACTS.md:610`, the `PROMPT_MEMORY.md` viewer table and
  `HEADLESS_AND_RESOURCES.md` principle 4 already enforce it. The
  retention design depends on it ([handoff] I3). Writing it down makes a
  future owner-reads-all shortcut a visible Identity change instead of a
  quiet code change. The second clause keeps the server-authority limit
  honest ([reach] "Draft member copy").

### Decision 3: may reader scope ever widen past the maker?

- **What each answer sets.**
  - Never: `widening: none` stays the only legal value. No code.
  - Maker-initiated sharing: `widening: maker_share`, plus the slice
    "Share with owner for help". The maker shares one record with a named
    person, sees it in a sharing list and can revoke it. Shared records
    show the maker a badge ([reach] "Sharing").
  - Disclosed owner access: `widening: disclosed_owner_access`, plus a
    break-glass slice. It needs a reason, a scope, a short expiry, an audit
    record under the real operator's name, and a member notice on request,
    grant and use ([reach] "Support and break-glass").
- **Recommendation: never for the owner role. Allow maker-initiated
  sharing when someone asks for it** (build it then, not now). Sharing
  keeps the maker in control of each disclosure. A standing owner path
  turns "private" into "private unless the owner chooses", and in a
  single-owner household there is no second approver ([reach] "Support and
  break-glass": "A single-owner household cannot have dual approval").

## 19. Open points and ungrounded claims

- UNGROUNDED: people expect deleting a Library item to delete its linked
  request-history records (section 3.1). The live check 16.3.4 and the UX
  review test it.
- UNGROUNDED: the memory cost of a memory-only STT path (section 5.1, row
  4). Measure it before offering it.
- UNGROUNDED: whether a Debug window should need a recent sign-in (section
  13.1). Proposed: no.
- **Needs Tim's OK: a read-only count of what LiteLLM's live database
  already holds.** The spend rows with non-empty `messages` or `response`,
  the rows with an `error_information` message, and the `LiteLLM_Config`
  `general_settings` row. The LiteLLM survey prepared the queries and did
  not connect. Deleting old rows is the owner's choice.
- **Found in passing, outside this design: about 104
  `data/litellm.config.yaml.launch.*` files.** They are on the live host
  (`~/applications/azizam/data`), some with mode 0664. Each holds a
  materialized LiteLLM config, which can carry header or URL credentials.
  That belongs to the secrets train (D30: "written to a restricted file").
  This document does not fix it.
- UNGROUNDED: the LiteLLM 1.86.2 route for the effective-settings check
  (section 5.2).
- Callback-enabling environment names are not a concern: the survey found
  none in 1.86.2, and the allowlisted child environment would make such a
  list unnecessary anyway.
- Reviewer focus: the choice of per-body classification over [research]
  R2's per-field registry (section 3.5). The one new key property (section
  10). The three new MCP tools against `HEADLESS_AND_RESOURCES.md` "No new
  tools yet".

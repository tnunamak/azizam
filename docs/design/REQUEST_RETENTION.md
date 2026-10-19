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
- Repo paths with line numbers, at `a372c303`. LiteLLM paths
  (`litellm/proxy/...`) are in the installed 1.86.2 package of the repo
  venv.
- **[survey]**: the repo and LiteLLM surveys made for this design on
  2026-10-19, by read-only subagents. The LiteLLM survey read the
  installed source and ran an in-process canary harness, which now lives
  outside the repo at `~/.tmp/llm-probe`. It read the names of the live
  process's environment variables, not their values.
- **[judge r1]**, **[judge r2]**, **[judge r3]**: the independent reviews
  of this design at `26b4d443`, `da9d3a7b` and `8711a9ec` (Codex
  GPT-6.1-Sol, high effort). Their
  findings are applied in this revision.

## 1. Summary

1. **One contract, four kinds.** Azizam keeps request content under one
   policy contract with one decision function, one write path, one reader
   rule, one erasure machine and one per-request control. The contract has
   four content kinds, and each kind has its own purpose sentence. Members
   see two switches. Library prompts and saved conversations share today's
   switch, unchanged. Request history has its own. The kinds are:
   - Library prompts and saved conversations already exist.
   - Request history and Debug captures are new.

   The default values are bundled registry data, not code constants.
2. **The reader floor is in core.** Only the person who made a request can
   read its content through Azizam. For saved conversations, the reader is
   today's person-and-App scope. Data can narrow that set. Nothing in data
   can widen it. Widening is one marked extension point, and it stays closed
   until Tim decides. A delegated credential (a key or an MCP grant)
   reaches the new controls only with a privacy permission that the person
   grants, so existing keys and grants gain nothing.
3. **LiteLLM keeps nothing.** Azizam decides retention before a request
   reaches LiteLLM. The generated LiteLLM config and the launch environment
   turn off every LiteLLM place that can keep content. A test reads the
   generated artifact and fails when a content sink can be on.

   Today this is not true on the live host. LiteLLM there has a database
   and writes spend and error rows. Its default ERROR line printed an
   echoed prompt in a measured test. Slice 3 fixes this. When the gateway
   cannot confirm that a database setting does not override the profile,
   it refuses to route through LiteLLM. Slice 3 ships first among the
   behaviour changes.
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
9. **Phases.** The build is ten small slices (section 17). Slice 1 changes
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
   and the owner can override every value. It holds each group's default
   and choices, and each kind's purpose, parts and readers. Core code has no default of its own. A missing file
   fails startup ([handoff] I7; `docs/EXTENSION_ARCHITECTURE.md:7-10` "Data
   owns ... policy defaults").
2. **Household policy.** The owner's values, in `admin.db`
   `operator_settings`, key `retention`. Today's `prompt_memory` key moves
   there in slice 1 (section 12). It holds `never_keep` (the kill switch,
   one for all kinds). For each group it holds `default_keep` for new
   members and `retention_cap`. For each kind it holds `readers` and
   `parts`. For `debug_capture` it holds `allowed`.
3. **Member choice.** One row per person and **preference group** in the
   member preference table (today: `prompt_memory_preferences`,
   `gateway/admin_store.py:236`). A row holds:
   - `keep` and `retention`;
   - `parts`, for groups that offer a choice (request history: "also keep
     replies");
   - `readers`, which can only narrow.

   A group is a set of kinds that share one switch. `library_prompt` and
   `conversation` form the group `prompts`, because today one member
   preference governs both. It gates Responses storage (`proxy.py:26725`),
   and turning it off deletes saved conversations (`proxy.py:20114`).
   `request_history` is its own group. `debug_capture` has no member row:
   opening a window is the choice (section 9).

   **New member rows.** The household default applies only when a row is
   created: at rollout, at user creation, or when a new group appears. It is
   never applied later to a person who has a row, so a household change
   never widens an existing member. This is today's rule
   (`PROMPT_MEMORY.md` "The household default affects new members only",
   `CORE_CONTRACTS.md` "Member preferences are saved at rollout and user
   creation").

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
groups:                     # one member switch per group
  prompts:                  # today's prompt-memory switch, unchanged
    kinds: [library_prompt, conversation]
    default_keep: true                # today's value (prompt_memory.py:33)
    choices: [never, 3_months, 1_year]
    default_retention: never          # today's value
  request_history:
    kinds: [request_history]
    default_keep: false               # TIM DECISION 1 (section 18); new members only
    choices: [1_day, 7_days, 30_days]
    default_retention: 7_days
    member_parts: [accepted]          # "also keep replies" adds received
kinds:
  library_prompt:
    purpose: Library keeps the words that made each thing you created, so you can find it and make it again.
    parts: {allowed: [accepted], default: [accepted]}
    readers: maker                    # see section 4
  conversation:
    purpose: An app can ask Azizam to keep a conversation so it can continue it later.
    parts: {allowed: [accepted, received], default: [accepted, received]}
    readers: scope                    # today's person + App/credential rule (section 4)
  request_history:
    purpose: Request history keeps what your apps sent for a short time, so you can check it and run it again.
    parts: {allowed: [accepted, received], default: [accepted]}
    max_record_bytes: 1048576
    readers: maker
  debug_capture:                      # no group: opening a window is the choice (section 9)
    purpose: A Debug window keeps what went to one engine and what came back while you set it up, then deletes it.
    allowed: true                     # the household may set false
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

- A `readers` value outside the core list, or a value given for a kind it
  is not legal for (section 4). `conversation` takes only `scope`.
- A `parts` default outside `allowed`.
- A kind in two groups, or a grouped kind that also has its own `choices`.
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
4. `debug_capture` only. Keep exactly when the household allows windows
   and an open window has this person as opener and the dispatched engine
   as its engine (reason `debug_window`). Otherwise `no_window`. Steps 5 and
   6 do not apply. The deadline is `min(ends_at, closed_at) + window
   retention`, so closing a window early shortens its records' deadlines.
   `parts` is the window's parts ∩ the household parts ∩ allowed.
5. The member's `keep` for the kind's group is false: `member_disabled`.
   Every person has a row for each group (section 3.2), so there is no
   "no row" case at decision time. A row that is missing because of
   contention during initialization degrades to not keeping, as today
   (`PROMPT_MEMORY.md` "Contention during legacy preference initialization
   degrades to no prompt storage").
6. Otherwise keep. `kept_until` is the request time plus the shorter of the
   member's retention and the household cap. `parts` is the member's parts
   ∩ the household parts ∩ the kind's allowed parts. `readers` is the
   narrower of the member's and the household's value.

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
   ([research] R6) is not part of this design. Section 17 lists it as a later
   need.
6. **Pasted secrets cannot be detected.** No rule can prove that a message
   holds no pasted secret. The UI says so, and offers forget and "never
   save" ([research] R3, second paragraph). Azizam ships no regex "secret
   detector", because a detector that misses would be a control that does
   less than it says.
7. **Only containers the gateway parses are captured.** These are JSON
   objects, and multipart forms (their fields and part listing). A body the
   gateway does not parse is recorded as
   `{"omitted": "unsupported_container", "media_type": ..., "bytes": N}`:
   a non-JSON native-lane body, raw audio, or any other media type. The
   request itself still runs. This is "unknown shapes fail closed for
   persistent capture, while ordinary authorized inference may still pass
   through" ([research] R2).
8. **A field named as a credential is removed, at any depth.** A field
   whose name has a credential segment gets the value `[removed]`. The
   segments are compared in snake, kebab or camel case: `secret`,
   `password`, `passwd`, `credential(s)`, `authorization`, `apikey`, or
   the pairs `api_key`, `access_token`, `refresh_token`, `id_token`,
   `auth_token`. A plain `token` segment is not one of them, so
   `max_tokens` is kept. This uses names only, like the D43 registration
   test, and no shape knowledge. It catches a native API that takes its
   key in the body. It cannot catch a credential under a neutral name, so
   such a shape needs the per-field extension below, and the disclosure
   names the limit.

**Classification is per container, not per field.** Rules 1-8 are generic.
They use no knowledge of an API shape, so every JSON or multipart shape,
including one that arrives tomorrow, is covered with no new data. Other
containers are not captured. Field-level selectors
(`retention.drop` JSON pointers on a protocol description, [research] R2)
come when a demonstrated shape needs a field dropped that rule 8 misses.
Building them now would be machinery on speculation (`AGENTS.md:39`). This is a deliberate change from
R2's full classifier, made for simplicity. A reviewer should check it.

### 3.6 Storage and the one write path

- `library_prompt` and `conversation` keep their tables
  (`asset_prompts`, `responses`). Their schemas do not change, and no row
  moves.
- `request_history` and `debug_capture` use one new table, `kept_requests`.
  It is an additive table in `data/media_jobs.db`, beside `asset_prompts`,
  so it inherits that store's lock, quarantine, corruption-only recovery
  and `secure_delete` (`docs/ops/DATA_RETENTION.md`, last sections). There
  is no new database file. More important: a Library deletion already
  commits prompts, hides and job rows together in that database
  (`DATA_RETENTION.md`: "Prompts, hides and job deletions commit together
  in the media-jobs database or not at all"). The cascade to linked
  records (section 3.1) joins the same transaction, so a crash cannot
  leave one half done. For a media request, the request-history record and
  the Library prompt are written in one transaction. Its columns:
  - `record_id`, `request_id`, `kind`, `person_id`, `app_id`
  - `engine_id`, `debug_window_id`
  - `created_at`, `kept_until`
  - `parts_json`, `omissions_json`, `policy_version`
  - `linked_asset_ids`
  - `fact_revisions`: the revision hashes of the facts in force when the
    request was sent (section 6.1)
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

**The core floor.** Each kind has one scope rule, written once in core:

- **`library_prompt`, `request_history` and `debug_capture`** are always
  written with a person. A write without one is refused (section 3.3, step
  3). Their content is projected only when `record.person_id` is not null
  and equals the viewer's person. A null person never matches a null
  person.
- **`conversation`** keeps today's rule exactly (`_responses_row_visible`,
  `proxy.py:25293`). The row needs a matching person **and** a matching
  immutable App id, or credential hash when there is no App. A no-person
  row is visible only through that App or credential scope. The existing
  tests stay the oracle (`tests/test_prompt_memory_responses.py:209,236`:
  same person and a different App is denied; a scoped no-person row is
  retrievable). The conversation's "maker" is that scope, not a person.

For the person-scoped kinds, the viewer's person comes from one of three
places:

- a browser session or OIDC sign-in;
- a personal App key minted by its owner (`admin_runtime.py:2294`,
  `person_for_app_credential`);
- an MCP grant made by that person (`proxy.py:2413`).

The last two are delegated credentials. For Library prompts, a personal key
reads as it does today. For request history and Debug captures, a
delegated credential reads only with the privacy permission, which the
person grants (section 10).

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

**Data may narrow it.** `readers` takes values from a core list. Each
value is legal only for the kinds named:

- `maker`: the floor, and the most it allows today. For the person-scoped
  kinds.
- `maker_signed_in`: only the maker's browser session; no delegated
  credential can read the content. For the person-scoped kinds.
- `scope`: today's conversation rule (above). For `conversation` only,
  and it is fixed there. It is the reason a saved conversation exists: the
  app that saved it must be able to continue it.

A member's `readers` in the `prompts` group therefore narrows Library
prompts only. Conversations keep `scope`. The guided form says so: "Only
when I am signed in (Library prompts and request history; an app can still
continue a conversation it saved)". No `readers` value ever replaces or
skips the conversation scope check.

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
| 2 | Receipts | always | yes | structural only; the guard extends to values (section 13.3) |
| 3 | Execution spool `data/image_media_jobs/` | async media jobs | yes | memory only, as today; the job cannot resume after a restart |
| 4 | STT workspace `data/stt-spool/` | long audio | yes | **today it writes to disk even under forget.** Slice 4 moves it to the gateway's private temp directory (row 5) and the copy names it. A memory-only STT path is a later choice: long audio would cost too much memory (UNGROUNDED size limit). |
| 5 | Multipart spill over 1 MiB | uploads | yes, through the temp directory | slice 4 sets the temp directory to a gateway-private directory (`TMPDIR`, mode 0700, emptied at start) and the unit sets `PrivateTmp=yes`. A deleted temp file can stay in free disk blocks; the copy says "a temporary file, deleted when the request ends". |
| 6 | Gateway logs (journal) | always | yes | D43/D44: no request or upstream text in logs. The canary test checks the journal capture. |
| 7 | Engine output files `data/processes/<id>.log` | engines the gateway starts | the file, yes; what the engine prints, no | the disclosure names it for every engine whose output file Azizam keeps (section 5.3). `log_max_bytes` is existing record data. |
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
| Database settings that override YAML | `general_settings.store_model_in_db: false`, plus the database-override precondition check below | unset | GW-L3; survey |
| Callbacks (`callbacks`, `success_callback`, `failure_callback`) | `[]` for each | unset (empty); no environment variable enables them in 1.86.2 | GW-L1, GW-L10; survey |
| Key and team callbacks (key `metadata.logging`, team `metadata.logging`, team `callback_settings`) | none set; the precondition check (step 2 below) fails closed on any | not checked today | `litellm/proxy/litellm_pre_call_utils.py:476-547,1722-1733`; [judge r2] |
| Message content in logging objects | `litellm_settings.turn_off_message_logging: true`; `redact_messages_in_exceptions: true`; `redact_user_api_key_info: true` (defence in depth only) | unset | GW-L1, GW-L12 |
| Process stdout and stderr | `LITELLM_LOG=CRITICAL` in the child environment; never `--debug`, `--detailed_debug`, `DEBUG` or `DETAILED_DEBUG` | unset: ERROR lines can carry echoed prompts | GW-L11; survey (measured) |
| Per-request debug and no-log fields (`litellm_request_debug`, `no-log`, `success_callback` in a body) | nothing new: the gateway's body allowlist already drops them (`gateway/backends/llm_execution.py:38-124,245-268`); a test pins it | blocked | survey |
| Response cache and Responses polling cache | `litellm_settings.cache: false`; no `cache_params`; `router_settings.cache_responses: false` | unset (off; `litellm.cache` is `None`, measured) | GW-L8, GW-L10 |
| `.env` auto-load by LiteLLM | `LITELLM_MODE=PRODUCTION` | unset (`DEV` loads a `.env` found by walking up; none exists today) | survey |
| Admin UI and API docs | `DISABLE_ADMIN_UI=true`, `NO_DOCS=true`, `NO_REDOC=true` (LiteLLM's own admin UI files stay mounted on 127.0.0.1 behind the master key) | unset | GW-L3; survey |
| Uploads over 1 MiB | `TMPDIR` set to the gateway's private temp directory (slice 4) | system temp | Starlette `formparsers.py:126,209` |
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

**The database overrides are checked, and a failure closes the path.** In
1.86.2, a database can switch content capture back on in two ways. The
first is the config table, which overrides the YAML. The second is per-key
and per-team callbacks, read on each request (step 2 below). The config
table works only one way. At startup, LiteLLM turns `store_model_in_db` on when the env var
`STORE_MODEL_IN_DB` is set, when the YAML sets it, or when the
`LiteLLM_Config` row `general_settings` has `store_model_in_db: true`
(`litellm/proxy/proxy_server.py:7409-7435`). Only then does it merge that
row's settings into the config (`:3624-3630`, `:5542-5575`), and it polls
the row every 30 seconds (`:5631-5675`). That poll is what sets
`store_prompts_in_spend_logs` from the row (`:5411-5425`).

The persisted UI settings sync only a fixed list of runtime flags, and no
content switch is on that list (`:7255-7285`;
`ui_crud_endpoints/proxy_setting_endpoints.py:191-200`). That list does
include `forward_client_headers_to_llm_api`.

The settings API cannot prove the content switches either:
`/config/list` reports only selected general settings, without the two
disable switches. It reads the database row, not the effective values
(`:14252-14275`, [judge r1]). So the gateway checks the precondition
itself:

1. **Environment.** The child's allowlisted environment never carries
   `STORE_MODEL_IN_DB`, and the YAML sets `store_model_in_db: false`.
   Deterministic tests cover both. Steps 2-5 apply to the managed LiteLLM,
   the one `start.sh` starts. Step 6 covers the shipped Compose service,
   and step 7 any other.
2. **Database rows.** It reads these with read-only queries, using the
   `DATABASE_URL` it already has. `start.sh:9-13` exports the sourced
   environment to both the gateway and the sidecar (`start.sh:105-107,173`).
   - The `LiteLLM_Config` rows `general_settings` and `litellm_settings`.
     The precondition fails when `store_model_in_db` or
     `store_prompts_in_spend_logs` is true, or when a `callbacks`,
     `success_callback`, `failure_callback` or `cache` key is present.
   - Key and team logging. LiteLLM 1.86.2 reads callbacks per request from
     a key's `metadata.logging`, a team's `metadata.logging` and a team's
     `callback_settings`. It adds them after the gateway's body allowlist
     (`litellm/proxy/litellm_pre_call_utils.py:476-547,1722-1733`; [judge
     r2]). The precondition fails when any key or team row has a non-empty
     value in one of these. The gateway's own virtual-key create path
     (`gateway/admin_api.py:1126-1167`) already allows only listed fields.
     It also refuses a team that fails the check, and re-runs the check
     after each of its own key writes. The maintenance loop re-runs the
     whole check hourly.
3. **The check is bound to one LiteLLM process.** `start.sh` writes the
   managed LiteLLM's pid file (`AZIZAM_LITELLM_PID_FILE`, default
   `data/litellm.pid`, `start.sh:95`), and its watcher can restart LiteLLM
   on its own (`start.sh:127-144`). The gateway identifies a generation by
   the pid in that file plus the process start time from `/proc`. This is
   the trust rule the repo already uses for state files (commit `b4ecc83d`:
   "trust a state file only when pid, start time, group and command line
   agree"). A passed check belongs to one generation. Before each
   LiteLLM-bound request, the gate compares the current generation with the
   checked one, which costs one `stat` and one small read. On a change, it
   checks again before it routes. A restart with an unchanged config is
   therefore checked again too.
4. **The boundary is exact: content execution only.** Requests to LiteLLM
   are built with `llm_execution.litellm_url` (`gateway/backends/llm_execution.py:210`).
   They have two purposes:
   - **Execution** carries request content, and it is gated. These are the
     chat lanes (`proxy.py:28337`) and the paid-provider leg
     (`gateway/provider_execution.py:555`).
   - **Management** carries no request content, and it is not gated: model
     listing (`proxy.py:24759`) and virtual-key administration
     (`gateway/backends/litellm_admin.py:112,163`).

   So the owner can still list and delete an offending key while execution
   is refused. That is the remedy ([judge r3] finding 2). A structural
   test lists every caller of `litellm_url`. Each caller must be either
   gated execution or a listed management caller, with the reason it
   carries no content. A new caller fails until it is classified. Direct
   engine traffic and the internal hop (LiteLLM calling back into the
   gateway) do not reach LiteLLM's front door, so they are outside the
   gate.
5. **Fail closed for the managed LiteLLM.** Until the check passes for the
   current generation, every request through that gate gets `503
   litellm_content_unverified`. This is a gateway refusal with a plain
   sentence that names the setting and the fix. The engine page shows the
   same attention item. A check that cannot run (for example, the database
   is unreachable) also fails closed. With no database, the database part
   of the check has nothing to read and passes.
6. **The shipped Compose LiteLLM gets the same profile.** `docker-compose.yml`
   is Azizam's own file. Its `litellm` service runs the config the gateway
   generates (`docker-compose.yml:30-52`), and today its environment holds
   only `LITELLM_MASTER_KEY`. So the profile's environment values
   (`LITELLM_LOG=CRITICAL`, `LITELLM_MODE=PRODUCTION`, `DISABLE_ADMIN_UI`,
   `NO_DOCS`, `NO_REDOC`) are added there. A test parses the file and
   asserts that the service environment equals the profile, that there is
   no `DATABASE_URL`, and that the config is the generated one. With no
   database, the database part of the check has nothing to read and passes.
   The pid generation does not apply in a container. The config and the
   environment are static files that the test covers. If the owner adds a
   database to that service, the gateway runs the database check when its
   own environment has the URL. Otherwise it reports the status `unmanaged`
   (step 7).
7. **A LiteLLM that Azizam does not run is disclosed, not refused.** This
   applies to `AZIZAM_MANAGE_LITELLM=false` with an owner-run LiteLLM
   (`start.sh:52`), to a substrate endpoint that is not the managed or the
   Compose one, and to a Compose service whose database the gateway cannot
   see. Its status is `unmanaged`. The ledger and the "never save" text
   move it to "What Azizam cannot control", with one of two sentences:
   - For a LiteLLM that Azizam does not run: "LiteLLM, which runs chat for
     Azizam, is run outside Azizam. Azizam cannot confirm that it keeps
     nothing."
   - For the shipped Compose service with a database the gateway cannot
     see: "Azizam cannot check the database settings of the LiteLLM that
     runs chat for it." The owner may record that they configured it to keep nothing,
   and the ledger shows that as declared by the owner, not observed. This
   is the boundary already used for engines and providers. It is not a
   control that does nothing: the copy changes, and it never claims what
   Azizam cannot see.

This is a deliberate behaviour change. On the live host it could refuse
chat on the first deploy, if such a row exists. So the read-only count in
section 19 runs before slice 3 deploys. The check reads LiteLLM's own table
names. That is allowed: LiteLLM is the pinned, managed substrate, and
`gateway/litellm_substrate.py` already knows it. It is not engine knowledge.
Any LiteLLM upgrade re-verifies these line references (the pin is
hash-locked, `PRODUCT_STRATEGY.md:200-201`).

Limit: a host process holding the LiteLLM master key or direct database
access could change settings in LiteLLM's memory after the check, through
`/config/update`. On this host that is the owner, and Azizam does not call
that route. A test pins that no gateway code calls it.

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
4. The precondition check is tested against a fixture of query results.
   With each overriding row, each key or team logging value, and a failed
   query, chat through LiteLLM is refused with
   `litellm_content_unverified`. With a clean result, chat goes through.
   Generation tests cover:
   - a restart with an unchanged config: checked again before the first
     request;
   - a pid file that does not match the process start time: treated as
     unchecked;
   - a substrate endpoint change and `AZIZAM_MANAGE_LITELLM=false`: status
     `unmanaged`, disclosed, not refused;
   - the paid-provider leg: gated;
   - while execution is refused, listing and deleting a virtual key still
     work;
   - `docker-compose.yml`: the `litellm` service matches the profile.

   A live check runs the check against a scratch LiteLLM database (16.3).
5. The admin preview (`GET /admin/api/operator/litellm-config`, D30) shows
   the content switches and the result of the last precondition check.

### 5.3 "Never save" copy that stays honest

The disclosure is data (`disclosure.never_save`). It is assembled from facts,
not written as one promise. This is the default text with no engine facts
added (draft; the UX review owns the final words):

> Azizam does not save this request in its database or its logs. While the
> request runs, it is in this computer's memory. Long audio, and uploads
> over 1 MB, also pass through temporary files in Azizam's private folder.
> Those files are deleted when the request ends, but the disk can keep
> their old blocks. The computer can copy memory to swap space, to a
> hibernation file or into a crash report.

When the engine is one that Azizam starts and keeps an output file for
(section 5.1, row 7), one line is added:

> <engine> prints to a file that Azizam keeps on this computer (the last
> 64 KB). If <engine> prints what it receives, that file holds it.

Facts are added for the engine and provider the request goes to (section 6):

> ComfyUI, which Azizam reaches at this computer's own address, keeps its
> own history of requests in its memory until it restarts. It drops the
> oldest entry only once it holds more than 10,000. Anything that can reach
> its port can read or clear that history. Azizam does not clear it.

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
- **History: immutable revisions.** A fact's revision is a hash of its
  canonical JSON. When a request is sent, each fact in force is appended
  once to an append-only table, `retention_fact_revisions` (revision hash,
  fact JSON, first seen), in `media_jobs.db`. The receipt records the
  revision hashes. The ledger of an old request renders the stored
  revisions, so an edit, a removal or an upgrade cannot rewrite what it
  said ([research] R8 "preserve the evidence date and exact
  route/model/config used"; [judge r1] finding 10). A revision row holds no
  request content, so it has no deadline. A kept record carries the same
  revision hashes (section 3.6), because a record can outlive its receipt
  (receipts can be kept for only 30 days, `gateway/receipt_store.py:52-54`).
  A revision is pruned only when no remaining receipt **and** no remaining
  kept record references it ([judge r2] finding 3).
- **Library prompts and conversations keep their schemas.** They do not
  carry revision references. Their ledger uses the revisions on their
  receipt while the receipt exists. After the owner's receipt setting has
  removed the receipt, the ledger shows today's facts, labelled: "Today's
  facts. The facts in force when this was made are no longer kept: its
  receipt was removed under the receipt-retention setting." This is
  honest and needs no change to the existing tables ([judge r3] finding
  3). Adding references to those tables can come later, if someone needs
  the history.
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
   - `how_long: in memory until the engine restarts or clears it; the oldest entry is dropped when it holds more than 10,000`
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
field, `control`. For example: "The history is cleared when ComfyUI
restarts, or by ComfyUI's own clear-history action. Keep ComfyUI's port
reachable only from this computer." Both facts come from L13 and L14 (an
in-memory dictionary; `POST /history` clears it; the default bind is
127.0.0.1). L13 found no setting that turns history off, so the advice
does not offer one. One existing behaviour is kept and not widened: a
maker's explicit video DELETE also asks the engine to drop that
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

1. **Who can read it in Azizam.** "Only you", made exact per kind while
   `readers` is `maker`:
   - A Library prompt adds "and anyone holding one of your personal App
     keys". That is today's delegation (`CORE_CONTRACTS.md:601-603`).
   - Request history and Debug captures add "and the keys and connected
     apps you gave the privacy permission", when any exist.
   - A conversation says "you, through the app that saved it".
2. **Who runs the machine.** "The owner, <name>, runs this computer and can
   read its files."
3. **Where it went.** The engine or provider, by display name. It shows the
   destination's provider facts and engine facts with their checked dates
   and their reach.
4. **What Azizam keeps.** Each kind kept for this request, with its deadline.
   Omitted parts show with their reason.
5. **What Azizam cannot control.** Rows 9-13 of section 5.1, and an
   unmanaged LiteLLM (section 5.2, step 7).

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
   - **Keep nothing:** both member switches off (prompts and
     conversations; request history).
   - **Prompts and conversations:** today's behaviour, and the state after
     migration.
   - **Prompts, conversations and 7 days of request history.**

   Picking a preset writes its values. When the stored values match no
   preset, the picker shows **Custom**. The UI never pretends a preset is
   still in force.
2. **Guided form.** One row per preference group: its purpose sentences,
   an on/off switch, the retention choice and, for request history, "also
   keep replies". "Also keep replies" is the member's `parts`. Under each
   row the form shows the ledger preview for a typical request (section 7) and the consequence of the change in plain words. For
   example: "Turning this off deletes 41 kept requests now. This cannot be
   undone." The owner's form adds the kill switch, the caps, the household
   `parts` and `readers`, and "Allow Debug windows". A member's form adds
   "Only when I am signed in" (`readers: maker_signed_in`). It shows the
   owner's caps as limits, not as errors.
3. **Full data editing.** The policy object as YAML or JSON, with the
   schema and the validator's plain refusals. It is the same object the
   REST API returns.

The essential complexity is four kinds × (keep, retention, parts, readers) ×
three layers of authority. The UI hides it this way:

- A member sees two switches. The first is "Remember my prompts and saved
  conversations", today's switch, unchanged. The second is "Request
  history". A saved conversation also needs the app to ask for it
  (`store`). Debug captures exist only inside a window.
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
- `parts`, chosen at opening from the allowed parts (default: all three)
- `retention`, chosen from `choices`
- `closed_at`

**Opening a window is the opener's explicit consent to keep content for a
bounded time.** So `debug_capture` has no member switch (section 3.2).
Decision step 4 keeps the opener's own requests to that engine while the
window is open. Steps 1-3 still apply: the kill switch, a forgetting
request and the no-person rule all win over a window. The household can
forbid windows (`kinds.debug_capture.allowed: false`); then opening returns
`403 debug_windows_disabled`.

Each record expires at `min(ends_at, closed_at) + retention`. Closing a
window early shortens its records' deadlines to match. A window can be
closed early. Closing a window or deleting its records uses the normal
erasure path. A request still in flight when its window closes is refused
at write time (section 12).

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
    emitted only to the record's maker after the scope check (section 4)
    and is forbidden for every other audience (D28 default Forbidden).
    `MakerContent` ships with the first content surface, request history
    (slice 5), not with the Debug window.
- **Honesty.** While a window is open, the header of every page the opener
  sees shows "Debug window open for <engine>, 12 minutes left". The receipt
  of each captured request says `retention.kind: debug_capture`.

## 10. Surfaces: REST, MCP and CLI for every control (owner task 8)

Every control is in one support matrix. Supported surfaces get the same
semantic tests. A route inventory test fails when an operation is missing
from the matrix (`HEADLESS_AND_RESOURCES.md` "Support matrix, not a parity
ritual"; Identity item 6).

**Who may use these controls.**

- **A browser session** of the person may use every control in the table
  below. A widening write checks the session is live in its own
  transaction (D41).
- **A delegated credential** (a personal App key or an MCP grant) may use
  the retention, kept-request and Debug-window controls only if it carries
  the **privacy permission**. The person chooses it when minting the key
  (minting already needs a recent sign-in, D36) or on the MCP consent page,
  where it is shown as an extra line: "Read and delete what Azizam keeps
  for you, and change your privacy settings". It defaults to off.
- **Existing keys and grants get no new reach.** The MCP consent page
  promises that an app "can't change your plan, keys, people or settings"
  (`web/src/app/authorize/page.tsx:108`), and grants have only the `mcp`
  and `offline_access` scopes (`gateway/mcp_oauth.py:80-81`). So the
  permission is a new scope, `privacy`, and existing grants do not have
  it. Without the permission, a delegated credential keeps exactly what it
  has today: a personal key can still read the maker's Library prompts
  through the existing routes (`CORE_CONTRACTS.md:601-603`), and every
  credential can use the per-request forget control.
- **Why the permission exists.** Without it, a leaked personal key could
  turn history on, capture the maker's requests from every other device,
  and read them later.
- **Household policy** needs the owner role: a session, an owner automation
  key (as today, `owner_openapi_routes.json:337,341`), or an owner's MCP
  grant with the permission.

| Control | REST | MCP | CLI (Restish over OpenAPI) |
|---|---|---|---|
| Household policy: read, write, preset | `GET/PUT /admin/api/settings/retention` (`If-Match`) | tool `retention`, action `household_get` / `household_set` (owner person) | `restish azizam get-retention-settings` / `put-retention-settings` (owner key) |
| My choices | `GET/PUT /me/api/retention` (session); `GET/PUT /gateway/v1/me/retention` (delegated credential with the privacy permission) | `retention` `mine_get` / `mine_set` | `restish azizam-app get-my-retention` / `put-my-retention` |
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
  A item 5). It runs at the caller's normal priority. It counts toward the
  caller's concurrency and outstanding-job limits
  (`media_jobs.create(..., outstanding_limits=)`). Engine waking and
  swapping happen in the normal dispatch path (`proxy.ensure_target`,
  `proxy.py:1598`, with the swap lock and drain), so Run again cannot jump
  the queue or unload another person's engine by a path of its own. There
  is no batch run: one record at a time, so history cannot become a bulk
  queue.
- **What the preview can honestly say.** Before confirming, `dry_run`
  shows only facts that exist today, labelled "not reserved":
  1. The admission outcome (allowed, delayed or refused, with its reasons)
     from `advise_resource_admission` (`gateway/resource_admission.py:158`),
     fed by the same inputs the route uses
     (`resource_admission_http.advise_route_admission`). That module "does
     not manage services, queues or workers" (`resource_admission.py:1`),
     so the preview does not claim it does.
  2. The engine's residency as last observed, with its time: "loaded" or
     "not loaded, so it will be started" (`gateway/engine_residency.py`;
     `observed_at` per `HEADLESS_AND_RESOURCES.md` principle 2).
  3. The cost estimate.

  The preview does not predict which other engine a swap would unload. No
  pure planner for that exists: the swap is decided inside `ensure_target`
  at dispatch. A preview of it is the "explain availability" read that
  `HEADLESS_AND_RESOURCES.md` lists under "Later, only on demonstrated
  need". Run again is a reason to build it later, not a reason to promise
  it now. The dispatch rechecks everything, so a state change between
  preview and send gives the real outcome, with its own reason.

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

- **Cutoffs gain a group.** A cutoff row becomes `(person_id, group)`.
  Existing rows become group `prompts`, which covers what they covered
  then: Library prompts, conversations, job hashes and spools. "Delete all"
  and the kill switch cover every group. A Debug window's close is its own
  cutoff, `(person_id, debug_window_id)`.
- **One asset's deletion is fenced too.** Deleting a Library item deletes
  its linked request-history records in the same transaction (section
  3.6). A request-history write for a media request runs in the same
  transaction as its Library prompt write. If the asset is already hidden
  for that person (`media_hidden_assets`), the write is refused. A late
  write therefore cannot bring back a record for an item the maker has
  deleted.
- **Turning a group off records an erasure intent for that group.** It is
  written in the same transaction as the choice, and the group's records are
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
- **Removing a member erases every kind.** Disabling a member keeps them
  until their deadlines, as today.

**Migration: no silent change.** Slice 1 is a pure move:

1. `operator_settings.prompt_memory` becomes `operator_settings.retention`.
   - `default_remember` becomes the group default `groups.prompts.default_keep`.
   - `retention_cap` becomes `groups.prompts.retention_cap`.
   - `never_keep` becomes `never_keep`.
   - The revision counter carries over.
2. Each member preference row becomes the row of group `prompts`, with the
   same `remember` and `retention`. One row still governs Library prompts
   and conversations, as today: `prompt_memory.decide` is called for
   Responses (`proxy.py:26725-26736`), and memory-off deletes saved
   conversations (`proxy.py:20114`). Turning it off later still deletes
   both, and an app's `store:true` still needs it on. The test in 16.1.2
   changes the switch after migration, not only once before it.
3. Group `request_history` rows are created at rollout for every existing
   member, with `keep: false`, whatever the household default is. So
   existing members see nothing new kept. If Tim chooses a keep default
   (decision 1), it applies to members created after rollout, and existing
   members get a one-time notice with the switch. Nobody already using
   Azizam starts having request bodies kept without being told.
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
  the kind's scope check (section 4). Every other audience gets Forbidden.
- Parts hold no gateway-attached credential by construction (section 3.5).
  The noninterference property (D32 e) is tested on gateway-composed
  output (section 16.2). The engine's success product is excluded (D45b).
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

A receipt also gets `replay_of` (Run again) and `retention_facts`: the
revision hashes of the facts in force (section 6.1). All values are enums, ids,
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
| Per-field classifier for every API shape now ([research] R2) | Machinery ahead of need. Generic per-container rules cover every JSON and multipart shape, and other containers are not captured (section 3.5). |
| A per-request `mode: "keep"` | Lets a key holder widen against the maker's choice (section 3.4). |
| Owner-editable reader scope | Crosses "who may do what" ([handoff] I2). |
| Purging engine history from the gateway | Per-engine remediation in code; Identity "detects and advises" ([handoff] I6). |
| Relying on `turn_off_message_logging` | The installed 1.86.2 predates the `raw_request` fix ([research] GW-L12), and the switch does not stop process logs (measured). |
| Removing LiteLLM's database | LiteLLM virtual keys need it (`LITELLM_SUBSTRATE_DECISION.md`). The content tables are turned off instead. Revisit when the virtual-key bypass is retired. |
| A new database file for kept requests | It would split the Library cascade across two stores and add a crash boundary ([judge r1] finding 12). One table in `media_jobs.db` keeps one transaction. |
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
   - The old functions are kept in the test as the reference until slice 10.
2. **Migration.**
   - On a fixture and on a copy of the live `admin.db` and `media_jobs.db`:
     every `kept_until` is unchanged.
   - Every member's effective decision for the existing kinds is unchanged.
   - **After** migration, turning the `prompts` switch off blocks both a new
     Library prompt and an app's `store:true`, and deletes saved
     conversations, exactly as before (the existing
     `tests/test_prompt_memory_responses.py` suite runs unchanged).
   - Running the migration twice is a no-op.
   - A partial failure rolls back.
3. **Data, not code.**
   - Deleting `registry/retention.yml` fails startup with a plain message.
   - A grep-based test finds no retention default literal in Python or the
     web source. This is the same ratchet style as the boundary guard.
   - Changing `groups.request_history.default_keep` in the file changes the
     decision for a new member, with no code change.
4. **Reader floor.**
   - A cross-principal matrix of the owner, an administrator, another
     member, the maker's personal key and MCP grant (each with and without
     the privacy permission), a service App key, an automation key, a
     trusted address and a signed URL.
   - Each is checked against every content route and MCP action: list,
     get, search, ledger, export, Run again, Debug window read.
   - Only the maker's session, and the maker's delegated credentials that
     carry the permission, succeed for request history and Debug
     captures. Library prompts keep today's key delegation.
     `readers: maker_signed_in` removes every delegated credential.
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
   - Every retention, kept-request and Debug-window operation through a
     personal key or MCP grant without the privacy permission returns 403
     `privacy_permission_required`. With the permission it succeeds.
   - Existing grants and keys have exactly today's reach after the
     migration.
9. **Receipts.**
   - `retention` is set only after the write commits.
   - A forced writer failure gives `failed`, never `kept`.
   - The value guard refuses free text in the new fields.
10. **Boundary guard.** `tests/test_engine_knowledge_boundary.py` passes,
    and the ratchet baseline does not rise.
11. **Every control does something.** A table test lists every control
    the UI, REST, MCP and the data file expose: each group's `keep`,
    `retention`, `parts` and `readers`; each household value; and each
    window's `parts` and `retention`. For each control, the test flips it
    and asserts a changed decision, capture or authorization. A control
    with no effect fails ("no control that does nothing").
12. **Debug activation.**
    - An open window captures the opener's request to its engine with the
      window's parts. Without a window, nothing is captured.
    - Another person's request, or a request to another engine, is not
      captured.
    - The kill switch, `forget` and `allowed: false` each win over an open
      window.
    - Closing early shortens deadlines to `closed_at + retention`.
    - A request in flight at close is refused at write.
13. **Containers and names.**
    - A non-JSON native-lane body is recorded as `unsupported_container`,
      and the request still succeeds.
    - Fields named `api_key`, `clientSecret` and `x-api-key` at depth 3 are
      removed. `max_tokens` and `top_k` are kept.
14. **Fact revisions.** After a fact is edited, removed, or its catalog is
    upgraded, an old request's ledger renders the revision it was sent
    under. When the receipt is pruned while its kept record survives, the
    record's ledger still renders, and pruning keeps the revision. With
    request history off and the receipt pruned, a Library prompt's ledger
    shows today's facts with the "no longer kept" label.
15. **Conversation scope and readers.** The existing Responses scope tests
    pass unchanged. A conversation row with a null person is never readable
    through a person check. A member's `readers: maker_signed_in` in the
    `prompts` group blocks a personal key from reading Library prompts and
    leaves an app's `previous_response_id` working. The bundled
    `registry/retention.yml` passes the validator, and each illegal
    `readers` value fails.

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
- **Runs and allowed places.** Each run asserts where content markers may
  be, by phase and policy. Anything outside its cell fails.

  | Phase | Under forget | Kept |
  |---|---|---|
  | During the request (sampled at a barrier the fake engine holds) | process memory; the gateway's private temp directory (5.1 rows 4-5); the echoing engine's output file (row 7) | the same, plus the execution spool file of an async job (row 3) |
  | After the request ends | the echoing engine's output file only; the private temp directory is empty; no spool file | the kind's table (`kept_requests`, `asset_prompts` or `responses`, WAL included) and the echoing engine's output file only; no spool file |

- **Credential markers** must not appear in any output the gateway
  controls, in any phase:
  - its databases and receipts;
  - its logs and its private temp directory;
  - the `accepted` and `sent` parts and record metadata;
  - management, MCP and CLI outputs;
  - LiteLLM's paths and stdout.

  The fake engines echo content but never echo a credential on success.
  A separate case labelled D45b covers an engine that echoes its own
  credential in a success body. That case asserts only that the gateway
  composed nothing around it.
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
- **The engine output file.** The echoing fake engine's output file holds
  the marker. That positive control is the reason row 7 of section 5.1 is
  disclosed, not claimed clean. The assertion there is that the "never
  save" text for that engine includes the engine-output line. A second
  fake engine that prints nothing leaves no marker.
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
- **Noninterference** (D32 e), scoped to what the gateway composes. Two
  configs that differ only in secret bytes give identical gateway-composed
  output: record metadata, omissions, the `accepted` and `sent` parts,
  ledgers, receipts, and the Debug view's headers and error parts. A
  `received` success body is the trusted engine's product (D45b) and is
  outside this property, because an engine may echo a credential it was
  given.

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
   The dry run shows the admission outcome and "not loaded, so it will be
   started". Confirm. The new receipt has `replay_of`.
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

**Rules for every slice.**

- Each slice ships on its own, passes the gate, and gets an independent
  model review before it merges (handoff standard).
- A control ships with every surface that supports it (REST, MCP and
  CLI). So no slice leaves a surface gap behind.
- A slice's acceptance uses only features shipped by then. The tests of
  section 16 named under a slice run in the part that applies to what
  exists. Slice 10 runs the whole matrix cumulatively.

| # | Slice | Changes behaviour? | Acceptance |
|---|---|---|---|
| 1 | **Contract and data, no behaviour change.** `registry/retention.yml` and its schema, with groups and kinds; `gateway/retention.py` with `decide`, `write`, the validator and the conversation scope rule moved in unchanged; `prompt_memory.decide` and the Responses rule call it; the settings key migration (group `prompts`); remove the three duplicated default copies (Python and web read the data through the settings API). | No | 16.1.1; 16.1.2; 16.1.3, except the new-group case; 16.1.10; 16.1.15; the existing prompt-memory and Responses suites unchanged; live: existing prompt-memory flows unchanged |
| 2 | **Receipts `retention` field** for the existing kinds, with the value guard. | Adds a field | 16.1.9; the client receipt shape snapshot updated deliberately |
| 3 | **LiteLLM keeps nothing.** See the 5.2 profile: the managed LiteLLM, the shipped Compose service and the `unmanaged` disclosure. | LiteLLM config and environment; LiteLLM errors leave the journal; chat is refused while the precondition fails | 5.2 proofs 1-5; the 16.2 LiteLLM canary and mutation tests; live checks on a scratch instance (below) |
| 4 | **Temporary files.** A gateway-private `TMPDIR`; `PrivateTmp=yes`; the STT workspace under that directory. | Moves temporary files | the 16.2 "during the request" and "after, under forget" assertions for multipart and STT; the STT suite unchanged |
| 5 | **Request history, end to end.** The group, the `kept_requests` table, capture of `accepted` and `received` with the full secret floor (3.5 rules 1-8), the per-request control on every data-plane route, the scope check and the `MakerContent` projection, the privacy permission on keys and MCP grants, and the member and household switches in the existing settings UI as guided-form rows. Surfaces: the maker routes under `/gateway/v1/`, and the MCP `requests` and `retention` tools for history and settings. Deletion, expiry and fences, including the Library cascade. The default comes from data (Tim's decision 1). | New group; off unless data says otherwise | 16.1.3 new-group case; 16.1.4 (history routes); 16.1.5; 16.1.6; 16.1.7; 16.1.8 (history and settings rows); 16.1.11 (history and settings controls); 16.1.13; 16.2 content and credential canaries; live checks 1 and 4 |
| 6 | **Facts and ledger.** The `retention_fact` schema and revisions; provider catalog seeds; protocol, definition and record levels; the ledger view and its routes and MCP action; the assembled "never save" text. | Adds views | 16.1.14; schema tests; a removed fact stays removed; the stale label; no network on read; live check 5 |
| 7 | **Debug window.** The windows table; decision step 4; the `sent` part at the internal hop and the egress builder; error-part withholding by role; the engine-page button; the `debug_window` MCP tool and the CLI operations. | New kind | 16.1.4 (Debug reads); 16.1.12; 16.1.11 (window controls); 16.2 with a window open; the administrator on an owner-stored-key withholding case; live check 2 |
| 8 | **Presets and full data editing.** The preset picker with Custom, and the YAML/JSON editor with the validator's refusals. | New UI views | each preset round-trips to its values; an edited value shows Custom; the UX review of the three layers per the design-review gate |
| 9 | **Run again.** The dry-run preview (admission outcome, residency, cost), re-authorization, the incomplete-record editor, `replay_of`, and the REST, MCP and CLI action. | New action | live check 3; access-plan and cap refusals through REST and MCP; a state change between preview and dispatch gives the real outcome |
| 10 | **Cumulative gate.** The full section 16 matrix over all kinds and surfaces. Remove the old `prompt_memory.decide` reference kept for 16.1.1. | No | all of section 16 green |

**Slice 1 as built.** Slice 1 changes no decision. These points are where
the build chose a form that the text above leaves open:

- The household policy is operator setting `retention`. Admin schema 14 moves
  `prompt_memory` to it (`gateway/retention_migration.py`) and an older gateway
  refuses the database, so a rollback cannot read the kill switch as unset. The
  stored shape is `{"never_keep", "groups": {"prompts": {"default_keep",
  "retention_cap"}}}`. The owner screen and `/admin/api/settings/prompt-memory`
  keep their wire names (`default_remember`, `never_keep`, `retention_cap`):
  they are the `prompts` group's view of the setting. `put_operator_setting`
  accepts that view under the key `prompt_memory`.
- The member rows stay in table `prompt_memory_preferences`. In slice 1 the rows
  of that table are the rows of group `prompts`; slice 5 adds the group to the
  table's key when the second group arrives. Cutoff rows get their group in the
  same slice.
- `registry/retention.yml` holds only the kinds that exist. The validator
  refuses a kind that core cannot capture, so `request_history` and
  `debug_capture` join the file in the slice that adds their capture points.
  The file also holds the household cap default (`default_cap`) and the
  kill-switch default (`household.never_keep`), which were code constants.
- Both prompt-memory settings responses gain one key, `contract`: the choices
  (with their lengths, in listing order) and every default of the group. The web
  reads them from there and keeps no copy. Nothing else in either response
  changes.
- `retention.write` takes the kind, the request's time and a `commit` function.
  It runs `commit` under the same lock that `retention_guard` held, for Library
  prompts (`prompt_memory.put`) and for the two Responses writes.

**Live checks for slice 3** run on a scratch instance with a scratch
LiteLLM database:

- chat works;
- a virtual key's budget still meters;
- no spend row and no error row is written;
- cancellation still correlates;
- a seeded `store_model_in_db` row makes chat refuse with
  `litellm_content_unverified`;
- a virtual key attached to a team with `metadata.logging` makes chat
  refuse, and a canary sent before the fix reaches no callback;
- killing LiteLLM so the watcher restarts it makes the next request wait
  for a new check.

Slice 3 deploys only after the live read-only count of section 19.

**Later, only on demonstrated need:** field-level selectors on protocol
descriptions; a blob store for request inputs ([research] R6); saved
templates ([research] R5 option 3); the widening slice Tim chooses
(section 18).

## 18. Tim's three decisions

The design does not depend on any answer. Each answer is a data value plus,
for decision 3, a named slice.

### Decision 1: the default for new request history

- **What each answer sets.**
  - Off: `groups.request_history.default_keep: false`.
  - On: `true`, with `default_retention` as chosen. The rollout then
    snapshots `keep: false` for existing members and shows them a notice
    (section 12).
  - Per route kind: not offered. One switch per group keeps the member's
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
- The LiteLLM line references in section 5.2 are for 1.86.2 in this
  machine's venv. Re-verify them on any upgrade.
- Callback-enabling environment names are not a concern: the survey found
  none in 1.86.2, and the allowlisted child environment would make such a
  list unnecessary anyway.
- Reviewer focus:
  - per-container classification instead of [research] R2's per-field
    registry (section 3.5);
  - the privacy permission on delegated credentials (section 10);
  - the three new MCP tools against `HEADLESS_AND_RESOURCES.md` "No new
    tools yet";
  - LiteLLM failing closed on the database-override precondition (section
    5.2).

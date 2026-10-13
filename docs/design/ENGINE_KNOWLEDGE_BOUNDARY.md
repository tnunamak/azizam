# Engine-Knowledge Boundary

**Status:** Design (architect's recommendation). Operationalizes the owner's binding principle.
**Last verified against code:** 2026-06-16, branch `ideogram4-quality-tuning`.

**Status update — 2026-10-12 (S2):** `gateway/engine_quarantine.py` is deleted. Its six
tables became service-record fields, seeded by `scripts/migrations/engine_service_fields.py`
and `proxy_config.yml`: the local-swap set is derived from the lifecycle driver;
`lifecycle.driver_config.activation_requires_absolute_path`; `loaded_model_identity: path`;
`aux` (`probe`, `process`, `flag`, `grants_input_modalities`); and
`context_window_probe.endpoint`. The migration also deletes the disabled `tabby` row.
`tests/test_engine_service_fields.py` runs each behaviour under two service ids.
The text below describes the earlier state.

**Status update — 2026-10-12:** Tabby support is removed (the service was disabled in
the live registry and the product is unreleased, so there is no migration shim). Five
quarantine tables existed only for it and are deleted; `LOCAL_SWAP_BACKENDS` is now
`{kobold}`. Six tables remain in `gateway/engine_quarantine.py`. A per-file
engine-knowledge ratchet (`tests/engine_knowledge_baseline.json`, enforced by
`tests/test_engine_knowledge_boundary.py`) counts every engine name in all production
Python and may only fall. Mentions of Tabby below are historical.

**Status update — 2026-10-10:** The owner-supplied audit at `fdc3c9d`
supersedes the D-1/D-2/D-3 implementation status and old code anchors below.
The engine-named classes are now generic transforms. D-2's engine framing is
removed. D-3 is complete. D-1 is resolved: `reasoning-effort-final-chat` runs
only when attached in service `config.transforms`. Admin schema v5 appends it
once to enabled local chat/completion endpoints, excluding remote providers
and the execution substrate.
Disabled services remain unchanged. Fresh bundled chat seeds declare the id as
data; later operator removal survives startup. The built-in runs after model/
alias defaults, only on `/v1/chat/completions`, and retains configured budgets
and validation errors. Existing `reasoning-effort` attachments keep their
ordinary list position and all-path behavior. The migration preserves those
attachments and adds the final chat step, matching the former two-stage behavior.
The binding principle is unchanged.

---

## Phase 2 migration

Verified against `0061d150` on 2026-10-11, after reading the full independent
`e9506bd6` engine-knowledge audit. This section supersedes the earlier status
where it calls D-1 fully resolved: attachment is opt-in, but its wire dialect
and default budgets are still compiled policy. Historical deployment claims
below are not evidence for the current configuration.

### Audit verdicts and scope

| Item | Verdict at the new base | Evidence and disposition |
|---|---|---|
| P2-1 Voice-design vocabulary | Confirmed; migrated (see Runtime migrations) | `gateway/voice_design.py:design_instruct` owns categories, aliases, precedence, ignored terms and comma rendering. `gateway/voices_api.py:_create_prompt_voice` calls it before the injected backend, so unsupported descriptions fail before dispatch. `proxy.py:_design_voice` also chooses a compiled default engine and sends a fixed voice. Migration planned. |
| P2-2 Reasoning dialect | Confirmed | `gateway/transforms.py:ReasoningEffortTransform` accepts configured budgets but defaults to its compiled table, writes `thinking_budget_tokens`, maps unlimited to `-1`, and suppresses thinking through a fixed nested key. Opt-in attachment does not make these values operator data. Migration planned. |
| P2-3 Quarantine properties | Confirmed, with existing test coverage | All ten property sets in `gateway/engine_quarantine.py` are compiled backend-ID policy in addition to `LOCAL_SWAP_BACKENDS`. The old boundary guard freezes only that last set. `tests/test_engine_quarantine.py` tests named backends and unknown examples, but does not reject every possible added member, table or changed predicate. Freeze all tables and predicate bodies now; migrate consumers later. |
| P2-4 Speech catalog/default | Partly confirmed; migrated (see Runtime migrations) | The catalog, aliases, language presets and default remain in `gateway/transforms.py`; `proxy.py:_engine_voice`, `_voice_metadata` and `list_voices` consume them. `voxtral-voices` itself is permitted declarative seed data. Speech dispatch uses the selected alias's map/default/transform, with a global transform fallback for non-alias resolution. The audit's Try `alloy` initializer is already absent at this base: `gateway/speech_voices.py` projects per-model choices and Try uses `selectedVoice`. Global backend catalog migration planned. |
| P2-5 Audio intake | Partly confirmed | `gateway/voice_audio.py` cites backend preferences and owns crop/DSP defaults. `SampleLimits` already injects duration and byte limits from `proxy.py:_voice_sample_limits`; 24 kHz mono PCM is also the stored-reference contract. `_probe_voice_quality` has a compiled fallback service ID that also needs a configured destination. Comments do not prove all DSP constants are backend requirements. Separate backend policy only where demonstrated; retain the storage contract. |
| P2-6 STT diagnostics | Confirmed; migrated (see Runtime migrations) | `gateway/stt_chunking.py:_parse_mergeable_chunk_payload` removes `timing` using `_ENGINE_DIAGNOSTIC_FIELDS` before rejecting unsupported fields for every service. `execute_chunking` feeds it raw response bytes; the `proxy.py` upload closure does not apply response transforms. The full service config is retained in `SttExecutionConfig.backend_config`, but this parser does not consume its transforms. Migration planned, pending preservation evidence. |
| P2-7 Try fallback capabilities | Confirmed | `web/src/app/try/page.tsx:loadCatalogSection` assigns every fallback `/openai/v1/models` ID to chat when the richer catalog fails. IDs alone establish no capability. Remove that inference now; use reported capabilities and keep unclassified IDs explicitly unknown. |
| P2-8 Voice-design public alias | Partly: behavior confirmed, no new engine violation | `gateway/voices_api.py:_create_prompt_voice` accepts only `auto`, then calls an injected dependency. Keep this public selection contract. Its single-model error wording and English preview script do not prove engine-specific behavior. Resolve the destination from data as part of P2-1. |
| P2-9 STT planning constants | Partly: constants confirmed, engine claim unproved | `gateway/stt_chunking.py` already reads chunk thresholds, time budgets and limits from service config. Silence merging and extraction tolerance remain generic gateway planning policy on available evidence. No migration without evidence that these vary by service. |
| P2-10 Think-tag convention | Confirmed as an existing reviewed format exception | `split_reasoning_visible_text`, `ReasoningFieldNormalizeTransform` and `StreamingThinkSplitTransform` in `gateway/transforms.py` interpret `</think>` without backend-name dispatch. Keep the existing reviewed contract; literal visible tags remain a semantic limitation. This is not a newly discovered engine branch. |

None of the ten findings is wholly wrong. Several deliberately report conditional
concerns rather than established violations. Accepted provider presets, fixed
primitive seed rows, stable alias configuration and manifest mappings remain data.

### Data homes and preservation gates

Field names described as **proposed** below are design targets, not implemented
configuration options. Each migration must expose the same operator editing path
used by seed data; moving a Python constant into another source file is insufficient.

| Item | Operator-owned destination | Required preservation proof before shipping |
|---|---|---|
| P2-1 | Selected service `config.voice_design` (proposed): ordered categories with attribute/alias lists, ignored words, separator and unmatched-prompt policy. Resolve the design service before compiling. No configured vocabulary means exact free-text passthrough with no ignored terms. Seed the legacy vocabulary only on the service that requires it. The service's request transform rows carry fixed voice, format and other wire defaults. | Capture a sanitized current service snapshot, resolve it through the real selection path, and compare old/new serialized outbound requests byte for byte for every attribute and synonym, overlaps, ignored terms, case, punctuation, fixed seed and explicit/default script. Preserve legacy error status/message and warning order. Separately verify a replacement service with no vocabulary receives the original free text and a service with a different vocabulary follows its own data. |
| P2-2 | Existing reasoning policy `effort_budgets` and `default_effort`; proposed service `config.reasoning_wire` for destination, unlimited sentinel and zero-budget kwargs. Compile this data to service `config.transforms` using existing param-map/value-map/default-params and predicates where semantics permit. Retain validation/idempotence as a reviewed generic built-in if primitives cannot preserve them. | Use current attachment order and policy to compare serialized requests and error responses for all efforts, null/absent/invalid effort, explicit existing budgets, zero, unlimited and repeated internal routing. Include a replacement destination/sentinel and prove removing the attachment survives reload. Do not claim a six-primitive replacement until existing-budget and error semantics match. |
| P2-3 | Existing service `config.default_model` and lifecycle `driver_config`; proposed fields listed below for the remaining properties. | For each property, capture current service data and compare old/new routing, activation payloads, probes, role resolution and context values. Rename the service ID without changing its properties and require identical behavior. Freeze tests alone do not establish migration parity. |
| P2-4 | Service `config.speech_voices` (proposed) owns `voices` metadata, `default_voice` and unknown-value policy. Project that through the existing `speech_voice_catalog`/catalog `speech_voices` path. Preserve existing alias `voice_map`, `default_voice` and `voice_transform` values during migration. Attach value-map rows for compatibility aliases and default-params for omission-only defaults. | With the current service/alias snapshot and reference identities, compare exact outbound request bytes for omitted, empty, mixed-case, canonical, legacy, language-preset, native, unknown and custom voice IDs on alias and non-alias routes. Compare catalog ordering/metadata too. A seed value-map currently defaults unknown supplied IDs as well as missing ones; do not silently change that behavior. Test a replacement service's novel voice and custom default separately. |
| P2-5 | Keep canonical stored WAV encoding as gateway policy. Proposed service `config.voice_intake` owns demonstrated backend-specific crop, minimum speech, gain or denoise policy. Existing global `voices` limits remain the compatibility source until explicitly migrated. Proposed `voices.quality_probe.service_id` owns the probe destination; see the migration below. | Replay identical audio fixtures and current limits through old/new preprocessing with the same ffmpeg version; compare WAV bytes, measurements, warnings and errors. Cover silence, clipping, short/long input and denoise. Verify each service's selected intake policy without changing shared stored references. Also prove probe selection, requests and failure behavior using the cases below. |
| P2-6 | Service `config.transforms` response row: `{"kind":"drop-params","phase":"response","drop":["timing"]}` on only the service that emits it. Apply after JSON decoding, before chunk validation, using the existing response pipeline. | Compare serialized final responses, status/errors and forwarded chunk requests against the old implementation using the current service snapshot, across timing/no-timing, multiple chunks and supported formats. Preserve strict rejection of every other unmergeable field. Confirm the attachment affects chunk responses at the intended boundary and does not alter direct non-chunk responses. A replacement diagnostic must need only a different row. |
| P2-7 | Capability metadata returned by discovered/admin-configured model catalogs, including fallback rows when present. Model IDs with no metadata carry unknown capability. Voice selections come from that model's `speech_voices`. | Component tests fail on the old chat inference, cover mixed known/unknown fallback rows, and assert unknown rows cannot silently become a chat request. Replace all fixture model/voice IDs and defaults, then verify selections and posted requests follow the replacement data. This is an intentional correction, not byte-preserving behavior for the old misclassification. |
| P2-8 | Keep public `auto`; destination is selected service data under P2-1. Script and seed bounds remain API defaults unless requirements prove them service-specific. | P2-1 request comparison includes explicit/default script and deterministic seed. Preserve the public unsupported-model error until intentionally revised. |
| P2-9 | Existing service `config.chunking` fields for configurable limits; remaining generic planning constants stay gateway policy. | Existing planning/extraction fixtures remain required. A future service-specific limit needs old/new plan and extracted-byte comparisons before moving it. |
| P2-10 | Existing reviewed built-ins remain; if the format becomes opt-in, proposed service `config.reasoning_format` owns delimiter/output field and attachment. No new plugin mechanism. | Preserve streaming boundaries, finish reasons and serialized nonstream responses. Add literal-tag/no-tag fixtures before any contract change; a literal visible closing tag is not currently guaranteed to pass unchanged. |

P2-5 quality-probe destination migration (proposed; runtime UNVERIFIED):

- Proposed global `voices.quality_probe.service_id` is an operator-owned registry
  service reference. Expose it in the voice-settings editor and the same settings
  import/export/seed schema; no executable default may name an engine/service.
- Before migration, obtain a sanitized snapshot of the effective old probe
  destination. A reviewed one-time migration manifest supplies its compatibility
  reference as DATA through that settings import path. Backfill only absent
  settings; preserve explicit values and never restore later operator deletion.
  Fresh installs seed a reference only alongside its configured service. If the
  old destination cannot be established, stop rather than guess.
- Differential fixtures must compare the current destination's selected service,
  method/path, outbound request bytes, authentication source, quality results,
  warnings and intake failure/continuation behavior. Rename/replace the service
  ID using only registry/settings edits; prove no call uses the old destination.
- Cover absent configuration, deleted/disabled service, timeout, unavailable
  probe, non-success and malformed responses. Require an honest unconfigured/
  unavailable result without a guessed destination. Preserve observed intake
  failure/continuation behavior or explicitly approve a change before shipping.
  Retry/cancellation must not switch destinations. Test one-time backfill,
  preservation of explicit values and persistence of operator removal.
- Proxy implementation is outside this repair's read scope. Current request and
  failure semantics and the settings/migration implementation remain UNVERIFIED;
  these are shipping gates, not runtime-preservation claims.

P2-3 property destinations, each independent of backend names:

| Frozen table | Destination |
|---|---|
| `LOCAL_SWAP_BACKENDS` | Presence of a swap-capable lifecycle driver |
| `_HAS_DEFAULT_MODEL_FALLBACK` | Service `config.default_model` |
| `_ACTIVATION_FROM_UPSTREAM_ID` | Lifecycle `driver_config.activation_model_source` (proposed) |
| `_ACTIVATION_REQUIRES_ABS_PATH` | Lifecycle `driver_config.activation_requires_absolute_path` (proposed) |
| `_HAS_AUX_MMPROJ` | Service `config.aux` probe declaration (proposed) |
| `_LOADED_MODEL_IS_PATH` / `_LOADED_MODEL_IS_HTTP_ID` | Service `config.state_type` and probe field/path/auth configuration (proposed) |
| `_ROLES_MAY_BE_UNPINNED` | Service `config.roles_allow_unpinned` (proposed) |
| `_VISION_VIA_AUX` | Capability metadata plus the configured aux probe result |
| `_CONTEXT_WINDOW_FROM_MODEL_PARAMS` / `_CONTEXT_WINDOW_FROM_LIVE_PROBE` | Service `config.context_window_source` and probe field configuration (proposed) |

### Guard changes and order

**P2-G1:** Scan `proxy.py` and every `gateway/**/*.py` file. Parse branch
conditions as Python AST so multiline comparisons are covered and comments are
not mistaken for executable policy. Keep an explicit inventory of the current
findings above; an inventory entry records debt, not approval to expand it.
The exact `protocol == "ollama"` branches in
`gateway/resource_admission_http.py:admission_protocol_body` implement a public
protocol's error envelope. They are a reviewed wire-contract
exception, not backend-ID selection. Engine-name matching remains a limited
syntactic check: arbitrary new vocabulary or renamed identifiers still need
semantic review (G3/G5).

**P2-G2:** Freeze all eleven quarantine tables, their names and values, and
one-line membership predicates. Test additions and body changes explicitly.
Migrate each property by deleting its frozen entry with its consumer migration;
do not extend quarantine for a new engine.

**P2-G6:** Guard discovered UI selections with component tests for initial,
replacement and fallback catalogs. No compiled model/voice ID should select a
backend in `web/src`; public operation aliases remain protocol contracts, but
Try should select one only when discovery supplies it or the user requests it.
The source scanner is a limited syntactic check of known model/voice property
and JSX sinks, local const/state bindings, assignments and shorthand properties.
It does not follow imports, arbitrary functions or computed data flows, or prove
runtime discovery provenance. Catalog-replacement tests remain the behavioral
backstop. G4 seed-schema enforcement and the broader G3/G5 ownership/replacement matrix
remain follow-up work, not a guarantee supplied by these guards.

Implementation order:

1. Record these verdicts; expand G1/G2 and demonstrate mutation failures.
2. Correct P2-7 and add G6 discovery/selection regressions.
3. Obtain sanitized current config fixtures; migrate P2-1 first, then P2-4
   and P2-6 only after their differential gates pass.
4. Move P2-2 dialect policy and P2-3 properties independently. Review P2-5
   thresholds against actual service requirements; retain P2-8/9/10 contracts.

**Preservation status (2026-10-11, `fix/engine-p2-runtime`):** P2-1, P2-4 and
P2-6 are migrated with differential proof; see "Runtime migrations" below.
P2-2/3/5 remain UNVERIFIED plan items. The differential fixtures were read from the
owner's registry and `proxy_config.yml` read-only; no live gateway call was made.

### Runtime migrations

The proof harness is `tests/engine_p2_differential.py`. It drives the real
routes of `proxy.app` with a sanitized copy of the owner's speech, voice and STT
service data (`tests/fixtures/engine_p2/owner_config.json`) and records each
outbound engine request byte for byte and each caller response.
`tests/fixtures/engine_p2/golden.json` holds these records from the code
before the migrations (`8a3147f0`). `tests/test_engine_p2_differential.py`
runs the migrated code with the migrated data
(`owner_config.migrated.json`, which a test holds equal to the shipped
`proxy_config.yml` sections) and requires the same records, except for the
intended changes it names.

| Item | Status | Data home | Proof |
|---|---|---|---|
| P2-1 | Migrated | `model_aliases.speech.engines.<voices.design_engine>.voice_design` in `proxy_config.yml`: ordered `vocabulary` (category → attribute → words), `ignore`, `separator`, and the engine `request` template (fixed voice and response format). No `voice_design.vocabulary` means the exact description is passed through. `voices.design_engine` has no compiled default; an unset value answers 503 "not configured". | 279 voice-design cases byte-identical (every vocabulary word plain, upper case and in a sentence; overlaps; words inside words; ignored and noise words; punctuation; non-ASCII; explicit and default script; fixed, string, zero and default seed; errors; engine 400/500; disabled engine). Replacement engine without vocabulary receives the free text with only `input`/`instructions`/`seed`. |

| P2-4 | Migrated | Each `model_aliases.speech.engines.<id>` record: `voices` (the complete list; an id or `{id, name, language, gender, description}`), `voice_map` (accepted spelling → voice id, never a listed voice), `default_voice` (used only when no voice is named). `model_aliases.speech.voice_aliases` holds the older spellings (Voxtral presets, retired names), applied before `voice_map`. A tier (`stable` list) sends a named voice to its first deployed engine, in the configured order, that accepts it, and falls back only among engines that accept it; no accepting engine is `400 voice_not_available`. `GET /v1/audio/voices?model=` lists every voice some engine of that model accepts (listed voices in engine order, then accepted spellings), and the list without `model` is that of `tts-1`; `default_voice` is the first engine's. Requests that resolve to no engine use the engine of service `tts`. Try's picker (`gateway/speech_voices.py`) shows exactly the voices the model's engines list, names as written. `voice_transform` and the `voxtral-voices` registry entry are removed. | Of 937 speech cases (12 model names × 78 voices: omitted/empty/null, the 13 OpenAI names in lower and upper case, mixed case, the 11 old spellings, the 16 presets, native ids of every engine, unknown and whitespace names, custom ids) plus the voice listing, 713 cases are byte-identical. The remaining 223 are the intended changes the test classifies and counts: 146 unknown voices that the old code silently replaced by the engine default now answer `400 voice_not_available`; 76 voices that only a later engine of a tier lists reach that engine with exactly the request that naming the engine sends (the 16 presets and `DE_Female` on `tts-1`, `gpt-4o-mini-tts`, `tts` and `tts-fast` reach OmniVoice; four Kokoro ids on `tts-1-hd` and `tts-quality` reach Kokoro); and the listed Kokoro voice `bm_george` reaches Kokoro instead of `af_heart`. The listing changes on purpose: for every model, each listed voice is spoken (200) and each voice the harness had spoken is listed, Try's choices equal the listed voices, and the default list (now led by Supertonic's F1–M5, default `F1`) equals that of `tts-1`. In the ungoverned discovery snapshot (Chatterbox enabled) only the four voice-listing paths change, the same way: Supertonic's F1–M5 are added and `default_voice` is `F1`; every earlier entry is byte-identical. A replacement engine's novel voice and default, and an engine with no voice list, follow their own data. |

| P2-6 | Migrated | Service `config.transforms` row `{kind: drop-params, phase: response, drop: [timing]}`. `stt_chunking.execute_chunking` reads the service's rows from `SttExecutionConfig.backend_config` and runs them through `run_response_pipeline` on each decoded chunk, before the strict merge check; direct (unchunked) answers and request transforms are untouched. Admin schema v6 (`gateway/stt_chunk_transform_migration.py`) attaches the row once to every speech-to-text service (any STT capability spelling, or a `chunking` block), enabled or not, because the old rule applied to all; services that also declare chat/completions are skipped, since service response rows also run on chat answers. `proxy_config.yml` seeds the same rows for new installs. A removed row stays removed. A row that cannot run is a setup failure found before any chunk upload (the service's `setup_failure_policy` applies). | 65 transcription cases byte-identical (each of the 5 owner STT services × plain, timing, usage, timing+usage, timing on one chunk only, unmergeable `segments`, at 2 and 3 chunks, plus a short direct request): forwarded chunk requests, merged transcripts and errors. Without the row, a timing chunk is refused (502) while plain chunks and other services are unchanged. Which engines actually emit `timing` was not checked against live engines (UNVERIFIED); the owner can remove the row from any service that does not. |

Intended P2-4 changes beyond the owner fixture: a design preset such as
`de_female` sent to Chatterbox (disabled in the owner's registry) is now
refused, because Chatterbox lists only the 13 clips; a custom voice may no
longer take a preset name; an engine without a `voices` list passes an unknown
voice through instead of its `default_voice`. The old spellings remain accepted
until the UX batch 1 cutover removes `voice_aliases`.

The `request` template is not a transform row: request transforms append keys,
so they cannot keep the old key order, and byte-identical requests were the gate.

### Verification at the reviewed base (historical)

The counts below predate the review-repair patch. They are not validation of
that patch; its bridge check receipts record the current results.

- Python: **382 passed, 31 subtests passed** across
  `test_engine_knowledge_boundary`, `test_engine_quarantine`,
  `test_voice_design`, `test_speech_voice_choices`, `test_transforms_primitives`,
  `test_transforms_golden`, `test_stt_chunking`, `test_reasoning_service_opt_in`
  and `test_reasoning_service_migration`. Used `scripts/run-hermetic-pytest.sh -q`
  with those nine `tests/*.py` paths. The runner archives the committed tree
  and supplies an isolated locked Python environment; it does not use the
  operator's databases. One Authlib deprecation warning remains.
- Boundary regressions failed before the guard changes for multiline branches,
  loop/comprehension iterables, match/case branches and quarantine expansion.
  The final standalone boundary run passed **14 tests** with the specified
  workspace Python and `pytest --noconftest`; this source-only test imports no
  gateway runtime. It also passed in the isolated combined run above.
- UI: **68 passed** across `src/app/try/page.spec.tsx`,
  `tests/try-workflow-media.spec.tsx`, `tests/try-model-list.spec.tsx`,
  `tests/try-prompt-memory.spec.tsx` and
  `tests/model-voice-discovery-guard.spec.ts`. The two fallback cases failed
  against the old behavior, as did the missing-catalog selection case.
  Replacement voice/model requests, picker reclassification and six injected
  literal-selection examples are covered.
- `npm --prefix web run check` passed TypeScript, lint, React Doctor and the
  production build. React Doctor reported 551 nonblocking warnings, not
  evaluated as a baseline delta here. Next.js reported existing export-rewrite
  and workspace-root configuration warnings. Generated `.next`/`out` output
  was removed afterward.
- Independent source review found a picker-refresh bypass; the fix clears
  picker choices whose capability disappears while retaining intentional URL
  overrides. The subsequent review found no remaining blockers. No live
  gateway or browser end-to-end run was performed.

All test/check launches checked system load first. Runtime equivalence for the
planned migrations remains UNVERIFIED; these test counts do not establish it.


---

## The binding principle (restated, so this doc is self-contained)

The gateway must **not** be in the business of tracking inference engines **in code**.
Engines churn (~2yr half-life); the OpenAI/Anthropic/ElevenLabs-style contracts the
gateway fronts are stable. The gateway's durable value is engine-**agnostic**: a stable
OpenAI-compatible front door (auth, access plans, routing, honest observability) over whatever
the operator points it at.

**The line:** engine-specific knowledge is acceptable as **DATA** (seed rows, presets,
declarative transforms) — because that is indistinguishable from what an operator could
configure themselves, and it churns harmlessly (add/delete a row). It is **not**
acceptable as **CODE** (a per-engine Python class or `if engine ==` branch) — because code
is the maintenance treadmill (every new engine = a PR + release we own).

**The test** for any piece of engine support: *"Could an operator have configured this
themselves through the same user-facing interface?"* YES → data. NO → it's the bad kind;
it must be cut, or live behind the code escape hatch with a reviewed justification.

The research is unambiguous: the dominant variance class (request/response shape) is provably
expressible as declarative config (Kong/Tyk transformers, LiteLLM's config path). **We push
everything to data, keep the one in-tree code escape hatch the repo already has
(`register_builtin`), and build zero new plugin machinery.**

---

## 1. THE BOUNDARY (precisely)

Four buckets. Everything engine-touching in the codebase lands in exactly one.

### Bucket A — DATA (seed rows / operator-editable config)

These are declarative transforms expressible with the **fixed, engine-agnostic primitive set**
already in `gateway/transforms.py` (`value-map`, `drop-params`, `default-params`, `param-map`,
`clamp`, `message-inject`). They carry **zero engine knowledge in code** — the engine knowledge
is the *content* of a config row.

| Item | Location today | Primitive(s) | Becomes |
|---|---|---|---|
| `voxtral-voices` | transforms.py:887 | `value-map` (voice) | seed transform row, attached to speech service |
| `ollama-unsupported-params` | transforms.py:968 | `drop-params` | seed transform row |
| `llamacpp-cache-prompt` | transforms.py:986 | `default-params` | seed transform row |
| `mistral-penalty-clamp` | transforms.py:998 | `clamp` ×2 | seed transform row |
| `tabby-penalty-floor` | transforms.py:1020 | `clamp` ×2 | seed transform row |
| `TOOLCALL_MARKERS` table | toolcall_detection.py:52 | data table (5 tuples) | **stays as data, already on the right side** — see note |
| `PROVIDER_PRESETS` | service_registry.py:503 | preset dicts | stays data; gains optional `transforms` list |
| `VOICE_MAP` (proxy.py:2665) | duplicate of voxtral-voices | — | **DELETE** after dispatch wiring lands (§3.1) |

**Rule for Bucket A:** the *primitive kinds* are a fixed, closed vocabulary (agnostic code).
The *instances* (which params to drop, which values to map) are data. New engine quirk of this
shape = a new row, no PR.

> **Note on `TOOLCALL_MARKERS`:** this 5-tuple table is a membership test ("does this content
> look like an unlifted tool call?"), not a parser. Family names are engine-neutral output labels.
> It is already data-shaped and tiny; a new family is one string. It is acceptable where it is.
> It does **not** need to move into the DB to honor the principle, because adding a row to a
> hand-maintained Python list of opaque marker strings is not a per-engine *branch* — there is
> no `if family ==` logic keyed off it in the detection hot path. (If the owner wants it
> operator-visible later, it can move to the same transform store; that is a nicety, not a
> principle violation.)

### Bucket B — AGNOSTIC INFRA (stays as code, correctly)

This is **not** engine knowledge — it is a **deployment pattern** the operator opted into. It
applies to a closed set of locally-managed backends and would exist even if every engine on
earth spoke perfect OpenAI.

| Item | Location | Why it's infra, not engine-knowledge |
|---|---|---|
| `_LOCAL_SWAP_BACKENDS = {kobold, tabby}` | proxy.py:591 | model-swap lifecycle (start/stop/restart, GGUF path) is a deployment choice |
| `if target in (tabby, kobold)` routing/health | proxy.py:964+ (~26 sites) | routes to the managed local-swap endpoint; not request-shape knowledge |
| `TABBY_HOST`/`KOBOLD_HOST` config extraction | proxy.py:336 | connection config for the two managed backends |
| systemd service-name handling | various | process lifecycle |

**Honest caveat (a real open call — see §4-E):** these branches are *named* after two specific
engines, which reads like the treadmill even though it is structurally a lifecycle concern. The
principle is satisfied *today* because the set is **closed and frozen** (two entries, no growth
intended) and the branches do **request lifecycle**, not request-shape transformation. The risk
is creep: a third managed engine tempts a third branch. The enforcement check in §5 freezes the
allowlist so creep is caught. The long-term cleanup (generalize "managed local-swap backend" to
a DB capability flag) is desirable but **out of scope** for this boundary pass — it is plumbing,
not engine knowledge, and touching 26 sites now would be a risky rewrite for no principle gain.

### Bucket C — SPEC-REPAIRS (always-on, engine-agnostic by construction)

These repair **OpenAI-spec violations** in backend output. They are keyed off the *shape of the
violation*, never an engine name. They are correct exactly because any conformant backend is a
no-op under them. They stay always-on in code; they are the gateway's "honest front door"
keeping its promise.

| Item | Location | Trigger (shape, not engine) |
|---|---|---|
| `normalize_toolcall_args` | toolcall_detection.py:303 | `arguments` is a dict → JSON-encode (spec wants string) |
| `normalize_toolcall_finish_reason` | toolcall_detection.py:343 | `tool_calls` present but `finish_reason != tool_calls` |
| `reasoning-field-normalize` | transforms.py:717 | leaked `</think>` tags in content → strip + promote |
| `streaming-think-split` | transforms.py:745 | `</think>` split across SSE chunks (stateful; cannot be a declarative primitive) |

**Rule for Bucket C:** a spec-repair is legitimate **only if** its trigger is a structural
predicate with no engine name. `normalize_toolcall_args` "happens to" fix TabbyAPI and
`finish_reason` "happens to" fix Ollama — but neither names them, and both no-op on a
conformant backend. That is the test they pass. They are not on the treadmill: a new engine
with the same violation is fixed for free, with zero code change.

**Note on `streaming-think-split`:** it is stateful (carries a rolling buffer across SSE
chunks), which is a genuine structural reason it cannot be expressed as a stateless declarative
primitive. It is not engine knowledge — it is a spec-preservation mechanism for chunked
streaming. It lives correctly as a reviewed built-in in code.

### Bucket D — IRREDUCIBLE-CODE (genuinely cannot be a data primitive — with honest status)

**Superseded status — 2026-10-10:** The table and D-1/D-2/D-3 proposals
below record the 2026-06-16 design history. Their pending work and line numbers
are not current implementation evidence. The current status is in the update above.

| Item | Location | Why irreducible | Current status | Verdict |
|---|---|---|---|---|
| `qwen3-thinking` (Qwen3ThinkingTransform) | transforms.py:567 | 400 validation sentinel; the rest is decomposable (see §D-1) | **Auto-fires globally today** (proxy.py:3217, 18896) | KEEP as a reviewed built-in BUT demote to opt-in. §D-1 describes the required migration. |
| `koboldcpp-null-strip` (KoboldCppNullStripTransform) | transforms.py:854 | value-based filter; `drop-params` is key-list only | Registered but attached to nothing (no-op today) | **CUT the engine framing** → keep as generic `drop-null-params` primitive. §D-2. |
| `advisory_for_backend` engine branches | toolcall_detection.py:97 | per-engine flag literals in code; collapses to engine-flag string constants | **Fires today** on every unlifted tool call advisory | **CUT the engine branches** → collapse to one generic advisory. §D-3. |

#### D-1 `qwen3-thinking` → keep as built-in, but MIGRATE from auto-fire to opt-in

**Historical status (2026-06-16; superseded):** `_Qwen3ThinkingTransform().apply(body)` was called
unconditionally on every `/v1/chat/completions` request at **two sites** (proxy.py:3217 and
proxy.py:18896). The "data-attached, never auto-fired" model described in this design is the
historical migration proposal. That intermediate implementation has now been replaced by the opt-in D-1
migration described in the current status above.

**What it actually does** (the honest decomposition):

1. `reasoning_effort` absent → no-op. Pop it if present.
2. Validate against `_VALID` set. If invalid → **return 400 error string** (the genuinely
   irreducible part — no declarative primitive emits a 400 sentinel).
3. If effort not in `{none, minimal}` → strip only (already popped), done.
4. If effort in `{none, minimal}`: run `apply_thinking_suppression` — which sets
   `chat_template_kwargs.enable_thinking = False` (expressible as `default-params`) and
   injects `/no_think` into the first system message (expressible as `message-inject`).

Steps 1/3/4 are therefore decomposable into existing primitives. **The true irreducible
residue is step 2**: a `validate-enum → 400` sentinel. The mutation path (steps 3-4) *could*
in principle be replaced by a 3-primitive pipeline, but the 400 path cannot.

**What it does NOT do** (correcting a prior false claim): `apply_thinking_suppression` is NOT
shared with the background-removal helper. Background-removal (proxy.py:5180-5203) directly
embeds `_QWEN3_NO_THINK_HINT` in a hardcoded payload dict and sets
`{_QWEN3_ENABLE_THINKING_KEY: False}` inline — it does not call `apply_thinking_suppression`
or the `Qwen3ThinkingTransform`. They share a *string constant*, not the function.

**Why keep it** (despite being engine-flavored): the `reasoning_effort` parameter is an
OpenAI-spec field the gateway promises to support. The validation logic is a spec-enforcement
responsibility of the gateway, not raw engine tracking. The suppression it applies is
legitimately configurable data (any operator can attach it to a backend that needs it). Its
continued existence as a named built-in is justified precisely by the 400 validator — the one
piece that cannot be data.

**Historical opt-in proposal (not the current implementation):**
- Remove the two unconditional call sites at proxy.py:3217 and proxy.py:18896.
- Move the `reasoning_effort` processing responsibility to the per-backend transform pipeline
  (§3.1): a backend that needs thinking suppression gets `"transforms": ["qwen3-thinking"]` in
  its config, exactly like any other transform.
- The 400 validation on invalid `reasoning_effort` values MUST still be enforced — move it to
  a gateway-level pre-flight check (before backend dispatch, engine-agnostic: "is this a valid
  OpenAI reasoning_effort value?") rather than as an engine-specific transform.
- This migration is a **behavior change** for backends that do not use Qwen3-style thinking
  suppression (they currently have `reasoning_effort` silently popped from every request; after
  migration it passes through unless the backend has the transform attached). This is an **open
  call (§4-A)** — the owner decides whether to migrate now or leave the auto-fire sites and
  document them as "pending opt-in migration".

#### D-2 `koboldcpp-null-strip` → rename to a generic primitive; cut the engine name

**Completed — 2026-10-10, owner-supplied audit:** The transform is now generic.
The engine framing is removed. The following rename proposal is historical.

The behavior (drop keys whose value is null) is **not** Kobold knowledge — plenty of strict
backends 422 on explicit nulls. The only engine-specific thing about it is its *name*. Promote
it to a generic primitive `drop-null-params` (or a `drop-params` mode `when_value: null`),
register it engine-neutrally, and let any backend attach it. The Kobold-ness becomes *which
seed row references it*, not the code. **This is a cut of engine framing, not new machinery.**

The `KoboldCppNullStripTransform` class name also triggers the class-name guard in §5 until
the rename is complete. This is currently in the allowlist; removing it from the allowlist is
the signal that D-2 is done.

#### D-3 `advisory_for_backend` → collapse the per-engine branches AND the engine-flag literals

**Completed — 2026-10-10, owner-supplied audit:** D-3 is done. The following
description and recommendation record the superseded design work.

This is the clearest treadmill in the tree, and it has two layers:

**Layer 1 (branches):** 6+ `if "vllm" in hint` / `if "sglang" in hint` / `if "llamacpp" in
hint` branches at toolcall_detection.py:97-130. Each branch carries a remediation string.

**Layer 2 (flag literals in the generic fallback):** Even the "generic / unknown" fallback at
lines 132-137 embeds engine-flag string literals (`--tool-call-parser`, `--jinja`, `--enable-
auto-tool-choice`). And `_parser_family` (lines 140-149) is an engine-family → flag-value
mapping dict. These mean the so-called "generic" branch is also engine-specific code.

**This is the payload the red-team flagged:** the `_parser_family` dict plus the engine-flag
string literals in the generic advisory both pass a branch-name grep but are still engine
knowledge in code. The enforcement test (§5) must catch these too.

**Recommendation: CUT ALL of it.** Replace the entire `advisory_for_backend` function with a
single engine-agnostic advisory:

```
Detected {fam}-format tool calls in content but tool_calls is empty.
Your backend is not lifting tool calls to the tool_calls field.
Check your backend's tool-calling documentation to enable native tool call parsing.
```

Drop `_parser_family` entirely. The detection (the marker table) is the durable value. The
per-engine remediation prose is the churn. If a specific operator wants exact flag strings for
their specific backend, that belongs as an **optional `tool_call_advisory` string field on the
service record** (operator-authored data, editable in the admin UI), not as code we maintain
against six backend changelogs.

This is a genuine behavior change (advisory text becomes more generic), so it is an **open call
(§4-D)** — but the architect's recommendation is unambiguous: cut it.

---

## 2. THE VERDICT ON A PLUGIN SYSTEM

**No plugin system. Not now, and almost certainly not ever for this gateway.** Data + the
existing in-tree escape hatch + cutting three small things suffices. Building a plugin system
now would itself *be* the treadmill the owner is avoiding.

The reasoning, plainly:

1. **The irreducible set is tiny and shrinking.** After this pass it is effectively *one*
   reviewed request-mutation (`qwen3-thinking`, the 400 validator) plus the always-on spec-
   repairs (which are not engine code at all). One built-in does not justify a plugin protocol
   — that is the textbook YAGNI smell ("a hook system with one registered handler").

2. **The repo's one code escape hatch is `register_builtin` — be precise about what it is.**
   `gateway/extensions.py` is a **workflow-input validator** — it validates extension request
   fields and rejects invalid ones; it has no `on_request`/`on_response` mutation hooks.
   `extensions/bundled/` contains **workflow/adapter YAML packages** (ComfyUI workflows,
   extension manifests), not Python request-mutation code. Neither is a plugin system for
   request/response transformation.
   
   The **only** code escape hatch for request/response mutation is
   `REGISTRY.register_builtin(name, instance)` in `gateway/transforms.py`, where `instance`
   exposes `on_request(body, ctx)` and `on_response(payload, ctx)`. This is the named-built-in
   registry — a 2-method protocol, no RPC, no manifest, no subprocess. It is in-tree, reviewed,
   restart-to-apply. It is **already the correct last-resort boundary** and it needs no new code.

3. **Promotion path exists but is premature.** If isolation/untrusted-code/non-Python ever
   becomes real, the same 2-method interface moves to a stdio subprocess — a transport swap of
   the same contract. That is cheap precisely because the contract stays identical. **Because
   adding isolation later is cheap, we build nothing speculative now.**

**Standing policy (adopt verbatim):**
> New engine quirk? (1) Make it a declarative transform row if you possibly can. (2) If it
> needs imperative logic, add a reviewed named built-in via `register_builtin` and attach it by
> id as data — never auto-fire it by engine detection. (3) Only after several real built-ins
> **and** a concrete isolation/trust/non-Python need, move that same 2-method interface out-of-
> process. Never build a registry or a versioned plugin protocol on speculation.

---

## 3. THE MIGRATION (code-registered → seed DATA, behavior-identical, operator-editable)

Goal: the five Tier-1 packs and the voice map stop being orphaned code registrations and become
**seed rows attachable per-service and editable in the admin UI**, with the agnostic plumbing
that makes per-service transforms actually fire. **No per-engine code is added anywhere.**

### 3.0 Why a plumbing change is needed (the data path is currently broken)

Verified against code: the infrastructure exists but is **not wired end-to-end**.
- `run_request_pipeline` / `run_response_pipeline` exist and are ready (transforms.py:482-519).
- The registry stores transforms by name; the honesty-law H1 passthrough already carries unknown
  config keys through `normalize_service_record` (line 453, `config` is deep-copied verbatim) and
  through `service_form_to_raw` (line 843, the H1 passthrough loop). So a `transforms` key on a
  service's `config` **already survives** create/edit/PATCH with zero schema change.
- **The gap:** `_RUNTIME_PROMOTED_FIELDS` (service_registry.py:231) does **not** include
  `transforms` — *but it does not need to*, because the field can live inside `config`, which
  `runtime_backend_from_service` copies verbatim (line 246). The genuine gap is the **dispatch
  site**: `_forward_litellm_proxy_request` (proxy.py:19064-19108) builds the execution request
  without ever reading or applying a per-backend transform list. Only the hardcoded
  `["voxtral-voices"]` at proxy.py:3721 fires, and only for `/v1/audio/speech`.

So the migration needs **exactly one agnostic plumbing change**: read the per-backend transform
list at the dispatch site and run the pipeline. Everything else is data.

### 3.1 The one agnostic plumbing change (request + response)

At `_forward_litellm_proxy_request` (proxy.py, after `body_for_litellm` is finalized ~line 19078,
before the `LlmExecutionRequest` is built at 19091):

```python
# Per-backend declarative transforms (data, attached on the service record).
# 100% engine-agnostic: we read a list of transform ids off the resolved
# backend config and run the fixed primitive pipeline. No engine branching.
backend_cfg = resolved_backend_config or {}   # the config[backends][sid] dict
transform_ids = backend_cfg.get("transforms") or []
if isinstance(transform_ids, list) and transform_ids:
    body_for_litellm = _run_request_pipeline(
        transform_ids, dict(body_for_litellm), _TransformContext(), _TRANSFORM_REGISTRY,
    )
```

And symmetrically after the response is received (proxy.py ~19108+), run
`_run_response_pipeline(transform_ids, payload, ...)` on the decoded body for non-streaming
responses (streaming response transforms remain opt-in/stateful and out of scope for v1; the
five Tier-1 packs are all request-phase, so response wiring is additive, not required for
parity).

**Properties:**
- Backends without a `transforms` key are untouched (empty list → no-op). **Behavior-identical**
  for the current 8 services until an operator opts a backend in.
- No `if backend ==`, no engine name, no preset sniffing. The list comes from data.
- `voxtral-voices` at proxy.py:3721 can stay as-is for the speech path, **or** (cleaner) the
  speech service gets `"transforms": ["voxtral-voices"]` on its record and the hardcoded list is
  removed. Recommend the latter as a follow-up once the dispatch wiring is proven; it deletes the
  proxy.py:2665 `VOICE_MAP` duplicate too. (Open call §4-B.)

### 3.2 Seed the five packs + voice map as DATA rows

The transform *definitions* already exist as registry entries. The migration makes them
**operator-visible and operator-editable** by either (a) storing each as a transform row in
`admin.db` (a small `transforms` registry document, parallel to services), or (b) — simpler,
recommended for v1 — keeping the agnostic primitive *definitions* registered by name in
`transforms.py` (they are a closed vocabulary of instances we author) and exposing **attachment**
as the editable surface: an operator picks transform ids for a service.

The architect's recommendation is **(a) eventually, (b) for v1**, because the dominant operator
need is *attach/detach + edit params on a specific backend*, and the cheapest honest version is:

- Service edit dialog gains a **Transforms** widget (parallel to the existing Models list editor):
  add/remove transform rows, each row = `{kind, …params}` using the fixed primitive vocabulary.
- Stored as `config.transforms` (already passes through — §3.0). No schema migration.
- `PROVIDER_PRESETS` gains an **optional `transforms` list** so selecting "Ollama" pre-fills
  `["ollama-unsupported-params"]` etc. as **editable seed data** (operator can delete it). This
  is the "seeded native preset" rung — indistinguishable from what the operator could type, and
  it churns harmlessly.

This closes the user-config-surface gap with **agnostic plumbing only**: a widget that
reads/writes a list of declarative primitive rows. No per-engine code.

### 3.3 Behavior-identical guarantee

- Current production config has **no** `transforms` keys → pipeline is a no-op → **identical**.
- The packs were already registered-but-attached-to-nothing → they fired for **no one** before,
  and fire for **no one** after, until explicitly attached. Zero behavior change on migration.
- `voxtral-voices` keeps firing on the speech path (whether via the hardcoded list or the seeded
  service row) → **identical** voice mapping.
- The migration is purely *additive capability* + *three small cuts* (D-2 rename, D-3 advisory
  collapse, VOICE_MAP delete), each independently testable against the GREEN suite (1963 passing).

---

## 4. OPEN CALLS FOR THE OWNER

**Historical proposals (2026-06-16):** D-1, D-2 and D-3 are now resolved as
described in the current status above.

Each is a genuine judgment call (behavior change, schema surface, or product stance), presented
as recommendation + tradeoff. None is taken unilaterally.

**A. Migrate `qwen3-thinking` from auto-fire to opt-in (the §D-1 migration).**
*Recommendation: DO IT.* The current auto-fire on every `/v1/chat/completions` request is the
most obvious boundary violation in the tree: engine-specific code (Qwen3-style thinking
suppression) runs for every backend regardless of whether it uses Qwen3. After migration,
backends that need it get `"transforms": ["qwen3-thinking"]`; others are clean.
*Tradeoff:* `reasoning_effort` will passthrough to backends that don't understand it (currently
it is silently stripped for everyone). The gateway-level pre-flight validator for
`reasoning_effort` format (not Qwen3 suppression — just "is the value a legal enum?") should
remain always-on if that validation is desired for all backends. If the owner prefers to leave
the auto-fire sites and document them as "pending migration", the boundary test's allowlist must
include them explicitly (named exceptions, not silent passes).

**B. Move `voxtral-voices` off the hardcoded speech path onto a seeded service row + delete the
proxy.py:2665 `VOICE_MAP` duplicate.**
*Recommendation: YES, as a follow-up* once §3.1 dispatch wiring lands and is tested.
*Tradeoff:* small risk of a voice-mapping regression on `/v1/audio/speech` during the move
(mitigated by the existing voice tests). Upside: deletes a duplicate (single source of truth) and
removes the last hardcoded transform attachment.

**C. Add a Transforms widget to the service edit dialog now, or ship dispatch-wiring + preset
seeds first and add the UI later.**
*Recommendation: ship the dispatch wiring + preset seeds first (§3.1, §3.2), add the UI widget as
the immediate next step.* Without the widget, operators can attach transforms only via the seeded
presets or a raw config edit — enough to prove the data path before investing in UI. The widget
is agnostic plumbing (a list editor), carrying no principle risk; it is purely a sequencing call.

**D. Collapse `advisory_for_backend`'s per-engine branches and flag literals into one generic
advisory (cut the treadmill) — accepting that remediation text becomes more generic.**
*Recommendation: YES, cut.* Operators lose copy-paste-exact flags for vLLM/SGLang/llama-server;
they gain an advisory that never goes stale and is engine-agnostic. If exact per-backend
remediation is valued, expose it as an **optional operator-authored `tool_call_advisory` string on
the service record** (data). The detection (marker table) — the actual durable value — is unchanged
either way. Note: cutting the generic fallback's engine-flag literals too is required (not
optional) for the boundary test to pass clean (§5.4).

**E. Long-term: generalize `_LOCAL_SWAP_BACKENDS = {kobold, tabby}` to a DB capability flag.**
*Recommendation: DEFER (not part of this boundary pass); freeze the set via the §5 check now.*
The two engine-named branches read like the treadmill, but they are lifecycle infra, the set is
closed/frozen, and refactoring 26 call sites is a risky rewrite for no principle gain *today*.
The enforcement check freezes the allowlist so it cannot grow; generalizing it is a separate,
schedulable cleanup.

**F. Store transforms as first-class DB rows (a `transforms` registry doc) vs keep the primitive
*definitions* registered in `transforms.py` and store only *attachments* in service config.**
*Recommendation: attachments-in-config for v1 (§3.2b); first-class rows later if operators want
to author novel transforms unattached to a backend.* Attachments-in-config needs zero schema
change and rides the existing honesty-law passthrough; first-class rows add a registry surface
but enable transform reuse/sharing. Start simple.

---

## 5. ENFORCEMENT (make the boundary stick — no treadmill creep)

**Historical enforcement proposal (2026-06-16):** The four-file scope,
proxy allowlist and single-table freeze described below are superseded by
Phase 2 G1/G2/G6 above. Keep these details as the original design record.

A boundary that isn't enforced erodes. Add a **guard test** to the suite that fails CI if new
per-engine *code* appears in the core. This is the single most important durable artifact of this
design.

### 5.1 The guard test (`tests/test_engine_knowledge_boundary.py`)

A static check (regex/AST over `proxy.py`, `gateway/transforms.py`, `gateway/service_registry.py`,
`gateway/toolcall_detection.py`) with a **frozen allowlist** of the legitimate existing items.
It fails if any of the checks in §5.2–§5.5 trigger outside the allowlist.

The allowlist lives in the test file as an explicit, commented dict (`{file:line → reason}`), so
every legitimate exception is documented at the choke point. Adding to it is a reviewable diff
that forces the author to justify the exception against the principle.

### 5.2 Check 1: engine-named branches in code

Pattern: `if <expr> == "<engine>"` or `"<engine>" in <expr>` where `<engine>` is in the
denylist:

```
vllm, sglang, ollama, mistral, qwen, gemma, hermes, command_r, tgi, lmstudio,
llama-cpp-python, llama_cpp_python, llama-server, llamacpp, tabby, kobold,
koboldcpp, tabbyapi
```

**Allowlisted (Bucket B — lifecycle infra, frozen):** the ~26 `tabby`/`kobold` sites in
`proxy.py` for `_LOCAL_SWAP_BACKENDS` lifecycle routing. These are enumerated in the allowlist
with the explicit reason "managed local-swap lifecycle infra, set frozen per §4-E".

**Status — 2026-10-10:** D-3 is complete per the owner-supplied audit.
The former advisory-branch exception and old line numbers are superseded.
The guard must prevent reintroducing engine-specific advisory branches.

New match outside the allowlist → test fails with:
> *"New engine-named branch at {loc}. Engine knowledge belongs in DATA (a transform row /
> preset), not code. If this is genuinely irreducible, add a reviewed named built-in and attach
> it by id — see docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md."*

### 5.3 Check 2: engine-named classes

Pattern: a class whose name matches `*(Ollama|VLLM|Mistral|Qwen|Gemma|Kobold|Tabby|Llama|
SGLang|LMStudio)*Transform` beyond the frozen allowlist.

**Status — 2026-10-10:** The former engine-named classes have been renamed
to generic transforms per the owner-supplied audit. Their pending class-name
exceptions are superseded. D-1 is resolved by the opt-in service attachment and one-time admin schema v5
migration described at the top of this document.

Growing the allowlist is a reviewable diff — the choke point that surfaces the decision.

### 5.4 Check 3: engine-flag string literals (the red-team gap)

This is the check the prior design omitted. Engine-knowledge-as-data-literal — flag strings
that embed engine CLI knowledge in code — must also be caught.

**Pattern:** presence of any of the following literals in the four core files:
```
--tool-call-parser
--enable-auto-tool-choice
--chat_format
--jinja
chatml-function-calling
functionary
```

**Status — 2026-10-10:** D-3 is complete per the owner-supplied audit.
The pending advisory and parser-family literal exceptions are superseded.
The guard must prevent reintroducing those engine-flag literals.

**After D-3 is complete:** any appearance of these literals in the four core files is a hard
failure. If an operator wants to convey exact engine flags, it belongs in a `tool_call_advisory`
field on the service record (data), visible in the admin UI.

### 5.5 Check 4: `_LOCAL_SWAP_BACKENDS` frozenset

Assert `_LOCAL_SWAP_BACKENDS` in `proxy.py` equals exactly `{"kobold", "tabby"}`. A third
managed engine forces an explicit allowlist edit + review, not a silent branch.

### 5.6 Check 5: primitive vocabulary stays closed

Assert the set of primitive `kind`s in `transforms.py` equals the documented fixed set:
```
{"value-map", "drop-params", "default-params", "param-map", "clamp", "message-inject"}
```

A new `kind` is allowed only via a reviewed allowlist edit. New agnostic primitives are welcome;
they must be *general*, not engine-named (check 2 already polices that).

### 5.7 Why static-allowlist over a runtime hook

A runtime plugin/hook system to "enforce" this would itself be the machinery we're refusing to
build. A static test is zero-runtime-cost, runs in the existing GREEN suite, and puts the
friction precisely where treadmill-creep happens: at the moment someone types `if backend ==`.
It cannot be bypassed without an obvious, reviewable allowlist edit.

### 5.8 Standing review rule (human layer)

In `CONTRIBUTING`/PR template: *"Does this PR add engine-specific knowledge? If yes, it must be
a DATA row (transform/preset), not code. If you believe it's irreducible, it goes in a reviewed
named built-in attached by id via `register_builtin` — and you must edit the boundary test's
allowlist, which is a flag for the reviewer. See docs/design/ENGINE_KNOWLEDGE_BOUNDARY.md."*

---

## Appendix — verified code anchors (2026-06-16)

**Historical anchors; status superseded 2026-10-10:** The D-1/D-2/D-3 names,
two auto-fire sites and pending cuts below describe the earlier revision.
The intermediate owner audit found one remaining automatic D-1 application.
The current migration removes it; D-1, D-2 and D-3 are complete.

- Fixed primitive set + `register_builtin` + pipelines: `gateway/transforms.py:460-519`.
- Tier-1 packs (orphaned, attached to nothing): `gateway/transforms.py:968-1034`.
- `voxtral-voices` definition: `gateway/transforms.py:887`; only attachment: `proxy.py:3721`
  (speech path) with `VOICE_MAP` duplicate at `proxy.py:2665`.
- `qwen3-thinking` built-in class: `gateway/transforms.py:567-622`.
- `qwen3-thinking` auto-fire sites (historical, removed):
  `proxy.py:3217` (early path) and `proxy.py:18896` (dispatch site).
- `apply_thinking_suppression` (shared helper): `gateway/transforms.py:533` — called only by
  `Qwen3ThinkingTransform`, NOT by background-removal (which uses the string constant directly).
- Background-removal thinking suppression (inline, not shared): `proxy.py:5186, 5203`.
- `_parser_family` engine-flag map (pending D-3 cut): `gateway/toolcall_detection.py:140`.
- `advisory_for_backend` per-engine branches (pending D-3 cut): `gateway/toolcall_detection.py:97`.
- Honesty-law H1 passthrough (carries `config.transforms` with no schema change):
  `gateway/service_registry.py:453` (normalize) + `:843` (form).
- `_RUNTIME_PROMOTED_FIELDS` (does not — and need not — list `transforms`):
  `gateway/service_registry.py:231`; `config` copied verbatim at `:246`.
- Dispatch site missing per-backend transforms (the one plumbing gap):
  `proxy.py:19064-19108` (`_forward_litellm_proxy_request`).
- `PROVIDER_PRESETS` (gain optional `transforms`): `gateway/service_registry.py:503`.
- Spec-repairs (always-on, agnostic): `gateway/toolcall_detection.py:303,343`;
  `gateway/transforms.py:717,745`.
- `gateway/extensions.py`: workflow-input **validator** only — no `on_request`/`on_response`
  mutation hooks. Not a plugin system.
- `extensions/bundled/`: workflow/adapter YAML packages (ComfyUI workflows, extension
  manifests) — not Python request-mutation code.
- The **only** code escape hatch for request/response mutation: `REGISTRY.register_builtin`
  in `gateway/transforms.py`, exposing `on_request(body, ctx)` / `on_response(payload, ctx)`.
- Local-swap infra (Bucket B, freeze the set): `proxy.py:591` + ~26 `tabby`/`kobold` sites.

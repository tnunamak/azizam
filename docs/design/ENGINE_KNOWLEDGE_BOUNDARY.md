# Engine-Knowledge Boundary

**Status:** Design (architect's recommendation). Operationalizes the owner's binding principle.
**Last verified against code:** 2026-06-16, branch `ideogram4-quality-tuning`.

**Status update — 2026-10-10:** The owner-supplied audit at `fdc3c9d`
supersedes the D-1/D-2/D-3 implementation status and old code anchors below.
The engine-named classes are now generic transforms. D-2's engine framing is
removed. D-3 is complete. D-1 is resolved: `reasoning-effort` runs only when attached in service
`config.transforms`. Admin schema v5 attaches it once to enabled local chat/
completion endpoints, excluding remote providers and the execution substrate.
Disabled services remain unchanged. Fresh bundled chat seeds declare the id as
data; later operator removal survives startup. The built-in runs after model/
alias defaults and retains configured budgets and validation errors.
The binding principle is unchanged.

---

## The binding principle (restated, so this doc is self-contained)

The gateway must **not** be in the business of tracking inference engines **in code**.
Engines churn (~2yr half-life); the OpenAI/Anthropic/ElevenLabs-style contracts the
gateway fronts are stable. The gateway's durable value is engine-**agnostic**: a stable
OpenAI-compatible front door (auth, profiles, routing, honest observability) over whatever
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

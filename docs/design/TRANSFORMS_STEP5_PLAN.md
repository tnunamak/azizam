# Transforms migration — Sonnet execution plan (Steps 2–5)

**Handoff plan.** Self-contained: a Sonnet agent should be able to execute this end-to-end
and a reviewer can gate the result against the acceptance checks. Builds on the shipped
transform engine (`gateway/transforms.py`) and golden-replay harness
(`tests/test_transforms_golden.py`). Binding design: `docs/design/ENGINE_TRANSFORMS_DESIGN.md` §7.
Evidence base: `docs/design/ENGINE_QUIRK_FINDINGS.md`.

## Non-negotiable invariants (apply to EVERY step)

1. **Byte-identical for the current deployment.** Every step replaces a hardcoded branch with a
   transform that produces the *same* request/response bytes for the current config. The ONE
   exception is the explicitly-new cross-chunk `</think>` split in Step 4, which ships opt-in.
2. **Golden-replay gates each step.** Extend `tests/test_transforms_golden.py` so each migrated
   branch has a before/after parity test proving the transform path == the legacy path for the
   full input matrix. A step is not done until its parity test passes.
3. **Baseline: `./venv/bin/python -m pytest -q` GREEN (currently 1786 passed).** Keep green; report
   the count after each step. `cd web && npm run check` exit 0 if web touched (it isn't here).
4. **Python is ALWAYS `./venv/bin/python`.** Never touch ports 5000/15000. Never print secrets.
   Do NOT commit, do NOT restart the systemd service — the reviewer commits + restarts per step.
5. **Delete a hardcoded constant ONLY in the step that replaces it, ONLY after its parity test
   passes.** Keep `VOICE_MAP`/`_REASONING_EFFORT_*` as data sources where still read elsewhere.
6. **No `kind: plugin`.** If a behavior exceeds the 6 primitives, it is a *named built-in*
   (composed, reviewed code registered by id) — never an arbitrary-code door.

## Engine API you build on (already shipped)

- `gateway/transforms.py`: 6 primitives (`value-map`, `drop-params`, `default-params`,
  `param-map`, `message-inject` [modes: prepend/append/append-or-insert], `clamp`), each with
  shared `phase: request|response` and `when:` predicate (ops: present/absent/equals/in/
  non_empty/gt/lt, dotted-path fields).
- `TransformRegistry` (`REGISTRY` singleton, `.register(name, cfg)`), `run_request_pipeline(ids,
  body, ctx)`, `run_response_pipeline(ids, payload, ctx)`.
- Built-ins that are more than one primitive are implemented as a small registered class /
  composed pipeline with an id; `message-inject` already reproduces the index-0 system insert.

---

## STEP 2 — reasoning-effort → `qwen3-thinking` built-in (highest-value migration)

**Target:** `apply_chat_completions_reasoning_effort()` (`proxy.py:5242`), dispatched at
`proxy.py:2985` for `/v1/chat/completions`. Constants `_REASONING_EFFORT_VALUES` (`:5235`),
`_REASONING_EFFORT_SUPPRESS` (`:5239`).

**Exact current contract — reproduce BYTE-FOR-BYTE:**
1. If `reasoning_effort` not in body → no-op, return None.
2. Pop `reasoning_effort`. If not a str OR not in `{none,minimal,low,medium,high,xhigh}` →
   return the 400 message string `f"Invalid value for 'reasoning_effort': {effort!r}. Expected
   one of: {sorted(_REASONING_EFFORT_VALUES)}."` (the dispatch at :2985 turns a returned string
   into a 400 Response — preserve that exact flow and status/shape).
3. If effort NOT in `{none, minimal}` → it's a valid high-thinking value: pop it (already popped)
   and return None (no suppression, no error). I.e. low/medium/high/xhigh just strip the param.
4. If effort in `{none, minimal}` (suppress): `body.setdefault("chat_template_kwargs", {})` then
   `kwargs.setdefault("enable_thinking", False)` (do NOT overwrite an existing value); AND inject
   `/no_think` as a system message — if a system message already exists, return None WITHOUT
   injecting (current code: it scans, and if it finds a system role it returns None early —
   VERIFY this exact behavior at :5285-5292 and reproduce it precisely; the message-inject
   `append-or-insert` mode may need a variant "insert-only-if-no-system" — check and match the
   legacy behavior exactly, do not "improve" it). Else `messages.insert(0, {"role":"system",
   "content":"/no_think"})`.

**Implementation:**
- Register a `qwen3-thinking` built-in (composed: the pop+validate+400 is control flow that must
  stay in the request handler OR be expressed as the built-in returning an error sentinel —
  decide and keep the 400 path identical). The body mutation (chat_template_kwargs +
  message-inject) goes through the transform engine. The validation/400 is the one piece that may
  stay as a thin guard calling the built-in; that's fine — the GOAL is the body mutation becomes
  declarative + the constant lives with the transform, not that the 400 plumbing moves.
- Replace the dispatch at `:2985` so the body mutation runs via the registry, the 400 contract is
  preserved exactly.

**Parity test (gate):** the full matrix — `none, minimal` (suppress: assert chat_template_kwargs
.enable_thinking False set via setdefault [not overwriting a pre-set True], /no_think injected
iff no system message, NOT injected if system message present), `low/medium/high/xhigh` (param
stripped, no suppression), `invalid`/non-str (identical 400 message + status), absent (no-op).
Assert the post-transform body is byte-identical to the legacy `apply_chat_completions_reasoning_
effort` output for every case. This is THE proof.

**Acceptance:** parity test green for all 8+ cases; 400 shape identical; suite green.

---

## STEP 3 — inline `/no_think` in background-removal helper → reuse `qwen3-thinking`

**Target:** the hardcoded `/no_think\n` system message + `chat_template_kwargs:{enable_thinking:
False}` literal in the background-removal helper (`proxy.py:~4864` and `~4881`).

**Implementation:** route that internal request construction through the same `qwen3-thinking`
built-in (or its body-mutation core) instead of the inline literals, so there's one source of
truth. Behavior-identical — the constructed body must be byte-identical to today's.

**Parity test:** the background-removal request body before/after == identical. **Acceptance:**
identical body; suite green.

---

## STEP 4 — egress reasoning normalize → `reasoning-field-normalize` (+ opt-in `streaming-think-split`)

**Targets:** `_normalize_chat_completion_visible_content()` (`proxy.py:18938`, the already-shipped,
NOT-capability-gated detect-and-normalize that promotes leaked `<think>` → `reasoning_content` and
backfills empty content via `_split_reasoning_visible_text` at `:18923`), and the per-chunk
`_normalize_chat_completion_sse_chunk()` (`:19002`, which JSON-parses each `data:` line
independently — so a `</think>` split across two SSE chunks is NOT caught today).

**Implementation (two parts, different risk):**
- **4a (consolidate, byte-identical):** port the existing non-streaming normalize logic into a
  registered `reasoning-field-normalize` response-phase built-in (`phase: response`). The canonical
  emitted field set must be EXACTLY what the gateway emits today — capture current output first,
  assert identical. This is a refactor: the live behavior at :18938 does not change for any client.
  Keep it DEFAULT-ON / self-healing (detect-and-normalize, per design Revision D) — it inspects the
  payload and acts when the canonical shape is absent, regardless of any capability flag.
- **4b (the ONE new capability, opt-in, replay-gated):** a `streaming-think-split` built-in with a
  bounded cross-chunk buffer (buffer only enough to span the longest delimiter `</think>`, NOT the
  whole reasoning phase) that catches a `</think>` split across SSE chunks — the gap proven at
  :19002. This is NEW behavior, so it ships OPT-IN (attached only when configured), and a test
  proves: (i) attached → a deliberately chunk-split `</think>` is correctly normalized; (ii) NOT
  attached → current per-chunk behavior is byte-identical (no regression).

**Parity test (gate):** 4a — a corpus of real-ish responses (leaked `<think>`, empty content +
reasoning_content, clean responses) through the built-in == identical to the legacy :18938 output.
4b — the split-delimiter case works when on, no-op when off. **Acceptance:** 4a byte-identical for
all clients; 4b new-capability works + off-by-default-no-regression; suite green.

---

## STEP 5 — per-backend Tier-1 packs + suggested-on-add (additive, lowest risk, do LAST)

**Goal:** ship the high-frequency Tier-1 transform instances from the audit as ready-to-attach
config, so adding a known engine suggests the right pack. NO behavior change for the current
config (these attach to NEW backends or are opt-in).

**Deliver as registered named transforms (data only, from ENGINE_QUIRK_FINDINGS.md):**
- `ollama-unsupported-params` (drop-params: the params Ollama rejects).
- `koboldcpp-null-strip` (drop-params: null-valued params Kobold chokes on).
- `llamacpp-cache-prompt` (default-params: `cache_prompt` true).
- `mistral-penalty-clamp` (clamp: frequency/presence_penalty to Mistral's range).
Each with a unit test (applies correctly, no-op when `when:` unmet). Do NOT auto-attach to existing
services — these are suggested-on-add (the UI wiring for suggestion is a separate later increment;
this step just ships the named transforms + tests so they EXIST and are correct).

**Acceptance:** each transform unit-tested; none alters the current 8-service config behavior; suite
green.

---

## Out of scope for this plan (named honestly, not silently dropped)

- Tool-call family extractors (~6) and the gpt-oss Harmony stateful demux — these are
  `stateful_stream` built-ins, higher-risk, their own increment (ENGINE_TRANSFORMS_DESIGN.md §3).
- Cross-turn DeepSeek V3.x reasoning passback — documented limitation, not built this pass.
- UI for attaching/suggesting transforms per service — a web increment after the engine side is
  proven.

## Reviewer gate checklist (what I verify before committing each step)

- [ ] Parity test exists and passes for the migrated branch (byte-identical, full input matrix).
- [ ] The hardcoded constant/branch is removed ONLY in its step, ONLY after parity passes.
- [ ] `./venv/bin/python -m pytest -q` green; report count vs 1786 baseline.
- [ ] I read the migrated logic myself and confirm the legacy contract (esp. Step 2's 400 path and
      the no-inject-if-system-present nuance) is reproduced exactly, not "improved."
- [ ] Live smoke after restart: chat 200, tts voices 200, a reasoning_effort request behaves as
      before.

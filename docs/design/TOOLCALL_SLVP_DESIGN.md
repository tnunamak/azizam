# Tool-Call Handling — SLVP Design (detect-and-advise, no parsers)

**Status:** design only, no code changes.
**Date:** 2026-06-16.
**Supersedes:** `docs/design/TOOLCALL_EXTRACTION_DECISION.md` (the "KEEP the six
extractors" memo — see §9, *Reversing the prior decision*, for the falsifying
finding).
**Confidence in the verdict:** >95%. The single adversarial hinge that the prior
KEEP rested on has been falsified against primary source this session.

---

## 0. TL;DR (the verdict, stated plainly)

The gateway's entire tool-call responsibility is **DETECT-AND-ADVISE plus light
STRUCTURAL NORMALIZATION**. It carries **no tool-call text parsers** and adds
**no parsing dependency**.

1. **Detect.** When a request carried `tools` and the response/stream comes back
   with a recognizable tool-call marker in `content` but `tool_calls` empty, the
   gateway flags a backend misconfiguration. Detection uses a ~7-entry table of
   *marker substrings* (`<tool_call>`, `[TOOL_CALLS]`, `<|python_tag|>`, …). A
   marker-presence table is **not a parser** — it answers "does this look
   unlifted?" not "what are the calls?" (argued honestly in §3.3).
2. **Advise.** The gateway knows which backend produced the response (it is in
   the service registry). It emits the *exact* remediation for that backend
   (`--tool-call-parser hermes` for vLLM, `--jinja` for llama.cpp, etc.) via a
   structured `x-gateway-toolcall-advisory` response header + a one-time admin
   notice + a WARN log. It never mutates the response body.
3. **Normalize (structural, not parsing).** For backends that *do* lift but
   slightly off-spec — TabbyAPI returns `arguments` as a dict not a JSON string;
   Ollama streaming sets `finish_reason=stop` instead of `tool_calls` — the
   gateway applies tiny structural repairs on **already-structured** `tool_calls`.
   This is fixing the shape of lifted output, not extracting calls from text.
4. **Delete.** The six hand-rolled extractors (`HermesToolCallExtractor` …
   `Llama3ToolCallExtractor`) and `tests/test_tool_extractors.py` are deleted.
   The transform engine, its six primitives, and the legitimate named built-ins
   (voice-map, qwen3-thinking, reasoning-field-normalize, the penalty clamps,
   null-strip) stay.

If a genuinely-stuck backend ever re-emerges (none exists today), the **only**
constraint-honoring fallback is an opt-in `transformers.parse_response` path
(depend on HuggingFace, off by default) — never our own parsers. This is
deferred, not built (§8).

---

## 1. Why this memo exists

The prior memo (`TOOLCALL_EXTRACTION_DECISION.md`, same date) concluded **KEEP**
the six extractors as an opt-in compensating control. The owner has since
rejected, as a *functional requirement*, that the product hand-maintain
per-model tool-call parsers. This memo pressure-tests the opposing thesis —

> "Because every popular backend already lifts tool calls (by default or with one
> flag), the gateway's tool-call job is DETECT-AND-ADVISE; carry no parsers, add
> no parsing dependency, delete the six extractors."

— adversarially, and then specifies the SLVP. The bar: I should be able to reject
a sloppy version of this design.

---

## 2. Established facts (primary-source verified this session)

These are not re-litigated below; the design builds on them.

| Backend | Default tool-call behavior | What lifts structured `tool_calls` | Source |
|---|---|---|---|
| **Ollama** | LIFTS | none (default) | ollama tool-support blog |
| **LM Studio** | LIFTS | none (since v0.3.5) | LM Studio changelog |
| **TGI** | LIFTS (via outlines) | none | HF TGI Messages API |
| **TabbyAPI / ExLlamaV2** | LIFTS, but `arguments` as **dict** not JSON string | none (needs args-stringify *normalize*) | tabby `chat_completion.py` + audit |
| **KoboldCpp** | **LIFTS** via `repack_toolcall_tags()` → OpenAI-shape `delta.tool_calls` | none (default) | koboldcpp.py `concedo`, verified §9 |
| **vLLM** | RAW TEXT in `content` | `--tool-call-parser <fam>` + `--enable-auto-tool-choice` | docs.vllm.ai tool_calling |
| **llama.cpp / llama-server** | RAW TEXT | `--jinja` | ggml-org function-calling.md |
| **SGLang** | RAW TEXT | `--tool-call-parser <name>` | docs.sglang.io tool_parser |
| **llama-cpp-python server** | RAW TEXT | `--chat_format chatml-function-calling` (or functionary) | llama-cpp-python README |

**Consequence:** every popular backend can produce structured `tool_calls` —
either by default (Ollama, LM Studio, TGI, TabbyAPI, **KoboldCpp**) or with one
documented flag (vLLM, llama.cpp, SGLang, llama-cpp-python). There is **no
popular backend that genuinely cannot lift.** The "no fix exists" KoboldCpp case
that the KEEP decision leaned on is gone (§9).

Other verified facts carried in:

- **No standalone maintained PyPI library** does cross-family tool-call *text*
  extraction. vLLM/SGLang parsers are engine-coupled (`pip install vllm` ≈ 8.9GB
  + torch; importing one parser loads ~4922 modules). Not usable as a light lib.
- `transformers.parse_response` works torch-free **but** needs a per-model
  `response_schema` that almost no model ships (Qwen/Hermes/Mistral/DeepSeek
  checked — none ship it), and `transformers` is **not** a current gateway
  dependency (adding it ≈ 151MB).
- The gateway currently has **zero** tools-aware request handling and **zero**
  tool-call detection.
- The six extractors (`gateway/transforms.py`, committed `15e7658`) are **dark**:
  registered in `REGISTRY` but never auto-attached to any backend, so they alter
  no current response. Their only dependents are the registry registrations and
  `tests/test_tool_extractors.py` (497 lines).

---

## 3. Adversarial validation — trying to break "detect-and-advise is sufficient"

I attacked the thesis on every angle that could force us to keep a parser. None
survives.

### 3.1 "Is there a backend the user CANNOT fix at the backend?"

This is the strongest possible attack, because if even one popular backend
genuinely can't lift, detect-and-advise degrades to "you're broken and there's
nothing you can do," and a parser becomes a real feature.

**Result: no such popular backend exists today.**

- The KEEP memo named exactly one — **KoboldCpp** ("RAW TEXT, no parser exists,
  no fix available at all," ~80% confidence, issue #1448, Mar 2025). I
  re-verified against the current `concedo`-branch `koboldcpp.py`: it now has
  `repack_toolcall_tags(text, original_tools)` that strips think blocks, scans a
  `tool_call_pairs` table (with family-specific handlers incl. `parse_glm`,
  `parse_gpt_oss`), and emits OpenAI-shape `tool_calls` — streamed as
  `delta.tool_calls` chunks with `id` / `type:"function"` / `function.name` /
  `function.arguments`. **KoboldCpp lifts.** The "stuck" case is gone.
- Every other popular backend either lifts by default or has a documented flag
  (§2). A flag the user controls is, by definition, fixable by the user.
- **Hosted / locked backend the user doesn't control?** If a user points the
  gateway at a remote endpoint they can't reconfigure, that endpoint is almost
  certainly an OpenAI-compatible API that *already lifts* (that is the entire
  reason to expose `/v1/chat/completions`). A remote endpoint that returns
  unlifted family text *and* that the user cannot reconfigure is not a real
  member of the r/LocalLLaMA / r/selfhosted audience; it is a hypothetical. The
  honest answer to "could this exist?" is "yes, in principle" — and the honest
  response to it is the deferred opt-in `transformers` fallback (§8), **not** our
  own parsers. The constraint forbids hand-maintained parsers even for this case.

**Verdict on 3.1:** the premise of detect-and-advise — "the fix lives at the
backend the user controls" — holds for 100% of the popular set. The KEEP
decision's one counterexample was a stale fact.

### 3.2 Streaming: can we detect mid-stream, or only post-hoc?

When tools are requested and the backend doesn't lift, the raw family text
streams as `delta.content` chunks. Question: can the gateway DETECT this to
advise, and does detect-and-advise degrade acceptably for streaming?

- **Detection is feasible mid-stream and does not require buffering.** The
  gateway already proxies the SSE byte stream. It maintains a tiny per-request
  detector: a rolling tail of the concatenated `delta.content` (bounded to the
  longest marker length, ~16 chars) plus a "tools were requested" flag from the
  request. The moment the rolling window contains a known marker substring
  (`<tool_call>`, `[TOOL_CALLS]`, `<|python_tag|>`, `Action:` + fenced json,
  ` ```tool_code`), it sets a `detected` flag. It **passes every byte through
  unchanged** — it never rewrites a delta. At stream end (or `[DONE]`), if
  `detected and no delta ever carried tool_calls`, it emits the advisory via
  header-equivalent trailer / log / admin notice (§5.4 covers the
  headers-already-sent constraint for streaming).
- **It degrades acceptably.** The thesis never promised to *fix* streaming — it
  promised to *tell the user the exact fix*. Detection is cheap and reliable;
  the advisory is emitted at stream close. The user's client still sees the raw
  family text (exactly as it would today, since the gateway has zero tool-call
  handling now), but the operator gets an unmissable "your vLLM is missing
  `--tool-call-parser hermes`" notice. That is a strict improvement over the
  status quo with zero risk of corrupting a stream.
- **Crucially, we are NOT parsing the stream.** A parser would have to buffer,
  correlate split markers across chunks, and re-emit synthetic `tool_calls`
  deltas — the exact stateful-streaming-rewrite class the engine-quirk audit
  flagged as "honestly not declarable." Detection sidesteps all of it: a
  substring match on a bounded rolling tail is O(1) per chunk and cannot corrupt
  output because it writes nothing.

**Verdict on 3.2:** streaming detection is sound and cheap; streaming *parsing*
is the thing we are right to refuse.

### 3.3 Is a marker-detection table a "parser" in disguise? (the honest line)

The owner's constraint is "no hand-maintained per-model tool-call **parsers**."
A detection table is a list of ~7 marker substrings. Is maintaining it a
violation?

**No — and the distinction is principled, not a dodge.** A parser must:

1. locate the marker, **and**
2. extract the structured payload (find boundaries, JSON-decode, handle
   Hermes-vs-Qwen body disambiguation, handle Llama `; `-separated multi-calls,
   `ast`-parse Gemma python expressions, map `parameters` vs `arguments` keys),
   **and**
3. mutate the response (set `tool_calls`, blank `content`, flip `finish_reason`).

A detector does **only (1)**: "is a known marker substring present?" It is a
membership test. Concretely, the deleted `HermesToolCallExtractor` is ~50 lines
that JSON-decode, disambiguate Qwen XML bodies, strip consumed spans, and rebuild
content; the detector's Hermes entry is the literal string `"<tool_call>"`.

Maintenance cost asymmetry makes the line real:

- **Detector** breaks only if a family invents a *brand-new opening marker*.
  Markers are extremely stable (they are special tokens baked into chat
  templates). A new family = one new string. No JSON-shape, key-name, or
  separator knowledge. No false-extract risk, because it extracts nothing.
- **Parser** breaks on any change to body JSON shape, key names
  (`arguments`/`parameters`/`tool_name`), separators, wrapping, or streaming
  tokenization — and a *wrong* parse silently corrupts a correct response. This
  is precisely the per-model maintenance burden the owner rejected.

The detection table is also intentionally **conservative and consequence-free**:
a false positive (prose that contains `<tool_call>`) only produces an *advisory*
("looks like an unlifted tool call — if this was prose, ignore"), never a body
mutation. Over-detection is recoverable; the extractors' over-*extraction* was
not. So a slightly loose detector is safe in a way a slightly loose parser never
is.

**Verdict on 3.3:** the marker-detection table is detection, not parsing, and is
within the constraint. We will say so in the code comment, with this exact
argument, so a future reader doesn't mistake it for a re-introduction of parsers.

### 3.4 Does detect-and-advise actually HELP, or just say "you're broken"?

Detect-and-advise is only worth shipping if the advice is *actionable and
specific*. "Your tool calls aren't being lifted" is nearly useless; "add
`--tool-call-parser hermes --enable-auto-tool-choice` to your vLLM launch" is a
copy-paste fix.

**The gateway has exactly the inputs needed to be specific:**

- It **knows the backend.** The service registry record carries `kind`,
  `protocol`, and (for managed backends) a provider preset id (`vllm`, `ollama`,
  `tabbyapi`, `lmstudio`, …). The routing layer already resolves which backend a
  request hit. So the advisory is keyed on a known backend id, not guessed.
- It **knows the family** from the detected marker (`<tool_call>` ⇒ hermes/qwen,
  `[TOOL_CALLS]` ⇒ mistral, `<|python_tag|>` ⇒ llama3, ` ```tool_code` ⇒ gemma).
  For vLLM/SGLang the remediation flag *takes a family argument*, so the detected
  marker lets us fill it in: `--tool-call-parser hermes` vs `mistral` vs `llama3`.

This yields a precise **backend × family → remediation** message (full table in
§5.3). Example, vLLM hitting Hermes text:

> Detected Hermes-format tool calls in `content` but `tool_calls` is empty. This
> vLLM backend is missing tool-call parsing. Restart vLLM with:
> `--enable-auto-tool-choice --tool-call-parser hermes`.
> Docs: docs.vllm.ai/en/latest/features/tool_calling.html

That is a strictly better outcome than the status quo (silent breakage) **and**
better than what a parser would give the user: a parser papers over the
misconfiguration, so the user runs a slow path forever and never learns to fix
their stack. Advise teaches the fix; parse hides the bug. For a self-hosted
audience that values understanding their stack, advise is the *better product*,
not merely the cheaper one.

### 3.5 The TabbyAPI / Ollama quirks — normalization, not parsing

Two real, verified quirks are sometimes conflated with "the gateway must parse":

- **TabbyAPI args-as-dict.** Tabby lifts `tool_calls` correctly but returns
  `function.arguments` as a Python **dict** rather than the spec-required JSON
  **string**. Fix = `json.dumps` the dict. (Tabby is one of Tim's live backends.)
- **Ollama streaming `finish_reason` bug.** Ollama emits `finish_reason=stop`
  (not `tool_calls`) on the final SSE chunk when tool calls are present (issues
  #9632 / #12557, through ~0.12.3). Fix = if the assembled message has
  `tool_calls` and `finish_reason==stop`, set it to `tool_calls`.

Both operate on **already-structured `tool_calls`** that the backend *already
lifted*. There is no text marker, no JSON-from-prose decode, no family-format
knowledge. They are field-shape repairs — the same category as the existing
`reasoning-field-normalize` built-in (which renames `reasoning_content`↔`content`
shapes). They are **in scope, cheap, and not parsers** (§6).

### 3.6 The legitimate "I truly can't reconfigure" user

Could a user *legitimately* want the gateway to parse? Real but vanishingly rare:
a backend that emits unlifted family text and that the user genuinely cannot
relaunch with a flag. Today, no such popular backend exists (§3.1). If one
emerges, the constraint is absolute: **no hand-maintained parsers.** The only
constraint-honoring answer is an **opt-in** path built on
`transformers.parse_response` (HF maintains the per-model schema, not us), off by
default, documented as a fallback. This is **deferred** (§8) — we do not build it
speculatively, because (a) nothing needs it today and (b) almost no model ships
the `response_schema` it requires, so it would parse almost nothing right now.

### 3.7 Summary of the adversarial pass

| Attack | Outcome |
|---|---|
| A popular backend that can't lift | **None exists.** KoboldCpp now lifts; all others lift-or-flag. |
| Streaming can't be detected | **False.** Bounded rolling-tail substring match; pass-through; advise at close. |
| Detection table is a parser | **No.** Membership test vs extract+mutate; consequence-free false positives. |
| Advice is useless | **No.** Gateway knows backend + family ⇒ exact flag, copy-paste. |
| Tabby/Ollama force parsing | **No.** Structural repair of already-lifted output. |
| A user truly can't reconfigure | **Rare/hypothetical.** Deferred opt-in `transformers` path, never our parsers. |

**No attack found a hole that requires a hand-maintained parser.** The thesis
holds.

---

## 4. Prior-art validation (this is the category norm, not a bespoke bet)

The strongest non-self confirmation is that the mature gateways in this category
**also do not carry per-family response-text parsers.**

- **LiteLLM (the closest analog — an OpenAI-compatible proxy/router).** Verified
  against its function-calling docs: LiteLLM keys behavior on a **capability
  registry** (`litellm.supports_function_calling(model=...)` returns true/false
  per provider+model). For models *without* native function-calling support its
  only escape hatch is `litellm.add_function_to_prompt = True` — a **request-side
  prompt injection**, not a response-side family text parser. LiteLLM does **not**
  ship Hermes/Mistral/Qwen tool-call *extractors*; it leans on the provider to
  emit structured calls and otherwise pushes the schema into the prompt. This is
  *exactly* "know the backend, lean on it, don't parse." (Our detect-and-advise
  is the natural complement: where LiteLLM silently returns false, we actively
  tell the operator the flag that flips the backend to native support.)
- **vLLM / SGLang** keep parsing **inside the engine**, opt-in by flag, precisely
  because it is tokenizer/streaming-coupled. They are the *producers* of the
  structured shape; a downstream proxy is the wrong layer to re-implement it.
- **Cloudflare AI Gateway / OpenRouter** punt: they pass the upstream
  tool-call shape through and rely on the upstream to be correct. They carry no
  cross-family text parsers either.
- **No standalone maintained library** exists to vendor (re-confirmed §2). So
  "keep the six because there's nothing better to use" is not actually a choice
  between our parsers and a clean library — it is a choice between our parsers
  and *the category-standard posture of not parsing at all.*

**Conclusion:** detect-and-advise + lean-on-backend is the **norm** for this
product category. Carrying family text parsers would make the gateway an
*outlier*, and an outlier that has signed up for permanent per-model maintenance.

---

## 5. The SLVP — detection + advisory

### 5.1 Where it hooks (response path)

The gateway already post-processes chat-completion responses (the
`reasoning-field-normalize` "Revision D" built-in runs detect-and-normalize on
the response payload, regardless of any capability flag — that is the precedent).
Tool-call detection hooks the **same response seam**:

- **Non-streaming:** after the upstream JSON is assembled and before it is
  returned, run `detect_unlifted_toolcalls(payload, request_had_tools)`.
- **Streaming:** wrap the SSE iterator with a stateful detector that watches
  `delta.content` and never mutates chunks (§5.5).

Detection runs **only when the request carried a non-empty `tools` array** (and
`tool_choice != "none"`). No tools requested ⇒ detector is a no-op ⇒ zero cost
and zero false positives on ordinary creative-writing responses. This single gate
is what makes a loose detector safe.

### 5.2 What it inspects (the detection predicate)

A response choice is flagged when **all** hold:

1. the request carried `tools` (and `tool_choice` was `auto`/unset, not `none`);
2. the assembled assistant message has **empty/absent `tool_calls`**; **and**
3. `message.content` (non-streaming) or the concatenated `delta.content`
   (streaming) **contains a known marker substring**.

The **marker set** (detection only — substrings, no structure):

| Family marker substring | Implies family | Source token |
|---|---|---|
| `<tool_call>` | Hermes or Qwen-XML | vLLM hermes/qwen parsers, model chat templates |
| `[TOOL_CALLS]` | Mistral | vLLM `mistral_tool_parser` bot_token |
| `<|python_tag|>` | Llama-3 | vLLM `llama_tool_parser` bot_token |
| ` ```tool_code` | Gemma | Gemma function-calling convention |
| `Action:` immediately followed by a ` ```json` fence | Command-R (classic) | Cohere prompting docs |

Five entries (Hermes and Qwen share `<tool_call>`; the advisory disambiguates via
"hermes or qwen" when only the opening marker is seen). This is the entire
table. It is data, lives next to the advisory map, and is the *only* per-family
knowledge the gateway retains — and it is *detection* knowledge, not *extraction*
knowledge (§3.3).

### 5.3 The advisory map (backend × family → remediation)

Keyed on the backend's provider-preset id (from the service registry) and the
detected family. Stored as a small static table beside the marker set.

| Backend (`kind`/preset) | Remediation string | Docs |
|---|---|---|
| **vllm** | `Restart vLLM with: --enable-auto-tool-choice --tool-call-parser <family>` (family from marker: `hermes`, `mistral`, `llama3`, `qwen`, …) | docs.vllm.ai/en/latest/features/tool_calling.html |
| **llama.cpp / llama-server** | `Restart llama-server with: --jinja` | github.com/ggml-org/llama.cpp docs/function-calling.md |
| **llama-cpp-python (server)** | `Start with: --chat_format chatml-function-calling` (or a functionary format) | llama-cpp-python README |
| **sglang** | `Restart SGLang with: --tool-call-parser <family>` | docs.sglang.io tool_parser |
| **ollama / lmstudio / tgi / tabbyapi / koboldcpp** | *(should already lift)* `This backend normally lifts tool calls. If you see this, check your version / model chat template supports tools.` | per-backend docs |
| **unknown / openai-compatible (custom)** | `Detected <family>-format tool calls in content but tool_calls is empty. Your backend isn't lifting tool calls. If it's vLLM/SGLang use --tool-call-parser <family>; llama.cpp use --jinja.` | generic |

Family substitution: the detected marker fills `<family>` for the flag-driven
backends. `<tool_call>` ⇒ advise `hermes` (and note "or `qwen` if this is a
Qwen3-Coder model"), `[TOOL_CALLS]` ⇒ `mistral`, `<|python_tag|>` ⇒ `llama3`.

### 5.4 How the advisory is surfaced (UX)

**Hard rule: never mutate the response body.** The client gets exactly what the
backend sent (today's behavior). The advisory is delivered out-of-band, three
ways, in increasing persistence:

1. **Response header** `x-gateway-toolcall-advisory: <remediation>` on
   non-streaming responses. Invisible to a working client, instantly visible to
   anyone debugging in curl / devtools. (Header value is single-line,
   URL/ASCII-safe; full text + docs link in the admin notice.)
2. **WARN log line**, structured: `event=toolcall.unlifted backend=<id>
   family=<fam> remediation="<string>"`. Greppable; this is what a self-hoster
   tails.
3. **One-time admin notice** surfaced in the admin UI's attention seam (the
   `derive_admin_attention` list the design already uses for operator-facing
   warnings). Deduplicated per (backend, family) so a hot path doesn't spam — it
   says "Backend `my-vllm` returned unlifted Hermes tool calls N times; fix:
   …", clears when a subsequent tools request from that backend comes back
   correctly lifted. This is the "impossible to miss" surface: the operator sees
   it in the dashboard without reading logs.

Polished-UX rationale: a working integration sees nothing (no header noise in the
body, no broken stream). A *broken* integration cannot miss it (header + log +
dashboard). And because the message is the exact flag, the operator's path from
"it's broken" to "it's fixed" is one copy-paste + one restart.

### 5.5 Streaming handling (detail)

- Wrap the upstream SSE async iterator. Maintain: `had_tools` (from request),
  `saw_tool_calls_delta` (bool), `tail` (rolling string, len ≤ longest marker),
  `detected_family` (optional).
- For each chunk: forward it **unchanged**; if it carries `delta.tool_calls`, set
  `saw_tool_calls_delta=True`; else append `delta.content` to `tail`, truncate
  `tail` to the max marker length, and if `tail` now contains a marker, set
  `detected_family`.
- On `[DONE]` / stream close: if `had_tools and detected_family and not
  saw_tool_calls_delta`, emit the advisory via **log + admin notice** (the
  response headers were already flushed at stream start, so the header channel
  isn't available mid-stream; log + admin notice carry it). Optionally a final
  SSE *comment* line (`: gateway-advisory ...`) which is spec-legal and ignored
  by compliant clients — include only if it doesn't trip strict client parsers
  (default off; behind a config flag).

No buffering of payload, no chunk rewriting, O(1) per chunk. The stream a client
receives is byte-identical to passthrough.

---

## 6. Structural normalization (in scope, separate from parsing)

Two named built-ins, both detect-and-normalize on **already-lifted**
`tool_calls`, both safe to default-on because they only fire on the off-spec
shape:

### 6.1 `toolcall-args-stringify` (TabbyAPI)

- **Detect:** any `tool_calls[].function.arguments` whose value is a `dict`
  (or non-string JSON).
- **Act:** `json.dumps(arguments, separators=(",",":"))` to the spec string.
- **Safety:** if `arguments` is already a string, no-op. Cannot corrupt — it only
  serializes a structured value to its canonical string form.

### 6.2 `toolcall-finish-reason-repair` (Ollama streaming)

- **Detect:** assembled message has non-empty `tool_calls` **and**
  `finish_reason == "stop"` (or absent).
- **Act:** set `finish_reason = "tool_calls"`.
- **Safety:** only fires when `tool_calls` are present; never invents tool_calls,
  never touches a plain `stop`.

**Why this is not parsing:** both operate exclusively on structured `tool_calls`
the backend already produced. No text, no markers, no family JSON shapes, no
key-name maps. They are the same category as the existing
`reasoning-field-normalize` built-in and the penalty-clamp built-ins — reviewed,
composed, shipped code that fixes *shape*, with the per-quirk source documented
inline. They are cheap (a few lines each) and maintainable (they key on the
OpenAI shape, which is the stable de-facto standard, not on any backend's
private format).

These attach to the relevant backends as **suggested-on-add** built-ins (the
existing per-backend pack mechanism), or default-on globally since they are
strictly idempotent on correct output. Recommendation: **default-on globally** —
they are zero-risk on spec-correct responses and remove two real, verified
foot-guns for Tim's own Tabby/Ollama stack.

---

## 7. What gets DELETED, and proof nothing else depends on it

**Delete from `gateway/transforms.py`:**

- Classes: `HermesToolCallExtractor`, `MistralToolCallExtractor`,
  `CommandRToolCallExtractor`, `QwenXmlToolCallExtractor`,
  `GemmaToolCallExtractor`, `Llama3ToolCallExtractor`, and the base
  `_ToolCallExtractor`.
- Helpers used *only* by them: `_apply_extracted`, `_new_tool_call`,
  `_scan_json_value`, `_strip_spans`, `_QWEN_TOOLCALL_RE`, `_HERMES_OPEN/CLOSE`,
  `_LLAMA_TAG`, the Mistral/Gemma/Command-R private helpers.
- The six `REGISTRY.register_builtin(...)` lines (`hermes2pro-toolcall-extract`
  … `llama3-toolcall-extract`).

**Delete the test file:** `tests/test_tool_extractors.py` (497 lines; it imports
only the six extractor ids from `REGISTRY` and tests positive/negative/malformed
extract behavior — it has no other purpose once the extractors are gone).

**Delete / supersede the docs:** `TOOLCALL_EXTRACTION_DECISION.md` (KEEP memo) is
superseded by this doc; `TOOLCALL_EXTRACTORS_REPORT.md` (the per-family
verified-formats report) is retained for historical value but marked superseded —
its verified marker substrings are exactly the inputs to the §5.2 detection table
and the §5.3 advisory family-substitution, so the research is *reused*, not
discarded.

**What stays (must NOT be touched):**

- The transform engine: `TransformContext`, the six **primitives** (`value-map`,
  `drop-params`, `default-params`, `param-map`, `message-inject`, `clamp`), the
  registry, the pipeline runner.
- Legitimate built-ins: `voxtral-voices`, `qwen3-thinking`,
  `reasoning-field-normalize`, `streaming-think-split`, `koboldcpp-null-strip`,
  the per-backend penalty/param packs.
- These have **zero dependency** on the extractor classes (the extractors are a
  self-contained `_ToolCallExtractor` subtree; nothing else imports
  `_apply_extracted`/`_new_tool_call`). Verification step in §10 confirms this by
  grep before declaring done.

**Add:**

- A detection module (marker table + advisory map + `detect_unlifted_toolcalls`
  for non-streaming and a streaming detector wrapper) — pure, dependency-free,
  ~80–120 lines incl. the two tables.
- Two structural-normalize built-ins (§6).
- Tests: detection positive/negative (incl. the tools-not-requested no-op and the
  prose-false-positive → advisory-only behavior), advisory-string mapping per
  backend, the two normalizers, and a streaming-detector test.

---

## 8. Deferred (explicitly not built now)

- **Opt-in `transformers.parse_response` fallback.** The *only* constraint-honoring
  way to actually parse, if a genuinely-stuck backend ever emerges. Off by
  default, depends on HuggingFace `transformers` (~151MB), and parses only models
  that ship a `response_schema` (almost none today). Build it **only** when a real
  stuck backend appears and a user asks. Never re-introduce hand-rolled parsers
  to fill this gap.
- **Request-side `add_function_to_prompt` analog** (LiteLLM-style) for backends
  with no tools support at all. Not needed for the popular set; revisit if a
  no-tools-support backend becomes common.
- **Auto-applying a server flag for the user.** Out of scope — the gateway can't
  restart someone's vLLM. Advise only.

---

## 9. Reversing the prior decision — the falsifying finding

The prior `TOOLCALL_EXTRACTION_DECISION.md` reached **KEEP** honestly, but on a
fact that has since changed. Its load-bearing claim was:

> "KoboldCpp → no fix exists at all" / "RAW TEXT (no parser exists), none
> available" (confidence ~80%, issue #1448, Mar 2025), used to argue
> "the gateway-as-compensating-parser role persists for 2-3+ years."

**This is falsified by current primary source.** The `concedo`-branch
`koboldcpp.py` contains `repack_toolcall_tags()` which lifts family text into
OpenAI-shape `tool_calls` and streams them as `delta.tool_calls`. KoboldCpp lifts
by default. With the one "no fix exists" backend removed, the KEEP argument's
premise — *that there is a popular backend the user cannot fix* — collapses, and
with it the case for carrying parsers.

The KEEP memo even *anticipated* its own reversal: its own confidence note flagged
"~80% on KoboldCpp specifics; couldn't confirm whether a recent KoboldCpp release
silently added lifting." It did. The owner's independent rejection of
hand-maintained parsers as a requirement and this falsified fact point the same
direction.

(The KEEP memo's other genuine contributions — the verified per-family marker
formats and the Tabby/Ollama normalization gaps — are *preserved* by this design:
the markers become the detection table, the normalizations become the two
structural built-ins. Nothing of value is lost; the parsers go.)

---

## 10. Migration plan (increment order, each independently shippable)

1. **Add structural normalizers** `toolcall-args-stringify` +
   `toolcall-finish-reason-repair` (default-on, idempotent). Ship + test first —
   pure win, removes two verified foot-guns, no dependency on the deletion.
2. **Add detection module** (marker table, advisory map,
   `detect_unlifted_toolcalls`) wired into the non-streaming response seam, gated
   on `request_had_tools`. Surfaces via header + log + admin notice (§5.4). Test.
3. **Add streaming detector** wrapper (§5.5). Test (bounded tail, pass-through,
   advise-at-close).
4. **Delete the six extractors + helpers + registrations** from
   `gateway/transforms.py` and delete `tests/test_tool_extractors.py`. Mark
   `TOOLCALL_EXTRACTION_DECISION.md` + `TOOLCALL_EXTRACTORS_REPORT.md` superseded.
5. **Verification gate (before declaring done):** `grep -rn` the repo for
   `ToolCallExtractor`, `_apply_extracted`, `_new_tool_call`,
   `*-toolcall-extract`, `toolcall.extract`, `_QWEN_TOOLCALL_RE` — expect **zero**
   hits outside the new detection module's history/comments. Run the full test
   suite. Read every file touched. Only then is it complete.

Ordering rationale: 1–3 add the replacement capability *before* 4 removes the old
one, so the tree is never in a "tool-call handling regressed" state. (Since the
extractors are dark today, removing them regresses nothing — but adding the
replacement first is the disciplined order and lets each step ship green.)

---

## 11. Confidence and what's deferred

- **Verdict confidence: >95%.** Every adversarial angle that could force a parser
  was checked against primary source and failed. The one fact the opposing (KEEP)
  case depended on — a popular backend that can't lift — is falsified. The
  category norm (LiteLLM, vLLM/SGLang-in-engine, Cloudflare/OpenRouter punt)
  independently confirms "don't carry family parsers."
- **The detection-table-is-not-a-parser argument** is the one place a skeptic
  could push back. The design answers it concretely (membership test vs
  extract-and-mutate; consequence-free false positives; one-string-per-new-family
  maintenance) and gates detection on `request_had_tools` so the table can never
  misfire on ordinary prose. I am confident it honors the constraint; if the
  owner reads "any per-family substring table" as forbidden, the detection table
  can be collapsed to a single generic heuristic (advisory wording becomes less
  specific) without changing the architecture — noted as the fallback-within-the-
  fallback.
- **Deferred:** the opt-in `transformers.parse_response` fallback (§8), built only
  if a genuinely-stuck backend emerges and a user requests it; never our own
  parsers.

**SLVP, stated once more, plainly:** detect-and-advise + two light structural
normalizers + delete the six extractors. No parsers. No parsing dependency. The
gateway's job is to *know the backend and name the fix*, not to clean up after a
misconfiguration it can teach the user to eliminate.
